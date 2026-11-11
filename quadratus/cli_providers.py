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
import errno
import hashlib
import json
import logging
import math
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .delegation import NativeChild
from .native_sessions import bounded_lines, first_record
from .providers import (
    LLMProvider,
    PermissionBlocked,
    ProviderError,
    ProviderRefusal,
    TransientOverload,
    Turn,
    TurnLimitReached,
    carry_transport_facts,
    scratch_root,
)

log = logging.getLogger(__name__)

__all__ = [
    "CLISpec",
    "CLIProvider",
    "ClaudeCLIProvider",
    "CodexCLIProvider",
    "GrokCLIProvider",
    "CLI_SPECS",
    "CLIOutputTooLarge",
    "CODEX_NATIVE_DELEGATION_CONTROL",
    "CODEX_NATIVE_DELEGATION_FEATURES",
    "CODEX_NATIVE_DELEGATION_OVERRIDE",
    "cli_provider_classes",
    "codex_override_conflicts",
    "codex_copy_boundary_conflicts",
    "native_delegation_mode",
    "NativeControlOverride",
]


class CLINotInstalled(RuntimeError):
    """The vendor CLI binary could not be found on PATH."""


#: Phrases a CLI uses when the *subscription* is out, as opposed to a
#: momentary server-side rate limit. Deliberately narrow: treating a transient
#: 429 as an exhausted window would take a healthy model out of the run for
#: the session.
#:
#: The first entry is not a guess. Observed verbatim from the Claude CLI on
#: 2026-09-12: "You've reached your Fable limit. Switch to another model, or
#: manage usage credits at claude.ai/..." -- which matched none of the
#: patterns written from imagination, so the run saw a generic transport
#: error instead of an exhausted window. Add what you actually see here.
#:
#: Read only in a CLI's own error report (``_own_error_facts``), never over
#: an exception's whole message: that message can quote what a model was
#: saying when its turn stopped, and a cancelled grok turn whose narration
#: said "limit reached" once took its model off the roster for the run.
LIMIT_WORDING = (
    "reached your",          # "You've reached your Fable limit" (Claude, seen)
    "usage limit",
    "usage limits",
    "quota exceeded",
    "out of credits",
    "subscription limit",
    "weekly limit",
    "plan limit",
    "limit reached",
    "upgrade to continue",
)

#: HTTP statuses that mean the request failed for a reason that passes: a
#: rate limit, a timeout, or the vendor's servers. 529 is Anthropic's
#: "overloaded".
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})

#: Optional error-class prefixes a runtime puts before its message.
_ERROR_PREFIX = r"(?:(?:[A-Za-z_][\w.]*)?(?:Error|ERROR|error): |fatal: ){0,3}"
#: The whole-line shapes of a transport failure, as the CLIs and the
#: runtimes under them print one. A line counts only when all of it is the
#: shape (``fullmatch``), so a sentence that mentions "500" or "connection"
#: is not one. Seeds like every table here: add the line you actually saw.
_TRANSPORT_SHAPES = (
    # The vendor API's own status, as each CLI words it.
    re.compile(_ERROR_PREFIX + r"API Error: (?P<status>\d{3})\b.*"),
    re.compile(_ERROR_PREFIX + r"(?:unexpected status|exceeded retry limit, last status:) (?P<status>\d{3})\b.*"),
    # The connection under the request failed.
    re.compile(_ERROR_PREFIX + r"API Error: Connection error\b.*"),
    re.compile(_ERROR_PREFIX + r"(?:connect|read|write|getaddrinfo) "
               r"(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ENETUNREACH|EHOSTUNREACH|EPIPE)\b.*"),
    re.compile(_ERROR_PREFIX + r"(?:socket hang up|fetch failed|stream disconnected before completion\b.*)"),
)
#: How many lines at each end of a CLI's own error text are read for those
#: shapes. Both ends, because a runtime names the error first and prints a
#: stack trace after it: read from the end alone, a spent window followed by
#: a dozen "at ..." lines was a plain transport error and the seat never moved.
_SHAPE_LINES = 10
#: How much of the start of a CLI's stderr is read beside its end
#: (``STDERR_TAIL``).
STDERR_HEAD = 400


def _own_stderr(stderr) -> str:
    """The two ends of a CLI's stderr, in whole lines, for ``_own_error_facts``.

    A line the cut went through is dropped: the shapes are whole-line shapes,
    and the last part of a sentence that only quotes one would match as one.
    """
    text = stderr if isinstance(stderr, str) else ""
    if len(text) <= STDERR_HEAD + STDERR_TAIL:
        return text
    head, tail = text[:STDERR_HEAD], text[-STDERR_TAIL:]
    if text[STDERR_HEAD] != "\n":
        head = head[:head.rfind("\n") + 1]
    if text[-STDERR_TAIL - 1] != "\n":
        tail = tail[tail.find("\n") + 1:] if "\n" in tail else ""
    return head + "\n" + tail


def _own_error_facts(text, status=None) -> Dict[str, object]:
    """The typed facts in a CLI's own report of a failed request.

    ``status`` is an HTTP status the CLI's JSON states. ``text`` is error
    text the CLI itself wrote: an error field of its envelope or the two
    ends of its stderr. Never the model's answer or narration, which is why
    callers pass a field and not a whole stream.

    A spent window is limit wording beside no status or beside 429, and is
    never retried: nothing changes until the window rolls over. Any other
    status in ``RETRYABLE_STATUS``, or a whole-line connection failure, is a
    failure that passes.
    """
    status = status if type(status) is int else None
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) > 2 * _SHAPE_LINES:
        lines = lines[:_SHAPE_LINES] + lines[-_SHAPE_LINES:]
    network = False
    for line in lines:
        for shape in _TRANSPORT_SHAPES:
            match = shape.fullmatch(line)
            if match is None:
                continue
            found = match.groupdict().get("status")
            if found is None:
                network = True
            elif status is None:
                status = int(found)
    lowered = "\n".join(lines).lower()
    exhausted = status in (None, 429) and any(marker in lowered for marker in LIMIT_WORDING)
    facts: Dict[str, object] = {}
    if exhausted:
        facts["window_exhausted"] = True
    elif status in RETRYABLE_STATUS or (network and status is None):
        facts["retryable"] = True
    if status is not None:
        facts["transport_status"] = status
    return facts


def _mark_transport(error: BaseException, text="", status=None) -> BaseException:
    """Set the typed transport facts on ``error`` from the CLI's own report."""
    for name, value in _own_error_facts(text, status).items():
        setattr(error, name, value)
    return error


def _no_envelope(message: str, *, exit_report: bool = False) -> ProviderError:
    """The failure for a stdout that holds no answer and no report of the
    CLI's own: not the envelope it was asked for, or a stream with nothing
    in it.

    Marked, because on a failed exit the end of stderr is then the CLI's
    only voice, and ``CLIProvider._call`` reads the typed facts from it. A
    spent grok or codex window that is reported on stderr alone was once
    recognised by searching the whole message; with that search gone, this
    mark is what keeps it recognised.

    ``exit_report``: on a failed exit the exit status and stderr replace this
    message altogether, since they say more than "not JSON" does (claude,
    whose failed exits always read that way).
    """
    error = ProviderError(message)
    error.no_envelope = True
    error.exit_report = exit_report
    return error


class StaleClient(RuntimeError):
    """An installed vendor client is older than the version this engine
    qualified. Raised by the run preflight before any model call,
    never by a provider: the seat stays constructed and reported, the run
    is refused with both versions named (see ``CLISpec.minimum_client``)."""


#: The version ``<binary> --version`` reported, read once per executable
#: identity (path, arguments, device, inode, size, mtime) per process and
#: kept for the status line, the invocation ledger and the preflight. A
#: replaced or upgraded executable at the same path is a new identity and
#: is read again, so a long-lived process sees the operator's remedy.
_CLIENT_VERSIONS: Dict[tuple, Optional[str]] = {}
_VERSION = re.compile(r"(?<![\w.])(\d{1,5})\.(\d{1,5})\.(\d{1,5})(?:-([0-9A-Za-z.]{1,32}))?(?![\w.])")
#: What a recorded client version may look like: exactly the bounded
#: ``major.minor.patch[-pre]`` form, never the binary's own line.
CLIENT_VERSION_FORM = re.compile(r"\d{1,5}\.\d{1,5}\.\d{1,5}(?:-[0-9A-Za-z.]{1,32})?")


def parse_client_version(text) -> Optional[tuple]:
    """The first ``major.minor.patch[-pre]`` in ``text`` as an orderable
    tuple, or None. A pre-release orders below its release (semver), so
    ``0.162.0-alpha.17.2 < 0.162.0 < 0.163.0``."""
    match = _VERSION.search(str(text or ""))
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), 0 if pre is None else -1, pre or "")


def _version_text(match) -> str:
    """The bounded ``major.minor.patch[-pre]`` a match found, and nothing
    around it: no product name, path, error or token text."""
    major, minor, patch, pre = match.groups()
    return f"{int(major)}.{int(minor)}.{int(patch)}" + (f"-{pre}" if pre else "")


def client_version_at_least(found, minimum) -> Optional[bool]:
    """Whether ``found`` (version text) meets ``minimum`` (version text);
    None when either does not parse: unknown is never a pass."""
    have, want = parse_client_version(found), parse_client_version(minimum)
    if have is None or want is None:
        return None
    return have[:4] >= want[:4]


#: Vendor wording for a spent subscription window, as seen or documented.
#: One tuple with ``LIMIT_WORDING``, so the typed fact the fleet routes on
#: (``window_exhausted``) and the class the ledger records cannot drift apart.
EXHAUSTION_MARKERS = LIMIT_WORDING
#: Vendor wording for a rejected sign-in (codex 401 seen on GameTape run 6;
#: grok "Not signed in" seen on blind-acceptance attempt 7).
_AUTH_MARKERS = ("unauthori", "not signed in", "not logged in", "sign in again", "authentication")
#: The classes a failed call may be recorded as. Anything else is unknown,
#: and unknown is recorded by absence, never guessed.
FAILURE_CLASSES = ("cap", "auth", "rate_limited", "unavailable")
#: A status in a failure message, only where the wording says it is one:
#: "last status: 429 Too Many Requests" (codex), "API Error: 429 temporary
#: capacity" (claude), "HTTP 503", "status code 502". A bare number in
#: other prose ("cannot open file429.py", "line 503") is not a status
#: (root's review of ace40f8). Read only from text a vendor reported as
#: an error, never from an answer; unknown otherwise.
_STATUS_IN_TEXT = re.compile(
    r"(?i)(?:\bstatus(?:\s+code)?\s*[:=]?\s*|\bhttp\s*[:=]?\s*|\berror\s*[:=]\s*)([45]\d{2})(?![\d.\w])")


def failure_facts(status, text) -> Dict[str, object]:
    """Bounded facts about how a failed call ended, shared by every vendor's
    extractor: the HTTP ``status`` the client reported (an integer field or
    a bare 4xx or 5xx in its error message) and a ``failure_class`` drawn only
    from what that status and the vendor's own wording establish. A cap
    (the subscription window) outranks a 429, because the vendor says it
    with the same status; an auth rejection is 401 or 403 or its wording;
    429 alone is rate limiting; 502, 503 and 504 are the service being
    unavailable. Nothing else is classified, no retry delay is inferred,
    and no text is kept: the message stays in the ledger's stderr tail."""
    facts: Dict[str, object] = {}
    message = str(text or "")
    if type(status) is not int or not 100 <= status <= 599:
        found = _STATUS_IN_TEXT.search(message)
        status = int(found.group(1)) if found else None
    if status is not None:
        facts["status"] = status
    lowered = message.lower()
    if any(marker in lowered for marker in EXHAUSTION_MARKERS):
        facts["failure_class"] = "cap"
    elif status in (401, 403) or any(marker in lowered for marker in _AUTH_MARKERS):
        facts["failure_class"] = "auth"
    elif status == 429:
        facts["failure_class"] = "rate_limited"
    elif status in (502, 503, 504):
        facts["failure_class"] = "unavailable"
    return facts


def client_version(path: str, args: Sequence[str]) -> Optional[str]:
    """The version the installed client reports (``0.154.0`` from
    ``codex-cli 0.154.0``), read once per executable identity with a
    bounded subprocess. None, meaning unknown, when the executable cannot
    be stat'ed or run, exits non-zero, or prints no recognised version on
    its first stdout line; stderr is never read, and nothing but the
    bounded version form is kept. Never a model call, never raises."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, tuple(args), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    if key not in _CLIENT_VERSIONS:
        text = None
        try:
            # Printing a version needs no sign-in and no credential, so this
            # child gets the probe profile like every other one.
            from .child_env import check_env
            env, _ = check_env()
            proc = subprocess.run([path, *args], capture_output=True, text=True, timeout=15, check=False,
                                  env=env)
            first = (proc.stdout or "").strip().splitlines()
            match = _VERSION.search(first[0][:200]) if proc.returncode == 0 and first else None
            text = _version_text(match) if match else None
        except (OSError, subprocess.SubprocessError, ValueError):
            text = None
        _CLIENT_VERSIONS[key] = text
    return _CLIENT_VERSIONS[key]


def _extract_claude_result(stdout: str) -> str:
    """Pull the assistant text out of ``claude --output-format json``."""
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        # Empty output, or text that is not JSON. Left to raise, this was a
        # JSONDecodeError the caller reported as a transport failure with no
        # diagnostics attached.
        shown = (stdout or "").strip()[:300] if isinstance(stdout, str) else ""
        raise _no_envelope(f"claude did not return a JSON envelope: {shown or '(empty output)'}",
                           exit_report=True) from exc
    if not isinstance(payload, dict):
        # ``[]`` or ``null`` parses, and then has no ``.get``.
        raise _no_envelope(f"claude returned unexpected JSON ({type(payload).__name__}): "
                           f"{stdout.strip()[:300]}", exit_report=True)
    if payload.get("stop_reason") == "refusal":
        # A classifier decline is not an error to the CLI: it is a completed
        # turn with nothing in it. Surface it as what it is, not as "empty
        # response": the run preserves a refusal and never retries or
        # re-routes the declined request through another model.
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
        # An error_during_execution envelope carries no result, only an
        # ``errors`` list; without it the message read "claude reported an
        # error: " and named nothing (lifecycle replay matrix, 2026-09-26).
        errors = payload.get("errors")
        detail = payload.get("result") or (
            "; ".join(str(e) for e in errors) if isinstance(errors, list) else "")
        if not str(detail).strip():
            detail = f"no detail in the envelope (subtype {payload.get('subtype')!r})"
        error = ProviderError(f"claude reported an error: {str(detail)[:300]}")
        # The typed facts come from the envelope's own status. Its text is
        # read as well only where it must be the CLI's: beside a status, or
        # when the model wrote nothing (a rejected request reports zero
        # output tokens). An error envelope around model output says nothing.
        status = payload.get("api_error_status")
        usage = payload.get("usage")
        silent = (isinstance(usage, dict) and type(usage.get("output_tokens")) is int
                  and usage["output_tokens"] == 0)
        own = str(detail) if type(status) is int or silent else ""
        if _own_error_facts(own, status).get("transport_status") == 529 and isinstance(usage, dict):
            # Overloaded, typed by the envelope's own status (or the client's
            # "API Error: 529" error line, read only where the text is the
            # CLI's), with usage the envelope settled: the failure
            # _generate_once re-sends under its own bound and budget check
            # (MAX_TRANSIENT_RETRIES; cycle06 run).
            error = TransientOverload(str(error), status=529)
        raise _mark_transport(error, own, status)
    if payload.get("stop_reason") == "max_tokens":
        # The output limit cut the final message: whatever text came back is
        # where the answer stopped, not the answer (the API's documented stop
        # reason; not yet seen from the installed binary). Kept as evidence
        # on the error, never returned as a completed reply.
        error = ProviderError("claude stopped at its output limit before finishing the reply")
        error.partial_text = payload.get("result")
        raise error
    result = payload.get("result")
    return result if isinstance(result, str) else ""


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
    #: An explicit terminal failure, held apart from its detail: a
    #: turn.failed whose error carries no message, an empty one or a
    #: malformed one is still a failed turn (root's review of ace40f8,
    #: where {"status": 503} alone let an earlier message through).
    failed = False
    failure_detail = ""
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
        if event.get("type") == "turn.failed":
            failed = True
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            status = error.get("status") if isinstance(error, dict) else None
            failure_detail = (message.strip() if isinstance(message, str) and message.strip()
                              else f"status {status}" if type(status) is int
                              else "the client reported no detail")
        elif event.get("type") == "turn.completed":
            # A completed turn after a failed one is the client's own
            # recovery (0.163 waits and resumes inside the sampling
            # session): the last terminal event in stream order decides.
            failed = False
            failure_detail = ""
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
    if answer and not failed:
        return answer
    if failed and re.search(r"\b401\b.*unauthori[sz]ed|unauthori[sz]ed.*\b401\b", failure_detail, re.IGNORECASE):
        # GameTape run 6: the turn failed with 401 after the websocket dropped.
        # A run on a copied sign-in loses it when another codex client on the
        # same account refreshes the token. Named, and vendor-wide.
        error = ProviderError(
            "codex sign-in was rejected (401 Unauthorized). The subscription token in this "
            "environment is no longer valid -- usually because another codex client on the same "
            "account refreshed it. Sign in again before the next run.")
        error.auth_invalid = True
        raise error
    if failed:
        # The turn ended in failure whatever came before it. Text an earlier
        # agent_message carried is where the turn had got to (an interim
        # message, a draft of the reply) and is kept on the error as
        # evidence; returning it as the answer made a 429 after a progress
        # message read as a completed delivery.
        error = ProviderError(f"codex turn failed: {failure_detail[:300]}"
                              + (f" (after a reported error: {errors[-1][:200]})" if errors else ""))
        error.partial_text = answer or None
        # ``turn.failed`` and ``error`` items are codex's own report, not the
        # model's text, so the typed facts may be read from them.
        raise _mark_transport(error, failure_detail)
    if errors:
        raise _mark_transport(ProviderError(f"codex reported an error: {errors[-1][:300]}"), errors[-1])
    if saw_json:
        raise _no_envelope(
            "codex produced no assistant message. The usual cause is a model "
            "the CLI does not serve: the turn starts and ends with nothing in "
            "it. Check `codex exec --model <name>` by hand."
        )
    raise _no_envelope(f"codex returned no parseable events: {stdout.strip()[:300]}")


#: Grok stop reasons that are an explicit decline. Only these: a refusal is
#: never inferred from prose or from any other stop.
_GROK_REFUSAL_STOPS = ("refusal", "content_filter")

#: Grok stop reasons that are the agent loop's own cap, not a failure: the
#: call ran, its usage is real, and a writable call may have changed files.
#: ``max_turn_requests`` is the installed CLI's name for it (grok 1.0.62,
#: stop vocabulary end_turn / max_tokens / max_turn_requests / refusal /
#: cancelled, confirmed by the operator against the binary on 2026-10-25;
#: the published headless runner maps StopReason::MaxTurnRequests to it).
#: Demonstrated live: proof-workflow-01 t4's second revision stopped there
#: after 8 turns and was read as a failed, unmerged task instead of a cap.
#: ``max_turns`` is kept as the older spelling. ``max_turns_reached`` is an
#: event name in the same binary, not a stop reason, and is not read here;
#: a bare ``cancelled`` at the configured count stays with the count rule
#: below (``_is_cap_shaped``), and every other incomplete stop is a failure.
_GROK_CAP_STOPS = ("max_turns", "max_turn_requests")


def _is_cap_shaped(payload: dict) -> bool:
    """Whether an incomplete envelope has the one shape a count-read cap takes.

    GameTape run 3's cap was a bare ``stopReason: cancelled`` at the cap's
    count. An ``error`` field, or any other stop (refusal, content_filter,
    error), is a failure whatever the count says; reading it as the cap
    replanned a safeguard or transport error and dropped its message (Grok
    review of #33, 2026-09-26).
    """
    return not payload.get("error") and payload.get("stopReason") == "cancelled"


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
        raise _no_envelope(
            f"grok did not return a JSON envelope: {stdout.strip()[:300]}"
        ) from exc
    if not isinstance(payload, dict):
        raise _no_envelope(f"grok returned unexpected JSON: {stdout.strip()[:300]}")
    stop = payload.get("stopReason")
    if stop in _GROK_REFUSAL_STOPS:
        # Before the error and cap handling: an explicit decline is a
        # refusal at any turn count, never an ordinary failed lead that
        # another model may take over (Codex review of f7548a2).
        details = payload.get("stopDetails") or payload.get("stop_details") or {}
        details = details if isinstance(details, dict) else {}
        category = details.get("category") or None      # only what grok supplied
        explanation = details.get("explanation") or (
            str(payload["error"])[:300] if payload.get("error") else None)
        raise ProviderRefusal(
            f"grok declined the request (stopReason {stop!r})"
            + (f" [{category}]" if category else "") + ".",
            category=category, explanation=explanation)
    if payload.get("error"):
        # The ``error`` field is the CLI's own; ``text`` below is the model's.
        # Its shape as an object is not documented, so every string in one is
        # read as a line of the report and a ``status`` number is used only
        # if it is there.
        own = payload["error"]
        if isinstance(own, dict):
            report = "\n".join(value for value in own.values() if isinstance(value, str))
            status = own.get("status")
        else:
            report, status = str(own), None
        raise _mark_transport(ProviderError(f"grok reported an error: {str(own)[:300]}"), report, status)
    text = payload.get("text")
    # A turn that did not reach end_turn still carries text, and that text is
    # the narration it had got to -- a denied tool comes back as
    # {"stopReason": "cancelled", "text": "I'll create proof.txt now."}. That
    # reads as a confident answer to everything downstream. Anything but a
    # completed turn is therefore an error, including a stop reason this code
    # has never seen: guessing that an unknown one is benign is how the
    # narration got mistaken for an answer in the first place.
    if stop in _GROK_CAP_STOPS:
        # Capped, not failed: the call ran and may have written. The text is
        # where the loop had got to, kept as evidence and never as an answer.
        raise TurnLimitReached(f"grok stopped at its turn limit before finishing (stopReason {stop!r})",
                               partial_text=text, turns=_envelope_turns(payload))
    if stop != "end_turn":
        diagnostics = _extract_grok_diagnostics(stdout) or {}
        attempted = diagnostics.get("attempted_tools")
        # The narration is quoted for whoever reads the failure. It is the
        # model's text, so this error carries no typed transport fact and
        # nothing may classify it by its message (see ``LIMIT_WORDING``).
        raise ProviderError(
            f"grok did not complete the turn (stopReason {stop!r})"
            + (f"; attempted tools: {', '.join(attempted)}" if attempted else "")
            + (f": {text.strip()[:200]}" if isinstance(text, str) and text.strip() else ".")
        )
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("grok completed the turn with no answer text.")
    return text


#: Grok's own line for a tool call a headless run could not get approved
#: (cycle14 t5's saved stderr: "permission required: Fetch: file:///.../README.md
#: was not run, as a headless run cannot ask for approval"). Matched whole,
#: one line at a time; nothing else in stderr or the envelope is read as one.
_GROK_HEADLESS_PERMISSION = re.compile(
    r"permission required: (?P<tool>[A-Za-z][\w.-]{0,63}): (?P<target>\S(?:.{0,4094}\S)?) "
    r"was not run, as a headless run cannot ask for approval")
_TARGET_SCHEME = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{0,15})://")


def _extract_grok_permission_block(stderr: str) -> Optional[Dict[str, object]]:
    """The bounded permission block from grok's exact headless-approval line,
    or None (cycle15). ``tool`` as grok named it, ``scheme`` of the target
    (``file``, ``https``, ... or ``path`` for a bare path), and a digest of
    the target: the raw target (a machine path) stays only in the private
    stderr tail. More than one such line keeps the first and counts them."""
    found = []
    for line in str(stderr or "").splitlines()[-200:]:
        match = _GROK_HEADLESS_PERMISSION.fullmatch(line.strip())
        if match is not None:
            found.append(match)
    if not found:
        return None
    first = found[0]
    target = first.group("target")
    scheme = _TARGET_SCHEME.match(target)
    return dict(tool=first.group("tool"), scheme=(scheme.group("scheme").lower() if scheme else "path"),
                headless_approval=True,
                target_digest=hashlib.sha256(target.encode("utf-8", "replace")).hexdigest()[:16],
                lines=len(found))


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
    error = payload.get("error")
    if error or payload.get("type") == "error":
        # How the failed call ended, as the client reports it: an integer
        # status on the envelope or its error object, and the class the
        # status and the vendor's wording establish. The grok envelope's
        # error shape is not documented; a field that is not there is
        # unknown, not zero.
        holder = error if isinstance(error, dict) else {}
        status = next((v for v in (holder.get("status"), holder.get("statusCode"), holder.get("code"),
                                   payload.get("status"), payload.get("statusCode"))
                       if type(v) is int), None)
        text = " ".join(str(v) for v in (holder.get("message") if holder else error, payload.get("message")) if v)
        diagnostics.update(failure_facts(status, text))
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


#: A sandbox launcher's own refusal to start a command: macOS
#: ``sandbox-exec``'s ``sandbox_apply`` failure (status 71) or one of
#: ``bwrap``'s namespace-setup errors, as the whole line. Any other line that
#: merely starts with a launcher's name (a test fixture's diagnostic, a
#: model's prose) is ordinary output (#35, root review of d51ae79).
SANDBOX_REFUSAL = re.compile(
    r"^[ \t]*(?:sandbox-exec: sandbox_apply: [^\n]*"
    r"|bwrap: (?:No permissions to create (?:a )?new namespace|Creating new namespace failed"
    r"|setting up uid map: Permission denied|Can't mount proc on /newroot/proc"
    r"|loopback: Failed RTM_NEWADDR)[^\n]*)", re.MULTILINE)

#: Journal tools that execute a local command. Only these can show that a
#: local command ran after a refusal; a search or documentation result that
#: quotes an exit status shows nothing (root review of feacb60).
_JOURNAL_SHELL_TOOLS = frozenset({"shell", "local_shell", "exec_command", "shell_command", "container.exec"})
#: The status line a shell tool writes first, when its output is text.
_JOURNAL_EXIT_LINE = re.compile(r"(?:Exit code: |Process exited with code )(-?\d+)")


def _journal_exit_status(output) -> Optional[int]:
    """A shell tool output's own exit status: ``metadata.exit_code`` (or a
    top-level ``exit_code``) of the output's JSON envelope, else an exit
    line as the first line of its first text part. None when it states
    none; text further in is the command's output, never its status."""
    texts = _tool_output_texts(output)
    if not texts:
        return None
    try:
        envelope = json.loads(texts[0])
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        meta = envelope.get("metadata")
        code = meta.get("exit_code") if isinstance(meta, dict) else envelope.get("exit_code")
        return code if type(code) is int else None
    first = texts[0].lstrip().split("\n", 1)[0].strip()
    match = _JOURNAL_EXIT_LINE.fullmatch(first)
    return int(match.group(1)) if match else None

#: How much of a Codex session journal is read for tool outputs.
_JOURNAL_BYTES = 8 * 1024 * 1024


def _tool_output_texts(output) -> List[str]:
    """The text items of a journal tool output: a string, or a list of
    ``{"type": "input_text"|"output_text", "text": ...}`` parts."""
    if isinstance(output, str):
        return [output]
    if isinstance(output, list):
        return [part["text"] for part in output
                if isinstance(part, dict) and isinstance(part.get("text"), str)]
    return []


def _codex_stdout_recovered(stdout: str) -> Optional[bool]:
    """Whether a command completed with status 0 after the last sandbox
    refusal in ``codex exec --json`` output; None when there was no refusal."""
    refused, recovered = False, False
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        output = item.get("aggregated_output")
        if isinstance(output, str) and SANDBOX_REFUSAL.search(output):
            refused, recovered = True, False
        elif refused and item.get("exit_code") == 0 and item.get("status") == "completed":
            recovered = True
    return recovered if refused else None


def _codex_journal(root: Path, session_id: str, cwd, started: Optional[datetime]) -> Optional[Path]:
    """This call's own journal, or None: exactly one file named for the
    session, whose first record is its ``session_meta`` with the same id and
    working directory, begun inside this call's window. A filename alone
    authorizes nothing (root review of d51ae79)."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", session_id) or not cwd or started is None:
        return None
    try:
        files = list(Path(root).glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
        if len(files) != 1:
            return None
        first = first_record(files[0])  # byte-bounded: the line is the vendor's, not ours
        meta = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
        if not isinstance(meta, dict) or meta.get("id") != session_id:
            return None
        if Path(str(meta.get("cwd") or "")).resolve() != Path(cwd).resolve() or not meta.get("cwd"):
            return None
        begun = datetime.fromisoformat(str(meta.get("timestamp") or "").replace("Z", "+00:00"))
        if begun.tzinfo is None:
            return None
        now = datetime.now(timezone.utc)
        if not started - timedelta(seconds=5) <= begun <= now + timedelta(seconds=5):
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return files[0]


def _codex_journal_tool_failures(root: Path, session_id: str, cwd=None,
                                 started: Optional[datetime] = None) -> Tuple[List[Dict[str, object]], Optional[bool]]:
    """Sandbox refusals in this call's own Codex session journal, as tool
    failures, and whether a local shell command of the same call reported
    its own exit status 0 after the last of them (None when there was none). Never raises; ([], None) when the
    journal is absent or not provably this call's.

    #35 (2026-10-13): on a Mac the native shell's failure is not a
    ``command_execution`` in stdout. It is a ``custom_tool_call_output``
    whose first part reads "Script completed" and whose second part reads
    "sandbox-exec: sandbox_apply: Operation not permitted". Only tool-output
    items are read; reasoning and messages are never inspected.
    """
    path = _codex_journal(root, session_id, cwd, started)
    if path is None:
        return [], None
    names, found, recovered = {}, [], False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            lines = handle.read(_JOURNAL_BYTES).splitlines()
    except OSError:
        return [], None
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind in ("custom_tool_call", "function_call"):
            names[payload.get("call_id")] = str(payload.get("name") or "tool")
        elif kind in ("custom_tool_call_output", "function_call_output"):
            texts = _tool_output_texts(payload.get("output"))
            refusal = next((m for m in map(SANDBOX_REFUSAL.search, texts) if m), None)
            if refusal:
                recovered = False
                if len(found) < TOOL_FAILURE_LIMIT:
                    found.append({"kind": "native-tool",
                                  "command": names.get(payload.get("call_id"), "tool")[:TOOL_FAILURE_TEXT],
                                  "status": "failed", "output_tail": refusal.group(0).strip()[-TOOL_FAILURE_TEXT:]})
            elif (found and names.get(payload.get("call_id")) in _JOURNAL_SHELL_TOOLS
                  and _journal_exit_status(payload.get("output")) == 0):
                recovered = True
    return found, (recovered if found else None)


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
        # Byte-bounded lines: the journal also holds tool output of any length.
        for line in bounded_lines(files[-1]):
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


#: The codex event-stream item types that are a tool step: one command, file
#: change, MCP tool call or web search the model asked for and the client ran.
CODEX_TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call", "web_search")


def _codex_tool_steps(stdout: str) -> int:
    """How many tool steps one codex call ran: completed tool items in its
    event stream. codex reports no turn count (its ``turn.completed`` is the
    whole exec), so this is the measured count of what each round did, by
    type only; nothing of a command or its output is read (Davis,
    2026-11-05: grok-worker-01's 16 codex calls had no round figure)."""
    steps = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") in CODEX_TOOL_ITEMS:
            steps += 1
    return steps


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
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.failed":
            # A failed turn as the client reports it: the terminal reason
            # and the HTTP status its message names ("exceeded retry limit,
            # last status: 429 Too Many Requests" on codex 0.154, which
            # carries nothing else). The message itself is not kept: it is
            # the vendor's free text, and the ledger holds stderr already.
            # Recorded, not acted on: the budget still settles the attempt
            # as unknown usage, and nothing here says the turn never ran.
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            diagnostics = {"stop_reason": "turn.failed"}
            diagnostics.update(failure_facts(error.get("status"), error.get("message")))
            continue
        if event.get("type") != "turn.completed":
            continue
        diagnostics = {"stop_reason": "turn.completed"}
    steps = _codex_tool_steps(stdout)
    account = _codex_usage_account(stdout)
    if steps or account["completed_turns"]:
        # A completed turn with no tool item ran zero steps, measured; a
        # stream with neither says nothing, so nothing is recorded.
        diagnostics["tool_steps"] = steps
    if account["completed_turns"] or account["pending"] or account["invalid"]:
        diagnostics["usage_completeness"] = "complete" if account["complete"] else "unresolved"
        diagnostics["completed_turns"] = account["completed_turns"]
        if account["cached_input_tokens"]:
            # A measured subset of completed turns' input, as the reread
            # fact was always written: only when there is one (integration
            # review: zero completed turns have no cache figure to report).
            diagnostics["cached_input_tokens"] = account["cached_input_tokens"]
        if not account["complete"] and account["completed_turns"]:
            diagnostics["usage_lower_bound"] = dict(account["reported"],
                usage_source="last cumulative thread snapshot; incomplete call")
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


def _codex_usage_account(stdout: str) -> dict:
    """The call's token count from the thread's cumulative total, never a
    sum of snapshots and never a partial stream as a total.

    Upstream rust-v0.163.0 ``exec/src/event_processor_with_jsonl_output.rs``
    (lines 118-128, 509-535): ``ThreadTokenUsageUpdated`` stores the
    thread's cumulative total and every ``turn.completed`` emits
    ``usage_from_last_total``, so successive completions repeat a running
    total and summing them double-counts (root's correction, 2026-10-21,
    of root's own earlier sum). The account is therefore the last valid
    snapshot on a single thread: snapshots must be monotonic (a regression
    is malformed), a repeated total charges nothing extra, cached input is
    a subset never added, and a second thread id in one stream is not the
    same cumulative account. A failed, open, double-started or malformed
    turn, a regression or a second thread leaves the call unknown, with the
    last valid snapshot kept as the observed lower bound. Nothing here
    recovers a historical call's usage or verifies the backend's bill.
    """
    last: Optional[Dict[str, int]] = None
    last_cache = 0
    completed, invalid = 0, False
    open_turn, unaccounted = False, False
    threads: set = set()
    for line in stdout.splitlines():
        if not line.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            invalid = True
            continue
        if not isinstance(event, dict):
            invalid = True
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread = event.get("thread_id")
            threads.add(thread if isinstance(thread, str) else repr(thread))
        if kind == "turn.started":
            unaccounted = unaccounted or open_turn
            open_turn = True
        elif kind in ("turn.failed", "error"):
            unaccounted = True
            open_turn = False
        if kind != "turn.completed":
            continue
        open_turn = False
        usage = event.get("usage")
        if not isinstance(usage, dict):
            invalid = True
            continue
        values = {key: _strict_count(usage, key, missing_ok=False) for key in ("input_tokens", "output_tokens")}
        cache = _strict_count(usage, "cached_input_tokens", missing_ok=True)
        if any(value is None for value in values.values()) or cache is None:
            invalid = True
            continue
        if cache > values["input_tokens"]:
            invalid = True
            continue
        if last is not None and (values["input_tokens"] < last["input_tokens"]
                                 or values["output_tokens"] < last["output_tokens"] or cache < last_cache):
            # A cumulative total cannot fall: the stream is not one account.
            invalid = True
            continue
        last, last_cache = dict(values), cache
        completed += 1
    pending = open_turn or unaccounted
    several_threads = len(threads) > 1
    reported = dict(last) if last is not None else {"input_tokens": 0, "output_tokens": 0}
    return {"complete": bool(completed) and not pending and not invalid and not several_threads,
            "completed_turns": completed, "reported": reported, "cached_input_tokens": last_cache,
            "invalid": invalid, "pending": pending, "threads": len(threads)}


def _extract_codex_usage(stdout: str) -> Optional[Dict[str, int]]:
    account = _codex_usage_account(stdout)
    return account["reported"] if account["complete"] else None


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


def _ending_only(diagnostics: Dict[str, object]) -> Optional[Dict[str, object]]:
    """An envelope with no auxiliary rows reports no auxiliary provenance, as
    before; how a failed call ended is kept regardless, so an error
    envelope's status and class reach the ledger. The call's own measured
    facts are kept too: its turn count, its cached-input subset and the
    vendor's stated cost. Dropping them here was the single-seat metadata
    loss carried since the recovery batch; grok-worker-01 recorded no turn
    count and no cache figure for any of its 24 claude calls (Davis,
    2026-11-05)."""
    kept = {k: diagnostics[k] for k in ("stop_reason", "status", "failure_class", "model_calls",
                                        "cached_input_tokens", "vendor_cost_usd") if k in diagnostics}
    return kept or None


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
        if payload.get("is_error"):
            # How the failed call ended, as the client reports it: the
            # envelope's stop reason, the HTTP status it names
            # (``api_error_status`` on a 429, seen) and the class those
            # establish. Facts only, never the result text.
            stop = payload.get("stop_reason") or payload.get("subtype")
            if isinstance(stop, str) and re.fullmatch(r"[A-Za-z_.-]{1,40}", stop):
                diagnostics["stop_reason"] = stop
            errors = payload.get("errors")
            text = str(payload.get("result") or "") + " " + (
                " ".join(str(e) for e in errors) if isinstance(errors, list) else "")
            diagnostics.update(failure_facts(payload.get("api_error_status"), text))
    if malformed:
        diagnostics["auxiliary_usage"] = "unknown"
        if seat is not None:
            diagnostics["seat_tokens"] = seat["input_tokens"] + seat["output_tokens"]
        return diagnostics
    if not rows:
        return _ending_only(diagnostics)
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
            return _ending_only(diagnostics)
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
    #: Allocate trace identity before launch, including calls with no stdout.
    session_id_flag: Optional[str] = None
    #: Args that make the agent read-only (no file writes, no shell).
    readonly_args: List[str] = field(default_factory=list)
    #: Args for an ungranted call that runs inside a disposable source copy
    #: (``runtime.Fleet._invoke``'s snapshot): the copy is the containment
    #: and is deleted when the call returns, so a vendor whose read-only mode
    #: also denies its own temporary directory may use a mode that writes
    #: inside the copy. Codex: under ``--sandbox read-only`` Python found no
    #: writable temp dir (the macOS per-user dir, /tmp, /var/tmp and the copy
    #: itself all refused), so a reviewer told to run the project's pytest
    #: could not, and blocked the task on "cannot run the required check"
    #: (series b1ff751 f3 and rule-b1ff751 f4). Empty means ``readonly_args``.
    copy_args: List[str] = field(default_factory=list)
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
    #: Operator arguments that would widen or move the policy of an ungranted
    #: call in a disposable copy (``copy_args``); such a call refuses them
    #: before dispatch. None where the vendor has no copy policy.
    copy_boundary_conflicts: Optional[Callable[[Sequence[str]], List[str]]] = None
    #: Optional run-wide denial request for other vendors' native helpers.
    #: This is not proof their CLI honors it; see the Grok specification.
    native_fanout_off_args: List[str] = field(default_factory=list)
    #: Startup controls separate from the variadic tool-denial flag.
    native_fanout_off_flags: List[str] = field(default_factory=list)
    #: Environment set on the subprocess only when the off-mode denial was
    #: folded into this call's argv. Documented kill switches that remove a
    #: fan-out path at startup, so a denial the model never sees cannot be
    #: argued with either.
    native_fanout_off_env: Dict[str, str] = field(default_factory=dict)
    disallowed_tools_flag: str = ""
    #: The flag that names what an editing call may run without approval, and
    #: the rule each granted command becomes. Claude: ``--allowedTools
    #: "Bash(python -m pytest -q)"`` grants exactly that command in headless
    #: ``-p`` and leaves every other Bash denied ("This command requires
    #: approval"), verified on claude 2.1.288 (2026-10-03). Empty when the
    #: vendor's sandbox runs commands instead (codex, grok).
    allowed_tools_flag: str = ""
    allow_rule_template: str = "Bash({command})"
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

    def summary_turn_capped(self) -> bool:
        """Whether a summary-only call is held to one model turn by argv.

        True where ``summary_only_args`` carries ``<max_turns_flag> 1``
        (claude, grok). False for codex, whose summary call has no turn flag:
        the empty directory and the prompt's wording ask for one turn but do
        not enforce it (Codex review of 961d2da on #53). The budget's
        summary-shaped reserve is gated on this, never on the request.
        """
        args = list(self.summary_only_args)
        flag = self.max_turns_flag
        return bool(flag) and any(args[i] == flag and args[i + 1] == "1"
                                  for i in range(len(args) - 1))

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
    #: What an editing call may execute without anyone approving it: "any"
    #: (the vendor's sandbox runs commands) or "granted" (only commands named
    #: in an allow rule; headless claude denies the rest, Codex, Run 18). The
    #: session reads it before dispatch, so a UI task whose lead could never
    #: run the capture stops before it is invoked instead of after its cap.
    editing_commands: str = "any"
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
    #: The CLI's own headless-approval refusal, read from stderr into bounded
    #: metadata (cycle15). A call whose turn failed with one present raises
    #: ``PermissionBlocked``. None where no such line is known.
    extract_permission_block: Optional[Callable[[str], Optional[Dict[str, object]]]] = None
    #: The local file readers a restricted seat already has, named in its
    #: source-copy prompt (cycle15). Every name must be in
    #: ``restricted_args``; the prompt never offers a tool the call lacks.
    local_read_tools: tuple = ()
    #: Failed tool calls the agent made during this call (command, exit code,
    #: output tail), bounded, for the ledger. None where the CLI's output does
    #: not report its tool calls in a parseable form.
    extract_tool_failures: Optional[Callable[[str], List[Dict[str, object]]]] = None
    #: Whether these flags have been checked against a real binary.
    verified: bool = False
    #: Arguments that print the client's version (read once per binary).
    version_args: List[str] = field(default_factory=lambda: ["--version"])
    #: The oldest client this engine qualified, or None. A run
    #: whose installed client parses below it is refused before any call
    #: (``runtime.Fleet.stale_clients``, ``project_run``); an unreadable
    #: version is reported as unknown and not refused. No override: the
    #: remedy is updating the client, and the refusal names both versions.
    minimum_client: Optional[str] = None
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


def _extract_claude_denials(stdout: str) -> List[Dict[str, object]]:
    """Each Bash command the claude envelope's ``permission_denials`` names,
    as a ``permission_denied`` tool failure, so it is kept in the ledger and
    the Fleet can tell a denied required command from exploration."""
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    out: List[Dict[str, object]] = []
    denials = payload.get("permission_denials")
    for entry in denials if isinstance(denials, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("tool_name") or entry.get("toolName") or entry.get("name")
        tool_input = entry.get("tool_input") if isinstance(entry.get("tool_input"), dict) else {}
        command = tool_input.get("command")
        if name == "Bash" and isinstance(command, str) and command.strip():
            out.append(dict(kind="permission_denied", command=command.strip(), status="denied"))
        elif name in CLAUDE_WRITE_TOOLS:
            # A denied project edit (Stage B series b332951, 2026-10-03: every
            # Edit/Write of ten Opus lead calls was denied in permissionMode
            # default and the ledger showed only budget stops). The path is
            # the file the call tried to write; nothing from its content.
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            out.append(dict(kind="permission_denied", tool=str(name), status="denied",
                            path=str(path)[:300] if isinstance(path, str) else ""))
    return out


#: The claude tools that change files; a denial of any of these on a call
#: that was granted writes is a capability failure, not a model choice.
CLAUDE_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


#: Verified against claude 2.1.228. ``--system-prompt`` fully replaces the
#: default Claude Code system prompt (it does not merely append), which keeps
#: an assigned collaboration role from competing with the coding-agent persona.
CLAUDE_SPEC = CLISpec(
    vendor="claude",
    # Root's qualification target (2026-10-21): the official npm stable
    # 2.1.304, from the installed 2.1.296, in the same batch that moved
    # codex to 0.163.0. Set with the client preflight so a run names both
    # versions and refuses before any call; an unreadable version is
    # unknown and not refused. No upstream 429 defect is established for
    # this client; the baseline records what was qualified, not a fault.
    minimum_client="2.1.304",
    editing_commands="granted",
    extract_tool_failures=_extract_claude_denials,
    binary="claude",
    print_flag=["-p"],
    system_flag="--system-prompt",
    model_flag="--model",
    output_args=["--output-format", "json"],
    session_id_flag="--session-id",
    readonly_args=["--disallowed-tools", "Bash Edit Write NotebookEdit"],
    # The write grant (2026-10-03). Without it a lead with writes launched in
    # permissionMode default, and claude -p denies every project Edit/Write
    # there: Stage B series b332951 spent ten Opus lead calls on denials the
    # ledger recorded only as token-threshold stops. ``acceptEdits`` approves
    # the file tools; Bash stays denied except for the exact commands the
    # harness grants through ``allowed_tools_flag`` (see ``_build_argv``).
    # Verified live on claude 2.1.288: default denies Write, acceptEdits
    # writes, ``Bash(python3 -V)`` grants that command and denies ``python3 -c``.
    write_args=["--permission-mode", "acceptEdits"],
    allowed_tools_flag="--allowedTools",
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


#: Operator flags that move or widen the sandbox boundary of a codex call:
#: refused on an ungranted call in a disposable copy (Codex review of 9b8c056
#: on #52), where the copy must stay the only writable root. ``--cd`` rebases
#: the working directory the policy is anchored to; the rest change the mode
#: or add roots. Each is listed in every spelling clap accepts for it.
CODEX_COPY_BOUNDARY_FLAGS = (
    "--sandbox", "-s", "--add-dir", "--full-auto", "--yolo",
    "--dangerously-bypass-approvals-and-sandbox", "--cd", "-C",
)
#: ``-c`` keys (and their tables) that do the same through configuration.
CODEX_COPY_BOUNDARY_KEYS = ("sandbox_mode", "sandbox_workspace_write", "sandbox", "cwd")


def codex_copy_boundary_conflicts(args: Sequence[str]) -> List[str]:
    """Operator arguments that would widen or move a disposable copy's policy.

    ``copy_args`` makes the copy the only writable root; the operator's
    ``QUADRATUS_CLI_ARGS_OPENAI`` lands after it and would win. So on an
    ungranted call in a disposable copy any flag or ``-c`` key that touches
    the sandbox mode, its writable roots, extra directories or the working
    directory is a refusal before dispatch, whatever its value: an agreeing
    override is as much of a surprise as a widening one, and the control is
    not the operator's to restate. A bare ``--`` hides what follows and is
    refused for the same reason as in :func:`codex_override_conflicts`.
    """
    tokens = list(args)
    conflicts: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            conflicts.append(token)
            index += 1
            continue
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in CODEX_COPY_BOUNDARY_FLAGS:
                conflicts.append(name)
                index += 1 if "=" in token or name in ("--full-auto", "--yolo",
                                                       "--dangerously-bypass-approvals-and-sandbox") else 2
                continue
        elif len(token) >= 2 and token[0] == "-" and token[1] in "sC":
            # A short option in every clap form: ``-s read-only``, ``-s=x``
            # and the attached ``-sdanger-full-access`` / ``-C/tmp/outside``
            # (Codex review of d866a83: the attached form slipped past a
            # check that split on ``=`` only). Only the flag is reported.
            conflicts.append(token[:2])
            index += 2 if len(token) == 2 else 1
            continue
        else:
            name = token
        payload, span = _config_override_value(tokens, index)
        if span:
            key = _override_key(payload) if payload is not None else ""
            if key in CODEX_COPY_BOUNDARY_KEYS or any(key.startswith(f"{k}.") for k in CODEX_COPY_BOUNDARY_KEYS):
                conflicts.append(f"{name[:8]} {key}=…")
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
    # A disposable source copy is its own containment: workspace-write there
    # lets a reviewer run the check it is told to run (see ``copy_args``).
    # Its writable roots are the copy alone (Codex review of c3abcf0 on #52):
    # workspace-write adds /tmp and $TMPDIR by default and keeps any
    # writable_roots from the operator's config, so each is switched off
    # here. The scratch TMPDIR the provider sets lives under the copy, which
    # is the working directory, so it stays writable without its own root.
    copy_args=["--sandbox", "workspace-write",
               "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
               "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
               "-c", "sandbox_workspace_write.writable_roots=[]"],
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
    # Qualified minimum (2026-10-21). Upstream rust-v0.154.0
    # codex-api/src/api_bridge.rs maps every 429 but the usage-limit codes
    # to a RetryLimit holding only the status and request id, discarding
    # the turn_rate_limit_exceeded body and its Retry-After, so a capacity
    # rejection ends the turn with a bare status after the client's own
    # retries. rust-v0.162.0 carries Retry-After through the websocket
    # TransportError (protocol/src/error.rs retry_delay) and waits inside
    # the same sampling session under its retry budget and the call
    # deadline (core/src/responses_retry.rs; suite retry_after.rs). 0.163.0
    # is the first version this engine qualified, live on 2026-10-21; it is
    # not a claim about which upstream release first carried the wait.
    # The engine runs no outer retry: the client is where the wait belongs,
    # and a run on a client that cannot wait is refused before any call.
    minimum_client="0.163.0",
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
    copy_boundary_conflicts=codex_copy_boundary_conflicts,
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
#: Ceiling, in encoded bytes, on a prompt delivered in argv. Linux caps one
#: argument at 131,072 bytes whatever ARG_MAX says (macOS shares 1 MiB
#: between argv and the environment), and the failure at the cap is an opaque
#: OSError from exec, so a clear refusal a little short of it is worth the
#: lost headroom. It was 200,000 *characters*: a 150,000-character restricted
#: prompt failed with "Argument list too long" in the Linux acceptance
#: container, and multi-byte text failed sooner. Set to the supplied-context
#: prompt bound (``runtime.Fleet._supplied_context``), the largest prompt a
#: restricted call is handed on purpose.
MAX_ARGV_PROMPT = 128_000

#: How much of a CLI's stderr survives into the ledger per attempt. Enough to
#: hold a sandbox refusal or an auth failure verbatim; not a transcript.
STDERR_TAIL = 2_000

#: Most bytes kept from one output stream of one CLI call. ``codex exec
#: --json`` repeats every command's output, so a model that prints a large
#: file, or loops, could otherwise grow the harness's memory without bound.
#: Far above any answer; ``QUADRATUS_CLI_OUTPUT_LIMIT_MB`` raises it.
MAX_CLI_OUTPUT_BYTES = 32 * 1024 * 1024
#: How much of each end of an over-long stream the error keeps.
OUTPUT_EDGE = 2_000
#: Seconds the output readers get once the CLI is gone: first for the pipes
#: to end with it (the usual case, at once), then for whatever a stopped
#: leftover process still held.
_EOF_GRACE, _DRAIN_GRACE = 0.25, 2.0
#: How often a reader with nothing to read looks up to see whether the CLI
#: has gone. Only a call whose pipe is held open ever waits this long.
_POLL_SECONDS = 0.05

GROK_SPEC = CLISpec(
    vendor="grok",
    binary="grok",
    # Root's qualification target (2026-10-21): the official Homebrew/x.ai
    # stable 1.0.62, from the installed 1.0.30 that every grok flag note
    # below was verified on. Same preflight as the other two vendors.
    minimum_client="1.0.62",
    system_flag="--system-prompt-override",
    model_flag="--model",
    # Without this the CLI streams the agent's narration and the "answer" is
    # whatever it was mid-sentence about; with it, one JSON envelope carrying
    # the final text and real token counts.
    output_args=["--output-format", "json"],
    # The native session's identity, allocated before launch (root on
    # 113f4e1, 6549956979 item 2): the installed 1.0.62 accepts a fresh UUID
    # here, and its session folder under ~/.grok/sessions carries it, so a
    # timeout that returns no envelope no longer hides the transcript or its
    # native events. The final-only JSON envelope is unchanged: usage and
    # stop semantics stay what they were.
    session_id_flag="--session-id",
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
    native_fanout_off_flags=["--no-subagents"],
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
    extract_permission_block=_extract_grok_permission_block,
    local_read_tools=("read_file", "grep", "list_dir"),
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

    A launcher that exits on its own is not the end of its group either. A
    dev server a model started in the background kept running against the
    project, or against a copy already deleted; and a child that still held
    the stdout pipe made ``communicate`` wait for an end of file that never
    came, so a call that had finished, with its whole answer read, was
    reported as a timeout and the answer thrown away. So after a normal exit
    whatever is left in the group is stopped (``_stop_leftovers``), and the
    output is read until the pipe is empty, never until end of file.

    Empty, not "for a while": a reader that is still behind when the wait
    ends has not read the whole answer, and a partial stream can parse (the
    last complete line of a JSONL stream may be a model's progress note).
    That call is reported as cut off, the way a pipe that never ended always
    was, and never returned as a finished one (``_Capture.unfinished``).

    Each stream is read by its own thread up to a byte cap
    (``MAX_CLI_OUTPUT_BYTES``). Past it the group is stopped and
    :class:`CLIOutputTooLarge` is raised with both ends of what was read.
    """
    grouped = hasattr(os, "killpg") and hasattr(os, "setsid")
    proc = subprocess.Popen(  # noqa: S603 -- argv is built, never a shell string
        argv,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        **({"start_new_session": True} if grouped else {}),
    )
    limit = _output_limit()
    # Set when the launcher exits or a stream passes its cap, whichever is
    # first, so the wait below needs no polling.
    wake = threading.Event()
    out, err = _Capture(proc.stdout, limit, wake), _Capture(proc.stderr, limit, wake)
    if input is not None:
        threading.Thread(target=_feed, args=(proc.stdin, input.encode("utf-8", errors="replace")),
                         name="quadratus-cli-stdin", daemon=True).start()
    threading.Thread(target=lambda: (proc.wait(), wake.set()), name="quadratus-cli-exit", daemon=True).start()
    try:
        woke = wake.wait(timeout)
    except BaseException:
        # KeyboardInterrupt included: an operator stopping the run must not
        # leave a vendor CLI and its children running against the project.
        _terminate_group(proc)
        _finish_captures((out, err), _DRAIN_GRACE)
        raise
    if not woke:
        _terminate_group(proc)
        _finish_captures((out, err), _DRAIN_GRACE)
        raise subprocess.TimeoutExpired(argv, timeout, output=out.text(), stderr=err.text())
    if proc.returncode is None:
        # Woken by a stream past its cap while the launcher still runs.
        _terminate_group(proc)
    # The launcher is gone. In the usual case both pipes end with it; a pipe
    # something else holds open is read until it is empty.
    _finish_captures((out, err), _EOF_GRACE)
    if grouped:
        _stop_leftovers(proc.pid)
    # A detached descendant may hold a pipe even now; it is not waited for.
    _finish_captures((out, err), _DRAIN_GRACE)
    proc.poll()
    for name, capture in (("stdout", out), ("stderr", err)):
        if capture.overflow.is_set():
            raise CLIOutputTooLarge(name, limit, capture.text(), output=out.text(), stderr=err.text())
    behind = [name for name, capture in (("stdout", out), ("stderr", err)) if capture.unfinished()]
    if behind:
        # Not a finished call: what was read is not known to be all of it.
        cut = subprocess.TimeoutExpired(argv, _EOF_GRACE + _DRAIN_GRACE, output=out.text(), stderr=err.text())
        cut.reason = (f"exited {proc.returncode}, but its {' and '.join(behind)} could not be read to the "
                      "end: output was still arriving, or the read failed, after it was gone")
        raise cut
    return subprocess.CompletedProcess(argv, proc.returncode, out.text(), err.text())


class CLIOutputTooLarge(RuntimeError):
    """A vendor CLI wrote more to one stream than a call may keep, and was stopped.

    Not a :class:`ProviderError`: the call was cut off from outside
    (``interrupted``), so it is never retried, and on a call that could write
    the provider reports suspected partial work as it does for a timeout.
    ``head`` and ``tail`` are the two ends of what was read before the stop.
    """

    #: Read by ``LLMProvider._generate_once``: stopped, not failed.
    interrupted = True

    def __init__(self, stream: str, limit: int, captured: str, *, output: str = "", stderr: str = ""):
        self.stream, self.limit = stream, limit
        self.head, self.tail = captured[:OUTPUT_EDGE], captured[-OUTPUT_EDGE:]
        #: What was read, for the caller's accounting. Dropped once observed.
        self.output, self.stderr = output, stderr
        super().__init__(
            f"the CLI wrote more than {limit:,} bytes to its {stream} and was stopped "
            f"(QUADRATUS_CLI_OUTPUT_LIMIT_MB raises the limit). It began {self.head[:200]!r} "
            f"and was last writing {self.tail[-200:]!r}")


def _output_limit() -> int:
    """The per-stream byte cap, after any operator override."""
    raw = os.environ.get("QUADRATUS_CLI_OUTPUT_LIMIT_MB", "").strip()
    if raw:
        try:
            megabytes = float(raw)
        except ValueError:
            megabytes = 0.0
        if math.isfinite(megabytes) and megabytes > 0:
            return max(1, int(megabytes * 1024 * 1024))
        log.warning("could not use QUADRATUS_CLI_OUTPUT_LIMIT_MB=%r; keeping the default", raw)
    return MAX_CLI_OUTPUT_BYTES


def _pipe_waiter(descriptor: int) -> Optional[Callable[[float], bool]]:
    """A function that waits up to so many seconds for the pipe to have
    something to read (data, or its end), or None where a pipe cannot be
    polled (no ``select.poll``: the reader then blocks in the read)."""
    poll = getattr(select, "poll", None)
    if poll is None:
        return None
    try:
        poller = poll()
        poller.register(descriptor, select.POLLIN)
    except (OSError, ValueError):
        return None
    return lambda seconds: bool(poller.poll(seconds * 1000))


class _Capture:
    """One output stream of a CLI, read by its own thread up to a byte cap.

    At the cap the thread stops reading and closes its end, so the writer
    stalls or fails instead of the harness growing; ``overflow`` tells the
    launcher to stop the group. The thread owns the stream: it closes it
    when it stops, and it is never closed from another thread.

    The thread stops at end of file, or, once the launcher is gone
    (``closing``), the first time it finds the pipe empty. Either way it has
    then read everything the launcher wrote (``drained``), so a process that
    only holds the pipe open does not keep the call waiting, and a thread
    still reading when the caller stops waiting is known to be behind
    (``unfinished``), not mistaken for one that has finished.
    """

    def __init__(self, stream, limit: int, wake: threading.Event):
        self._stream, self._limit, self._wake = stream, limit, wake
        self._chunks: List[bytes] = []
        self.overflow = threading.Event()
        #: Set by the launcher once the CLI is gone: stop at an empty pipe.
        self.closing = threading.Event()
        #: Set by the thread when it stopped with nothing left to read.
        self.drained = threading.Event()
        try:
            self._ready = _pipe_waiter(stream.fileno())
        except (OSError, ValueError):
            self._ready = None
        self.thread = threading.Thread(target=self._read, name="quadratus-cli-output", daemon=True)
        self.thread.start()

    def _read(self) -> None:
        size = 0
        try:
            descriptor = self._stream.fileno()
            while True:
                if self._ready is not None:
                    # Read before the wait, never after it: "empty" then
                    # means empty at a moment after the launcher's last write.
                    closing = self.closing.is_set()
                    if not self._ready(0.0 if closing else _POLL_SECONDS):
                        if closing:
                            self.drained.set()
                            return
                        continue
                block = os.read(descriptor, 65536)
                if not block:
                    self.drained.set()
                    return
                room = self._limit - size
                self._chunks.append(block[:room])
                size += len(block)
                if size > self._limit:
                    self.overflow.set()
                    self._wake.set()
                    return
        except (OSError, ValueError):
            return
        finally:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass

    def unfinished(self) -> bool:
        """Whether what was read may not be all the launcher wrote.

        Asked once the launcher is gone and the waiting is over. A thread
        that stopped on a failed read has not read to the end. One that is
        still running has not either, where the pipe can be polled: it would
        have stopped at the first empty pipe. Where it cannot, a running
        thread is blocked behind whoever holds the pipe open, which is the
        one case the bounded wait exists to let through.
        """
        if self.drained.is_set() or self.overflow.is_set():
            return False
        return self._ready is not None or not self.thread.is_alive()

    def text(self) -> str:
        """What has been read so far, decoded; newlines as text mode gave them."""
        data = b"".join(list(self._chunks)).decode("utf-8", errors="replace")
        return data.replace("\r\n", "\n").replace("\r", "\n")


def _finish_captures(captures, grace: float) -> None:
    """The CLI is gone: let the readers stop at an empty pipe, and wait up to
    ``grace`` seconds in all for them to get there."""
    for capture in captures:
        capture.closing.set()
    end = time.monotonic() + grace
    for capture in captures:
        capture.thread.join(max(0.0, end - time.monotonic()))


def _feed(stream, data: bytes) -> None:
    """Write the prompt to the CLI's stdin and close it. A CLI that exits
    without reading it all is the caller's failure to report, not this one's."""
    try:
        stream.write(data)
        stream.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _stop_leftovers(pgid: int) -> Optional[dict]:
    """Stop what a launcher that exited on its own left in its process group.

    The members only, one verified pid at a time, by the routine the
    integration gate uses for its own commands (``integration._stop_owned``).
    Never a whole-group signal here: the launcher has been reaped, and its
    pid, which names the group, could come to name something else. A process
    that moved to its own session is not in the group and is not touched.
    Returns the cleanup record, or None when nothing was left or the process
    list could not be read. Never raises.
    """
    try:
        from .integration import _group_members, _stop_owned
        members = _group_members(pgid)
        if not members:
            if members is None:
                log.debug("could not list processes; anything the CLI left running was not stopped")
            return None
        record = _stop_owned(pgid)
    except Exception:  # noqa: BLE001 -- cleanup never fails a finished call
        log.debug("could not stop what the CLI left running", exc_info=True)
        return None
    log.warning(
        "The CLI exited leaving %d process(es) running in its group: %d signalled%s.",
        # One process can be in both lists: asked to stop, then killed.
        len(record["found"]), len(set(record["terminated"]) | set(record["killed"])),
        "" if record.get("complete") else "; cleanup is NOT confirmed complete")
    return record


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


def _values_end(argv: Sequence[str], at: int) -> int:
    """The index just past the values of the variadic flag at ``at``: every
    following token up to the next option, which is how the CLI reads it."""
    end = at + 1
    while end < len(argv) and not argv[end].startswith("-"):
        end += 1
    return end


def _fold_allowed(argv: List[str], flag: str, extra: Sequence[str]) -> List[str]:
    """Add ``extra`` rules to an existing ``flag`` rather than repeating the flag.

    The allow flag is variadic: each rule is its own argument after it. So
    the new rules go in after the ones already there, in the same form, and
    the flag appears once. A flag not yet present is appended with them.
    """
    if flag not in argv:
        return list(argv) + [flag, *extra]
    at = argv.index(flag)
    end = _values_end(argv, at)
    present = argv[at + 1:end]
    return list(argv[:end]) + [rule for rule in extra if rule not in present] + list(argv[end:])


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
    denials = payload.get("permission_denials")
    for entry in denials if isinstance(denials, list) else []:
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
        #: Whether this view runs in a disposable source copy; set only by
        #: ``in_directory(..., disposable=True)`` on an ungranted call.
        self._disposable: bool = False
        #: The in-session worker tool for this view (``WorkerBridge.spec()``),
        #: set per lead call by runtime.Fleet. None attaches nothing.
        self.worker_tool: Optional[dict] = kwargs.pop("worker_tool", None)
        #: The commands a granted editing call may run unapproved (the
        #: project's checks), set per view by runtime.Fleet from the gate
        #: plan. Each becomes one allow rule, exact and with a ``:*`` prefix
        #: form so the same check with extra arguments still runs. Only read
        #: when writes are granted and the spec has an ``allowed_tools_flag``.
        self.granted_commands: tuple = tuple(kwargs.pop("granted_commands", ()) or ())
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
        # The installed client's version, for the status line, every
        # invocation row and the run preflight (codex 0.154 discarded a
        # 429's body and Retry-After inside its API bridge; 0.163.0 is the
        # version this engine qualified). Recorded here, never refused
        # here: a seat is constructed and reported as it is.
        self.client_version = client_version(path, self.spec.version_args)
        self.minimum_client = self.spec.minimum_client or None
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
        if not self._owned_workdir or not os.path.isdir(self._owned_workdir):
            # Under the private scratch root, not the shared temp folder that
            # another call's workspace-write sandbox may write. Made again
            # when it is gone: a run's start removes day-old leftovers there
            # (providers.sweep_scratch), and a provider held by a process
            # older than that would otherwise be started in a missing folder.
            self._owned_workdir = tempfile.mkdtemp(prefix=f"quadratus-{self.name}-", dir=scratch_root())
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

    def in_directory(self, workdir, *, allow_writes=False, disposable=False):
        """A per-call view running in ``workdir``. ``disposable`` marks a
        source copy the Fleet deletes when the call returns; an ungranted
        call there may use ``CLISpec.copy_args`` and gets a temp dir inside
        the copy, since nothing it writes can reach the project."""
        view = copy.copy(self)
        view._workdir = str(workdir)
        view._owned_workdir = None
        view._allow_writes = allow_writes
        view._disposable = bool(disposable and not allow_writes)
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
                if self._allow_writes:
                    argv += list(spec.write_args)
                elif getattr(self, "_disposable", False) and spec.copy_args:
                    argv += list(spec.copy_args)
                else:
                    argv += list(spec.readonly_args)
                if self._allow_writes and spec.allowed_tools_flag:
                    rules = []
                    for command in getattr(self, "granted_commands", ()) or ():
                        command = str(command).strip()
                        if command:
                            rules.append(spec.allow_rule_template.format(command=command))
                            rules.append(spec.allow_rule_template.format(command=command + ":*"))
                    if rules:
                        # Variadic like --disallowed-tools; the prompt is on
                        # stdin for claude, so nothing after it is swallowed.
                        argv += [spec.allowed_tools_flag, *rules]
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
            # Supplied research analysis preserves its existing call timeout
            # and model effort. Ordinary closeout keeps the 60 s bound.
            timeout_limit = 180 if getattr(self, 'supplied_analysis', False) else 60
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0 or timeout > timeout_limit):
                # nan compares false against everything, so "not > 60" is not
                # "<= 60"; the bound is stated positively.
                raise ProviderError(f"{self.label}: summary_only needs a finite timeout in "
                                    f"(0, {timeout_limit}], not {timeout!r}")
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
            argv += list(spec.native_fanout_off_flags)
        else:
            self._native_fanout_denied = []
        if (getattr(self, "_disposable", False) and not self._allow_writes
                and spec.copy_args and spec.copy_boundary_conflicts is not None):
            # The copy is the only writable root, and the operator's
            # arguments land last; an override of the boundary is refused
            # before the call rather than out-ordered by it.
            conflicts = spec.copy_boundary_conflicts(extra)
            if conflicts:
                raise NativeControlOverride(
                    f"{self.label}: QUADRATUS_CLI_ARGS_{spec.vendor.upper()} would widen or move the "
                    f"sandbox of an ungranted call in a disposable source copy ({'; '.join(conflicts)}). "
                    "The copy is the only writable root there; remove the override, not the control.")
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
        allow = spec.allowed_tools_flag
        if tool_args and allow and allow in tool_args and allow in argv:
            # The granted checks already carry this flag, and a repeated tool
            # flag is not known to merge (see ``_fold_disallowed``): if the
            # second replaced the first, the lead would lose its checks and
            # stop as CapabilityUnavailable. One flag, both sets of rules.
            at = tool_args.index(allow)
            end = _values_end(tool_args, at)
            argv = _fold_allowed(argv, allow, tool_args[at + 1:end])
            tool_args = tool_args[:at] + tool_args[end:]
        argv += tool_args or []
        argv += list(spec.control_args)
        argv += extra
        if self.summary_only:
            argv += list(spec.summary_only_args)
        elif self.max_turns and spec.max_turns_flag:
            argv += [spec.max_turns_flag, str(int(self.max_turns))]
        if (self.restricted or mode == 'off') and spec.restricted_prompt_flag:
            # Bytes, as exec counts them: characters undercount any text
            # that is not ASCII.
            size = len(prompt.encode("utf-8", errors="replace"))
            if size > MAX_ARGV_PROMPT:
                # Falling back to a prompt file here would silently drop the
                # tool restrictions with it, turning a bounded worker call
                # into a full agent with writes. Refusing is the only option
                # that cannot be mistaken for success.
                raise ProviderError(
                    f"{self.label}: a restricted or native-delegation-off call delivers its prompt as an "
                    f"argument, and this one is {size:,} bytes "
                    f"(limit {MAX_ARGV_PROMPT:,}). "
                    + ("Shorten the prompt without removing its native-delegation controls."
                       if mode == 'off' else
                       "Route this task to an unrestricted seat rather than sending it unbounded.")
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
            if not isinstance(payload, dict) or not _is_cap_shaped(payload):
                raise
            turns = _envelope_turns(payload)
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

        A new 0600 file per call, in the private scratch root
        (``providers.scratch_root``): outside the directory the agent runs
        in, so it is never mistaken for source, and outside the shared temp
        folder, where another call's sandbox could read or replace it.
        ``_call`` removes it when the call ends and ``cleanup`` removes any
        that are left, so only a hard kill leaves a prompt behind.
        """
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         prefix="quadratus-prompt-", suffix=".txt",
                                         dir=scratch_root(), delete=False) as stream:
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
        #: A sandbox refusal was followed by a successful tool run in this call.
        self.last_tool_recovered = False
        self._call_started = datetime.now(timezone.utc)
        composed = self._compose_prompt(prompt, system, history)
        argv = self._build_argv(composed, system)
        if self.spec.session_id_flag:
            self.last_session_id = str(uuid.uuid4())
            argv += [self.spec.session_id_flag, self.last_session_id]
        # An inherited ANTHROPIC_API_KEY would silently divert a subscription
        # run onto billed API credits, and so would an auth token, a base URL
        # or a provider switch. Every other credential in the harness's
        # environment is readable by a model with a shell. The vendor profile
        # leaves all of them out and keeps this vendor's own sign-in variable.
        from .child_env import vendor_env
        env, _ = vendor_env(self.spec.vendor)
        env.update(self.spec.env)
        if getattr(self, "_disposable", False):
            # Python's tempfile tries TMPDIR first: pointed inside the copy,
            # a sandbox that allows writes under the working directory lets a
            # reviewer's pytest start (series rule-b1ff751 f4: no usable
            # temporary directory under the vendor's read-only mode).
            scratch = os.path.join(self.workdir, ".quadratus-tmp")
            os.makedirs(scratch, exist_ok=True)
            env["TMPDIR"] = scratch
        if getattr(self, "_native_fanout_denied", None):
            env.update(self.spec.native_fanout_off_env)
        if self.worker_tool_attached and self.spec.worker_tool_style == "claude":
            # claude's MCP tool-call timeout, in milliseconds.
            env["MCP_TOOL_TIMEOUT"] = str(int(self.worker_tool["timeout"]) * 1000)

        # The binary and a count only: the arguments carry the system prompt,
        # and on a restricted call the task prompt, which a debug log must
        # not copy out.
        log.debug("%s invoking: %s with %d arguments", self.label,
                  os.path.basename(str(argv[0])), len(argv) - 1)
        try:
            # exec carries no NUL in an argument: subprocess answers one with
            # ValueError("embedded null byte"), which is no ProviderError and
            # stopped the run as an unknown failure. Named here, before
            # anything is started; the argument itself is never quoted, since
            # it is a prompt.
            holding = [i for i, arg in enumerate(argv) if "\x00" in str(arg)]
            if holding:
                raise ProviderError(
                    f"{self.label} CLI was not started: its prompt holds a NUL character "
                    f"(argument {holding[0]} of {len(argv) - 1}), which a command line cannot "
                    "carry. Nothing ran. Remove the NUL from the text this call was given.")
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
            # ``reason``: the launcher's own account of a call it cut off for
            # a cause other than the clock (output it could not read whole).
            reason = getattr(exc, "reason", "") or f"timed out after {self.timeout}s"
            raise TimeoutError(f"{self.label} CLI {reason}") from exc
        except CLIOutputTooLarge as exc:
            # Accounting from what was read, then the bulk is let go: the
            # error keeps the two ends.
            self.last_stderr = (exc.stderr or "")[-STDERR_TAIL:]
            self._observe_output(exc.output or "")
            exc.output = exc.stderr = ""
            raise
        except FileNotFoundError as exc:
            raise ProviderError(f"{self.label} CLI vanished from PATH: {exc}") from exc
        except OSError as exc:
            if exc.errno != errno.E2BIG:
                raise
            # exec refused the argument list, so nothing ran. Named, since
            # the bare OSError reads as a transport failure.
            largest = max((len(str(arg).encode("utf-8", errors="replace")) for arg in argv), default=0)
            raise ProviderError(
                f"{self.label} CLI was not started: its argument list is too long for this system "
                f"(largest argument {largest:,} bytes; Linux allows 131,072 for one). "
                "Nothing ran.") from exc
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
        blocked = None
        if self.spec.extract_permission_block is not None:
            try:
                blocked = self.spec.extract_permission_block(proc.stderr or "")
            except Exception:  # noqa: BLE001 -- observational; the call's own failure stands
                blocked = None
            if blocked:
                self.last_diagnostics = dict(self.last_diagnostics or {}, permission_block=blocked)

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
                if blocked:
                    raise self._permission_blocked(parsed, blocked) from parsed
                if not getattr(parsed, "exit_report", False):
                    detail = (proc.stderr or '').strip()[:400]
                    if detail:
                        # The wrapper is what the caller sees, so it keeps
                        # what the envelope established.
                        wrapped = ProviderError(f'{parsed}; {detail}')
                        wrapped.diagnostics = self.last_diagnostics
                        carry_transport_facts(wrapped, parsed)
                        if getattr(parsed, "no_envelope", False):
                            # stdout held no report at all, so stderr is the
                            # CLI's only voice: read there, as for the bare
                            # exit below. An envelope that did report (a
                            # cancelled turn and its narration, an error
                            # field) is never overruled from stderr.
                            _mark_transport(wrapped, _own_stderr(proc.stderr))
                        raise wrapped from parsed
                    raise
            except Exception:  # noqa: BLE001 -- unparseable is the normal case
                pass
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            failure = RuntimeError(f"{self.label} CLI exited {proc.returncode}: {detail}")
            if proc.returncode < 0:
                # Ended by a signal: stopped from outside, like a timeout, so
                # what it had done by then is unknown and it is not re-sent.
                failure.interrupted = True
                raise failure
            # Classified from stderr alone, which is the CLI's own voice.
            # stdout may be a model's text and is only quoted above.
            raise _mark_transport(failure, _own_stderr(proc.stderr))

        try:
            return self._extract(proc.stdout)
        except ProviderRefusal as refusal:
            refusal.diagnostics = self.last_diagnostics
            refusal.model = self.model
            raise
        except ProviderError as failure:
            failure.diagnostics = self.last_diagnostics
            if blocked and not isinstance(failure, TurnLimitReached) and not getattr(failure, "auth_invalid", False):
                raise self._permission_blocked(failure, blocked) from failure
            raise

    def _permission_blocked(self, failure: ProviderError, blocked: Dict[str, object]) -> PermissionBlocked:
        """The typed denial for a failed turn whose stderr carries the CLI's
        own headless-approval line (cycle15). A refusal, a cap and an
        authentication failure keep their own types (the caller re-raises
        them first). The message names the tool, the scheme and the digest,
        never the raw target."""
        exc = PermissionBlocked(
            f"{self.label} stopped: {blocked.get('tool')} on a {blocked.get('scheme')} target "
            f"(digest {blocked.get('target_digest')}) needed approval a headless run cannot ask for; "
            "the call was not completed and is not retried",
            permission_block=blocked)
        exc.diagnostics = self.last_diagnostics
        return exc

    def _observe_output(self, stdout):
        """Extract accounting even when response parsing later fails.

        Each step is guarded on its own. They once shared one ``except``, so
        a parent journal with no timestamp, or a ``modelUsage`` that was not
        a mapping, ended the function where it stood and the CONTROL FAILURE
        note below was skipped without a trace. That note is now written in
        a ``finally``: whatever children were recorded before a step failed
        still get it.
        """
        try:
            for step in (self._observe_usage, self._observe_tool_failures, self._observe_children,
                         self._observe_session, self._observe_codex_session):
                try:
                    step(stdout)
                except Exception:  # noqa: BLE001 -- observational only
                    log.debug("native accounting step %s unavailable", step.__name__, exc_info=True)
        finally:
            try:
                self._annotate_control_failures()
            except Exception:  # noqa: BLE001 -- observational only
                log.debug("native control annotation unavailable", exc_info=True)

    def _observe_usage(self, stdout) -> None:
        self.last_usage = self.spec.extract_usage(stdout) if self.spec.extract_usage else None
        self.last_diagnostics = (
            self.spec.extract_diagnostics(stdout) if self.spec.extract_diagnostics else None
        )
        if getattr(self, "client_version", None):
            # Which client answered: the row can then be read against
            # the client's known behaviour (a 0.154 429 is a bare status).
            self.last_diagnostics = dict(self.last_diagnostics or {}, client_version=self.client_version)

    def _observe_tool_failures(self, stdout) -> None:
        try:
            # Isolated: a malformed denial list must not cost the rest of
            # the call's accounting (Codex review of 3a55d82).
            self.last_tool_failures = (
                self.spec.extract_tool_failures(stdout or "") if self.spec.extract_tool_failures else []
            )
        except Exception:  # noqa: BLE001 -- observational only
            self.last_tool_failures = []

    def _observe_children(self, stdout) -> None:
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

    def _observe_session(self, stdout) -> None:
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
            models = event.get("modelUsage")
            if not isinstance(models, dict):
                continue
            matching = [name for name in models
                        if isinstance(name, str) and self.model and self.model in name]
            if len(matching) == 1:
                self.resolved_model = matching[0]
        if not getattr(self, "last_session_id", None):
            try:
                whole = json.loads(stdout or "")
                if isinstance(whole, dict) and isinstance(whole.get("sessionId"), str):
                    self.last_session_id = whole["sessionId"]
            except ValueError:
                pass

    def _observe_codex_session(self, stdout) -> None:
        if self.spec.vendor != "openai" or not getattr(self, "last_session_id", None):
            return
        from .native_sessions import codex_children
        root = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
        # The native shell records a sandbox refusal only in the
        # session journal (#35); Fleet stops the run on one that no
        # later tool run recovered from.
        journal, journal_recovered = _codex_journal_tool_failures(
            root, self.last_session_id, self.workdir, self._call_started)
        self.last_tool_failures = list(self.last_tool_failures or []) + journal
        stdout_recovered = _codex_stdout_recovered(stdout)
        verdicts = [v for v in (stdout_recovered, journal_recovered) if v is not None]
        self.last_tool_recovered = bool(verdicts) and all(verdicts)
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

    def _annotate_control_failures(self) -> None:
        if not (self.native_children and self.native_delegation_disabled):
            return
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
            if note not in child.detail:
                log.warning("%s: %s", self.label, note)
            annotated.append(_annotate_child(child, note))
        self.native_children = annotated

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
        # The typed fact, set where the CLI's own envelope or stderr was read
        # (``_own_error_facts``). This once searched the failure's whole text
        # for "500", "connection" or "try again", and that text can quote a
        # model's answer.
        return getattr(exc, "retryable", False) is True


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
