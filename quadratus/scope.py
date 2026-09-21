"""What a task is allowed to touch, and how far it may go.

The 2026-09-13 saved-filter trial recorded the failure this module exists to
catch: a worker handed a two-line fixture task went on to implement the rest
of the goal's interface and regression tests, hundreds of lines across files
nobody had named. Nothing was broken in a way the harness could see. The gate
passed, the close-out read well, and the actual delivered change bore no
relation to the task that was authorised.

That is not a model failure that better prompting fixes. A task with no stated
bounds has no expansion to detect, because there is nothing for the observed
change to disagree with. So a :class:`TaskScope` states four things up front:

* **permitted paths** -- where edits may land, as glob patterns.
* **intended result** -- one sentence a person can check the diff against.
* **acceptance criteria** -- how the task knows it is finished.
* **change bounds** -- roughly how large the change should be.

and :meth:`TaskScope.assess` compares them to what actually changed.

Two rules govern the response, and both come from the trial:

**Expansion is reported, never reverted.** The trial's over-wide change was
also the only work that existed; discarding it would have destroyed real
output to satisfy a bookkeeping rule. :class:`ScopeReport` is evidence for the
lead and the record, and :attr:`ScopeReport.blocking` marks only the case the
operator actually forbade -- a write outside the permitted paths. Size allows a 50 percent tolerance; the project dispatcher stops when that
tolerance is exceeded and preserves the unfinished task for review.

**Bounds are advisory to the model and checked by the harness.** Telling a
model its line ceiling helps a little; measuring the diff afterwards is what
makes the ceiling real. Both happen -- :meth:`render` goes into the prompt,
:meth:`assess` runs against the diff.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence

__all__ = ["TaskScope", "ScopeReport", "changed_paths", "count_change_lines",
           "count_change_lines_by_path", "is_test_path", "declared_signatures",
           "lint_declaration"]

#: How far past ``max_lines`` a change may drift before it is called expansion
#: rather than an estimate that ran long. A task sized at 100 lines landing at
#: 130 is normal; landing at 400 is a different task wearing the same name.
_OVERRUN_TOLERANCE = 1.5


@dataclass(frozen=True)
class ScopeReport:
    """What a task actually did, against what it said it would.

    ``blocking`` is deliberately narrow. Out-of-scope *paths* block: the
    operator named where edits may land, and a write elsewhere is the grant
    being exceeded. ``oversized`` is a separate size-budget result. The normal
    editing dispatcher stops on either result and preserves the partial work.
    Acceptance criteria are still checked by the task's reviews and gates.
    """

    within_scope: bool
    changed: List[str] = field(default_factory=list)
    out_of_scope: List[str] = field(default_factory=list)
    changed_lines: int = 0
    max_lines: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    #: ``changed_lines`` split by whether the path is a test file. The bound is
    #: a single figure and tests count in full against it; the split is
    #: reported so the next estimate can be calibrated against what actually
    #: happened, not so that either half is excused.
    code_lines: int = 0
    test_lines: int = 0
    overrun_ratio: float = _OVERRUN_TOLERANCE

    @property
    def blocking(self) -> bool:
        return bool(self.out_of_scope)

    @property
    def oversized(self) -> bool:
        return (
            self.max_lines is not None
            and self.changed_lines > self.max_lines * self.overrun_ratio
        )

    def render(self) -> str:
        if self.within_scope and not self.oversized and not self.notes:
            return (
                f"Scope check passed: {len(self.changed)} file(s), "
                f"{self.changed_lines} changed line(s)."
            )
        lines = ["Scope check:"]
        if self.out_of_scope:
            lines.append(
                "  Outside the permitted paths for this task: "
                + ", ".join(sorted(self.out_of_scope))
            )
        if self.oversized:
            # A measurement, not a diagnosis. The overrun may be an estimate
            # that missed (attempt 2 of the blind acceptance: a validator plus
            # ten named test scenarios sized at 100 lines landed at 223, all
            # inside the slice) or a task growing into the whole feature; the
            # harness cannot tell which, so it reports the split and asks.
            allowed = int(self.max_lines * self.overrun_ratio)
            lines.append(
                f"  {self.changed_lines} changed lines ({self.code_lines} in "
                f"code, {self.test_lines} in tests) against a stated bound of "
                f"~{self.max_lines}, stopped past {allowed}. Tests count in "
                f"full. Say what the estimate missed, or what grew."
            )
        for note in self.notes:
            lines.append(f"  {note}")
        if self.changed:
            lines.append("  Changed: " + ", ".join(sorted(self.changed)))
        return "\n".join(lines)


@dataclass
class TaskScope:
    """The bounds a task declares before any model is invoked.

    Attributes:
        permitted_paths: Globs, matched against project-relative POSIX paths.
            Empty means unrestricted, which is recorded as such rather than
            silently treated as "everything is fine".
        intended_result: One sentence describing the finished state.
        acceptance: How the task knows it is done. Rendered into the prompt
            and carried into the close-out.
        max_lines: Rough ceiling on changed lines.
        forbidden_paths: Globs that are never editable regardless of
            ``permitted_paths`` -- the operator's explicit exclusions.
    """

    permitted_paths: Sequence[str] = ()
    intended_result: str = ""
    acceptance: Sequence[str] = ()
    max_lines: Optional[int] = None
    forbidden_paths: Sequence[str] = ()
    overrun_ratio: float = _OVERRUN_TOLERANCE

    def permits(self, path: str) -> bool:
        """Whether ``path`` may be edited under this scope."""
        path = str(path).lstrip("./")
        if any(_matches(path, pattern) for pattern in self.forbidden_paths):
            return False
        if not self.permitted_paths:
            return True
        return any(_matches(path, pattern) for pattern in self.permitted_paths)

    def assess(self, diff: str) -> ScopeReport:
        """Compare an actual diff against what this task said it would do."""
        changed = changed_paths(diff)
        lines = count_change_lines(diff)
        by_path = count_change_lines_by_path(diff)
        test_lines = sum(n for path, n in by_path.items() if is_test_path(path))
        out = sorted(p for p in changed if not self.permits(p))
        notes: List[str] = []
        if not self.permitted_paths and not self.forbidden_paths:
            notes.append(
                "No permitted paths were declared for this task, so no path "
                "check was possible."
            )
        report = ScopeReport(
            within_scope=not out,
            changed=sorted(changed),
            out_of_scope=out,
            changed_lines=lines,
            max_lines=self.max_lines,
            notes=notes,
            code_lines=lines - test_lines,
            test_lines=test_lines,
            overrun_ratio=self.overrun_ratio,
        )
        return report

    def render(self) -> str:
        """The scope as a prompt block, so the bound is stated before the work."""
        parts = ["## Scope of this task"]
        if self.intended_result:
            parts.append(f"Intended result: {self.intended_result}")
        if self.permitted_paths:
            parts.append(
                "You may change only these paths: "
                + ", ".join(self.permitted_paths)
                + ". A change anywhere else will be rejected as out of scope."
            )
        if self.forbidden_paths:
            parts.append("Never change: " + ", ".join(self.forbidden_paths) + ".")
        if self.acceptance:
            parts.append(
                "This task is finished when:\n"
                + "\n".join(f"  - {a}" for a in self.acceptance)
            )
        if self.max_lines:
            parts.append(
                f"Expected size: about {self.max_lines} changed lines. If the "
                f"work genuinely needs substantially more, say so and stop "
                f"rather than delivering the larger change -- an undersized "
                f"task quietly growing into a whole feature is the specific "
                f"failure this bound exists to catch."
            )
        return "\n".join(parts)

    @classmethod
    def unbounded(cls) -> "TaskScope":
        """An explicit 'nobody scoped this', which reads better than None."""
        return cls()

    def to_dict(self) -> Dict[str, object]:
        return {
            "permitted_paths": list(self.permitted_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "intended_result": self.intended_result,
            "acceptance": list(self.acceptance),
            "max_lines": self.max_lines,
        }


def _matches(path: str, pattern: str) -> bool:
    """Glob match that also treats a bare directory prefix as covering it.

    ``docs`` and ``docs/`` both cover ``docs/BULK_TAGGING.md``; writing
    ``docs/**`` for every directory would be noise, and getting it wrong would
    silently widen or narrow the grant.
    """
    pattern = str(pattern).lstrip("./")
    if fnmatch.fnmatch(path, pattern):
        return True
    prefix = pattern.rstrip("/")
    if prefix and not any(ch in prefix for ch in "*?["):
        return path == prefix or path.startswith(prefix + "/")
    # Also accept a double-star directory wildcard matching zero levels.
    if "**" in pattern:
        return fnmatch.fnmatch(path, pattern.replace("**/", "*").replace("**", "*"))
    return False


def changed_paths(diff: str) -> List[str]:
    """Project-relative paths named by a unified diff, deduplicated.

    Reads ``+++``/``---`` headers rather than ``diff --git`` lines, because
    :meth:`quadratus.project.Project.diff` emits the former and not always the
    latter. ``/dev/null`` is skipped; an added file is named by its ``+++``
    side and a deleted one by its ``---`` side.
    """
    seen: List[str] = []
    for line in (diff or "").splitlines():
        if line.startswith(("Binary file changed: ", "Empty file added/deleted: ")):
            name = line.partition(": ")[2]
            if name not in seen:
                seen.append(name)
            continue
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        raw = line[4:].strip().split("\t")[0]
        if raw == "/dev/null" or not raw:
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        if raw not in seen:
            seen.append(raw)
    return seen


def count_change_lines(diff: str) -> int:
    """Added plus removed lines, excluding diff headers.

    A moved line counts twice, which overstates a pure reshuffle. Accepted:
    the number is a size signal, and a reshuffle large enough to matter is
    itself worth a look.
    """
    total = 0
    for line in (diff or "").splitlines():
        if line.startswith(("+++", "---", "@@", "diff --git", "index ")):
            continue
        if line.startswith(("+", "-")):
            total += 1
    return total


def count_change_lines_by_path(diff: str) -> Dict[str, int]:
    """:func:`count_change_lines`, attributed to the file each hunk belongs to.

    Only ``+++``/``---`` headers name files (see :func:`changed_paths`), so a
    hunk is attributed to the most recent header. Lines before any header
    are counted under ``""``; a diff that lacks headers still sums correctly.
    """
    counts: Dict[str, int] = {}
    current = ""
    for line in (diff or "").splitlines():
        if line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip().split("\t")[0]
            if raw != "/dev/null" and raw:
                current = raw[2:] if raw.startswith(("a/", "b/")) else raw
            continue
        if line.startswith(("@@", "diff --git", "index ")):
            continue
        if line.startswith(("+", "-")):
            counts[current] = counts.get(current, 0) + 1
    return counts


_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_TEST_NAMES = re.compile(
    r"^(test_.*|.*_test|.*\.test|.*\.spec|.*_spec|conftest)\.[A-Za-z0-9]+$"
)


def is_test_path(path: str) -> bool:
    """Whether a project-relative path is a test file by common convention.

    A directory named ``tests``, ``test``, ``spec`` or ``__tests__`` anywhere
    in the path, or a basename such as ``test_x.py``, ``x_test.go`` or
    ``x.spec.ts``. Conventions only: a project with tests elsewhere gets them
    counted as code, which errs toward the stricter reading.
    """
    parts = PurePosixPath(str(path).lstrip("./")).parts
    if not parts:
        return False
    if any(part in _TEST_DIRS for part in parts[:-1]):
        return True
    return bool(_TEST_NAMES.match(parts[-1]))


_DEFINED = re.compile(r"\bdef\s+([A-Za-z_][\w.]*)\s*\(")
_DEFINED_WITH_ARGS = re.compile(r"\bdef\s+([A-Za-z_][\w.]*)\s*\(([^()]*)\)")
_QUOTED = re.compile(r"`\s*(?:def\s+)?([A-Za-z_][\w.]*)\s*\(([^()`]*)\)\s*:?\s*`")


def declared_signatures(description: str, *fields: Sequence[str]) -> Dict[str, List[str]]:
    """Every signature the declaration gives each function it defines.

    A function is *declared* only where the description writes ``def name(``.
    For those names, every ``def`` line and every backticked ``name(args)``
    across the whole declaration is collected, with whitespace normalised.
    Incidental calls (``float()``, ``len(row)``) are never declared, so they
    cannot trip the check, and neither can a backticked name that is not a
    function: attempt 3 of the blind acceptance quoted the CSV columns
    ``Start (s)`` and ``End (s)`` in acceptance, which read as ``name(args)``
    but are column headings. Requiring a ``def`` keeps the check on functions.

    Returns ``{name: [distinct signatures, in order of appearance]}``. More
    than one entry for a name is the contradiction the decomposition prompt
    forbids: attempt 2 of the blind acceptance wrote
    ``def _validate_clip_row(project, cols, row)`` in a code block, revised it
    mid-description to ``(project, cols, width, row)``, and emitted acceptance
    from the first draft. The lead implemented one and was measured against
    the other.
    """
    quoted_fields = [x for group in fields for x in group]
    declared = set(_DEFINED.findall(description))
    if not declared:
        return {}
    found: Dict[str, List[str]] = {}

    def note(name: str, args: str) -> None:
        if name not in declared:
            return
        normalised = ", ".join(a.strip() for a in args.split(",") if a.strip())
        forms = found.setdefault(name, [])
        if normalised not in forms:
            forms.append(normalised)

    for name, args in _DEFINED_WITH_ARGS.findall(description):
        note(name, args)
    for text in [description, *quoted_fields]:
        for name, args in _QUOTED.findall(text):
            note(name, args)
    return found



def lint_declaration(description: str, scope: "TaskScope") -> None:
    """Deterministic checks a decomposition must pass before dispatch.

    Raises :class:`ValueError` with a message the correction round can quote.
    The prompt asks for a final description with one signature per function,
    quoted verbatim in acceptance; this is the backstop for when the
    orchestrator thinks aloud inside the description instead.
    """
    forms = declared_signatures(description, [scope.intended_result], scope.acceptance)
    for name, variants in forms.items():
        if len(variants) > 1:
            listed = "; ".join(f"{name}({v})" for v in variants)
            raise ValueError(
                f"the declaration gives {name} more than one signature ({listed}); "
                f"write the description as final text with exactly one signature "
                f"per function and quote it verbatim in acceptance"
            )


def read_scope(description: str, *, max_lines: int):
    """Require a structured declaration before normal project dispatch."""
    matches = list(re.finditer(r"^SCOPE:\s*(.+)$", description, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Declare exactly one SCOPE JSON line")
    try:
        data = json.loads(matches[0].group(1))
    except ValueError as exc:
        raise ValueError("SCOPE must contain valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("SCOPE must be an object")
    paths = data.get("permitted_paths")
    acceptance = data.get("acceptance")
    result = data.get("intended_result")
    bound = data.get("max_lines")
    if not isinstance(paths, list) or not paths:
        raise ValueError("SCOPE needs permitted_paths")
    for name in paths:
        if (not isinstance(name, str) or not name.strip() or name != name.strip()
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                or "\\" in name or name.startswith(("~", ".quadratus"))
                or name.strip("./*?[]") == ""):
            raise ValueError("SCOPE paths must be narrow project-relative paths")
    if not isinstance(result, str) or not result.strip():
        raise ValueError("SCOPE needs an intended_result")
    if (not isinstance(acceptance, list) or not acceptance
            or any(not isinstance(x, str) or not x.strip() for x in acceptance)):
        raise ValueError("SCOPE needs verifiable acceptance conditions")
    if type(bound) is not int or not 0 < bound <= max_lines:
        raise ValueError(f"SCOPE max_lines must be between 1 and {max_lines}")
    body = (description[:matches[0].start()] + description[matches[0].end():]).strip()
    scope = TaskScope(paths, result, acceptance, bound)
    lint_declaration(body, scope)
    return scope, body
