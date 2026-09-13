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
operator actually forbade -- a write outside the permitted paths. Size overrun
is loud and advisory, because a task that needed 140 lines instead of 100 is a
sizing error, not a betrayal.

**Bounds are advisory to the model and checked by the harness.** Telling a
model its line ceiling helps a little; measuring the diff afterwards is what
makes the ceiling real. Both happen -- :meth:`render` goes into the prompt,
:meth:`assess` runs against the diff.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

__all__ = ["TaskScope", "ScopeReport", "changed_paths", "count_change_lines"]

#: How far past ``max_lines`` a change may drift before it is called expansion
#: rather than an estimate that ran long. A task sized at 100 lines landing at
#: 130 is normal; landing at 400 is a different task wearing the same name.
_OVERRUN_TOLERANCE = 1.5


@dataclass(frozen=True)
class ScopeReport:
    """What a task actually did, against what it said it would.

    ``blocking`` is deliberately narrow. Out-of-scope *paths* block: the
    operator named where edits may land, and a write elsewhere is the grant
    being exceeded. Everything else -- size overrun, an unmet acceptance
    criterion -- is reported to the lead and carried into the close-out, where
    a person decides.
    """

    within_scope: bool
    changed: List[str] = field(default_factory=list)
    out_of_scope: List[str] = field(default_factory=list)
    changed_lines: int = 0
    max_lines: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.out_of_scope)

    @property
    def oversized(self) -> bool:
        return (
            self.max_lines is not None
            and self.changed_lines > self.max_lines * _OVERRUN_TOLERANCE
        )

    def render(self) -> str:
        if self.within_scope and not self.notes:
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
            lines.append(
                f"  {self.changed_lines} changed lines against a stated bound "
                f"of ~{self.max_lines}. This is the shape of a task expanding "
                f"into the whole feature; say what grew and why."
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
    # fnmatch's ``*`` does not cross ``/``; a ``**`` pattern should.
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
