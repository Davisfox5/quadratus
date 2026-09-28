"""Typed outcomes and their parity check (quadratus.outcome, phase 1).

Unit cases, including the negative controls that show parity can fail: a
parity check that always agreed would prove nothing on the replays.
"""

from quadratus.outcome import (
    PRECEDENCE,
    RunOutcome,
    TaskOutcome,
    classify,
    completion_blockers,
    missing_facts,
    parity,
)


class ProviderError(RuntimeError):
    pass


class ProviderRefusal(ProviderError):
    pass


class TurnLimitReached(ProviderError):
    pass


class WindowExhausted(ProviderError):
    pass


class SomethingNew(RuntimeError):
    pass


def test_exceptions_are_classed_along_their_mro_and_unknown_is_an_operator_handoff():
    assert classify(ProviderRefusal()) == "refusal"
    assert classify(TurnLimitReached()) == "cap"
    assert classify(WindowExhausted()) == "transport"
    assert classify(ProviderError()) == "transport"
    assert classify(SomethingNew()) == "operator", "never a product repair"
    assert classify(KeyboardInterrupt()) == "operator"


def test_primary_follows_the_precedence_and_secondary_keeps_the_rest():
    outcome = TaskOutcome("t1", "implementation")
    outcome.note("product", "gate failing", legacy_route=True)
    outcome.note("cap", "capped")
    outcome.note("integrity", "scope")
    data = outcome.to_dict()
    assert data["primary"] == "integrity" and data["secondary"] == ["cap", "product"]
    assert PRECEDENCE.index("refusal") == 0 and PRECEDENCE[-1] == "unverified"


def test_a_recovered_fact_keeps_its_record_and_stops_blocking():
    outcome = TaskOutcome("t1", "implementation")
    outcome.note("cap", "capped")
    outcome.recover("cap")
    assert outcome.primary == "clean" and outcome.facts[0].recovered is True
    assert len(outcome.facts) == 1, "history is not erased"


def test_a_non_terminal_fact_is_history_only():
    run = RunOutcome()
    run.note("unverified", "DONE sent back", terminal=False)
    run.done_accepted = True
    assert parity(run, [], open_findings=[], legacy_completed=True, legacy_error="", history=[])["agree"]


REQUIRED = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
                settlement=False, design_collaboration_applicable=False)


def _closed(task_id="t1"):
    outcome = TaskOutcome(task_id, "implementation", lead="claude:opus", source_before="a" * 64,
                          source_after="b" * 64, dependency="unchanged",
                          partial=dict(changed=["app.py"], changed_lines=2, inspected=True),
                          contract=dict(task_id=task_id, owner="claude:opus", required=dict(REQUIRED)),
                          dispatch=dict(state="dispatched", owner="claude:opus"))
    outcome.closed_as = "closed"
    return outcome


def test_parity_agrees_on_a_clean_run():
    run = RunOutcome(done_accepted=True)
    result = parity(run, [_closed()], open_findings=[], legacy_completed=True, legacy_error="",
                    history=["t1:closed"])
    assert result == dict(agree=True, problems=[], complete=True, missing=[], typed_completed=True,
                          primary="clean")


def test_parity_catches_a_legacy_stop_the_typed_record_missed():
    result = parity(RunOutcome(), [_closed()], open_findings=[], legacy_completed=False,
                    legacy_error="RunStalled: same task twice", history=["t1:closed"])
    assert not result["agree"] and "no typed stop fact" in result["problems"][0]


def test_parity_catches_a_stop_recorded_under_the_wrong_name():
    run = RunOutcome()
    run.note("cap", "breaker", legacy="TurnLimitBreaker")
    result = parity(run, [_closed()], open_findings=[], legacy_completed=False,
                    legacy_error="FindingsUnresolved: F1 open", history=["t1:closed"])
    assert not result["agree"] and "stop: typed 'TurnLimitBreaker'" in result["problems"][0]


def test_parity_catches_a_completion_disagreement():
    run = RunOutcome(done_accepted=True)
    task = _closed()
    task.note("product", "gate failing")
    run.note("unverified", "DONE with open work", legacy="")
    result = parity(run, [task], open_findings=[], legacy_completed=True, legacy_error="",
                    history=["t1:closed"])
    assert not result["agree"] and result["problems"][0].startswith("completed: typed False")


def test_parity_catches_a_task_the_typed_record_closed_differently():
    capped = _closed()
    capped.closed_as = "turn_limited"
    run = RunOutcome(done_accepted=True)
    result = parity(run, [capped], open_findings=[], legacy_completed=False, legacy_error="",
                    history=["t1:closed"])
    assert not result["agree"] and any(p.startswith("tasks:") for p in result["problems"])


def test_open_ledger_findings_block_typed_completion_by_reference():
    run = RunOutcome(done_accepted=True)
    run.note("unverified", "audit findings open", legacy="FindingsUnresolved")
    result = parity(run, [_closed()], open_findings=["F1"], legacy_completed=False,
                    legacy_error="FindingsUnresolved: audit findings F1 are still open",
                    history=["t1:closed"])
    assert result["agree"] and result["typed_completed"] is False


def test_a_closed_task_without_its_owner_source_or_work_is_incomplete():
    bare = TaskOutcome("t1", "implementation")
    bare.closed_as = "closed"
    assert missing_facts(bare) == ["t1.dispatch", "t1.lead", "t1.source_before", "t1.source_after",
                                   "t1.dependency", "t1.partial"]
    result = parity(RunOutcome(done_accepted=True), [bare], open_findings=[], legacy_completed=True,
                    legacy_error="", history=["t1:closed"])
    assert result["agree"] and not result["complete"], "routing agreement alone is not a complete record"


def test_an_explicit_unavailable_counts_and_an_absent_value_does_not():
    task = _closed()
    task.source_after = "unavailable"
    assert missing_facts(task) == []
    task.source_after = None
    assert missing_facts(task) == ["t1.source_after"]


def test_a_closed_task_that_reached_its_checks_must_carry_an_attempt():
    task = _closed()
    task.stage("checks")
    assert missing_facts(task) == ["t1.checks"]
    task.checks.append(dict(attempt=1, passed=True, receipts=[], output_artifact="abc123"))
    assert missing_facts(task) == []


def test_a_stopped_task_whose_check_was_refused_needs_no_attempt():
    task = TaskOutcome("t1", "implementation", lead="claude:opus",
                       contract=dict(task_id="t1", owner="claude:opus", required=dict(REQUIRED)),
                       dispatch=dict(state="dispatched", owner="claude:opus"))
    task.stage("checks")
    task.closed_as = "stopped:DependencyTreeChanged"
    assert missing_facts(task) == []


def test_a_check_attempt_without_its_kept_output_is_incomplete():
    task = _closed()
    task.checks.append(dict(attempt=1, passed=True, receipts=[], output_artifact="abc123",
                            output_artifact_error=None))
    assert missing_facts(task) == []
    task.checks.append(dict(attempt=2, passed=False, receipts=[], output_artifact="unavailable",
                            output_artifact_error="OSError: disk full"))
    assert missing_facts(task) == ["t1.checks[2].output_artifact"]


# -- dispatch and owner (Codex 5857796277) -----------------------------------------------

def test_a_stop_before_dispatch_must_say_why_and_carry_no_contract():
    task = TaskOutcome("t1", "implementation")
    task.closed_as = "stopped:RunStalled"
    assert missing_facts(task) == ["t1.dispatch"], "silence is not a pre-dispatch record"
    task.dispatch = dict(state="not_dispatched", reason="")
    assert missing_facts(task) == ["t1.dispatch.reason"]
    task.dispatch["reason"] = "RunStalled: no seat"
    assert missing_facts(task) == []
    task.contract = dict(task_id="t1", owner="claude:opus", required=dict(REQUIRED))
    assert missing_facts(task) == ["t1.contract on a task that was not dispatched"]


def test_the_contract_owner_is_the_dispatched_owner_and_never_follows_a_switch():
    task = _closed()
    task.contract = dict(task.contract, owner="openai:sol")
    assert missing_facts(task) == ["t1.contract owner 'openai:sol' is not the dispatched owner 'claude:opus'"]


def test_an_invoked_owner_follows_an_unbroken_chain_of_recorded_switches():
    task = _closed()
    task.lead = "openai:sol"
    assert missing_facts(task) == ["t1.lead 'openai:sol' is not the invoked owner 'claude:opus'"]
    task.owner_changes.append({"from": "xai:grok", "to": "openai:sol", "reason": "lead_recovery"})
    assert missing_facts(task) == ["t1.owner_changes from 'xai:grok', expected 'claude:opus'"]
    task.owner_changes[0]["from"] = "claude:opus"
    assert missing_facts(task) == [] and task.to_dict()["invoked_owner"] == "openai:sol"
    assert task.contract["owner"] == "claude:opus"


# -- completion guard (phase 3) ----------------------------------------------------------

def test_completion_blockers_are_empty_only_for_a_complete_satisfied_record():
    assert completion_blockers([_closed()]) == []
    assert completion_blockers([_closed()], owed=["finding F1"]) == ["owed finding F1"]
    never = TaskOutcome("t2", "implementation")
    assert completion_blockers([_closed(), never]) == ["t2 never closed"]


def _capped(task_id="t1", **required):
    capped = _closed(task_id)
    capped.contract = dict(capped.contract, required=dict(REQUIRED, checks=True, **required))
    capped.closed_as = "turn_limited"
    capped.note("cap", "lead turn limit", stage="draft")
    return capped


def _continuing(task_id, predecessor, *, checked=True, **required):
    """A successor that factually ran and passed the checks it required."""
    successor = _closed(task_id)
    successor.continues = predecessor
    if checked:
        successor.contract = dict(successor.contract, required=dict(REQUIRED, checks=True, **required))
        successor.edge("checks", True)
    return successor


def test_a_recovered_cap_is_discharged_only_by_a_successor_that_did_the_work():
    capped = _capped()
    assert completion_blockers([capped]) == ["t1.cap: lead turn limit", "t1.checks unsatisfied"]
    capped.recover("cap")
    assert completion_blockers([capped, _continuing("t2", "t1")]) == []
    unchecked = _continuing("t2", "t1", checked=False)
    assert completion_blockers([capped, unchecked]) == ["t1.checks unsatisfied"], \
        "a clean successor under a weaker contract discharges nothing it did not do"
    capped.source_after = None
    assert completion_blockers([capped, _continuing("t2", "t1")]) == ["record t1.source_after"]


def test_a_design_edge_is_discharged_only_under_the_same_intended_state():
    state = '{"page": "/", "steps": []}'
    capped = _capped(design_review=True)
    capped.contract = dict(capped.contract, intended_state=state)
    capped.recover("cap")
    other = _continuing("t2", "t1", design_review=True)
    other.edge("delivered", True)
    other.edge("reviewer", True)
    assert completion_blockers([capped, other]) == ["t1.delivered unsatisfied", "t1.reviewer unsatisfied"]
    other.contract = dict(other.contract, intended_state=state)
    assert completion_blockers([capped, other]) == []
    stateless = _capped(design_review=True)
    stateless.recover("cap")
    assert completion_blockers([stateless, other]) == ["t1.delivered unsatisfied", "t1.reviewer unsatisfied"], \
        "no declared state, nothing to bind the successor's evidence to"


def test_a_successor_that_fails_or_never_closes_cannot_hide_predecessor_debt():
    capped = _capped()
    capped.recover("cap")
    failing = _continuing("t2", "t1", checked=False)
    failing.contract = dict(failing.contract, required=dict(REQUIRED, checks=True))
    failing.edge("checks", False)
    assert completion_blockers([capped, failing]) == ["t1.checks unsatisfied", "t2.checks unsatisfied"]
    stopped = _continuing("t2", "t1")
    stopped.closed_as = "stopped:ProviderRefusal"
    stopped.note("refusal", "declined")
    assert "t1.checks unsatisfied" in completion_blockers([capped, stopped])
    unclosed = _continuing("t2", "t1")
    unclosed.closed_as = "open"
    assert completion_blockers([capped, unclosed]) == ["t1.checks unsatisfied", "t2 never closed"]


def test_a_non_recoverable_predecessor_stop_is_never_waived():
    for kind in ("refusal", "security", "integrity", "operator", "denial", "transport"):
        predecessor = _capped()
        predecessor.recover("cap")
        predecessor.note(kind, "still standing")
        assert completion_blockers([predecessor, _continuing("t2", "t1")]) == [f"t1.{kind}: still standing"]


def test_a_chain_discharges_only_through_eligible_links_and_bad_references_block():
    t1, t2 = _capped("t1"), _capped("t2")
    t2.continues = "t1"
    t1.recover("cap")
    t2.recover("cap")
    assert completion_blockers([t1, t2, _continuing("t3", "t2")]) == []
    assert completion_blockers([t1, t2]) == ["t1.checks unsatisfied", "t2.checks unsatisfied"]
    unknown = _continuing("t2", "t9")
    assert completion_blockers([_closed(), unknown]) == ["t2 CONTINUES 't9', which is not an earlier task"]
    first, second = _continuing("t1", "t2"), _continuing("t2", "t1")
    assert "t1 CONTINUES 't2', which is not an earlier task" in completion_blockers([first, second])


def test_settled_audit_debt_discharges_only_the_design_edges():
    audit = _closed()
    audit.intent = "audit"
    audit.contract = dict(audit.contract, required=dict(REQUIRED, design_review=True, checks=True,
                                                        security_verification=True, settlement=True))
    everything = ["t1.checks unsatisfied", "t1.verification unsatisfied", "t1.delivered unsatisfied",
                  "t1.reviewer unsatisfied", "t1.settlement unsatisfied"]
    assert completion_blockers([audit]) == everything
    assert completion_blockers([audit], audit_findings={"t1": ["resolved"]}) == [
        "t1.checks unsatisfied", "t1.verification unsatisfied", "t1.settlement unsatisfied"]
    assert completion_blockers([audit], audit_findings={"t1": ["resolved", "open"]}) == everything, \
        "an unsettled finding discharges nothing"
    assert completion_blockers([audit], audit_findings={"t2": ["resolved"]}) == everything, "provenance"
    assert completion_blockers([audit], audit_findings={"t1": []}) == everything
    audit.contract = dict(audit.contract, required=dict(REQUIRED, design_review=True))
    assert completion_blockers([audit], audit_findings={"t1": ["resolved"]}, owed=["finding F1"]) == [
        "owed finding F1"]
    audit.note("integrity", "evidence tampered")
    assert completion_blockers([audit], audit_findings={"t1": ["resolved"]}) == ["t1.integrity: evidence tampered"]
    implementation = _closed()
    implementation.contract = dict(implementation.contract, required=dict(REQUIRED, design_review=True))
    assert completion_blockers([implementation], audit_findings={"t1": ["resolved"]}) == [
        "t1.delivered unsatisfied", "t1.reviewer unsatisfied"]


# -- malformed records (Codex review 5858204394) -----------------------------------------

def test_an_unsupported_dispatch_state_or_owner_is_malformed():
    task = _closed()
    task.dispatch = dict(state="invalid-state", owner="claude:opus")
    assert missing_facts(task) == ["t1.dispatch.state 'invalid-state' is not a supported state"]
    task.dispatch = dict(state="dispatched", owner="")
    assert "t1.dispatch.owner" in missing_facts(task)
    task.dispatch = dict(state="not_dispatched", reason="  ")
    assert "t1.dispatch.reason" in missing_facts(task)


def test_a_missing_or_malformed_requirement_declaration_blocks():
    task = _closed()
    del task.contract["required"]
    assert missing_facts(task) == ["t1.contract.required"]
    assert completion_blockers([task]) == ["record t1.contract.required"]
    for key, bad in (("checks", None), ("checks", "yes"), ("settlement", 1), ("design_evidence", "maybe")):
        task.contract["required"] = dict(REQUIRED, **{key: bad})
        assert missing_facts(task) == [f"t1.contract.required.{key}"], key
    task.contract["required"] = {k: v for k, v in REQUIRED.items() if k != "design_review"}
    assert missing_facts(task) == ["t1.contract.required.design_review"]


def test_a_contract_for_another_task_or_a_malformed_owner_change_is_missing():
    task = _closed()
    task.contract = dict(task.contract, task_id="t9")
    assert missing_facts(task) == ["t1.contract names task 't9'"]
    task = _closed()
    task.owner_changes.append({"from": "claude:opus", "to": "", "reason": "lead_recovery"})
    assert "t1.owner_changes[0] malformed" in missing_facts(task)
