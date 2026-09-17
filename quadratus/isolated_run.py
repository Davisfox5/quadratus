"""Docker isolation and an external wall watchdog for a prepared solver tree.

This module never discovers credentials or downloads an image. It mounts only
the explicit disposable work tree, runtime tree and optional credential seed. Neither should contain the
examiner bundle, prior session state or developer home. Vendor readiness and
blindness of their instruction inputs require a separate preflight.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IsolatedResult:
    outcome: str
    exit_code: int | None
    image_id: str
    container_name: str
    #: Seconds since the heartbeat last advanced when the run ended. None when
    #: no heartbeat was watched. Present so a stall can be read from the record
    #: rather than inferred.
    idle_seconds: float | None = None


#: How long one `docker wait` slice runs before the heartbeat is re-read.
#: Short enough that a stall is noticed promptly, long enough that the
#: supervisor is not spinning: the workload writes far less often than this.
_POLL_SECONDS = 5


def _tree(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Mount must be a real directory')
    if path.resolve() != path:
        raise ValueError('Symlinked parent directories are not accepted')
    for entry in path.rglob('*'):
        if entry.is_symlink() or (not entry.is_file() and not entry.is_dir()):
            raise ValueError(f'Only ordinary files/directories may be mounted: {entry.name}')
    if ',' in str(path) or '\n' in str(path):
        raise ValueError('Mount paths cannot contain commas or newlines')
    return path


_AUTH_FILES = {'.codex/auth.json', '.claude/.credentials.json', '.grok/auth.json'}


def _credential_tree(path):
    """Accept a private, explicitly prepared auth-only tree, never a host home.

    Callers must stage subscription credentials only; filenames do not establish
    the authentication method. Secrets are neither returned nor logged here.
    """
    path = _tree(path)
    files = set()
    for entry in [path, *path.rglob('*')]:
        name = entry.relative_to(path).as_posix()
        allowed = (name in _AUTH_FILES if entry.is_file()
                   else name in {'.', '.codex', '.claude', '.grok'})
        if not allowed:
            raise ValueError('Credential seed may contain only supported authentication files')
        if entry.stat().st_mode & 0o077:
            raise ValueError('Credential seed must be private to its owner')
        if entry.is_file():
            files.add(name)
    if not files:
        raise ValueError('Credential seed must contain authentication files')
    return path


def run_isolated(*, image, work, runtime, command, wall_seconds=900, network=False,
                 credentials=None, heartbeat=None, stall_seconds=None):
    """Run a command in a disposable container and always remove its processes.

    Timeout bounds the workload; Docker control-plane cleanup has a separate
    bounded grace. Cleanup failure raises instead of claiming work has stopped.
    The writable work directory survives timeout for inspection of partial work.

    ``wall_seconds`` is a *ceiling*, not a schedule. Set it above whatever
    bounds the workload itself, so a run that uses its full time still gets to
    write its own records: attempt 10 of the blind acceptance had the two equal
    at 900 seconds and lost ``result.json``, ``report.md``, ``ledger.md``,
    ``delegation.md``, ``changes.diff`` and every vendor session file, because
    the kill landed before the workload's own final steps.

    ``heartbeat`` is a path the workload touches as it makes progress -- the
    run budget's state file is the natural one, since it is rewritten at every
    call boundary. With ``stall_seconds`` it separates the two failures that a
    single timeout conflates: a run still working when the ceiling arrives
    (``wall_deadline``) and a run that stopped making progress and would
    otherwise be waited on to the ceiling for nothing (``stalled``). A stall is
    caught in ``stall_seconds`` rather than in ``wall_seconds``, so a wedged
    container dies sooner than a busy one, not later.
    """
    if (isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float))
            or not math.isfinite(wall_seconds) or wall_seconds <= 0):
        raise ValueError('wall_seconds must be positive and finite')
    if stall_seconds is not None:
        if (isinstance(stall_seconds, bool) or not isinstance(stall_seconds, (int, float))
                or not math.isfinite(stall_seconds) or stall_seconds <= 0):
            raise ValueError('stall_seconds must be positive and finite')
        if heartbeat is None:
            raise ValueError('stall_seconds needs a heartbeat path to watch')
    if (not isinstance(command, (list, tuple)) or not command
            or any(not isinstance(x, str) or '\0' in x for x in command)
            or not command[0].strip()):
        raise ValueError('command must be an argv list')
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Use a locally inspected immutable Docker image ID')
    work, runtime = _tree(work), _tree(runtime)
    if work == runtime or work.is_relative_to(runtime) or runtime.is_relative_to(work):
        raise ValueError('Work and runtime mounts must be separate trees')
    auth_mount = []
    if credentials is not None:
        credentials = _credential_tree(credentials)
        if any(credentials == tree or credentials.is_relative_to(tree)
               or tree.is_relative_to(credentials) for tree in (work, runtime)):
            raise ValueError('Credentials must be separate from work and runtime')
        auth_mount = ['--mount', f'type=bind,source={credentials},target=/run/solver-auth,readonly']
        # The credential source is immutable. Refreshes and sessions are written
        # into this container's tmpfs HOME and disappear with the container.
        command = ['/bin/sh', '-c',
                   'umask 077; mkdir -p "$HOME" && cp -R /run/solver-auth/. "$HOME"/ '
                   '&& exec "$@"', 'solver-auth-bootstrap', *command]
    name = 'quadratus-blind-' + uuid.uuid4().hex
    deadline = time.monotonic() + wall_seconds
    args = ['docker', 'create', '--name', name, '--pull', 'never', '--init',
            '--user', f'{os.getuid() or 65534}:{os.getgid() or 65534}',
            '--read-only', '--network', 'bridge' if network else 'none',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '2g', '--cpus', '2', '--pids-limit', '512',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
            '--env', 'HOME=/tmp/solver-home', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            '--env', 'PYTHONPATH=/opt/quadratus', '--workdir', '/work',
            '--mount', f'type=bind,source={work},target=/work',
            '--mount', f'type=bind,source={runtime},target=/opt/quadratus,readonly',
            *auth_mount,
            '--entrypoint', command[0], image, *command[1:]]

    def docker(argv):
        return subprocess.run(argv, capture_output=True, text=True, check=True,
                              timeout=max(0.01, deadline - time.monotonic()))

    def beat():
        """When the workload last showed progress, or None if it never has."""
        if heartbeat is None:
            return None
        try:
            return Path(heartbeat).stat().st_mtime
        except OSError:
            return None

    outcome, code, idle = 'failed', None, None
    try:
        docker(args)
        docker(['docker', 'start', name])
        if stall_seconds is None:
            result = docker(['docker', 'wait', name])
            code = int(result.stdout.strip())
            outcome = 'success' if code == 0 else 'failed'
        else:
            # Waited in slices so the heartbeat can be read between them. The
            # slice is short relative to any sane stall window; the cost is one
            # stat per slice against a file the workload is writing anyway.
            started = time.monotonic()
            last_beat, last_seen = beat(), time.monotonic()
            while True:
                slice_end = min(deadline, time.monotonic() + _POLL_SECONDS)
                try:
                    result = subprocess.run(['docker', 'wait', name], capture_output=True,
                                            text=True, check=True,
                                            timeout=max(0.01, slice_end - time.monotonic()))
                    code = int(result.stdout.strip())
                    outcome = 'success' if code == 0 else 'failed'
                    break
                except subprocess.TimeoutExpired:
                    pass
                current = beat()
                if current != last_beat:
                    last_beat, last_seen = current, time.monotonic()
                idle = time.monotonic() - last_seen
                if idle >= stall_seconds:
                    # Progress stopped. Waiting out the remaining ceiling would
                    # buy nothing and is exactly the indefinite wait a
                    # heartbeat exists to avoid.
                    outcome = 'stalled'
                    break
                if time.monotonic() >= deadline:
                    outcome = 'wall_deadline'
                    break
                if time.monotonic() - started > wall_seconds:  # pragma: no cover
                    outcome = 'wall_deadline'
                    break
    except subprocess.TimeoutExpired:
        outcome = 'wall_deadline'
    finally:
        # Killing the whole container also kills descendants that created
        # their own process groups; killpg on the Python parent cannot do that.
        stopped = subprocess.run(['docker', 'rm', '--force', name],
                                 capture_output=True, text=True, timeout=15)
        if stopped.returncode and 'No such container' not in stopped.stderr:
            raise RuntimeError('Container cleanup failed; workload stop is unverified')
    # Stdout from workload must be explicitly written under /work if needed;
    # keep this supervisor's result free of possible vendor prompts/credentials.
    return IsolatedResult(outcome, code, image, name, idle_seconds=idle)
