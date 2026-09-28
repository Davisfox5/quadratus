"""The completion decision candidate (map P3.4, "DONE computed twice").

Two halves. The unit controls build typed records directly and pin each
branch of both DONE sites: what completes, what is sent back, what stops and
under which exact name, and which question may be asked when. The shadow
parity cases replay real journeys through the lifecycle harness with
read-only spies on the session, then drive the candidate from the session's
own state with the answers the session actually got. The candidate must ask
exactly the questions the session asked and land on the same ``completed``,
``done_accepted`` and ``stop_reason``. Nothing here wires the candidate in.
"""

import copy
from dataclasses import asdict, replace

import pytest

from quadratus import completion_decision as C
from quadratus.completion_decision import (
    CAP,
    CHECK_REQUIREMENTS,
    COMPLETE,
    CONFIRM_GOAL,
    DONE_REPLY,
    INCOMPLETE,
    OPERATOR_STOP,
    SEND_BACK,
    VERIFY_DEPENDENCIES,
    CompletionInputs,
    FindingRef,
    LedgerSnapshot,
    RequirementsAnswer,
    decide,
    resolve,
)
from quadratus.outcome import RunOutcome, TaskOutcome

LEAD = "claude:opus"
MET = {"R1": "met (audited)"}


def task(tid="t1", *, required=None, edges=None, checks=(True,), facts=(), covers=(), resolves=(),
         continues="", intent="implementation", closed_as="closed", intended_state=None):
    """A well-formed closed task: every fact present, every required edge
    satisfied unless ``edges`` says otherwise."""
    req = dict(checks=True, design_review=False, security_verification=False, settlement=False,
               design_evidence="none", design_collaboration_applicable=False, design_instruction="none", security_verdict="none", operator_limits="none", requirements_ledger=False)
    req.update(required or {})
    contract = dict(task_id=tid, owner=LEAD, required=req)
    if intended_state is not None:
        contract["intended_state"] = intended_state
    outcome = TaskOutcome(task_id=tid, intent=intent, lead=LEAD, covers=list(covers), resolves=list(resolves),
                          continues=continues, source_before="sha:before", source_after="sha:after",
                          dependency="unchanged", partial={}, contract=contract,
                          dispatch=dict(state="dispatched", owner=LEAD), closed_as=closed_as)
    wanted = (["checks"] if req["checks"] else []) + (["verification"] if req["security_verification"] else []) \
        + (["evidence"] if req["design_evidence"] in ("harness", "self") else []) \
        + (["delivered", "reviewer"] if req["design_review"] else []) + (["settlement"] if req["settlement"] else [])
    for edge in wanted:
        outcome.edge(edge, True)
    for edge, value in (edges or {}).items():
        outcome.edge(edge, value)
    if checks:
        outcome.stage("checks")
        outcome.checks = [dict(attempt=i, passed=ok, output_artifact=f"a{i}", gate="full")
                          for i, ok in enumerate(checks, 1)]
    for kind, detail, kw in facts:
        outcome.note(kind, detail, **kw)
    return outcome


def inputs(site=DONE_REPLY, tasks=None, **kw):
    kw.setdefault("ledger", LedgerSnapshot(enabled=True, requirement_status=dict(MET)))
    kw.setdefault("run", RunOutcome())
    if site == CAP:
        kw.setdefault("max_tasks", 3)
    return CompletionInputs(site=site, tasks=tuple(tasks if tasks is not None else [task(covers=["R1"])]), **kw)


def answered(site=DONE_REPLY, tasks=None, *, goal=True, satisfied=True, refusal="", **kw):
    """Inputs with every question the order would ask already answered."""
    base = inputs(site, tasks, **kw)
    if site == CAP:
        base = replace(base, goal_confirmed=goal)
        return replace(base, requirements_satisfied=satisfied if goal else None, done_refusal=refusal)
    return replace(base, requirements_satisfied=satisfied, done_refusal=refusal, dependencies_verified=True)


def drive(inp, *, goal=True, satisfied=True, refusal="", ledger=None):
    """``resolve`` with recording callables; returns (decision, steps made)."""
    made = []

    def confirm():
        made.append(CONFIRM_GOAL)
        return goal

    def check():
        made.append(CHECK_REQUIREMENTS)
        return RequirementsAnswer(satisfied, refusal, ledger or inp.ledger)

    def verify():
        made.append(VERIFY_DEPENDENCIES)
    return resolve(inp, confirm_goal=confirm, check_requirements=check, verify_dependencies=verify), made


# -- clean completion ---------------------------------------------------------------------

def test_a_clean_done_reply_checks_requirements_then_dependencies_then_completes():
    decision, made = drive(inputs())
    assert made == [CHECK_REQUIREMENTS, VERIFY_DEPENDENCIES]
    assert decision.status == COMPLETE and decision.ready and decision.completed
    assert decision.done_accepted is True and decision.stop is None and decision.blockers == ()


def test_a_clean_cap_asks_the_goal_then_requirements_and_never_verifies_dependencies_there():
    decision, made = drive(inputs(CAP))
    assert made == [CONFIRM_GOAL, CHECK_REQUIREMENTS], "the end of run() verifies dependencies at the cap"
    assert decision.ready and decision.done_accepted is True


@pytest.mark.parametrize("site", [DONE_REPLY, CAP])
def test_with_the_ledger_off_no_requirements_step_is_asked(site):
    decision, made = drive(inputs(site, [task()], ledger=LedgerSnapshot(enabled=False)))
    assert CHECK_REQUIREMENTS not in made and decision.ready


def test_with_the_ledger_off_a_covers_reference_is_still_owed_as_today():
    """``_open_refs`` reads requirement status whether or not the ledger is on."""
    decision = decide(answered(tasks=[task(covers=["R1"])], ledger=LedgerSnapshot(enabled=False)))
    assert decision.blockers == ("owed requirement R1",)


def test_a_check_that_failed_then_passed_in_the_same_task_is_history_g12():
    decision = decide(answered(tasks=[task(covers=["R1"], checks=(False, True))]))
    assert decision.ready


def test_a_capped_task_finished_by_its_continuation_completes():
    capped = task("t1", covers=["R1"], closed_as="turn_limited", facts=[("cap", "turn limit", {})])
    capped.recover("cap")
    successor = task("t2", covers=["R1"], continues="t1")
    assert decide(answered(tasks=[capped, successor])).ready


def test_settled_audit_debt_discharges_the_audits_own_design_edges_only():
    audit = task("t1", intent="audit", required=dict(design_evidence="self", design_review=True),
                 edges=dict(evidence=False, delivered=False, reviewer=False), covers=["R1"])
    ledger = LedgerSnapshot(requirement_status=MET, findings=(FindingRef("F1", "t1", "resolved", ("R1",)),))
    assert decide(answered(tasks=[audit], ledger=ledger)).ready
    audit.edge("checks", None)
    decision = decide(answered(tasks=[audit], ledger=ledger))
    assert not decision.ready and "t1.checks unsatisfied" in decision.stop.reason


# -- missing evidence, delivery, reviewer, verification -----------------------------------

@pytest.mark.parametrize("edge", ["evidence", "delivered", "reviewer"])
@pytest.mark.parametrize("site,where", [(DONE_REPLY, "DONE"), (CAP, "the cap's goal confirmation")])
def test_a_missing_design_edge_is_completion_unproven(edge, site, where):
    design = task(required=dict(design_evidence="self", design_review=True), edges={edge: None}, covers=["R1"])
    decision = decide(answered(site, [design]))
    assert decision.status == INCOMPLETE and not decision.completed
    assert decision.stop.kind == "unverified" and decision.stop.name == "CompletionUnproven"
    assert decision.stop.reason.startswith(f"CompletionUnproven: {where} was accepted but the record does not "
                                           "support it: ")
    assert f"t1.{edge} unsatisfied" in decision.blockers
    assert decision.debt.unsatisfied == {edge: ("t1",)}
    assert decision.done_accepted is True, "today both sites have set done_accepted before the guard runs"


def test_a_missing_security_verification_edge_is_completion_unproven():
    secure = task(required=dict(security_verification=True), edges=dict(verification=False), covers=["R1"])
    decision = decide(answered(tasks=[secure]))
    assert "t1.verification unsatisfied" in decision.blockers and not decision.ready


def test_an_incomplete_record_is_completion_unproven():
    broken = task(covers=["R1"])
    broken.source_after = None
    decision = decide(answered(tasks=[broken]))
    assert "record t1.source_after" in decision.blockers


def test_an_owed_covers_requirement_is_completion_unproven():
    ledger = LedgerSnapshot(requirement_status={"R1": "met (audited)", "R2": "open"})
    decision = decide(answered(tasks=[task(covers=["R1", "R2"])], ledger=ledger))
    assert decision.blockers == ("owed requirement R2",)
    assert decision.debt.owed == ("requirement R2",)


def test_an_unknown_continues_reference_is_completion_unproven():
    decision = decide(answered(tasks=[task(covers=["R1"], continues="t9")]))
    assert "t1 CONTINUES 't9', which is not an earlier task" in decision.blockers


# -- requirements: missing, unreviewed, unmet ---------------------------------------------

NO_REQUIREMENTS = ("\n\n--- DONE SENT BACK ---\nNo requirements were listed. Before DONE can stand, reply with a "
                   "'REQUIREMENTS:' block numbering the goal's requirements (R1: ...), then name the next task "
                   "or reply DONE.")


def test_requirements_not_satisfied_send_done_back_while_the_allowance_lasts():
    decision = decide(answered(satisfied=False, refusal=NO_REQUIREMENTS, reopens=2, max_reopens=3))
    assert decision.status == SEND_BACK and decision.reopen_fact == "DONE sent back: requirements open"
    assert decision.refusal == "", "the requirements check already wrote its own refusal"
    assert decision.done_accepted is None and decision.stop is None


def test_requirements_unmet_after_the_allowance_is_the_named_stop():
    decision = decide(answered(satisfied=False, refusal=NO_REQUIREMENTS, reopens=3, max_reopens=3))
    assert decision.status == INCOMPLETE
    assert decision.stop == C.Stop("unverified", (
        "RequirementsUnmet: DONE was sent back 3 time(s) and the requirements are still not met: "
        + NO_REQUIREMENTS.replace("--- DONE SENT BACK ---", "").strip()[:400] + ". Work preserved."))


def test_requirements_unmet_with_no_refusal_text_says_the_check_did_not_pass():
    decision = decide(answered(satisfied=False, reopens=0, max_reopens=0))
    assert decision.stop.reason == ("RequirementsUnmet: DONE was sent back 0 time(s) and the requirements are "
                                    "still not met: the requirements check did not pass. Work preserved.")


def test_requirements_unmet_at_the_cap_is_named_with_the_cap():
    decision = decide(answered(CAP, satisfied=False, refusal="\n\n--- DONE SENT BACK ---\nR1 not met", max_tasks=4))
    assert decision.stop == C.Stop("unverified", ("RequirementsUnmet: the task cap (4) was reached with the goal "
                                                  "confirmed but the requirements not met: R1 not met. "
                                                  "Work preserved."))
    assert decision.done_accepted is False


def test_dependencies_are_verified_only_after_requirements_pass():
    decision, made = drive(inputs(), satisfied=False)
    assert made == [CHECK_REQUIREMENTS] and decision.status == SEND_BACK


# -- audit findings -----------------------------------------------------------------------

OPEN_F1 = LedgerSnapshot(requirement_status={"R1": "NOT MET: open finding F1"},
                         findings=(FindingRef("F1", "t1", "open", ("R1",)),))


def test_open_audit_findings_send_done_back_before_any_requirements_call():
    decision, made = drive(inputs(ledger=OPEN_F1, reopens=0))
    assert made == [] and decision.status == SEND_BACK
    assert decision.refusal == "\n\n--- DONE SENT BACK ---\nAudit findings are still open: F1."
    assert decision.reopen_fact == "DONE sent back: audit findings open"


def test_open_audit_findings_after_the_allowance_are_findings_unresolved():
    decision = decide(inputs(ledger=OPEN_F1, reopens=3))
    assert decision.stop == C.Stop("unverified", ("FindingsUnresolved: audit findings F1 are still open; the "
                                                  "reopen allowance ran out. Work preserved."))
    assert decision.annotate == ("F1",) and decision.annotate_reason == "open when the reopen allowance ran out"
    assert decision.done_accepted is None, "today this stop leaves done_accepted alone"


def test_open_audit_findings_at_the_cap_stop_without_the_goal_question():
    decision, made = drive(inputs(CAP, ledger=OPEN_F1))
    assert made == []
    assert decision.stop.reason == ("FindingsUnresolved: audit findings F1 are still open; the task cap was "
                                    "reached. Work preserved.")
    with pytest.raises(ValueError, match="forbid the goal question"):
        decide(replace(inputs(CAP, ledger=OPEN_F1), goal_confirmed=True))


# -- capped work and CONTINUES ------------------------------------------------------------

def _capped(tid="t1"):
    return task(tid, covers=["R1"], closed_as="turn_limited", facts=[("cap", "turn limit", {})])


def test_done_with_a_capped_task_never_continued_is_done_with_open_work():
    decision = decide(answered(tasks=[_capped()], legacy_partial=frozenset({"t1"})))
    assert decision.stop == C.Stop("cap", ("DoneWithOpenWork: the orchestrator reported DONE, but capped task(s) "
                                           "t1 not continued to completion. Work preserved."))
    assert decision.status == INCOMPLETE and decision.done_accepted is True


def test_the_cap_with_capped_debt_never_asks_the_goal():
    inp = inputs(CAP, [_capped()], legacy_partial=frozenset({"t1"}), max_tasks=1)
    decision, made = drive(inp)
    assert made == [], "the goal question is not asked over capped debt"
    assert decision.stop.reason == ("GoalUnconfirmedAtCap: the task cap (1) was reached, but capped task(s) t1 "
                                    "not continued to completion. Work preserved.")
    with pytest.raises(ValueError, match="forbids the goal question"):
        decide(replace(inp, goal_confirmed=True))


def test_typed_and_legacy_partial_disagreement_is_reported_for_the_session_to_record():
    decision = decide(answered(tasks=[_capped()]))
    assert decision.partial_mismatches == (("t1", "partial: typed True, legacy False"),)
    assert not decision.ready
    # Named since Codex 5864252244: the reason reads typed and legacy together.
    assert decision.divergences == ()
    assert decision.stop.reason.endswith("capped task(s) t1 not continued to completion. Work preserved.")


def test_partiality_is_not_evaluated_at_done_when_legacy_open_findings_short_circuit():
    decision = decide(answered(tasks=[_capped()], legacy_open_findings=("a reviewer finding",)))
    assert decision.partial_mismatches == () and not decision.ready


def test_the_cap_without_a_confirmed_goal_is_named():
    decision, made = drive(inputs(CAP, max_tasks=1), goal=False)
    assert made == [CONFIRM_GOAL], "requirements only after a confirmed goal"
    assert decision.stop == C.Stop("cap", ("GoalUnconfirmedAtCap: the task cap (1) was reached and the "
                                           "orchestrator did not confirm the goal met. Work preserved."))
    assert decision.done_accepted is False
    with pytest.raises(ValueError, match="only after a confirmed goal"):
        decide(replace(inputs(CAP), goal_confirmed=False, requirements_satisfied=True))


# -- checks -------------------------------------------------------------------------------

def test_a_standing_failed_check_is_done_with_open_work():
    decision = decide(answered(tasks=[task(covers=["R1"], checks=(True, False))]))
    assert decision.stop.reason == ("DoneWithOpenWork: the orchestrator reported DONE, but task t1's last check "
                                    "still fails (attempt 2, output artifact a2). Work preserved.")
    assert decision.debt.checks_failing == ("t1",)


def test_a_failing_merge_gate_is_open_work_of_its_product_class():
    run = RunOutcome()
    run.note("product", "a gate outside any task (the merge gate) failed")
    decision = decide(answered(run=run))
    assert decision.stop.kind == "product" and "the merge gate still fails" in decision.stop.reason
    assert decision.status == INCOMPLETE


def test_the_cap_asks_the_goal_even_with_a_failing_check_as_today():
    decision, made = drive(inputs(CAP, [task(covers=["R1"], checks=(False,))], max_tasks=2))
    assert made == [CONFIRM_GOAL, CHECK_REQUIREMENTS]
    assert decision.stop.name == "GoalUnconfirmedAtCap" and "last check still fails" in decision.stop.reason


# -- dominant constraints -----------------------------------------------------------------

@pytest.mark.parametrize("kind", ["refusal", "security", "integrity"])
def test_a_dominant_active_fact_names_the_open_work_stop_and_hands_off(kind):
    flagged = task(covers=["R1"], facts=[(kind, f"a live {kind} fact", {}), ("product", "also failing", {})])
    decision = decide(answered(tasks=[flagged], legacy_open_findings=(f"a live {kind} fact",)))
    assert decision.stop.kind == kind and decision.status == OPERATOR_STOP
    assert decision.debt.constraints == (kind, "product")


def test_refusal_outranks_security_and_integrity():
    flagged = task(covers=["R1"], facts=[("integrity", "i", {}), ("security", "s", {}), ("refusal", "r", {})])
    decision = decide(answered(tasks=[flagged], legacy_open_findings=("r",)))
    assert decision.debt.dominant == "refusal" and decision.stop.kind == "refusal"


def test_a_dominant_fact_reaching_the_guard_keeps_todays_name_and_is_still_a_handoff():
    flagged = task(covers=["R1"], facts=[("integrity", "evidence identity mismatch", {})])
    decision = decide(answered(tasks=[flagged]))
    assert decision.stop.name == "CompletionUnproven" and decision.stop.kind == "unverified"
    assert "t1.integrity: evidence identity mismatch" in decision.blockers
    assert decision.status == OPERATOR_STOP, "no repair: the record's dominant constraint is integrity"
    assert any("dominates" in d for d in decision.divergences)


def test_a_recovered_fact_does_not_block():
    flagged = task(covers=["R1"], facts=[("transport", "timeout", {})])
    flagged.recover("transport")
    assert decide(answered(tasks=[flagged])).ready


def test_a_run_level_active_fact_the_guard_does_not_read_is_reported_not_acted_on():
    run = RunOutcome()
    run.note("denial", "an unrouted run-level denial")
    decision = decide(answered(run=run))
    assert decision.ready, "today's answer, unchanged"
    assert decision.divergences and "denial" in decision.divergences[0]


def test_non_terminal_send_back_notes_are_history():
    run = RunOutcome()
    run.note("unverified", "DONE sent back: requirements open", terminal=False)
    decision = decide(answered(run=run))
    assert decision.ready and decision.divergences == ()


# -- the contract of the function itself --------------------------------------------------

def test_the_decision_is_pure_and_deterministic():
    tasks = [_capped(), task("t2", covers=["R1"], checks=(False,), facts=[("security", "s", {})])]
    inp = inputs(CAP, tasks, legacy_partial=frozenset({"t1"}), legacy_open_findings=("x",), ledger=OPEN_F1)
    before = [asdict(t) for t in tasks], asdict(inp.run), copy.deepcopy(inp.ledger)
    first, second = decide(inp), decide(inp)
    assert first == second
    assert ([asdict(t) for t in tasks], asdict(inp.run), inp.ledger) == before


@pytest.mark.parametrize("bad,match", [
    (dict(site="later"), "unknown completion site"),
    (dict(site=CAP, max_tasks=0), "task cap"),
    (dict(reopens=-1), "negative"),
    (dict(goal_confirmed=True), "only at the cap"),
    (dict(ledger=LedgerSnapshot(enabled=False), requirements_satisfied=False), "ledger off"),
    (dict(dependencies_verified=True), "only after the requirements check"),
])
def test_inputs_the_order_cannot_produce_are_refused(bad, match):
    with pytest.raises(ValueError, match=match):
        decide(replace(inputs(), **bad))


def test_the_driver_never_asks_a_question_twice_and_keeps_the_order():
    decision, made = drive(inputs(CAP), goal=True, satisfied=True)
    assert made == [CONFIRM_GOAL, CHECK_REQUIREMENTS] and decision.ready


def test_the_driver_reads_the_ledger_the_requirements_check_left():
    """The audit writes requirement status; the guard must read it as left."""
    stale = LedgerSnapshot(requirement_status={"R1": "covered (t1)", "R2": "covered (t1)"})
    after = LedgerSnapshot(requirement_status={"R1": "met (audited)", "R2": "NOT MET: flaky"})
    decision, _ = drive(inputs(tasks=[task(covers=["R1", "R2"])], ledger=stale), ledger=after)
    assert decision.blockers == ("owed requirement R2",)


def test_a_dependency_stop_raised_by_the_session_propagates_unchanged():
    class DependencyTreeChanged(Exception):
        pass

    def verify():
        raise DependencyTreeChanged("node_modules changed")
    with pytest.raises(DependencyTreeChanged):
        resolve(inputs(), confirm_goal=lambda: True,
                check_requirements=lambda: RequirementsAnswer(True, "", inputs().ledger),
                verify_dependencies=verify)


# -- shadow parity on whole-controller replays --------------------------------------------

@pytest.fixture
def shadow(monkeypatch):
    """Read-only spies: the session instance, and the answers it got."""
    from quadratus import session as S
    seen = dict(session=None, goal=None, requirements=None, refusal="", ledger=None, done_rounds=0)
    run, confirm, requirements, block = (S.Session.run, S.Session._confirm_goal_met,
                                         S.Session._requirements_satisfied, S.Session._findings_block_done)

    def spy_run(self, *a, **kw):
        seen["session"] = self
        return run(self, *a, **kw)

    def spy_confirm(self):
        seen["goal"] = confirm(self)
        return seen["goal"]

    def spy_requirements(self):
        result = requirements(self)
        seen.update(requirements=result, refusal=self._done_refusal, ledger=_ledger(self))
        return result

    def spy_block(self):
        # A new DONE reply: the previous round's requirements answer is gone.
        seen.update(requirements=None, refusal="", ledger=None, done_rounds=seen["done_rounds"] + 1)
        return block(self)
    for name, spy in (("run", spy_run), ("_confirm_goal_met", spy_confirm),
                      ("_requirements_satisfied", spy_requirements), ("_findings_block_done", spy_block)):
        monkeypatch.setattr(S.Session, name, spy)
    return seen


def _ledger(session):
    return LedgerSnapshot.from_parts(enabled=session.config.requirements_ledger,
                                     requirement_status=session.memory.ledger.requirement_status,
                                     findings=session.findings)


def _shadow_decision(seen, site, max_tasks):
    """The candidate, driven from the session's end state with the answers
    the session got; each callable fails if the session never asked it."""
    session = seen["session"]
    inp = C.snapshot_session(session, site=site, max_tasks=max_tasks)
    # The decision's own stop fact is what is being reproduced, not an input.
    inp = replace(inp, run=RunOutcome(facts=[f for f in session.run_outcome.facts if f.legacy is None]))
    made = []

    def confirm():
        assert seen["goal"] is not None, "the candidate asked the goal question the session did not"
        made.append(CONFIRM_GOAL)
        return seen["goal"]

    def check():
        assert seen["requirements"] is not None, "the candidate checked requirements the session did not"
        made.append(CHECK_REQUIREMENTS)
        return RequirementsAnswer(seen["requirements"], seen["refusal"], seen["ledger"])

    def verify():
        made.append(VERIFY_DEPENDENCIES)
    decision = resolve(inp, confirm_goal=confirm, check_requirements=check, verify_dependencies=verify)
    assert (CONFIRM_GOAL in made) == (seen["goal"] is not None), "the goal question parity"
    # With the ledger off the session's check returns True before any call,
    # and the candidate skips it: the same answer with nothing to make.
    asked = seen["requirements"] is not None and session.config.requirements_ledger
    assert (CHECK_REQUIREMENTS in made) == asked, "the requirements check parity"
    return decision, session


def _agrees(decision, session, error):
    assert decision.completed == session.completed
    assert bool(decision.done_accepted) == session.run_outcome.done_accepted
    assert (decision.stop.reason if decision.stop else "") == session.stop_reason == error
    if decision.stop:
        stop = session.run_outcome.stop()
        assert (decision.stop.kind, decision.stop.name) == (stop.kind, stop.legacy)


def _lifecycle():
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, Script
    return H, DECL_T1, FILES, FIXED, Script


def _then_done(first):
    def orchestrator(call, replay):
        return first if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


def test_shadow_clean_done_reply(tmp_path, monkeypatch, shadow):
    H, DECL_T1, FILES, _, Script = _lifecycle()
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3)
    decision, session = _shadow_decision(shadow, DONE_REPLY, 3)
    assert decision.ready and replay.result.completed
    _agrees(decision, session, replay.result.error)


def test_shadow_clean_cap_confirmation(tmp_path, monkeypatch, shadow):
    H, DECL_T1, FILES, _, Script = _lifecycle()
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=1)
    decision, session = _shadow_decision(shadow, CAP, 1)
    assert decision.ready
    _agrees(decision, session, replay.result.error)


def test_shadow_cap_without_a_confirmed_goal(tmp_path, monkeypatch, shadow):
    H, _, FILES, _, Script = _lifecycle()
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, max_tasks=1)
    decision, session = _shadow_decision(shadow, CAP, 1)
    assert decision.stop.name == "GoalUnconfirmedAtCap"
    _agrees(decision, session, replay.result.error)


def _capped_lead(FIXED, H):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
    return lead


def test_shadow_done_with_a_capped_task_never_continued(tmp_path, monkeypatch, shadow):
    from quadratus.config import Settings
    H, DECL_T1, FILES, FIXED, Script = _lifecycle()
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1), lead=_capped_lead(FIXED, H)),
                   files=FILES, max_tasks=4, settings=Settings(backend="cli", lead_max_turns=14))
    decision, session = _shadow_decision(shadow, DONE_REPLY, 4)
    assert decision.stop.name == "DoneWithOpenWork"
    _agrees(decision, session, replay.result.error)


def test_shadow_cap_with_a_capped_task_never_continued(tmp_path, monkeypatch, shadow):
    from quadratus.config import Settings
    H, _, FILES, FIXED, Script = _lifecycle()
    replay = H.run(tmp_path, monkeypatch, Script(lead=_capped_lead(FIXED, H)), files=FILES, max_tasks=1,
                   settings=Settings(backend="cli", lead_max_turns=14))
    decision, session = _shadow_decision(shadow, CAP, 1)
    assert shadow["goal"] is None and decision.stop.name == "GoalUnconfirmedAtCap"
    _agrees(decision, session, replay.result.error)


def test_shadow_an_unsatisfied_edge_is_completion_unproven(tmp_path, monkeypatch, shadow):
    H, DECL_T1, FILES, _, Script = _lifecycle()
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge", lambda self, name, ok: None if name == "checks" else edge(self, name, ok))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3)
    decision, session = _shadow_decision(shadow, DONE_REPLY, 3)
    assert "t1.checks unsatisfied" in decision.blockers
    _agrees(decision, session, replay.result.error)


def test_shadow_a_capped_predecessor_with_integrity_is_refused_and_handed_off(tmp_path, monkeypatch, shadow):
    from quadratus import session as S
    from quadratus.config import Settings
    from tests.lifecycle.test_lifecycle_matrix import _continuing, _finish
    H, DECL_T1, FILES, FIXED, Script = _lifecycle()
    record = S.Session._record_work

    def with_integrity(self, outcome, partial):
        record(self, outcome, partial)
        if outcome.task_id == "t1":
            outcome.note("integrity", "evidence identity mismatch")
    monkeypatch.setattr(S.Session, "_record_work", with_integrity)

    def lead(call, replay):
        return _capped_lead(FIXED, H)(call, replay) if call.task == "t1" else _finish(call, replay)
    continuing = _continuing(DECL_T1)

    def orchestrator(call, replay):
        return continuing(call, replay) if len(replay.of("orchestrator")) <= 2 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES, max_tasks=4,
                   settings=Settings(backend="cli", lead_max_turns=14))
    decision, session = _shadow_decision(shadow, DONE_REPLY, 4)
    assert decision.status == OPERATOR_STOP and decision.debt.dominant == "integrity"
    _agrees(decision, session, replay.result.error)


def test_shadow_a_continued_cap_completes(tmp_path, monkeypatch, shadow):
    from quadratus.config import Settings
    from tests.lifecycle.test_lifecycle_matrix import _continuing, _finish
    H, DECL_T1, FILES, FIXED, Script = _lifecycle()

    def lead(call, replay):
        return _capped_lead(FIXED, H)(call, replay) if call.task == "t1" else _finish(call, replay)
    continuing = _continuing(DECL_T1)

    def orchestrator(call, replay):
        return continuing(call, replay) if len(replay.of("orchestrator")) <= 2 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES, max_tasks=4,
                   settings=Settings(backend="cli", lead_max_turns=14))
    decision, session = _shadow_decision(shadow, DONE_REPLY, 4)
    assert decision.ready
    _agrees(decision, session, replay.result.error)


@pytest.mark.requirements_ledger
def test_shadow_requirements_unmet_after_the_reopen_allowance(tmp_path, shadow):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    from tests.test_requirements_ledger import PLAN
    from tests.test_requirements_ledger import Script as LedgerScript
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    script = LedgerScript([plan, "DONE", "DONE", "DONE", "DONE"],
                          audits=["R1: MET - app.py\nR2: NOT MET - no button\nR3: MET - app.py"] * 4)
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script,
                      config=SessionConfig(max_requirement_reopens=2))
    session.run(max_tasks=6)
    decision, _ = _shadow_decision(shadow, DONE_REPLY, 6)
    assert decision.stop.name == "RequirementsUnmet" and shadow["done_rounds"] == 3
    _agrees(decision, session, session.stop_reason)


def _audit(tmp_path, monkeypatch, plan, leads, **kw):
    from tests.lifecycle.test_audit_findings import _run
    return _run(tmp_path, monkeypatch, plan, leads, **kw)


@pytest.mark.requirements_ledger
def test_shadow_an_open_finding_at_the_cap(tmp_path, monkeypatch, shadow):
    from tests.lifecycle.test_audit_findings import AUDIT, REQS, WIDE, _capture
    replay = _audit(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)}, max_tasks=1)
    decision, session = _shadow_decision(shadow, CAP, 1)
    assert decision.stop.name == "FindingsUnresolved" and decision.annotate == ("F1",)
    assert [f["unresolved_reason"] for f in session.findings] == [decision.annotate_reason]
    _agrees(decision, session, replay.result.error)


@pytest.mark.requirements_ledger
def test_shadow_a_finding_reopened_at_done_runs_out_the_allowance(tmp_path, monkeypatch, shadow):
    from pathlib import Path

    from tests.lifecycle.test_audit_findings import (
        AUDIT,
        DOCS,
        REPAIR,
        REQS,
        WIDE,
        _capture,
        _repair,
    )
    H = _lifecycle()[0]

    def docs(call, replay):
        H.write(call, {"README.md": "# app\n\nchanged later\n"})
        assert Path(call.cwd).is_dir()
        return 'Documented.\nCHANGED: ["README.md"]'
    replay = _audit(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS],
                    {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": docs}, max_tasks=10)
    decision, session = _shadow_decision(shadow, DONE_REPLY, 10)
    assert decision.stop.name == "FindingsUnresolved" and "reopen allowance" in decision.stop.reason
    _agrees(decision, session, replay.result.error)


@pytest.mark.requirements_ledger
def test_shadow_audit_then_repair_completes_with_the_ledger_on(tmp_path, monkeypatch, shadow):
    from tests.lifecycle.test_audit_findings import AUDIT, REPAIR, REQS, WIDE, _capture, _repair
    replay = _audit(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                    {"t1": _capture(measured=WIDE), "t2": _repair()})
    decision, session = _shadow_decision(shadow, DONE_REPLY, 6)
    assert decision.ready and shadow["requirements"] is True
    _agrees(decision, session, replay.result.error)


@pytest.mark.requirements_ledger
def test_shadow_requirements_unmet_at_the_cap(tmp_path, monkeypatch, shadow):
    from tests.lifecycle.test_audit_findings import AUDIT, REQS, _capture
    unmet = "R1: NOT MET - no phone layout\nR2: MET - templates/index.html"
    replay = _audit(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture()}, max_tasks=1, audits=[unmet] * 4)
    decision, session = _shadow_decision(shadow, CAP, 1)
    assert decision.stop.name == "RequirementsUnmet" and "task cap (1)" in decision.stop.reason
    _agrees(decision, session, replay.result.error)
