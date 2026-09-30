"""The explicit-task entry point (J43, 2026-09-30; Codex on #42).

``run_project(tasks=[...])`` runs an operator-written task list under the
runner's own lifecycle: lock, gate plan, budget, readiness, the dependency
watch, every per-task check and stop, the result record. No planner call, no
acknowledgment call, no task the list did not name. The list ending is not a
DONE: the record says which tasks closed and that nobody judged the goal.
"""
import json
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.test_direct_tier import FAVICON, _files, _task
from tests.lifecycle.test_lifecycle_matrix import CLOSEOUT, FILES, FIXED, T1, T2, Script

FAVICON_TASK = "KIND: frontend simple\nSCOPE: " + json.dumps(FAVICON) + "\nAdd a favicon route and link."
README_TASK = "KIND: docs simple\nSCOPE: " + json.dumps(T2) + "\nDocument add in README.md."
ADD_TASK = "KIND: architect complex\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."


def _favicon_lead(call, replay):
    H.write(call, {"static/favicon.svg": "<svg/>\n",
                   "templates/index.html": '<html><head><link rel="icon" href="/favicon.ico"></head></html>\n'})
    H.evidence(Path(call.cwd), call.task, age=H.FRESH)
    return 'Added the favicon.\nCHANGED: ["static/favicon.svg", "templates/index.html"]'


def _readme_lead(call, replay):
    H.write(call, {"README.md": "# app\n\nadd(a, b) returns a + b.\n"})
    return 'Documented add.\nCHANGED: ["README.md"]'


def _is_readme(call) -> bool:
    return "Intended result: README documents add" in call.prompt


def _lead(call, replay):
    return _readme_lead(call, replay) if _is_readme(call) else _favicon_lead(call, replay)


def _orchestrator(call, replay):
    raise AssertionError(f"the orchestrator was called on an explicit-task run: {call.role}")


def _script(**roles):
    base = dict(orchestrator=_orchestrator, lead=_lead, closeout=lambda c, r: CLOSEOUT,
                **{"design-review": lambda c, r: "APPROVED",
                   "collaborator": lambda c, r: "BLOCKING: the link tag has no type attribute"})
    return Script(**{**base, **roles})


def _run(tmp_path, monkeypatch, tasks, *, files=None, script=None, **kw):
    kw.setdefault("max_tasks", 4)
    return H.run(tmp_path, monkeypatch, script or _script(), files=files or _files(), tasks=tasks, **kw)


def test_listed_tasks_run_in_order_with_no_planner_call(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [FAVICON_TASK, README_TASK])
    assert replay.of("orchestrator") == [], "no planner, acknowledgment or goal call"
    assert [t["task_id"] for t in replay.workflow["tasks"]] == ["t1", "t2"]
    assert replay.result.completed, replay.result.error
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())["explicit_tasks"]
    assert record["listed"] == 2 and [r["task"] for r in record["ran"]] == ["t1", "t2"]
    assert record["tasks_closed_clean"] == ["t1", "t2"] and record["tasks_unfinished"] == []
    assert record["goal_judged"] is False and record["invalid"] is None and record["not_run"] == 0
    assert (replay.project / "README.md").read_text().startswith("# app\n\nadd(")
    assert _task(replay)["contract"]["required"]["tier"] == "normal"
    assert {c.role.split(":")[0] for c in replay.calls} >= {"lead", "collaborator", "closeout"}


def test_a_listed_task_may_carry_the_direct_tier_label(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [FAVICON_TASK + "\nTIER: direct", README_TASK], direct_tier=True)
    assert replay.of("orchestrator") == []
    t1, t2 = _task(replay), _task(replay, "t2")
    assert t1["contract"]["required"]["tier"] == "direct" and t2["contract"]["required"]["tier"] == "normal"
    assert replay.result.completed, replay.result.error
    by_task = replay.workflow["calls_by_task"]
    assert "collaborator" not in by_task["t1"] and "closeout" not in by_task["t1"]
    assert "closeout" in by_task["t2"]


@pytest.mark.parametrize("text,reason", [
    ("KIND: docs simple\nSCOPE: {not json}\nDocument add.", "scope"),
    ("KIND: docs simple\nDocument add.", "scope"),
    ("KIND: docs simple\nSCOPE: " + json.dumps(T2) + "\nDONE", "DONE"),
    ("DONE", "DONE"),
])
def test_a_task_the_loop_would_send_back_stops_the_run_before_any_lead_call(tmp_path, monkeypatch, text, reason):
    replay = _run(tmp_path, monkeypatch, [README_TASK, text], record_complete=False)
    assert replay.result.error.startswith("TaskListInvalid: listed task 2:"), replay.result.error
    assert reason.lower() in replay.result.error.lower()
    assert replay.of("orchestrator") == [] and len(replay.of("lead")) == 1
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())["explicit_tasks"]
    assert record["invalid"]["index"] == 2 and record["not_run"] == 1
    assert record["tasks_closed_clean"] == ["t1"] and not replay.result.completed


def test_a_list_longer_than_the_cap_is_refused_before_any_call(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match=r"max_tasks \(1\) must cover the 2 listed task\(s\)"):
        _run(tmp_path, monkeypatch, [README_TASK, FAVICON_TASK], max_tasks=1, record_complete=False)
    assert not (tmp_path / "project" / ".quadratus" / "runs").exists(), "refused before a run directory"


def test_the_session_itself_refuses_a_list_past_the_cap_with_a_typed_stop(tmp_path):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=None))
    with pytest.raises(ValueError, match="must cover"):
        session.run(max_tasks=1, tasks=[README_TASK, ADD_TASK])
    assert session.run_outcome.stop() is not None and "must cover" in session.run_outcome.stop().detail
    assert session.explicit is None


def test_a_refusal_on_a_listed_task_is_still_the_refusal_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [README_TASK],
                  script=_script(lead=lambda c, r: H.claude_refusal("cyber")), record_complete=False)
    assert replay.result.error.startswith("Provider") and not replay.result.completed
    assert replay.of("orchestrator") == []
    assert (replay.project / "README.md").read_text() == FILES["README.md"], "source preserved"


def test_the_run_budget_still_stops_a_listed_run(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [README_TASK, FAVICON_TASK],
                  limits=RunLimits(max_calls=1, max_reported_tokens=6_000_000, wall_seconds=600,
                                   max_concurrent_workers=2),
                  record_complete=False)
    assert "budget" in replay.result.error.lower(), replay.result.error
    assert replay.of("orchestrator") == [] and not replay.result.completed


def test_a_failed_listed_task_keeps_its_work_and_the_list_ends_incomplete(tmp_path, monkeypatch):
    def lead(call, replay):
        if not _is_readme(call):
            H.write(call, {"README.md": "# rewritten\n", "static/favicon.svg": "<svg/>\n"})
            return 'Did both.\nCHANGED: ["README.md", "static/favicon.svg"]'
        return _readme_lead(call, replay)
    replay = _run(tmp_path, monkeypatch, [FAVICON_TASK, README_TASK], script=_script(lead=lead),
                  record_complete=False)
    assert replay.of("orchestrator") == []
    assert _task(replay)["closed_as"] == "failed" and _task(replay, "t2")["closed_as"] == "closed"
    assert not replay.result.completed and replay.result.error, "the list ending is not a DONE"
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())["explicit_tasks"]
    assert record["tasks_unfinished"] == ["t1"] and record["tasks_closed_clean"] == ["t2"]


def test_the_cli_reads_the_task_list_from_a_json_file(tmp_path):
    from quadratus.cli import _task_list
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([README_TASK, ADD_TASK]))
    assert _task_list(str(path)) == [README_TASK, ADD_TASK]
    assert _task_list(None) is None
    path.write_text(json.dumps(["", README_TASK]))
    with pytest.raises(ValueError, match="non-empty task texts"):
        _task_list(str(path))
    path.write_text("{}")
    with pytest.raises(ValueError):
        _task_list(str(path))


def test_the_direct_and_normal_tiers_deliver_the_same_checked_readme(tmp_path, monkeypatch):
    """An unrelated task type on both tiers: same source, same check verdict."""
    (tmp_path / "direct").mkdir()
    (tmp_path / "normal").mkdir()
    files = {**FILES, "app.py": FIXED}
    direct = _run(tmp_path / "direct", monkeypatch, [README_TASK + "\nTIER: direct"], files=files,
                  script=_script(lead=_readme_lead), direct_tier=True)
    normal = _run(tmp_path / "normal", monkeypatch, [README_TASK], files=files, script=_script(lead=_readme_lead))
    assert direct.result.completed and normal.result.completed, (direct.result.error, normal.result.error)
    assert (direct.project / "README.md").read_text() == (normal.project / "README.md").read_text()
    assert H.gate_results(direct)[-1] == H.gate_results(normal)[-1] == "PASSED"
    assert _task(direct)["contract"]["required"]["tier"] == "direct"
    assert "closeout" not in direct.workflow["calls_by_task"]["t1"]
    assert "closeout" in normal.workflow["calls_by_task"]["t1"]
