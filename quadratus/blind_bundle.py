"""Export an explicit, immutable Git input set without repository/session state."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

_EXCLUDED_PARTS = {'.git', '.quadratus', '.claude', '.codex', '.cursor', '.gemini',
                   '.aider', 'node_modules',
                   '__pycache__', '.venv', 'collaboration', 'handoffs'}
_EXCLUDED_NAMES = {'CLAUDE.md', 'AGENTS.md', 'JOINT_REPAIR.md', 'MEMORY.md', 'GEMINI.md', '.env',
                   '.cursorrules', '.windsurfrules', 'copilot-instructions.md'}


def _path_allowed(name):
    path = PurePosixPath(name)
    return (name not in ('', '.') and not path.is_absolute() and '..' not in path.parts
            and not set(path.parts) & _EXCLUDED_PARTS
            and path.name not in _EXCLUDED_NAMES and not path.name.startswith('.env.')
            and not any(part.startswith('.aider') for part in path.parts)
            and '.github/instructions' not in str(path)
            and not any(c in name for c in ('\0', '\n', '\r', '\t')))


def freeze_input(repo, revision, paths, destination, *, brief):
    """Copy only selected tracked regular files and a task brief; hash each one.

    This prepares inputs, not isolation by itself. The examiner must remain
    outside the work/runtime trees passed to run_isolated. A fresh destination
    is required so unrelated files cannot ride along from an earlier attempt.
    """
    if not paths or any(not _path_allowed(p) for p in paths):
        raise ValueError('Select explicit application paths without agent or run state')
    if not brief.strip():
        raise ValueError('A task brief is required')
    if not isinstance(revision, str) or revision.startswith('-'):
        raise ValueError('Invalid Git revision')
    repo = Path(repo).resolve()
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('Destination must be new')

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    commit = git('rev-parse', '--verify', revision + '^{commit}').decode().strip()
    rows = git('ls-tree', '-rz', '--full-tree', commit, '--', *paths).split(b'\0')
    entries = []
    for row in filter(None, rows):
        header, raw_name = row.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        name = raw_name.decode()
        if not _path_allowed(name) or kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError(f'Input contains excluded state or a non-regular file: {name}')
        if name == 'TASK.md':
            raise ValueError('TASK.md is reserved for the frozen brief')
        entries.append((name, oid, mode))
    if not entries:
        raise ValueError('No tracked input files matched')
    for selected in paths:
        if not any(name == selected or name.startswith(selected.rstrip('/') + '/')
                   for name, _, _ in entries):
            raise ValueError(f'Selected input does not exist: {selected}')
    destination.mkdir(parents=True)
    work = destination / 'work'
    work.mkdir()
    hashes = {}
    for name, oid, mode in entries:
        data = git('cat-file', 'blob', oid)
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode == '100755' else 0o644)
        hashes[name] = hashlib.sha256(data).hexdigest()
    task = brief.encode()
    (work / 'TASK.md').write_bytes(task)
    hashes['TASK.md'] = hashlib.sha256(task).hexdigest()
    manifest = {'source_commit': commit, 'selected_paths': list(paths),
                'files': dict(sorted(hashes.items())),
                'state': 'input frozen; examiner and authenticated runtime not implied'}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest
