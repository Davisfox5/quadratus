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
import math
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
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .delegation import NativeChild
from .providers import LLMProvider, ProviderError, ProviderRefusal, Turn, TurnLimitReached

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
    "CODEX_NATIVE_DELEGATION_OVERRIDE",
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
    if payload.get("subtype") == "error_max_turns":
        # The result envelope's turn-cap subtype. Not yet seen from the
        # installed binary: confirm the exact shape with a bounded probe.
        raise TurnLimitReached("claude stopped at its turn limit before finishing",
                               partial_text=payload.get("result"),
                               turns=_envelope_turns(payload))
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
    failed = ""
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
        if event.get("type") == "turn.failed" and isinstance(event.get("error"), dict):
            failed = str(event["error"].get("message") or "")
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
    if re.search(r"\b401\b.*unauthori[sz]ed|unauthori[sz]ed.*\b401\b", failed, re.IGNORECASE):
        # GameTape run 6: the turn failed with 401 after the websocket dropped.
        # A run on a copied sign-in loses it when another codex client on the
        # same account refreshes the token. Named, and vendor-wide.
        error = ProviderError(
            "codex sign-in was rejected (401 Unauthorized). The subscription token in this "
            "environment is no longer valid -- usually because another codex client on the same "
            "account refreshed it. Sign in again before the next run.")
        error.auth_invalid = True
        raise error
    if failed and not errors:
        raise ProviderError(f"codex turn failed: {failed[:300]}")
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
    if stop == "max_turns":
        # Capped, not failed: the call ran and may have written. The text is
        # where the loop had got to, kept as evidence and never as an answer.
        raise TurnLimitReached("grok stopped at its turn limit before finishing",
                               partial_text=text, turns=_envelope_turns(payload))
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
    # This envelope reports both, and grok is where the re-read cost is
    # largest: 383,104 of attempt 8's 437,173 input tokens.
    turns = _envelope_turns(payload)
    if turns is not None:
        diagnostics.setdefault("model_calls", turns)
    diagnostics.update(_reread_and_cost(payload.get("usage"),
                                        payload.get("total_cost_usd")))
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


#: Fields whose presence means a turn *began*, so its spend is genuinely
#: unknown when the envelope carries no usage. An error envelope with none of
#: them is the CLI refusing before it contacted the service.
_TURN_BEGAN_KEYS = ("usage", "sessionId", "session_id", "text", "stopReason",
                    "stop_reason", "modelUsage", "messages")


def _refused_before_the_turn(payload: object) -> bool:
    """Did the vendor refuse to start, without reaching a model?

    A call that never began spent nothing, and that is a different fact from
    "this call's spend is unknown". The distinction matters because unknown
    usage latches the run budget and stops the run -- which is right when a
    call may have burned a window, and wrong when the CLI printed a one-line
    refusal in under a second and exited.

    Attempt 7 of the blind acceptance is the case. The grok CLI's session had
    expired, so it answered ``{"type": "error", "message": "Not signed in..."}``
    in 0.37 seconds. That reported no usage, the budget latched, and the
    recovery on Sol that the engine had *already selected* was refused. One
    expired login ended a run that had every other seat working.

    Deliberately narrow, and it errs towards unknown. An envelope counts as a
    refusal only if it is an error and carries nothing that implies a turn
    began -- no usage, no session id, no text, no stop reason. A turn that
    started and then failed carries at least a session id, so it stays
    unknown, and so does anything unparseable. This is not the same as
    ``window_exhausted``: that is a vendor limit reported by a live service,
    this is the CLI never getting that far.
    """
    if not isinstance(payload, dict):
        return False
    looks_like_error = payload.get("type") == "error" or bool(payload.get("error"))
    if not looks_like_error:
        return False
    return not any(payload.get(key) for key in _TURN_BEGAN_KEYS)


def _extract_grok_usage(stdout: str) -> Optional[Dict[str, int]]:
    """Real token counts from the grok JSON envelope.

    Cache reads and creations fold into input for the same reason they do on
    the claude side: the meter asks what this would have cost on API keys, and
    cached input is still billed input there.
    """
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if _refused_before_the_turn(payload):
        return {"input_tokens": 0, "output_tokens": 0}
    try:
        usage = payload.get("usage") or {}
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


def _codex_rollout_usage(root: Path, session_id: str) -> Optional[dict]:
    """A lower bound on a failed call's usage, from codex's own session
    record, for diagnostics. Never raises; None when nothing was recorded.

    Not a measurement. GameTape run 6 (2026-09-25): a stream closed before
    response.completed and the budget stopped on unknown usage. The first
    version of this function turned token_count events with ``info: null``
    into zero and cleared that stop; Codex's review showed null info is an
    absence of accounting (that call also ended in a 401), and that
    ``output_tokens`` already includes reasoning tokens. So: the last
    recorded total, as a floor, or None.
    """
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", session_id):
        return None
    try:
        files = sorted(Path(root).rglob(f"rollout-*{session_id}.jsonl"))
    except OSError:
        return None
    if not files:
        return None
    counts, total = 0, None
    try:
        with open(files[-1], encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                counts += 1
                info = payload.get("info")
                if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                    total = info["total_token_usage"]
    except OSError:
        return None
    if total is None:
        return None
    try:
        return {"input_tokens": int(total.get("input_tokens") or 0),
                "cached_input_tokens": int(total.get("cached_input_tokens") or 0),
                # already includes reasoning_output_tokens
                "output_tokens": int(total.get("output_tokens") or 0),
                "usage_source": "codex session record, lower bound (stream ended early)",
                "token_count_events": counts}
    except (TypeError, ValueError):
        return None


def _envelope_turns(payload) -> Optional[int]:
    """How many model turns one CLI call ran, from its JSON envelope.

    claude and grok report ``num_turns``; grok also counts ``modelCalls`` per
    model inside ``modelUsage``, which the top-level lookup used to miss, so
    the 32-turn GameTape UI lead recorded no turn count at all.
    """
    if not isinstance(payload, dict):
        return None
    turns = payload.get("num_turns")
    if isinstance(turns, int) and not isinstance(turns, bool) and turns >= 0:
        return turns
    rows = payload.get("modelUsage")
    if isinstance(rows, dict):
        counts = [r.get("modelCalls") for r in rows.values() if isinstance(r, dict)]
        counts = [n for n in counts if isinstance(n, int) and not isinstance(n, bool) and n >= 0]
        if counts:
            return sum(counts)
    return None


def _reread_and_cost(usage, cost=None) -> Dict[str, object]:
    """How much of this call's input was text the model had already seen.

    Attempt 9 reported 532,795 tokens, of which 402,816 were re-read: an agent
    re-sends its whole conversation on every step, so a long loop's total is
    mostly repetition. Nothing in the run record showed that. Finding it meant
    opening the private vendor envelopes, which is precisely the digging a
    ledger exists to spare someone.

    Reported as a *subset* of the normalised ``input_tokens``, never an
    addition to it, so the two can be compared without double counting.
    ``vendor_cost_usd`` is what the vendor says it charged, kept beside our own
    API-price counterfactual rather than replacing it -- on attempt 8's lead
    the two differed 7.4x, because cache reads bill at a fraction of fresh
    input, and which of them a subscription window meters by is undocumented.
    """
    facts: Dict[str, object] = {}
    if isinstance(usage, dict):
        total = 0
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens",
                    "cached_input_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                total += value
        if total:
            facts["cached_input_tokens"] = total
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        facts["vendor_cost_usd"] = float(cost)
    return facts


def _extract_codex_diagnostics(stdout: str) -> Optional[Dict[str, object]]:
    """Bounded facts from codex's ``turn.completed`` event.

    This vendor had no diagnostics extractor at all, so its calls reached the
    ledger with no stop reason and no re-read split -- and codex holds the
    orchestrator seat whenever Fable is out, which by attempt 9 was every run.
    """
    diagnostics: Dict[str, object] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        # The last completed turn wins, matching _extract_codex_usage.
        diagnostics = {"stop_reason": "turn.completed"}
        diagnostics.update(_reread_and_cost(event.get("usage")))
    return diagnostics or None


#: Upper bound on retained tool failures per call, and on each field's text.
TOOL_FAILURE_LIMIT = 8
TOOL_FAILURE_TEXT = 500


def _extract_codex_tool_failures(stdout: str) -> List[Dict[str, object]]:
    """Commands the codex agent ran that failed, from ``codex exec --json``.

    Bounded evidence, not a transcript: only ``command_execution`` items with
    a nonzero exit or a failed status, plus ``error`` items, each cut to a
    command line, an exit code and an output tail. Both Q9 canary runs
    (2026-09-22) ended with the lead reporting that its sandbox could not
    start, and nothing retained showed the command that said so. The verifier
    called the claim unevidenced and was right; this is the evidence.

    Item shape observed on codex-cli 0.154.0::

        {"type":"item.completed","item":{"id":"item_2","type":"command_execution",
         "command":"cat app.py","aggregated_output":"...","exit_code":1,
         "status":"failed"}}

    Field names beyond ``command``, ``aggregated_output``, ``exit_code`` and
    ``status`` are not relied on; a shape change degrades to fewer entries,
    never to an exception.
    """
    failures: List[Dict[str, object]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "command_execution":
            exit_code = item.get("exit_code")
            status = item.get("status")
            failed = (type(exit_code) is int and exit_code != 0) or status in ("failed", "declined")
            if not failed:
                continue
            output = item.get("aggregated_output")
            failures.append({
                "kind": "command",
                "command": str(item.get("command", ""))[:TOOL_FAILURE_TEXT],
                "exit_code": exit_code if type(exit_code) is int else None,
                "status": str(status) if status is not None else "",
                "output_tail": (output if isinstance(output, str) else "")[-TOOL_FAILURE_TEXT:],
            })
        elif kind == "error":
            message = item.get("message")
            if isinstance(message, str) and message.strip():
                failures.append({"kind": "error", "message": message.strip()[:TOOL_FAILURE_TEXT]})
        if len(failures) >= TOOL_FAILURE_LIMIT:
            break
    return failures


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


_ABSENT = object()


def _strict_count(mapping, key: str, *, missing_ok: bool) -> Optional[int]:
    """A token count as the envelope must state it: a non-negative int.

    Strings, floats, booleans, negatives and an explicit ``null`` are
    malformed, not coerced. An absent optional field is zero; an absent
    required one is malformed. Absence and ``null`` are different claims:
    the first says nothing, the second says "no value", which for a count
    is not a number.
    """
    value = mapping.get(key, _ABSENT)
    if value is _ABSENT:
        return 0 if missing_ok else None
    if type(value) is not int or value < 0:
        return None
    return value


def _claude_row_totals(row) -> Optional[Dict[str, int]]:
    """One ``modelUsage`` row as (input incl. cache, output), or None if malformed."""
    if not isinstance(row, dict):
        return None
    fields = [
        _strict_count(row, "inputTokens", missing_ok=False),
        _strict_count(row, "cacheReadInputTokens", missing_ok=True),
        _strict_count(row, "cacheCreationInputTokens", missing_ok=True),
        _strict_count(row, "outputTokens", missing_ok=False),
    ]
    if any(f is None for f in fields):
        return None
    return {"input_tokens": fields[0] + fields[1] + fields[2], "output_tokens": fields[3]}


def _claude_usage_parts(stdout: str):
    """(seat usage from top-level ``usage``, per-model rows, malformed flag).

    Scored attempt 1 (2026-09-15) showed why both are read: the envelope's
    top-level ``usage`` is the seat model's alone, while ``modelUsage`` also
    carried a Haiku row (2,817 tokens) for the CLI's own auxiliary call. That
    row was real subscription usage the run budget never saw.

    ``malformed`` is true when any part of the metadata could not be read as
    stated: a row that is not a mapping, a count that is not a non-negative
    integer, a ``modelUsage`` that is not a mapping, or rows whose sum is
    smaller than the seat's own figure (the seat is one of the rows, so a
    smaller sum means rows are missing).
    """
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return None, {}, False
    if not isinstance(payload, dict):
        return None, {}, False
    seat = None
    malformed = False
    usage = payload.get("usage")
    if isinstance(usage, dict):
        counts = [
            _strict_count(usage, "input_tokens", missing_ok=True),
            _strict_count(usage, "cache_read_input_tokens", missing_ok=True),
            _strict_count(usage, "cache_creation_input_tokens", missing_ok=True),
            _strict_count(usage, "output_tokens", missing_ok=True),
        ]
        if any(c is None for c in counts):
            malformed = True
        else:
            # An all-zero report is a report. An earlier version collapsed it
            # to "no usage", on the reasoning that zero is indistinguishable
            # from missing -- which is not true here: a missing ``usage`` key
            # never reaches this branch at all. Attempt 4 of the blind
            # acceptance paid for the difference. A vendor window-limit
            # envelope reports zeros because the request was rejected, that
            # read as unknown usage, the run budget latched, and the
            # orchestrator's fallback seat -- already chosen, and logged as
            # chosen -- was refused its reservation. The run ended in 3.5
            # seconds without ever calling the deputy the fallback exists for.
            seat = {"input_tokens": counts[0] + counts[1] + counts[2], "output_tokens": counts[3]}
    elif usage is not None:
        malformed = True
    rows: Dict[str, Dict[str, int]] = {}
    model_usage = payload.get("modelUsage", _ABSENT)
    if isinstance(model_usage, dict):
        if not model_usage and seat is not None and (seat["input_tokens"] or seat["output_tokens"]):
            # Present and empty is a claim of "no models", which only a
            # *nonzero* seat contradicts: a seat that reports zero agrees with
            # it. Absent says nothing and keeps the seat.
            malformed = True
        for name, row in model_usage.items():
            totals = _claude_row_totals(row)
            if totals is None or not isinstance(name, str) or not name:
                malformed = True
                continue
            rows[name] = totals
    elif model_usage is not _ABSENT:
        malformed = True
    if rows and seat is not None:
        # Per component, not grand total: the seat is one of the rows, so the
        # rows' input must cover the seat's input and likewise for output.
        if (sum(r["input_tokens"] for r in rows.values()) < seat["input_tokens"]
                or sum(r["output_tokens"] for r in rows.values()) < seat["output_tokens"]):
            malformed = True
    return seat, rows, malformed


def _extract_claude_usage(stdout: str) -> Optional[Dict[str, int]]:
    """Real token counts from the claude JSON envelope, when present.

    Cache reads and creations are folded into ``input_tokens``: the meter's
    question is what the call would have cost on API keys, and cached input is
    still billed input there (at a different rate the seed sheet does not try
    to model -- the counterfactual is deliberately the conservative one).

    The reported figure is the sum of every ``modelUsage`` row (seat plus the
    CLI's own auxiliary models) when rows are present, else the top-level
    ``usage`` (the seat alone). The two are never added to each other: the
    seat's row is inside the sum, so each token is counted once.

    Malformed or partial metadata makes the whole figure **unknown** (None),
    so the run budget stops rather than continue on a count that is known to
    be incomplete. The known seat part is preserved in the diagnostics for
    the record; it is not presented as the total.
    """
    seat, rows, malformed = _claude_usage_parts(stdout)
    if malformed:
        return None
    if rows:
        return {"input_tokens": sum(r["input_tokens"] for r in rows.values()),
                "output_tokens": sum(r["output_tokens"] for r in rows.values())}
    return seat


def _extract_claude_diagnostics(stdout: str) -> Optional[Dict[str, object]]:
    """Provenance for the usage figure: which rows beyond the seat were counted.

    ``auxiliary_models`` names the ``modelUsage`` rows that are not the seat,
    the seat being the one row whose totals equal the top-level ``usage``;
    ``auxiliary_tokens`` is their input plus output. When no row matches the
    seat, or more than one does (a tie is not an identity), the rows are
    ``unattributed`` and the figure is the excess of the rows' sum over the
    seat. Malformed metadata sets ``auxiliary_usage`` to ``unknown`` and
    reports the seat's own known figure as ``seat_tokens``: what could not be
    parsed is missing, not zero, and the run budget sees None. Names and
    integers only; nothing from the envelope's text reaches here.
    """
    seat, rows, malformed = _claude_usage_parts(stdout)
    diagnostics: Dict[str, object] = {}
    try:
        payload = json.loads(stdout)
    except ValueError:
        payload = None
    # Only an object envelope carries turns or a reread; a malformed, empty
    # or list envelope must not raise here, or _observe_output loses the rest
    # of the failure evidence (Codex review of #25, 2026-09-25).
    if isinstance(payload, dict):
        turns = _envelope_turns(payload)
        if turns is not None:
            diagnostics.setdefault("model_calls", turns)
        diagnostics.update(_reread_and_cost(payload.get("usage"),
                                            payload.get("total_cost_usd")))
    if malformed:
        diagnostics["auxiliary_usage"] = "unknown"
        if seat is not None:
            diagnostics["seat_tokens"] = seat["input_tokens"] + seat["output_tokens"]
        return diagnostics
    if not rows:
        return None
    total = sum(r["input_tokens"] + r["output_tokens"] for r in rows.values())
    if seat is None:
        diagnostics["auxiliary_models"] = sorted(rows)
        diagnostics["auxiliary_tokens"] = total
        diagnostics["auxiliary_usage"] = "unattributed"
        return diagnostics
    seat_rows = [name for name, r in rows.items() if r == seat]
    if len(seat_rows) == 1:
        aux = {name: r for name, r in rows.items() if name != seat_rows[0]}
        if not aux:
            return None
        diagnostics["auxiliary_models"] = sorted(aux)
        diagnostics["auxiliary_tokens"] = sum(r["input_tokens"] + r["output_tokens"] for r in aux.values())
        return diagnostics
    diagnostics["auxiliary_models"] = sorted(rows)
    diagnostics["auxiliary_tokens"] = total - seat["input_tokens"] - seat["output_tokens"]
    diagnostics["auxiliary_usage"] = "unattributed"
    return diagnostics


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
    #: Used instead of whichever sandbox args a seat would otherwise get --
    #: ``restricted_args`` on a bounded seat, ``readonly_args``/``write_args``
    #: on an agentic one -- when the operator asserts this process is already
    #: inside an OS sandbox that is the security boundary
    #: (``QUADRATUS_CONTAINED=1``). Empty means containment changes nothing,
    #: which is the right answer for a vendor whose restriction is a tool
    #: denial: denying a tool costs nothing inside a container and still
    #: bounds what the model can reach for.
    #:
    #: It covers both branches because they are the same mechanism. Attempt 6
    #: of the blind acceptance stopped exactly where attempt 5 did, with this
    #: field set, because it was then consulted only on the restricted branch
    #: -- and every seat that had ever been blind was an agentic one. An
    #: orchestrator or lead is a senior seat and is never restricted, so a
    #: substitution that skipped the agentic branch could not reach any of
    #: them. See ``contained`` for what the permission axis rests on instead.
    contained_sandbox_args: List[str] = field(default_factory=list)
    #: A command that exercises the vendor's own sandbox without invoking a
    #: model, given as the arguments after the binary. Empty means the vendor
    #: has no such sandbox to test, which is reported as "not applicable"
    #: rather than passing silently. ``{mode}`` is filled in by
    #: ``sandbox_selftest`` with the mode a seat would really be given.
    sandbox_selftest_args: List[str] = field(default_factory=list)
    #: A command that reports whether this CLI is signed in, without invoking a
    #: model. Empty means the vendor offers no such readout, which is reported
    #: as "not applicable" rather than passing silently.
    auth_check_args: List[str] = field(default_factory=list)
    #: A regex the auth readout must match to count as signed in. Empty means
    #: the CLI prints nothing distinctive when it is, so only a failure can be
    #: recognised -- see ``auth_failure_pattern``.
    auth_ok_pattern: str = ""
    #: A regex in the auth readout that means definitely not signed in. Needed
    #: because a CLI can report a dead session and still exit 0: ``grok models``
    #: prints "You are not authenticated." and returns success, which is how
    #: attempt 7 got as far as spending 82,051 tokens before finding out.
    auth_failure_pattern: str = ""
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
    #: Environment set on the subprocess only when the off-mode denial was
    #: folded into this call's argv. Documented kill switches that remove a
    #: fan-out path at startup, so a denial the model never sees cannot be
    #: argued with either.
    native_fanout_off_env: Dict[str, str] = field(default_factory=dict)
    disallowed_tools_flag: str = ""
    #: How this CLI attaches the in-session ``commission_worker`` tool
    #: (``worker_bridge``): "claude", "codex", "grok", or "" for none. Each
    #: form was probed on 2026-09-25; see ``_worker_tool_argv``.
    worker_tool_style: str = ""
    #: Names the run-wide denial must drop while the tool is attached, or the
    #: denial would remove the tool it is meant to leave alone.
    worker_tool_undeny: Tuple[str, ...] = ()
    #: Flags that stop this CLI loading the operator's personal configuration
    #: (plugins, hooks, rules) for a run with QUADRATUS_NEUTRAL_PREFERENCES
    #: set. Sign-in is kept. Empty when the CLI offers no such flag.
    neutral_args: List[str] = field(default_factory=list)
    #: What neutral mode cannot remove on this CLI, recorded with the run.
    neutral_gap: str = ""
    #: Arguments that turn the restricted seat into a one-turn, tool-less (or
    #: as close as the CLI documents) summary call. Sent only when a provider
    #: view carries ``summary_only=True``; see ``CLIProvider._build_argv``.
    summary_only_args: List[str] = field(default_factory=list)
    #: The flag that caps agentic turns in one call, for a lead's turn limit
    #: (``Settings.lead_max_turns``). Empty where the CLI has none (codex):
    #: there the call is bounded by time and attempts only.
    max_turns_flag: str = ""
    #: Whether a failed envelope whose turn count reached the cap is read as
    #: the cap. True only where the CLI's reported count is the cap's own
    #: counter and the cap has no explicit marker (grok, GameTape run 3).
    #: Claude marks a real cap with ``subtype: error_max_turns``, and its
    #: success ``num_turns`` counts every user message, one per tool result,
    #: so it can pass the cap without reaching it (frozen 2.1.269, Run 12:
    #: num_turns 17 at --max-turns 14, end_turn, 16 tool results).
    turn_cap_by_count: bool = False
    #: How the CLI separates several names in one ``disallowed_tools_flag``
    #: value. Claude takes whitespace; grok's ``--help`` says comma-separated,
    #: and a space-joined list would reach it as one nonsense tool name that
    #: denies nothing (caught 2026-09-15 before the live check).
    disallowed_tools_separator: str = " "
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
    #: Failed tool calls the agent made during this call (command, exit code,
    #: output tail), bounded, for the ledger. None where the CLI's output does
    #: not report its tool calls in a parseable form.
    extract_tool_failures: Optional[Callable[[str], List[Dict[str, object]]]] = None
    #: Whether these flags have been checked against a real binary.
    verified: bool = False
    #: Environment variables to set for the subprocess.
    env: Dict[str, str] = field(default_factory=dict)
    #: Vendor key, used to find this spec's environment overrides.
    vendor: str = ""

    def sandbox_selftest(self, probe_file: str,
                         env: Optional[Mapping[str, str]] = None) -> List[str]:
        """The sandbox self-test, in the mode a seat here would really be given.

        ``probe_file`` is a readable file in the tree the seats must reach, so
        the test answers the question the run cares about -- can this seat read
        the source -- rather than only whether a sandbox starts.

        A self-test in some other mode answers a question nobody asked. That
        was the first version's mistake, and the note it carried -- that no
        model-free check could settle which mode ``exec`` would get -- was
        simply wrong: ``codex sandbox`` honours ``-c sandbox_mode=`` like any
        other entry point. Measured inside the acceptance container on
        2026-09-17, reading one file from the mounted tree: ``read-only`` and
        ``workspace-write`` both fail with bwrap's namespace error and
        ``danger-full-access`` returns the file's contents.

        The mode tested is the strictest a seat could draw -- the contained
        substitute where one applies, otherwise the read-only form, since a
        write grant only ever loosens it.
        """
        if not self.sandbox_selftest_args:
            return []
        effective = (self.contained_sandbox_args
                     if contained(env) and self.contained_sandbox_args
                     else self.readonly_args)
        # These arg lists are a flag followed by its value; the mode is the
        # value. Stated here because the self-test borrows it by position.
        mode = effective[-1] if effective else ""
        return [arg.format(mode=mode, probe_file=probe_file)
                for arg in self.sandbox_selftest_args]

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
    # Run-wide off mode. Every name here is a documented way for one claude
    # call to start work outside itself or reach another session, and a bare
    # name in --disallowed-tools removes the tool from the model's context
    # (code.claude.com/docs/en/cli-reference, read 2026-09-15):
    #   Task, Agent      subagents (Task is the older name, kept for older CLIs)
    #   Workflow         "orchestrates many subagents in the background"
    #   SendMessage,     cross-session messaging: other sessions in the same
    #   ListAgents       filesystem, and cloud / Remote Control sessions
    #   RemoteTrigger    claude.ai Routines, which can start fresh sessions
    #   CronCreate       a scheduled prompt that re-enters this session later
    #   mcp__*           every MCP tool; a project .mcp.json is not vetted
    # Ordinary source read/write/exec/web tools are untouched. TaskOutput,
    # TaskStop, ToolSearch and ScheduleWakeup stay: the docs scope them to
    # this session, and a denied tool stays denied however its schema was
    # loaded. Agent teams are off unless CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS
    # is set and never form under -p; pinned to 0 below anyway.
    native_fanout_off_args=["--disallowed-tools",
                            "Task Agent Workflow SendMessage ListAgents RemoteTrigger CronCreate mcp__*"],
    # --strict-mcp-config loads only the server Quadratus names, which is what
    # the mcp__* denial protected against, so the denial can step aside.
    worker_tool_style="claude",
    worker_tool_undeny=("mcp__*",),
    # --bare would also drop the subscription sign-in (it reads only
    # ANTHROPIC_API_KEY), so neutral mode narrows setting sources instead:
    # user settings, which carry plugins and hooks, are not loaded.
    neutral_args=["--setting-sources", "project,local"],
    neutral_gap="claude: the user CLAUDE.md memory file may still load; --bare would remove it but also the sign-in",
    native_fanout_off_env={
        # Read at startup: workflows unavailable, not merely denied.
        "CLAUDE_CODE_DISABLE_WORKFLOWS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    },
    disallowed_tools_flag="--disallowed-tools",
    # Summary-only form (cli-reference, read 2026-09-15): ``--tools ""``
    # "disables all tools"; ``--max-turns 1`` limits agentic turns in print
    # mode and "exits with an error when the limit is reached". Together they
    # make the closeout a single model call over the prompt it was given.
    summary_only_args=["--tools", "", "--max-turns", "1"],
    max_turns_flag="--max-turns",
    extract=_extract_claude_result,
    # Mandatory, not merely safer: --disallowed-tools is variadic, so a
    # positional prompt after it is swallowed as another tool name and the CLI
    # exits with "Input must be provided...". Stdin also sidesteps the argv
    # length limit, which whole code artifacts would otherwise hit.
    prompt_on_stdin=True,
    verified=True,
    extract_usage=_extract_claude_usage,
    extract_diagnostics=_extract_claude_diagnostics,
    # ``claude auth status`` is a model-free readout that reports
    # ``loggedIn`` (observed true, with the subscription tier, on Davis's Mac
    # on 2026-09-22 by Codex). Before this the preflight reported claude's
    # sign-in as not applicable, which read as untested rather than untestable.
    auth_check_args=["auth", "status"],
    auth_ok_pattern=r"(?i)loggedIn\W+true|logged in",
    auth_failure_pattern=r"(?i)loggedIn\W+false|not logged in|not authenticated",
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
#: **The two feature switches are not the control** (live finding,
#: 2026-09-15). In a clean image with both reported ``false``, Sol spawned
#: Sol. The reason is in codex-rs at tag ``rust-v0.154.0``,
#: ``core/src/config/mod.rs``::
#:
#:     fn multi_agent_version_override(&self) -> Option<MultiAgentVersion> {
#:         if self.features.enabled(Feature::MultiAgentV2) { Some(V2) }
#:         else if !self.agents_enabled { Some(Disabled) }
#:         else { None }
#:     }
#:     fn multi_agent_version_for_model(&self, model: Option<MultiAgentVersion>) {
#:         self.multi_agent_version_override()
#:             .or(model)
#:             .unwrap_or_else(|| self.multi_agent_version_from_features())
#:     }
#:
#: Precedence, highest first: ``features.multi_agent_v2`` on forces V2;
#: ``agents.enabled = false`` forces Disabled; otherwise the *model's own
#: declared version* (server-supplied model metadata, ``None`` in the
#: built-in table) applies; and only when the model declares nothing do the
#: feature flags decide. ``--disable`` edits the last resort. A model that
#: ships with a multi-agent version, as GPT-5.6 evidently does, never reaches
#: it. ``spec_plan.rs`` then adds ``spawn_agent`` and the rest whenever the
#: resolved version is not ``Disabled``.
#:
#: So the control is ``agents.enabled = false`` (schema: "Whether multi-agent
#: tools are enabled. Defaults to true. An enabled features.multi_agent_v2
#: setting takes precedence"), sent as a ``-c`` override, *with* the two
#: ``--disable`` switches kept so that nothing can force V2 over it. The
#: conflict scanner refuses any operator override of ``features.multi_agent*``
#: or of the ``agents`` table for the same reason. Still configuration
#: until the bounded tool-list probe below has run: on this vendor a switch
#: that reads ``false`` has already been shown not to be the switch.
CODEX_NATIVE_DELEGATION_FEATURES = ("multi_agent", "multi_agent_v2")
#: Config tables whose only purpose is to shape native sub-agents
#: (``agents.enabled``, ``agents.max_threads``, ...). The 0.154.0 binary
#: accepts ``agents.enabled`` and rejects ``agents.bogus``, so the table is
#: real; with spawning disabled any override of it is at best inert and at
#: worst an attempt to re-admit it, and both are refused.
CODEX_NATIVE_DELEGATION_TABLES = ("agents",)
#: The one override that sits ahead of the model's declared version.
CODEX_NATIVE_DELEGATION_OVERRIDE = "agents.enabled=false"
#: The control itself: both feature switches off (so nothing forces V2) and
#: the agents table disabled (so the model's own default cannot re-admit
#: the tools). Order is irrelevant to codex for these; kept stable for the
#: record.
CODEX_NATIVE_DELEGATION_CONTROL = [
    flag for name in CODEX_NATIVE_DELEGATION_FEATURES for flag in ("--disable", name)
] + ["-c", CODEX_NATIVE_DELEGATION_OVERRIDE]


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
    # Inside our own container the vendor sandbox cannot start, and it is
    # redundant there -- see ``contained`` for the measurement and the trade.
    # This is the only mode codex documents as running commands without
    # sandboxing, and the three modes are the whole of its sandbox surface
    # (``-s`` takes read-only, workspace-write or danger-full-access and
    # nothing else), so there is no fourth setting that keeps a write denial
    # while letting the sandbox stand down. The name is alarming and accurate;
    # what it means here is "the container is the sandbox".
    contained_sandbox_args=["--sandbox", "danger-full-access"],
    # Exercises the vendor sandbox with no model call, which is exactly the
    # mechanism that fails in the acceptance container. The mode is filled in
    # from the seat's own effective mode, and the command reads a real file
    # rather than running `true`: starting the sandbox is the question, and a
    # command that touches nothing could pass without answering it.
    sandbox_selftest_args=["sandbox", "-c", "sandbox_mode={mode}",
                           "--", "cat", "{probe_file}"],
    # Prints "Logged in using ChatGPT" for a subscription session, so this
    # vendor can be checked positively rather than by known failure wording.
    auth_check_args=["login", "status"],
    auth_ok_pattern=r"(?i)logged in",
    extract_diagnostics=_extract_codex_diagnostics,
    extract_tool_failures=_extract_codex_tool_failures,
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
    # Summary-only form: codex exec has no tool allowlist and no turn cap in
    # this spec, so the bound is ``--sandbox read-only`` plus the native
    # controls plus the caller's 60 s / one-attempt limits. Stated, not
    # papered over with a flag this file has never seen accepted.
    summary_only_args=[],
    extract=_extract_codex_result,
    extract_usage=_extract_codex_usage,
    prompt_on_stdin=True,
    verified=True,
    # config.toml carries plugins and MCP servers; auth.json is still read
    # from CODEX_HOME. Not --ignore-rules: it drops the project's .rules too,
    # and a switch for personal preferences must not remove repository
    # safety policy (Codex review of #25).
    neutral_args=["--ignore-user-config"],
    neutral_gap="codex: user .rules execpolicy files still load, because the only switch also drops the project's",
    worker_tool_style="codex",
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

#: How much of a CLI's stderr survives into the ledger per attempt. Enough to
#: hold a sandbox refusal or an auth failure verbatim; not a transcript.
STDERR_TAIL = 2_000

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
    # `grok models` reports the session and exits 0 either way, so only the
    # failure wording is recognisable; there is no distinctive line to match
    # when the session is live. Verified on grok 1.0.30 with an expired
    # session on 2026-09-17: "You are not authenticated.", exit 0.
    auth_check_args=["models"],
    auth_failure_pattern=r"(?i)not\s+authenticated|not\s+signed\s+in",
    readonly_args=[],
    # Run-wide off mode, from the installed grok 1.0.30 documentation as
    # quoted by Codex on PR #11 (2026-09-15), since docs.x.ai is unreachable
    # from the review environment:
    #   spawn_subagent   the native spawner (user guide); Agent is its alias,
    #                    so both names are denied
    #   workflow         every workflow agent() call and parallel() item
    #                    spends a child-agent slot (04-slash-commands.md:296);
    #                    on by default, GROK_WORKFLOWS=0 disables it
    #                    (05-configuration.md:364-372)
    #   scheduler_create schedules a later re-entry, the analogue of Claude's
    #                    CronCreate; a way past the one turn asked for
    #   search_tool,     MCP discovery and calling an integration by qualified
    #   use_tool         name (07-mcp-servers.md:213-218): integration
    #                    dispatchers, not shown to be an Agent bypass, denied
    #                    because a fresh HOME has no vetted integrations
    # --disallowed-tools removes built-in tools and is comma-separated
    # (--help; 14-headless-mode.md:35,51-82). Direct read_file,
    # run_terminal_command, search_replace, write, grep, list_dir and the web
    # tools are separate names and are untouched. The 2026-09-15 live check
    # of the space-joined form listed spawn_subagent, scheduler_create,
    # workflow, use_tool and search_tool still present, which is what a
    # single nonsense name denies: nothing. Hence the separator field.
    native_fanout_off_args=["--disallowed-tools",
                            "Agent,spawn_subagent,workflow,scheduler_create,use_tool,search_tool"],
    # grok reaches MCP tools through use_tool (and finds them with
    # search_tool). Attached only inside the container, whose HOME holds no
    # other integrations; see _worker_tool_argv.
    worker_tool_style="grok",
    worker_tool_undeny=("use_tool", "search_tool"),
    neutral_gap=("grok: account-level <user_rules> are attached server-side and have no CLI "
                 "switch; each call's injected rules are recorded by hash in the trace"),
    disallowed_tools_separator=",",
    # Workflows removed at startup as well as denied by name.
    native_fanout_off_env={"GROK_WORKFLOWS": "0"},
    # Summary-only form: the guide documents ``--max-turns`` on the ``-p``
    # form, and no tool-less form, so the bound is one turn on top of the
    # restricted read-only allowlist and the full denial. A read tool that
    # is present but has no turn to run in is the honest description.
    summary_only_args=["--max-turns", "1"],
    max_turns_flag="--max-turns",
    turn_cap_by_count=True,
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


def contained(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the operator asserts an OS sandbox already bounds this process.

    Set by the isolated runner's launcher, never inferred. It asserts one
    thing: the container is the security boundary, so a vendor's own inner
    sandbox is redundant here and may be stood down where it cannot start.

    The case is Codex. Its CLI sandboxes model-run shell commands with
    bubblewrap, which needs an unprivileged user namespace, and the acceptance
    container denies that through Docker's seccomp profile. The sandbox cannot
    start, so the seat can run no command at all. Attempts 3 and 5 of the blind
    acceptance each spent a window on an OpenAI seat that could not read a
    single file; attempt 5's orchestrator correctly gave up and asked the
    operator for read access rather than inventing a task.

    The alternative was to let the container create user namespaces. Measured
    and rejected on 2026-09-17: ``--security-opt seccomp=unconfined`` makes the
    inner sandbox work, ``--cap-add SYS_ADMIN`` does not, so the only working
    route opens the surface behind most container escapes, for every process in
    the container, to enable a second sandbox inside a boundary that already
    holds writes to the work tree and a tmpfs on a read-only root with no
    capabilities. Weakening the wall that holds to prop up one that does not is
    the wrong trade.

    It applies to every seat on that vendor, bounded or agentic, and attempt 6
    is why that is stated rather than assumed. The first version substituted
    only on the restricted branch, which reads as the cautious choice and is
    the opposite: senior seats are *never* restricted, so the substitution
    could not reach an orchestrator, a lead, a reviewer or a consultant --
    which is every seat that had ever gone blind. Attempt 6 stopped at the
    same `bwrap` failure as attempt 5, at the same point, with this assertion
    set.

    Standing the sandbox down does not hand the permission axis away, because
    the sandbox flag was not what held it. A call without a write grant runs
    in a fresh source copy that is deleted when the call returns
    (``runtime.Fleet._invoke``), and a restricted editing seat returns a text
    patch the harness applies -- neither depends on the vendor refusing a
    write. What is given up is narrower and worth saying plainly: inside this
    container a Codex seat can now write into its own disposable copy, and a
    seat that already holds a write grant can write to the tmpfs HOME as well
    as to /work. Both disappear with the container, and a seat that can read
    that HOME could already read it under ``read-only``.

    Off by default, so an ordinary host run keeps every vendor sandbox. There
    the vendor's sandbox *is* the boundary, and standing it down would be a
    real loss rather than a redundant one.
    """
    environ = os.environ if env is None else env
    return environ.get('QUADRATUS_CONTAINED', '').strip().lower() in {'1', 'true', 'yes'}


class NativeControlOverride(ProviderError):
    """A configuration conflicts with the selected native-delegation policy."""


def neutral_preferences(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this run drops the operator's personal CLI configuration.

    Davis's ruling (2026-09-25): his personal rules stay active by default,
    because they are how he wants the models to work, and Quadratus builds
    the important ones in as product behaviour. This switch is the option to
    run without them, for a measurement that must not depend on one account.
    """
    environ = os.environ if env is None else env
    return environ.get("QUADRATUS_NEUTRAL_PREFERENCES", "").strip().lower() in {"1", "true", "yes"}


def native_delegation_mode(env: Optional[Mapping[str, str]] = None) -> str:
    environ = os.environ if env is None else env
    raw = environ.get('QUADRATUS_NATIVE_DELEGATION', '').strip().lower()
    if raw not in ('', 'vendor-default', 'off'):
        raise NativeControlOverride(
            'QUADRATUS_NATIVE_DELEGATION must be vendor-default or off')
    return raw or 'vendor-default'


def _split_tool_names(value: str, separator: str = " ") -> List[str]:
    """The names in one tool-list value, whichever separator the CLI uses."""
    return [name for name in re.split(r"[\s,]+" if separator.strip() == "" else re.escape(separator) + r"|\s+", value) if name]


def _fold_disallowed(argv: List[str], flag: str, extra: List[str], separator: str = " ") -> List[str]:
    """Add ``extra`` to an existing ``flag`` value rather than repeating the flag.

    Neither CLI documents that a repeated ``--disallowed-tools`` merges, so the
    names are folded into the one value, joined with the CLI's own separator.
    A flag with no value after it (last token) is left alone and the denial
    appended as its own pair.
    """
    if flag in argv:
        index = argv.index(flag) + 1
        if index < len(argv):
            present = _split_tool_names(argv[index], separator)
            argv[index] = separator.join(present + [name for name in extra if name not in present])
            return argv
    return argv + [flag, separator.join(extra)]


#: Prefix on a synthesised child's detail that marks it as *attempted*
#: delegation: a denied fan-out tool was named in the envelope, and whether it
#: executed or what it spent is unknown. Distinct from a codex child, which
#: the vendor's own stream reports as having run.
ATTEMPTED_DELEGATION = "ATTEMPTED native delegation"


def _denied_fanout_children(vendor: str, denied: List[str], stdout: str,
                            diagnostics) -> List[NativeChild]:
    """Suspected native children on the vendors whose control is a tool denial.

    Codex reports its own agents in its event stream; claude and grok do not.
    Under ``QUADRATUS_NATIVE_DELEGATION=off`` the only evidence about a denied
    fan-out tool is the envelope's own record of tool calls:

    * grok's ``toolCalls`` (read through the diagnostics whitelist, names
      only). A denied name that still appears there is an *attempt*: the
      turn asked for the tool. A cancelled or refused request leaves the
      same record as one that ran, so this does not establish that a child
      executed, and the child carries unknown usage and the
      ``ATTEMPTED_DELEGATION`` marker. The run still stops on it, because
      the 2026-09-12 experiment showed ``--always-approve`` overrides
      ``--disallowed-tools`` and an attempt under that flag may well have
      run; stopping on suspicion is the conservative reading and the
      record says "attempted", not "ran".
    * claude's ``--output-format json`` envelope carries no tool calls at all,
      only ``permission_denials``. A denial naming Task/Agent is the control
      *holding*, not a child, so it is recorded in the diagnostics
      (``denied_tools``) and not as a child. A claude child that was not
      denied is invisible in this output format; that gap is documented in
      the property below and is closed only by a live probe or stream-json.

    Nothing here is trusted as complete: an empty list means "no evidence",
    never "no child ran".
    """
    children: List[NativeChild] = []
    wanted = {name.lower() for name in denied}
    if vendor == "grok":
        names = (diagnostics or {}).get("attempted_tools") or []
        for name in names:
            if isinstance(name, str) and name.lower() in wanted:
                children.append(NativeChild(
                    session_id=f"unidentified:grok:{name}",
                    tool_name=name,
                    detail=f"{ATTEMPTED_DELEGATION}: denied fan-out tool named in the "
                           "envelope's tool calls; whether it executed and what it "
                           "spent are unknown",
                ))
    return children


def _claude_denied_fanout(stdout: str, denied: List[str]) -> List[str]:
    """Names in the claude result envelope's ``permission_denials`` that match a denied fan-out tool."""
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    wanted = {name.lower() for name in denied}
    found: List[str] = []
    for entry in payload.get("permission_denials") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("tool_name") or entry.get("toolName") or entry.get("name")
        if isinstance(name, str) and name.lower() in wanted and name not in found:
            found.append(name)
    return found


class CLIProvider(LLMProvider):
    """Base class for providers that drive a vendor CLI as a subprocess."""

    spec: CLISpec = CLAUDE_SPEC

    transport = "cli"

    def __init__(self, model, api_key=None, *, workdir=None, allow_writes=False, **kwargs):
        # CLI providers authenticate through the vendor's cached OAuth login, so
        # there is no key to supply. A truthy sentinel keeps the base class from
        # short-circuiting to "unavailable"; real availability is decided by
        # whether the binary resolves in _build_client.
        self._workdir = workdir
        self._owned_workdir: Optional[str] = None
        self._allow_writes = allow_writes
        #: One-turn, tool-less summary call on the restricted seat (closeout).
        #: Off by default; a per-call view sets it. See ``_build_argv``.
        self.summary_only: bool = bool(kwargs.pop("summary_only", False))
        #: A lead's agentic turn limit for this view, set per call by
        #: runtime.Fleet. None sends no flag. A proxy for spend, not a
        #: token ceiling: an 11-turn grok lead still reported 365,138 tokens.
        self.max_turns: Optional[int] = kwargs.pop("max_turns", None)
        #: The in-session worker tool for this view (``WorkerBridge.spec()``),
        #: set per lead call by runtime.Fleet. None attaches nothing.
        self.worker_tool: Optional[dict] = kwargs.pop("worker_tool", None)
        #: Per-run neutral mode from Settings; None falls back to the env var.
        self.neutral: Optional[bool] = None
        self._worker_tool_files: List[str] = []
        self.worker_tool_attached = False
        self._prompt_files = set()
        #: Real token counts from the most recent call, when the CLI reported
        #: them; None otherwise. Read by metering glue, never load-bearing.
        self.last_usage: Optional[Dict[str, int]] = None
        #: Vendor-native sub-agents seen in the last call's event stream.
        #: Observed, not dispatched: outside Quadratus worker selection and
        #: outside every per-task budget. Kept distinct so they are never
        #: folded into Quadratus-dispatched totals.
        self.native_children: List[NativeChild] = []
        #: Fan-out tool names this call denied under the run-wide off mode;
        #: set by ``_build_argv`` per call, empty when the mode is not on.
        self._native_fanout_denied: List[str] = []
        #: How the most recent call ended, in the bounded form the ledger may
        #: keep: stop reason, model-call count, attempted tool names. Reset at
        #: the start of every attempt so a stale record never describes a
        #: later call. Attached to a raised ProviderError as ``diagnostics``.
        self.last_diagnostics: Optional[Dict[str, object]] = None
        #: The tail of the CLI's stderr and the failed tool calls it reported,
        #: kept per attempt for the ledger. Q9 (2026-09-22): a blocked lead's
        #: bwrap error existed only in the model's prose, so nobody could
        #: check it. Bounded; never the whole transcript.
        self.last_stderr: str = ""
        self.last_tool_failures: List[Dict[str, object]] = []
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
        neutral = getattr(self, "neutral", None)
        if (neutral_preferences() if neutral is None else neutral) and spec.neutral_args:
            argv += list(spec.neutral_args)
        if self.restricted and spec.restricted_args:
            # A bounded call, not an agent. The permission axis does not apply:
            # readonly_args and write_args both describe what an agent may do
            # with its tools, and this seat's dangerous tools are absent rather
            # than governed.
            if contained() and spec.contained_sandbox_args:
                argv += list(spec.contained_sandbox_args)
            else:
                argv += list(spec.restricted_args)
        else:
            argv += list(spec.agentic_args)
            if contained() and spec.contained_sandbox_args:
                # Same substitution, same reason: this vendor's two permission
                # modes are one sandbox mechanism, and it cannot start here.
                # What the permission axis rested on survives the swap --
                # a call without a write grant runs in a disposable source
                # copy (``runtime.Fleet._invoke``), and a call with one runs
                # in a container whose root is read-only, so /work and a
                # tmpfs are the only writable paths either way.
                argv += list(spec.contained_sandbox_args)
            else:
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
        if self.summary_only:
            # The closeout form (2026-09-15): the record that survives a task
            # is written from the transcript and diff the caller supplies,
            # never by re-reading the project. Scored attempt 1 spent 258k
            # tokens on exactly that re-reading. So this view refuses to be
            # anything but bounded: restricted seat, empty directory, one
            # attempt, a minute, no operator override, and native fan-out
            # off whatever the run-wide mode says.
            if not self.restricted:
                raise ProviderError(f"{self.label}: summary_only requires the restricted seat form")
            if extra:
                raise NativeControlOverride(
                    f"QUADRATUS_CLI_ARGS_{spec.vendor.upper()} must be empty for a summary-only call")
            if self.max_retries != 1:
                raise ProviderError(f"{self.label}: summary_only allows exactly one attempt, not {self.max_retries}")
            timeout = self.timeout
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0 or timeout > 60):
                # nan compares false against everything, so "not > 60" is not
                # "<= 60"; the bound is stated positively.
                raise ProviderError(f"{self.label}: summary_only needs a finite timeout in (0, 60], not {timeout!r}")
            if os.listdir(self.workdir):
                raise ProviderError(f"{self.label}: summary_only requires an empty working directory")
            mode = 'off'
        tool = getattr(self, "worker_tool", None)
        tool_args = self._worker_tool_argv(tool) if tool and not self.summary_only else None
        self.worker_tool_attached = tool_args is not None
        if getattr(self, 'native_fanout_off', False):
            # Set per call on a view by runtime.Fleet for a seat whose job
            # excludes delegation (the verifier). The same denial, kill
            # switches and refusal of overrides as the run-wide off mode.
            mode = 'off'
        if mode == 'off' and spec.native_fanout_off_args:
            # Vendor-specific flag precedence is not a reliable generic
            # parser. In this strict opt-in mode, reject overrides rather
            # than let a later tools/settings flag silently undo the denial.
            if extra:
                raise NativeControlOverride(
                    f'QUADRATUS_CLI_ARGS_{spec.vendor.upper()} must be empty '
                    'when QUADRATUS_NATIVE_DELEGATION=off')
            flag, *names = spec.native_fanout_off_args
            denied = _split_tool_names(" ".join(names), spec.disallowed_tools_separator)
            if tool_args is not None:
                denied = [name for name in denied if name not in spec.worker_tool_undeny]
            argv = _fold_disallowed(argv, spec.disallowed_tools_flag or flag, denied,
                                    spec.disallowed_tools_separator)
            self._native_fanout_denied = denied
        else:
            self._native_fanout_denied = []
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
        argv += tool_args or []
        argv += list(spec.control_args)
        argv += extra
        if self.summary_only:
            argv += list(spec.summary_only_args)
        elif self.max_turns and spec.max_turns_flag:
            argv += [spec.max_turns_flag, str(int(self.max_turns))]
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

    def _extract(self, stdout: str) -> str:
        """The spec's extractor, with the turn cap recognised by count.

        GameTape run 3 (2026-09-25): grok at --max-turns 14 reported
        stopReason 'cancelled' with num_turns 14, not 'max_turns'. Read as a
        failed lead with changed source, it stopped the whole run -- the
        failure Codex warned the cap must not cause. When a cap was set and the
        envelope's own turn count reached it, an incomplete turn is the cap --
        for a CLI whose count is the cap's counter (``turn_cap_by_count``).
        Claude's is not, and it marks a real cap explicitly, which its
        extractor already raises as ``TurnLimitReached``.
        """
        try:
            return self.spec.extract(stdout)
        except ProviderRefusal:
            raise
        except TurnLimitReached:
            raise
        except ProviderError as exc:
            if not self.max_turns or self.summary_only or not self.spec.turn_cap_by_count:
                raise
            try:
                payload = json.loads(stdout or "")
            except ValueError:
                raise exc from None
            turns = _envelope_turns(payload) if isinstance(payload, dict) else None
            if turns is None or turns < int(self.max_turns):
                raise
            text = payload.get("text") or payload.get("result")
            raise TurnLimitReached(
                f"{self.spec.vendor} stopped at its turn limit ({turns} of {self.max_turns}) before finishing",
                partial_text=text if isinstance(text, str) else None, turns=turns) from exc

    def _worker_tool_argv(self, tool: dict) -> Optional[List[str]]:
        """Arguments that attach the in-session worker tool, or None.

        Forms from the 2026-09-25 probe. claude takes an inline MCP config and
        --strict-mcp-config, so no user or project server loads beside it.
        codex takes -c overrides; without the per-server approval mode an MCP
        call is refused under approval policy "never". grok reads MCP servers
        only from a project-scoped .grok/config.toml and starts them only in a
        trusted folder; --trust persists that trust in ~/.grok, so grok gets
        the tool only inside the container, where HOME is disposable, and only
        when the directory has no .grok of its own to overwrite.
        """
        name = tool["name"]
        style = self.spec.worker_tool_style
        if style == "claude":
            config = {"mcpServers": {name: {"command": tool["command"], "args": tool["args"],
                                            "env": tool["env"]}}}
            return ["--mcp-config", json.dumps(config), "--strict-mcp-config",
                    "--allowedTools", f"mcp__{name}__commission_worker"]
        if style == "codex":
            env = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in tool["env"].items()) + "}"
            prefix = f"mcp_servers.{name}"
            return ["-c", f"{prefix}.command={json.dumps(tool['command'])}",
                    "-c", f"{prefix}.args={json.dumps(tool['args'])}",
                    "-c", f"{prefix}.env={env}",
                    "-c", f'{prefix}.default_tools_approval_mode="approve"',
                    "-c", f"{prefix}.tool_timeout_sec={int(tool['timeout'])}",
                    "-c", f"{prefix}.startup_timeout_sec=30"]
        if style == "grok" and contained():
            folder = Path(self.workdir) / ".grok"
            if folder.exists():
                return None
            env = "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in tool["env"].items()) + " }"
            folder.mkdir()
            (folder / "config.toml").write_text(
                f"[mcp_servers.{name}]\ncommand = {json.dumps(tool['command'])}\n"
                f"args = {json.dumps(tool['args'])}\nenv = {env}\n"
                f"tool_timeout_sec = {int(tool['timeout'])}\n", encoding="utf-8")
            # Reassigned, not appended: views are shallow copies.
            self._worker_tool_files = [*self._worker_tool_files, str(folder)]
            return ["--trust"]
        return None

    def _remove_worker_tool_files(self) -> None:
        for path in self._worker_tool_files:
            shutil.rmtree(path, ignore_errors=True)
        self._worker_tool_files = []

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
        self.last_session_id = None  # likewise: a trace must never join the wrong transcript
        self.last_stderr = ""
        self.last_tool_failures = []
        composed = self._compose_prompt(prompt, system, history)
        argv = self._build_argv(composed, system)
        env = {**os.environ, **self.spec.env}
        if getattr(self, "_native_fanout_denied", None):
            env.update(self.spec.native_fanout_off_env)
        if self.worker_tool_attached and self.spec.worker_tool_style == "claude":
            # claude's MCP tool-call timeout, in milliseconds.
            env["MCP_TOOL_TIMEOUT"] = str(int(self.worker_tool["timeout"]) * 1000)
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
            errors = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            self.last_stderr = (errors or "")[-STDERR_TAIL:]
            self._observe_output(output or "")
            raise TimeoutError(
                f"{self.label} CLI timed out after {self.timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise ProviderError(f"{self.label} CLI vanished from PATH: {exc}") from exc
        finally:
            self._remove_worker_tool_files()
            if self.spec.prompt_file_flag and self.spec.prompt_file_flag in argv:
                path = argv[argv.index(self.spec.prompt_file_flag) + 1]
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                self._prompt_files.discard(path)

        self._observe_output(proc.stdout)
        self.last_stderr = (proc.stderr or "")[-STDERR_TAIL:]

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
                self._extract(proc.stdout)
            except (ProviderError, ProviderRefusal) as parsed:
                parsed.diagnostics = self.last_diagnostics
                if isinstance(parsed, TurnLimitReached) or getattr(parsed, "auth_invalid", False):
                    raise
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
            return self._extract(proc.stdout)
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
            self.last_tool_failures = (
                self.spec.extract_tool_failures(stdout or "") if self.spec.extract_tool_failures else []
            )
            self.native_children = _extract_native_children(stdout)
            denied = list(getattr(self, "_native_fanout_denied", []) or [])
            if denied:
                self.native_children.extend(_denied_fanout_children(
                    self.spec.vendor, denied, stdout, self.last_diagnostics
                ))
                if self.spec.vendor == "claude":
                    held = _claude_denied_fanout(stdout, denied)
                    if held:
                        diagnostics = dict(self.last_diagnostics or {})
                        diagnostics["denied_tools"] = held
                        self.last_diagnostics = diagnostics
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
                elif event.get("sessionId"):
                    # grok spells it this way; without it no grok row had an id
                    self.last_session_id = event["sessionId"]
                # A Claude result names concrete releases, while argv often
                # uses a moving alias. Auxiliary modelUsage rows are not seats.
                models = event.get("modelUsage") or {}
                matching = [name for name in models if self.model and self.model in name]
                if len(matching) == 1:
                    self.resolved_model = matching[0]
            if not getattr(self, "last_session_id", None):
                try:
                    whole = json.loads(stdout or "")
                    if isinstance(whole, dict) and isinstance(whole.get("sessionId"), str):
                        self.last_session_id = whole["sessionId"]
                except ValueError:
                    pass
            if self.spec.vendor == "openai" and getattr(self, "last_session_id", None):
                from .native_sessions import codex_children
                root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
                if self.last_usage is None:
                    # Diagnostic only (Codex review of #25): a stream that ended
                    # before response.completed may have unreported usage, so
                    # the session record's total is a lower bound and never
                    # clears unknown usage. The budget still stops on it.
                    floor = _codex_rollout_usage(root, self.last_session_id)
                    if floor is not None:
                        self.last_diagnostics = dict(self.last_diagnostics or {}, usage_lower_bound=floor)
                self.native_children.extend(codex_children(
                    root, self.last_session_id, self.workdir,
                    ended=datetime.now(timezone.utc),
                ))
            if self.native_children and self.native_delegation_disabled:
                # The switch was sent and the record shows native activity
                # anyway. Say so on the record itself, where the ledger and
                # the evidence bundle will carry it; a child silently filed
                # under "observed" would read as the expected state of a
                # vendor without a switch, which this vendor no longer is.
                # Two wordings, because two kinds of evidence: a child the
                # vendor's own stream reports (codex) *ran*; a denied tool
                # name in an envelope (grok) was *attempted*, and the
                # public record must not claim execution from a name.
                sent = " ".join(token for token in (
                    list(self.spec.control_args) or (
                        [self.spec.disallowed_tools_flag or (self.spec.native_fanout_off_args or [""])[0]]
                        + list(getattr(self, "_native_fanout_denied", []) or [])
                    )
                ) if token)
                ran = f"CONTROL FAILURE: native child ran although the call sent {sent}"
                attempted = (
                    "CONTROL FAILURE (suspected): a denied fan-out tool was attempted "
                    f"although the call sent {sent}; execution and usage unknown"
                )
                annotated = []
                for child in self.native_children:
                    note = attempted if child.detail.startswith(ATTEMPTED_DELEGATION) else ran
                    log.warning("%s: %s", self.label, note)
                    annotated.append(_annotate_child(child, note))
                self.native_children = annotated
        except Exception:
            log.debug("native accounting unavailable", exc_info=True)

    @property
    def native_delegation_disabled(self) -> bool:
        """Whether this transport tells the CLI not to spawn its own agents.

        True where the spec carries a control (codex's ``--disable`` pair), or
        where this call folded the run-wide off-mode denial into its argv
        (claude's and grok's ``--disallowed-tools``). A vendor without either
        is reported as such rather than assumed bounded.

        What each is evidence of differs. Codex reports its children in its
        own stream, so a child seen there is a confirmed control failure.
        Grok's denial is unverified and its envelope lists tool calls, so a
        denied tool named there is a *suspected* failure: an attempt with
        unknown execution and usage, stopped on conservatively and recorded
        as attempted. Claude's json envelope shows only denials, so on
        claude a child that was *not* denied is not observable here at all;
        that is a live-probe question, not one this property answers.
        """
        return bool(self.spec.control_args) or bool(getattr(self, "_native_fanout_denied", []))

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
