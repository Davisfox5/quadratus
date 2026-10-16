"""The finish within the job's own limits (Davis, 2026-10-16).

R22 closed three tasks and spent its 20 calls before the cap's goal
question; the audit after it never ran. Two changes, no limit moved:

- the job's last dispatched wave closes with a factual record the harness
  writes (its reviews as they ended, its last check and source, its open
  debt, evidence pointers), not a model summary no later planning call
  reads;
- the goal question and the audit are held one call and one full per-call
  reserve each from ordinary work. An ordinary call that would spend them
  is refused before it starts and recorded; what it would have done stays
  undone and every current debt gate still decides, so a refused review
  leaves its finding open and the run stops on it rather than finishing.

Whole-run replays below the vendor CLI (tests.lifecycle.harness).
"""

import json
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_step2_recovery import _episode, _interrupt_after, _simple

PLAN_1 = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
          + _simple("app.py", "Implement add in app.py.", "R1"))
PLAN_2 = _simple("README.md", "Document add in README.md.", "R2")
BATCH = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\nPARALLEL\n"
         + _simple("app.py", "Implement add in app.py.", "R1") + "\n---\n"
         + _simple("README.md", "Document add in README.md.", "R2"))
README = "# app\n\n`add(a, b)` returns the sum.\n"


def _limits(calls):
    return RunLimits(max_calls=calls, max_reported_tokens=6_000_000, wall_seconds=600, max_concurrent_workers=2)


def _script(plan, *, t2_review="BLOCKING: README lacks an example\nR2: MET - README.md",
            recheck="RESOLVED", closeout=None):
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plan[n - 1] if n <= len(plan) else "DONE"

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collaborator(call, replay):
        return t2_review if call.task == "t2" else "NO FINDINGS\nR1: MET - app.py"

    extra = {"closeout": closeout} if closeout else {}
    return Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                  recheck=lambda call, replay: recheck,
                  auditor=lambda call, replay: "R1: MET - app.py\nR2: MET - README.md",
                  **{"requirements-review": lambda call, replay: "COMPLETE"}, **extra)


def _budget(replay):
    return json.loads((Path(replay.result.run_dir) / "budget.json").read_text())


def _roles(replay):
    return [(c.task, c.role) for c in replay.calls]


@pytest.mark.requirements_ledger
def test_serial_the_last_wave_closes_factually_and_the_finish_runs_inside_the_limit(tmp_path, monkeypatch):
    """12 calls. On 12f8 this script spent call 11 on t2's model close-out,
    asked the goal question at 12 and stopped at call_limit before the
    audit. Now t2 closes factually, the goal question is call 11 and the
    audit call 12, and the run completes. t1's close-out, which the next
    planning call reads, is still the lead's own."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=2,
                   limits=_limits(12), record_complete=False)
    assert _roles(replay)[-2:] == [("run", "orchestrator"), ("audit", "auditor")]
    assert [c.task for c in replay.of("closeout")] == ["t1"]
    assert replay.result.completed, replay.result.error
    budget = _budget(replay)
    assert budget["reserved_attempts"] == 12 and budget["finish_protection"]["started"] == ["goal", "audit"]
    assert budget["finish_protection"]["refusals"] == []
    record = json.loads(replay.artifact_texts("factual-closeout")[0])
    assert record["task"] == "t2" and "last dispatched wave" in record["why"]
    assert record["reviews"] == [["openai:gpt-5.6-sol", "reviewed: blocking findings"],
                                 ["openai:gpt-5.6-sol", "recheck: resolved"]]
    assert record["check"] == "last check PASSED on the current source"
    assert record["changed"] == ["README.md"] and len(record["evidence"]) == 2
    summary = H.result_json(replay)
    t2 = next(t for t in summary["workflow"]["tasks"] if t["task_id"] == "t2")
    assert t2["edges"].get("review") is True, "the factual record changes no review edge"
    ledger = (Path(replay.result.run_dir) / "ledger.md").read_text()
    assert "Recorded by the harness, not a model" in ledger and "No collaborator review" not in ledger
    assert "codebase map was not amended" in ledger


@pytest.mark.requirements_ledger
def test_serial_a_review_the_finish_cannot_afford_is_refused_recorded_and_never_resolved(tmp_path, monkeypatch):
    """11 calls: t2's recheck would leave one call for two finishing
    judgments, so it is refused before it starts. Its finding stays open,
    the existing open-finding stop ends the run, and neither the goal
    question nor the audit is forced across that debt. On 12f8 the recheck
    ran and the run stopped at call_limit on t2's model close-out."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=2,
                   limits=_limits(11), record_complete=False)
    assert not [c for c in replay.of("recheck")], "the refused recheck never launched"
    assert not replay.of("auditor") and _roles(replay)[-1] == ("t2", "revision")
    assert replay.result.error.startswith("FindingsOpen") and "not re-checked after the revision" in replay.result.error
    refusals = H.result_json(replay)["finish_refusals"]
    assert [r["what"] for r in refusals] == ["recheck by openai:gpt-5.6-sol"]
    budget = _budget(replay)
    assert budget["stop_reason"] == "" and budget["finish_protection"]["pending"] == ["goal", "audit"]
    assert budget["finish_protection"]["refusals"][0]["calls_after"] == 1
    record = json.loads(replay.artifact_texts("factual-closeout")[0])
    assert record["finish_refused"] == [refusals[0]["what"]]
    assert [r[1] for r in record["reviews"]][-1] == "recheck not run: refused to keep the finish"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("calls", [13, 12])
def test_parallel_last_wave_closes_factually_and_a_refused_merge_recheck_stays_open(tmp_path, monkeypatch, calls):
    """Both children are the last wave and close factually. t2's finding is
    left open in its copy (UNRESOLVED there, after its two fix rounds) and
    rechecked once on the merged source. With the calls for it, that recheck resolves it and the goal
    question and audit follow; one call short, it is refused, its finding is
    kept, and the run stops on it."""
    def recheck(call, replay):
        return "RESOLVED" if "now merged" in call.prompt else "UNRESOLVED: no example yet"

    script = _script([BATCH])
    script.overrides["recheck"] = recheck
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_limits(calls),
                   record_complete=False)
    assert not replay.of("closeout"), "no model close-out in the last wave"
    assert sorted(json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")) == ["t1", "t2"]
    batch = H.result_json(replay)["parallel_batches"][0]
    verdict = batch["post_merge_rechecks"][0]["rechecked"][0]["verdict"]
    if calls == 13:
        assert verdict == "resolved" and replay.result.completed, replay.result.error
        assert _roles(replay)[-2:] == [("run", "orchestrator"), ("audit", "auditor")]
    else:
        assert verdict == "kept: recheck refused to keep the finish"
        assert not replay.of("auditor") and not replay.result.completed
        assert "FindingsOpen" in replay.result.error
        assert H.result_json(replay)["finish_refusals"][0]["what"].startswith("post-merge recheck by")


@pytest.mark.requirements_ledger
def test_a_resumed_episode_holds_the_same_finish_from_the_carried_spend(tmp_path, monkeypatch):
    """Interrupted after t1 at a quiescent point, then resumed: the second
    episode carries the job's spend and holds the goal question and the
    audit again, once, from the same allowance. Twelve calls in all, as
    the uninterrupted job: ordinary work never takes the finish's two."""
    _interrupt_after(monkeypatch, 1)
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=2,
                   limits=_limits(12), record_complete=False)
    first = Path(replay.result.run_dir)
    assert "KeyboardInterrupt" in replay.result.error
    held = json.loads((first / "budget.json").read_text())["finish_protection"]
    assert held["pending"] == ["goal", "audit"] and held["started"] == []
    second = Path(_episode(replay, resume_from=first.name, limits=_limits(12)).run_dir)
    budget = json.loads((second / "budget.json").read_text())
    assert budget["finish_protection"]["started"] == ["goal", "audit"] and budget["finish_protection"]["pending"] == []
    assert budget["reserved_attempts"] <= 12 and budget["stop_reason"] == ""
    assert json.loads((second / "result.json").read_text())["completed"]


# -- a refused first editor is NOT RUN, and the finish the gates allow follows ----
# (root reconciliation of R23, 6208197375). Every replayed call reports 110
# tokens. With a 1,000 reserve and a 3,000 per-call ceiling, an editing call
# needs 3,000 + 2,000 held, so it starts only while 5,000 tokens remain; a
# review, or the goal question beside the audit's hold, needs 3,000 or 2,000.

ONE = "REQUIREMENTS:\nR1: add(a, b) returns the sum\n" + _simple("app.py", "Implement add in app.py.", "R1")
MORE = _simple("app.py", "Add a docstring to add in app.py.", "R1")


def _tight(total):
    return RunLimits(max_calls=40, max_reported_tokens=total, wall_seconds=600, max_concurrent_workers=2,
                     reserve_tokens_per_call=1_000, max_tokens_per_call=3_000)


@pytest.mark.requirements_ledger
def test_a_first_editor_refused_before_any_write_is_not_run_and_the_finish_follows(tmp_path, monkeypatch):
    """5,440 tokens. After t1 and the planning call for t2, 4,780 remain:
    t2's lead is exposed at 3,000 and refused before it starts. Old: that
    refusal stopped the run with neither finishing judgment. Now t2 is NOT
    RUN, nothing is planned after it, the goal question and the audit run,
    and the run still is not complete: the work the plan asked for was not
    done."""
    script = _script([ONE, MORE], t2_review="NO FINDINGS\nR1: MET - app.py")
    script.overrides["auditor"] = lambda call, replay: "R1: MET - app.py"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_tight(5_440),
                   record_complete=False)
    assert not [c for c in replay.calls if c.task == "t2"], "t2's lead never started"
    assert _roles(replay)[-3:] == [("run", "orchestrator"), ("run", "orchestrator"), ("audit", "auditor")]
    assert len(replay.of("orchestrator")) == 3, "plan, plan t2, goal question: no planning after the refusal"
    result = H.result_json(replay)
    assert [n["task"] for n in result["not_started"]] == ["t2"]
    assert result["finish_refusals"][-1]["what"] == "first editing call by " + result["not_started"][0]["lead"]
    t2 = next(t for t in result["workflow"]["tasks"] if t["task_id"] == "t2")
    assert t2["closed_as"] == "not_started"
    assert not replay.result.completed and "t2 NOT RUN" in replay.result.error
    assert result["requirements"]["audits"][-1]["verdicts"]["R1"]["met"] is True
    budget = _budget(replay)
    refusal = budget["finish_protection"]["refusals"][0]
    assert refusal["exposure"] == 3_000 and refusal["tokens_after"] == 4_780 - 3_000
    assert budget["finish_protection"]["started"] == ["goal", "audit"] and budget["stop_reason"] == ""


@pytest.mark.requirements_ledger
def test_a_not_run_task_never_carries_the_finish_past_partial_debt(tmp_path, monkeypatch):
    """5,330 tokens: t1's lead stops at its turn cap (partial), then t2's
    first editing call is refused. t2 is NOT RUN; t1's capped work is still
    debt, so the cap's own decision stops before the goal question and no
    audit runs."""
    from quadratus.config import Settings

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lead
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_tight(5_330),
                   record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    assert [n["task"] for n in H.result_json(replay)["not_started"]] == ["t2"]
    assert len(replay.of("orchestrator")) == 2 and not replay.of("auditor"), "no goal question, no audit"
    assert replay.result.error.startswith("GoalUnconfirmedAtCap") and "t1 not continued" in replay.result.error
    assert _budget(replay)["finish_protection"]["pending"] == ["goal", "audit"]


@pytest.mark.requirements_ledger
def test_a_refusal_after_the_task_has_edited_still_stops_the_run(tmp_path, monkeypatch):
    """5,660 tokens: t2's lead is admitted with exactly 5,000 left, writes
    README.md and asks to FETCH; its second turn is refused. Work was
    written, so this is not a task that never started: the run stops on the
    refusal as before, with the edit kept and nothing recorded NOT RUN."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README})
        return "FETCH: no-such-artifact"
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lead
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_tight(5_660),
                   record_complete=False)
    assert len([c for c in replay.calls if c.task == "t2"]) == 1
    assert replay.result.error.startswith("FinishReserved") and H.result_json(replay)["not_started"] == []
    assert (replay.project / "README.md").read_text() == README
    assert not replay.of("auditor")
