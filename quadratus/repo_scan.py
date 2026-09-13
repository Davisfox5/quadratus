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
    #: The project's own check command, when one is recognisable. This is what
    #: the integration gate runs; None means the operator must supply one for
    #: the gate to exist.
    check_command: Optional[List[str]] = None

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
    if "package.json" in manifests:
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            if (pkg.get("scripts") or {}).get("test"):
                return ["npm", "test", "--silent"]
        except (ValueError, OSError):
            pass
    python_signals = (
        "pyproject.toml" in manifests
        or "setup.py" in manifests
        or (root / "pytest.ini").exists()
        or (root / "tests").is_dir()
    )
    if python_signals and (root / "tests").is_dir():
        local_python = root / '.venv' / 'bin' / 'python'
        interpreter = str(local_python) if local_python.is_file() else sys.executable
        return [interpreter, "-m", "pytest", "-q"]
    if "go.mod" in manifests:
        return ["go", "test", "./..."]
    if "Cargo.toml" in manifests:
        return ["cargo", "test", "--quiet"]
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

    report.check_command = _detect_check_command(root, report.manifests)
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
