"""Typed outcomes and their parity check (quadratus.outcome, phase 1).

Unit cases, including the negative controls that show parity can fail: a
parity check that always agreed would prove nothing on the replays.
"""

from quadratus.outcome import PRECEDENCE, RunOutcome, TaskOutcome, classify, parity


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


def _closed(task_id="t1"):
    outcome = TaskOutcome(task_id, "implementation")
    outcome.closed_as = "closed"
    return outcome


def test_parity_agrees_on_a_clean_run():
    run = RunOutcome(done_accepted=True)
    result = parity(run, [_closed()], open_findings=[], legacy_completed=True, legacy_error="",
                    history=["t1:closed"])
    assert result == dict(agree=True, problems=[], typed_completed=True, primary="clean")


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
