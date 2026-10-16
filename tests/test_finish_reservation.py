"""The finish's capacity is held from ordinary work (Davis, 2026-10-16).

R22 spent its 20th call on a post-merge recheck and its 21st, the cap's
goal question, was refused at ``call_limit``; the independent audit after it
never ran. ``RunBudget.protect_finish`` holds the goal question and the
audit, one call and one full per-call reserve each, from ordinary calls. An
ordinary call that would spend them is refused before it starts
(``FinishReserved``), recorded, and latches nothing: the finishing calls are
still admitted under every existing limit. No limit moves.
"""

import json

import pytest

from quadratus.run_budget import (
    FINISH_STAGES,
    FinishReserved,
    RunBudget,
    RunBudgetExceeded,
    RunLimits,
    finishing,
)

R22 = dict(max_calls=20, max_reported_tokens=2_000_000, wall_seconds=1800, max_concurrent_workers=2,
           reserve_tokens_per_call=200_000, max_tokens_per_call=500_000)


def _spend(budget, tokens, *, stage=None):
    if stage is None:
        ticket, _ = budget.reserve()
    else:
        with finishing(stage):
            ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=tokens, output_tokens=0))


def test_held_calls_refuse_ordinary_work_and_latch_nothing():
    budget = RunBudget(RunLimits(max_calls=5, max_reported_tokens=10_000, wall_seconds=60))
    budget.protect_finish(FINISH_STAGES)
    for _ in range(3):
        _spend(budget, 10)
    with pytest.raises(FinishReserved, match="goal, audit need 2 call"):
        budget.reserve()
    snapshot = budget.snapshot()
    assert snapshot["stop_reason"] == "" and snapshot["reserved_attempts"] == 3
    assert snapshot["finish_protection"]["refusals"][0]["held"] == ["goal", "audit"]
    _spend(budget, 10, stage="goal")
    _spend(budget, 10, stage="audit")
    assert budget.snapshot()["finish_protection"]["started"] == ["goal", "audit"]
    # Nothing is held now: the next ordinary call meets the ordinary limit.
    with pytest.raises(RunBudgetExceeded) as caught:
        budget.reserve()
    assert not isinstance(caught.value, FinishReserved) and budget.snapshot()["stop_reason"] == "call_limit"


def test_held_tokens_are_one_full_reserve_per_stage_and_count_calls_in_flight():
    budget = RunBudget(RunLimits(max_calls=50, max_reported_tokens=1_000, wall_seconds=60,
                                 reserve_tokens_per_call=200))
    budget.protect_finish(FINISH_STAGES)
    _spend(budget, 200)                    # 800 left
    first, _ = budget.reserve()            # 800 - 200 own - 0 in flight = 600 >= 400 held
    second, _ = budget.reserve()           # 800 - 200 - 200 in flight = 400 >= 400
    with pytest.raises(FinishReserved):
        budget.reserve()                   # 800 - 200 - 400 in flight = 200 < 400
    budget.finish(first, dict(input_tokens=0, output_tokens=0))
    budget.finish(second, dict(input_tokens=0, output_tokens=0))
    # A summary-shaped ordinary call is still held to the finish's full reserves.
    _spend(budget, 300)                    # 500 left: 500 - 64 - 0 = 436 >= 400 passes the shape...
    with pytest.raises(FinishReserved):
        budget.reserve(expected_tokens=150)  # 500 - 150 = 350 < 400
    # ...and the finishing calls take the full operator reserve, never the shape.
    with finishing("goal"):
        ticket, _ = budget.reserve(expected_tokens=10)
    assert budget.snapshot()["shaped_reservations"] == 0
    budget.finish(ticket, dict(input_tokens=250, output_tokens=0))   # 250 left
    with finishing("audit"):
        ticket, _ = budget.reserve()       # the ordinary 200 reserve still applies to it
    budget.finish(ticket, dict(input_tokens=0, output_tokens=0))


def test_a_goal_follow_up_may_not_spend_the_audits_hold():
    budget = RunBudget(RunLimits(max_calls=3, max_reported_tokens=10_000, wall_seconds=60))
    budget.protect_finish(FINISH_STAGES)
    _spend(budget, 1)
    _spend(budget, 1, stage="goal")        # the goal question itself
    with pytest.raises(FinishReserved, match="audit needs 1 call"):
        with finishing("goal"):            # a FETCH turn or a re-ask of it
            budget.reserve()
    _spend(budget, 1, stage="audit")
    assert budget.snapshot()["stop_reason"] == ""


def test_holding_is_idempotent_never_additive_and_a_started_stage_is_not_held_again(tmp_path):
    path = tmp_path / "budget.json"
    budget = RunBudget(RunLimits(max_calls=10, max_reported_tokens=10_000, wall_seconds=60), path=path)
    budget.protect_finish(FINISH_STAGES)
    budget.protect_finish(FINISH_STAGES)
    assert budget.finish_pending() == ["goal", "audit"]
    _spend(budget, 1, stage="goal")
    budget.protect_finish(FINISH_STAGES)
    assert budget.finish_pending() == ["audit"]
    budget.release_finish("audit", "no requirements ledger")
    record = json.loads(path.read_text())["finish_protection"]
    assert record["pending"] == [] and record["started"] == ["goal"]
    assert record["released"] == [dict(stage="audit", why="no requirements ledger")]
    with pytest.raises(ValueError):
        with finishing("summary"):
            pass


def test_a_resumed_episode_holds_the_same_capacity_from_the_carried_spend():
    """The allowance is the job's: a resumed episode carries the spend and
    the session names the stages still pending; the hold is the same size,
    not a second one, and grants nothing."""
    first = RunBudget(RunLimits(max_calls=6, max_reported_tokens=10_000, wall_seconds=60))
    first.protect_finish(FINISH_STAGES)
    _spend(first, 5)
    _spend(first, 5)
    carried = dict(calls=first.snapshot()["reserved_attempts"], input_tokens=10, output_tokens=0,
                   elapsed_seconds=1.0, api_cost_usd=0)
    second = RunBudget(first.limits, carried=carried)
    second.protect_finish(FINISH_STAGES)
    _spend(second, 5)
    _spend(second, 5)
    with pytest.raises(FinishReserved):
        second.reserve()
    _spend(second, 5, stage="goal")
    _spend(second, 5, stage="audit")
    assert second.snapshot()["reserved_attempts"] == 6


# -- the actual R22 account, replayed through admission without any model ----------

def _r22_rows():
    """The 20 posted rows of R22's call account (root packet, #73 comment
    6205131091): task, role, input, output. Kept here verbatim."""
    table = """
run|orchestrator|77971|7180
plan|requirements-review|12167|174
t1|lead|58118|2842
t1|revision|95378|1202
t1|collaborator|56219|4655
t1|revision|72037|1133
t1|closeout|15684|575
run|orchestrator|101888|5465
t3|lead|158213|3935
t3|collaborator|124104|2500
t3|revision|145140|2169
t3|recheck|42691|339
t3|closeout|7162|1306
t2|lead|166465|12066
t2|collaborator|85809|5049
t2|revision|230915|11373
t2|recheck|83337|540
t2|closeout|12649|1011
t2|recheck|99289|1118
t3|recheck|28286|177
"""
    return [(t, role, int(i), int(o)) for t, role, i, o in
            (line.split("|") for line in table.strip().splitlines())]


def test_the_r22_account_replayed_through_admission():
    """Illustration, not a measurement: R22's rows in their posted order,
    serially (the real t2 and t3 overlapped; their in-flight reservations
    would only refuse earlier). Today's rules admit all 20 and refuse the
    goal question at call 21. Holding the finish admits rows 1-16 and
    refuses row 17 (t2's recheck, 488,076 tokens left: 200,000 for itself
    and 400,000 held do not fit) and every ordinary row after it; the goal
    question is then admitted with four calls left. Whether the audit is
    admitted after it depends on the goal question's reported usage, which
    no record holds: the hold is an admission policy, not a forecast."""
    rows = _r22_rows()
    assert len(rows) == 20 and sum(i + o for _, _, i, o in rows) == 1_738_331

    today = RunBudget(RunLimits(**R22))
    for _, _, i, o in rows:
        ticket, _ = today.reserve()
        today.finish(ticket, dict(input_tokens=i, output_tokens=o))
    with pytest.raises(RunBudgetExceeded, match="call_limit"):
        with finishing("goal"):
            today.reserve()

    held = RunBudget(RunLimits(**R22))
    held.protect_finish(FINISH_STAGES)
    refused = []
    for n, (task, role, i, o) in enumerate(rows, 1):
        try:
            ticket, _ = held.reserve()
        except FinishReserved:
            refused.append((n, task, role))
            continue
        held.finish(ticket, dict(input_tokens=i, output_tokens=o))
    assert refused == [(17, "t2", "recheck"), (18, "t2", "closeout"), (19, "t2", "recheck"), (20, "t3", "recheck")]
    snapshot = held.snapshot()
    assert snapshot["reserved_attempts"] == 16 and snapshot["reported_tokens"] == 1_511_924
    assert snapshot["stop_reason"] == ""
    with finishing("goal"):
        ticket, remaining = held.reserve()
    assert held.snapshot()["finish_protection"]["pending"] == ["audit"]
    held.finish(ticket, dict(input_tokens=0, output_tokens=0))
    # Row 18 (t2's close-out) is not a model call under this batch: the last
    # wave's close-outs are written by the harness. Rows 17, 19 and 20 are
    # reviews: refused, they stay open as findings, never resolved.
    assert [r for r in refused if r[2] != "closeout"] == [(17, "t2", "recheck"), (19, "t2", "recheck"),
                                                           (20, "t3", "recheck")]

