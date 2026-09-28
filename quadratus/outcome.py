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

import re
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
    "EvidenceIdentityMismatch": "integrity",
    "CapabilityUnavailable": "denial",
    "OperatorInputNeeded": "operator",
    "RunStalled": "operator",
    "PolicyError": "operator",
    "CapabilityProbeFailed": "operator",
    "CheckUnattributable": "operator",
    "PreviewUnavailable": "operator",
    "RunBudgetExceeded": "budget",
    "TurnLimitReached": "cap",
    "WindowExhausted": "transport",
    "ProviderError": "transport",
}

#: ``stop_reason`` prefixes the session writes, to outcome class.
_STOP_PREFIX = {"TurnLimitBreaker": "cap", "FindingsUnresolved": "unverified",
                "DesignUnverified": "unverified", "CompletionUnproven": "unverified"}


def classify(exc: BaseException) -> str:
    for klass in type(exc).__mro__:
        kind = _EXCEPTION_CLASS.get(klass.__name__)
        if kind:
            return kind
    return "operator"


#: A worker's own declared outcomes, reported back to its lead as today;
#: named here only so they are not mistaken for unknown failures.
_ERRAND_OUTCOMES = frozenset({"FanOutExceeded", "RepeatedFailure", "ErrandToolMismatch"})


def unclassified(exc: BaseException) -> bool:
    """Whether nothing names ``exc``: no outcome row and no declared errand
    outcome along its MRO (map J27). An unknown failure is handed to the
    operator as it was raised and never turned into more model work."""
    names = {klass.__name__ for klass in type(exc).__mro__}
    return not (names & _EXCEPTION_CLASS.keys() or names & _ERRAND_OUTCOMES)


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
    #: The whole text, only when ``detail`` was cut to its bound (map P3.4).
    full: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.terminal and not self.recovered


def _cut(detail, bound: int) -> Optional[str]:
    """The whole text when ``detail`` is longer than ``bound``, else None."""
    text = str(detail)
    return text if len(text) > bound else None


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
    #: Renders actually handed to the reviewer: path to sha256, as bound.
    delivery: Optional[dict] = None
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
    #: The dispatch contract (quadratus.contract), fixed for this invocation.
    contract: Optional[dict] = None
    #: Whether the task was dispatched: {"state": "dispatched", "owner"} at
    #: the selection point, or {"state": "not_dispatched", "reason"} for a
    #: stop before it. A task with no contract says which of the two it is.
    dispatch: Optional[dict] = None
    #: Owner switches after dispatch (bounded lead recovery), in order:
    #: {"from", "to", "reason", "failure"}. The contract's owner never changes;
    #: the last "to" is the owner actually invoked.
    owner_changes: List[dict] = field(default_factory=list)
    #: Places where a legacy applicability decision disagreed with the
    #: contract's derived requirement. Recorded, never acted on (phase 2).
    mismatches: List[str] = field(default_factory=list)
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
                    terminal=terminal, legacy_route=legacy_route, full=_cut(detail, 400))
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

    def unsatisfied(self) -> List[str]:
        """The contract's mandatory edges this task did not satisfy (not
        attempted, or attempted and failed). Reporting only in phase 2."""
        if not self.contract:
            return []
        required = self.contract.get("required") or {}
        wanted = []
        if required.get("checks"):
            wanted.append("checks")
        if required.get("security_verification"):
            wanted.append("verification")
        if required.get("design_evidence") in ("harness", "self"):
            wanted.append("evidence")
        if required.get("design_review"):
            wanted += ["delivered", "reviewer"]
        if required.get("settlement"):
            wanted.append("settlement")
        return [edge for edge in wanted if self.edges.get(edge) is not True]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["primary"] = self.primary
        data["secondary"] = [f.kind for f in _ranked(self.active)[1:]]
        data["unsatisfied"] = self.unsatisfied()
        data["invoked_owner"] = self.invoked_owner
        return data

    @property
    def invoked_owner(self) -> str:
        if self.owner_changes:
            return self.owner_changes[-1].get("to", "")
        return (self.dispatch or {}).get("owner", "")


@dataclass
class RunOutcome:
    facts: List[Fact] = field(default_factory=list)
    done_accepted: bool = False
    #: Readiness probe receipts from run start (quadratus.readiness).
    readiness: List[dict] = field(default_factory=list)

    def note(self, kind: str, detail: str, *, terminal: bool = True, legacy: Optional[str] = None,
             stage: str = "run", full: Optional[str] = None) -> Fact:
        """``full``: the whole text when the caller already cut ``detail``."""
        fact = Fact(kind, str(detail)[:400], stage, terminal=terminal, legacy=legacy,
                    full=full if full is not None and full != str(detail)[:400] else _cut(detail, 400))
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
    missing = [f"{task.task_id}.contract mismatch: {m}" for m in task.mismatches]
    if task.closed_as != "open":
        missing += _dispatch_missing(task)
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


_DISPATCH_STATES = ("dispatched", "not_dispatched")
_REQUIRED_FLAGS = ("checks", "design_review", "security_verification", "settlement")
_DESIGN_EVIDENCE = ("harness", "self", "disabled", "none")
_COLLABORATION = "design_collaboration_applicable"
_INSTRUCTION = "design_instruction"
_INSTRUCTIONS = ("harness", "self", "none")
_VERDICT = "security_verdict"
_VERDICTS = ("json", "prose", "none")
_LIMITS = "operator_limits"
#: Edges an audit's ledger findings stand for: the design obligations whose
#: failure the audit recorded as requirement debt.
_AUDIT_EDGES = ("evidence", "delivered", "reviewer")
#: Edges that bind to a declared intended state, not just to "a check ran".
_STATE_EDGES = ("evidence", "delivered", "reviewer")


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _contract_missing(task: TaskOutcome) -> List[str]:
    """A contract names this task and declares every requirement with its
    type. A missing or malformed declaration is missing, never optional."""
    tid, contract = task.task_id, task.contract
    if not isinstance(contract, dict):
        return [f"{tid}.contract is not a record"]
    missing = []
    if contract.get("task_id") != tid:
        missing.append(f"{tid}.contract names task {contract.get('task_id')!r}")
    required = contract.get("required")
    if not isinstance(required, dict):
        return missing + [f"{tid}.contract.required"]
    for name in _REQUIRED_FLAGS:
        if type(required.get(name)) is not bool:
            missing.append(f"{tid}.contract.required.{name}")
    if required.get("design_evidence") not in _DESIGN_EVIDENCE:
        missing.append(f"{tid}.contract.required.design_evidence")
    # Added later (map P3.4): a record written before the field existed says
    # so, and is never read as False.
    if _COLLABORATION not in required:
        missing.append(f"{tid}.contract.required.{_COLLABORATION} (absent: recorded before this field existed)")
    elif type(required.get(_COLLABORATION)) is not bool:
        missing.append(f"{tid}.contract.required.{_COLLABORATION}")
    if _INSTRUCTION not in required:
        missing.append(f"{tid}.contract.required.{_INSTRUCTION} (absent: recorded before this field existed)")
    elif required.get(_INSTRUCTION) not in _INSTRUCTIONS:
        missing.append(f"{tid}.contract.required.{_INSTRUCTION}")
    elif required[_INSTRUCTION] == "harness" and not _nonempty(contract.get("capture_page")):
        missing.append(f"{tid}.contract.capture_page")
    if _VERDICT not in required:
        missing.append(f"{tid}.contract.required.{_VERDICT} (absent: recorded before this field existed)")
    elif required.get(_VERDICT) not in _VERDICTS:
        missing.append(f"{tid}.contract.required.{_VERDICT}")
    if _LIMITS not in required:
        missing.append(f"{tid}.contract.required.{_LIMITS} (absent: recorded before this field existed)")
    elif not (required.get(_LIMITS) == "none" or (isinstance(required.get(_LIMITS), str)
                                                 and re.fullmatch(r"sha256:[0-9a-f]{64}", required[_LIMITS]))):
        missing.append(f"{tid}.contract.required.{_LIMITS}")
    return missing


def _dispatch_missing(task: TaskOutcome) -> List[str]:
    """A dispatched task carries a well-formed contract whose owner is the
    one selected, and its invoked owner follows an unbroken chain of recorded
    switches from there. A task stopped before dispatch carries no contract
    and says why. Any other state is malformed."""
    tid, dispatch = task.task_id, task.dispatch
    if not dispatch:
        return [f"{tid}.dispatch"]
    if not isinstance(dispatch, dict) or dispatch.get("state") not in _DISPATCH_STATES:
        state = dispatch.get("state") if isinstance(dispatch, dict) else dispatch
        return [f"{tid}.dispatch.state {state!r} is not a supported state"]
    if dispatch["state"] == "not_dispatched":
        missing = [] if _nonempty(dispatch.get("reason")) else [f"{tid}.dispatch.reason"]
        if task.contract is not None:
            missing.append(f"{tid}.contract on a task that was not dispatched")
        return missing
    missing, owner = [], dispatch.get("owner")
    if not _nonempty(owner):
        missing.append(f"{tid}.dispatch.owner")
    if not task.contract:
        missing.append(f"{tid}.contract")
    else:
        missing += _contract_missing(task)
        if task.contract.get("owner") != owner:
            missing.append(f"{tid}.contract owner {task.contract.get('owner')!r} is not the dispatched "
                           f"owner {owner!r}")
    expected = owner
    for index, change in enumerate(task.owner_changes):
        if not isinstance(change, dict) or not all(_nonempty(change.get(k)) for k in ("from", "to", "reason")):
            missing.append(f"{tid}.owner_changes[{index}] malformed")
            continue
        if change.get("from") != expected:
            missing.append(f"{tid}.owner_changes from {change.get('from')!r}, expected {expected!r}")
        expected = change.get("to")
    if task.lead != expected:
        missing.append(f"{tid}.lead {task.lead!r} is not the invoked owner {expected!r}")
    return missing


def _required_edges(task: TaskOutcome) -> List[str]:
    required = (task.contract or {}).get("required") or {}
    edges = []
    if required.get("checks"):
        edges.append("checks")
    if required.get("security_verification"):
        edges.append("verification")
    if required.get("design_evidence") in ("harness", "self"):
        edges.append("evidence")
    if required.get("design_review"):
        edges += ["delivered", "reviewer"]
    if required.get("settlement"):
        edges.append("settlement")
    return edges


def completion_blockers(tasks: List[TaskOutcome], *, owed: Optional[List[str]] = None,
                        audit_findings: Optional[dict] = None) -> List[str]:
    """Why the typed record cannot count as complete, whatever the legacy
    inputs say. Empty only when every task closed with a complete, well-formed
    record, no task carries an active terminal fact, every mandatory edge was
    satisfied or factually discharged, and the ledger (``owed``, read by the
    caller) holds nothing the tasks named.

    Nothing but an unmet edge is ever discharged: missing facts, owed
    references and active facts always block. An edge is discharged only by
    fact, never by a label or a successor's own weaker contract:

    - CONTINUES: a later task in the continuation chain, recorded complete
      and with no active fact, satisfied that same edge under a contract that
      required it; for a design edge (evidence, delivered, reviewer) its
      contract also carries the predecessor's own declared intended state.
      An unknown, later or cyclic reference blocks.
    - Audit debt: an audit's design edges (evidence, delivered, reviewer)
      only, when the ledger holds findings that task recorded
      (``audit_findings``: task id to their statuses) and every one is
      resolved. Checks, verification and settlement are never discharged.
    """
    order = {t.task_id: i for i, t in enumerate(tasks)}
    by_id = {t.task_id: t for t in tasks}
    successors: dict = {}
    blockers = [f"owed {ref}" for ref in (owed or [])]
    for task in tasks:
        if not task.continues:
            continue
        if task.continues not in order or order[task.continues] >= order[task.task_id]:
            blockers.append(f"{task.task_id} CONTINUES {task.continues!r}, which is not an earlier task")
            continue
        successors.setdefault(task.continues, []).append(task.task_id)

    def chain(tid):
        """Every later task continuing ``tid``, transitively, cycle-guarded."""
        seen, stack, found = {tid}, list(successors.get(tid, [])), []
        while stack:
            nxt = stack.pop(0)
            if nxt in seen:
                continue
            seen.add(nxt)
            found.append(by_id[nxt])
            stack += successors.get(nxt, [])
        return found

    def discharged_by_continuation(task, edge) -> bool:
        state = (task.contract or {}).get("intended_state")
        for successor in chain(task.task_id):
            if (successor.closed_as not in ("closed", "turn_limited") or successor.active
                    or missing_facts(successor)):
                continue
            if successor.edges.get(edge) is not True or edge not in _required_edges(successor):
                continue
            if edge in _STATE_EDGES and (state is None
                                         or (successor.contract or {}).get("intended_state") != state):
                continue
            return True
        return False

    for task in tasks:
        tid = task.task_id
        if task.closed_as == "open":
            blockers.append(f"{tid} never closed")
            continue
        blockers += [f"record {m}" for m in missing_facts(task)]
        blockers += [f"{tid}.{fact.kind}: {fact.detail[:120]}" for fact in task.active if fact.terminal]
        statuses = list((audit_findings or {}).get(tid) or [])
        audit_settled = (task.intent == "audit" and bool(statuses)
                         and all(status == "resolved" for status in statuses))
        for edge in task.unsatisfied():
            if audit_settled and edge in _AUDIT_EDGES:
                continue
            if discharged_by_continuation(task, edge):
                continue
            blockers.append(f"{tid}.{edge} unsatisfied")
    return blockers


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
