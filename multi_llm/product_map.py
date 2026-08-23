"""The product map: a deep, verified, living reference for the codebase.

This is the operator's ruling on codebase understanding, upgraded from the
lazy scan-and-accrue posture: **no project work starts until the codebase
has been thoroughly surveyed into a reviewable document** -- the same idea
as Blitzy's tech spec, built with this system's own architecture instead of
an enterprise compute budget.

How it stays affordable on subscription windows:

* **The skeleton is mechanical and free.** File walk, area partitioning,
  and the dependency graph are parsed from the code itself -- no model, no
  tokens, and nothing to hallucinate.
* **The reading is wide, not long.** The codebase is partitioned into
  areas; each area is read in full by one model, all four vendors' windows
  in parallel. Heavily-depended-on areas get a stronger reader off the
  difficulty ladder; leaf areas get the cheap rungs.
* **Every section is verified across vendor lines** before it enters the
  map -- a different vendor's model re-reads the same files and challenges
  the claims. A survey section is testimony; cross-family verification is
  what promotes it to reference.
* **Nothing is paid for twice.** Every section records a fingerprint of the
  files it describes. Later runs re-survey only areas whose fingerprint
  changed; an untouched area keeps its verified section forever.

How it stays *living*:

* After every wave, fingerprints are re-checked; a section whose files
  changed is marked STALE in every render, and the orchestrator is required
  to resurvey it before relying on it.
* The full document is regenerated to ``product_map.md`` on every change --
  that file is the operator's reviewable spec, gated before a project
  starts, and readable at any time.
* Superseded section versions stay in the artifact store; the map file
  points at the current one. History is kept, like everything here.

The brain trust sees the overview and a section index in every prompt and
fetches any full section on demand -- an index over durable originals, the
same contract as the ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .registry import MODE_ROSTERS, resolve
from .repo_scan import _LANGUAGES, _SKIP_DIRS
from .routing import cross_family_verifier
from .task_kinds import DIFFICULTY_LADDER, TaskKind
from .task_kinds import route as route_kind

__all__ = [
    "SurveyUnit",
    "ProductMap",
    "build_units",
    "import_graph",
    "run_survey",
    "survey_estimate",
]

#: An area bigger than this is split before surveying: a reader that gets a
#: whole subsystem in one prompt skims, and a skimmed section is exactly the
#: confident-but-wrong document this survey exists to avoid.
_MAX_UNIT_LINES = 2_500

#: Cap on file content handed to one reader, after which files are truncated
#: head-first (the head carries imports, signatures, and docstrings).
_MAX_UNIT_CHARS = 80_000

#: An area this many others depend on is load-bearing enough to earn a
#: stronger reader off the ladder.
_CENTRAL_USED_BY = 3


@dataclass(frozen=True)
class SurveyUnit:
    """One survey-sized area of the codebase."""

    name: str
    #: Repo-relative file paths, sorted.
    files: tuple
    fingerprint: str
    lines: int


def _code_files(root: Path) -> List[Path]:
    out: List[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                    stack.append(entry)
            elif entry.suffix.lower() in _LANGUAGES:
                out.append(entry.relative_to(root))
    return sorted(out)


def _fingerprint(root: Path, files: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for rel in files:
        digest.update(str(rel).encode("utf-8"))
        try:
            digest.update(hashlib.sha256((root / rel).read_bytes()).digest())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()[:16]


def _line_count(root: Path, rel: Path) -> int:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace").count("\n") + 1
    except OSError:
        return 0


def build_units(root) -> List[SurveyUnit]:
    """Partition the codebase into survey-sized areas.

    Areas follow the directory structure -- that is where humans already
    drew the subsystem boundaries -- splitting one level deeper wherever an
    area exceeds the reading ceiling, and chunking as a last resort. Root
    files form their own area.
    """
    root = Path(root)
    files = _code_files(root)
    groups: Dict[str, List[Path]] = {}
    for rel in files:
        key = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        groups.setdefault(key, []).append(rel)

    units: List[SurveyUnit] = []

    def _emit(name: str, members: List[Path]) -> None:
        lines = {m: _line_count(root, m) for m in members}
        total = sum(lines.values())
        if total <= _MAX_UNIT_LINES or len(members) == 1:
            units.append(SurveyUnit(
                name=name, files=tuple(members),
                fingerprint=_fingerprint(root, members), lines=total,
            ))
            return
        # Split one directory level deeper.
        deeper: Dict[str, List[Path]] = {}
        base_depth = len(Path(name).parts) if name != "(root)" else 0
        for m in members:
            parts = m.parts
            if len(parts) > base_depth + 1:
                deeper.setdefault("/".join(parts[: base_depth + 1]), []).append(m)
            else:
                deeper.setdefault(name + " (top)", []).append(m)
        if len(deeper) > 1:
            for sub_name, sub_members in sorted(deeper.items()):
                _emit(sub_name, sub_members)
            return
        # One flat directory that is simply large: chunk by line budget.
        chunk: List[Path] = []
        chunk_lines = 0
        part = 1
        for m in members:
            chunk.append(m)
            chunk_lines += lines[m]
            if chunk_lines >= _MAX_UNIT_LINES:
                units.append(SurveyUnit(
                    name=f"{name} (part {part})", files=tuple(chunk),
                    fingerprint=_fingerprint(root, chunk), lines=chunk_lines,
                ))
                chunk, chunk_lines, part = [], 0, part + 1
        if chunk:
            units.append(SurveyUnit(
                name=f"{name} (part {part})", files=tuple(chunk),
                fingerprint=_fingerprint(root, chunk),
                lines=chunk_lines,
            ))

    for name, members in sorted(groups.items()):
        _emit(name, members)
    return units


_PY_IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)
_JS_IMPORT = re.compile(
    r"""(?:from\s+|require\()\s*['"]([^'"]+)['"]""", re.MULTILINE
)


def import_graph(root, units: Sequence[SurveyUnit]) -> Dict[str, List[str]]:
    """Which areas depend on which, parsed mechanically from the code.

    Deliberately coarse -- edges between survey areas, not a symbol graph --
    because coarse is what stays correct with a regex parser, and area-level
    "changing this touches that" is the question leads and the orchestrator
    actually ask. External imports are ignored; only edges inside the repo
    count.
    """
    root = Path(root)
    owner: Dict[str, str] = {}
    for unit in units:
        for rel in unit.files:
            owner[rel.parts[0] if len(rel.parts) > 1 else rel.stem] = unit.name
            owner.setdefault(Path(rel.parts[0]).stem, unit.name)

    edges: Dict[str, set] = {u.name: set() for u in units}
    for unit in units:
        for rel in unit.files:
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            targets: List[str] = []
            if rel.suffix == ".py":
                targets = [m.split(".")[0] for m in _PY_IMPORT.findall(text)]
            elif rel.suffix.lower() in (".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"):
                for target in _JS_IMPORT.findall(text):
                    if target.startswith("."):
                        resolved = (rel.parent / target).resolve()
                        try:
                            resolved = resolved.relative_to(root.resolve())
                            targets.append(resolved.parts[0] if resolved.parts else "")
                        except ValueError:
                            continue
            for target in targets:
                target_unit = owner.get(target)
                if target_unit and target_unit != unit.name:
                    edges[unit.name].add(target_unit)
    return {name: sorted(deps) for name, deps in edges.items()}


def _used_by(graph: Dict[str, List[str]]) -> Dict[str, List[str]]:
    reverse: Dict[str, List[str]] = {name: [] for name in graph}
    for name, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, []).append(name)
    return {name: sorted(users) for name, users in reverse.items()}


class ProductMap:
    """The stored map: verified sections, an overview, and staleness."""

    def __init__(self, path, *, root, md_path=None) -> None:
        self.path = Path(path)
        self.root = Path(root)
        self.md_path = Path(md_path) if md_path else None
        self.sections: Dict[str, dict] = {}
        self.overview: Optional[dict] = None
        self._lock = threading.Lock()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.sections = data.get("sections", {})
                self.overview = data.get("overview")
            except (ValueError, OSError):
                pass

    # -- persistence ---------------------------------------------------------
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"sections": self.sections, "overview": self.overview}, indent=1,
        ), encoding="utf-8")
        if self.md_path is not None:
            self.md_path.write_text(self.render_markdown(), encoding="utf-8")

    # -- content -------------------------------------------------------------
    def update_section(self, unit: SurveyUnit, *, content: str, author: str,
                       verifier: str, artifact_id: str) -> None:
        with self._lock:
            self.sections[unit.name] = {
                "content": content,
                "author": author,
                "verifier": verifier,
                "artifact": artifact_id,
                "fingerprint": unit.fingerprint,
                "files": [str(f) for f in unit.files],
                "lines": unit.lines,
            }
            self._save()

    def set_overview(self, *, content: str, author: str, artifact_id: str) -> None:
        with self._lock:
            self.overview = {
                "content": content, "author": author, "artifact": artifact_id,
            }
            self._save()

    # -- staleness -----------------------------------------------------------
    def stale_units(self, units: Sequence[SurveyUnit]) -> List[SurveyUnit]:
        """Areas needing (re)survey: new, or changed since their section."""
        stale = []
        for unit in units:
            section = self.sections.get(unit.name)
            if section is None or section.get("fingerprint") != unit.fingerprint:
                stale.append(unit)
        return stale

    def stale_names(self) -> List[str]:
        """Section names whose code has changed since they were written."""
        units = {u.name: u for u in build_units(self.root)}
        out = []
        for name, section in self.sections.items():
            current = units.get(name)
            if current is None or current.fingerprint != section.get("fingerprint"):
                out.append(name)
        return sorted(out)

    def refresh(self) -> List[str]:
        """Re-check fingerprints. Called at wave boundaries; deterministic."""
        with self._lock:
            self._stale_cache = self.stale_names()
            return list(self._stale_cache)

    # -- rendering -----------------------------------------------------------
    def render_markdown(self) -> str:
        """The full reviewable document -- the operator's spec sheet."""
        lines = ["# Product map", ""]
        stale = set(self.stale_names())
        if self.overview:
            lines += ["## Overview", "", self.overview["content"].strip(), ""]
        for name in sorted(self.sections):
            section = self.sections[name]
            flag = "  ⚠ STALE — code changed since this was written" \
                if name in stale else ""
            lines += [
                f"## {name}{flag}",
                f"_Surveyed by {section['author']}; verified by "
                f"{section['verifier']}; {section['lines']} lines across "
                f"{len(section['files'])} file(s); artifact "
                f"{section['artifact']}._",
                "",
                section["content"].strip(),
                "",
            ]
        if not self.sections:
            lines.append("_No survey has run yet._")
        return "\n".join(lines)

    def render_block(self) -> str:
        """The prompt-sized view: overview head plus a fetchable index.

        The full document does not ride in prompts -- an index over durable
        originals does, the same contract as the ledger. Any participant
        fetches a full section by its artifact id when a decision turns on
        the detail.
        """
        if not self.sections:
            return ""
        stale = set(getattr(self, "_stale_cache", None) or self.stale_names())
        lines = ["## Product map (the standing codebase reference)"]
        if self.overview:
            head = "\n".join(self.overview["content"].strip().splitlines()[:18])
            lines.append(head)
        index = []
        for name in sorted(self.sections):
            section = self.sections[name]
            mark = " [STALE]" if name in stale else ""
            index.append(f"- {name}{mark} -- artifact {section['artifact']}")
        lines.append(
            "Sections (reply 'FETCH: <artifact-id>' to read one in full):\n"
            + "\n".join(index)
        )
        if stale:
            lines.append(
                "STALE sections describe code that has since changed: do not "
                "rely on one -- issue a comprehend task to resurvey that "
                "area first."
            )
        return "\n\n".join(lines)


def survey_estimate(stale_count: int) -> int:
    """Model calls a survey will spend: read + verify per area, one overview."""
    return stale_count * 2 + (1 if stale_count else 0)


def _unit_content(root: Path, unit: SurveyUnit) -> str:
    budget = _MAX_UNIT_CHARS
    parts: List[str] = []
    per_file = max(2_000, budget // max(1, len(unit.files)))
    for rel in unit.files:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = "(unreadable)"
        if len(text) > per_file:
            text = text[:per_file] + "\n[...truncated for length]"
        parts.append(f"--- {rel} ---\n{text}")
    return "\n\n".join(parts)[:budget]


def _reader_prompt(unit: SurveyUnit, content: str,
                   deps: List[str], users: List[str]) -> str:
    return (
        f"You are surveying one area of a codebase to write its section of "
        f"the product map -- the standing reference document every future "
        f"task will consult. Area: {unit.name}\n\n"
        f"Mechanically-derived facts (trust these): this area imports from "
        f"{', '.join(deps) or 'nothing internal'}; it is imported by "
        f"{', '.join(users) or 'nothing internal'}.\n\n"
        f"The area's files, in full:\n\n{content}\n\n"
        "Write the reference section with exactly these headings:\n"
        "PURPOSE: what this area is for, in plain language.\n"
        "KEY COMPONENTS: each significant class/function/file and its job.\n"
        "PUBLIC INTERFACES: what other code calls or imports from here.\n"
        "BEHAVIOUR & DATA FLOW: how it works when it runs.\n"
        "CONVENTIONS: patterns a change here must follow.\n"
        "RISKS & GOTCHAS: traps, fragile spots, surprising behaviour.\n"
        "TESTS: how this area is tested, or that it is not.\n\n"
        "Ground every claim in the code shown. Where the code does not "
        "answer, write UNKNOWN rather than guessing -- a wrong reference "
        "misleads every future task; a gap invites a look."
    )


def _verifier_prompt(unit: SurveyUnit, content: str, section: str) -> str:
    return (
        f"Another vendor's model surveyed the codebase area {unit.name!r} "
        f"and wrote the reference section below. You are its verifier: "
        f"re-read the same files and challenge every checkable claim.\n\n"
        f"The files:\n\n{content}\n\n"
        f"The section under verification:\n\n{section}\n\n"
        "Reply exactly 'VERIFIED' if every claim is accurate. Otherwise "
        "list only the inaccuracies, one per line, as '- <wrong claim> -> "
        "<correction>'. Do not rewrite the section, do not add new "
        "material, do not comment on style."
    )


def _overview_prompt(product_map: ProductMap,
                     graph: Dict[str, List[str]]) -> str:
    digests = []
    for name in sorted(product_map.sections):
        head = "\n".join(
            product_map.sections[name]["content"].strip().splitlines()[:12]
        )
        digests.append(f"### {name}\n{head}")
    edges = "\n".join(
        f"- {name} -> {', '.join(deps)}"
        for name, deps in sorted(graph.items()) if deps
    )
    return (
        "Every area of this codebase has been surveyed and verified; the "
        "section digests and the mechanically-derived dependency edges are "
        "below. Write the product map's overview -- the first thing every "
        "reader sees -- with these headings:\n"
        "WHAT THIS SYSTEM IS: purpose and shape, in plain language.\n"
        "ARCHITECTURE: the areas and how they fit together.\n"
        "KEY FLOWS: the two or three paths through the system that matter "
        "most.\n"
        "BUILD, TEST, RUN: how the project is exercised.\n"
        "RISKS: the cross-cutting hazards a change anywhere should know.\n\n"
        f"Dependency edges (area -> imports from):\n{edges or '- none'}\n\n"
        f"Section digests:\n\n" + "\n\n".join(digests)
    )


def run_survey(
    root,
    *,
    invoke: Callable[..., str],
    store,
    product_map: ProductMap,
    available: Callable[[str], bool] = lambda _key: True,
    max_parallel_per_vendor: int = 4,
    progress: Callable[[str], None] = lambda _msg: None,
) -> int:
    """Survey every stale area in parallel and refresh the overview.

    Returns the number of sections written. Incremental by fingerprint: an
    unchanged area costs nothing. Each section is read by one model (ladder
    rung by how central the area is) and verified by a different vendor's
    model before it enters the map; verifier corrections are appended under
    their own attribution rather than silently merged.
    """
    root = Path(root)
    units = build_units(root)
    graph = import_graph(root, units)
    users = _used_by(graph)
    stale = product_map.stale_units(units)
    if not stale:
        return 0
    peers = MODE_ROSTERS["adversarial"]["peers"]

    gates: Dict[str, threading.Semaphore] = {}
    gate_lock = threading.Lock()

    def _gate(model_key: str) -> threading.Semaphore:
        spec = resolve(model_key)
        vendor = spec.provider if spec else model_key.split(":", 1)[0]
        with gate_lock:
            if vendor not in gates:
                gates[vendor] = threading.Semaphore(max(1, max_parallel_per_vendor))
            return gates[vendor]

    def _call(model_key: str, prompt: str) -> str:
        with _gate(model_key):
            return invoke(model_key, prompt)

    def _survey_one(unit: SurveyUnit) -> None:
        central = len(users.get(unit.name, [])) >= _CENTRAL_USED_BY
        difficulty = "standard" if central else "simple"
        reader = route_kind(
            TaskKind.COMPREHEND,
            difficulty=difficulty,
            default=DIFFICULTY_LADDER[difficulty],
            candidates=peers,
            available=available,
        )
        content = _unit_content(root, unit)
        section = _call(reader, _reader_prompt(
            unit, content, graph.get(unit.name, []), users.get(unit.name, []),
        ))
        verifier = cross_family_verifier(
            reader, candidates=peers, available=available,
        ) or reader
        verdict = _call(verifier, _verifier_prompt(unit, content, section))
        if verdict.strip().upper() != "VERIFIED":
            section += (
                f"\n\nVERIFIER CORRECTIONS ({verifier}):\n{verdict.strip()}"
            )
        ref = store.put(section, kind=f"map-section:{unit.name}", author=reader)
        product_map.update_section(
            unit, content=section, author=reader, verifier=verifier,
            artifact_id=ref.id,
        )
        progress(f"surveyed {unit.name} ({reader} read, {verifier} verified)")

    width = min(len(stale), max_parallel_per_vendor * 4)
    errors: List[BaseException] = []
    with ThreadPoolExecutor(max_workers=max(1, width)) as pool:
        for future in [pool.submit(_survey_one, u) for u in stale]:
            try:
                future.result()
            except BaseException as exc:  # noqa: BLE001 -- re-raised below
                errors.append(exc)
    if errors:
        raise errors[0]

    # The overview is rewritten whenever any section changed: it is the one
    # part whose job is the whole, so a stale overview is never acceptable.
    overview_model = route_kind(
        TaskKind.COMPREHEND,
        difficulty="complex",
        default=DIFFICULTY_LADDER["complex"],
        candidates=peers,
        available=available,
    )
    overview = _call(overview_model, _overview_prompt(product_map, graph))
    ref = store.put(overview, kind="map-overview", author=overview_model)
    product_map.set_overview(
        content=overview, author=overview_model, artifact_id=ref.id,
    )
    progress(f"overview assembled by {overview_model}")
    return len(stale)
