"""Harness-owned preview and capture from an operator-declared profile.

Codex, Run 18 (2026-09-27): every UI lead was told to start the app and run
the capture command, but a Claude editing lead may run only the exact
commands it is granted, and neither a server start nor a capture with a
chosen URL can be one. Both repair leads spent their whole turn cap on
denied commands and no evidence was taken. The capability belongs to the
harness, not to a seat: the operator declares, before the run, how the
project's preview starts and where it listens, and the harness starts it,
captures the task's declared page, and stops it. No model is granted
anything, and nothing a model was denied is replayed.

What is trusted is the operator's profile, validated here before any model
call: an argv with no shell syntax whose paths stay inside the project, a
loopback origin with a fixed port, and bounded timeouts. The preview runs in
its own process group, which is the only thing ever signalled; a grandchild
that leaves the group (``setsid``) is outside what this controls. There is
no network isolation beyond the sandbox the harness already runs in.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

__all__ = ["CaptureFailure", "CaptureProfile", "ENVIRONMENT", "PreviewFailed", "load_profile",
           "profile_from_dict", "running", "capture_task", "capture_argv", "validate_capture"]

#: Runners an operator may name by bare name; anything else is a project file.
_RUNNERS = re.compile(r"python(?:\d+(?:\.\d+)*)?|node|npm|npx|uv|flask|pnpm|yarn")
_SHELL = re.compile(r"[;&|<>`$\n\r\\]")
_ORIGIN = re.compile(r"http://(127\.0\.0\.1|localhost):(\d{4,5})")
_MAX_TIMEOUT = 600
#: SIGINT, then SIGTERM, then SIGKILL, each given this long to work.
SHUTDOWN_STEP_SECONDS = 4
SHUTDOWN_SECONDS = 3 * SHUTDOWN_STEP_SECONDS
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
#: Names an operator profile may not set: what changes how programs load or
#: where they look, and anything that reads as a credential.
_ENV_RESERVED = re.compile(r"PATH|HOME|SHELL|USER|TMPDIR|.*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH"
                           r"|COOKIE|SESSION).*|(?:LD|DYLD|PYTHON|NODE|NPM|UV|PIP|GIT|SSH|AWS|GCP|AZURE)_.*"
                           r"|PYTHON.*|NODE.*")
#: How much preview output a failure carries back.
_LOG_TAIL = 2_000


class _Refuse(urllib.request.HTTPRedirectHandler):
    """Readiness is the declared status at the declared path: a redirect,
    even to the same origin, is an answer, not readiness."""

    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECTS = urllib.request.build_opener(_Refuse)


class _BoundedLog:
    """The preview's combined output, read as it is written and kept only
    as a bounded tail, so a chatty preview cannot grow a file without limit
    (Codex review of 3a55d82)."""

    KEEP = 8_192

    def __init__(self, stream):
        import threading
        self._stream, self._buffer = stream, bytearray()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            for chunk in iter(lambda: self._stream.read1(4096) if hasattr(self._stream, "read1")
                              else self._stream.read(4096), b""):
                self._buffer += chunk
                if len(self._buffer) > self.KEEP:
                    del self._buffer[:-self.KEEP]
        except (OSError, ValueError):
            pass

    def tail(self) -> str:
        return bytes(self._buffer).decode("utf-8", "replace")[-_LOG_TAIL:].strip() or "(no output)"

    def close(self):
        """Never blocks on a pipe a surviving descendant still holds: the
        reader is given a bounded wait, and the stream is closed only once
        the reader has finished (closing under a blocked read would wait on
        its lock). Otherwise the daemon reader is left to end at EOF."""
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            return
        try:
            self._stream.close()
        except (OSError, ValueError):
            pass


#: A failure the harness can prove came from the environment, not the
#: project: the preview was never launched, or its launch itself failed.
ENVIRONMENT = "environment"


class PreviewFailed(RuntimeError):
    """The preview or the capture did not complete; the message says which.

    ``origin`` is ``ENVIRONMENT`` only where a structured fact proves it
    (map E1; Codex, 5862699144); None means unattributed, never "product"."""

    def __init__(self, message: str, origin: Optional[str] = None):
        super().__init__(message)
        self.origin = origin


class CaptureFailure(str):
    """Why a harness capture failed, as the same text as before, carrying
    ``origin`` from the failure that produced it (None: unattributed)."""

    origin: Optional[str] = None

    def __new__(cls, text: str, origin: Optional[str] = None):
        value = super().__new__(cls, text)
        value.origin = origin
        return value


@dataclass(frozen=True)
class CaptureProfile:
    """How the operator's project is previewed. See :func:`profile_from_dict`."""

    preview: Tuple[str, ...]
    origin: str
    ready_path: str = "/"
    ready_timeout: float = 30
    capture_timeout: float = 120
    #: The status the readiness path must answer with; a 404 is not ready.
    ready_status: int = 200
    #: One budget for startup, readiness and capture together; each phase
    #: gets only what is left of it. Shutdown is bounded separately
    #: (``SHUTDOWN_SECONDS``).
    total_timeout: float = 150
    #: Operator-declared environment for the preview only; see ``_env``.
    env: Tuple[Tuple[str, str], ...] = ()

    @property
    def port(self) -> int:
        return int(_ORIGIN.fullmatch(self.origin).group(2))

    @property
    def host(self) -> str:
        return _ORIGIN.fullmatch(self.origin).group(1)


def _path_problem(value: str, root: Path) -> Optional[str]:
    """Why an argument that names a path may not be used; None if it may."""
    raw = PurePosixPath(value)
    if raw.is_absolute() or ".." in raw.parts:
        return "a path outside the project"
    current = root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            return "a path through a symlink"
    if not current.resolve().is_relative_to(root.resolve()):
        return "a path outside the project"
    return None


def _web_path(value, name: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or "//" in value
            or ".." in PurePosixPath(value.split("?", 1)[0]).parts or ":" in value.split("?", 1)[0]
            or _SHELL.search(value) or any(ord(c) < 33 for c in value)):
        raise ValueError(f"capture profile {name} must be a path on the origin, starting with /")
    return value


def _timeout(value, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 1 <= value <= _MAX_TIMEOUT):
        raise ValueError(f"capture profile {name} must be between 1 and {_MAX_TIMEOUT} seconds")
    return float(value)


def _project_relative(argument: str) -> str:
    """A preview argument with ``{project}`` written the way ``env`` accepts
    it, made project-relative: the preview runs in the project, so
    ``{project}/x`` is ``x`` and ``{project}`` alone is ``.``. Any other
    ``{name}`` is refused, because nothing substitutes it and it would reach
    the command literally (UI diagnostic lane on 4a273a3)."""
    head, sep, value = argument.partition("=") if argument.startswith("-") else ("", "", argument)
    if value == "{project}":
        value = "."
    elif value.startswith("{project}/"):
        value = value[len("{project}/"):] or "."
    if re.search(r"\{[^{}]*\}", head + value):
        raise ValueError(f"capture profile preview argument {argument[:60]!r} has a placeholder nothing "
                         "substitutes; write {project} or {project}/relative, or a project-relative path")
    return head + sep + value


def profile_from_dict(data, root) -> CaptureProfile:
    """Validate an operator profile against the selected project ``root``.

    Raises ``ValueError`` naming the first field that may not be used.
    """
    root = Path(root)
    if not isinstance(data, dict):
        raise ValueError("capture profile must be a JSON object")
    unknown = set(data) - {"preview", "origin", "ready_path", "ready_timeout", "capture_timeout",
                           "ready_status", "total_timeout", "env"}
    if unknown:
        raise ValueError(f"capture profile has unknown fields: {', '.join(sorted(unknown))}")
    argv = data.get("preview")
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(a, str) or not a or _SHELL.search(a) for a in argv)):
        raise ValueError("capture profile preview must be an argv list with no shell syntax")
    argv = [_project_relative(a) for a in argv]
    first = argv[0]
    # A conventional interpreter may live outside the project (its absolute
    # path, by basename); any other executable is a project file.
    if not (_RUNNERS.fullmatch(first) or (Path(first).is_absolute() and _RUNNERS.fullmatch(Path(first).name))):
        problem = _path_problem(first, root)
        if problem or not (root / first).is_file():
            raise ValueError("capture profile preview must start with a known runner or a project file")
    for argument in argv[1:]:
        # Conservative, since an argument's meaning cannot be inferred: any
        # value that is absolute, climbs, or names something that exists in
        # the project (a symlink included) must be a clean project path
        # (Codex review of 3a55d82: --config=<symlink to outside> passed).
        value = argument.split("=", 1)[-1] if argument.startswith("-") else argument
        if not value:
            continue
        raw = PurePosixPath(value)
        if (raw.is_absolute() or ".." in raw.parts or "/" in value or value.startswith(".")
                or os.path.lexists(root / value)):
            problem = _path_problem(value, root)
            if problem:
                raise ValueError(f"capture profile preview argument {argument[:60]!r} is {problem}")
    script = argv[1] if len(argv) > 1 and re.fullmatch(r"python[\d.]*|node", Path(first).name) else None
    if script is not None and not script.startswith("-") and (
            "/" in script or PurePosixPath(script).suffix in (".py", ".js", ".mjs", ".cjs", ".ts")):
        # The script an interpreter is told to run must be in the project
        # now: a wrong path otherwise surfaces only when the harness first
        # previews, after every editing and review call of the task has been
        # spent (UI diagnostic lane on 4a273a3: 1.48M tokens, then exit 2).
        if not (root / script).is_file():
            raise ValueError(f"capture profile preview script {script[:80]!r} is not a file in the project")
    origin = data.get("origin")
    match = _ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
    if not match or not 1024 <= int(match.group(2)) <= 65535:
        raise ValueError("capture profile origin must be http://127.0.0.1:<port> or http://localhost:<port>")
    ready_timeout = _timeout(data.get("ready_timeout", 30), "ready_timeout")
    capture_timeout = _timeout(data.get("capture_timeout", 120), "capture_timeout")
    total = data.get("total_timeout", ready_timeout + capture_timeout)
    if (isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total)
            or not 1 <= total <= 2 * _MAX_TIMEOUT):
        raise ValueError(f"capture profile total_timeout must be between 1 and {2 * _MAX_TIMEOUT} seconds")
    status = data.get("ready_status", 200)
    if type(status) is not int or not 200 <= status <= 299:
        raise ValueError("capture profile ready_status must be a 2xx status")
    return CaptureProfile(
        preview=tuple(argv), origin=origin,
        ready_path=_web_path(data.get("ready_path", "/"), "ready_path"),
        ready_timeout=ready_timeout, capture_timeout=capture_timeout, ready_status=status,
        total_timeout=float(total), env=_env(data.get("env", {}), root))


def _env(values, root: Path) -> Tuple[Tuple[str, str], ...]:
    """The operator's preview environment, checked: upper-case names that do
    not change how programs load and do not read as credentials; plain
    string values, where a path is written ``{project}`` or
    ``{project}/relative`` and must stay inside the project. No model ever
    chooses any of it (Codex review of contract v2)."""
    if not isinstance(values, dict) or len(values) > 16:
        raise ValueError("capture profile env must be an object of at most 16 names")
    out = []
    for name, value in values.items():
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name) or _ENV_RESERVED.fullmatch(name):
            raise ValueError(f"capture profile env name {str(name)[:40]!r} may not be set")
        if (not isinstance(value, str) or len(value) > 512 or _SHELL.search(value)
                or any(ord(c) < 32 for c in value)):
            raise ValueError(f"capture profile env {name} must be a plain string")
        if value == "{project}" or value.startswith("{project}/"):
            relative = value[len("{project}/"):] if value != "{project}" else "."
            problem = _path_problem(relative, root) if relative != "." else None
            if problem:
                raise ValueError(f"capture profile env {name} is {problem}")
            value = str((root / relative).resolve()) if relative != "." else str(root.resolve())
        elif "/" in value or value.startswith(("~", ".")) or "{" in value:
            raise ValueError(f"capture profile env {name}: write a path as {{project}}/relative")
        out.append((name, value))
    return tuple(sorted(out))


def load_profile(path, root) -> CaptureProfile:
    """Read and validate a profile file (operator input, before any model call)."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"capture profile could not be read: {type(exc).__name__}") from None
    return profile_from_dict(data, root)


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _environment() -> dict:
    """The gate's environment rule: no stale bytecode, nothing else added."""
    from .integration import _fresh_bytecode_env
    with _fresh_bytecode_env() as env:
        return {k: v for k, v in env.items() if k != "PYTHONPYCACHEPREFIX"}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _stop(proc: subprocess.Popen) -> None:
    """SIGINT, SIGTERM, then SIGKILL to the process group this module
    created, each followed by a wait until the whole group, not only its
    leader, is gone, then reap. SIGINT first so an app whose cleanup runs
    on interrupt gets to run it; that is a chance, not proof, that the app
    removed what it made. Safe to call after the leader has already exited:
    a descendant still in the group is stopped too (Codex review of d731499)."""
    pgid = proc.pid
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except OSError:
            break
        end = time.monotonic() + SHUTDOWN_STEP_SECONDS
        while time.monotonic() < end and (proc.poll() is None or _group_alive(pgid)):
            time.sleep(0.05)
        if proc.poll() is not None and not _group_alive(pgid):
            break
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _outside_runner(first: str, root: Path) -> bool:
    """Whether ``first`` names a conventional runner outside the project.
    Total: a path that cannot be resolved (a symlink loop) is not proven
    outside, so it stays unattributed (Sol review, 5863184501)."""
    if _RUNNERS.fullmatch(first):
        return True
    path = Path(first)
    if not (path.is_absolute() and _RUNNERS.fullmatch(path.name)):
        return False
    try:
        resolved, project = path.resolve(), root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(project)
    except ValueError:
        return True
    return False


@contextmanager
def running(profile: CaptureProfile, root, deadline: Optional[float] = None):
    """Start the preview, wait until it answers, yield, and always stop it.

    Refused before launch when something already listens on the port, so a
    stray service can never stand in for this preview. Readiness needs the
    preview still alive at each poll for the same reason.
    """
    root = Path(root)
    if _listening(profile.host, profile.port):
        raise PreviewFailed(f"something is already listening on {profile.origin}; the preview was not started",
                            origin=ENVIRONMENT)
    ready_url = profile.origin + profile.ready_path
    tempdir = tempfile.mkdtemp(prefix="quadratus-preview-pyc-")
    env = dict(_environment(), **dict(profile.env), PYTHONPYCACHEPREFIX=tempdir)
    deadline = deadline if deadline is not None else time.monotonic() + profile.total_timeout
    try:
        proc = subprocess.Popen(list(profile.preview), cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        import shutil
        shutil.rmtree(tempdir, ignore_errors=True)
        # A conventional runner that cannot launch is the environment; a
        # project file that cannot (missing, not executable) may be the
        # project's own doing, so it is left unattributed. A runner is what
        # profile validation accepts as one: a bare name, or an absolute
        # path outside the project (Sol review, 5862984388: ./python3 is a
        # project file).
        first = profile.preview[0]
        raise PreviewFailed(f"the preview could not start: {exc}",
                            origin=ENVIRONMENT if _outside_runner(first, root) else None) from None
    log = _BoundedLog(proc.stdout)
    try:
        ready_by = min(deadline, time.monotonic() + profile.ready_timeout)
        while True:
            if proc.poll() is not None:
                raise PreviewFailed(f"the preview exited with {proc.returncode} before it was ready: "
                                    + log.tail())
            try:
                with _NO_REDIRECTS.open(ready_url, timeout=2) as response:
                    if response.status == profile.ready_status:
                        break
            except urllib.error.HTTPError as exc:
                if exc.code == profile.ready_status:
                    break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > ready_by:
                raise PreviewFailed(f"the preview was not ready at {ready_url} within "
                                    f"{profile.ready_timeout:g}s: " + log.tail())
            time.sleep(0.2)
        if proc.poll() is not None:
            raise PreviewFailed(f"the preview exited with {proc.returncode} as it became ready")
        yield proc
    finally:
        _stop(proc)
        log.close()
        import shutil
        shutil.rmtree(tempdir, ignore_errors=True)


#: How a harness measurement that could not be read is written on the
#: capture command: present, and never a match.
MISSING_DIGEST = "missing"


def capture_argv(profile: CaptureProfile, task_id: str, capture: dict, view: Optional[str] = None,
                 attempt: Optional[str] = None, measured: Optional[dict] = None) -> List[str]:
    """The harness's own capture command for a task's declared capture;
    ``view`` renders that one width only, under the capture's ``attempt``
    token so the views of one attempt combine and nothing older does, and
    ``measured`` hands the capture the harness's receipt for the views
    already taken, so a sibling is reused only when it still carries
    exactly those digests."""
    target = profile.origin + _web_path(capture.get("path", "/"), "capture path")
    # -P and a working directory outside the project: a project folder named
    # quadratus can never stand in for the harness's own capture module
    # (Codex review of 3a55d82); the project is passed as an absolute root.
    isolate = ["-P"] if sys.version_info >= (3, 11) else []
    argv = [sys.executable, *isolate, "-m", "quadratus.design_evidence", target, task_id, "{root}", "--pinned"]
    for step in capture.get("steps") or []:
        if step.get("action") == "file":
            argv += ["--upload", step["selector"], step["path"]]
        elif step.get("action") == "confirm":
            argv += ["--confirm", step["selector"], step["message"]]
        else:
            argv += [f"--{step['action']}", step["selector"]]
    for name, files in sorted((measured or {}).items()):
        for leaf, digest in sorted((files or {}).items()):
            # A digest that could not be read travels as "missing", never as
            # nothing: dropping it would turn a held measurement back into
            # self-capture reuse (Codex review of 4a273a3).
            argv += ["--measured", name, leaf, digest if isinstance(digest, str) else MISSING_DIGEST]
    if view:
        argv += ["--view", view]
    if attempt:
        argv += ["--attempt", attempt]
    return argv


def capture_task(profile: CaptureProfile, root, task_id: str, capture: dict,
                 receipt: Optional[dict] = None) -> str:
    """Preview, capture the task's declared state, stop. Returns "" on success
    or why it failed; never raises for a preview or capture failure.

    ``receipt``, when given, is filled with each view's file digests as the
    harness measures them right after that view's capture process ends
    (``design_evidence.view_receipt``): a record kept outside the project,
    for ``check_records(receipt=...)`` to hold the summary to.

    A declaration that changes the preview's state (``mutates_preview``) is
    captured one view per preview: the preview is started, the view
    rendered and the preview stopped, then again for the next view, so the
    second view meets the profile's own seed and not the state the first
    view left (Codex, 6038178890). Whether the profile reseeds on start is
    the profile's property; a view whose final wait was already satisfied
    before its steps is caught by the evidence check as a declaration
    problem, never passed off as proof."""
    root = Path(root)
    deadline = time.monotonic() + profile.total_timeout
    if mutates_preview(capture):
        import secrets

        from .design_evidence import VIEWPORTS, view_receipt
        attempt = secrets.token_hex(8)
        # One capture allowance for the whole attempt, spent across the
        # views, beside the one total deadline (Codex review of 4a51291:
        # each view had been granted the full allowance again).
        allowance = float(profile.capture_timeout)
        taken: dict = {}
        for view in VIEWPORTS:
            # The views already measured travel with the next capture, so a
            # sibling is reused only against the harness's own receipt.
            failure, spent = _capture_once(profile, root,
                                           capture_argv(profile, task_id, capture, view, attempt, measured=taken),
                                           deadline, allowance)
            if failure:
                return failure
            taken[view] = view_receipt(root, task_id, view)
            if receipt is not None:
                receipt[view] = taken[view]
            unread = sorted(leaf for leaf, digest in taken[view].items() if not isinstance(digest, str))
            if unread:
                # A view the harness cannot measure is not a view the next
                # one may be reused beside: the attempt stops unverified.
                return (f"the {view} capture left {', '.join(unread)} unreadable, so the harness could not "
                        "measure it; the attempt stops before the next view")
            allowance -= spent
        return ""
    failure, _ = _capture_once(profile, root, capture_argv(profile, task_id, capture), deadline,
                               float(profile.capture_timeout))
    if not failure and receipt is not None:
        from .design_evidence import VIEWPORTS, view_receipt
        for view in VIEWPORTS:
            receipt[view] = view_receipt(root, task_id, view)
    return failure


def _capture_once(profile: CaptureProfile, root: Path, argv: List[str], deadline: float, allowance: float):
    """One preview around one capture command: ``(failure, seconds the
    capture itself took)``, the capture bounded by ``allowance`` and by
    what is left of the deadline."""
    package = str(Path(__file__).resolve().parent.parent)
    spent = 0.0
    try:
        with running(profile, root, deadline):
            env = dict(_environment())
            env["PYTHONPATH"] = package + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            # What is left of the one budget, never a fresh full timeout.
            left = min(allowance, deadline - time.monotonic())
            if left <= 0:
                return (f"the preview used the whole {profile.total_timeout:g}s budget before the capture"
                        if allowance > 0 else
                        f"the {profile.capture_timeout:g}s capture allowance was spent on an earlier view"), spent
            argv = [str(root.resolve()) if a == "{root}" else a for a in argv]
            started = time.monotonic()
            capture = subprocess.Popen(argv, cwd=package, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
            output = _BoundedLog(capture.stdout)
            try:
                try:
                    capture.wait(timeout=left)
                except subprocess.TimeoutExpired:
                    spent = time.monotonic() - started
                    return (f"the capture did not finish within {left:.0f}s of the {profile.total_timeout:g}s "
                            f"budget"), spent
                spent = time.monotonic() - started
                if capture.returncode != 0:
                    return f"the capture exited with {capture.returncode}: " + output.tail()[-400:], spent
            finally:
                # Whatever happened, the capture and everything it started
                # (the browser, a child holding its output) are stopped.
                _stop(capture)
                output.close()
    except PreviewFailed as exc:
        return CaptureFailure(str(exc), exc.origin), spent
    return "", spent


def validate_capture(capture) -> dict:
    """A task's SCOPE capture block, checked syntactically before any model
    call: a path on the origin and well-formed steps. Fixtures are resolved
    and hashed by the capture itself, since a repair may write its fixture."""
    if not isinstance(capture, dict) or set(capture) - {"path", "steps"}:
        raise ValueError('SCOPE capture must be {"path": "/...", "steps": [...]}')
    path = _web_path(capture.get("path", "/"), "capture path")
    steps = capture.get("steps") or []
    from .design_evidence import MAX_SELECTOR_CHARS, MAX_STEPS
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError(f"SCOPE capture steps must be a list of at most {MAX_STEPS} steps")
    out = []
    for step in steps:
        if (not isinstance(step, dict) or step.get("action") not in ("click", "wait", "file", "confirm")
                or not isinstance(step.get("selector"), str) or not step["selector"].strip()
                or len(step["selector"]) > MAX_SELECTOR_CHARS or set(step) - {"action", "selector", "path", "message"}
                or (step["action"] == "file") != isinstance(step.get("path"), str)
                or (step["action"] == "confirm") != isinstance(step.get("message"), str)
                or (step["action"] == "confirm" and (not step["message"].strip()
                                                     or len(step["message"]) > MAX_SELECTOR_CHARS))):
            raise ValueError("SCOPE capture steps need action click, wait, confirm or file, a selector, "
                             "a path for file steps only and a message for confirm steps only")
        if step["action"] == "file":
            # Syntax and containment now; which task owns a fixture is checked
            # at dispatch, existence and hash by the capture (a repair may
            # write its fixture). Codex review of 3a55d82: an absolute path
            # reached a started preview before it was refused. A committed,
            # non-hidden project sample is as valid as a capture-only fixture
            # (design_evidence._fixture holds the same rule); the phase-4
            # rerun on a6c9576 stalled here on tests/fixtures/<sample>.csv.
            # A separate name: ``path`` is the page route this function returns
            # (Codex review of 0115f0c caught it being overwritten here).
            upload = step["path"]
            parts = PurePosixPath(upload).parts
            fixture = len(parts) == 4 and parts[:2] == (".quadratus", "capture-fixtures")
            hidden = any(part.startswith(".") for part in (parts[3:] if fixture else parts))
            if (not parts or PurePosixPath(upload).is_absolute() or ".." in parts or hidden
                    or any(ord(c) < 32 for c in upload)):
                raise ValueError("SCOPE capture file steps must name a non-hidden project file or "
                                 ".quadratus/capture-fixtures/<task>/<name>")
        out.append({k: (v.strip() if k in ("selector", "message") else v) for k, v in step.items()})
    return dict(path=path, steps=out)


def mutates_preview(capture: dict) -> bool:
    """Whether a declaration changes the preview's state (a confirm step):
    such a capture gets one preview per view, so the second view starts
    from the profile's own seed and not from what the first view did."""
    return any(s.get("action") == "confirm" for s in (capture.get("steps") or []))
