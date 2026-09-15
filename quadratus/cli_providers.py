"""Subscription-backed providers that drive the vendor coding-agent CLIs.

Instead of calling a billed HTTP API, these providers shell out to the CLI the
vendor ships with its consumer subscription -- ``claude`` (Claude Pro/Max),
``codex`` (ChatGPT Plus/Pro), ``grok`` (SuperGrok / X Premium+). Each CLI is
already logged in via its own OAuth flow, so no API key is involved and usage
draws on the subscription's rate-limit window rather than per-token billing.

Scope of use
------------
Vendor terms permit this for ordinary *individual* use: you, on your own
machine, against your own subscription. Routing other people's prompts through
your credential is prohibited by every vendor here. The GUI must therefore
never be exposed publicly while a CLI backend is active; see ``gui.py``, which
refuses to enable Gradio sharing in that configuration.

Design notes
------------
* **Flag specs are declarative.** Each CLI is described by a :class:`CLISpec`
  so that a vendor renaming a flag is a one-line fix rather than a code change.
  All three specs have now been exercised against live, signed-in binaries
  (claude 2.1.269, codex-cli 0.154.0, grok 1.0.30) and carry
  ``verified=True``. A new or changed spec starts at False, and the provider
  warns on every call until someone has actually made one: a flag set read
  off a ``--help`` page is a hypothesis.
* **And the operator can fix a spec without editing Python.**
  ``QUADRATUS_CLI_BINARY_<VENDOR>`` renames the binary and
  ``QUADRATUS_CLI_ARGS_<VENDOR>`` appends arguments to every invocation. This
  exists because two of the three specs here are written from documentation
  rather than from a binary: the likeliest failure on a fresh machine is one
  wrong flag, and the difference between a config line and a patch is the
  difference between a working evening and a blocked one. ``quadratus probe``
  is how you find out which it is. The one thing an override may not do is
  undo a control the harness sends on every call -- today, codex's native
  sub-agent switch -- and an override that would is refused, not out-ordered
  (see :data:`CODEX_NATIVE_DELEGATION_FEATURES`).
* **Project access is explicit.** Fleet gives editing calls the persistent
  project and other calls fresh source copies. Vendor tool restrictions still
  apply. Copies isolate relative writes; they are not an operating-system
  sandbox against absolute paths, network tools, or malicious commands.
* **Prompt caching dominates cost.** A cold invocation carries ~20k tokens of
  agent scaffolding; within the vendor's cache window a warm one costs ~10% of
  that, and swapping the system prompt only re-creates the changed suffix
  (~3.4k). Keeping a run's invocations close together therefore matters far
  more than trimming individual prompts.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .delegation import NativeChild
from .providers import LLMProvider, ProviderError, ProviderRefusal, Turn

log = logging.getLogger(__name__)

__all__ = [
    "CLISpec",
    "CLIProvider",
    "ClaudeCLIProvider",
    "CodexCLIProvider",
    "GrokCLIProvider",
    "CLI_SPECS",
    "CODEX_NATIVE_DELEGATION_CONTROL",
    "CODEX_NATIVE_DELEGATION_FEATURES",
    "cli_provider_classes",
    "codex_override_conflicts",
    "native_delegation_mode",
    "NativeControlOverride",
]


class CLINotInstalled(RuntimeError):
    """The vendor CLI binary could not be found on PATH."""


def _extract_claude_result(stdout: str) -> str:
    """Pull the assistant text out of ``claude --output-format json``."""
    payload = json.loads(stdout)
    if payload.get("stop_reason") == "refusal":
        # A classifier decline is not an error to the CLI: it is a completed
        # turn with nothing in it. Surface it as what it is so the caller can
        # re-route rather than see "empty response".
        details = payload.get("stop_details") or {}
        category = details.get("category") if isinstance(details, dict) else None
        raise ProviderRefusal(
            "claude declined the request"
            + (f" [{category}]" if category else "")
            + ".",
            category=category,
            explanation=(details.get("explanation") if isinstance(details, dict) else None),
        )
    if payload.get("is_error"):
        raise ProviderError(f"claude reported an error: {payload.get('result', '')[:300]}")
    return payload.get("result", "") or ""


def _extract_codex_result(stdout: str) -> str:
    """Pull the final assistant message out of ``codex exec --json`` JSONL.

    Event shape captured from codex-cli 0.154.0 on 2026-09-12::

        {"type":"thread.started","thread_id":"..."}
        {"type":"item.completed","item":{"id":"item_0","type":"error","message":"..."}}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}
        {"type":"turn.completed","usage":{...}}

    Two details the earlier guess got wrong, both of which mattered:

    * The text is nested in ``item.text`` on an ``agent_message`` item, not on
      the event. Scanning the event's own keys found nothing, so every call
      fell through to the old ``or stdout.strip()`` fallback and returned the
      raw JSONL *as if it were the answer* -- which meant a run that produced
      no answer at all looked like a success. That fallback is gone: a stream
      with no ``agent_message`` is a failure and says so.
    * ``error`` items are not necessarily fatal. A routine "under-development
      features enabled" warning arrives as one on a perfectly good run, so an
      error item is only raised when no assistant message ever follows it.
    """
    answer = ""
    errors: List[str] = []
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        saw_json = True
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                answer = text
        elif kind == "error":
            message = item.get("message")
            if isinstance(message, str) and message.strip():
                errors.append(message.strip())
    if answer:
        return answer
    if errors:
        raise ProviderError(f"codex reported an error: {errors[-1][:300]}")
    if saw_json:
        raise ProviderError(
            "codex produced no assistant message. The usual cause is a model "
            "the CLI does not serve: the turn starts and ends with nothing in "
            "it. Check `codex exec --model <name>` by hand."
        )
    raise ProviderError(f"codex returned no parseable events: {stdout.strip()[:300]}")


def _extract_grok_result(stdout: str) -> str:
    """Pull the final answer out of ``grok --output-format json``.

    Without that flag the grok CLI streams an agent's narration to stdout and
    the last thing printed is whatever it happened to be saying -- which for a
    task that uses tools is a preamble, not an answer. A live session closed a
    task on 119 characters of "I'll implement this, checking the workspace"
    before the flag was added, and nothing in the run could tell that from
    success. So the envelope is parsed and a missing ``text`` raises: a task
    closed on no answer is the failure this whole extractor exists to prevent.
    """
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise ProviderError(
            f"grok did not return a JSON envelope: {stdout.strip()[:300]}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderError(f"grok returned unexpected JSON: {stdout.strip()[:300]}")
    if payload.get("error"):
        raise ProviderError(f"grok reported an error: {str(payload['error'])[:300]}")
    stop = payload.get("stopReason")
    text = payload.get("text")
    # A turn that did not reach end_turn still carries text, and that text is
    # the narration it had got to -- a denied tool comes back as
    # {"stopReason": "cancelled", "text": "I'll create proof.txt now."}. That
    # reads as a confident answer to everything downstream. Anything but a
    # completed turn is therefore an error, including a stop reason this code
    # has never seen: guessing that an unknown one is benign is how the
    # narration got mistaken for an answer in the first place.
    if stop != "end_turn":
        diagnostics = _extract_grok_diagnostics(stdout) or {}
        attempted = diagnostics.get("attempted_tools")
        raise ProviderError(
            f"grok did not complete the turn (stopReason {stop!r})"
            + (f"; attempted tools: {', '.join(attempted)}" if attempted else "")
            + (f": {text.strip()[:200]}" if isinstance(text, str) and text.strip() else ".")
        )
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("grok completed the turn with no answer text.")
    return text


#: Containers whose entries *are* tool calls by construction.
_DIAGNOSTIC_TOOL_CONTAINERS = ("toolCalls", "tool_calls", "toolUses", "tool_uses")
#: Containers that hold mixed transcript entries; an entry there counts only
#: when it says it is a tool call (``type``) or carries a tool-specific name
#: field. A bare ``name`` on a message is an author, not a tool.
_DIAGNOSTIC_MIXED_CONTAINERS = ("steps", "events", "messages", "items", "turns", "content")
_DIAGNOSTIC_TOOL_TYPES = frozenset({"tool_call", "toolcall", "tool_use", "tooluse", "function_call", "functioncall", "tool"})
_DIAGNOSTIC_TOOL_NAME_KEYS = ("toolName", "tool_name", "tool")
_DIAGNOSTIC_NAME_RE = re.compile(r"^[A-Za-z][\w.-]{0,63}$")
_MAX_DIAGNOSTIC_TOOLS = 20


def _extract_grok_diagnostics(stdout: str) -> Optional[Dict[str, object]]:
    """Bounded facts about how a grok turn ended, from its JSON envelope.

    Exactly three things, and nothing else: the stop reason, the model-call
    count, and the *names* of tools the turn attempted. No arguments, no
    paths, no URLs, no response text -- this is written into the ledger and
    published in evidence bundles, so the whitelist is the point. The
    2026-09-14 restricted-worker cancellation left no record of *what* the
    turn tried before the vendor cancelled it; the report had to say the
    trigger was not recorded. This is that record.

    The tool-call shape inside the envelope is not documented by the vendor.
    The walk therefore reads names only where the envelope says they are
    tool calls: entries of a tool-call container (``toolCalls`` and
    spellings), or entries of a mixed transcript list that carry a tool-call
    ``type`` or a tool-specific name field (``toolName``, ``tool``, or
    ``function.name``). A bare ``name`` on a message or event is an author
    or a label, possibly a person, and never a tool. A recorded name is a
    fact; an absent list is "not reported", never "no tools were called".
    """
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    diagnostics: Dict[str, object] = {}
    stop = payload.get("stopReason")
    if isinstance(stop, str) and stop.strip():
        diagnostics["stop_reason"] = stop.strip()[:40]
    calls = payload.get("modelCalls")
    if isinstance(calls, int) and not isinstance(calls, bool) and calls >= 0:
        diagnostics["model_calls"] = calls
    names: List[str] = []

    def tool_name(item, certain):
        """The tool this entry names, or None when it is not a tool call."""
        for key in _DIAGNOSTIC_TOOL_NAME_KEYS:
            if isinstance(item.get(key), str):
                return item[key]
        function = item.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return function["name"]
        kind = item.get("type")
        typed = isinstance(kind, str) and kind.strip().lower().replace("-", "_") in _DIAGNOSTIC_TOOL_TYPES
        if (certain or typed) and isinstance(item.get("name"), str):
            return item["name"]
        return None

    def record(name):
        if (isinstance(name, str) and _DIAGNOSTIC_NAME_RE.match(name)
                and name not in names and len(names) < _MAX_DIAGNOSTIC_TOOLS):
            names.append(name)

    def walk(node, depth):
        if depth > 3 or len(names) >= _MAX_DIAGNOSTIC_TOOLS or not isinstance(node, dict):
            return
        # Envelope order, so the record reads as the turn happened.
        for key, items in node.items():
            if key in _DIAGNOSTIC_TOOL_CONTAINERS:
                certain = True
            elif key in _DIAGNOSTIC_MIXED_CONTAINERS:
                certain = False
            else:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    record(tool_name(item, certain))
                    walk(item, depth + 1)

    walk(payload, 0)
    if names:
        diagnostics["attempted_tools"] = names
    return diagnostics or None


def _extract_grok_usage(stdout: str) -> Optional[Dict[str, int]]:
    """Real token counts from the grok JSON envelope.

    Cache reads and creations fold into input for the same reason they do on
    the claude side: the meter asks what this would have cost on API keys, and
    cached input is still billed input there.
    """
    try:
        usage = json.loads(stdout).get("usage") or {}
        input_tokens = (
            int(usage.get("input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        output_tokens = int(usage.get("output_tokens", 0))
    except (ValueError, TypeError, AttributeError):
        return None
    if input_tokens == 0 and output_tokens == 0:
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _extract_plain(stdout: str) -> str:
    return stdout.strip()


def _extract_codex_usage(stdout: str) -> Optional[Dict[str, int]]:
    """Token counts from ``codex exec --json``'s ``turn.completed`` event.

    Observed on codex-cli 0.154.0::

        {"type":"turn.completed","usage":{"input_tokens":20114,
         "cached_input_tokens":12928,"cache_write_input_tokens":0,
         "output_tokens":5,"reasoning_output_tokens":0}}

    ``cached_input_tokens`` is read as a *subset* of ``input_tokens`` rather
    than an addition to it, so it is not folded in -- unlike the Claude
    envelope, where cache reads are reported separately and are folded. The
    two vendors report cache differently and guessing wrong in the additive
    direction would inflate the counterfactual bill. The last turn wins: a
    multi-turn run reports per turn, and the meter records one call.
    """
    best: Optional[Dict[str, int]] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            found = {
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
            }
        except (TypeError, ValueError):
            continue
        if found["input_tokens"] or found["output_tokens"]:
            best = found
    return best


def _extract_native_children(stdout: str) -> List[NativeChild]:
    """Vendor-native sub-agents visible in a CLI's own event stream.

    The Codex CLI can spawn its own agents (``spawn_agent``), which is how a
    Sol review on 2026-09-13 created a second Sol that never passed through
    Quadratus worker selection or any per-task budget. The parent's reported
    usage covered the parent alone, so 135,105 child tokens were spent inside
    an authorised run and appeared in no total it produced.

    On codex the harness now switches this off at the CLI
    (``CODEX_SPEC.control_args``), so a child observed here is a *control
    failure* and is marked as one. The observation stays regardless: it is
    the check that the control held, and on the other two vendors it is still
    all there is. Children are keyed by session id and their usage is taken
    as the **maximum** seen, never the sum: these event streams restate the
    session's cumulative total on every update, so summing the updates
    multiplies the real figure.

    Unparseable lines are skipped rather than raising. This is accounting, and
    accounting must never fail a call.
    """
    found: Dict[str, NativeChild] = {}
    parent_id = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            parent_id = event.get("thread_id")
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "collab_tool_call":
            receivers = item.get("receiver_thread_ids") or []
            for child_id in receivers or [f"unidentified:{parent_id or 'session'}:{item.get('id', 'native')}"]:
                found[str(child_id)] = NativeChild(
                    session_id=str(child_id), parent_session_id=item.get("sender_thread_id") or parent_id,
                    tool_name=item.get("tool") or "collab_tool_call",
                    detail="native activity observed; stream does not report child usage",
                )
        name = str(event.get("name") or event.get("type") or "")
        session_id = (
            event.get("child_session")
            or event.get("child_session_id")
            or event.get("agent_session_id")
        )
        is_spawn = "spawn_agent" in name or "subagent" in name
        if not session_id and not is_spawn:
            continue
        if not session_id:
            # A spawn was observed but the child cannot be identified, so its
            # usage cannot be attributed at all. Recorded under a synthetic id
            # with unknown usage, which is the honest shape of what is known.
            session_id = f"unidentified:{name}"
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        child = NativeChild(
            session_id=str(session_id),
            model=event.get("child_model") or event.get("model"),
            parent_session_id=event.get("session_id") or event.get("parent_session_id"),
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            tool_name=name or "spawn_agent",
        )
        existing = found.get(child.session_id)
        if existing is None:
            found[child.session_id] = child
            continue
        # Cumulative restatements: keep the largest reading, never add them.
        found[child.session_id] = NativeChild(
            session_id=child.session_id,
            model=child.model or existing.model,
            parent_session_id=child.parent_session_id or existing.parent_session_id,
            input_tokens=_pick_max(existing.input_tokens, child.input_tokens),
            output_tokens=_pick_max(existing.output_tokens, child.output_tokens),
            tool_name=existing.tool_name or child.tool_name,
        )
    return list(found.values())


def _annotate_child(child: NativeChild, note: str) -> NativeChild:
    """The same observation with the control verdict prepended to its detail."""
    if note in child.detail:
        return child
    return NativeChild(
        session_id=child.session_id,
        model=child.model,
        parent_session_id=child.parent_session_id,
        input_tokens=child.input_tokens,
        output_tokens=child.output_tokens,
        tool_name=child.tool_name,
        detail=f"{note}; {child.detail}" if child.detail else note,
    )


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_max(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _extract_claude_usage(stdout: str) -> Optional[Dict[str, int]]:
    """Real token counts from the claude JSON envelope, when present.

    Cache reads and creations are folded into ``input_tokens``: the meter's
    question is what the call would have cost on API keys, and cached input is
    still billed input there (at a different rate the seed sheet does not try
    to model -- the counterfactual is deliberately the conservative one).
    """
    try:
        usage = json.loads(stdout).get("usage") or {}
        input_tokens = (
            int(usage.get("input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
        )
        output_tokens = int(usage.get("output_tokens", 0))
    except (ValueError, TypeError, AttributeError):
        return None
    if input_tokens == 0 and output_tokens == 0:
        return None
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


@dataclass
class CLISpec:
    """Declarative description of how to drive one vendor CLI."""

    #: Executable name looked up on PATH.
    binary: str
    #: Sub-command inserted before flags, e.g. ``["exec"]`` for codex.
    subcommand: List[str] = field(default_factory=list)
    #: Flag that puts the CLI in non-interactive mode. Empty if implied.
    print_flag: List[str] = field(default_factory=list)
    #: Flag accepting a full replacement system prompt. Empty if unsupported,
    #: in which case the system text is folded into the user prompt instead.
    system_flag: Optional[str] = None
    #: Flag selecting the model.
    model_flag: Optional[str] = None
    #: Extra args requesting machine-readable output.
    output_args: List[str] = field(default_factory=list)
    #: Args that make the agent read-only (no file writes, no shell).
    readonly_args: List[str] = field(default_factory=list)
    #: Args added when the caller *has* opted into writes. Some CLIs need an
    #: explicit approval mode or a non-interactive run stalls waiting for a
    #: confirmation nobody is there to give.
    write_args: List[str] = field(default_factory=list)
    #: Args sent on every invocation regardless of mode.
    always_args: List[str] = field(default_factory=list)
    #: Flags that turn one call into a bounded, read-only one instead of an
    #: agent loop. Empty means the vendor offers no such mode and every seat
    #: gets the full agent.
    restricted_args: List[str] = field(default_factory=list)
    #: Flags an agentic seat needs and a restricted one must not get. Distinct
    #: from ``always_args``, which is genuinely unconditional: codex's
    #: ``--skip-git-repo-check`` has to survive into a restricted call, while
    #: grok's ``--always-approve`` is the very thing being withheld.
    agentic_args: List[str] = field(default_factory=list)
    #: Flags that hold a harness invariant on *every* call, whatever the seat,
    #: the permission mode or the operator's overrides say. Today this is one
    #: thing: codex is told not to spawn its own agents, so that every helper
    #: passes through WorkerPool and its budgets. Sent after the permission
    #: axis and before ``extra_args``; ``override_conflicts`` is what stops an
    #: operator override from undoing them. Empty for a vendor whose CLI
    #: offers no such switch -- which is a gap, and is documented as one.
    control_args: List[str] = field(default_factory=list)
    #: Given the operator's extra arguments, the tokens that would undo or
    #: hide ``control_args``. A non-empty answer refuses the call: an override
    #: that re-enables native delegation is a configuration error, not a
    #: preference, and a refusal cannot be mistaken for a bounded run.
    override_conflicts: Optional[Callable[[Sequence[str]], List[str]]] = None
    #: Optional run-wide denial request for other vendors' native helpers.
    #: This is not proof their CLI honors it; see the Grok specification.
    native_fanout_off_args: List[str] = field(default_factory=list)
    disallowed_tools_flag: str = ""
    #: Some CLIs only honour their tool-filtering flags when the prompt is an
    #: argument rather than a file (grok ignores them under --prompt-file, in
    #: silence). Where that is so, restricted mode must deliver the prompt in
    #: argv, and the caller pays an argv size limit for it.
    restricted_prompt_flag: str = ""
    #: The reasoning-depth dial, if the CLI has one. Two vendors spell it as
    #: a flag (``--effort low``); codex has no flag and takes it as a config
    #: override (``-c model_reasoning_effort=low``), so the value is a
    #: template rather than the bare level.
    effort_flag: str = ""
    effort_template: str = "{level}"
    #: Flag that takes a *path* to a file holding the prompt. Preferred over
    #: both argv and stdin where a CLI offers it: whole code artifacts go
    #: through here, and argv has a hard length limit.
    prompt_file_flag: Optional[str] = None
    #: Turns CLI stdout into assistant text.
    extract: Callable[[str], str] = _extract_plain
    #: Pass the prompt on stdin rather than as a positional arg. Safer for very
    #: long prompts, which can exceed the OS argv limit.
    prompt_on_stdin: bool = False
    #: Pull real token counts out of CLI stdout, as a dict with
    #: ``input_tokens``/``output_tokens`` (or None when the CLI does not report
    #: them). Measured counts beat any character estimate, so a spec that can
    #: provide this should.
    extract_usage: Optional[Callable[[str], Optional[Dict[str, int]]]] = None
    #: Bounded facts about how the turn ended (stop reason, model-call
    #: count, attempted tool names), for the ledger. None where the CLI's
    #: output carries nothing of the kind.
    extract_diagnostics: Optional[Callable[[str], Optional[Dict[str, object]]]] = None
    #: Whether these flags have been checked against a real binary.
    verified: bool = False
    #: Environment variables to set for the subprocess.
    env: Dict[str, str] = field(default_factory=dict)
    #: Vendor key, used to find this spec's environment overrides.
    vendor: str = ""

    def resolved_binary(self, env: Optional[Mapping[str, str]] = None) -> str:
        """The executable to run, after any operator override."""
        environ = os.environ if env is None else env
        override = environ.get(f"QUADRATUS_CLI_BINARY_{self.vendor.upper()}", "").strip()
        return override or self.binary

    def extra_args(self, env: Optional[Mapping[str, str]] = None) -> List[str]:
        """Operator-supplied arguments appended to every invocation.

        Shell-quoted, so a value with spaces survives. This is the escape
        hatch for a spec written from documentation: a missing
        ``--skip-git-repo-check`` or a renamed sandbox flag becomes a line in
        ``.env`` instead of a patch to this file.
        """
        environ = os.environ if env is None else env
        raw = environ.get(f"QUADRATUS_CLI_ARGS_{self.vendor.upper()}", "").strip()
        if not raw:
            return []
        try:
            return shlex.split(raw)
        except ValueError:
            log.warning("could not parse QUADRATUS_CLI_ARGS_%s=%r; ignoring it",
                        self.vendor.upper(), raw)
            return []


#: Verified against claude 2.1.228. ``--system-prompt`` fully replaces the
#: default Claude Code system prompt (it does not merely append), which keeps
#: an assigned collaboration role from competing with the coding-agent persona.
CLAUDE_SPEC = CLISpec(
    vendor="claude",
    binary="claude",
    print_flag=["-p"],
    system_flag="--system-prompt",
    model_flag="--model",
    output_args=["--output-format", "json"],
    readonly_args=["--disallowed-tools", "Bash Edit Write NotebookEdit"],
    # Levels verified on claude 2.1.269: low, medium, high, xhigh, max.
    effort_flag="--effort",
    # A worker call. Task is the expensive one -- each subagent is a fresh
    # context window billed against the same subscription -- and the write
    # tools go for the same reason they do in readonly_args. Unlike grok,
    # claude degrades gracefully when a tool is missing: measured at one turn
    # with the answer inline, rather than a cancelled turn.
    restricted_args=["--disallowed-tools", "Bash Edit Write NotebookEdit Task"],
    native_fanout_off_args=["--disallowed-tools", "Task Agent"],
    disallowed_tools_flag="--disallowed-tools",
    extract=_extract_claude_result,
    # Mandatory, not merely safer: --disallowed-tools is variadic, so a
    # positional prompt after it is swallowed as another tool name and the CLI
    # exits with "Input must be provided...". Stdin also sidesteps the argv
    # length limit, which whole code artifacts would otherwise hit.
    prompt_on_stdin=True,
    verified=True,
    extract_usage=_extract_claude_usage,
)

#: Codex feature switches that admit vendor-native sub-agents (``spawn_agent``
#: and friends). Both are governed by ``--disable <name>`` on codex-cli
#: 0.154.0, and the precedence was probed on 2026-09-15 with ``features
#: list`` (a configuration read, not a model call):
#:
#: * ``--disable multi_agent`` wins over ``--enable multi_agent``, over
#:   ``-c features.multi_agent=true`` in every spelling tried (``-c``,
#:   ``-cKEY=``, ``--config``, an inline ``features={...}`` table, spaces
#:   around ``=``), and over ``[features] multi_agent = true`` in a
#:   ``CODEX_HOME`` config file -- *regardless of argument order*.
#: * ``-c features.multi_agent=false`` alone does not: a later ``-c ...=true``
#:   wins, and ``--enable`` beats it wherever it sits. So the control is the
#:   ``--disable`` form, and the ``-c`` form is not relied on.
#: * ``multi_agent_v2`` is a separate switch (present, off by default) that
#:   disabling the first leaves alone, so both are disabled.
#: * ``--disable <unknown>`` errors ("Unknown feature flag"), whereas ``-c
#:   features.<unknown>=false`` is ignored in silence. The loud form is the
#:   one wanted here: a codex release that renames the switch fails every
#:   call and says why, rather than quietly running with spawning back on.
#:
#: Configuration proof only. ``features list`` reporting ``false`` is the
#: binary's own statement of the switch; that no child can then be spawned in
#: a live ``exec`` turn is still to be shown by a bounded probe.
CODEX_NATIVE_DELEGATION_FEATURES = ("multi_agent", "multi_agent_v2")
#: Config tables whose only purpose is to shape native sub-agents
#: (``agents.enabled``, ``agents.max_threads``, ...). The 0.154.0 binary
#: accepts ``agents.enabled`` and rejects ``agents.bogus``, so the table is
#: real; with spawning disabled any override of it is at best inert and at
#: worst an attempt to re-admit it, and both are refused.
CODEX_NATIVE_DELEGATION_TABLES = ("agents",)
#: The control itself, in the form the precedence probe showed to win.
CODEX_NATIVE_DELEGATION_CONTROL = [
    flag for name in CODEX_NATIVE_DELEGATION_FEATURES for flag in ("--disable", name)
]


def _config_override_value(tokens: Sequence[str], index: int):
    """The ``key=value`` payload of a ``-c``/``--config`` at ``index``, and
    how many tokens it spans. None when the token is not a config override."""
    token = tokens[index]
    if token in ("-c", "--config"):
        if index + 1 < len(tokens):
            return tokens[index + 1], 2
        return None, 1
    if token.startswith("--config="):
        return token[len("--config="):], 1
    if token.startswith("-c") and len(token) > 2 and not token.startswith("--"):
        # clap accepts both ``-cKEY=VAL`` and ``-c=KEY=VAL``.
        return token[2:].lstrip("="), 1
    return None, 0


def _feature_switch_value(tokens: Sequence[str], index: int):
    """The feature named by an ``--enable`` at ``index``, and its span."""
    token = tokens[index]
    if token == "--enable":
        if index + 1 < len(tokens):
            return tokens[index + 1], 2
        return None, 1
    if token.startswith("--enable="):
        return token[len("--enable="):], 1
    return None, 0


def codex_override_conflicts(args: Sequence[str]) -> List[str]:
    """Operator arguments that would undo or hide the native-delegation control.

    Three shapes, each rendered back as the tokens that caused it:

    * ``--enable <feature>`` for a feature in
      :data:`CODEX_NATIVE_DELEGATION_FEATURES`. On 0.154.0 it would *lose* to
      the ``--disable`` the harness sends, but an override whose only effect is
      to lose is a misunderstanding worth stopping on rather than a no-op.
    * A ``-c``/``--config`` override of ``features.<feature>`` to anything but
      ``false``, of the whole ``features`` table naming one, or of anything
      under an ``agents`` table. Keys are normalised by stripping quotes and
      whitespace, since TOML allows both.
    * A bare ``--``. Everything after an argument terminator is positional to
      the CLI and invisible to this check, so the terminator itself is refused.

    An agreeing override (``--disable multi_agent``, ``-c
    features.multi_agent=false``) is not a conflict; neither is anything
    unrelated, which is most of what ``QUADRATUS_CLI_ARGS_OPENAI`` is for.
    """
    tokens = list(args)
    conflicts: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--", "resume", "fork"):
            conflicts.append(token)
            index += 1
            continue
        feature, span = _feature_switch_value(tokens, index)
        if span:
            if feature in CODEX_NATIVE_DELEGATION_FEATURES:
                conflicts.append(" ".join(tokens[index:index + span]))
            index += span
            continue
        payload, span = _config_override_value(tokens, index)
        if span:
            if payload is not None and _config_override_conflicts(payload):
                # The key only. An override's value can carry private
                # configuration, and this string ends up in an error message
                # and a log.
                conflicts.append(f"{token.split('=', 1)[0][:8]} {_override_key(payload)}=…")
            index += span
            continue
        index += 1
    return conflicts


def _override_key(payload: str) -> str:
    return re.sub(r"""[\s"']""", "", payload.partition("=")[0])


def _config_override_conflicts(payload: str) -> bool:
    key, _, value = payload.partition("=")
    key = _override_key(payload)
    value = value.strip().strip("\"'").lower()
    if key in CODEX_NATIVE_DELEGATION_TABLES or any(
        key.startswith(f"{table}.") for table in CODEX_NATIVE_DELEGATION_TABLES
    ):
        return True
    for name in CODEX_NATIVE_DELEGATION_FEATURES:
        if key == f"features.{name}" and value != "false":
            return True
        if key == "features" and name in value:
            return True
    return False


#: Verified end to end against codex-cli 0.154.0 on 2026-09-12: a signed-in
#: ``codex exec`` round trip through these exact flags, with the JSONL event
#: shape captured from the run and encoded in the extractors and their tests.
#: Also verified that a model name it does not know fails loudly rather than
#: silently falling back -- it emits "Model metadata for X not found" and ends
#: the turn with no assistant message, which the extractor now reports.
#:
#: ``--skip-git-repo-check`` is not optional. Every provider runs in a scratch
#: directory by design, codex refuses to run outside a git repository without
#: it, and there is no configuration in which this harness wants the check.
#:
#: There is genuinely no system-prompt flag on ``codex exec``, so the role is
#: folded into the prompt; ``-c`` could override config instead, which is a
#: heavier mechanism for the same effect.
CODEX_SPEC = CLISpec(
    vendor="openai",
    binary="codex",
    subcommand=["exec"],
    system_flag=None,
    model_flag="--model",
    output_args=["--json"],
    readonly_args=["--sandbox", "read-only"],
    write_args=["--sandbox", "workspace-write"],
    # codex exposes no effort flag; the config override is the documented
    # route, and it is validated rather than ignored -- a deliberately
    # misspelled key is rejected as "unknown configuration field ... in
    # -c/--config override", which is how this was confirmed to take effect.
    effort_flag="-c",
    effort_template="model_reasoning_effort={level}",
    # codex has no tool allowlist: the sandbox is the whole lever, and
    # read-only is already what an ungranted seat gets. A restricted codex
    # seat is therefore the same sandbox at a lower reasoning effort -- less
    # than the other two vendors can do, and said plainly rather than
    # implied by an empty list.
    restricted_args=["--sandbox", "read-only"],
    always_args=["--skip-git-repo-check"],
    # No seat on this transport may spawn its own agents. A Sol review on
    # 2026-09-13 used the CLI's spawn_agent to create a second Sol that passed
    # through no worker selection and no budget; see delegation.py. The
    # switch is sent on every call -- read-only, writable and restricted
    # alike -- and an operator override that would undo it is refused rather
    # than out-ordered. Helpers on this vendor are Luna and Terra, dispatched
    # by WorkerPool. Configuration is checked locally; runtime enforcement
    # still needs a live probe, so observed children remain in the telemetry.
    control_args=list(CODEX_NATIVE_DELEGATION_CONTROL),
    override_conflicts=codex_override_conflicts,
    extract=_extract_codex_result,
    extract_usage=_extract_codex_usage,
    prompt_on_stdin=True,
    verified=True,
)

#: Grok Build (``brew install --cask grok-build``), authenticating against
#: SuperGrok / X Premium+. Flags checked against ``grok 1.0.30 --help`` on
#: 2026-09-12 and then confirmed by signed-in calls the same day: this spec
#: round-trips prompts and rejects an unknown model id loudly, which is the
#: pair of behaviours ``verified`` is asserting.
#:
#: The earlier spec here was wrong in a way that would have failed on the
#: first call, which is what reading a real ``--help`` is for:
#:
#: * ``-p`` is not a "print mode" switch. It is ``-p, --single <PROMPT>`` and
#:   consumes the next argument as the prompt, so pairing it with a prompt on
#:   stdin would have fed the CLI whichever flag came next.
#: * ``--prompt-file`` takes the single-turn prompt from a path, which is
#:   better than either argv or stdin here: the prompts this harness sends
#:   carry whole code artifacts, and argv has a hard length limit.
#: * There *is* a system-prompt flag -- ``--system-prompt-override`` -- so the
#:   assigned role no longer has to be folded into the user prompt, where it
#:   competes with the coding-agent persona.
#:
#: ``readonly_args`` is empty, and unlike the other two vendors that is not a
#: gap waiting to be filled -- it is what a day of experiment on 2026-09-12
#: concluded. Grok Build does not have a working read-only mode:
#:
#: * ``--always-approve`` is mandatory for tool use at all. Without it any
#:   tool call ends the turn as ``cancelled``, headlessly and with no error --
#:   ``--permission-mode acceptEdits`` does not cover it.
#: * ``--always-approve`` then overrides ``--disallowed-tools``. Asked to
#:   write a file under ``--always-approve --disallowed-tools "Bash Edit
#:   Write"``, it wrote the file and ran a shell check on it.
#: * Without ``--always-approve`` the denial holds, but the turn cancels the
#:   moment the model reaches for a tool, which it does for almost anything:
#:   even a system prompt saying "you have NO filesystem and NO tools, answer
#:   inline" did not stop it trying to create a module.
#: * ``--permission-mode plan``, the obvious candidate for read-only, cancels
#:   the same way.
#:
#: So the choice is tools-with-writes or no tools at all. Operator decision,
#: 2026-09-12: take the tools. ``--always-approve`` is therefore in
#: ``agentic_args`` rather than ``write_args`` -- it goes on every senior call,
#: including read-only ones, because a grok that cancels the moment it reaches
#: for a tool is not a worker at all, and the ROTE rung and the lookup errand
#: are both grok's.
#:
#: Be clear about what that costs, because it is not what the other two
#: vendors give: for claude a read-only call denies the write tools and for
#: codex it is a ``--sandbox read-only``, while for grok "read-only" means
#: only that the writes land in a ``mkdtemp`` scratch directory instead of the
#: working tree. Containment, not prevention, and grok alone holds the weaker
#: guarantee. Fleet supplies the real project only for granted edits; other
#: calls receive disposable source copies. On 2026-09-13 a live project trial
#: showed that adding ``--permission-mode acceptEdits`` overrides the effective
#: approval behavior: read tools succeed, but search_replace cancels. The same
#: authorized edit succeeds with ``--always-approve`` alone. Directory selection
#: and the existing per-call grant, not a second CLI flag, enforce this boundary.
#:
#: Recheck this on a Grok Build release that documents its tool names;
#: ``QUADRATUS_CLI_ARGS_GROK`` is the place to try a fix without editing
#: Python.
#: Ceiling on a prompt delivered in argv. ARG_MAX is 1 MiB on macOS and is
#: shared with the environment, so this leaves generous room rather than
#: racing the real limit -- the failure at the limit is an opaque OSError from
#: exec, and a clear refusal well short of it is worth the lost headroom.
MAX_ARGV_PROMPT = 200_000

GROK_SPEC = CLISpec(
    vendor="grok",
    binary="grok",
    system_flag="--system-prompt-override",
    model_flag="--model",
    # Without this the CLI streams the agent's narration and the "answer" is
    # whatever it was mid-sentence about; with it, one JSON envelope carrying
    # the final text and real token counts.
    output_args=["--output-format", "json"],
    # Verified levels on grok 1.0.30: low, medium, high, xhigh. Both --help
    # and the shipped README also advertise none/minimal/max; the binary
    # rejects all three. One more reason the probe outranks the table.
    effort_flag="--effort",
    # A worker call: the file-writing tools are absent rather than denied, so
    # the model answers inline instead of trying to write and cancelling.
    # Subagents are blocked because each one is a fresh context window billed
    # against the same subscription. Read-only web tools serve lookup errands;
    # their names are present in the installed 1.0.30 binary. The narrower
    # filesystem-only allowlist was live-verified; web calls need a live smoke.
    restricted_args=[
        "--tools", "read_file,grep,list_dir,web_search,web_fetch",
        "--disallowed-tools", "Agent",
    ],
    # Not a preference. --tools and --disallowed-tools are honoured only in
    # headless mode; under --prompt-file they are ignored silently and the
    # call comes back as a cancelled agent turn, which is how the restriction
    # was first thought impossible.
    restricted_prompt_flag="-p",
    readonly_args=[],
    # Cloud Claude's opt-in request. UNVERIFIED: --always-approve may
    # override this denial. No bounded Grok claim until the live probe.
    native_fanout_off_args=["--disallowed-tools", "Agent"],
    disallowed_tools_flag="--disallowed-tools",
    # Every *agentic* call, read-only included: without it the turn is
    # cancelled silently the first time a tool is called. A restricted seat
    # must not get it -- there, the write tools are absent rather than denied,
    # and approving tools that do not exist would only re-admit the loop.
    agentic_args=["--always-approve"],
    # Fleet enforces edit grants by selecting the project or a source copy.
    # acceptEdits conflicts with --always-approve and cancels headless edits.
    write_args=[],
    prompt_file_flag="--prompt-file",
    extract=_extract_grok_result,
    extract_usage=_extract_grok_usage,
    extract_diagnostics=_extract_grok_diagnostics,
    verified=True,
)

#: One spec per vendor in the lineup. Google's Antigravity spec was removed on
#: 2026-09-12 with the rest of that vendor; see quadratus/registry.py.
CLI_SPECS: Dict[str, CLISpec] = {
    "claude": CLAUDE_SPEC,
    "openai": CODEX_SPEC,
    "grok": GROK_SPEC,
}


def _launch(argv, *, input=None, timeout=None, cwd=None, env=None):  # noqa: A002
    """The one place this module executes a vendor CLI.

    ``subprocess.run`` that takes the CLI's whole process tree down on timeout.

    ``subprocess.run`` kills only the process it started. Every vendor CLI here
    is a launcher that spawns its own children -- a language runtime, MCP
    servers, native sub-agents -- and those survive the parent being killed.
    They keep holding the working tree, and on a long run they accumulate.

    So the child gets its own process group and the timeout path signals the
    group. TERM first so a CLI can flush what it has written, then KILL for
    whatever ignored it. Output collected before the timeout is preserved on
    the raised :class:`subprocess.TimeoutExpired`, because a timed-out editing
    call's partial output is exactly what the caller needs in order to decide
    whether anything was already written.

    ``start_new_session`` is POSIX; on platforms without it the call degrades
    to the old single-process behaviour rather than failing.
    """
    kwargs = {}
    if hasattr(os, "killpg") and hasattr(os, "setsid"):
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(  # noqa: S603 -- argv is built, never a shell string
        argv,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        **kwargs,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_group(proc)
        stdout, stderr = _drain_output(proc, exc)
        raise subprocess.TimeoutExpired(
            argv, timeout, output=stdout, stderr=stderr
        ) from None
    except BaseException:
        # KeyboardInterrupt included: an operator stopping the run must not
        # leave a vendor CLI and its children running against the project.
        _terminate_group(proc)
        _drain_output(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _drain_output(proc, previous=None):
    """A detached descendant may retain pipes even after the group is gone."""
    try:
        return proc.communicate(timeout=2)
    except subprocess.TimeoutExpired as exc:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        def decoded(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        return (decoded(exc.output or getattr(previous, "output", None)),
                decoded(exc.stderr or getattr(previous, "stderr", None)))


def _terminate_group(proc) -> None:
    """Signal our stable process group even after the launcher has exited."""
    grouped = hasattr(os, "killpg") and hasattr(os, "setsid")
    # _launch starts a new session: its PID is the PGID. getpgid(pid) fails
    # once the launcher has exited, even while its children are still alive.
    pgid = proc.pid
    for signum, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            if grouped:
                os.killpg(pgid, signum)
            elif proc.poll() is None:  # pragma: no cover -- non-POSIX
                proc.terminate() if signum == signal.SIGTERM else proc.kill()
            else:
                return
        except OSError:
            proc.poll()
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            proc.poll()  # reap the parent, but do not mistake it for the group
            try:
                if grouped:
                    os.killpg(pgid, 0)
                elif proc.poll() is not None:  # pragma: no cover
                    return
            except OSError:
                # macOS can report EPERM for a group whose last member just
                # exited; still attempt KILL after the bounded grace.
                break
            time.sleep(0.05)


def _render_history(history: Sequence[Turn]) -> str:
    if not history:
        return ""
    lines = ["--- Prior conversation ---"]
    for turn in history:
        who = "User" if turn.role == "user" else "Assistant"
        lines.append(f"[{who}] {turn.content}")
    lines.append("--- End prior conversation ---")
    return "\n".join(lines)


class NativeControlOverride(ProviderError):
    """A configuration conflicts with the selected native-delegation policy."""


def native_delegation_mode(env: Optional[Mapping[str, str]] = None) -> str:
    environ = os.environ if env is None else env
    raw = environ.get('QUADRATUS_NATIVE_DELEGATION', '').strip().lower()
    if raw not in ('', 'vendor-default', 'off'):
        raise NativeControlOverride(
            'QUADRATUS_NATIVE_DELEGATION must be vendor-default or off')
    return raw or 'vendor-default'


def _fold_disallowed(argv: List[str], flag: str, extra: List[str]) -> List[str]:
    if flag in argv:
        index = argv.index(flag) + 1
        present = argv[index].split()
        argv[index] = ' '.join(present + [name for name in extra if name not in present])
        return argv
    return argv + [flag, ' '.join(extra)]


class CLIProvider(LLMProvider):
    """Base class for providers that drive a vendor CLI as a subprocess."""

    spec: CLISpec = CLAUDE_SPEC

    def __init__(self, model, api_key=None, *, workdir=None, allow_writes=False, **kwargs):
        # CLI providers authenticate through the vendor's cached OAuth login, so
        # there is no key to supply. A truthy sentinel keeps the base class from
        # short-circuiting to "unavailable"; real availability is decided by
        # whether the binary resolves in _build_client.
        self._workdir = workdir
        self._owned_workdir: Optional[str] = None
        self._allow_writes = allow_writes
        self._prompt_files = set()
        #: Real token counts from the most recent call, when the CLI reported
        #: them; None otherwise. Read by metering glue, never load-bearing.
        self.last_usage: Optional[Dict[str, int]] = None
        #: Vendor-native sub-agents seen in the last call's event stream.
        #: Observed, not dispatched: outside Quadratus worker selection and
        #: outside every per-task budget. Kept distinct so they are never
        #: folded into Quadratus-dispatched totals.
        self.native_children: List[NativeChild] = []
        #: How the most recent call ended, in the bounded form the ledger may
        #: keep: stop reason, model-call count, attempted tool names. Reset at
        #: the start of every attempt so a stale record never describes a
        #: later call. Attached to a raised ProviderError as ``diagnostics``.
        self.last_diagnostics: Optional[Dict[str, object]] = None
        super().__init__(model, api_key="cli-oauth", **kwargs)

    # -- lifecycle -----------------------------------------------------------
    def _build_client(self):
        binary = self.spec.resolved_binary()
        path = shutil.which(binary)
        if not path:
            raise CLINotInstalled(
                f"'{binary}' is not on PATH. Install the CLI and sign in "
                f"with your subscription, or set this provider's backend to 'api'."
            )
        if not self.spec.verified:
            log.warning(
                "%s CLI flags are unverified against a live binary; if calls fail, "
                "check the spec in cli_providers.py against `%s --help`.",
                self.label,
                self.spec.binary,
            )
        return path

    @property
    def allow_writes(self):
        return self._allow_writes

    @property
    def workdir(self) -> str:
        """Scratch directory the agent runs in.

        Isolating the CWD is what stops a reviewer from editing the real
        working tree, so it is created lazily rather than left to the caller.
        """
        if self._workdir:
            os.makedirs(self._workdir, exist_ok=True)
            return self._workdir
        if not self._owned_workdir:
            self._owned_workdir = tempfile.mkdtemp(prefix=f"quadratus-{self.name}-")
        return self._owned_workdir

    def cleanup(self) -> None:
        for path in list(self._prompt_files):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            self._prompt_files.discard(path)
        if self._owned_workdir:
            shutil.rmtree(self._owned_workdir, ignore_errors=True)
            self._owned_workdir = None

    def for_seat(self, model, *, effort="", restricted=False):
        # Allocate on the owner before cloning so Fleet.close owns the directory.
        if self._workdir is None:
            _ = self.workdir
        return super().for_seat(model, effort=effort, restricted=restricted)

    def for_model(self, model):
        if self._workdir is None:
            _ = self.workdir
        return super().for_model(model)

    def in_directory(self, workdir, *, allow_writes=False):
        view = copy.copy(self)
        view._workdir = str(workdir)
        view._owned_workdir = None
        view._allow_writes = allow_writes
        return view

    # -- invocation ----------------------------------------------------------
    def _build_argv(self, prompt: str, system: str) -> List[str]:
        spec = self.spec
        argv = [self._client, *spec.subcommand, *spec.print_flag]

        if spec.system_flag and system:
            argv += [spec.system_flag, system]
        if spec.model_flag and self.model:
            argv += [spec.model_flag, self.model]
        argv += list(spec.output_args)
        if spec.effort_flag and self.effort:
            argv += [spec.effort_flag, spec.effort_template.format(level=self.effort)]
        argv += list(spec.always_args)
        if self.restricted and spec.restricted_args:
            # A bounded call, not an agent. The permission axis does not apply:
            # readonly_args and write_args both describe what an agent may do
            # with its tools, and this seat's dangerous tools are absent rather
            # than governed.
            argv += list(spec.restricted_args)
        else:
            argv += list(spec.agentic_args)
            argv += list(
                spec.readonly_args if not self._allow_writes else spec.write_args
            )
        # Operator overrides go last, so they can also correct something the
        # spec got wrong above -- most CLIs let a later flag win. The one
        # thing they may not correct is the control: it is checked against
        # them first, and a conflict is a refusal, not a warning, because a
        # call that ran with native spawning back on would look exactly like
        # a bounded one from the outside.
        extra = spec.extra_args()
        mode = native_delegation_mode()
        if mode == 'off' and spec.native_fanout_off_args:
            # Vendor-specific flag precedence is not a reliable generic
            # parser. In this strict opt-in mode, reject overrides rather
            # than let a later tools/settings flag silently undo the denial.
            if extra:
                raise NativeControlOverride(
                    f'QUADRATUS_CLI_ARGS_{spec.vendor.upper()} must be empty '
                    'when QUADRATUS_NATIVE_DELEGATION=off')
            flag, *names = spec.native_fanout_off_args
            argv = _fold_disallowed(argv, spec.disallowed_tools_flag or flag,
                                    ' '.join(names).split())
        if spec.override_conflicts is not None:
            conflicts = spec.override_conflicts(extra)
            if conflicts:
                raise NativeControlOverride(
                    f"{self.label}: QUADRATUS_CLI_ARGS_{spec.vendor.upper()} would "
                    f"re-enable or hide vendor-native sub-agents "
                    f"({'; '.join(conflicts)}). Quadratus disables native "
                    f"spawning on every {spec.binary} call so that helpers pass "
                    f"through WorkerPool; remove the override, not the control."
                )
        # Before the overrides rather than after them, and deliberately so:
        # the precedence probe (see CODEX_NATIVE_DELEGATION_FEATURES) showed
        # --disable wins from any position, the conflict check above covers
        # the spellings that could out-order a weaker form, and the operator
        # contract that overrides land last is one this module already
        # promises.
        argv += list(spec.control_args)
        argv += extra
        if self.restricted and spec.restricted_prompt_flag:
            if len(prompt) > MAX_ARGV_PROMPT:
                # Falling back to a prompt file here would silently drop the
                # tool restrictions with it, turning a bounded worker call
                # into a full agent with writes. Refusing is the only option
                # that cannot be mistaken for success.
                raise ProviderError(
                    f"{self.label}: a restricted call delivers its prompt as an "
                    f"argument, and this one is {len(prompt):,} characters "
                    f"(limit {MAX_ARGV_PROMPT:,}). Route this task to an "
                    f"unrestricted seat rather than sending it unbounded."
                )
            argv += [spec.restricted_prompt_flag, prompt]
        elif spec.prompt_file_flag:
            argv += [spec.prompt_file_flag, self._write_prompt_file(prompt)]
        elif not spec.prompt_on_stdin:
            argv.append(prompt)
        return argv

    def _write_prompt_file(self, prompt: str) -> str:
        """Spill the prompt to a file for CLIs that read it from a path.

        Lives in the scratch directory the agent already runs in, and is
        overwritten per call rather than accumulating: one prompt is live at a
        time per provider, and ``cleanup`` takes the directory with it.
        """
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         prefix="quadratus-prompt-", suffix=".txt",
                                         delete=False) as stream:
            stream.write(prompt)
            self._prompt_files.add(stream.name)
            return stream.name

    def _compose_prompt(self, prompt: str, system: str, history: Sequence[Turn]) -> str:
        """Fold whatever the CLI cannot express natively into the user prompt."""
        parts = []
        if system and not self.spec.system_flag:
            parts.append(f"--- Your role and instructions ---\n{system}")
        rendered = _render_history(history)
        if rendered:
            parts.append(rendered)
        parts.append(prompt)
        return "\n\n".join(parts)

    def _call(self, prompt: str, system: str, history: Sequence[Turn]) -> str:
        self.last_diagnostics = None  # per attempt: a stale record must not describe this call
        composed = self._compose_prompt(prompt, system, history)
        argv = self._build_argv(composed, system)
        env = {**os.environ, **self.spec.env}
        # An inherited ANTHROPIC_API_KEY would silently divert a subscription
        # run onto billed API credits, so clear key vars for the child.
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                    "GOOGLE_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
            env.pop(var, None)

        log.debug("%s invoking: %s", self.label, " ".join(argv[:6]))
        try:
            proc = _launch(
                argv,
                input=composed if self.spec.prompt_on_stdin else None,
                timeout=self.timeout,
                cwd=self.workdir,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output.decode(errors="replace") if isinstance(exc.output, bytes) else exc.output
            self._observe_output(output or "")
            raise TimeoutError(
                f"{self.label} CLI timed out after {self.timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise ProviderError(f"{self.label} CLI vanished from PATH: {exc}") from exc
        finally:
            if self.spec.prompt_file_flag and self.spec.prompt_file_flag in argv:
                path = argv[argv.index(self.spec.prompt_file_flag) + 1]
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                self._prompt_files.discard(path)

        self._observe_output(proc.stdout)

        if proc.returncode != 0:
            # A failing exit code does not mean there is nothing to read. The
            # Claude CLI exits non-zero on an API error while still printing a
            # complete JSON envelope naming the cause -- observed on a 429:
            # {"is_error":true,"api_error_status":429,"result":"You've reached
            # your Fable limit..."}. Discarding that in favour of "exited 1"
            # threw away the one sentence the operator needed, and hid
            # classifier refusals behind the same shrug. So the extractor gets
            # first refusal; only if it has nothing to say does the exit code
            # become the error.
            try:
                self.spec.extract(proc.stdout)
            except (ProviderError, ProviderRefusal) as parsed:
                parsed.diagnostics = self.last_diagnostics
                if isinstance(parsed, ProviderRefusal):
                    parsed.model = self.model
                    raise
                detail = (proc.stderr or '').strip()[:400]
                if detail:
                    raise ProviderError(f'{parsed}; {detail}') from parsed
                raise
            except Exception:  # noqa: BLE001 -- unparseable is the normal case
                pass
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise RuntimeError(
                f"{self.label} CLI exited {proc.returncode}: {detail}"
            )

        try:
            return self.spec.extract(proc.stdout)
        except ProviderRefusal as refusal:
            refusal.diagnostics = self.last_diagnostics
            refusal.model = self.model
            raise
        except ProviderError as failure:
            failure.diagnostics = self.last_diagnostics
            raise

    def _observe_output(self, stdout):
        """Extract accounting even when response parsing later fails."""
        try:
            self.last_usage = self.spec.extract_usage(stdout) if self.spec.extract_usage else None
            self.last_diagnostics = (
                self.spec.extract_diagnostics(stdout) if self.spec.extract_diagnostics else None
            )
            self.native_children = _extract_native_children(stdout)
            for line in (stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "thread.started":
                    self.last_session_id = event.get("thread_id")
                elif event.get("session_id"):
                    self.last_session_id = event["session_id"]
                # A Claude result names concrete releases, while argv often
                # uses a moving alias. Auxiliary modelUsage rows are not seats.
                models = event.get("modelUsage") or {}
                matching = [name for name in models if self.model and self.model in name]
                if len(matching) == 1:
                    self.resolved_model = matching[0]
            if self.spec.vendor == "openai" and getattr(self, "last_session_id", None):
                from .native_sessions import codex_children
                root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
                self.native_children.extend(codex_children(
                    root, self.last_session_id, self.workdir,
                    ended=datetime.now(timezone.utc),
                ))
            if self.native_children and self.native_delegation_disabled:
                # The switch was sent and a child ran anyway. Say so on the
                # record itself, where the ledger and the evidence bundle
                # will carry it; a child silently filed under "observed"
                # would read as the expected state of a vendor without a
                # switch, which this vendor no longer is.
                note = (
                    "CONTROL FAILURE: native child ran although the call sent "
                    + " ".join(self.spec.control_args)
                )
                log.warning("%s: %s", self.label, note)
                self.native_children = [
                    _annotate_child(child, note) for child in self.native_children
                ]
        except Exception:
            log.debug("native accounting unavailable", exc_info=True)

    @property
    def native_delegation_disabled(self) -> bool:
        """Whether this transport tells the CLI not to spawn its own agents.

        True only where the spec carries a control; a vendor without one is
        reported as such rather than assumed bounded.
        """
        return bool(self.spec.control_args)

    def _retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            return True
        if isinstance(exc, CLINotInstalled):
            return False
        text = str(exc).lower()
        # Rate-limit and transient-server language varies by vendor; match on
        # the wording rather than an exit code, which is uniformly 1.
        return any(
            token in text
            for token in (
                "rate limit",
                "rate_limit",
                "overloaded",
                "timed out",
                "timeout",
                "503",
                "502",
                "500",
                "connection",
                "temporarily",
                "try again",
                "usage limit",
            )
        )


class ClaudeCLIProvider(CLIProvider):
    name = "claude"
    label = "Claude"
    spec = CLAUDE_SPEC


class CodexCLIProvider(CLIProvider):
    name = "openai"
    label = "ChatGPT"
    spec = CODEX_SPEC


class GrokCLIProvider(CLIProvider):
    name = "grok"
    label = "Grok"
    spec = GROK_SPEC


def cli_provider_classes() -> Dict[str, type]:
    """Map provider name to its CLI-backed implementation."""
    return {
        "claude": ClaudeCLIProvider,
        "openai": CodexCLIProvider,
        "grok": GrokCLIProvider,
    }
