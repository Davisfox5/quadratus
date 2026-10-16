"""Judgment integrity (strategy reset approved by Davis, 2026-10-16).

R24 ended after its first task because two mandatory judgments arrived
without a valid verdict: one review with no verdict line and no
requirement lines, one recheck whose RESOLVED was glued to its narration.
Strict parsing was right; the controller had no way to obtain the judgment
it required, so the run stopped with most of its allowance left. And the
three verdict readers combined repeated lines three different ways, so a
contradictory audit or reassessment reply could approve a requirement.

Now one strict rule reads every judgment (quadratus.judgment), and an
arrived reply that is not a judgment may be completed by the same seat,
once per whole job, on the same source, inside the run's own limits. A
genuine finding is never routed as a format gap, a cap or failure is never
retried, and no other seat or the audit stands in for a missing review.

Whole-run replays below the vendor CLI (tests.lifecycle.harness).
"""

import json
from pathlib import Path

import pytest

from quadratus import judgment
from quadratus.config import Settings
from quadratus.run_budget import FINISH_STAGES, FinishReserved, RunBudget, RunLimits, finishing
from tests.lifecycle import harness as H
from tests.lifecycle.test_finish_capacity import BATCH, PLAN_1, PLAN_2, _budget, _script
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_step2_recovery import _episode, _interrupt_after

COMPLETION = "arrived without a valid verdict in the required form"
#: Arrived, but no judgment: narration with no verdict and no requirement line.
NARRATION = "I'll read app.py and the tests, then judge the work against the requirements."
CLEAN_T1 = "NO FINDINGS\nR1: MET - app.py"
CLEAN_T2 = "NO FINDINGS\nR2: MET - README.md"
GLUED = "I'll re-check only the blocking finding against the revised source.RESOLVED"


def _limits(**kw):
    return RunLimits(**dict(dict(max_calls=40, max_reported_tokens=6_000_000, wall_seconds=600,
                                 max_concurrent_workers=2), **kw))


def _reviews(first, *, then=None, t2=CLEAN_T2):
    """t1's review replies ``first``; a completion prompt gets ``then``."""
    def collaborator(call, replay):
        if call.task != "t1":
            return t2
        if COMPLETION in call.prompt:
            return then
        return first
    return collaborator


def _completions(replay):
    return [c for c in replay.calls if COMPLETION in c.prompt]


# -- A: one strict reading ------------------------------------------------------------

def test_every_reader_combines_repeated_lines_the_same_strict_way():
    for lines in ([("R1", False, "x"), ("R1", True, "y")], [("R1", True, "y"), ("R1", False, "x")]):
        assert judgment.combine(lines)["R1"].state == judgment.NOT_MET
    assert judgment.combine([("R1", True, "a"), ("R1", True, "b")])["R1"].state == judgment.DUPLICATE
    assert judgment.combine([("R1", True, "a")])["R1"].approved
    assert judgment.recheck_state("RESOLVED") == judgment.RESOLVED
    assert judgment.recheck_state(GLUED) == judgment.INVALID, "no verdict is extracted from narration"
    assert judgment.recheck_state("RESOLVED\nRESOLVED") == judgment.INVALID
    assert judgment.recheck_state("UNRESOLVED: still wrong") == judgment.UNRESOLVED
    assert judgment.recheck_state("BLOCKING: x\nRESOLVED") == judgment.UNRESOLVED


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("reply", ["R1: NOT MET - no test\nR1: MET - app.py",
                                   "R1: MET - app.py\nR1: NOT MET - no test",
                                   "R1: MET - app.py\nR1: MET - app.py"])
def test_a_contradictory_or_duplicate_audit_reply_never_approves(tmp_path, monkeypatch, reply):
    """Old: the audit kept the last line, so NOT MET then MET approved R1.
    Now any NOT MET line, in either order, is NOT MET, and two MET lines are
    no verdict: the run is not complete."""
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["auditor"] = lambda call, replay: reply + "\nR2: MET - README.md"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["verdicts"]["R1"]["met"] is False and audit["verdicts"]["R2"]["met"] is True
    assert not replay.result.completed


# -- B: one same-seat judgment completion per job ---------------------------------------

@pytest.mark.requirements_ledger
def test_an_arrived_review_without_a_verdict_is_completed_once_by_the_same_seat(tmp_path, monkeypatch):
    """R24's shape, once: t1's review arrives as narration. The same seat,
    in the same collaborator role and source copy, gives its judgment once;
    the raw reply is kept, the claim is in budget.json before the call, and
    the run goes on to t2, the goal question and the audit."""
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["collaborator"] = _reviews(NARRATION, then=CLEAN_T1)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    done = _completions(replay)
    first = next(c for c in replay.calls if c.task == "t1" and c.role == "collaborator")
    assert len(done) == 1 and done[0].role == "collaborator" and done[0].vendor == first.vendor
    assert NARRATION in done[0].prompt, "the raw reply is shown to the same seat, unchanged"
    assert replay.result.completed, replay.result.error
    budget = _budget(replay)
    assert budget["judgment_completions"] == 1 and budget["judgment_completion_records"][0]["seat"]
    record = H.result_json(replay)["judgment_completions"]
    assert [r["outcome"] for r in record] == ["answered"] and record[0]["raw_artifact"]
    raw = replay.artifacts("judgment-raw:review")
    assert raw == [record[0]["raw_artifact"]], "the raw reply is kept unchanged as its own artifact"


@pytest.mark.requirements_ledger
def test_a_completion_that_still_gives_no_verdict_is_an_honest_stop_with_no_loop(tmp_path, monkeypatch):
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["collaborator"] = _reviews(NARRATION, then=NARRATION)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    assert len(_completions(replay)) == 1
    assert replay.result.error.startswith("FindingsOpen") and not replay.result.completed
    gaps = H.result_json(replay)["judgment_gaps"]
    assert gaps and gaps[0]["task"] == "t1" and gaps[0]["kind"] == "review"
    assert not [c for c in replay.calls if c.task == "t2"]


@pytest.mark.requirements_ledger
def test_the_allowance_is_one_per_job_not_per_task(tmp_path, monkeypatch):
    """t1's review is completed; t2's arrives without a verdict too and is
    not offered a second completion: the run stops on t2's unfinished review."""
    script = _script([PLAN_1, PLAN_2])
    script.overrides["collaborator"] = _reviews(NARRATION, then=CLEAN_T1, t2=NARRATION)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    assert len(_completions(replay)) == 1
    outcomes = [r["outcome"] for r in H.result_json(replay)["judgment_completions"]]
    assert outcomes == ["answered", "not attempted: the job's allowance is spent"]
    assert replay.result.error.startswith("FindingsOpen") and _budget(replay)["judgment_completions"] == 1


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("review", ["BLOCKING: add is wrong for negatives; example: add(-1, -2) should be -3\n"
                                    "R1: NOT MET - negatives", "NO FINDINGS\nR1: NOT MET - no negative test"])
def test_a_genuine_finding_is_never_routed_as_a_format_gap(tmp_path, monkeypatch, review):
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2, recheck="RESOLVED")
    script.overrides["collaborator"] = _reviews(review, then=CLEAN_T1)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    assert not _completions(replay), "a finding goes to the revision, never to a completion"
    assert [c for c in replay.calls if c.task == "t1" and c.role == "revision"]


def test_a_capped_review_is_never_completed(tmp_path, monkeypatch):
    """A cap is a transport fact, not an arrived reply: no completion."""
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["collaborator"] = lambda call, replay: (H.claude_cap("Still reading.", num_turns=14)
                                                             if call.task == "t1" else CLEAN_T2)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits(), settings=Settings(backend="cli", lead_max_turns=14))
    assert not _completions(replay) and not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_glued_recheck_is_completed_and_buys_no_extra_revision(tmp_path, monkeypatch):
    """R24's Grok recheck: RESOLVED glued to narration is no verdict. Old:
    it counted as UNRESOLVED and bought a second revision. Now the same
    seat completes it once, in the recheck role, and its RESOLVED closes
    exactly that finding."""
    def recheck(call, replay):
        return "RESOLVED" if COMPLETION in call.prompt else GLUED
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["collaborator"] = _reviews(
        "BLOCKING: add is wrong for negatives; example: add(-1, -2) should be -3\nR1: MET - app.py")
    script.overrides["recheck"] = recheck
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    done = _completions(replay)
    assert len(done) == 1 and done[0].role == "recheck"
    assert len([c for c in replay.calls if c.task == "t1" and c.role == "revision"]) == 1
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_genuine_unresolved_recheck_still_buys_the_fix_round_and_no_completion(tmp_path, monkeypatch):
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2, recheck="UNRESOLVED: negatives still wrong")
    script.overrides["collaborator"] = _reviews(
        "BLOCKING: add is wrong for negatives; example: add(-1, -2) should be -3\nR1: MET - app.py")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    assert not _completions(replay)
    assert len([c for c in replay.calls if c.task == "t1" and c.role == "revision"]) == 2


@pytest.mark.requirements_ledger
def test_parallel_children_share_the_one_completion(tmp_path, monkeypatch):
    """Both children's reviews arrive without a verdict; one budget holds
    the job's single claim, so exactly one is completed."""
    script = _script([BATCH])
    script.overrides["collaborator"] = lambda call, replay: (
        (CLEAN_T1 if call.task == "t1" else CLEAN_T2) if COMPLETION in call.prompt else NARRATION)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    assert len(_completions(replay)) == 1 and _budget(replay)["judgment_completions"] == 1
    outcomes = sorted(r["outcome"] for r in H.result_json(replay)["judgment_completions"])
    assert outcomes == ["answered", "not attempted: the job's allowance is spent"]
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_resumed_episode_carries_the_spent_claim(tmp_path, monkeypatch):
    """t1's review is completed in the first episode, which is interrupted
    at the quiescent point after t1. The resumed episode carries the claim
    from budget.json: t2's unfinished review is not offered a completion."""
    _interrupt_after(monkeypatch, 1)
    script = _script([PLAN_1, PLAN_2])
    script.overrides["collaborator"] = _reviews(NARRATION, then=CLEAN_T1, t2=NARRATION)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False,
                   limits=_limits())
    first = Path(replay.result.run_dir)
    assert json.loads((first / "budget.json").read_text())["judgment_completions"] == 1
    second = Path(_episode(replay, resume_from=first.name, limits=_limits()).run_dir)
    assert json.loads((second / "budget.json").read_text())["judgment_completions"] == 1
    result = json.loads((second / "result.json").read_text())
    assert [r["outcome"] for r in result["judgment_completions"]][-1] == \
        "not attempted: the job's allowance is spent"
    assert len(_completions(replay)) == 1 and not result["completed"]


def test_a_claim_is_never_refunded_and_a_refused_budget_write_fails_closed(tmp_path):
    budget = RunBudget(_limits(), path=tmp_path / "budget.json")
    assert budget.claim_judgment_completion(1, dict(task="t1", seat="a:b"))
    assert not budget.claim_judgment_completion(1, dict(task="t2", seat="a:b"))
    carried = RunBudget(_limits(), carried=dict(calls=0, input_tokens=0, output_tokens=0, elapsed_seconds=0,
                                                api_cost_usd=0, judgment_completions=1))
    assert not carried.claim_judgment_completion(1, dict(task="t3", seat="a:b"))


# -- C: the obligation view and an unpayable dispatch ------------------------------------

@pytest.mark.requirements_ledger
def test_the_planner_sees_obligations_and_the_allowance_left_from_recorded_facts(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES,
                   max_tasks=2, record_complete=False, limits=_limits())
    second = replay.of("orchestrator")[1].prompt
    assert "Obligations as the harness holds them now" in second
    assert "R1: covered by t1; taken on by t1" in second and "R2: not covered; taken on by no task yet" in second
    assert "last check PASSED on the current source" in second
    assert "Allowance left after the held finish:" in second and "s. A task needs one call" in second
    assert "Obligations as the harness holds them now" in replay.of("auditor")[-1].prompt
    assert "Allowance left" not in replay.of("auditor")[-1].prompt


@pytest.mark.requirements_ledger
def test_a_task_whose_lead_and_reviews_cannot_be_paid_is_not_started(tmp_path, monkeypatch):
    """Exact counts, no estimate: after t1 (orchestrator, requirements
    review, lead, review, close-out) and the planning call for t2, the
    calls left after the held goal question and audit cannot pay t2's lead
    and its review. t2 is not started before any call."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES,
                   max_tasks=2, record_complete=False, limits=_limits(max_calls=8))
    assert not [c for c in replay.calls if c.task == "t2"]
    result = H.result_json(replay)
    assert [n["task"] for n in result["not_started"]] == ["t2"]
    assert result["finish_refusals"][-1]["what"].startswith("dispatch: the calls left cannot pay")
    assert "t2 NOT RUN" in replay.result.error or "R2" in replay.result.error


# -- D: wall time held for the finish ------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_finish_holds_its_wall_time_inside_the_run_limit():
    """Policy finish-wall/1: 120 s per pending stage inside wall_seconds. An
    ordinary call gets what remains after both holds and is refused when
    none is left; the goal question gets what remains after the audit's
    hold; the audit gets the rest. Nothing is latched."""
    clock = _Clock()
    budget = RunBudget(RunLimits(max_calls=20, max_reported_tokens=10_000, wall_seconds=600), clock=clock)
    budget.protect_finish(FINISH_STAGES)
    ticket, left = budget.reserve()
    assert left == 600 - 240
    budget.finish(ticket, dict(input_tokens=1, output_tokens=0))
    clock.now = 400
    with pytest.raises(FinishReserved, match="240 s of wall time"):
        budget.reserve()
    with finishing("goal"):
        ticket, left = budget.reserve()
    assert left == 200 - 120
    budget.finish(ticket, dict(input_tokens=1, output_tokens=0))
    with finishing("audit"):
        ticket, left = budget.reserve()
    assert left == 200
    snapshot = budget.snapshot()
    assert snapshot["stop_reason"] == "" and snapshot["finish_protection"]["wall_policy"] == "finish-wall/1"


def test_no_wall_is_held_when_the_policy_is_off_or_nothing_is_pending():
    clock = _Clock()
    budget = RunBudget(RunLimits(max_calls=20, max_reported_tokens=10_000, wall_seconds=600,
                                 finish_wall_seconds=0), clock=clock)
    budget.protect_finish(FINISH_STAGES)
    assert budget.reserve()[1] == 600
    plain = RunBudget(RunLimits(max_calls=20, max_reported_tokens=10_000, wall_seconds=600), clock=clock)
    assert plain.reserve()[1] == 600, "no stage pending: nothing held"
