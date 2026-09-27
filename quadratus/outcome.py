"""Typed task and run outcomes, recorded beside the legacy decisions.

Phase 1 of the shared workflow plan (#25, 5856716643; map in
docs/workflow-map.md). Nothing here decides anything yet: the session records
what happened as typed facts at the same points where it already acts, and
:func:`parity` checks that those facts describe what the legacy decision
inputs (``open_findings``, ``checks``, ``_partial_tasks``, ``stop_reason``,
exceptions, ``completed``) decided. Phase 3 switches the decisions to read
these facts, one input at a time, and removes each legacy input it replaces.

Rules carried from the discussion (5856768605):

- A fact is history. A recovered failure (a transport error the lead
  recovered from, a gate that passed after a fix, a cap a CONTINUES task
  finished) keeps its record and stops blocking completion.
- ``primary`` is for reporting. It never licenses ignoring another active
  constraint: a latched budget, a refusal or an integrity stop still binds
  whatever ranks first.
- The requirements ledger stays authoritative for requirement and finding
  identity and settlement. Outcomes carry references to it, not a copy.
- ``legacy_route`` marks a fact classed the way today's engine routes it,
  known-wrong routes included (any failed check is a product repair today).
  Phase 3 reclassifies those from structured evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

#: Highest first. See docs/workflow-map.md section 5.
PRECEDENCE = ("refusal", "security", "integrity", "denial", "operator", "budget",
              "cap", "transport", "product", "invalid_proof", "unverified")

#: Exception class name to outcome class. Looked up along the MRO, so a
#: subclass without its own row takes its parent's. Unknown is an operator
#: handoff, never a product repair.
_EXCEPTION_CLASS = {
    "ProviderRefusal": "refusal",
    "PartialWorkStopped": "integrity",
    "PartialWorkSuspected": "integrity",
    "DependencyTreeChanged": "integrity",
    "DependencyIdentityUnavailable": "integrity",
    "CapabilityUnavailable": "denial",
    "OperatorInputNeeded": "operator",
    "RunStalled": "operator",
    "PolicyError": "operator",
    "RunBudgetExceeded": "budget",
    "TurnLimitReached": "cap",
    "WindowExhausted": "transport",
    "ProviderError": "transport",
}

#: ``stop_reason`` prefixes the session writes, to outcome class.
_STOP_PREFIX = {"TurnLimitBreaker": "cap", "FindingsUnresolved": "unverified",
                "DesignUnverified": "unverified"}


def classify(exc: BaseException) -> str:
    for klass in type(exc).__mro__:
        kind = _EXCEPTION_CLASS.get(klass.__name__)
        if kind:
            return kind
    return "operator"


def legacy_error_class(error: str) -> str:
    """The legacy ``result.json`` error string's leading name."""
    return (error or "").split(":", 1)[0].strip()


@dataclass
class Fact:
    kind: str
    detail: str
    stage: str = ""
    #: Blocks completion while active.
    terminal: bool = True
    recovered: bool = False
    #: Classed as today's engine routes it; phase 3 may reclassify.
    legacy_route: bool = False
    #: For a run stop: the legacy error name it produced ("" for a silent stop).
    legacy: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.terminal and not self.recovered


def _ranked(facts):
    order = {k: i for i, k in enumerate(PRECEDENCE)}
    return sorted(facts, key=lambda f: order.get(f.kind, len(order)))


@dataclass
class TaskOutcome:
    task_id: str
    intent: str
    lead: str = ""
    covers: List[str] = field(default_factory=list)
    resolves: List[str] = field(default_factory=list)
    continues: str = ""
    #: Stages attempted, in order; separate from the edges satisfied.
    stages: List[str] = field(default_factory=list)
    #: Mandatory edges and whether each was satisfied (None: not applicable).
    edges: Dict[str, Optional[bool]] = field(default_factory=dict)
    attempts: Dict[str, int] = field(default_factory=dict)
    checks: List[dict] = field(default_factory=list)
    evidence: Optional[dict] = None
    partial: Optional[dict] = None
    source_before: Optional[str] = None
    source_after: Optional[str] = None
    #: The run's dependency identity status when the task ended.
    dependency: Optional[str] = None
    #: Snapshot at task close, after the task's own settlement and coverage:
    #: RESOLVES finding ids still open and COVERS requirement ids not covered
    #: or met. References, not a ledger: a later DONE audit may update the
    #: ledger, which stays the authority.
    open_at_close: Dict[str, List[str]] = field(default_factory=dict)
    facts: List[Fact] = field(default_factory=list)
    #: Legacy mirror: "closed", "turn_limited" or "stopped:<Exception>".
    #: Temporary; removed in phase 3 when history reads outcomes.
    closed_as: str = "open"

    def stage(self, name: str) -> None:
        self.stages.append(name)

    def edge(self, name: str, satisfied: Optional[bool]) -> None:
        self.edges[name] = satisfied

    def note(self, kind: str, detail: str, *, stage: str = "", terminal: bool = True,
             legacy_route: bool = False) -> Fact:
        fact = Fact(kind, str(detail)[:400], stage or (self.stages[-1] if self.stages else ""),
                    terminal=terminal, legacy_route=legacy_route)
        self.facts.append(fact)
        return fact

    def recover(self, kind: str, stage: str = "") -> None:
        for fact in self.facts:
            if fact.kind == kind and fact.active and (not stage or fact.stage == stage):
                fact.recovered = True

    def count(self, attempt: str, n: int = 1) -> None:
        self.attempts[attempt] = self.attempts.get(attempt, 0) + n

    @property
    def active(self) -> List[Fact]:
        return [f for f in self.facts if f.active]

    @property
    def primary(self) -> str:
        ranked = _ranked(self.active)
        return ranked[0].kind if ranked else "clean"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["primary"] = self.primary
        data["secondary"] = [f.kind for f in _ranked(self.active)[1:]]
        return data


@dataclass
class RunOutcome:
    facts: List[Fact] = field(default_factory=list)
    done_accepted: bool = False

    def note(self, kind: str, detail: str, *, terminal: bool = True, legacy: Optional[str] = None,
             stage: str = "run") -> Fact:
        fact = Fact(kind, str(detail)[:400], stage, terminal=terminal, legacy=legacy)
        self.facts.append(fact)
        return fact

    def stop(self) -> Optional[Fact]:
        """The fact that ended the run, if one did: the last terminal one."""
        stops = [f for f in self.facts if f.active and f.legacy is not None]
        return stops[-1] if stops else None


def missing_facts(task: TaskOutcome) -> List[str]:
    """Facts a task's record must carry and does not. A closed task names
    its owner, its source identity before and after (an explicit "n/a" or
    "unavailable" counts, an absent value does not), what it changed, and
    the dependency status, and every check stage it reached left an attempt.
    A stopped task is exempt from the last rule: a check stopped by an
    integrity failure has no receipt to accept, by design. Every recorded
    attempt must point at its kept output; a lost artifact is missing, and
    its ``output_artifact_error`` says why."""
    missing = []
    if task.closed_as in ("closed", "turn_limited"):
        for name in ("lead", "source_before", "source_after", "dependency"):
            if not getattr(task, name):
                missing.append(f"{task.task_id}.{name}")
        if task.partial is None:
            missing.append(f"{task.task_id}.partial")
        if "checks" in task.stages and not task.checks:
            missing.append(f"{task.task_id}.checks")
    for check in task.checks:
        artifact = check.get("output_artifact")
        if not artifact or artifact == "unavailable":
            missing.append(f"{task.task_id}.checks[{check.get('attempt', '?')}].output_artifact")
    return missing


def typed_completed(run: RunOutcome, tasks: List[TaskOutcome], open_findings: List[str]) -> bool:
    """Completion as the typed record would decide it. ``open_findings`` are
    the ledger's open audit-finding ids: the ledger stays authoritative."""
    return (run.done_accepted and not any(f.active for f in run.facts)
            and not any(t.active for t in tasks) and not open_findings)


def primary(run: RunOutcome, tasks: List[TaskOutcome], completed: bool) -> str:
    ranked = _ranked([f for f in run.facts if f.active] + [f for t in tasks for f in t.active])
    if ranked:
        return ranked[0].kind
    return "clean" if completed else "unverified"


def parity(run: RunOutcome, tasks: List[TaskOutcome], *, open_findings: List[str],
           legacy_completed: bool, legacy_error: str, history: List[str]) -> dict:
    """Whether the typed record describes what the legacy inputs decided.

    Three checks: completion agrees; an incomplete run's legacy error name is
    the one its typed stop fact recorded (a silent legacy stop records "");
    and every task the legacy history closed has a typed outcome closed the
    same way. Observational: a disagreement is reported, never raised.
    """
    typed = typed_completed(run, tasks, open_findings)
    problems = []
    if typed != legacy_completed:
        problems.append(f"completed: typed {typed}, legacy {legacy_completed}")
    if not legacy_completed:
        stop = run.stop()
        want = legacy_error_class(legacy_error)
        if stop is None:
            problems.append(f"no typed stop fact for legacy error {want!r}")
        elif stop.legacy != want:
            problems.append(f"stop: typed {stop.legacy!r}, legacy {want!r}")
    closed = [f"{t.task_id}:{t.closed_as}" for t in tasks if t.closed_as in ("closed", "turn_limited")]
    if sorted(closed) != sorted(history):
        problems.append(f"tasks: typed {sorted(closed)}, legacy {sorted(history)}")
    missing = [m for t in tasks for m in missing_facts(t)]
    return dict(agree=not problems, problems=problems, complete=not missing, missing=missing,
                typed_completed=typed, primary=primary(run, tasks, legacy_completed))
