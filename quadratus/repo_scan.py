"""Deterministic first look at an existing codebase.

The system was designed goal-first, but most real runs do not start from an
empty directory -- they start from a repository someone wants improved. The
architecture handles that without a new mode: the same loop runs, and the
difference is entirely in *seeding*. Two places change today, and a third
is planned:

* **The interview**, when it is built, gets this scan's summary, so it
  stops asking questions the code already answers and asks about intent
  instead ("improve what, for whom?"). It is not implemented yet, so
  nothing consumes the summary this way today.
* **The codebase map** -- normally empty until close-outs populate it -- is
  seeded with scanned facts, each carrying ``author="scan"`` provenance so a
  reader can weigh a mechanical observation differently from a model's
  verified close-out note.
* **The integration gate** is configured automatically when the scan finds
  the project's own check command, so "does it still pass its own tests" is
  enforced from task one.

Everything here is deterministic and dumb by design, like the browser
evidence collector: no model is invoked, nothing is judged. Understanding
*why* the code is shaped the way it is stays where it belongs -- in
comprehension tasks, whose close-out map notes accumulate on top of these
seeds with real provenance.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = ["ScanReport", "scan_repo", "seed_map"]

#: Directories that are dependency caches or build output, not the codebase.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", "target", "vendor", ".idea", ".vscode", ".ruff_cache",
    ".multi_llm", ".quadratus",
}

#: Extension -> language label, for the stack summary.
_LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cs": "C#",
    ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".sql": "SQL",
    ".html": "HTML", ".css": "CSS", ".vue": "Vue", ".svelte": "Svelte",
}

#: How many source files the map names. Small on purpose: this block is
#: re-sent on every orchestrator and lead call, so it pays its way only while
#: it stays orientation rather than an inventory.
_MAX_PRINCIPAL_FILES = 6
_MAX_TEST_DIRS = 3
#: Lines read before giving up on one file, so a generated bundle or a minified
#: asset cannot turn a bounded scan into a long one.
_MAX_LINES_COUNTED = 200_000
#: Bytes read from one file while counting its lines, so one enormous line
#: costs no more than a short one.
_MAX_COUNT_BYTES = 16 * 1024 * 1024
#: Bytes of a README or a manifest the scan will read.
_README_BYTES = 16_384
_MANIFEST_BYTES = 256_000
_CHUNK = 65_536


def _read_text(path: Path, limit: int = _MANIFEST_BYTES) -> str:
    """A small regular file as UTF-8 text, or '' when it is missing, not a
    regular file, over ``limit`` bytes, unreadable or not UTF-8. A manifest
    is project input: nothing about it is assumed."""
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return ""
    if len(data) > limit:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _table(value) -> dict:
    """``value`` when it is a JSON or TOML table, else an empty one."""
    return value if isinstance(value, dict) else {}


def _is_test_path(relative: str) -> bool:
    """Test files are excluded from 'largest source files' and counted apart.

    A suite is often the biggest thing in a repository and naming it as the
    principal source would point a lead at exactly the wrong place.
    """
    lowered = relative.lower()
    parts = lowered.split("/")
    name = parts[-1]
    return (any(p in ("test", "tests", "spec", "specs", "__tests__") for p in parts[:-1])
            or name.startswith("test_") or name.startswith("spec_")
            or ".test." in name or ".spec." in name
            or name.endswith("_test.py") or name.endswith("_spec.rb"))


def _count_lines(path: Path) -> int:
    """Line count, or 0 when the file cannot be read.

    Newlines are counted in fixed-size chunks and reading stops at the line
    or byte bound: iterating lines loaded a multi-gigabyte single-line file
    as one line.
    """
    try:
        with path.open("rb") as handle:
            count, read, last = 0, 0, b"\n"
            while count < _MAX_LINES_COUNTED and read < _MAX_COUNT_BYTES:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                count += chunk.count(b"\n")
                last = chunk[-1:]
            if read and last != b"\n":
                count += 1  # the final line, unterminated or cut at the bound
            return min(count, _MAX_LINES_COUNTED)
    except OSError:
        return 0


#: Manifest files worth naming, in the order they are worth naming.
_MANIFESTS = [
    "pyproject.toml", "setup.py", "requirements.txt", "package.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "Gemfile",
    "composer.json", "Makefile", "Dockerfile", "docker-compose.yml",
]

#: Walk no further than this many files; a scan is a sketch, not an index.
_MAX_FILES = 50_000
#: Nor look at more than this many directory entries of any kind: a tree of
#: empty folders has no files to count and was walked whole.
_MAX_ENTRIES = 200_000


@dataclass
class ScanReport:
    """What one deterministic pass over the tree found."""

    root: Path
    file_count: int = 0
    #: Language label -> file count, most common first.
    languages: Dict[str, int] = field(default_factory=dict)
    manifests: List[str] = field(default_factory=list)
    top_dirs: List[str] = field(default_factory=list)
    readme_head: str = ""
    #: ``(relative path, line count)`` for the biggest non-test source files,
    #: largest first. A lead is told which file to change and still has to
    #: discover what is in the project; naming the principal files costs a
    #: line and saves it guessing which of nineteen JavaScript files matters.
    principal_files: List[Tuple[str, int]] = field(default_factory=list)
    #: Directories holding the test suite, most populated first.
    test_dirs: List[str] = field(default_factory=list)
    #: The project's own check command, when one is recognisable. This is what
    #: the integration gate runs; None means the operator must supply one for
    #: the gate to exist.
    check_command: Optional[List[str]] = None
    #: Every test entry point the repository itself declares, in the order
    #: they are recognised. A project with both a ``package.json`` test script
    #: and a Python suite declares both, and both are run by the gate.
    declared_checks: List[List[str]] = field(default_factory=list)
    #: Why the project's own ``.venv`` interpreter was not used for the Python
    #: check, when it has one that was passed over. Shown in the gate plan.
    interpreter_note: str = ""

    @property
    def has_code(self) -> bool:
        return bool(self.languages)

    def summary(self) -> str:
        """One prompt-sized block describing the codebase."""
        if not self.has_code and not self.manifests:
            return ""
        lines = [f"Existing codebase at {self.root}: {self.file_count} files."]
        if self.languages:
            langs = ", ".join(
                f"{lang} ({count})" for lang, count in
                sorted(self.languages.items(), key=lambda kv: -kv[1])[:5]
            )
            lines.append(f"Languages: {langs}.")
        if self.top_dirs:
            lines.append("Top-level directories: " + ", ".join(self.top_dirs) + ".")
        if self.manifests:
            lines.append("Manifests: " + ", ".join(self.manifests) + ".")
        if self.check_command:
            lines.append("Check command: " + " ".join(self.check_command) + ".")
        if self.readme_head:
            lines.append("README begins:\n" + self.readme_head)
        return "\n".join(lines)

    def map_notes(self) -> List[Tuple[str, str]]:
        """(topic, note) pairs worth seeding into the codebase map."""
        notes: List[Tuple[str, str]] = []
        if self.languages:
            ranked = sorted(self.languages.items(), key=lambda kv: -kv[1])
            notes.append((
                "stack",
                "Primary language: " + ", ".join(
                    f"{lang} ({count} files)" for lang, count in ranked[:3]
                ),
            ))
        if self.top_dirs:
            notes.append(("layout", "Top-level directories: " + ", ".join(self.top_dirs)))
        if self.principal_files:
            notes.append((
                "layout",
                "Largest source files: " + ", ".join(
                    f"{name} ({lines} lines)" for name, lines in self.principal_files
                ),
            ))
        if self.test_dirs:
            notes.append(("tests", "Test files live under: " + ", ".join(self.test_dirs)))
        if self.manifests:
            notes.append(("dependencies", "Manifests present: " + ", ".join(self.manifests)))
        if self.check_command:
            notes.append((
                "checks",
                "The project's own check command is: " + " ".join(self.check_command),
            ))
        return notes


def _detect_check_command(root: Path, manifests: List[str]) -> Optional[List[str]]:
    """Recognise the project's own test entry point, conservatively.

    Only commands the repository itself declares are returned -- a guessed
    command that happens to pass proves nothing, and one that happens to fail
    would charge the lead a fix round for the scanner's mistake.
    """
    declared = _declared_checks(root, manifests)
    return declared[0] if declared else None


def _declared_checks(root: Path, manifests: List[str],
                     notes: Optional[List[str]] = None) -> List[List[str]]:
    """Every test entry point the repository declares, first one first.

    Codex, Run 15: a project with a ``package.json`` test script and a
    Python suite had only pytest run in-run, so its Node UI tests never
    counted toward completion.
    """
    found: List[List[str]] = []
    if "package.json" in manifests:
        try:
            pkg = json.loads(_read_text(root / "package.json") or "{}")
            if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("test"):
                found.append(["npm", "test", "--silent"])
        except (ValueError, RecursionError):
            pass
    python = _python_check(root, manifests, notes)
    if python:
        found.append(python)
    if "go.mod" in manifests:
        found.append(["go", "test", "./..."])
    if "Cargo.toml" in manifests:
        found.append(["cargo", "test", "--quiet"])
    return found


def _has_python_tests(tests: Path, limit: int = 2000) -> bool:
    """Whether ``tests`` holds a Python file, consuming at most ``limit`` + 1
    directory entries in all: the one past the limit is read only to learn
    the budget is spent (Codex review of 8a71d25: the listing was
    materialised whole before the budget applied).

    Hidden, cache and vendored directories and symlinks are skipped, as in
    the main scan. Running out of budget answers False: no Python file was
    shown to exist, so no Python gate is declared from it.
    """
    seen = 0
    stack = [tests]
    while stack:
        try:
            listing = os.scandir(stack.pop())
        except OSError:
            continue
        with listing:
            for entry in listing:
                seen += 1
                if seen > limit:
                    return False
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not entry.name.startswith(".") and entry.name not in _SKIP_DIRS:
                            stack.append(Path(entry.path))
                    elif entry.name.endswith(".py"):
                        return True
                except OSError:
                    continue
    return False


def _git_tracks(root: Path, relative: str) -> Optional[bool]:
    """Whether git tracks anything at or under ``relative``. False when the
    folder is in no repository; None when git could not say."""
    try:
        base = root.resolve()
    except (OSError, RuntimeError):
        return None
    if not any(os.path.lexists(folder / ".git") for folder in (base, *base.parents)):
        return False
    from .project import GIT
    try:
        result = subprocess.run([*GIT, "ls-files", "-z", "--", relative], cwd=root,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return bool(result.stdout)


def _project_interpreter(root: Path) -> Tuple[Optional[str], str]:
    """The project's own interpreter when it can be trusted as one, else
    ``(None, why not)``; ``(None, "")`` when the project has no ``.venv``.

    A gate's first argument is a program the harness runs, so a file the
    repository can simply contain is not enough (whole-repo review,
    2026-11-10: a committed ``.venv/bin/python`` became the pytest gate's
    interpreter). It is used only when ``.venv`` is a real virtual
    environment (``pyvenv.cfg``), its ``bin/python`` resolves through its
    whole link chain to an executable outside the project, and git tracks
    nothing under ``.venv``.
    """
    venv = root / ".venv"
    local = venv / "bin" / "python"
    if not os.path.lexists(local):
        return None, ""
    try:
        marker = venv / "pyvenv.cfg"
        if marker.is_symlink() or not marker.is_file():
            return None, "it is not a virtual environment: there is no .venv/pyvenv.cfg"
        target = local.resolve(strict=True)
        if not target.is_file() or not os.access(target, os.X_OK):
            return None, "it does not resolve to an executable file"
        if target.is_relative_to(root.resolve()):
            return None, "it resolves to a file inside the project, which the project could have written"
    except (OSError, RuntimeError) as exc:
        return None, f"it could not be resolved ({type(exc).__name__})"
    tracked = _git_tracks(root, ".venv")
    if tracked is None:
        return None, "git could not say whether .venv is tracked"
    if tracked:
        return None, "git tracks files under .venv, so the repository chose it"
    return str(local), ""


def _python_check(root: Path, manifests: List[str],
                  notes: Optional[List[str]] = None) -> Optional[List[str]]:
    # A tests/ directory alone is not a Python signal: a JS-only project with
    # tests/ui.test.js gained a pytest gate that failed on zero tests (Codex
    # review of 3ef9962). A manifest, pytest.ini or a Python file under
    # tests/ is.
    tests = root / "tests"
    python_signals = (
        "pyproject.toml" in manifests
        or "setup.py" in manifests
        or (root / "pytest.ini").exists()
        or (tests.is_dir() and _has_python_tests(tests))
    )
    if python_signals and tests.is_dir():
        interpreter, why = _project_interpreter(root)
        if why and notes is not None:
            notes.append(
                f"The project's .venv/bin/python was not used for the Python check: {why}. "
                "The harness interpreter runs it. Name an interpreter in your own check "
                "command to use it.")
        return [interpreter or sys.executable, "-m", "pytest", "-q"]
    return None


def scan_repo(root) -> ScanReport:
    """One bounded, deterministic pass over the tree."""
    root = Path(root)
    report = ScanReport(root=root)
    if not root.is_dir():
        return report

    report.manifests = [m for m in _MANIFESTS if (root / m).is_file()
                        and not (root / m).is_symlink()]

    sized: List[Tuple[str, int]] = []
    test_files: List[str] = []
    top_dirs: List[str] = []
    stack: List[Path] = [root]
    # Every directory entry counts against the budget, and a listing is read
    # as it is walked: a tree of millions of empty folders, or one folder
    # with millions of names, was materialised whole before any bound applied.
    seen = 0
    while stack and report.file_count < _MAX_FILES and seen < _MAX_ENTRIES:
        current = stack.pop()
        try:
            listing = os.scandir(current)
        except OSError:
            continue
        with listing:
            for item in listing:
                seen += 1
                if seen > _MAX_ENTRIES:
                    break
                entry = Path(item.path)
                named = item.name not in _SKIP_DIRS and not item.name.startswith(".")
                try:
                    if item.is_symlink():
                        # Never walked. A linked top-level folder is still named.
                        if current is root and named and item.is_dir():
                            top_dirs.append(item.name)
                        continue
                    if item.is_dir(follow_symlinks=False):
                        if named:
                            stack.append(entry)
                            if current is root:
                                top_dirs.append(item.name)
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                report.file_count += 1
                lang = _LANGUAGES.get(entry.suffix.lower())
                if lang:
                    report.languages[lang] = report.languages.get(lang, 0) + 1
                    relative = entry.relative_to(root).as_posix()
                    if _is_test_path(relative):
                        test_files.append(relative)
                    else:
                        lines = _count_lines(entry)
                        if lines:
                            sized.append((relative, lines))
                if report.file_count >= _MAX_FILES:
                    break
    report.top_dirs = sorted(top_dirs)

    for name in ("README.md", "README.rst", "README.txt", "README"):
        candidate = root / name
        if candidate.is_file() and not candidate.is_symlink():
            try:
                # The head only: twelve lines never needed the whole file.
                with candidate.open("rb") as handle:
                    head = handle.read(_README_BYTES).decode("utf-8", errors="replace")
                report.readme_head = "\n".join(head.splitlines()[:12]).strip()
            except OSError:
                pass
            break

    # Largest first, and few: this is rendered into every orchestrator and
    # lead prompt, so it is orientation, not an inventory.
    sized.sort(key=lambda pair: (-pair[1], pair[0]))
    report.principal_files = sized[:_MAX_PRINCIPAL_FILES]
    directories = {}
    for name in test_files:
        parent = name.rsplit("/", 1)[0] + "/" if "/" in name else "(project root)"
        directories[parent] = directories.get(parent, 0) + 1
    report.test_dirs = [d for d, _ in sorted(directories.items(),
                                             key=lambda kv: (-kv[1], kv[0]))][:_MAX_TEST_DIRS]

    notes: List[str] = []
    report.declared_checks = _declared_checks(root, report.manifests, notes)
    report.check_command = report.declared_checks[0] if report.declared_checks else None
    report.interpreter_note = " ".join(notes)
    return report


def seed_map(report: ScanReport, codebase_map, *, session: str = "scan") -> int:
    """Seed scanned facts into the map, skipping notes already present.

    Provenance is ``author="scan"`` so a future reader can tell a mechanical
    observation from a model's verified close-out note. Idempotent across
    runs: re-scanning an unchanged repository adds nothing.
    """
    existing = {n.note for n in codebase_map.notes}
    added = 0
    for topic, note in report.map_notes():
        if note not in existing:
            codebase_map.amend(topic=topic, note=note, author="scan", session=session)
            added += 1
    return added


def detect_adapters(root, report=None):
    """Manifest-backed adapter hints. No imports, commands, or capability grants."""
    import tomllib

    root = Path(root).resolve()
    report = report or scan_repo(root)
    found = {}

    def put(name, value, source):
        found[name] = {'status': 'detected', 'value': value, 'source': source}

    def read(relative):
        # A manifest is project input. A link out of the project, a file that
        # is too large, unreadable or not UTF-8 all read as absent.
        path = root / relative
        try:
            if not path.resolve().is_relative_to(root):
                return ''
        except (OSError, RuntimeError):
            return ''
        return _read_text(path)

    if report.check_command:
        for name in ('test_runner', 'runner'):
            put(name, list(report.check_command), 'repo_scan.check_command')
    try:
        project = _table(tomllib.loads(read('pyproject.toml')))
    except (ValueError, RecursionError):
        project = {}
    if _table(project.get('project')).get('name') == 'quadratus' and read('quadratus/registry.py'):
        put('runtime_model_policy', 'multi-vendor', 'pyproject.toml#project.name')
        put('catalog_module', 'quadratus/registry.py', 'quadratus/registry.py')
        put('permitted_calls', ['quadratus/cli_providers.py', 'quadratus/providers.py'],
            'quadratus/registry.py')
        put('routing_audit_exemption', 'CLAUDE.md#model-routing-this-repo-is-build-time-tooling',
            'CLAUDE.md')
    for manifest in ('package.json', 'site/package.json', 'apps/app/package.json'):
        try:
            pkg = json.loads(read(manifest) or '{}')
        except (ValueError, RecursionError):
            continue
        if not isinstance(pkg, dict):
            continue
        deps = {**_table(pkg.get('devDependencies')), **_table(pkg.get('dependencies'))}
        for name in ('@mui/material', 'react'):
            if name in deps and 'ui_lib' not in found:
                put('ui_lib', name, manifest)
        if 'zod' in deps:
            put('validation_lib', 'zod', manifest)
    for relative in ('prisma/schema.prisma', 'site/prisma/schema.prisma'):
        if read(relative):
            put('tool', 'prisma', relative)
            break
    else:
        for relative in ('alembic.ini', 'backend/alembic.ini'):
            if read(relative):
                put('tool', 'alembic', relative)
                break
    for relative in ('.claude/agents', '.agents'):
        path = root / relative
        if path.is_dir() and path.resolve().is_relative_to(root):
            put('agent_dir', relative, relative)
            break
    # All unrecognised fields are marked not configured by the family resolver.
    return found
