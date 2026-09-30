"""A check that still fails after its fix round is requirement debt, not a
run stop (survey plan, 2026-09-30; the second remaining task-level stop after
session.TaskFailed).

The task's COVERS go NOT MET under a ``check.failed`` finding; the
orchestrator names a repair task with RESOLVES, which needs no renders: its
own integration check passing and a clean close settle it. Without the
requirements ledger, or without a COVERS line, today's CheckFailing stop
stands. Whole-controller replays through the real gate.
"""

import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, T1, Script

pytestmark = pytest.mark.requirements_ledger

REQS = "REQUIREMENTS:\nR1: add returns the sum\n"
IMPLEMENT = DECL_T1 + "\nCOVERS: R1"
REPAIR = "KIND: architect complex\nSCOPE: " + json.dumps(T1) + "\nFix add so the test passes.\nCOVERS: R1\nRESOLVES: F1"
MET = "R1: MET - app.py"


def _plan(*replies):
    replies = list(replies)

    def orchestrator(call, replay):
        return replies.pop(0) if replies else "DONE"
    return orchestrator


def _looked(call, replay):
    return "Looked; left it.\nCHANGED: []"


def _fixes(call, replay):
    H.write(call, {"app.py": FIXED})
    return 'Fixed add.\nCHANGED: ["app.py"]'


def _run(tmp_path, monkeypatch, orchestrator, leads, **kw):
    overrides = dict(orchestrator=orchestrator, lead=lambda call, replay: leads[call.task](call, replay),
                     **{"requirements-review": lambda call, replay: "COMPLETE",
                        "auditor": lambda call, replay: MET})
    replay = H.run(tmp_path, monkeypatch, Script(**overrides), files=FILES, max_tasks=4, **kw)
    replay.findings = H.result_json(replay).get("findings", [])
    return replay


def _task(replay, tid):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == tid)


def test_a_failed_check_becomes_a_finding_and_a_repair_task_resolves_it(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _plan(REQS + IMPLEMENT, REPAIR), {"t1": _looked, "t2": _fixes})
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the run went on after the failed check"
    (f1,) = replay.findings
    assert f1["id"] == "F1" and f1["kind"] == "check.failed" and f1["task"] == "t1"
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t2"
    assert f1["check"]["attempt"] == 2 and f1["target"] is None
    assert H.gate_results(replay)[-1] == "PASSED"
    assert replay.result.completed, replay.result.error
    t1 = _task(replay, "t1")
    assert t1["checks"][-1]["passed"] is False, "t1's own record is unchanged"
    assert all(f["recovered"] for f in t1["facts"] if f["kind"] == "product"), "the failure is history"
    assert H.result_json(replay)["requirements"]["status"]["R1"].startswith("met")


def test_the_orchestrator_is_told_the_check_finding_and_how_it_resolves(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _plan(REQS + IMPLEMENT, REPAIR), {"t1": _looked, "t2": _fixes})
    second = replay.of("orchestrator")[1].prompt
    assert "--- OPEN FINDINGS ---" in second
    assert "F1 (found by t1, requirements R1): the project check still failed after t1's work" in second
    assert "A failed-check finding resolves when the resolving task's own integration check passes" in second
    assert "RESOLVES: F<n>" in second


def test_a_repair_whose_check_still_fails_leaves_the_finding_open(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _plan(REQS + IMPLEMENT, REPAIR), {"t1": _looked, "t2": _looked})
    f1, f2 = replay.findings
    assert f1["status"] == "open" and f1["last_attempt"].startswith("t2: its integration gate failed")
    assert f2["task"] == "t2" and f2["kind"] == "check.failed" and f2["status"] == "open", \
        "the failed repair leaves its own debt too"
    assert not replay.result.completed
    assert replay.result.error.startswith("FindingsUnresolved"), replay.result.error


def test_with_the_ledger_off_the_failed_check_is_todays_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "0")
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1), lead=_looked), files=FILES,
                   max_tasks=3)
    assert replay.result.error.startswith("CheckFailing")
    assert H.result_json(replay).get("findings", []) == []
