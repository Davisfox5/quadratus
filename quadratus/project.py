"""Persistent source trees and disposable source copies for model reads."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .providers import ProviderError

_SKIP = {'.git', '.quadratus', '.multi_llm', '.venv', 'venv', 'env',
         'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache',
         '.next', 'dist', 'build', '.DS_Store', '.coverage', 'htmlcov'}
_GITHUB = re.compile(r'(?:https://github\.com/|git@github\.com:)[\w.-]+/[\w.-]+/?$')
_MAX_SOURCE_BYTES = 100 * 1024 * 1024


class Project:
    def __init__(self, path, *, exclude=()):
        self.root = Path(path).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f'Project folder does not exist: {self.root}')
        self.exclude = {Path(p).resolve() for p in exclude}

    @classmethod
    def open(cls, path, *, clone_url='', branch=''):
        root = Path(path).expanduser().resolve()
        if clone_url:
            if not _GITHUB.fullmatch(clone_url):
                raise ValueError('Use an HTTPS or git@github.com: repository URL.')
            if root.exists() and any(root.iterdir()):
                raise ValueError('Clone destination must be a new or empty folder.')
            root.parent.mkdir(parents=True, exist_ok=True)
            cls._command(['git', 'clone', '--', clone_url, str(root)], cwd=root.parent)
        elif not root.exists():
            root.mkdir(parents=True)
            cls._command(['git', 'init', '--quiet'], cwd=root)
        project = cls(root)
        if branch:
            cls._command(['git', 'check-ref-format', '--branch', branch], cwd=root)
            cls._command(['git', 'switch', '-c', branch], cwd=root)
        return project

    @staticmethod
    def _command(argv, *, cwd, input=None):
        try:
            result = subprocess.run(argv, cwd=cwd, input=input, text=True,
                                    capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f'Project command failed: {exc}') from exc
        if result.returncode:
            raise ProviderError((result.stderr or result.stdout).strip()[:2000])
        return result.stdout

    def files(self):
        """Source only; never follow a symlink into another tree."""
        for directory, dirs, names in os.walk(self.root, followlinks=False):
            parent = Path(directory)
            dirs[:] = sorted(d for d in dirs if d not in _SKIP
                             and not (parent / d).is_symlink()
                             and (parent / d).resolve() not in self.exclude)
            for name in sorted(names):
                path = parent / name
                if (name in _SKIP or path.is_symlink() or path.resolve() in self.exclude
                        or (name.startswith('.env') and name != '.env.example')):
                    continue
                yield path

    def contents(self):
        result, total = {}, 0
        for path in self.files():
            total += path.stat().st_size
            if total > _MAX_SOURCE_BYTES:
                raise ProviderError('Project source exceeds 100 MiB; select a smaller project folder.')
            result[path.relative_to(self.root).as_posix()] = path.read_bytes()
        return result

    def fingerprint(self):
        digest = hashlib.sha256()
        for name, data in sorted(self.contents().items()):
            digest.update(name.encode() + b'\0' + hashlib.sha256(data).digest())
        return digest.hexdigest()

    @contextmanager
    def snapshot(self):
        # copy2, never hard links: a reviewer's writes must not alter source.
        with tempfile.TemporaryDirectory(prefix='quadratus-review-') as directory:
            root = Path(directory)
            for name, data in self.contents().items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                shutil.copymode(self.root / name, target)
            yield root

    def diff(self, before):
        after = self.contents()
        out = []
        for name in sorted(before.keys() | after.keys()):
            old, new = before.get(name, b''), after.get(name, b'')
            if old == new and (name in before) == (name in after):
                continue
            try:
                old_lines, new_lines = old.decode().splitlines(True), new.decode().splitlines(True)
            except UnicodeDecodeError:
                out.append(f'Binary file changed: {name}\n')
                continue
            diff_lines = difflib.unified_diff(
                old_lines, new_lines, fromfile=f'a/{name}' if name in before else '/dev/null',
                tofile=f'b/{name}' if name in after else '/dev/null')
            chunk = ''.join(line if line.endswith('\n') else
                            line + '\n\\ No newline at end of file\n' for line in diff_lines)
            out.append(chunk or f'Empty file added/deleted: {name}\n')
        return ''.join(out)

    def apply_patch(self, patch):
        """Apply an explicitly requested unified diff without granting tools."""
        # Only text hunks are supported. Strip optional git headers after
        # rejecting extended operations, whose paths don't live in ---/+++.
        if re.search(r'^(?:rename |copy |GIT binary patch|Binary files |old mode |new mode |new file mode 120|deleted file mode 120)', patch, re.MULTILINE):
            raise ProviderError('PATCH supports text file hunks, not renames, binary files or symlinks.')
        patch = re.sub(r'^(?:diff --git |index |new file mode |deleted file mode ).*\n', '', patch, flags=re.MULTILINE)
        paths = re.findall(r'^(?:---|\+\+\+) (\S+)', patch, re.MULTILINE)
        if not paths:
            raise ProviderError('PATCH must contain a unified diff with --- and +++ paths.')
        for raw in paths:
            if raw == '/dev/null':
                continue
            if not raw.startswith(('a/', 'b/')):
                raise ProviderError('PATCH paths must start with a/ or b/.')
            relative = Path(raw[2:])
            target = self.root / relative
            if (relative.is_absolute() or '..' in relative.parts
                    or any(part in _SKIP or (part.startswith('.env') and part != '.env.example')
                           for part in relative.parts)
                    or not target.resolve().is_relative_to(self.root)
                    or any(p.is_symlink() for p in [target, *target.parents] if p != self.root)
                    or any(target.resolve().is_relative_to(p) for p in self.exclude)):
                raise ProviderError(f'PATCH path is outside editable project source: {raw}')
        self._command(['git', 'apply', '--check', '-'], cwd=self.root, input=patch)
        self._command(['git', 'apply', '-'], cwd=self.root, input=patch)

    def git_status(self):
        try:
            return self._command(['git', 'status', '--short'], cwd=self.root).strip()
        except ProviderError:
            return 'This folder is not a Git repository.'
