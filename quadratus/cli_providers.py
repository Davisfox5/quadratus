"""Subscription-backed providers that drive the vendor coding-agent CLIs.

Instead of calling a billed HTTP API, these providers shell out to the CLI the
vendor ships with its consumer subscription -- ``claude`` (Claude Pro/Max),
``codex`` (ChatGPT Plus/Pro), ``agy`` (Google AI Pro/Ultra), ``grok``
(SuperGrok / X Premium+). Each CLI is already logged in via its own OAuth flow,
so no API key is involved and usage draws on the subscription's rate-limit
window rather than per-token billing.

Scope of use
------------
Vendor terms permit this for ordinary *individual* use: you, on your own
machine, against your own subscription. Routing other people's prompts through
your credential is prohibited by all three major vendors. The GUI must
therefore never be exposed publicly while a CLI backend is active; see
``gui.py``, which refuses to enable Gradio sharing in that configuration.

Design notes
------------
* **Flag specs are declarative.** Each CLI is described by a :class:`CLISpec`
  so that a vendor renaming a flag is a one-line fix rather than a code change.
  Only the ``claude`` spec has been verified against a live binary (v2.1.228);
  the others are marked ``verified=False`` and should be confirmed against the
  installed CLI before being relied on.
* **Agents are sandboxed by default.** These CLIs can read and write files. A
  reviewer that is merely asked to critique a solution will happily go edit the
  working tree, and with several models running that becomes write-thrash. Every
  provider therefore runs in a scratch directory with tools denied unless a
  caller explicitly opts into write access.
* **Prompt caching dominates cost.** A cold invocation carries ~20k tokens of
  agent scaffolding; within the vendor's cache window a warm one costs ~10% of
  that, and swapping the system prompt only re-creates the changed suffix
  (~3.4k). Keeping a run's invocations close together therefore matters far
  more than trimming individual prompts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .providers import LLMProvider, ProviderError, ProviderRefusal, Turn

log = logging.getLogger(__name__)

__all__ = [
    "CLISpec",
    "CLIProvider",
    "ClaudeCLIProvider",
    "CodexCLIProvider",
    "AntigravityCLIProvider",
    "GrokCLIProvider",
    "CLI_SPECS",
    "cli_provider_classes",
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

    The stream is newline-delimited events; the last one carrying assistant
    text is the answer. Falls back to raw stdout if nothing parses, which keeps
    the provider working if the event shape changes.
    """
    last = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for key in ("last_agent_message", "text", "message", "result"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                last = value
            elif isinstance(value, dict):
                inner = value.get("content") or value.get("text")
                if isinstance(inner, str) and inner.strip():
                    last = inner
    return last or stdout.strip()


def _extract_plain(stdout: str) -> str:
    return stdout.strip()


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
    #: Whether these flags have been checked against a real binary.
    verified: bool = False
    #: Environment variables to set for the subprocess.
    env: Dict[str, str] = field(default_factory=dict)


#: Verified against claude 2.1.228. ``--system-prompt`` fully replaces the
#: default Claude Code system prompt (it does not merely append), which keeps
#: an assigned collaboration role from competing with the coding-agent persona.
CLAUDE_SPEC = CLISpec(
    binary="claude",
    print_flag=["-p"],
    system_flag="--system-prompt",
    model_flag="--model",
    output_args=["--output-format", "json"],
    readonly_args=["--disallowed-tools", "Bash Edit Write NotebookEdit"],
    extract=_extract_claude_result,
    # Mandatory, not merely safer: --disallowed-tools is variadic, so a
    # positional prompt after it is swallowed as another tool name and the CLI
    # exits with "Input must be provided...". Stdin also sidesteps the argv
    # length limit, which whole code artifacts would otherwise hit.
    prompt_on_stdin=True,
    verified=True,
    extract_usage=_extract_claude_usage,
)

#: Unverified: no ``codex`` binary was available to check against.
CODEX_SPEC = CLISpec(
    binary="codex",
    subcommand=["exec"],
    system_flag=None,  # codex exec has no system-prompt flag; folded into prompt
    model_flag="--model",
    output_args=["--json"],
    readonly_args=["--sandbox", "read-only"],
    extract=_extract_codex_result,
    prompt_on_stdin=True,
    verified=False,
)

#: Unverified. Antigravity CLI replaced Gemini CLI for consumer Google
#: accounts after Gemini CLI's OAuth login was retired in June 2026.
ANTIGRAVITY_SPEC = CLISpec(
    binary="agy",
    print_flag=["-p"],
    system_flag=None,
    model_flag="--model",
    output_args=[],
    readonly_args=[],
    extract=_extract_plain,
    prompt_on_stdin=True,
    verified=False,
)

#: Unverified. Grok Build CLI authenticates against SuperGrok / X Premium+.
GROK_SPEC = CLISpec(
    binary="grok",
    print_flag=["-p"],
    system_flag=None,
    model_flag="--model",
    output_args=[],
    readonly_args=[],
    extract=_extract_plain,
    prompt_on_stdin=True,
    verified=False,
)

CLI_SPECS: Dict[str, CLISpec] = {
    "claude": CLAUDE_SPEC,
    "openai": CODEX_SPEC,
    "gemini": ANTIGRAVITY_SPEC,
    "grok": GROK_SPEC,
}


def _render_history(history: Sequence[Turn]) -> str:
    if not history:
        return ""
    lines = ["--- Prior conversation ---"]
    for turn in history:
        who = "User" if turn.role == "user" else "Assistant"
        lines.append(f"[{who}] {turn.content}")
    lines.append("--- End prior conversation ---")
    return "\n".join(lines)


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
        #: Real token counts from the most recent call, when the CLI reported
        #: them; None otherwise. Read by metering glue, never load-bearing.
        self.last_usage: Optional[Dict[str, int]] = None
        super().__init__(model, api_key="cli-oauth", **kwargs)

    # -- lifecycle -----------------------------------------------------------
    def _build_client(self):
        path = shutil.which(self.spec.binary)
        if not path:
            raise CLINotInstalled(
                f"'{self.spec.binary}' is not on PATH. Install the CLI and sign in "
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
        if self._owned_workdir:
            shutil.rmtree(self._owned_workdir, ignore_errors=True)
            self._owned_workdir = None

    # -- invocation ----------------------------------------------------------
    def _build_argv(self, prompt: str, system: str) -> List[str]:
        spec = self.spec
        argv = [self._client, *spec.subcommand, *spec.print_flag]

        if spec.system_flag and system:
            argv += [spec.system_flag, system]
        if spec.model_flag and self.model:
            argv += [spec.model_flag, self.model]
        argv += list(spec.output_args)
        if not self._allow_writes:
            argv += list(spec.readonly_args)
        if not spec.prompt_on_stdin:
            argv.append(prompt)
        return argv

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
            proc = subprocess.run(
                argv,
                input=composed if self.spec.prompt_on_stdin else None,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self.workdir,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"{self.label} CLI timed out after {self.timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise ProviderError(f"{self.label} CLI vanished from PATH: {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise RuntimeError(
                f"{self.label} CLI exited {proc.returncode}: {detail}"
            )

        if self.spec.extract_usage is not None:
            try:
                self.last_usage = self.spec.extract_usage(proc.stdout)
            except Exception:  # noqa: BLE001 -- metering must never fail a call
                self.last_usage = None
        try:
            return self.spec.extract(proc.stdout)
        except ProviderRefusal as refusal:
            refusal.model = self.model
            raise

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


class AntigravityCLIProvider(CLIProvider):
    name = "gemini"
    label = "Gemini"
    spec = ANTIGRAVITY_SPEC


class GrokCLIProvider(CLIProvider):
    name = "grok"
    label = "Grok"
    spec = GROK_SPEC


def cli_provider_classes() -> Dict[str, type]:
    """Map provider name to its CLI-backed implementation."""
    return {
        "claude": ClaudeCLIProvider,
        "openai": CodexCLIProvider,
        "gemini": AntigravityCLIProvider,
        "grok": GrokCLIProvider,
    }
