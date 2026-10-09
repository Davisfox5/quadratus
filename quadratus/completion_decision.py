"""One completion decision for both DONE sites: a candidate, not yet wired.

Map P3.4, "DONE computed twice" (docs/workflow-map.md section 12): the DONE
reply and the cap's goal confirmation each compute their own legacy
conjunction before both end in ``_guard_completion``. This module is the one
function the retirement row asks for. Nothing in the runtime imports it yet;
the incumbent session owner wires it (docs/completion-decision-contract.md).

The decision interleaves pure checks with three effectful moves that stay in
the session: the goal question at the cap, the requirements review and audit,
and the dependency identity check at DONE. :func:`decide` never makes them.
It names the one it needs next as a ``step``; the session makes it and hands
the answer back in a new :class:`CompletionInputs`. :func:`resolve` is that
loop over caller-supplied callables, each called at most once, in the
legacy order.

Rules kept from the engine, unchanged:

- The ledger stays authoritative for requirement and finding identity and
  settlement; the snapshot carries references and statuses, not a copy of
  the engine's state that could be written back.
- The cap's goal question is asked only with no capped or audit debt, and
  requirements are checked there only after a confirmed goal.
- ``max_requirement_reopens`` and the task cap are read, never extended.
- A reply to the goal question is never executed; this module sees only
  whether it confirmed.

Parity first. The stop names, kinds and texts are the ones the session
writes today, byte for byte. Where today's decision looks wrong, the
decision says so in ``divergences`` and still returns today's answer: this
candidate carries no authority to change final DONE behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .outcome import PRECEDENCE, RunOutcome, TaskOutcome, completion_blockers, primary

#: The orchestrator replied DONE to "name the next task".
DONE_REPLY = "done_reply"
#: Every task slot was spent; the one terminal goal question.
CAP = "cap"
SITES = (DONE_REPLY, CAP)

#: Steps the session makes on the evaluator's behalf.
CONFIRM_GOAL = "confirm_goal"
CHECK_REQUIREMENTS = "check_requirements"
VERIFY_DEPENDENCIES = "verify_dependencies"

#: Decision statuses.
COMPLETE = "complete"
SEND_BACK = "send_back"
STEP = "step"
INCOMPLETE = "incomplete"
OPERATOR_STOP = "operator_stop"

#: Classes more model work cannot repair: refusal down to budget in the
#: precedence (map section 5, "stops dominate repair").
HANDOFF_KINDS = frozenset(PRECEDENCE[:PRECEDENCE.index("budget") + 1])

#: The legacy mandatory edges, grouped for the debt report.
EDGES = ("checks", "verification", "evidence", "delivered", "reviewer", "settlement")

_SENT_BACK = "--- DONE SENT BACK ---"


@dataclass(frozen=True)
class FindingRef:
    """One audit finding as the ledger holds it: identity and settlement only."""
    id: str
    task: str
    status: str
    requirements: Tuple[str, ...] = ()
    #: "check.failed" for a failed-check finding (session._record_check_debt);
    #: a measured render overflow carries its own kind.
    kind: str = ""


@dataclass(frozen=True)
class LedgerSnapshot:
    """The requirement and finding ledger as read at the decision point.

    Take it after the session's own re-check of resolved findings and, when a
    requirements check ran, after it: the audit writes requirement status and
    the review may add requirements, and the guard reads them as they are.
    """
    enabled: bool = True
    requirement_status: Mapping[str, str] = field(default_factory=dict)
    findings: Tuple[FindingRef, ...] = ()

    @classmethod
    def from_parts(cls, *, enabled: bool, requirement_status: Mapping[str, str],
                   findings: Sequence[Mapping]) -> "LedgerSnapshot":
        """From the engine's own shapes: ``config.requirements_ledger``,
        ``memory.ledger.requirement_status`` and the ``findings`` dicts."""
        return cls(enabled=bool(enabled), requirement_status=dict(requirement_status),
                   findings=tuple(FindingRef(str(f.get("id")), str(f.get("task")), str(f.get("status")),
                                             tuple(f.get("requirements") or ()), str(f.get("kind") or ""))
                                  for f in findings))

    def open_findings(self) -> List[str]:
        """Open finding ids, in ledger order (``_open_findings_for(None)``)."""
        return [f.id for f in self.findings if f.status == "open"]

    def owed(self, tasks: Sequence[TaskOutcome]) -> List[str]:
        """What the tasks' own references still owe (``_open_refs``, as the
        guard formats it): RESOLVES findings not resolved, COVERS
        requirements neither covered nor met."""
        refs = set()
        for task in tasks:
            refs.update(f"finding {f.id}" for f in self.findings
                        if f.id in task.resolves and f.status != "resolved")
            refs.update(f"requirement {rid}" for rid in task.covers
                        if not str(self.requirement_status.get(rid, "")).startswith(("covered", "met")))
        return sorted(refs)

    def audit_findings(self) -> Dict[str, List[str]]:
        """Finding statuses by the task that recorded them."""
        found: Dict[str, List[str]] = {}
        for finding in self.findings:
            found.setdefault(finding.task, []).append(finding.status)
        return found

    def check_findings(self) -> Dict[str, List[str]]:
        """Failed-check finding statuses by the task whose check failed."""
        found: Dict[str, List[str]] = {}
        for finding in self.findings:
            if finding.kind == "check.failed":
                found.setdefault(finding.task, []).append(finding.status)
        return found

    def checks_settled(self) -> set:
        """Tasks whose failed check was recorded as debt and repaired since."""
        return {tid for tid, statuses in self.check_findings().items()
                if statuses and all(s == "resolved" for s in statuses)}


@dataclass(frozen=True)
class CompletionInputs:
    """Everything one completion decision reads. Built fresh for each step.

    ``legacy_open_findings`` and ``legacy_partial`` are the engine's legacy
    mirrors (``open_findings``, ``_partial_tasks``). They are inputs because
    they still decide today; each drops out when its retirement row clears.
    """
    site: str
    tasks: Tuple[TaskOutcome, ...]
    run: RunOutcome
    ledger: LedgerSnapshot
    legacy_open_findings: Tuple[str, ...] = ()
    legacy_partial: FrozenSet[str] = frozenset()
    #: The run's task cap; required at the cap, where the stop names it.
    max_tasks: int = 0
    #: DONE send-backs spent so far, and the allowance.
    reopens: int = 0
    max_reopens: int = 3
    #: Answers to steps already made. None: not asked.
    goal_confirmed: Optional[bool] = None
    requirements_satisfied: Optional[bool] = None
    #: The refusal text the requirements check left (``_done_refusal``).
    done_refusal: str = ""
    dependencies_verified: bool = False


@dataclass(frozen=True)
class Stop:
    """A named run stop, as ``_stop_with`` records it."""
    kind: str
    reason: str

    @property
    def name(self) -> str:
        return self.reason.split(":", 1)[0].strip()


@dataclass(frozen=True)
class Debt:
    """What stands between the record and completion, by category. A report:
    the decision reads some of it, and all of it is stated."""
    #: Tasks whose last check attempt failed (G12: a later pass repairs).
    checks_failing: Tuple[str, ...] = ()
    #: A gate outside any task (the merge gate) whose product fact is active.
    merge_gate_failing: bool = False
    #: Capped or unmerged work: typed, legacy, and where they disagree.
    partial_typed: Tuple[str, ...] = ()
    partial_legacy: Tuple[str, ...] = ()
    #: Audit findings the ledger holds open.
    ledger_findings_open: Tuple[str, ...] = ()
    legacy_open_findings: Tuple[str, ...] = ()
    #: Mandatory contract edges not satisfied, before any discharge, by edge.
    unsatisfied: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    #: Ledger references the tasks still owe.
    owed: Tuple[str, ...] = ()
    #: Active terminal facts, run and tasks, highest precedence first.
    constraints: Tuple[str, ...] = ()

    @property
    def partial(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.partial_typed) | set(self.partial_legacy)))

    @property
    def partial_mismatches(self) -> Tuple[Tuple[str, str], ...]:
        typed, legacy = set(self.partial_typed), set(self.partial_legacy)
        return tuple((tid, f"partial: typed {tid in typed}, legacy {tid in legacy}")
                     for tid in sorted(typed ^ legacy))

    @property
    def dominant(self) -> str:
        return self.constraints[0] if self.constraints else "clean"


@dataclass(frozen=True)
class Decision:
    status: str
    site: str
    debt: Debt
    #: With status STEP: the one move the session makes next.
    step: Optional[str] = None
    stop: Optional[Stop] = None
    completed: bool = False
    #: The value ``run_outcome.done_accepted`` takes; None leaves it alone.
    done_accepted: Optional[bool] = None
    #: With SEND_BACK: the refusal the orchestrator is shown ("" when the
    #: requirements check already set it) and the non-terminal fact noted.
    refusal: str = ""
    reopen_fact: str = ""
    #: Findings whose record says why they stayed open, and that reason.
    annotate: Tuple[str, ...] = ()
    annotate_reason: str = ""
    #: ``(task_id, note)`` mismatches the legacy evaluation would record on
    #: tasks; empty where today's short-circuit never evaluates partiality.
    partial_mismatches: Tuple[Tuple[str, str], ...] = ()
    #: The guard's blockers, when the guard ran.
    blockers: Tuple[str, ...] = ()
    #: Where today's answer, returned here, looks wrong. Reported only.
    divergences: Tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == COMPLETE


def assess(inputs: CompletionInputs) -> Debt:
    """The debt report for ``inputs``. Pure; mutates nothing it reads."""
    tasks = inputs.tasks
    unsatisfied: Dict[str, List[str]] = {}
    for task in tasks:
        for edge in task.unsatisfied():
            unsatisfied.setdefault(edge, []).append(task.task_id)
    active = [f for f in inputs.run.facts if f.active] + [f for t in tasks for f in t.active]
    order = {k: i for i, k in enumerate(PRECEDENCE)}
    kinds = sorted({f.kind for f in active}, key=lambda k: (order.get(k, len(order)), k))
    settled = inputs.ledger.checks_settled()
    return Debt(
        checks_failing=tuple(t.task_id for t in tasks
                             if t.checks and not t.checks[-1]["passed"] and t.task_id not in settled),
        merge_gate_failing=any(f.active and f.kind == "product" for f in inputs.run.facts),
        partial_typed=tuple(sorted({t.task_id for t in tasks
                                    if any(f.active and (f.kind in ("cap", "failed") or f.stage == "merge")
                                           for f in t.facts)})),
        partial_legacy=tuple(sorted(inputs.legacy_partial)),
        ledger_findings_open=tuple(inputs.ledger.open_findings()),
        legacy_open_findings=tuple(inputs.legacy_open_findings),
        unsatisfied={edge: tuple(unsatisfied[edge]) for edge in EDGES if edge in unsatisfied},
        owed=tuple(inputs.ledger.owed(tasks)),
        constraints=tuple(kinds),
    )


def decide(inputs: CompletionInputs) -> Decision:
    """The completion decision at ``inputs.site``, or the next step it needs.

    Raises:
        ValueError: on inputs today's order could not produce -- an unknown
            site, a cap with no task count, or an answer to a question the
            order forbids asking (the goal question with capped or audit
            debt, requirements before a confirmed goal). The wiring must not
            ask early, and a guess here would hide that it did.
    """
    _validate(inputs)
    debt = assess(inputs)
    if inputs.site == DONE_REPLY:
        return _done_reply(inputs, debt)
    return _cap(inputs, debt)


def _validate(inputs: CompletionInputs) -> None:
    if inputs.site not in SITES:
        raise ValueError(f"unknown completion site {inputs.site!r}")
    if inputs.site == CAP and inputs.max_tasks < 1:
        raise ValueError("the cap site needs the run's task cap (max_tasks >= 1)")
    if inputs.reopens < 0 or inputs.max_reopens < 0:
        raise ValueError("reopen counts cannot be negative")
    if not inputs.ledger.enabled and inputs.requirements_satisfied is False:
        raise ValueError("requirements cannot fail with the ledger off; today's check returns True")
    if inputs.site == DONE_REPLY and inputs.goal_confirmed is not None:
        raise ValueError("the goal question is asked only at the cap")


def _done_reply(inputs: CompletionInputs, debt: Debt) -> Decision:
    site = DONE_REPLY
    if debt.ledger_findings_open:
        if inputs.requirements_satisfied is not None or inputs.dependencies_verified:
            raise ValueError("open audit findings send DONE back before requirements or dependencies")
        if inputs.reopens < inputs.max_reopens:
            return Decision(SEND_BACK, site, debt, reopen_fact="DONE sent back: audit findings open",
                            refusal=(f"\n\n{_SENT_BACK}\nAudit findings are still open: "
                                     f"{', '.join(debt.ledger_findings_open)}."))
        return _findings_stop(site, debt, "the reopen allowance ran out")
    satisfied = inputs.requirements_satisfied if inputs.ledger.enabled else True
    if satisfied is None:
        if inputs.dependencies_verified:
            raise ValueError("dependencies are verified at DONE only after the requirements check")
        return Decision(STEP, site, debt, step=CHECK_REQUIREMENTS)
    if not satisfied:
        if inputs.reopens < inputs.max_reopens:
            return Decision(SEND_BACK, site, debt, reopen_fact="DONE sent back: requirements open")
        return _classed(Decision(INCOMPLETE, site, debt, stop=Stop("unverified", (
            f"RequirementsUnmet: DONE was sent back {inputs.reopens} time(s) and the "
            f"requirements are still not met: {_why(inputs)}. Work preserved."))))
    if not inputs.dependencies_verified:
        return Decision(STEP, site, debt, step=VERIFY_DEPENDENCIES)
    # Today's conjunction, in its order: partiality is evaluated (and its
    # mismatches recorded) only when the legacy open-findings list is empty.
    evaluated = not debt.legacy_open_findings
    mismatches = debt.partial_mismatches if evaluated else ()
    completed = (evaluated and not debt.partial and not debt.ledger_findings_open
                 and not _checks_standing_failed(debt))
    if not completed:
        return _open_work_stop(inputs, debt, "DoneWithOpenWork", "the orchestrator reported DONE",
                               done_accepted=True, partial_mismatches=mismatches)
    return _guard(inputs, debt, "DONE", done_accepted=True, partial_mismatches=mismatches)


def _cap(inputs: CompletionInputs, debt: Debt) -> Decision:
    site = CAP
    if debt.ledger_findings_open:
        # No slot is left to repay them, so the goal question is not asked.
        if inputs.goal_confirmed is not None or inputs.requirements_satisfied is not None:
            raise ValueError("open audit findings at the cap forbid the goal question")
        return _findings_stop(site, debt, "the task cap was reached")
    mismatches = debt.partial_mismatches
    confirmed = satisfied = None
    if debt.partial:
        if inputs.goal_confirmed is not None:
            raise ValueError("capped or unmerged work at the cap forbids the goal question")
    elif inputs.goal_confirmed is None:
        return Decision(STEP, site, debt, step=CONFIRM_GOAL, partial_mismatches=mismatches)
    else:
        confirmed = inputs.goal_confirmed
    if confirmed:
        satisfied = inputs.requirements_satisfied if inputs.ledger.enabled else True
        if satisfied is None:
            return Decision(STEP, site, debt, step=CHECK_REQUIREMENTS, partial_mismatches=mismatches)
    elif inputs.requirements_satisfied is not None:
        raise ValueError("requirements are checked at the cap only after a confirmed goal")
    completed = bool(confirmed and satisfied and not debt.legacy_open_findings
                     and not _checks_standing_failed(debt))
    if completed:
        return _guard(inputs, debt, "the cap's goal confirmation", done_accepted=True,
                      partial_mismatches=mismatches)
    cap = inputs.max_tasks
    if confirmed is False:
        return _classed(Decision(INCOMPLETE, site, debt, done_accepted=False, partial_mismatches=mismatches,
                                 stop=Stop("cap", (f"GoalUnconfirmedAtCap: the task cap ({cap}) was reached and "
                                                   "the orchestrator did not confirm the goal met. "
                                                   "Work preserved."))))
    if confirmed and satisfied is False:
        return _classed(Decision(INCOMPLETE, site, debt, done_accepted=False, partial_mismatches=mismatches,
                                 stop=Stop("unverified", (
                                     f"RequirementsUnmet: the task cap ({cap}) was reached with the goal "
                                     f"confirmed but the requirements not met: {_why(inputs)}. "
                                     "Work preserved."))))
    return _open_work_stop(inputs, debt, "GoalUnconfirmedAtCap", f"the task cap ({cap}) was reached",
                           done_accepted=False, partial_mismatches=mismatches)


def _why(inputs: CompletionInputs) -> str:
    why = (inputs.done_refusal or "").replace(_SENT_BACK, "").strip()
    return why[:400] or "the requirements check did not pass"


def _checks_standing_failed(debt: Debt) -> bool:
    return bool(debt.checks_failing) or debt.merge_gate_failing


def _classed(decision: Decision) -> Decision:
    """INCOMPLETE becomes OPERATOR_STOP when the stop's kind, or the record's
    dominant active constraint, is one more model work cannot repair. The
    stop itself is unchanged: this is what the decision means, not a new name."""
    stop = decision.stop
    if stop is not None and (stop.kind in HANDOFF_KINDS or decision.debt.dominant in HANDOFF_KINDS):
        return replace(decision, status=OPERATOR_STOP)
    return decision


def _findings_stop(site: str, debt: Debt, why: str) -> Decision:
    opened = debt.ledger_findings_open
    return _classed(Decision(INCOMPLETE, site, debt, annotate=opened, annotate_reason=f"open when {why}",
                             stop=Stop("unverified", (f"FindingsUnresolved: audit findings {', '.join(opened)} "
                                                      f"are still open; {why}. Work preserved."))))


def _open_work(inputs: CompletionInputs, debt: Debt) -> List[str]:
    """``Session._open_work``, from the snapshot: what is still open."""
    reasons = []
    for task in inputs.tasks:
        if task.checks and not task.checks[-1]["passed"] and task.task_id not in debt.checks_failing:
            continue
        if task.checks and not task.checks[-1]["passed"]:
            last = task.checks[-1]
            reasons.append(f"task {task.task_id}'s last check still fails (attempt {last.get('attempt', '?')}, "
                           f"output artifact {last.get('output_artifact', 'unavailable')})")
    if debt.merge_gate_failing:
        reasons.append("the merge gate still fails")
    if debt.partial:
        # Typed and legacy together, as the blocking predicate reads them
        # (Codex, 5864252244); the same text when they agree.
        reasons.append(f"capped or failed task(s) {', '.join(debt.partial)} not continued to completion")
    if debt.ledger_findings_open:
        reasons.append(f"audit findings {', '.join(debt.ledger_findings_open)} are open")
    if debt.legacy_open_findings:
        reasons.append(f"{len(debt.legacy_open_findings)} open finding(s), first: "
                       f"{debt.legacy_open_findings[0][:160]}")
    return reasons


def _open_work_stop(inputs: CompletionInputs, debt: Debt, name: str, where: str, *,
                    done_accepted: bool, partial_mismatches) -> Decision:
    """``Session._stop_open_work``: the kind is the record's own primary."""
    kind = primary(inputs.run, list(inputs.tasks), False)
    if kind in ("clean", "unverified"):
        kind = "unverified"
    reasons = _open_work(inputs, debt)
    divergences = ()
    if not reasons:
        reasons = ["the record shows no single open item; see the task outcomes"]
        if debt.partial_typed:
            divergences = (f"typed-only partial work ({', '.join(debt.partial_typed)}) blocks completion "
                           "but is not named among the stop's reasons, which read the legacy set",)
    return _classed(Decision(INCOMPLETE, inputs.site, debt, done_accepted=done_accepted,
                             partial_mismatches=partial_mismatches, divergences=divergences,
                             stop=Stop(kind, f"{name}: {where}, but {'; '.join(reasons)[:600]}. Work preserved.")))


def _guard(inputs: CompletionInputs, debt: Debt, where: str, *, done_accepted: bool,
           partial_mismatches) -> Decision:
    """``Session._guard_completion``: the typed record must agree."""
    blockers = completion_blockers(list(inputs.tasks), owed=list(debt.owed),
                                   audit_findings=inputs.ledger.audit_findings(),
                                   check_findings=inputs.ledger.check_findings())
    divergences = tuple(f"run-level active {f.kind} fact is not a completion blocker today: {f.detail[:120]}"
                        for f in inputs.run.facts if f.active)
    if blockers:
        if debt.dominant not in ("clean", "unverified"):
            divergences += (f"the refusal is typed unverified while an active {debt.dominant} fact "
                            "dominates the record",)
        return _classed(Decision(INCOMPLETE, inputs.site, debt, done_accepted=done_accepted,
                                 partial_mismatches=partial_mismatches, blockers=tuple(blockers),
                                 divergences=divergences,
                                 stop=Stop("unverified", (f"CompletionUnproven: {where} was accepted but the "
                                                          f"record does not support it: "
                                                          f"{'; '.join(blockers)[:600]}. Work preserved."))))
    return Decision(COMPLETE, inputs.site, debt, completed=True, done_accepted=done_accepted,
                    partial_mismatches=partial_mismatches, divergences=divergences)


@dataclass(frozen=True)
class RequirementsAnswer:
    """What the session's requirements check returns: whether DONE may
    stand, the refusal text it left, and the ledger as it left it."""
    satisfied: bool
    refusal: str
    ledger: LedgerSnapshot


def resolve(inputs: CompletionInputs, *, confirm_goal: Callable[[], bool],
            check_requirements: Callable[[], RequirementsAnswer],
            verify_dependencies: Callable[[], None]) -> Decision:
    """Drive :func:`decide` to a decision, making each step through the
    caller's callable. Each is called at most once and only when the order
    asks for it; ``verify_dependencies`` raises to stop the run, as today.
    The callables are the session's; this module makes no call of its own."""
    made: List[str] = []
    while True:
        decision = decide(inputs)
        if decision.status != STEP:
            return decision
        if decision.step in made:
            raise RuntimeError(f"step {decision.step} requested twice")
        made.append(decision.step)
        if decision.step == CONFIRM_GOAL:
            inputs = replace(inputs, goal_confirmed=bool(confirm_goal()))
        elif decision.step == CHECK_REQUIREMENTS:
            answer = check_requirements()
            inputs = replace(inputs, requirements_satisfied=bool(answer.satisfied),
                             done_refusal=answer.refusal or "", ledger=answer.ledger)
        else:
            verify_dependencies()
            inputs = replace(inputs, dependencies_verified=True)


def snapshot_session(session, *, site: str, max_tasks: int = 0, **answers) -> CompletionInputs:
    """The proposed adapter: inputs read from a live ``Session``'s current
    attributes, without calling or changing anything. Duck-typed so this
    module does not import the session. ``answers`` are the step fields."""
    return CompletionInputs(
        site=site,
        tasks=tuple(session.task_outcomes),
        run=session.run_outcome,
        ledger=LedgerSnapshot.from_parts(enabled=session.config.requirements_ledger,
                                         requirement_status=session.memory.ledger.requirement_status,
                                         findings=session.findings),
        legacy_open_findings=tuple(session.open_findings),
        legacy_partial=frozenset(session._partial_tasks),
        max_tasks=max_tasks,
        reopens=answers.pop("reopens", session._requirement_reopens),
        max_reopens=answers.pop("max_reopens", session.config.max_requirement_reopens),
        **answers,
    )
