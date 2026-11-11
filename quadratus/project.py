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

from .providers import ProviderError, scratch_root

#: Folders and files a build or a test run writes. Never source, so a passing
#: check that writes one has not changed the source it checked. The project
#: watch records a change under one as an observation (quadratus.project_watch).
OUTPUT_NAMES = frozenset({
    'dist', 'build', '.next', 'htmlcov', '.hypothesis', '.mypy_cache', '.tox', '.nox',
    '.eslintcache', '.nyc_output', 'coverage.xml', 'junit.xml', 'test-results',
    'playwright-report'})
#: The output folders a build or an environment tool fills with Python. Every
#: other name above holds reports or caches and never Python, and pytest
#: collects from the ones without a leading dot: a conftest.py planted in
#: test-results/ turned a failing check green with the measured source
#: unchanged (second review of 6ea9794). The project watch holds a Python file
#: under those to the sensitive class.
PYTHON_OUTPUT_NAMES = frozenset({'dist', 'build', '.tox', '.nox'})
_SKIP = {'.git', '.quadratus', '.multi_llm', '.venv', 'venv', 'env',
         'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache',
         '.DS_Store', '.coverage'} | OUTPUT_NAMES
#: Credential files by name, never copied to a model. Short and unambiguous on
#: purpose: *.pem and *.key are left alone because test fixtures use them.
CREDENTIAL_NAMES = frozenset({'.npmrc', '.pypirc', '.netrc', '.git-credentials',
                              'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'})
CREDENTIAL_TAILS = (('.aws', 'credentials'), ('.docker', 'config.json'))
#: Configuration a vendor's agent CLI reads from its working directory. A
#: headless CLI can run hooks and start MCP servers from these without asking,
#: so no copy handed to a model carries them.
AGENT_CONFIG_NAMES = frozenset({'.mcp.json'})
AGENT_CONFIG_TAILS = (('.claude', 'settings.json'), ('.claude', 'settings.local.json'))
AGENT_CONFIG_DIRS = frozenset({'.codex', '.grok'})
#: Folders a file in AGENT_CONFIG_TAILS is read through. A link so named can
#: lead to settings kept under any other name, so the link itself counts.
AGENT_CONFIG_PARENTS = frozenset(tail[0] for tail in AGENT_CONFIG_TAILS)
_GITHUB = re.compile(r'(?:https://github\.com/|git@github\.com:)[\w.-]+/[\w.-]+/?$')
_MAX_SOURCE_BYTES = 100 * 1024 * 1024
#: git, for commands run inside an existing project. The project's own
#: .git/config must not choose a program for the harness to run:
#: core.fsmonitor names one, and commands that read the index ask it
#: (probed on git 2.43: the hook ran on a plain status and on ls-files, and
#: on neither with this override).
GIT = ('git', '-c', 'core.fsmonitor=false')


def withheld(parts) -> str:
    """Why a project-relative path is never copied to a model, or ''.

    ``'secret'`` for a ``.env*`` file, ``'credential'`` for a credential file,
    ``'agent-config'`` for vendor agent configuration. The one place the rule
    is written: source listing, patches, review copies and the project watch
    all ask here.
    """
    # Names are compared without case: on a case-insensitive filesystem (the
    # macOS default) a CLI that opens .mcp.json is handed .MCP.json.
    parts = tuple(str(part).lower() for part in parts)
    if not parts:
        return ''
    name, tail = parts[-1], parts[-2:]
    if any(part in AGENT_CONFIG_DIRS for part in parts) or name in AGENT_CONFIG_NAMES \
            or tail in AGENT_CONFIG_TAILS:
        return 'agent-config'
    if name in CREDENTIAL_NAMES or tail in CREDENTIAL_TAILS:
        return 'credential'
    if name.startswith('.env') and name != '.env.example':
        return 'secret'
    return ''


def source_path(parts) -> bool:
    """Whether a project-relative path can be measured source by its name:
    no skipped component and nothing withheld. Symlinks and operator
    exclusions are facts about the tree and are checked where it is walked."""
    parts = tuple(parts)
    return bool(parts) and not any(part in _SKIP for part in parts) and not withheld(parts)


#: The most of one withheld file, and of all of them, a harness copy takes.
_MAX_WITHHELD_FILE = 8 * 1024 * 1024
_MAX_WITHHELD_TOTAL = 64 * 1024 * 1024


def withheld_files(root, exclude=()):
    """``{relative path: bytes}`` for the regular files :func:`withheld`
    keeps out of every model copy, for a copy only the harness's own checks
    run in (``integration.run_original_tests``).

    No model is given these. The project's own suite reads them where it
    runs in the project (a ``.npmrc`` fixture, a ``.env.test``), so a copy
    the same suite is run in must hold them too, or the suite fails there
    for a reason that says nothing about the source. Links are never
    followed, skipped folders and operator exclusions stay skipped, and a
    file over the bound or unreadable is left out, as it was before.
    """
    root = Path(root)
    excluded = {Path(p).resolve() for p in exclude}
    found, total = {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in _SKIP and not (parent / d).is_symlink()
                         and (parent / d).resolve() not in excluded)
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(root)
            if name in _SKIP or path.is_symlink() or not withheld(relative.parts):
                continue
            try:
                if not path.is_file() or path.resolve() in excluded:
                    continue
                size = path.stat().st_size
                if size > _MAX_WITHHELD_FILE or total + size > _MAX_WITHHELD_TOTAL:
                    continue
                found[relative.as_posix()] = path.read_bytes()
                total += size
            except OSError:
                continue
    return found


class Project:
    def __init__(self, path, *, exclude=()):
        self.root = Path(path).expanduser().resolve()
        if self.root.exists() and not self.root.is_dir():
            # Said plainly (Codex GUI diagnostic on 0a99ee2: a file given as
            # the project folder was reported as missing).
            raise ValueError(f'Project folder is a file, not a folder: {self.root}')
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
            cls._command([*GIT, 'check-ref-format', '--branch', branch], cwd=root)
            cls._command([*GIT, 'switch', '-c', branch], cwd=root)
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
        """Source only; never follow a symlink into another tree.

        This is the measured source set and the set every model copy is
        built from. Secrets, credential files and vendor agent configuration
        are in neither (:func:`withheld`); what an editing call does to them,
        to symlinks and to the skipped folders is the project watch's to see.
        """
        for directory, dirs, names in os.walk(self.root, followlinks=False):
            parent = Path(directory)
            dirs[:] = sorted(d for d in dirs if d not in _SKIP and d.lower() not in AGENT_CONFIG_DIRS
                             and not (parent / d).is_symlink()
                             and (parent / d).resolve() not in self.exclude)
            for name in sorted(names):
                path = parent / name
                if (name in _SKIP or path.is_symlink() or not path.is_file()
                        or path.resolve() in self.exclude
                        or withheld(path.relative_to(self.root).parts)):
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
        with tempfile.TemporaryDirectory(prefix='quadratus-review-', dir=scratch_root()) as directory:
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
        from .scope import _diff_lines

        # Only text hunks are supported. Strip optional git headers after
        # rejecting extended operations, whose paths don't live in ---/+++.
        if re.search(r'^(?:rename |copy |GIT binary patch|Binary files |old mode |new mode |new file mode 120|deleted file mode 120)', patch, re.MULTILINE):
            raise ProviderError('PATCH supports text file hunks, not renames, binary files or symlinks.')
        patch = re.sub(r'^(?:diff --git |index |new file mode |deleted file mode ).*\n', '', patch, flags=re.MULTILINE)
        # Unified-diff filenames may contain spaces; only a tab separates an
        # optional timestamp. Validating the first word would let git apply a
        # different path, including an excluded file below a spaced directory.
        paths = [line[4:].split('\t', 1)[0] for line, in_hunk in _diff_lines(patch)
                 if not in_hunk and line.startswith(('--- ', '+++ '))]
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
                    or not source_path(relative.parts)
                    or any(part.startswith('.env') and part != '.env.example'
                           for part in relative.parts)
                    or not target.resolve().is_relative_to(self.root)
                    or any(p.is_symlink() for p in [target, *target.parents] if p != self.root)
                    or any(target.resolve().is_relative_to(p) for p in self.exclude)):
                raise ProviderError(f'PATCH path is outside editable project source: {raw}')
        self._command([*GIT, 'apply', '--check', '-'], cwd=self.root, input=patch)
        self._command([*GIT, 'apply', '-'], cwd=self.root, input=patch)

    def git_status(self):
        try:
            return self._command([*GIT, 'status', '--short'], cwd=self.root).strip()
        except ProviderError:
            return 'This folder is not a Git repository.'
