"""Cycle03 (PR73 6710677736), planning that leaves the continuation payable.

Completion-cycle02: after t5's repair route was refused with 999,869
spendable, planning call 36 (297,576) left 702,293, enough for t6's lead and
review (700,000); the planner then asked FETCH, the re-ask (call 37,
347,776) left 354,517 and t6 was refused. Nothing priced a re-ask against the
continuation it was planning.

Real controller and real RunBudget, scripted providers: every call reports
110 tokens (harness.USAGE), the reserve is 100 and the per-call ceiling (the
write-granted lead's exposure) 300, so a continuation is a planning call
(110) + lead (300) + review (100), and the next planning call (110) where a
slot is left. Each run is calibrated from an unconstrained replay of the
same script, so the budget left at the decision is exact.

- A FETCH that would cost the payable continuation is refused before any
  call; the same budget without the FETCH dispatches the repair on its
  terminal route; a FETCH that fits is served.
- With known editing work and nothing payable, no planner call is bought.
- With no known editing work, a low budget never blocks a DONE or a FETCH
  on the way to it: the audit still decides.
"""
import json
import re
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.test_check_debt import IMPLEMENT, REPAIR, REQS, _fixes, _looked, _run

pytestmark = pytest.mark.requirements_ledger

PER_CALL = 110
RESERVE = 100
CEILING = 300
HELD = 2 * RESERVE  # the goal question and the audit
PLAN_TEXT = "Name the single next task"


def _limits(tokens):
    return RunLimits(max_calls=120, max_reported_tokens=tokens, wall_seconds=600, max_concurrent_workers=2,
                     reserve_tokens_per_call=RESERVE, max_tokens_per_call=CEILING)


def _planning(replay):
    return [c for c in replay.of("orchestrator") if PLAN_TEXT in c.prompt]


def _orchestrator(second):
    """t1's plan, then ``second`` for the planning call after t1, then DONE."""
    def orchestrator(call, replay):
        n = len(_planning(replay))
        if PLAN_TEXT not in call.prompt:
            return "DONE"
        if n == 1:
            return REQS + IMPLEMENT
        if n == 2 or (n > 2 and "With that read, answer now" in call.prompt):
            # The second decision, and any re-ask inside it.
            return second(call, replay)
        return "DONE"
    return orchestrator


def _fetch_output(call, replay):
    return "FETCH: " + re.search(r"output artifact ([0-9a-f]{12})", call.prompt).group(1)


def _fetch_then_repair(call, replay):
    if "FETCHED" in call.prompt or "With that read, answer now" in call.prompt:
        return REPAIR
    return _fetch_output(call, replay)


def _calls_through_second_plan(replay):
    second = _planning(replay)[1]
    return next(i for i, c in enumerate(replay.calls) if c is second) + 1


def _calibrated(tmp_path, monkeypatch, second, leads, *, spendable, before_plan=False):
    """Run once unconstrained, then with the total set so that exactly
    ``spendable`` is left beyond the held finish after the second planning
    call (or before it, ``before_plan``)."""
    (tmp_path / "calibrate").mkdir()
    (tmp_path / "limited").mkdir()
    base = _run(tmp_path / "calibrate", monkeypatch, _orchestrator(second), leads, limits=_limits(6_000_000))
    budget = json.loads((Path(base.result.run_dir) / "budget.json").read_text())
    assert budget["reported_tokens"] == PER_CALL * budget["reserved_attempts"], "every call reports 110"
    calls = _calls_through_second_plan(base) - (1 if before_plan else 0)
    replay = _run(tmp_path / "limited", monkeypatch, _orchestrator(second), leads,
                  limits=_limits(PER_CALL * calls + HELD + spendable))
    assert [c.role for c in replay.calls[:calls]] == [c.role for c in base.calls[:calls]], "the same calls first"
    replay.calls_before = calls
    return replay


def _record(replay):
    return H.result_json(replay)


def test_a_fetch_that_would_cost_the_payable_repair_is_refused_before_any_call(tmp_path, monkeypatch):
    # 450 left after the planning call: lead + review (400) fit on the
    # terminal route; a re-ask (110) first does not (510).
    replay = _calibrated(tmp_path, monkeypatch, _fetch_output, {"t1": _looked, "t2": _fixes}, spendable=450)
    second = _planning(replay)[1].prompt
    assert "FETCH and EVIDENCE requests are not available in this decision" in second
    assert "reply with exactly 'FETCH: <artifact-id>'" not in second
    assert len(_planning(replay)) == 2, "no re-ask was bought"
    assert not any("With that read, answer now" in c.prompt for c in replay.calls)
    record = _record(replay)
    (refusal,) = record["planning_refusals"]
    assert refusal["site"] == "planner re-ask (FETCH)"
    assert refusal["reported_tokens"] == PER_CALL * replay.calls_before, "the spent planning call counted once"
    assert refusal["terminal_short"] == {"tokens": 60}, refusal
    assert not replay.result.completed
    assert "forfeited planning re-ask" in replay.result.error, replay.result.error
    assert [c.task for c in replay.of("lead")] == ["t1"]


def test_the_same_budget_without_the_fetch_dispatches_the_repair_on_its_terminal_route(tmp_path, monkeypatch):
    replay = _calibrated(tmp_path, monkeypatch, lambda c, r: REPAIR, {"t1": _looked, "t2": _fixes}, spendable=450)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the affordable continuation was dispatched"
    assert _record(replay)["planning_refusals"] == []
    terminal = [a for a in _record(replay)["plan_admissions"] if a["task"] == "t2" and a["scope"] == "terminal"]
    assert terminal and terminal[0]["feasible"], "admitted on the terminal route, as quoted"


def test_a_fetch_that_leaves_the_continuation_payable_is_served(tmp_path, monkeypatch):
    # 700 left: a re-ask (110), lead, review and the next planning call (620) fit.
    replay = _calibrated(tmp_path, monkeypatch, _fetch_then_repair, {"t1": _looked, "t2": _fixes}, spendable=700)
    assert "reply with exactly 'FETCH: <artifact-id>'" in _planning(replay)[1].prompt
    assert any("With that read, answer now" in c.prompt for c in replay.calls), "the FETCH was served"
    assert not [r for r in _record(replay)["planning_refusals"] if r["site"].startswith("planner re-ask")]
    assert "t2" in [c.task for c in replay.of("lead")]


def test_known_editing_work_with_nothing_payable_buys_no_planning_call(tmp_path, monkeypatch):
    # 450 left before the planning call: naming (110) + lead + review (400)
    # do not fit, and t1's check still fails.
    replay = _calibrated(tmp_path, monkeypatch, lambda c, r: REPAIR, {"t1": _looked, "t2": _fixes},
                         spendable=450, before_plan=True)
    assert len(_planning(replay)) == 1, "no planner call after t1"
    (refusal,) = _record(replay)["planning_refusals"]
    assert refusal["site"] == "before planning" and "t1's last check still fails" in refusal["work"][0]
    assert not replay.result.completed and "forfeited before planning" in replay.result.error, replay.result.error


@pytest.mark.parametrize("second", ["DONE", "FETCH"])
def test_with_no_known_editing_work_a_low_budget_still_reaches_done_and_the_audit(tmp_path, monkeypatch, second):
    # t1 fixes add: nothing only an edit can settle. 450 left before the
    # planning call: no continuation is payable, which blocks nothing here.
    def reply(call, replay):
        if second == "FETCH" and "With that read, answer now" not in call.prompt:
            ids = re.findall(r"\b([0-9a-f]{12})\b", call.prompt)
            return f"FETCH: {ids[-1]}"
        return "DONE"
    replay = _calibrated(tmp_path, monkeypatch, reply, {"t1": _fixes}, spendable=450, before_plan=True)
    # The planner was asked; a FETCH on the way to DONE is served (its re-ask
    # is one more planning prompt), because nothing here needs an edit.
    assert len(_planning(replay)) == (3 if second == "FETCH" else 2)
    assert any("With that read, answer now" in c.prompt for c in replay.calls) is (second == "FETCH")
    assert _record(replay)["planning_refusals"] == []
    assert replay.of("auditor"), "the audit decided"
    assert replay.result.completed, replay.result.error
