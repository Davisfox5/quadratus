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
from dataclasses import dataclass, replace
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
    #: The operator's escape for a page that needs shared workers. A pinned
    #: capture runs without them, because their requests cannot be held to
    #: the preview origin (``browser.render_page``); with this they run,
    #: unpinned, and the render records that they did.
    shared_workers: bool = False
    #: The operator's word that this preview is served by a process outside
    #: the one the run starts (a container's port forwarder, a server the
    #: command hands off to). Without it such a listener stops the capture
    #: (``running``); with it the listener is recorded and the capture goes on.
    outside_listener: bool = False

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


#: Python options that take no value, as single letters (combinable: -uB).
_PY_FLAGS = set("bBdEIOPqRsSux")
#: Node options whose value is the next argument.
_NODE_VALUED = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions",
                "--input-type", "--title", "--env-file", "--redirect-warnings", "--icu-data-dir"}
#: Node long options that take no value. Any other long option is refused:
#: an option the grammar does not know may take a value, and then the
#: script it would name is a guess (Codex review of 0a99ee2..6cf72f4).
_NODE_FLAGS = {"--enable-source-maps", "--no-warnings", "--no-deprecation", "--trace-warnings",
               "--trace-deprecation", "--trace-uncaught", "--throw-deprecation", "--pending-deprecation",
               "--preserve-symlinks", "--preserve-symlinks-main", "--abort-on-uncaught-exception",
               "--experimental-vm-modules", "--no-experimental-fetch", "--inspect", "--inspect-brk",
               "--frozen-intrinsics", "--disable-proto=delete", "--no-addons", "--no-global-search-paths"}
#: Node long options accepted only in their ``--option=value`` form.
_NODE_EQUALS = {"--inspect", "--inspect-brk", "--inspect-port", "--max-old-space-size", "--stack-size",
                "--unhandled-rejections", "--disable-proto", "--dns-result-order", "--experimental-specifier-resolution"}
#: Options that print or open a prompt and exit: never a preview.
_NOT_A_SERVER = {"-h", "--help", "-V", "-v", "--version", "-i", "--interactive", "-c", "--check", "-"}


def _operand(option: str, value: str, name: str) -> None:
    """``-m``, ``-c``, ``-e`` and ``-p`` run what follows them; with nothing
    there the interpreter exits with a usage error instead of serving."""
    if not value.strip() or (option == "-m" and value.startswith("-")):
        what = "a module name" if option == "-m" else "code to run"
        raise ValueError(f"capture profile preview runs {name} {option} without {what}; "
                         f"give it one, or name the project file it serves")


def _interpreter_script(argv, root: Optional[Path] = None) -> Optional[str]:
    """The script file a ``python`` or ``node`` preview command runs, found
    through a bounded option grammar; None when the command runs a module or
    inline code (``python -m``/``-c``, ``node -e``/``-p``) or is not one of
    those interpreters. Raises ValueError for a form the grammar does not
    know, since the harness cannot say what it would run."""
    name = Path(argv[0]).name
    is_python = re.fullmatch(r"python[\d.]*", name) is not None
    if not is_python and name != "node":
        return None
    if argv[0] != name:
        # A path, not a bare interpreter name: a project file that is merely
        # named like one (./python3, <project>/python3) is the program itself.
        first = Path(argv[0])
        if not first.is_absolute():
            return None
        if root is not None:
            try:
                if first.resolve().is_relative_to(Path(root).resolve()) or first.parent.resolve().is_relative_to(
                        Path(root).resolve()):
                    return None
            except (OSError, RuntimeError):
                return None
    args, i = list(argv[1:]), 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        if is_python:
            if arg[:2] in ("-m", "-c"):
                _operand(arg[:2], arg[2:] if len(arg) > 2 else (args[i + 1] if i + 1 < len(args) else ""), name)
                return None                       # a module or inline code, not a file
            if arg in ("-W", "-X"):
                i += 2
                continue
            if arg[:2] in ("-W", "-X"):
                i += 1
                continue
            if not arg.startswith("--") and len(arg) > 1 and set(arg[1:]) <= _PY_FLAGS:
                i += 1
                continue
        else:
            option = arg.split("=", 1)[0]
            if option in ("-e", "--eval", "-p", "--print"):
                _operand(option, arg.split("=", 1)[1] if "=" in arg
                         else (args[i + 1] if i + 1 < len(args) else ""), name)
                return None
            if option in _NODE_VALUED:
                i += 1 if "=" in arg else 2
                continue
            if arg in _NODE_FLAGS or ("=" in arg and option in _NODE_EQUALS and arg.split("=", 1)[1]):
                i += 1
                continue
        raise ValueError(f"capture profile preview option {arg[:40]!r} is not one the harness can read; "
                         f"start the {name} preview as '{name} [options] <script in the project>'"
                         + (" or 'python -m <module>'" if is_python else ""))
    if i >= len(args) or args[i] == "-":
        raise ValueError(f"capture profile preview runs {name} with no script; name the project file it serves")
    return args[i]


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
    if not isinstance(data, dict):
        raise ValueError("capture profile must be a JSON object")
    # Read apart from the fields below: the operator's two escapes, which
    # are about what the capture accepts, not about how the preview starts.
    data = dict(data)
    escapes = {}
    for name in ("shared_workers", "outside_listener"):
        value = data.pop(name, False)
        if type(value) is not bool:
            raise ValueError(f"capture profile {name} must be true or false")
        if value:
            escapes[name] = True
    profile = _preview_profile(data, root)
    return replace(profile, **escapes) if escapes else profile


def _preview_profile(data: dict, root) -> CaptureProfile:
    root = Path(root)
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
    # The script an interpreter is told to run must be in the project now: a
    # wrong path otherwise surfaces only when the harness first previews,
    # after every editing and review call of the task has been spent (UI
    # diagnostic lane on 4a273a3: 1.48M tokens, then exit 2). The operand is
    # found through the interpreter's own option grammar (Codex review of
    # 3e95645: python -u missing.py and an extensionless script slipped by).
    script = _interpreter_script(argv, root)
    if script is not None and not (root / script).is_file():
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


def _proc_listeners(port: int) -> Optional[list]:
    """Linux: every TCP socket listening on ``port`` as ``(address, pids)``,
    read from /proc; pids is empty when no readable process holds it. None
    when /proc does not list sockets here."""
    import struct
    sockets = []
    try:
        for table, family, size in (("/proc/net/tcp", socket.AF_INET, 8), ("/proc/net/tcp6", socket.AF_INET6, 32)):
            if not os.path.exists(table):
                continue
            with open(table, encoding="ascii", errors="replace") as handle:
                for line in handle.readlines()[1:]:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != "0A":
                        continue
                    address, _, hex_port = fields[1].rpartition(":")
                    if int(hex_port, 16) != port or len(address) != size:
                        continue
                    words = [int(address[i:i + 8], 16) for i in range(0, size, 8)]
                    host = socket.inet_ntop(family, struct.pack(f"={len(words)}I", *words))
                    sockets.append((f"[{host}]" if family == socket.AF_INET6 else host, fields[9]))
    except (OSError, ValueError):
        return None
    if not sockets and not os.path.exists("/proc/net/tcp"):
        return None
    holders: dict = {inode: set() for _, inode in sockets}
    for entry in (os.listdir("/proc") if holders else ()):
        if not entry.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{entry}/fd"):
                try:
                    link = os.readlink(f"/proc/{entry}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in holders:
                    holders[link[8:-1]].add(int(entry))
        except OSError:
            continue    # another user's process, or one that has gone
    return [(host, sorted(holders[inode])) for host, inode in sockets]


def _lsof_listeners(port: int) -> Optional[list]:
    """Elsewhere (macOS): the same, from ``lsof``. None when it cannot say."""
    try:
        found = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"], capture_output=True,
                               text=True, timeout=5, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode not in (0, 1):
        return None
    sockets, pid = [], None
    for line in found.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            sockets.append((line[1:].rpartition(":")[0] or line[1:], [pid]))
    return sockets


def _parent(pid: int) -> Optional[int]:
    """The parent of ``pid``, or None when it cannot be told."""
    if os.path.exists("/proc/self/stat"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                # "pid (name) state ppid ...": the name may hold spaces and ")".
                return int(handle.read(4096).rpartition(b")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    try:
        found = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                               timeout=2, check=False, stdin=subprocess.DEVNULL)
        return int(found.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _started_by(pid: int, ancestor: int, deadline: Optional[float] = None) -> Optional[bool]:
    """Whether ``pid`` is ``ancestor`` or a process it started, however
    indirectly: True, False once its line of parents reaches init without
    meeting ``ancestor``, or None when that line cannot be read to its end
    (a process that has gone, no tool, out of time)."""
    for _ in range(32):
        if pid == ancestor:
            return True
        if deadline is not None and time.monotonic() > deadline:
            return None
        parent = _parent(pid)
        if parent is None:
            return None
        if parent <= 1:
            return False
        pid = parent
    return None


def _listener_record(port: int, pgid: int) -> dict:
    """Who listens on the preview's port and where, as far as it can be told.

    ``addresses`` are the bound addresses; ``exposed`` is True when one is
    not loopback, so a server bound to every interface is on record (and
    absent when the listener could not be read: unknown, never "not
    exposed"). ``owner`` is ``preview`` when a listening socket is held by a process in
    the preview's own process group, or by one the preview started that
    left the group (honcho, watchexec and air start their server in a group
    of its own); ``other`` only when every listener's holder is known and
    shown not to be the preview's; and ``unknown`` otherwise (no tool,
    another user's process, a holder that has gone or whose parents cannot
    be read). Best effort: only ``other`` is ever acted on."""
    sockets = _proc_listeners(port) if os.path.exists("/proc/net/tcp") else _lsof_listeners(port)
    if not sockets:
        return dict(addresses=[], owner="unknown", pids=[])
    ours, known, pids = False, True, set()
    for _, holders in sockets:
        groups = set()
        for pid in holders:
            try:
                groups.add(os.getpgid(pid))
            except OSError:
                known = False
        ours = ours or pgid in groups
        known = known and bool(groups)
        pids.update(holders)
    if known and not ours:
        # Asked only where the group says "other": the preview's own server,
        # started in another group, is still the preview's. "other" stands
        # only when every holder's line of parents was read and none met
        # the preview; one that cannot be read leaves the owner unknown.
        deadline = time.monotonic() + 5
        answers = [_started_by(pid, pgid, deadline) for pid in sorted(pids)[:8]]
        ours = True in answers
        known = ours or (len(pids) <= 8 and all(answer is False for answer in answers))
    addresses = sorted({host for host, _ in sockets})
    loopback = ("127.", "[::1]", "localhost", "[::ffff:127.")
    return dict(addresses=addresses, owner="preview" if ours else "other" if known else "unknown",
                pids=sorted(pids)[:8], exposed=any(not host.startswith(loopback) for host in addresses))


#: What the last preview started for each origin recorded about its
#: listener; cleared when the next one starts.
_LISTENERS: dict = {}


def _note_listener(profile: CaptureProfile, listener: Optional[dict]) -> None:
    if listener is not None and profile.origin in _LISTENERS:
        listener.update(_LISTENERS[profile.origin])


@contextmanager
def running(profile: CaptureProfile, root, deadline: Optional[float] = None, listener: Optional[dict] = None):
    """Start the preview, wait until it answers, yield, and always stop it.

    Refused before launch when something already listens on the port, so a
    stray service can never stand in for this preview. Readiness needs the
    preview still alive at each poll for the same reason. Once it answers,
    who holds the listening socket is read (``_listener_record``, filled
    into ``listener`` when given): a process that bound the port between
    the check and the launch, with the preview alive but not listening,
    would otherwise be captured as the preview (review 2026-11-10, checks
    22). Only a listener shown to be outside the preview's process group
    stops the capture; where the owner cannot be told, nothing changes.
    A preview that serves through another process on purpose (a container's
    port forwarder) says so in its profile (``outside_listener``): the
    listener is then recorded and the capture goes on.
    """
    root = Path(root)
    _LISTENERS.pop(profile.origin, None)
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
        try:
            record = _listener_record(profile.port, proc.pid)
        except Exception:  # noqa: BLE001 -- best effort: an unreadable owner is never a failure
            record = dict(addresses=[], owner="unknown", pids=[])
        _LISTENERS[profile.origin] = record
        if listener is not None:
            listener.update(record)
        if record["owner"] == "other" and not profile.outside_listener:
            # Refused by default, with the operator's way out named: a
            # preview that serves through a container's port forwarder is
            # held by a process outside its group on purpose.
            raise PreviewFailed(f"{profile.origin} is served by another process (pid "
                                f"{', '.join(map(str, record['pids'])) or 'unknown'}), not by the preview "
                                "this run started; nothing was captured. Where the preview is meant to be "
                                "served by another process (a container's port forwarder), the capture "
                                'profile can say so with "outside_listener": true')
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
    exactly those digests.

    The harness's own options come before the first step, and the capture
    reads them nowhere else: what a task declares (a selector, a message, a
    file path) fills step positions only, so it can never be taken for a
    view name, a measurement or the attempt token (review 2026-11-10,
    checks 16)."""
    target = profile.origin + _web_path(capture.get("path", "/"), "capture path")
    # -P and a working directory outside the project: a project folder named
    # quadratus can never stand in for the harness's own capture module
    # (Codex review of 3a55d82); the project is passed as an absolute root.
    isolate = ["-P"] if sys.version_info >= (3, 11) else []
    argv = [sys.executable, *isolate, "-m", "quadratus.design_evidence", target, task_id, "{root}", "--pinned"]
    if profile.shared_workers:
        argv.append("--shared-workers")
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
    for step in capture.get("steps") or []:
        if step.get("action") == "file":
            argv += ["--upload", step["selector"], step["path"]]
        elif step.get("action") == "confirm":
            argv += ["--confirm", step["selector"], step["message"]]
        else:
            argv += [f"--{step['action']}", step["selector"]]
    return argv


def capture_task(profile: CaptureProfile, root, task_id: str, capture: dict,
                 receipt: Optional[dict] = None, listener: Optional[dict] = None) -> str:
    """Preview, capture the task's declared state, stop. Returns "" on success
    or why it failed; never raises for a preview or capture failure.

    ``receipt``, when given, is filled with each view's file digests as the
    harness measures them right after that view's capture process ends
    (``design_evidence.view_receipt``): a record kept outside the project,
    for ``check_records(receipt=...)`` to hold the summary to.

    ``listener``, when given, is filled with where the preview listened and
    who held the socket (``_listener_record``), from the last preview this
    capture started: the address it bound, and whether that was every
    interface.

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
            _note_listener(profile, listener)
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
    _note_listener(profile, listener)
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
        # No declared value may read as a command-line option: each travels
        # as an argument of the capture command (checks 16). The command
        # reads them by position as well; this is the same rule stated to
        # the declaring seat, refused where it is declared.
        for key in ("selector", "path", "message"):
            if isinstance(step.get(key), str) and step[key].strip().startswith("--"):
                raise ValueError(f'SCOPE capture steps: a {key} may not begin with "--"')
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
