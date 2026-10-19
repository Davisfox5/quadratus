"""The job's obligations, evidence and remaining work, as recorded facts.

Strategy reset (Davis, 2026-10-16) and root review of d264b0c: the planner
and the requirements audit read one view built from what the harness has
recorded, never from a summary or a guess:

- each requirement's ledger status and the tasks that took it on;
- each task's state: how it closed, what it continues, whether its work is
  still owed (held), whether it was continued more than once, and its
  measured scope consumption (code files, test files, changed lines against
  its declared ceiling);
- the executed evidence: each recorded check with the cases its receipts
  name as passed, marked current only when it ran on the current source
  under the current effective gate (a different source is stale, a
  different gate is the wrong gate);
- the mandatory judgments that ended without a valid verdict;
- the capacity left after the held finish: calls, reported tokens, wall
  seconds, task slots and recovery tasks.

Everything here is pure: plain inputs in, a frozen view and its bounded
text out. It judges nothing. Whether the evidence suffices stays the
independent seats' judgment; nothing here marks a requirement met, maps
paths to requirements, or estimates a task's cost. ``dispatch_floor`` only
compares the known mandatory model stages of a task with the calls left.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional, Sequence, Tuple

__all__ = ["RequirementRow", "TaskRow", "EvidenceRow", "Capacity", "View", "build_evidence",
           "render", "dispatch_floor"]

#: Bounds on the rendered text, so the view never grows with the run.
MAX_CHECKS = 3
MAX_CASES = 5
MAX_TASKS = 12


@dataclass(frozen=True)
class RequirementRow:
    rid: str
    status: str
    owners: Tuple[str, ...]


@dataclass(frozen=True)
class TaskRow:
    task: str
    closed_as: str
    covers: Tuple[str, ...]
    continues: str = ""
    held: bool = False
    continued_by: Tuple[str, ...] = ()
    code_files: int = 0
    test_files: int = 0
    changed_lines: int = 0
    max_lines: Optional[int] = None
    open_items: int = 0


@dataclass(frozen=True)
class EvidenceRow:
    label: str
    state: str          # "current", "stale: another source", "wrong gate"
    passed: bool
    cases: Tuple[str, ...]
    case_count: int


@dataclass(frozen=True)
class Capacity:
    calls: int
    tokens: int
    wall_seconds: float
    slots_left: Optional[int] = None
    slots_total: Optional[int] = None
    #: Slots actually spent, and slots relinquished by a terminal commitment
    #: (``Session._commit_terminal``); ``slots_left`` is what is usable.
    slots_spent: Optional[int] = None
    slots_relinquished: Optional[int] = None
    recovery_left: Optional[int] = None


@dataclass(frozen=True)
class View:
    requirements: Tuple[RequirementRow, ...]
    tasks: Tuple[TaskRow, ...]
    evidence: Tuple[EvidenceRow, ...]
    judgments: Tuple[str, ...]
    capacity: Optional[Capacity] = None
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)


def build_evidence(checks: Sequence[dict], *, source: Optional[str], gate: str) -> Tuple[EvidenceRow, ...]:
    """The last ``MAX_CHECKS`` recorded checks, each with the cases its
    receipts name as passed. Current only on this source and this gate."""
    rows = []
    for number, check in enumerate(checks, 1):
        if check.get("source") is None or check.get("source") != source:
            state = "stale: another source"
        elif (check.get("gate") or "") != (gate or ""):
            state = "wrong gate"
        else:
            state = "current"
        cases: List[str] = []
        for receipt in check.get("receipts") or ():
            report = receipt.get("report") or {}
            cases.extend(report.get("passed_cases") or ())
        if not cases:
            cases.extend((check.get("report") or {}).get("passed_cases") or ())
        rows.append(EvidenceRow(label=f"check {number}", state=state, passed=bool(check.get("passed")),
                                cases=tuple(cases[:MAX_CASES]), case_count=len(cases)))
    return tuple(rows[-MAX_CHECKS:])


def _task_line(row: TaskRow) -> str:
    parts = [f"{row.task} {row.closed_as}"]
    if row.covers:
        parts.append("covers " + ",".join(row.covers))
    if row.continues:
        parts.append(f"continues {row.continues}")
    if row.held:
        parts.append("HELD: its work is still owed")
    if len(row.continued_by) > 1:
        parts.append(f"continued {len(row.continued_by)} times ({', '.join(row.continued_by)})")
    elif row.continued_by:
        parts.append(f"continued by {row.continued_by[0]}")
    size = f"{row.code_files} code / {row.test_files} test file(s), {row.changed_lines} line(s)"
    if row.max_lines:
        size += f" of {row.max_lines}"
    parts.append(size)
    if row.open_items:
        parts.append(f"{row.open_items} open item(s)")
    return "- " + "; ".join(parts)


def render(view: View) -> str:
    """The bounded text the planner and the audit both read."""
    out = ["Obligations, evidence and remaining work as the harness holds them now (recorded "
           "facts, not a summary; sufficiency is the independent seats' judgment):"]
    out.append("Requirements:")
    out.extend(f"- {r.rid}: {r.status[:160]}; taken on by {', '.join(r.owners) or 'no task yet'}"
               for r in view.requirements)
    if view.tasks:
        out.append("Tasks:")
        shown = view.tasks[-MAX_TASKS:]
        if len(view.tasks) > len(shown):
            out.append(f"- ({len(view.tasks) - len(shown)} earlier task(s) not shown)")
        out.extend(_task_line(t) for t in shown)
    if view.evidence:
        out.append("Executed evidence (cases the receipts name as passed):")
        for e in view.evidence:
            cases = ", ".join(e.cases) + (f" and {e.case_count - len(e.cases)} more"
                                          if e.case_count > len(e.cases) else "")
            out.append(f"- {e.label}: {'PASSED' if e.passed else 'FAILED'}, {e.state}; "
                       + (f"{e.case_count} passed case(s): {cases}" if e.case_count else "no case named"))
    else:
        out.append("Executed evidence: no check has run.")
    out.append("Mandatory judgments without a valid verdict: " + ("; ".join(view.judgments) or "none") + ".")
    c = view.capacity
    if c is not None:
        line = (f"Capacity left after the held finish: {max(c.calls, 0)} call(s), {max(c.tokens, 0):,} "
                f"reported tokens, {max(c.wall_seconds, 0):.0f} s")
        if c.slots_total is not None:
            line += f"; task slots usable {c.slots_left} of {c.slots_total}"
            if c.slots_spent is not None:
                line += f" ({c.slots_spent} spent"
                if c.slots_relinquished:
                    line += f", {c.slots_relinquished} relinquished by the terminal commitment"
                line += ")"
        if c.recovery_left is not None:
            line += f"; recovery tasks left {c.recovery_left}"
        out.append(line + ". A task needs one call per mandatory stage (its lead, each required review, a "
                   "required design review or security verification) before any revision.")
    out.extend(view.notes)
    return "\n".join(out)


def dispatch_floor(stages: Sequence[str], calls_left: int) -> str:
    """Why a task cannot reach its mandatory judgments, or "": one call per
    known mandatory stage, exactly counted. Never a forecast of revisions,
    fixes or retries, which may cost more."""
    if calls_left < len(stages):
        return (f"{calls_left} call(s) left after the held finish cannot pay its {len(stages)} mandatory "
                f"stage(s): {', '.join(stages)}")
    return ""
