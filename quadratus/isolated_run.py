"""Docker isolation and an external wall watchdog for a prepared solver tree.

This module never provisions credentials or downloads an image. It mounts only
the explicit disposable work tree and runtime tree. Neither should contain the
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


def run_isolated(*, image, work, runtime, command, wall_seconds=900, network=False):
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
    name = 'quadratus-blind-' + uuid.uuid4().hex
    deadline = time.monotonic() + wall_seconds
    args = ['docker', 'create', '--name', name, '--pull', 'never', '--init',
            '--user', f'{os.getuid() or 65534}:{os.getgid() or 65534}',
            '--read-only', '--network', 'bridge' if network else 'none',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '2g', '--cpus', '2', '--pids-limit', '128',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
            '--env', 'HOME=/tmp/solver-home', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            '--env', 'PYTHONPATH=/opt/quadratus', '--workdir', '/work',
            '--mount', f'type=bind,source={work},target=/work',
            '--mount', f'type=bind,source={runtime},target=/opt/quadratus,readonly',
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
