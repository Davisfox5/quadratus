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
                 credentials=None):
    """Run a command in a disposable container and always remove its processes.

    Timeout bounds the workload; Docker control-plane cleanup has a separate
    bounded grace. Cleanup failure raises instead of claiming work has stopped.
    The writable work directory survives timeout for inspection of partial work.
    """
    if (isinstance(wall_seconds, bool) or not isinstance(wall_seconds, (int, float))
            or not math.isfinite(wall_seconds) or wall_seconds <= 0):
        raise ValueError('wall_seconds must be positive and finite')
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

    outcome, code = 'failed', None
    try:
        docker(args)
        docker(['docker', 'start', name])
        result = docker(['docker', 'wait', name])
        code = int(result.stdout.strip())
        outcome = 'success' if code == 0 else 'failed'
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
    return IsolatedResult(outcome, code, image, name)
