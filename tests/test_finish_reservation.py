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
import threading
import time

import pytest

from quadratus.run_budget import (
    FINISH_STAGES,
    FinishReserved,
    RunBudget,
    RunBudgetExceeded,
    RunLimits,
    admitted_as,
    finish_stage,
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



# -- editing exposure and deferral (root reconciliation of R23, 6208197375) ------

def _editing(budget, tokens=None, *, may_wait=True):
    """Admit one editing call; finish it with ``tokens`` when given."""
    with admitted_as(editing=True, may_wait=may_wait):
        ticket, _ = budget.reserve()
    if tokens is not None:
        budget.finish(ticket, dict(input_tokens=tokens, output_tokens=0))
    return ticket


def _left(budget, left):
    """Spend down to ``left`` reported tokens with ordinary calls, each
    under the per-call ceiling."""
    gone = budget.limits.max_reported_tokens - left
    while gone:
        step = min(gone, 450_000)
        _spend(budget, step)
        gone -= step


def test_an_editing_call_is_held_at_the_per_call_ceiling_not_its_reserve():
    """Old: an editing call needed only its 200k reserve beside the held
    400k, so with 670,394 left (R23's t3 lead) it started, returned 346,846
    and left the goal question itself unadmissible. New: it is exposed at
    the 500k post-return ceiling, refused before it starts, and a call that
    does not hold the write grant still meets its reserve as before."""
    old = RunBudget(RunLimits(**R22))
    old.protect_finish(FINISH_STAGES)
    _left(old, 670_394)
    _spend(old, 346_846)                   # admitted on its reserve alone
    with pytest.raises(FinishReserved, match="audit needs 1 call"):
        with finishing("goal"):
            old.reserve()

    new = RunBudget(RunLimits(**R22))
    new.protect_finish(FINISH_STAGES)
    _left(new, 670_394)
    with pytest.raises(FinishReserved):
        _editing(new)
    refusal = new.snapshot()["finish_protection"]["refusals"][-1]
    assert refusal["exposure"] == 500_000 and refusal["tokens_after"] == 170_394
    _spend(new, 100_000)                   # a review: 670,394 - 200,000 >= 400,000
    with finishing("goal"):
        ticket, _ = new.reserve()
    new.finish(ticket, dict(input_tokens=60_000, output_tokens=0))
    with finishing("audit"):
        new.reserve()


@pytest.mark.parametrize("used", [200_001, 346_846, 500_000])
def test_admitted_editing_use_up_to_the_ceiling_never_eats_the_finish(used):
    budget = RunBudget(RunLimits(**R22))
    budget.protect_finish(FINISH_STAGES)
    _left(budget, 900_000)                 # exactly 500,000 + 400,000
    _editing(budget, used)
    with finishing("goal"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=200_000, output_tokens=0))
    with finishing("audit"):
        ticket, _ = budget.reserve()
    assert budget.snapshot()["finish_protection"]["started"] == ["goal", "audit"]


def test_without_a_per_call_ceiling_an_editing_call_is_held_at_its_reserve_and_says_so():
    limits = dict(R22, max_tokens_per_call=None)
    budget = RunBudget(RunLimits(**limits))
    budget.protect_finish(FINISH_STAGES)
    _left(budget, 600_000)
    _editing(budget, 0)                    # 600,000 - 200,000 >= 400,000
    record = budget.snapshot()["finish_protection"]
    assert record["editing_exposure"] is None and "the hold does not bound it" in record["boundary"]


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.01)


def _deferred(budget):
    return budget.snapshot()["finish_protection"]["deferrals"]


def test_an_edit_kept_out_only_by_an_edit_in_flight_waits_unadmitted_and_starts_once_it_settles():
    """R23's t2 revision: 1,374,424 left with t1's revision in flight. Both
    exposed at 500k leaves 374,424 < 400,000, so it is not admitted; alone
    it fits, so it waits with no ticket and no attempt, and starts once the
    in-flight revision settles and the room is there."""
    budget = RunBudget(RunLimits(**R22))
    budget.protect_finish(FINISH_STAGES)
    _left(budget, 1_374_424)
    first = _editing(budget)
    admitted = []
    thread = threading.Thread(target=lambda: admitted.append(_editing(budget)), daemon=True)
    thread.start()
    _wait_for(lambda: _deferred(budget))
    snapshot = budget.snapshot()
    assert snapshot["reserved_attempts"] == first and snapshot["in_flight"] == 1 and not admitted
    assert _deferred(budget)[0]["outcome"] == "waiting" and _deferred(budget)[0]["behind"] == [first]
    assert _deferred(budget)[0]["attempt"] == first + 1
    budget.finish(first, dict(input_tokens=300_000, output_tokens=0))
    thread.join(5)
    assert admitted == [first + 1] and budget.snapshot()["reserved_attempts"] == first + 1
    record = _deferred(budget)[0]
    assert record["outcome"] == "admitted" and record["admitted_as"] == first + 1 and record["waited_seconds"] >= 0
    assert budget.snapshot()["finish_protection"]["refusals"] == []


def test_a_deferred_edit_is_refused_when_the_settled_call_leaves_no_room():
    budget = RunBudget(RunLimits(**R22))
    budget.protect_finish(FINISH_STAGES)
    _left(budget, 1_374_424)
    first = _editing(budget)
    errors = []

    def second():
        try:
            _editing(budget)
        except FinishReserved as exc:
            errors.append(exc)
    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    _wait_for(lambda: _deferred(budget))
    budget.finish(first, dict(input_tokens=480_000, output_tokens=0))   # 894,424 left
    thread.join(5)
    assert len(errors) == 1 and _deferred(budget)[0]["outcome"] == "refused"
    refusal = budget.snapshot()["finish_protection"]["refusals"][-1]
    assert refusal["deferred"] is True and refusal["in_flight"] == 0
    assert budget.snapshot()["reserved_attempts"] == first and budget.snapshot()["stop_reason"] == ""


def test_a_call_that_cannot_fit_alone_or_may_not_wait_is_refused_at_once():
    budget = RunBudget(RunLimits(**R22))
    budget.protect_finish(FINISH_STAGES)
    _left(budget, 1_000_000)
    _editing(budget)                       # 1,000,000 - 500,000 >= 400,000
    # A worker served inside its lead's call never waits on calls in flight.
    with pytest.raises(FinishReserved):
        _editing(budget, may_wait=False)
    # A review kept out by the edit in flight: outside a Fleet call there is
    # no admission shape, so no waiting either (1,000,000 - 200,000 - 500,000).
    with pytest.raises(FinishReserved):
        _spend(budget, 0)
    assert _deferred(budget) == []

    alone = RunBudget(RunLimits(**R22))
    alone.protect_finish(FINISH_STAGES)
    _left(alone, 850_000)
    ticket, _ = alone.reserve()           # a review in flight
    with pytest.raises(FinishReserved):
        _editing(alone)                    # 850,000 - 500,000 < 400,000 even alone
    assert _deferred(alone) == []
    alone.finish(ticket, dict(input_tokens=0, output_tokens=0))


def test_the_wall_ends_a_deferral_with_no_attempt_and_no_reset():
    limits = dict(R22, wall_seconds=0.5)
    budget = RunBudget(RunLimits(**limits))
    budget.protect_finish(FINISH_STAGES)
    _left(budget, 1_374_424)
    first = _editing(budget)
    with pytest.raises(RunBudgetExceeded, match="wall_deadline") as caught:
        _editing(budget)
    assert not isinstance(caught.value, FinishReserved)
    snapshot = budget.snapshot()
    assert snapshot["reserved_attempts"] == first and snapshot["stop_reason"] == "wall_deadline"
    assert _deferred(budget)[0]["outcome"] == "stopped: wall_deadline"
    assert snapshot["elapsed_seconds"] >= 0.5


def _obstruct_when(budget, folder, predicate):
    """Replace the record's folder with an ordinary file the first time the
    real write runs with ``predicate`` true, so that write fails for real."""
    real = budget._persist

    def persist():
        if predicate() and folder.is_dir():
            for child in folder.iterdir():
                child.unlink()
            folder.rmdir()
            folder.write_text("not a directory")
        return real()
    budget._persist = persist


def test_a_deferred_admission_whose_record_cannot_be_written_is_never_invoked(tmp_path):
    """Root on cf8cd2b (#73 6208568305): the admission of a deferred call was
    recorded in a second write whose failure was swallowed, and the call got
    its ticket with budget_state_unavailable latched. Now the admission and
    its deferral record are one write: if it fails, the call is withdrawn
    (no ticket, attempt or call in flight) and the latched stop is raised."""
    folder = tmp_path / "run"
    folder.mkdir()
    budget = RunBudget(RunLimits(max_calls=10, max_reported_tokens=1_000, wall_seconds=60,
                                 reserve_tokens_per_call=100, max_tokens_per_call=300),
                       path=folder / "budget.json")
    budget.protect_finish(FINISH_STAGES)
    _spend(budget, 300)
    first = _editing(budget)               # 700 - 300 >= 200 held
    sent, errors = [], []

    def second():
        try:
            sent.append(_editing(budget))  # 700 - 300 - 300 < 200: waits
        except RunBudgetExceeded as exc:
            errors.append(exc)
    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    _wait_for(lambda: budget._finish_deferrals)
    _obstruct_when(budget, folder, lambda: budget._finish_deferrals[0]["outcome"] == "admitted")
    budget.finish(first, dict(input_tokens=50, output_tokens=0))
    thread.join(5)
    assert sent == [] and len(errors) == 1 and not isinstance(errors[0], FinishReserved)
    assert "budget_state_unavailable" in str(errors[0])
    snapshot = budget.snapshot()
    assert snapshot["stop_reason"] == "budget_state_unavailable"
    assert snapshot["reserved_attempts"] == first and snapshot["in_flight"] == 0
    record = snapshot["finish_protection"]["deferrals"][0]
    assert record["outcome"].startswith("stopped:") and "admitted_as" not in record
    with pytest.raises(RunBudgetExceeded, match="budget_state_unavailable"):
        budget.reserve()


def test_a_finishing_call_whose_record_cannot_be_written_leaves_its_stage_held(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    budget = RunBudget(RunLimits(max_calls=10, max_reported_tokens=10_000, wall_seconds=60),
                       path=folder / "budget.json")
    budget.protect_finish(FINISH_STAGES)
    _obstruct_when(budget, folder, lambda: finish_stage.get() == "goal")
    with pytest.raises(RunBudgetExceeded, match="budget_state_unavailable"):
        with finishing("goal"):
            budget.reserve()
    snapshot = budget.snapshot()
    assert snapshot["reserved_attempts"] == 0 and snapshot["in_flight"] == 0
    assert snapshot["finish_protection"]["pending"] == ["goal", "audit"]
    assert snapshot["finish_protection"]["started"] == []
