"""Unified provider abstraction for Claude, ChatGPT and Grok.

Each provider sends the request shape its vendor documents *today* -- the
endpoint, the token-cap parameter and the model ID format were each checked
against the vendor reference on 2026-09-06 and the finding is recorded in
the class docstring, so the next audit knows what was verified and when.

Each provider exposes the same ``generate()`` interface, hides SDK-specific
details, and shares retry/backoff handling. Providers degrade gracefully: if
an SDK is not installed or an API key is missing, the provider reports itself
as unavailable instead of crashing the whole workflow.
"""

from __future__ import annotations

import copy
import logging
import os
import random
import re
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider call failed permanently (after retries or non-retryable)."""


class TurnLimitReached(ProviderError):
    """A call stopped at its agentic turn limit before finishing.

    Not a refusal, a timeout or a transport failure: the vendor ran the call,
    its usage is real, and a writable call may already have changed files. It
    is never retried here -- a ProviderError passes straight through -- and it
    carries whatever answer text the vendor returned, which is narration of
    unfinished work and must never be read as a result.
    """

    def __init__(self, message, *, partial_text=None, turns=None):
        super().__init__(message)
        self.partial_text = partial_text if isinstance(partial_text, str) and partial_text.strip() else None
        self.turns = turns if isinstance(turns, int) and not isinstance(turns, bool) else None


class PartialWorkSuspected(ProviderError):
    """An editing call stopped without saying what it had already written.

    A timeout or a cancellation is not the same as a failure: the vendor
    process may have saved most of its work before it stopped. The 2026-09-13
    trial recorded exactly this -- a 900-second editing call whose output was
    on disk while the transport layer, seeing only a timeout, began replaying
    the same writing prompt against the tree that call had just changed.

    A :class:`ProviderError` subclass so existing handlers still treat it as a
    permanent call failure rather than something to retry, and so nothing that
    catches ProviderError has to learn a new type to stay correct. What it adds
    is the instruction not to replay, and ``cause`` for the caller that wants
    to know which kind of stop it was.
    """

    def __init__(self, message: str, *, cause: Optional[Exception] = None,
                 interrupted: bool = True, retryable: bool = False) -> None:
        super().__init__(message)
        #: The transport-level exception underneath -- a TimeoutError, usually.
        self.cause = cause
        #: The call was stopped from outside (a timeout, an output cap) and
        #: did not fail on its own, so what it had done by then is unknown.
        #: True unless whoever raises this knows otherwise: the careful
        #: reading is the default.
        self.interrupted = interrupted
        #: The failure underneath carried the typed ``retryable`` fact. The
        #: provider still does not replay it: only a caller that has measured
        #: the project unchanged may (``runtime.Fleet``).
        self.retryable = retryable


class TransientOverload(ProviderError):
    """The vendor said it was overloaded and did not answer: a typed status
    (Anthropic's 529 overloaded_error, as the Claude CLI's error envelope
    reports it) on a call whose usage the envelope settled. Retried a
    bounded number of times by ``_generate_once``, only where a replay
    cannot be a second edit (cycle06 run 20261108T075040Z-4454b282: the
    read-only planner's 529 ended a run with 32 calls and 2.36M tokens
    left). Never matched from a model's prose."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


#: How many times one call is re-sent after a TransientOverload, each one a
#: separately admitted attempt (``_observed_call``): budget, finish hold and
#: ledger as for any attempt.
MAX_TRANSIENT_RETRIES = 2


class PermissionBlocked(ProviderError):
    """A headless CLI call stopped on a tool call it needed approval for and
    could not ask for (cycle15; cycle14 t5: grok's restricted seat opened
    README.md through Fetch on a file:// URL and the turn came back
    cancelled). Raised only from the CLI's own exact headless-approval line,
    never from model text. A denial, not a transport failure: never retried,
    never handed to another model or tool, never a failed task to continue.

    ``permission_block`` is bounded metadata: the tool, the target's scheme
    ("file", "https", ... or "path") and a digest of the target; the raw
    target stays only in the private stderr evidence."""

    def __init__(self, message: str, *, permission_block: Optional[dict] = None) -> None:
        super().__init__(message)
        self.permission_block = dict(permission_block or {})


class ProviderRefusal(ProviderError):
    """The model's safety classifiers declined the request.

    Anthropic returns this as a *successful* HTTP 200 with
    ``stop_reason: "refusal"`` and an empty or partial ``content`` -- the
    Claude API since Opus 4.7, and every Fable / Mythos model. Reading
    ``content[0]`` unguarded turns it into a misleading "empty response".
    Raised instead so the cause is visible, and so :meth:`LLMProvider.generate`
    can re-send the same request once to a configured fallback model.

    Never retried on the same model: the classifiers are deterministic for a
    given request, so a verbatim retry is a known-bad action.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str = "",
        category: Optional[str] = None,
        explanation: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        #: ``stop_details.category`` when the API supplied one -- e.g. "cyber",
        #: "bio", "reasoning_extraction". None is a valid, permanent state.
        self.category = category
        self.explanation = explanation


#: The typed facts a failed call may carry, as attributes on its exception.
#: They are set where a provider reads the vendor's own envelope or exit
#: shape (an HTTP status in the CLI's JSON, a line of its stderr), never from
#: text a model wrote, and they are the only thing retry, replay and
#: window-exhaustion decisions read. The message of an exception may quote
#: model narration for a person to read; no decision is made from it.
#:
#: ``window_exhausted``: the subscription window is spent. ``retryable``: a
#: failure that passes (a rate limit, a server error, a dropped connection).
#: ``auth_invalid``: the sign-in was rejected. ``transport_status``: the HTTP
#: status the envelope stated, for the record.
TRANSPORT_FACTS = ("window_exhausted", "retryable", "auth_invalid", "transport_status")


def carry_transport_facts(target: BaseException, source: Optional[BaseException]) -> BaseException:
    """Copy the typed facts from ``source`` onto the error that wraps it.

    A wrapper keeps the message for people and would otherwise drop the facts
    the decisions read, which is how classification drifted onto the message.
    """
    for name in TRANSPORT_FACTS:
        value = getattr(source, name, None)
        if value is None or value is False:
            continue
        try:
            setattr(target, name, value)
        except Exception:  # noqa: BLE001 -- an exception that refuses attributes keeps its own
            pass
    return target


_scratch_noted = False

#: How old a leftover under the scratch root must be before a run removes it.
SCRATCH_MAX_AGE_SECONDS = 24 * 60 * 60
#: The file the harness leaves in a scratch folder it made. Only a folder
#: that carries it is ever swept: a folder the operator made, and may keep
#: other things in, is never cleared on the strength of a name.
SCRATCH_MARKER = ".quadratus-scratch"
#: What the harness makes there, exactly as it names it: a source copy
#: (review, original-tests), a summary folder (analysis, closeout), a
#: provider's own folder (by provider name) and the worker bridge's socket
#: folder, each with the eight characters ``tempfile`` adds; and a prompt
#: file. Final review, 2026-11-11: any name starting ``quadratus-`` or
#: ``qw-`` went, and in a scratch folder the operator chose that took
#: ``quadratus-main``, a checkout, with it. A test holds these to the
#: prefixes the package passes to ``tempfile``.
SCRATCH_FOLDER_KINDS = ("review", "analysis", "closeout", "original-tests", "claude", "openai", "grok")
_SCRATCH_FOLDER = re.compile(rf"(?:quadratus-(?:{'|'.join(SCRATCH_FOLDER_KINDS)})-|qw-)[a-z0-9_]{{8}}")
_SCRATCH_FILE = re.compile(r"quadratus-prompt-[a-z0-9_]{8}\.txt")
_unmarked_noted = False


def _mark_scratch(root) -> None:
    """Leave the marker in a scratch folder the harness made. Never raises:
    a folder that cannot be marked is simply never swept."""
    try:
        fd = os.open(os.path.join(root, SCRATCH_MARKER),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return
    try:
        os.write(fd, b"Quadratus made this folder for its scratch copies. A run removes what earlier runs\n"
                     b"left here after a day, and nothing from a folder without this file.\n")
    except OSError:
        pass
    finally:
        os.close(fd)


def scratch_marked(root) -> bool:
    """Whether ``root`` carries the harness's own marker: a regular file
    this user owns, never a link."""
    try:
        info = os.lstat(os.path.join(root, SCRATCH_MARKER))
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and (os.name != "posix" or info.st_uid == os.getuid())


def scratch_root_path() -> Path:
    """Where :func:`scratch_root` is, without creating or checking it:
    ``QUADRATUS_SCRATCH_DIR`` when set, else ``~/.cache/quadratus/scratch``."""
    raw = os.environ.get("QUADRATUS_SCRATCH_DIR", "").strip()
    return Path(os.path.abspath(Path(raw).expanduser() if raw else Path.home() / ".cache" / "quadratus" / "scratch"))


def scratch_inside(folder) -> str:
    """Why the scratch root may not be used with a project at ``folder``,
    or "" when it may.

    A scratch root inside the project puts every source copy the harness
    makes into the tree it measures: the copies are diffed, reviewed and
    copied again as project source. Asked before the root is created, so a
    refusal leaves nothing behind in the project.
    """
    try:
        root, project = scratch_root_path(), Path(folder)
        resolved, inside = Path(os.path.realpath(root)), Path(os.path.realpath(project))
    except (OSError, RuntimeError, ValueError):
        return ""  # scratch_root itself reports a root it cannot place
    if resolved != inside and inside not in resolved.parents:
        return ""
    # The default itself is inside a project that is the home folder or
    # ~/.cache: "unset it" would then lead straight back here.
    chosen = bool(os.environ.get("QUADRATUS_SCRATCH_DIR", "").strip())
    return (f"The scratch folder {root} is inside the project {project}. The harness makes its "
            "source copies there, and inside the project they would be measured, diffed and "
            "reviewed as project source. Set QUADRATUS_SCRATCH_DIR to a folder outside the "
            "project that only you can read and write"
            + (", or unset it to use ~/.cache/quadratus/scratch." if chosen else
               " (the default, ~/.cache/quadratus/scratch, is inside this project)."))


def sweep_scratch(max_age: float = SCRATCH_MAX_AGE_SECONDS, *, now: Optional[float] = None) -> List[str]:
    """Remove what earlier runs left under the scratch root; return the names.

    A hard kill leaves source copies behind, and nothing clears a folder
    under the home directory the way the system clears its temp folder.

    Two conditions, both required. The root carries the marker the harness
    wrote when it made the folder (``SCRATCH_MARKER``): a root without it is
    never swept, whatever is in it. And only a direct child goes that is
    exactly what the harness makes there, a real folder or a real file of
    that shape (``SCRATCH_FOLDER_KINDS``), that this user owns and that is
    older than ``max_age``: a run still using a copy made it minutes ago,
    not a day ago. A link is never followed and never removed (the harness
    makes none), nothing above or beside the root is opened, and a failure
    costs that one entry. Never raises: this is housekeeping before a run,
    not a condition of it.
    """
    import shutil
    global _unmarked_noted
    removed: List[str] = []
    try:
        root = scratch_root()
        cutoff = (time.time() if now is None else now) - float(max_age)
        with os.scandir(root) as listing:
            entries = [entry.name for entry in listing]
    except Exception:  # noqa: BLE001 -- the first call that needs the root reports it
        log.debug("the scratch folder was not swept", exc_info=True)
        return removed
    if not scratch_marked(root):
        if not _unmarked_noted:
            _unmarked_noted = True
            log.info("The scratch folder %s was not made by the harness (it has no %s file), so nothing in "
                     "it is ever removed. Create an empty file of that name there to let a run clear what "
                     "earlier runs left.", root, SCRATCH_MARKER)
        return removed
    for name in sorted(entries):
        folder, single = _SCRATCH_FOLDER.fullmatch(name), _SCRATCH_FILE.fullmatch(name)
        if not folder and not single:
            continue
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
            if (os.name == "posix" and info.st_uid != os.getuid()) or info.st_mtime > cutoff:
                continue
            if folder and stat.S_ISDIR(info.st_mode):
                shutil.rmtree(path)  # unlinks a link inside, never follows it
            elif single and stat.S_ISREG(info.st_mode):
                os.unlink(path)
            else:
                continue             # the name, but not what the harness makes under it
            removed.append(name)
        except OSError:
            log.debug("could not remove the scratch leftover %s", name, exc_info=True)
    if removed:
        log.info("Removed %d leftover(s) older than %d hours from the scratch folder %s.",
                 len(removed), int(max_age // 3600), root)
    return removed


def scratch_root() -> str:
    """The private folder harness scratch lives under: source copies handed
    to review calls, prompt files, the empty folders summary calls run in.

    ``QUADRATUS_SCRATCH_DIR`` when set, else ``~/.cache/quadratus/scratch``.
    Not the system temp folder, because a vendor sandbox in workspace-write
    mode may write under ``/tmp`` and ``$TMPDIR``: an editing call steered by
    text it read could drop a ``conftest.py`` into a concurrent reviewer's
    copy there, and new files in a copy are that call's own scratch by
    design, so nothing would flag it.

    Each missing folder on the way is created 0700. The root must be a real
    folder (not a link) that this user owns and nobody else can enter; one
    that is not is refused, never repaired and never replaced by the temp
    folder in silence, and the refusal names the variable that picks another.

    A root this call creates is given the marker that lets a later run sweep
    it (``SCRATCH_MARKER``). So is the default root when it has none: only
    the harness makes ``~/.cache/quadratus/scratch``, and one made before
    the marker existed would otherwise never be cleared again. A folder the
    operator named that already existed is theirs and is left unmarked.
    """
    global _scratch_noted
    way_out = "Set QUADRATUS_SCRATCH_DIR to a folder only you can read and write."
    try:
        root = scratch_root_path()
    except RuntimeError as exc:  # no home folder to resolve
        raise ProviderError(f"Cannot place the private scratch folder: {exc}. {way_out}") from exc
    try:
        missing, probe = [], root
        while not os.path.lexists(probe) and probe.parent != probe:
            missing.append(probe)
            probe = probe.parent
        made = False
        for folder in reversed(missing):
            try:
                os.mkdir(folder, 0o700)
                made = folder == root
            except FileExistsError:
                pass  # another run made it first
        info = os.lstat(root)
    except OSError as exc:
        raise ProviderError(f"Cannot prepare the private scratch folder {root}: {exc}. {way_out}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ProviderError(f"The scratch folder {root} is a link or a file, not a folder. {way_out}")
    if os.name == "posix":
        if info.st_uid != os.getuid():
            raise ProviderError(f"The scratch folder {root} belongs to another user. {way_out}")
        if info.st_mode & 0o077:
            raise ProviderError(
                f"The scratch folder {root} is open to other users (mode "
                f"{stat.S_IMODE(info.st_mode):o}). Make it private (chmod 700) or name another. {way_out}")
    if made or not os.environ.get("QUADRATUS_SCRATCH_DIR", "").strip():
        _mark_scratch(root)
    if not _scratch_noted:
        try:
            inside_temp = root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        except (OSError, ValueError):
            inside_temp = False
        if inside_temp:
            # Said once, and not as a warning: it is the operator's choice
            # (or a container whose home is a temp mount), not a fault.
            _scratch_noted = True
            log.info(
                "The scratch folder %s is inside the system temp folder, which a vendor sandbox in "
                "workspace-write mode can write. %s", root, way_out)
    return str(root)


@dataclass
class Turn:
    """A single prior message handed to a provider as conversation context."""

    role: str  # "user" or "assistant"
    content: str


class LLMProvider:
    """Base class implementing shared retry/backoff and availability logic.

    Subclasses implement :meth:`_build_client`, :meth:`_call`, and
    :meth:`_retryable`.
    """

    #: short machine name, e.g. "claude"
    transport = "api"
    name = "provider"
    #: human label, e.g. "Claude"
    label = "Provider"

    def __init__(
        self,
        model: str,
        api_key: Optional[str],
        *,
        max_tokens: int = 8000,
        timeout: float = 120.0,
        max_retries: int = 4,
        retry_base_delay: float = 2.0,
        refusal_fallback_model: Optional[str] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.retry_base_delay = retry_base_delay
        #: Where a request goes when this model's classifiers decline it.
        #: None or empty means the refusal surfaces as :class:`ProviderRefusal`.
        #: Not sticky: the next call goes back to the primary model, because
        #: history here is plain text -- no thinking blocks are replayed, so
        #: there is no reasoning continuity to protect by staying switched.
        self.refusal_fallback_model = refusal_fallback_model or None
        self._client = None
        self._init_error: Optional[str] = None
        if api_key:
            try:
                self._client = self._build_client()
            except Exception as exc:  # ImportError or bad config
                self._init_error = str(exc)
                log.warning("Could not initialise %s: %s", self.label, exc)
        else:
            self._init_error = "no API key configured"

    # -- subclass hooks ------------------------------------------------------
    def _build_client(self):
        raise NotImplementedError

    def _call(self, prompt: str, system: str, history: Sequence[Turn]) -> str:
        raise NotImplementedError

    def _retryable(self, exc: Exception) -> bool:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _exc_tuple(module, names: Sequence[str]) -> tuple:
        """Collect exception classes that exist on ``module`` into a tuple."""
        found = []
        for n in names:
            cls = getattr(module, n, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                found.append(cls)
        return tuple(found)

    # -- public API ----------------------------------------------------------
    def available(self) -> bool:
        return self._client is not None

    #: How much reasoning the seat asks for, where the transport exposes a
    #: dial. Empty means "whatever the CLI does by default".
    effort: str = ""
    #: Whether this seat gets a bounded call instead of a full agent loop.
    #: Meaningless to an HTTP API, which is a completion already -- the
    #: attribute lives on the base so routing can set it without knowing
    #: which transport is underneath.
    restricted: bool = False

    def for_seat(
        self,
        model: Optional[str],
        *,
        effort: str = "",
        restricted: bool = False,
    ) -> "LLMProvider":
        """A view of this provider bound to one seat's whole invocation.

        A seat is not only a model. Two seats can address the same model and
        still want different calls -- a brain-trust member exploring a
        repository and a worker answering one question are the same weights
        run very differently, and on a subscription the difference is most of
        the cost. So the identity that matters here is (model, effort,
        restricted), and a clone is made whenever any of the three differs.
        """
        same = (
            (model is None or model == self.model)
            and effort == self.effort
            and restricted == self.restricted
        )
        if same:
            return self
        clone = copy.copy(self)
        if model is not None:
            clone.model = model
        clone.effort = effort
        clone.restricted = restricted
        return clone

    def for_model(self, model: Optional[str]) -> "LLMProvider":
        """Return a view of this provider bound to a different model.

        Model routing sends cheap phases (convergence judging, classification,
        summarisation) to a smaller model on the same subscription. Copying the
        provider keeps the already-built client and avoids re-resolving a CLI
        binary or re-constructing an SDK client on every routed call.

        ``None`` means "no change". The empty string is different and load
        bearing: it means *name no model*, so the CLI applies its own current
        default. Collapsing the two would silently send whichever model this
        provider happened to be constructed with.
        """
        if model is None or model == self.model:
            return self
        clone = copy.copy(self)
        clone.model = model
        return clone

    @property
    def status(self) -> str:
        if not self.available():
            return f"{self.label} unavailable ({self._init_error})"
        # An empty model is a value here, not a gap: it means the CLI picks,
        # which is how a seat stays on whatever the vendor currently ships.
        return f"{self.label} ({self.model or 'CLI default'})"

    def generate(
        self,
        prompt: str,
        *,
        system: str = "You are a helpful assistant.",
        history: Optional[Sequence[Turn]] = None,
    ) -> str:
        if not self.available():
            raise ProviderError(f"{self.label} is not available: {self._init_error}.")
        turns = list(history or [])
        try:
            return self._generate_once(prompt, system, turns)
        except ProviderRefusal as refusal:
            fallback = getattr(self, "refusal_fallback_model", None)
            if not fallback or fallback == self.model:
                raise
            log.warning(
                "%s (%s) declined the request%s; re-sending once on %s.",
                self.label,
                self.model,
                f" [{refusal.category}]" if refusal.category else "",
                fallback,
            )
            try:
                return self.for_model(fallback)._generate_once(prompt, system, turns)
            except ProviderRefusal as second:
                raise ProviderRefusal(
                    f"{self.label}: both {self.model} and the fallback {fallback} "
                    f"declined the request ({second}).",
                    model=fallback,
                    category=second.category,
                    explanation=second.explanation,
                ) from second

    def _observed_call(self, prompt, system, turns, attempt):
        from .workers import worker_loop_control
        worker_control = worker_loop_control.get()
        if worker_control is not None:
            worker_control.reserve()
        control = getattr(self, 'run_budget', None)
        # A summary-only call reserves a measured allowance instead of a full
        # agentic call's headroom, and only where the CLI argv enforces one
        # model turn (CLISpec.summary_turn_capped): the request alone is not
        # a bound (Codex review of 961d2da on #53).
        shape = {}
        spec = getattr(self, 'spec', None)
        if (getattr(self, 'summary_only', False) and not getattr(self, 'full_token_reserve', False)
                and spec is not None
                and getattr(spec, 'summary_turn_capped', lambda: False)()):
            from .run_budget import SUMMARY_CALL_RESERVE_TOKENS
            shape = dict(expected_tokens=SUMMARY_CALL_RESERVE_TOKENS)
        identity = None
        if control is not None:
            import hashlib

            from .delegation import invocation_context
            context = invocation_context.get() or {}
            if context.get('origin') == 'worker':
                # A worker errand: a reserve the budget cannot hold fails this
                # errand and nothing else (run_budget.ReserveRefused). Passed
                # only here, so every other call reserves as it always did.
                shape['terminal'] = False
            identity = dict(seat=f'{self.name}:{self.model}', task=str(context.get('task') or 'run'),
                            role=str(context.get('role') or 'direct'),
                            prompt_digest=hashlib.sha256((prompt or '').encode()).hexdigest()[:32],
                            authority=str(context.get('authority') or ''))
        if control is not None and control.limits.max_cost_usd is not None:
            ticket, remaining = control.reserve(transport=self.transport,
                                                price_key=f'{self.name}:{self.model}', identity=identity, **shape)
        else:
            ticket, remaining = (control.reserve(identity=identity, **shape) if control is not None
                                 else (None, None))
        # The budget operation this call is: its id rides on the ledger row,
        # so an unknown-usage latch names the call that caused it.
        self.last_operation = f'O-{ticket}' if isinstance(ticket, int) else None
        previous_timeout = self.timeout if control is not None else None
        if control is not None:
            self.timeout = remaining if previous_timeout is None else min(previous_timeout, remaining)
        self.last_usage = None
        self.last_session_id = None
        self.resolved_model = None
        self.native_children = []
        started = time.monotonic()
        failure = None
        text = ""
        waited = getattr(self, "_retry_wait", None)
        self._retry_wait = None
        try:
            text = self._call(prompt, system, turns)
            if not text:
                raise ProviderError(f"{self.label} returned an empty response.")
            return text
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if waited:
                # The wait before this re-sent attempt, on its own ledger row.
                self.last_diagnostics = {**(getattr(self, "last_diagnostics", None) or {}),
                                         "retry_wait_seconds": waited["seconds"], "retry_cause": waited["cause"]}
            budget_failure = None
            if control is not None:
                try:
                    control.finish(ticket, self.last_usage, native_children=self.native_children,
                                   reply=text, price_key=f'{self.name}:{self.resolved_model or self.model}')
                except Exception as exc:
                    # Preserve an original interruption/partial-work failure.
                    # The shared controller is latched, so no retry can start.
                    if failure is None:
                        failure = budget_failure = exc
                        exc.provider_outcome = 'ok'
                        exc.post_return_failure = True
                        # Also retain it on the exception for direct callers
                        # whose controller has no on-disk run directory.
                        exc.provider_response = text
            if control is not None:
                self.timeout = previous_timeout
            if worker_control is not None:
                try:
                    worker_control.finish(self.last_usage)
                except Exception as exc:
                    if failure is None:
                        failure = budget_failure = exc
                        exc.provider_outcome = 'ok'
                        exc.post_return_failure = True
                        exc.provider_response = text
            observer = getattr(self, "attempt_observer", None)
            if observer is not None:
                try:
                    observer(self, attempt, time.monotonic() - started, failure, text)
                except Exception:
                    log.debug("attempt accounting failed", exc_info=True)
            if budget_failure is not None:
                raise budget_failure

    def _generate_once(self, prompt: str, system: str, turns: List[Turn]) -> str:
        """One model's attempt, with transport retries. Refusals pass through.

        A call that can write is never retried here, whatever the failure
        says. On 2026-09-13 an editing call hit its 900s ceiling with work
        already saved to disk, and the retry loop began replaying the same
        writing prompt against a tree that call had already changed: doubling
        edits, or applying a second pass to a first pass's output. That stop
        once covered only failures whose text said "timed out" or
        "cancelled", so a writing call that edited and then printed a
        connection error was replayed four times. Whether a failed editing
        call may be replayed is decided by whoever measured the project
        around it (``runtime.Fleet``), never by the wording of the failure.

        A call that cannot write changed nothing, so re-sending it is a
        genuine retry: on a timeout, and on a failure that carries the typed
        ``retryable`` fact (``TRANSPORT_FACTS``).
        """
        from .run_budget import FinishReserved, RunBudgetExceeded

        writes = bool(getattr(self, "allow_writes", False))
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            final = attempt >= self.max_retries - 1
            try:
                text = self._observed_call(prompt, system, turns, attempt + 1)
                if not text:
                    raise ProviderError(f"{self.label} returned an empty response.")
                return text
            except TransientOverload as exc:
                wait = self._transient_wait(exc, attempt)
                if wait is None:
                    raise
                log.warning("%s was overloaded (status %s); re-sending the same call in %.1fs (attempt %d).",
                            self.label, exc.status, wait, attempt + 1)
                self._retry_wait = dict(seconds=round(wait, 3), cause="overloaded", status=exc.status)
                last_exc = exc
                time.sleep(wait)
                continue
            except FinishReserved as held:
                if last_exc is None:
                    raise
                # A re-send the finish hold refused. The call was sent and
                # failed, so its own failure is what the caller gets:
                # FinishReserved means "refused before it started", and
                # every handler reads it as no call made. The refusal is on
                # the budget's record already.
                failure = (last_exc if isinstance(last_exc, ProviderError) else
                           carry_transport_facts(ProviderError(f"{self.label} call failed: {last_exc}"), last_exc))
                failure.resend_refused = str(held)[:300]
                try:
                    failure.args = (f"{failure}; it was not sent again, to keep the calls held for the finish",)
                except Exception:  # noqa: BLE001 -- the failure stands as it is
                    pass
                raise failure from held
            except RunBudgetExceeded:
                raise
            except ProviderError as exc:
                # Final, unless the vendor's own envelope named a failure
                # that passes (a rate limit, a server error).
                if writes or final or getattr(exc, "retryable", False) is not True:
                    raise
                last_exc = exc
            except Exception as exc:
                last_exc = exc
                if writes:
                    interrupted = (isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                                   or getattr(exc, "interrupted", False) is True)
                    try:
                        passing = not interrupted and bool(self._retryable(exc))
                    except Exception:  # noqa: BLE001 -- a provider with no retry rule
                        passing = False
                    stop = PartialWorkSuspected(
                        f"{self.label} did not complete ({exc}). This call could "
                        f"write, so the working tree may already hold partial "
                        f"work; it must be inspected before anything is retried. "
                        f"Replaying the same prompt against a changed tree is "
                        f"not a retry, it is a second, different edit.",
                        cause=exc,
                        interrupted=interrupted,
                        retryable=passing,
                    )
                    # The stop is what the caller sees, so it keeps the facts
                    # its cause carried: without them a spent window met by
                    # an editing call was never marked, and the model stayed
                    # on the roster. ``retryable`` stays as decided above.
                    carry_transport_facts(stop, exc)
                    stop.retryable = passing
                    raise stop from exc
                if final or not self._retryable(exc):
                    break
            delay = self.retry_base_delay * (2 ** attempt) + random.uniform(0, 1)
            log.warning(
                "%s call failed (%s); retrying in %.1fs (attempt %d/%d).",
                self.label,
                last_exc,
                delay,
                attempt + 1,
                self.max_retries,
            )
            time.sleep(delay)
        failure = ProviderError(f"{self.label} call failed: {last_exc}")
        raise carry_transport_facts(failure, last_exc) from last_exc

    def _transient_wait(self, exc: "TransientOverload", attempt: int) -> Optional[float]:
        """The wait before re-sending a call the vendor refused as overloaded,
        or None when it must not be re-sent: the retries are spent
        (``MAX_TRANSIENT_RETRIES``, within ``max_retries``), the call could
        write (a replay against a tree it may have changed is a second edit),
        its usage is unknown (nothing settled the attempt), or the wait would
        not leave the run's own wall time, after the finish hold, for the
        call itself. The same seat, model and role are re-sent; nothing
        falls back to another model or an API key."""
        if attempt >= min(MAX_TRANSIENT_RETRIES, self.max_retries - 1):
            return None
        if getattr(self, "allow_writes", False):
            return None
        if getattr(self, "last_usage", None) is None:
            return None
        wait = self.retry_base_delay * (2 ** attempt) + random.uniform(0, 1)
        control = getattr(self, "run_budget", None)
        if control is not None:
            snap = control.snapshot()
            limits = snap.get("limits") or {}
            from .run_budget import FINISH_STAGES, finish_stage
            stage = finish_stage.get()
            pending = [p for p in ((snap.get("finish_protection") or {}).get("pending") or []) if p in FINISH_STAGES]
            # The finishing calls still held after this one (all of them for
            # an ordinary call), as the budget's own admission counts them.
            held_calls = len(pending if stage is None else
                             [p for p in pending if FINISH_STAGES.index(p) > FINISH_STAGES.index(stage)])
            if limits.get("max_calls") is not None and (
                    int(limits["max_calls"]) - int(snap.get("reserved_attempts") or 0) < 1 + held_calls):
                # No attempt is left for it: waiting would only delay the
                # same stop (or the finish hold's refusal).
                return None
            held = float((snap.get("finish_protection") or {}).get("held_wall_seconds") or 0)
            left = float(limits.get("wall_seconds") or 0) - float(snap.get("elapsed_seconds") or 0) - held
            if wait >= left / 2:
                # Half of what is left at most: the re-sent call needs time
                # of its own after the wait.
                return None
        return wait


def _field(obj, name: str, default=None):
    """Read ``name`` off an SDK model or a plain dict; None when absent."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class ClaudeProvider(LLMProvider):
    """Claude over the Messages API.

    Verified against Anthropic's reference on 2026-09-06: ``messages.create``
    with ``system`` and ``max_tokens``; no sampling parameters (``temperature``
    and friends return 400 on Opus 4.7 and later); omitting ``thinking`` runs
    adaptive thinking on Claude Opus 5; a classifier decline is an HTTP 200 with
    ``stop_reason: "refusal"`` and a ``stop_details`` object, which is why
    :meth:`_call` branches on the stop reason before reading content.
    """

    name = "claude"
    label = "Claude"

    def _build_client(self):
        import anthropic

        self._sdk = anthropic
        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=0)

    def _call(self, prompt, system, history):
        messages = [{"role": t.role, "content": t.content} for t in history]
        messages.append({"role": "user", "content": prompt})
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            timeout=self.timeout,
        )
        usage = _field(resp, 'usage')
        values = [_field(usage, key) for key in ('input_tokens', 'output_tokens')]
        cached = [_field(usage, key, 0) for key in ('cache_read_input_tokens', 'cache_creation_input_tokens')]
        self.last_usage = ({'input_tokens': values[0] + sum(cached), 'output_tokens': values[1]}
                           if all(type(n) is int and n >= 0 for n in values + cached) else None)
        self.resolved_model = _field(resp, 'model') or self.model
        # Branch on stop_reason before touching content: a classifier decline
        # is an HTTP 200 whose content is empty (pre-output) or partial
        # (mid-stream), and a partial must not be mistaken for an answer.
        if _field(resp, "stop_reason") == "refusal":
            details = _field(resp, "stop_details")
            category = _field(details, "category")
            explanation = _field(details, "explanation")
            raise ProviderRefusal(
                f"{self.label} ({self.model}) declined the request"
                + (f" [{category}]" if category else "")
                + (f": {explanation}" if explanation else "")
                + ".",
                model=self.model,
                category=category,
                explanation=explanation,
            )
        return "".join(
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )

    def _retryable(self, exc):
        retry = self._exc_tuple(
            self._sdk,
            (
                "RateLimitError",
                "APITimeoutError",
                "APIConnectionError",
                "InternalServerError",
            ),
        )
        return bool(retry) and isinstance(exc, retry)


class _OpenAISDKProvider(LLMProvider):
    """Shared client construction and retry rules for the OpenAI Python SDK.

    Two vendors speak this SDK -- OpenAI itself and xAI -- but they do not
    share an endpoint (see the subclasses), so only the client and the error
    classes live here.
    """

    #: Override to point the same SDK at a compatible host.
    base_url: Optional[str] = None

    def _build_client(self):
        import openai

        self._sdk = openai
        kwargs = {"api_key": self.api_key, "timeout": self.timeout, "max_retries": 0}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return openai.OpenAI(**kwargs)

    def _retryable(self, exc):
        retry = self._exc_tuple(
            self._sdk,
            (
                "RateLimitError",
                "APITimeoutError",
                "APIConnectionError",
                "InternalServerError",
            ),
        )
        return bool(retry) and isinstance(exc, retry)


class OpenAIProvider(_OpenAISDKProvider):
    """ChatGPT over the Responses API (``POST /v1/responses``).

    Verified against OpenAI's API reference on 2026-09-06: "While Chat
    Completions remains supported, Responses is recommended for all new
    projects", and the ``-pro`` models (``gpt-5.5-pro`` among them) list Chat
    Completions as *not supported* -- they exist only on Responses and Batch.
    Responses therefore covers every current model; Chat Completions does not.

    ``instructions`` carries the system prompt, ``input`` the turn list, and
    ``max_output_tokens`` the cap. ``temperature`` is deliberately not sent:
    the reasoning models reject it.
    """

    name = "openai"
    label = "ChatGPT"

    def _call(self, prompt, system, history):
        turns = [{"role": t.role, "content": t.content} for t in history]
        turns.append({"role": "user", "content": prompt})
        resp = self._client.responses.create(
            model=self.model,
            instructions=system,
            input=turns,
            max_output_tokens=self.max_tokens,
            timeout=self.timeout,
        )
        usage = _field(resp, 'usage')
        self.last_usage = {'input_tokens': _field(usage, 'input_tokens'),
                           'output_tokens': _field(usage, 'output_tokens')}
        self.resolved_model = _field(resp, 'model') or self.model
        return resp.output_text or ""


#: xAI serves Grok over an OpenAI-compatible API on its own host.
XAI_BASE_URL = "https://api.x.ai/v1"


class GrokProvider(_OpenAISDKProvider):
    """Grok over xAI's billed API (``POST /v1/chat/completions``).

    Verified against docs.x.ai on 2026-09-06: the documented integration is
    the OpenAI Python SDK with ``base_url="https://api.x.ai/v1"`` against Chat
    Completions, with ``max_completion_tokens`` as the token cap (``max_tokens``
    is marked deprecated there) and the ``system`` role accepted in
    ``messages``. xAI also exposes ``/v1/responses``, but Chat Completions is
    what its reference documents for every current text model, so that is
    what this sends.
    """

    name = "grok"
    label = "Grok"
    base_url = XAI_BASE_URL

    def _call(self, prompt, system, history):
        messages = [{"role": "system", "content": system}]
        messages += [{"role": t.role, "content": t.content} for t in history]
        messages.append({"role": "user", "content": prompt})
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_completion_tokens=self.max_tokens,
            timeout=self.timeout,
        )
        usage = _field(resp, 'usage')
        self.last_usage = {'input_tokens': _field(usage, 'prompt_tokens'),
                           'output_tokens': _field(usage, 'completion_tokens')}
        self.resolved_model = _field(resp, 'model') or self.model
        return resp.choices[0].message.content or ""


#: Registry mapping provider names to (class, key-attr, model-attr).
_REGISTRY = {
    "claude": (ClaudeProvider, "anthropic_api_key", "claude_model"),
    "openai": (OpenAIProvider, "openai_api_key", "openai_model"),
    "grok": (GrokProvider, "xai_api_key", "grok_model"),
}


def build_provider(name: str, settings, *, allow_writes: bool = False, workdir=None,
                   resolve_family: bool = True) -> Optional[LLMProvider]:
    """Build a single provider by name from settings, or ``None`` if unknown.

    The transport is chosen per provider by ``settings.backend_for(name)``:
    ``"cli"`` drives the vendor's subscription-authenticated coding-agent CLI,
    ``"api"`` uses the billed HTTP SDK. Mixing is supported, so a provider
    whose CLI is not installed can fall back to an API key without forcing the
    whole run onto billed transport -- but only when the operator asks for it
    in so many words. A missing CLI reports itself unavailable rather than
    silently moving that provider onto billed transport, because the whole
    point of the default is that a run costs nothing per token.

    ``allow_writes`` reaches the CLI backends only. It is off by default and
    should stay off for anything that is reviewing rather than building: a
    coding agent asked merely to critique will edit the working tree, and
    several of them at once is write-thrash. The API backends have no file
    access to grant.

    ``resolve_family``: the bare path (``build_providers``, the four-phase
    pipeline) binds the provider to the configured tier's family, resolved
    through quadratus.latest once the installed client is known. A Fleet
    passes False: it binds each seat's own family per call, and the
    vendor's transport availability must not depend on whether the
    settings' default family resolves (root's isolation control on
    aab4fe7: a catalogue without Sol took Luna and Astra down with it).
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        log.warning("Unknown provider %r; skipping.", name)
        return None
    cls, key_attr, model_attr = entry

    backend = settings.backend_for(name)
    if backend == "cli":
        # Imported lazily: cli_providers depends on this module.
        from .cli_providers import cli_provider_classes

        cli_cls = cli_provider_classes().get(name)
        if cli_cls is None:
            log.warning("No CLI backend for %r; falling back to the API backend.", name)
        else:
            provider = cli_cls(
                model=settings.model_for(name),
                allow_writes=allow_writes,
                workdir=workdir,
                max_tokens=settings.max_tokens,
                timeout=settings.cli_timeout,
                max_retries=settings.max_retries,
                retry_base_delay=settings.retry_base_delay,
                refusal_fallback_model=settings.refusal_fallback_for(name),
            )
            if resolve_family and provider.available():
                # After construction, so the installed client's version is
                # known to the catalogue check (minimal_client_version).
                wire, unavailable = _cli_wire_model(name, settings,
                                                    installed_client=getattr(provider, "client_version", None))
                if unavailable:
                    # A family the installed client cannot resolve: the
                    # provider is kept so the status line can say why, and
                    # makes no call. The transport itself is untouched.
                    provider._client = None
                    provider._init_error = unavailable
                else:
                    provider.model = wire
            return provider

    return cls(
        model=getattr(settings, model_attr),
        api_key=getattr(settings, key_attr),
        max_tokens=settings.max_tokens,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
        refusal_fallback_model=settings.refusal_fallback_for(name),
    )


def _cli_wire_model(name: str, settings, *, installed_client=None):
    """``(wire model, why unavailable)`` for the bare CLI path: the
    configured tier name is a family (``sol``, ``opus``) or the vendor
    default (``""``), resolved through quadratus.latest exactly as a Fleet
    seat is, so no path hands a family name to a client as a model id
    (root's factory control on bcd2b52). An explicit operator override
    stays visible through the same resolution."""
    alias = settings.model_for(name)
    if not alias:
        return alias, None
    from .latest import resolve_model  # lazy: latest imports this module
    found = resolve_model(f"{name}:{alias}", installed_client=installed_client)
    if found.available:
        return found.wire or "", None
    return alias, f"{name}:{alias} is not usable: {found.reason}"


def build_providers(settings) -> List[LLMProvider]:
    """Build all providers in the configured order (unknown names skipped)."""
    providers = []
    for name in settings.provider_order:
        p = build_provider(name, settings)
        if p is not None:
            providers.append(p)
    return providers
