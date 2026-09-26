"""Deterministic first look at an existing codebase.

The system was designed goal-first, but most real runs do not start from an
empty directory -- they start from a repository someone wants improved. The
architecture handles that without a new mode: the same loop runs, and the
difference is entirely in *seeding*. Three places change:

* **The interview** gets this scan's summary, so it stops asking questions
  the code already answers and asks about intent instead ("improve what,
  for whom?").
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
    """Line count, or 0 when the file cannot be read as text."""
    try:
        with path.open("rb") as handle:
            count = 0
            for count, _ in enumerate(handle, start=1):
                if count >= _MAX_LINES_COUNTED:
                    break
            return count
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


def _declared_checks(root: Path, manifests: List[str]) -> List[List[str]]:
    """Every test entry point the repository declares, first one first.

    Codex, Run 15: a project with a ``package.json`` test script and a
    Python suite had only pytest run in-run, so its Node UI tests never
    counted toward completion.
    """
    found: List[List[str]] = []
    if "package.json" in manifests:
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) and pkg["scripts"].get("test"):
                found.append(["npm", "test", "--silent"])
        except (ValueError, OSError):
            pass
    python = _python_check(root, manifests)
    if python:
        found.append(python)
    if not found and "go.mod" in manifests:
        found.append(["go", "test", "./..."])
    if not found and "Cargo.toml" in manifests:
        found.append(["cargo", "test", "--quiet"])
    return found


def _has_python_tests(tests: Path, limit: int = 2000) -> bool:
    """Whether ``tests`` holds a Python file, consuming at most ``limit``
    directory entries in all (Codex review of 8a71d25: the listing was
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


def _python_check(root: Path, manifests: List[str]) -> Optional[List[str]]:
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
        local_python = root / '.venv' / 'bin' / 'python'
        interpreter = str(local_python) if local_python.is_file() else sys.executable
        return [interpreter, "-m", "pytest", "-q"]
    return None


def scan_repo(root) -> ScanReport:
    """One bounded, deterministic pass over the tree."""
    root = Path(root)
    report = ScanReport(root=root)
    if not root.is_dir():
        return report

    report.top_dirs = sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and p.name not in _SKIP_DIRS and not p.name.startswith(".")
    )
    report.manifests = [m for m in _MANIFESTS if (root / m).is_file()
                        and not (root / m).is_symlink()]

    sized: List[Tuple[str, int]] = []
    test_files: List[str] = []
    stack: List[Path] = [root]
    while stack and report.file_count < _MAX_FILES:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
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

    for name in ("README.md", "README.rst", "README.txt", "README"):
        candidate = root / name
        if candidate.is_file() and not candidate.is_symlink():
            try:
                head = candidate.read_text(encoding="utf-8", errors="replace")
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

    report.declared_checks = _declared_checks(root, report.manifests)
    report.check_command = report.declared_checks[0] if report.declared_checks else None
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
        path = root / relative
        if (not path.resolve().is_relative_to(root) or not path.is_file()
                or path.stat().st_size > 256_000):
            return ''
        return path.read_text(encoding='utf-8')

    if report.check_command:
        for name in ('test_runner', 'runner'):
            put(name, list(report.check_command), 'repo_scan.check_command')
    try:
        project = tomllib.loads(read('pyproject.toml'))
    except (ValueError, OSError):
        project = {}
    if project.get('project', {}).get('name') == 'quadratus' and read('quadratus/registry.py'):
        put('runtime_model_policy', 'multi-vendor', 'pyproject.toml#project.name')
        put('catalog_module', 'quadratus/registry.py', 'quadratus/registry.py')
        put('permitted_calls', ['quadratus/cli_providers.py', 'quadratus/providers.py'],
            'quadratus/registry.py')
        put('routing_audit_exemption', 'CLAUDE.md#model-routing-this-repo-is-build-time-tooling',
            'CLAUDE.md')
    for manifest in ('package.json', 'site/package.json', 'apps/app/package.json'):
        try:
            pkg = json.loads(read(manifest) or '{}')
        except (ValueError, OSError):
            continue
        deps = {**pkg.get('devDependencies', {}), **pkg.get('dependencies', {})}
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
