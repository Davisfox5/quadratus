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
    unknown = set(data) - {"preview", "origin", "ready_path", "ready_timeout", "capture_timeout"}
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
    return CaptureProfile(
        preview=tuple(argv), origin=origin,
        ready_path=_web_path(data.get("ready_path", "/"), "ready_path"),
        ready_timeout=_timeout(data.get("ready_timeout", 30), "ready_timeout"),
        capture_timeout=_timeout(data.get("capture_timeout", 120), "capture_timeout"))


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
    """SIGTERM then SIGKILL to the preview's own process group, then reap."""
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
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
def running(profile: CaptureProfile, root):
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
    env = dict(_environment(), PYTHONPYCACHEPREFIX=tempdir)
    try:
        proc = subprocess.Popen(list(profile.preview), cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        log.close()
        import shutil
        shutil.rmtree(tempdir, ignore_errors=True)
        raise PreviewFailed(f"the preview could not start: {exc}") from None
    try:
        deadline = time.monotonic() + profile.ready_timeout
        while True:
            if proc.poll() is not None:
                raise PreviewFailed(f"the preview exited with {proc.returncode} before it was ready: "
                                    + _tail(log))
            try:
                with urllib.request.urlopen(ready_url, timeout=2) as response:
                    if response.status < 500:
                        break
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > deadline:
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
    argv = [sys.executable, "-m", "quadratus.design_evidence", target, task_id, "."]
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
    try:
        with running(profile, root):
            env = dict(_environment())
            env["PYTHONPATH"] = package + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            try:
                done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True,
                                      timeout=profile.capture_timeout, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return f"the capture did not finish within {profile.capture_timeout:g}s"
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
