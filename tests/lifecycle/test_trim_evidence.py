"""The terminal budget note (B): the terminal record says when what was
left could not admit an ordinary call (``budget_note``), as an observation,
never a stop.

This file once also held the size-trim evidence controls (R20 on c7e9449).
Size is advisory since the operator directive of 2026-10-20 and the harness
trims nothing; see tests/lifecycle/test_advisory_sizing.py.
"""

import json
from types import SimpleNamespace

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_step2_recovery import _R1, _TYPED, DECL_SIMPLE, _plan

# -- B: the terminal budget note -------------------------------------------------------------

def test_the_terminal_record_says_when_the_rest_could_not_admit_an_ordinary_call():
    """From the same terminal snapshot; never a stop or a failure fact; says
    a shaped summary call reserves less; says when usage went unreported."""
    from quadratus.project_run import _budget_fields

    def budget(used, reserve=200_000, unknown=0, stop=""):
        snap = dict(limits=dict(max_reported_tokens=2_000_000, reserve_tokens_per_call=reserve),
                    reported_tokens=used, unknown_usage_attempts=unknown, stop_reason=stop)
        return SimpleNamespace(snapshot=lambda: snap)

    note = _budget_fields(budget(1_858_053))["budget_note"]
    assert note["remaining_reported_tokens"] == 141_947 and note["ordinary_call_reserve"] == 200_000
    assert note["stop_reason"] == "" and note["summary_call_reserve"] == 64_000
    assert note["text"] == (
        "At the end of the run 141,947 reported tokens remained, below the ordinary-call token reservation of "
        "200,000. The token remainder covers the 64,000-token reservation for a summary-only call whose CLI "
        "enforces one turn. This is a token-capacity observation, not the run's stop or an admission decision; "
        "other limits and workflow conditions are not assessed.")
    tight = _budget_fields(budget(2_000_000 - 50_000))["budget_note"]
    assert "The token remainder does not cover the 64,000-token reservation" in tight["text"]
    for reserve in (600, 64_000):
        # At or under the summary allowance the shaped reservation equals
        # the ordinary one (root on b4d8398).
        same = _budget_fields(budget(2_000_000 - 500, reserve=reserve))["budget_note"]
        assert same["summary_call_reserve"] == reserve
        assert f"does not cover the {reserve:,}-token reservation" in same["text"] and "less" not in same["text"]
    assert _budget_fields(budget(1_000_000))["budget_note"] is None, "room left: no note"
    assert _budget_fields(budget(1_858_053, reserve=0))["budget_note"] is None, "no reserve configured"
    assert "may be lower" in _budget_fields(budget(1_900_000, unknown=2))["budget_note"]["text"]
    assert _budget_fields(None) == {"budget": None, "budget_note": None}


def _starved_run(tmp_path, monkeypatch, where, total):
    """One run of the reserve-starved scenario under ``total`` tokens: a
    600-token reserve, no finish hold, one simple task, DONE, the audit."""
    from quadratus.run_budget import RunLimits
    declared = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_SIMPLE + "\nCOVERS: R1"

    def lead(call, replay):
        H.write(call, {"app.py": _TYPED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
    where.mkdir()
    return H.run(where, monkeypatch, Script(
        orchestrator=_plan(declared), lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:5",
        auditor=lambda c, r: "R1: MET - app.py:5", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=1, record_complete=False,
        limits=RunLimits(max_calls=120, max_reported_tokens=total, wall_seconds=600,
                         max_concurrent_workers=2, reserve_tokens_per_call=600))


@pytest.mark.requirements_ledger
def test_a_reserve_starved_terminal_keeps_its_own_stop_and_outcome(tmp_path, monkeypatch):
    """Whole controller: the note is metadata beside the run's own result;
    completed, error and the typed outcome are what they would have been.
    Every call is admitted and the run ends with 500, under the 600 reserve.

    Restaged on the admitted route (root's cap reconciliation on bc421f8):
    t1's selected route (the draft and the review, each at the 600 reserve
    here: 1,200; a correction is admitted only when a finding selects it,
    admission/2) is admitted at dispatch only from what the allowance holds
    after the calls already made, so the harness's default 110-token calls
    cannot both admit the route and end 500 under the reserve (that
    refusal is kept below). With each call reporting 590 (under the
    reserve; test-local H.USAGE), the measured spend is 3,540 over six
    calls; the tight allowance of used + 500 = 4,040 admits the route at
    dispatch (2,860 left against 1,200), every later call starts with at
    least 1,090, and the final audit ends with 500. Same calls, same
    admitted route, same stop and outcome as the roomy run; the receipts
    are asserted, not assumed."""
    # The finish hold (Davis, 2026-10-16) would refuse an earlier ordinary
    # call; this case pins the limit itself, so the hold is off here and is
    # controlled in tests/lifecycle/test_finish_capacity.py.
    monkeypatch.setattr("quadratus.session.Session._protect_finish", lambda self: None)
    monkeypatch.setattr(H, "USAGE", {"input_tokens": 580, "output_tokens": 10})
    roomy = _starved_run(tmp_path, monkeypatch, tmp_path / "roomy", 6_000_000)
    used = H.result_json(roomy)["budget"]["reported_tokens"]
    assert used == 590 * len(roomy.calls) == 3_540, (used, len(roomy.calls))
    tight = _starved_run(tmp_path, monkeypatch, tmp_path / "tight", used + 500)
    result = H.result_json(tight)
    assert [c.role for c in tight.calls] == [c.role for c in roomy.calls], "the same calls, every one admitted"
    admitted = [a for a in result["plan_admissions"] if a["task"] == "t1" and a["scope"] == "full"]
    assert admitted and admitted[-1]["feasible"] and len(admitted[-1]["stages"]) == 2, admitted
    assert "2 call(s), 1,200 tokens" in admitted[-1]["detail"] and "2,860 tokens" in admitted[-1]["detail"]
    assert used + 500 - 590 * admitted[-1]["at_attempt"] == 2_860 >= 1_200, "the route was paid from what was left"
    assert result["not_started"] == [] and result["finish_refusals"] == []
    assert H.result_json(roomy)["budget_note"] is None
    assert result["budget_note"]["remaining_reported_tokens"] == 500
    assert "does not cover the 600-token reservation" in result["budget_note"]["text"]
    assert (tight.result.completed, tight.result.error) == (roomy.result.completed, roomy.result.error)
    assert result["workflow"]["run"] == H.result_json(roomy)["workflow"]["run"], "no new fact"
    assert json.dumps(result["budget"]["stop_reason"]) == json.dumps(H.result_json(roomy)["budget"]["stop_reason"])


@pytest.mark.requirements_ledger
def test_an_allowance_that_cannot_pay_the_route_never_reaches_the_reserve_starved_terminal(tmp_path, monkeypatch):
    """The original fixture's raw failure on bc421f8, kept as the control:
    with the harness's default 110-token calls, the tight allowance of
    used + 500 holds 940 at dispatch against t1's two-call route at the
    600 reserve (1,200), so the task is NOT STARTED before any task call,
    the shortfall is named, no reserve-starved terminal is reached and no
    budget note is written. The run's own stop stands: incomplete,
    RequirementsUnmet, nothing written."""
    monkeypatch.setattr("quadratus.session.Session._protect_finish", lambda self: None)
    roomy = _starved_run(tmp_path, monkeypatch, tmp_path / "roomy", 6_000_000)
    used = H.result_json(roomy)["budget"]["reported_tokens"]
    assert used == 110 * len(roomy.calls)
    tight = _starved_run(tmp_path, monkeypatch, tmp_path / "tight", used + 500)
    result = H.result_json(tight)
    assert not tight.result.completed and "RequirementsUnmet" in (tight.result.error or "")
    assert [n["task"] for n in result["not_started"]] == ["t1"] and not tight.of("lead")
    (refusal,) = [r for r in result["finish_refusals"] if r["task"] == "t1"]
    assert "2 call(s), 1,200 tokens" in refusal["detail"] and "short by tokens" in refusal["detail"], refusal
    assert result["budget_note"] is None
    assert (tight.project / "app.py").read_text() == FILES["app.py"]


@pytest.mark.parametrize("exhausted", ["calls", "wall"])
def test_the_budget_note_is_token_capacity_never_an_admission_prediction(tmp_path, exhausted):
    """Root on 38ec681: with every call used, or the wall clock spent, the
    note said a summary call could still be admitted while RunBudget refuses
    it. The note now states token capacity only, says other limits are not
    assessed, and reading it changes nothing in the budget."""
    from quadratus.project_run import _budget_fields
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
    limits = RunLimits(max_calls=17, max_reported_tokens=2_000_000, wall_seconds=1800,
                       max_concurrent_workers=2, reserve_tokens_per_call=200_000)
    carried = dict(calls=17 if exhausted == "calls" else 10, input_tokens=1_858_053, output_tokens=0,
                   elapsed_seconds=1800 if exhausted == "wall" else 100)
    budget = RunBudget(limits, carried=carried)
    def state():
        return {k: v for k, v in budget.snapshot().items() if k != "elapsed_seconds"}
    before = state()
    note = _budget_fields(budget)["budget_note"]
    assert "The token remainder covers the 64,000-token reservation" in note["text"]
    assert "not the run's stop or an admission decision; other limits and workflow conditions are not assessed" \
        in note["text"]
    assert "could" not in note["text"] and "admitted" not in note["text"]
    assert state() == before, "metadata only: reading the note changes nothing in the budget"
    with pytest.raises(RunBudgetExceeded, match="call_limit" if exhausted == "calls" else "wall_deadline"):
        budget.reserve(expected_tokens=64_000)
