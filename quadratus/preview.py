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

__all__ = ["CaptureProfile", "PreviewFailed", "load_profile", "profile_from_dict", "running",
           "capture_task", "capture_argv", "validate_capture"]

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


class PreviewFailed(RuntimeError):
    """The preview or the capture did not complete; the message says which."""


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
    first = argv[0]
    # A conventional interpreter may live outside the project (its absolute
    # path, by basename); any other executable is a project file.
    if not (_RUNNERS.fullmatch(first) or (Path(first).is_absolute() and _RUNNERS.fullmatch(Path(first).name))):
        problem = _path_problem(first, root)
        if problem or not (root / first).is_file():
            raise ValueError("capture profile preview must start with a known runner or a project file")
    for argument in argv[1:]:
        value = argument.split("=", 1)[-1] if argument.startswith("-") else argument
        if "/" in value or value.startswith("."):
            problem = _path_problem(value, root)
            if problem:
                raise ValueError(f"capture profile preview argument {argument[:60]!r} is {problem}")
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


def _stop(proc: subprocess.Popen) -> None:
    """SIGINT, SIGTERM, then SIGKILL to the preview's own process group, then
    reap. SIGINT first so an app whose cleanup runs on interrupt gets to run
    it; that is a chance, not proof, that the app removed what it made."""
    for sig, wait in ((signal.SIGINT, SHUTDOWN_STEP_SECONDS), (signal.SIGTERM, SHUTDOWN_STEP_SECONDS),
                      (signal.SIGKILL, SHUTDOWN_STEP_SECONDS)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            break
        try:
            proc.wait(timeout=wait)
            # The leader is gone; signal the group once more so a child that
            # outlived it does not keep the port.
            continue
        except subprocess.TimeoutExpired:
            continue
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


@contextmanager
def running(profile: CaptureProfile, root, deadline: Optional[float] = None):
    """Start the preview, wait until it answers, yield, and always stop it.

    Refused before launch when something already listens on the port, so a
    stray service can never stand in for this preview. Readiness needs the
    preview still alive at each poll for the same reason.
    """
    root = Path(root)
    if _listening(profile.host, profile.port):
        raise PreviewFailed(f"something is already listening on {profile.origin}; the preview was not started")
    log = tempfile.TemporaryFile()
    ready_url = profile.origin + profile.ready_path
    tempdir = tempfile.mkdtemp(prefix="quadratus-preview-pyc-")
    env = dict(_environment(), **dict(profile.env), PYTHONPYCACHEPREFIX=tempdir)
    deadline = deadline if deadline is not None else time.monotonic() + profile.total_timeout
    try:
        proc = subprocess.Popen(list(profile.preview), cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        log.close()
        import shutil
        shutil.rmtree(tempdir, ignore_errors=True)
        raise PreviewFailed(f"the preview could not start: {exc}") from None
    try:
        ready_by = min(deadline, time.monotonic() + profile.ready_timeout)
        while True:
            if proc.poll() is not None:
                raise PreviewFailed(f"the preview exited with {proc.returncode} before it was ready: "
                                    + _tail(log))
            try:
                with urllib.request.urlopen(ready_url, timeout=2) as response:
                    if response.status == profile.ready_status:
                        break
            except urllib.error.HTTPError as exc:
                if exc.code == profile.ready_status:
                    break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > ready_by:
                raise PreviewFailed(f"the preview was not ready at {ready_url} within "
                                    f"{profile.ready_timeout:g}s: " + _tail(log))
            time.sleep(0.2)
        if proc.poll() is not None:
            raise PreviewFailed(f"the preview exited with {proc.returncode} as it became ready")
        yield proc
    finally:
        _stop(proc)
        log.close()
        import shutil
        shutil.rmtree(tempdir, ignore_errors=True)


def _tail(log) -> str:
    try:
        log.seek(0)
        return log.read().decode("utf-8", "replace")[-_LOG_TAIL:].strip() or "(no output)"
    except (OSError, ValueError):
        return "(output unavailable)"


def capture_argv(profile: CaptureProfile, task_id: str, capture: dict) -> List[str]:
    """The harness's own capture command for a task's declared capture."""
    target = profile.origin + _web_path(capture.get("path", "/"), "capture path")
    argv = [sys.executable, "-m", "quadratus.design_evidence", target, task_id, ".", "--pinned"]
    for step in capture.get("steps") or []:
        if step.get("action") == "file":
            argv += ["--upload", step["selector"], step["path"]]
        else:
            argv += [f"--{step['action']}", step["selector"]]
    return argv


def capture_task(profile: CaptureProfile, root, task_id: str, capture: dict) -> str:
    """Preview, capture the task's declared state, stop. Returns "" on success
    or why it failed; never raises for a preview or capture failure."""
    root = Path(root)
    argv = capture_argv(profile, task_id, capture)
    package = str(Path(__file__).resolve().parent.parent)
    deadline = time.monotonic() + profile.total_timeout
    try:
        with running(profile, root, deadline):
            env = dict(_environment())
            env["PYTHONPATH"] = package + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            # What is left of the one budget, never a fresh full timeout.
            left = min(profile.capture_timeout, deadline - time.monotonic())
            if left <= 0:
                return f"the preview used the whole {profile.total_timeout:g}s budget before the capture"
            try:
                done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True,
                                      timeout=left, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return f"the capture did not finish within {left:.0f}s of the {profile.total_timeout:g}s budget"
            if done.returncode != 0:
                return (f"the capture exited with {done.returncode}: "
                        + ((done.stderr or "") + (done.stdout or "")).strip()[-400:])
    except PreviewFailed as exc:
        return str(exc)
    return ""


def validate_capture(capture) -> dict:
    """A task's SCOPE capture block, checked syntactically before any model
    call: a path on the origin and well-formed steps. Fixtures are resolved
    and hashed by the capture itself, since a repair may write its fixture."""
    if not isinstance(capture, dict) or set(capture) - {"path", "steps"}:
        raise ValueError('SCOPE capture must be {"path": "/...", "steps": [...]}')
    path = _web_path(capture.get("path", "/"), "capture path")
    steps = capture.get("steps") or []
    if not isinstance(steps, list) or len(steps) > 12:
        raise ValueError("SCOPE capture steps must be a list of at most 12 steps")
    out = []
    for step in steps:
        if (not isinstance(step, dict) or step.get("action") not in ("click", "wait", "file")
                or not isinstance(step.get("selector"), str) or not step["selector"].strip()
                or len(step["selector"]) > 300 or set(step) - {"action", "selector", "path"}
                or (step["action"] == "file") != isinstance(step.get("path"), str)):
            raise ValueError("SCOPE capture steps need action click, wait or file, a selector, "
                             "and a path for file steps only")
        out.append({k: (v.strip() if k == "selector" else v) for k, v in step.items()})
    return dict(path=path, steps=out)
