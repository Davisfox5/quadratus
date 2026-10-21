"""Every task closes on the harness's own record; no model summary call
exists any more (Davis, 2026-10-21).

The ticket-workflow run's only task finished its lead, review, revision
and recheck, then spent a 60 s summary call that returned nothing and
ended the run with the work unrecorded. The record is now written by the
harness from what it holds, for every task, whether or not a later planning
call reads it. Real Session and scripted replies; every claim is a record
the harness wrote. Reviews, the goal question and the final audit are
enforced exactly as before; a stale or failed check is never promoted."""
import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_finish_capacity import _BLOCKING, PLAN_1, PLAN_2, _roles, _script
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script


def _factual(replay):
    """The harness records, by task id (artifact order is not chronological)."""
    return sorted((json.loads(t) for t in replay.artifact_texts("factual-closeout")), key=lambda r: r["task"])


@pytest.mark.requirements_ledger
def test_a_reviewed_task_closes_on_the_harness_record_and_the_run_goes_on_to_the_next(tmp_path, monkeypatch):
    script = _script([PLAN_1, PLAN_2])
    script.overrides["collaborator"] = lambda call, replay: (
        _BLOCKING if call.task == "t1" else "NO FINDINGS\nR2: MET - README.md")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    assert replay.result.completed, replay.result.error
    assert replay.of("closeout") == [], "no summary call for any task"
    assert not any("The task is finished at this checkpoint" in c.prompt for c in replay.calls)
    records = _factual(replay)
    assert [r["task"] for r in records] == ["t1", "t2"]
    t1 = records[0]
    assert "every task closes on the harness's record" in t1["why"]
    assert t1["reviews"] == [["claude:opus", "reviewed: blocking findings"],
                             ["claude:opus", "recheck: RFT1-1 resolved"]], t1["reviews"]
    assert t1["check"].startswith("last check PASSED") and "on the current source" in t1["check"]
    assert t1["proof_scenarios"] == ["P1: binding bound (tests/test_app.py::test_add), execution ran"]
    assert t1["source"] and t1["changed"] == ["app.py"] and t1["design"] == "no design evidence required"
    # The reviews, the revision and its recheck happened; the next planning
    # call read the record; the goal question and the final audit still ran.
    roles = _roles(replay)
    assert ("t1", "collaborator") in roles and ("t1", "revision") in roles and ("t1", "recheck") in roles
    assert ("t2", "collaborator") in roles and roles[-1] == ("audit", "auditor")
    planning = replay.of("orchestrator")
    assert len(planning) == 3, "plan t1, plan t2 after t1's record, then the goal question"
    assert "Recorded by the harness, not a model" in planning[1].prompt
    assert "reviewed: blocking findings" in planning[1].prompt and "recheck: RFT1-1 resolved" in planning[1].prompt
    assert "Proof scenarios: P1: binding bound" in planning[1].prompt
    assert "DECISIONS" not in planning[1].prompt.split("Recorded by the harness")[1][:600]


@pytest.mark.requirements_ledger
def test_a_failed_check_is_recorded_as_failed_and_the_run_is_incomplete(tmp_path, monkeypatch):
    def lead(call, replay):
        return 'Looked at it.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: []'
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lead
    script.overrides["gate-fix"] = lambda call, replay: "Could not fix it.\nCHANGED: []"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    assert not replay.result.completed
    assert replay.of("closeout") == []
    records = _factual(replay)
    assert records and records[0]["task"] == "t1"
    assert records[0]["check"].startswith("last check FAILED") and "PASSED" not in records[0]["check"]
    assert all("PASSED" not in r["check"] for r in records)
    assert any("FAILED" in c.prompt and "Recorded by the harness" in c.prompt for c in replay.of("orchestrator")[1:]) \
        or len(replay.of("orchestrator")) == 1


@pytest.mark.requirements_ledger
def test_a_model_that_would_refuse_or_stall_a_summary_is_never_asked(tmp_path, monkeypatch):
    """The old close-out could be refused or time out; the record cannot."""
    script = _script([PLAN_1, PLAN_2], closeout=lambda call, replay: H.claude_refusal("cyber"))
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    assert replay.result.completed, replay.result.error
    assert replay.of("closeout") == [] and not replay.artifact_texts("closeout-refused")
    assert [r["task"] for r in _factual(replay)] == ["t1", "t2"]


def test_a_legacy_task_without_a_ledger_closes_the_same_way(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, max_tasks=1, record_complete=False)
    assert replay.of("closeout") == [] and len(_factual(replay)) == 1
    assert (replay.project / "app.py").read_text() == FIXED
