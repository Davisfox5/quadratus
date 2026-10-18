"""Typed delivery records and pure transitions (blueprint stage A, records).

These exercise the records alone. Nothing here is wired into the controller
yet, and none of these tests claims the three measured failures of the
controller-reset job are fixed; they pin what the records refuse so the
wiring cannot loosen it.
"""

import pytest

from quadratus import delivery as D
from quadratus.judgment import INVALID, INVALIDATED, RESOLVED, UNRESOLVED, recheck_verdicts

SRC_A = "a" * 64
SRC_B = "b" * 64


# --- requirement revisions ------------------------------------------------

def test_requirement_revision_origin_decides_approval_and_text_digest():
    said = D.RequirementRevision("R1", 1, "IDs are positive integers", "user-said")
    inferred = D.RequirementRevision("R2", 1, "report stays a CSV", "inferred")
    accepted = D.RequirementRevision("R2", 2, "report stays a CSV", "inferred", decision_receipt="B1@v3")
    assert said.approved and not inferred.approved and accepted.approved
    assert inferred.origin == accepted.origin == "inferred"
    assert said.text_digest == D.RequirementRevision("R1", 2, "  IDs are positive integers ", "user-said").text_digest
    assert said.to_dict()["approved"] is True


@pytest.mark.parametrize("bad", [
    dict(rid="r1"), dict(revision=0), dict(revision=True), dict(text=" "), dict(origin="guessed"),
    dict(brief_version=-1),
])
def test_requirement_revision_rejects_malformed_rows(bad):
    row = dict(rid="R1", revision=1, text="x", origin="user-said")
    row.update(bad)
    with pytest.raises(ValueError):
        D.RequirementRevision(**row)


# --- findings -------------------------------------------------------------

B1, B2 = "bind-" + "1" * 60, "bind-" + "2" * 60


def _finding(fid="RF1", basis=("requirement", "R3@1"), impact="correctness", source=SRC_A, author="claude:opus"):
    return D.Finding(fid, author, "collaborator", f"{fid} claim", D.FindingBasis(*basis), impact, source, task="t2")


#: What the record resolves for these probes: one project test file and
#: one recorded case; any other reference is not on the record.
ON_RECORD = ("tests/test_app.py", "test_cycle")


def _resolves(why):
    return any(ref in why for ref in ON_RECORD)


def _apply(findings, text, *, author="claude:opus", role="recheck", binding=B1, judged=None, source=SRC_B,
           resolve=_resolves):
    reading = recheck_verdicts(text, [f.fid for f in findings])
    return D.apply_reading(findings, reading, author=author, role=role, binding=binding,
                           judged_binding=judged or binding, source_digest=source, call="c7", resolve=resolve)


def test_finding_basis_needs_a_reference_unless_none():
    with pytest.raises(ValueError):
        D.FindingBasis("reproduction", "")
    assert D.FindingBasis("none").ref == ""
    with pytest.raises(ValueError):
        _finding(impact="severe")


def test_fixed_findings_close_while_the_new_one_opens_with_its_basis_and_impact():
    findings = (_finding("RF1"), _finding("RF2"), _finding("RF3"))
    after = _apply(findings, "RF1: RESOLVED fixed in store.py\nRF2: RESOLVED\nRF3: RESOLVED\n"
                             "NEW: dispatch parses 1_0 as 10 | basis: R10 | impact: correctness\n"
                             "NEW: lock held across the CLI call | impact: data-loss\nNOTE: rename helper")
    assert [f.state for f in after[:3]] == [RESOLVED, RESOLVED, RESOLVED]
    assert after[3].fid == "RF4" and after[3].basis == D.FindingBasis("requirement", "R10")
    assert after[3].impact == "correctness" and not after[3].audit_carried and not after[3].holds_effects
    assert after[4].fid == "RF5" and after[4].basis.kind == "none" and after[4].holds_effects
    assert after[4].impact == "data-loss" and not after[4].audit_carried
    assert after[0].history[0].call == "c7" and after[0].history[0].binding == B1


def test_unresolved_wins_duplicates_and_missing_settle_nothing_foreign_ignored():
    findings = (_finding("RF1"), _finding("RF2"), _finding("RF3"), _finding("RF4"))
    reading = recheck_verdicts(
        "RF1: RESOLVED\nRF1: UNRESOLVED still fails on bool\nRF2: RESOLVED\nRF2: RESOLVED again\n"
        "RF9: RESOLVED\nI think RF4 is RESOLVED now.", ["RF1", "RF2", "RF3", "RF4"])
    assert reading.foreign == ("RF9",)
    after = D.apply_reading(findings, reading, author="claude:opus", role="recheck", binding=B1, judged_binding=B1)
    assert after[0].state == UNRESOLVED
    assert after[1].state == "open" and after[1].history[-1].state == "unjudged"
    assert after[2].history[-1].why == "no line judged it"
    assert after[3].history[-1].state == "unjudged"
    assert reading.unjudged == ("RF2", "RF3", "RF4")


def test_resolved_needs_no_source_change_but_is_bound_to_what_the_judge_saw():
    after = _apply((_finding("RF1"),), "RF1: RESOLVED the setup was wrong, the test passes unchanged", source=SRC_A)
    assert after[0].state == RESOLVED and after[0].history[-1].binding == B1
    drifted = _apply((_finding("RF1"),), "RF1: RESOLVED", binding=B2, judged=B1)
    assert drifted[0].state == "open" and "bound identity changed" in drifted[0].history[-1].why


def test_only_the_designated_judge_settles_a_finding():
    independent = _finding("RF1", author="claude:opus")
    by_lead = _apply((independent,), "RF1: RESOLVED", author="openai:gpt-5.6-sol", role="recheck")
    assert by_lead[0].state == "open" and "does not judge it" in by_lead[0].history[-1].why
    as_lead_role = _apply((independent,), "RF1: RESOLVED", author="claude:opus", role="lead")
    assert as_lead_role[0].state == "open" and "not a judging role" in as_lead_role[0].history[-1].why
    carried = _finding("RF2", basis=("none", ""), impact="other")
    by_audit = _apply((carried,), "RF2: INVALIDATED tests/test_app.py covers the path", author="openai:gpt-5.6-sol",
                      role="audit")
    assert by_audit[0].state == "invalidated"
    not_carried = _apply((independent,), "RF1: INVALIDATED tests/test_app.py", author="openai:gpt-5.6-sol",
                         role="audit")
    assert not_carried[0].state == "open" and "not carried to the audit" in not_carried[0].history[-1].why


def test_invalidated_needs_checkable_evidence_and_then_closes():
    findings = (_finding("RF1"), _finding("RF2"))
    reading = recheck_verdicts("RF1: INVALIDATED it is fine really\nRF2: INVALIDATED test_cycle covers it, run 3 passed",
                               ["RF1", "RF2"])
    assert reading.dispositions["RF1"].state == INVALID
    after = D.apply_reading(findings, reading, author="claude:opus", role="recheck", binding=B1, judged_binding=B1,
                            resolve=_resolves)
    assert after[0].state == "open" and after[1].state == "invalidated" and after[1].closed
    # The reference must be on the record: a token that merely looks like one
    # (root's finding on f4fe5b7: ``UTF8``, a backticked word) closes nothing,
    # and with no resolver at all nothing is invalidated.
    looks = recheck_verdicts("RF1: INVALIDATED `UTF8` handling covers it", ["RF1"])
    assert looks.dispositions["RF1"].state == INVALIDATED
    unresolved = D.apply_reading(findings[:1], looks, author="claude:opus", role="recheck", binding=B1,
                                 judged_binding=B1, resolve=_resolves)
    assert unresolved[0].state == "open" and "not on the record" in unresolved[0].history[-1].why
    blind = D.apply_reading(findings[1:], reading, author="claude:opus", role="recheck", binding=B1, judged_binding=B1)
    assert blind[0].state == "open" and "not on the record" in blind[0].history[-1].why


def test_immediate_risk_holds_effects_whatever_its_basis_and_is_not_audit_only():
    risky = _finding("RF1", basis=("none", ""), impact="data-loss")
    assert risky.holds_effects and not risky.audit_carried
    plain = _finding("RF2", basis=("none", ""), impact="other")
    assert plain.audit_carried and not plain.holds_effects
    cited = _finding("RF3", basis=("reproduction", "pytest -k cycle: 1 failed"), impact="correctness")
    assert not cited.audit_carried and not cited.holds_effects and cited.state == "open"


def test_finding_round_trips_through_dicts_and_lines():
    line = "dispatch parses 1_0 as 10 | basis: `pytest -k ids` | impact: Data loss"
    made = D.finding_from_line("RF7", line, author="claude:opus", role="collaborator", source_digest=SRC_A, task="t1")
    assert made.basis.kind == "reproduction" and made.impact == "data-loss" and made.claim.startswith("dispatch")
    judged = _apply((made,), "RF7: UNRESOLVED still")
    back = D.Finding.from_dict(judged[0].to_dict())
    assert back == judged[0] and back.state == UNRESOLVED


def test_apply_reading_requires_bindings_and_leaves_unmentioned_findings_alone():
    reading = recheck_verdicts("RF1: RESOLVED", ["RF1"])
    with pytest.raises(ValueError):
        D.apply_reading((_finding("RF1"),), reading, author="x", role="recheck", binding=" ", judged_binding=B1)
    after = D.apply_reading((_finding("RF1"), _finding("RF7")), reading, author="claude:opus", role="recheck",
                            binding=B1, judged_binding=B1)
    assert after[1].history == ()


# --- evidence obligations -------------------------------------------------

CURRENT = {"R8": 1, "R9": 1}


def _obligation(**over):
    base = dict(oid="O1", behavior="real HTTP server answers POST /tickets/{id}/dependencies",
                requirements=(("R8", 1), ("R9", 1)), gate="g1", runner="loopback-http", environment="env-a",
                policy="review/1", cases=("test_real_handler_rejects_bad_json_and_deletes",))
    base.update(over)
    return D.EvidenceObligation(**base)


def _receipt(**over):
    base = dict(id="rcpt-1", source=SRC_A, gate="g1", runner="loopback-http", environment="env-a",
                requirements=[["R8", 1], ["R9", 1]], completed=True, passed=True,
                passed_cases=["test_real_handler_rejects_bad_json_and_deletes"], skipped_cases=[])
    base.update(over)
    return base


def test_obligation_without_runner_is_a_prerequisite_not_a_pass():
    ob = _obligation(runner=None)
    assert not ob.executable and ob.execution == "missing" and not ob.established
    out = D.discharge(ob, _receipt(), current_source=SRC_A, current_requirements=CURRENT)
    assert not out.accepted and out.obligation.execution == "invalid"
    with pytest.raises(ValueError):
        _obligation(runner=" ")


def test_matching_receipt_discharges_execution_only():
    out = D.discharge(_obligation(), _receipt(), current_source=SRC_A, current_requirements=CURRENT)
    assert out.accepted and out.obligation.execution == "ran" and out.obligation.receipts == ("rcpt-1",)
    assert out.obligation.sufficiency == "unjudged" and not out.obligation.established


@pytest.mark.parametrize("over, reason", [
    (dict(source=SRC_B), "current source"),
    (dict(gate="g2"), "another gate"),
    (dict(runner="lead-sandbox"), "another runner"),
    (dict(environment="env-b"), "another environment"),
    (dict(requirements=[["R8", 2], ["R9", 1]]), "other requirement revisions"),
    (dict(completed=False), "did not complete"),
    (dict(passed_cases=[], skipped_cases=["test_real_handler_rejects_bad_json_and_deletes"]), "skipped"),
    (dict(passed_cases=["test_other_name"]), "not among the passed cases"),
])
def test_mismatched_receipt_is_invalid_and_names_the_first_reason(over, reason):
    out = D.discharge(_obligation(), _receipt(**over), current_source=SRC_A, current_requirements=CURRENT)
    assert not out.accepted and out.obligation.execution == "invalid"
    assert reason in out.reason and out.obligation.receipts == ()


def test_a_receipt_on_a_superseded_requirement_revision_is_invalid():
    out = D.discharge(_obligation(), _receipt(), current_source=SRC_A, current_requirements={"R8": 2, "R9": 1})
    assert not out.accepted and "no longer current" in out.reason
    stale = D.refresh(_obligation(execution="ran", source_digest=SRC_A), current_source=SRC_A,
                      current_requirements={"R8": 2, "R9": 1})
    assert stale.execution == "stale" and "revision changed" in stale.reason
    moved = D.refresh(_obligation(execution="ran", source_digest=SRC_A), current_source=SRC_B,
                      current_requirements=CURRENT)
    assert moved.execution == "stale" and "another source" in moved.reason


def test_failed_matching_run_records_failed():
    out = D.discharge(_obligation(), _receipt(passed=False, passed_cases=[]), current_source=SRC_A,
                      current_requirements=CURRENT)
    assert out.accepted and out.obligation.execution == "failed"


def test_sufficiency_is_bound_to_exactly_the_evidence_it_judged():
    ran = D.discharge(_obligation(), _receipt(), current_source=SRC_A, current_requirements=CURRENT).obligation
    with pytest.raises(ValueError):
        D.judge_sufficiency(ran, "sufficient", judged_by="harness")
    with pytest.raises(ValueError):
        D.judge_sufficiency(_obligation(), "sufficient", judged_by="claude:opus")
    judged = D.judge_sufficiency(ran, "sufficient", judged_by="claude:opus")
    assert judged.established and judged.judged_by == "claude:opus" and judged.sufficiency_current
    again = D.discharge(judged, _receipt(id="rcpt-2", source=SRC_B), current_source=SRC_B,
                        current_requirements=CURRENT).obligation
    assert again.execution == "ran" and again.sufficiency == "sufficient"
    assert not again.sufficiency_current and not again.established, "a new execution needs a fresh judgment"
    assert D.judge_sufficiency(_obligation(), "insufficient", judged_by="openai:gpt-5.6-sol").sufficiency == "insufficient"
    back = D.EvidenceObligation.from_dict(judged.to_dict())
    assert back == judged and back.established


def test_obligation_rejects_malformed_rows():
    with pytest.raises(ValueError):
        _obligation(requirements=())
    with pytest.raises(ValueError):
        _obligation(requirements=(("R8", True),))
    with pytest.raises(ValueError):
        _obligation(execution="done")
    with pytest.raises(ValueError):
        _obligation(sufficiency="sufficient")


# --- operations -----------------------------------------------------------

def _op(**over):
    base = dict(opid="O-1", purpose="lead draft t2", owner="openai:gpt-5.6-sol",
                identity={"purpose": "draft", "role": "lead", "policy": "p1", "input": "d" * 8},
                allowed_effects=("project-write",), reservation="ticket-13")
    base.update(over)
    return D.OperationRecord(**base)


def test_operation_starts_only_with_a_reservation_and_settles_on_a_result():
    with pytest.raises(ValueError):
        D.start_operation(_op(reservation=None))
    started = D.start_operation(_op())
    assert started.status == "started" and started.unsettled
    with pytest.raises(ValueError):
        D.settle_operation(started, " ")
    settled = D.settle_operation(started, "r" * 64)
    assert settled.status == "settled" and not settled.unsettled
    with pytest.raises(ValueError):
        D.start_operation(settled)


def test_started_without_result_is_uncertain_not_absent_and_not_restartable():
    started = D.start_operation(_op())
    unsure = D.uncertain_operation(started, "process died before the receipt")
    assert unsure.status == "uncertain" and unsure.unsettled
    with pytest.raises(ValueError):
        D.start_operation(unsure)
    with pytest.raises(ValueError):
        D.settle_operation(unsure, "r" * 64)


def test_operation_identity_names_purpose_role_policy_and_input():
    with pytest.raises(ValueError):
        _op(identity={"purpose": "draft", "role": "lead", "policy": "p1"})
    same = _op(opid="O-2", reservation="ticket-14")
    assert same.dedupe_key == _op().dedupe_key
    assert _op(identity={"purpose": "draft", "role": "collaborator", "policy": "p1",
                         "input": "d" * 8}).dedupe_key != _op().dedupe_key


# --- the remaining path ---------------------------------------------------

EXPOSURES = {"editing": 500_000, "reserve": 200_000}


def test_last_wave_replay_cannot_pay_a_write_revision_plus_the_held_finish():
    stages = [D.PlanStage("t2 revision", "editing", editing=True),
              D.PlanStage("goal", "reserve", finishing=True), D.PlanStage("audit", "reserve", finishing=True)]
    verdict = D.admit_plan(stages, calls_left=5, tokens_left=657_000, wall_left=1_200,
                           exposures=EXPOSURES, wall_per_stage=120)
    assert not verdict.feasible and verdict.short == {"tokens": 243_000}
    assert verdict.calls_needed == 3 and verdict.tokens_needed == 900_000
    assert "short by tokens 243,000" in verdict.detail
    without = D.admit_plan(stages[1:], calls_left=5, tokens_left=657_000, wall_left=1_200,
                           exposures=EXPOSURES, wall_per_stage=120)
    assert without.feasible and without.short == {}


def test_write_grant_is_always_exposed_at_the_editing_ceiling():
    with pytest.raises(ValueError):
        D.PlanStage("trim", "shaped", editing=True)
    with pytest.raises(ValueError):
        D.PlanStage("trim", "reserve", editing=True)
    low_ceiling = D.admit_plan([D.PlanStage("edit", "editing", editing=True)], calls_left=1, tokens_left=250_000,
                               wall_left=200, exposures={"editing": 100_000, "reserve": 200_000}, wall_per_stage=120)
    assert low_ceiling.tokens_needed == 200_000


def test_shaped_without_a_budget_figure_counts_at_the_reserve_and_all_axes_report():
    verdict = D.admit_plan([D.PlanStage("closeout", "shaped"), D.PlanStage("audit", "reserve", calls=2)],
                           calls_left=2, tokens_left=100_000, wall_left=100, exposures=EXPOSURES, wall_per_stage=120)
    assert verdict.tokens_needed == 600_000
    assert verdict.short == {"calls": 1, "tokens": 500_000, "wall_seconds": 260.0}
    shaped = D.admit_plan([D.PlanStage("closeout", "shaped")], calls_left=1, tokens_left=70_000, wall_left=200,
                          exposures=dict(EXPOSURES, shaped=64_000), wall_per_stage=120)
    assert shaped.feasible and shaped.tokens_needed == 64_000


@pytest.mark.parametrize("bad", [
    dict(calls_left=-1), dict(calls_left=True), dict(calls_left=2.0), dict(tokens_left=float("nan")),
    dict(wall_left=float("inf")), dict(wall_left=-1), dict(exposures={"editing": True, "reserve": 1}),
    dict(exposures={"editing": 1.5, "reserve": 1}),
])
def test_admit_plan_rejects_boolean_and_nonfinite_inputs(bad):
    row = dict(calls_left=1, tokens_left=1, wall_left=1.0, exposures=EXPOSURES, wall_per_stage=1.0)
    row.update(bad)
    with pytest.raises(ValueError):
        D.admit_plan([], **row)
    with pytest.raises(ValueError):
        D.PlanStage("x", "reserve", calls=True)


def test_without_a_token_reserve_only_calls_and_wall_are_counted():
    verdict = D.admit_plan([D.PlanStage("lead", "editing", editing=True), D.PlanStage("review", "reserve")],
                           calls_left=1, tokens_left=0, wall_left=1000, exposures={}, wall_per_stage=10)
    assert verdict.short == {"calls": 1} and verdict.tokens_needed == 0 and "not counted" in verdict.detail
    empty = D.admit_plan([], calls_left=0, tokens_left=0, wall_left=0, exposures=EXPOSURES, wall_per_stage=1)
    assert empty.feasible and empty.to_dict()["calls_needed"] == 0
