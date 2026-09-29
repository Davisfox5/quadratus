"""J39: a task that breaks its own rules fails the task, not the run
(operator ruling, 2026-09-28; ``session.TaskFailed``).

Two live runs each ended on the first task-level fault (a scope overrun, then
an unparsed request), so every later task's faults stayed unseen. These cases
drive the whole controller: the failing task keeps its edits, carries a
terminal ``failed`` fact, hands the remaining work back with CONTINUES, a
clean continuation recovers it, and two unfinished tasks in a row trip the
breaker with a named stop.
"""

import json

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FILES_OK, FIXED, T1, Script


def _task(replay, task_id):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == task_id)


def _active(task):
    return [f["kind"] for f in task["facts"] if f["terminal"] and not f["recovered"]]


def _stop(replay):
    stops = [f for f in replay.workflow["run"]["facts"]
             if f["terminal"] and not f["recovered"] and f["legacy"] is not None]
    return stops[-1] if stops else None


CONTINUE_T1 = ("KIND: architect complex\nSCOPE: "
               + json.dumps({**T1, "permitted_paths": T1["permitted_paths"] + ["README.md"]})
               + "\nFinish add: revert the README change.\nCONTINUES: t1")


def _then_continue_then_done(call, replay):
    n = len(replay.of("orchestrator"))
    return DECL_T1 if n == 1 else CONTINUE_T1 if n == 2 else "DONE"


def _overrun(call, replay):
    """t1 writes outside its scope; the continuation puts the tree right."""
    if call.task == "t1":
        H.write(call, {"app.py": FIXED, "README.md": "# rewritten\n"})
        return 'Did both.\nCHANGED: ["README.md", "app.py"]'
    H.write(call, {"README.md": FILES["README.md"]})
    return 'Reverted the README; add is in place.\nCHANGED: ["README.md"]'


def test_a_scope_overrun_fails_the_task_keeps_the_work_and_the_run_goes_on(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_continue_then_done, lead=_overrun),
                   files=FILES, max_tasks=3)
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "failed" and _active(t1) == [], "recovered by the continuation"
    failed = [f for f in t1["facts"] if f["kind"] == "failed"]
    assert len(failed) == 1 and failed[0]["detail"].startswith("scope: Task exceeded its declared scope")
    assert failed[0]["recovered"] is True
    assert t1["partial"]["changed"] == ["README.md", "app.py"]
    assert not [c for c in replay.of("collaborator") if c.task == "t1"], "no review of a failed task"
    assert not [c for c in replay.of("closeout") if c.task == "t1"], "no close-out call for a failed task"
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    assert replay.result.completed, replay.result.error
    assert (replay.project / "app.py").read_text() == FIXED
    result = json.loads((replay.result.run_dir / "result.json").read_text())
    assert result["failed_tasks"] == ["t1"] and result["turn_limited_tasks"] == []
    assert replay.artifacts("task-failed")


def test_the_orchestrator_and_the_continuation_are_told_what_failed(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_continue_then_done, lead=_overrun),
                   files=FILES, max_tasks=3)
    second = replay.of("orchestrator")[1].prompt
    assert "FAILED before finishing: its edits exceeded the declared scope" in second
    assert "CONTINUES: t1" in second and "do not assume any of it is finished" in second
    continuation = [c for c in replay.of("lead") if c.task == "t2"][0].prompt
    assert "## Handoff from t1" in continuation
    assert "failed before finishing (scope)" in continuation
    assert "README.md" in continuation


def test_a_failed_task_never_continued_blocks_completion(tmp_path, monkeypatch):
    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=_overrun),
                   files=FILES, max_tasks=3)
    assert not replay.result.completed
    assert replay.result.error.startswith("DoneWithOpenWork")
    assert "capped or failed task(s) t1" in replay.result.error
    assert _active(_task(replay, "t1")) == ["failed"]
    assert (replay.project / "README.md").read_text() == "# rewritten\n", "the work is kept"


def test_an_unparsed_request_fails_the_task_and_keeps_the_reply(tmp_path, monkeypatch):
    """Run 9: a request line that failed to parse, with no CHANGED line."""
    def lead(call, replay):
        if call.task == "t1":
            return "I need more context.\nWORKER {not json"
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_continue_then_done, lead=lead),
                   files=FILES, max_tasks=3)
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "failed"
    fact = next(f for f in t1["facts"] if f["kind"] == "failed")
    assert fact["detail"].startswith("reply: ") and "neither a request nor a delivery" in fact["detail"]
    assert replay.artifacts("unparsed-request"), "the reply is kept"
    assert replay.result.completed, replay.result.error


def test_a_lead_that_never_stops_asking_fails_its_task_only(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            return "CONSULT Sol: what should add return?"
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_continue_then_done, lead=lead,
                                                 consultant=lambda call, replay: "The sum."),
                   files=FILES, max_tasks=3)
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "failed"
    fact = next(f for f in t1["facts"] if f["kind"] == "failed")
    assert fact["detail"].startswith("channel: Consult budget exhausted")
    assert replay.result.completed, replay.result.error


def test_two_failed_tasks_in_a_row_trip_the_breaker_with_a_named_stop(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "other.py": f"# written by {call.task}\n"})
        return 'Did both.\nCHANGED: ["other.py", "app.py"]'
    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else CONTINUE_T1
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES, max_tasks=4)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "no third lead"
    assert not replay.result.completed
    assert replay.result.error.startswith("TaskFailureBreaker: 2 serial tasks were left unfinished in a row "
                                          "(t1 failed: scope, t2 failed: scope)")
    assert _stop(replay)["kind"] == "failed" and _stop(replay)["legacy"] == "TaskFailureBreaker"
    assert (replay.project / "other.py").read_text() == "# written by t2\n", "every failed task's edits stay"
    in_flight = json.loads((replay.result.run_dir / "in-flight.json").read_text())
    assert [(r["task"], r["cause"]) for r in in_flight["failed"]] == [("t1", "scope"), ("t2", "scope")]
    assert in_flight["turn_limited"] == []
    result = json.loads((replay.result.run_dir / "result.json").read_text())
    assert result["failed_tasks"] == ["t1", "t2"] and result["error"] == replay.result.error


def test_a_cap_then_a_failure_is_the_same_breaker(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"README.md": "# app\n\npartial\n"})
            if call.vendor == "grok":
                return H.grok_ok("Documenting add; not finished.", stop="cancelled", num_turns=14)
            return H.claude_cap("Documenting add; not finished.", num_turns=14)
        H.write(call, {"app.py": "def add(a, b):\n    return 1\n"})
        return 'Finished.\nCHANGED: ["app.py"]'
    def orchestrator(call, replay):
        first = "KIND: docs simple\nSCOPE: " + json.dumps(
            dict(permitted_paths=["README.md"], intended_result="README documents add",
                 acceptance=["README names add"], max_lines=20)) + "\nDocument add in README.md."
        if len(replay.of("orchestrator")) == 1:
            return first
        return first.replace("Document add in README.md.", "Finish the README.\nCONTINUES: t1")
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES_OK,
                   max_tasks=4, settings=Settings(backend="cli", lead_max_turns=14))
    assert replay.result.error.startswith("TaskFailureBreaker: 2 serial tasks were left unfinished in a row "
                                          "(t1 capped, t2 failed: scope)")
    assert _task(replay, "t1")["closed_as"] == "turn_limited" and _task(replay, "t2")["closed_as"] == "failed"


@pytest.mark.parametrize("kind", ["scope", "transport", "reply", "channel"])
def test_what_the_harness_cannot_inspect_still_stops_the_run(tmp_path, monkeypatch, kind):
    """The task-level path needs an inspected tree; without one the run stops.
    The reply and channel cases reach the common close path (Codex review of
    d80d9d3: a malformed WORKER on an uninspectable tree ran a second round)."""
    import subprocess

    from quadratus import session as session_module

    def lead(call, replay):
        H.write(call, {"README.md": "# rewritten\n"})
        if kind == "transport":
            raise subprocess.TimeoutExpired(call.argv, 1)
        if kind == "reply":
            return 'WORKER {"errand":"code"}'
        if kind == "channel":
            return "CONSULT Sol: what should add return?"
        return 'Did it.\nCHANGED: ["README.md"]'

    uninspected = dict(inspected=False, changed=[], changed_lines=0, note="Inspection unavailable")
    monkeypatch.setattr(session_module.Session, "_inspect_partial_edits", lambda self, before: uninspected)
    monkeypatch.setattr(session_module.Session, "_assess_scope", lambda self, spec, task, before: None)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_continue_then_done, lead=lead,
                                                 consultant=lambda call, replay: "The sum."),
                   files=FILES, max_tasks=3, record_complete=False)
    assert not replay.result.completed
    assert replay.result.error.startswith("PartialWorkStopped"), replay.result.error
    assert len(replay.of("orchestrator")) == 1, "no second round on an uninspectable tree"
    assert _task(replay, "t1")["closed_as"] == "stopped:PartialWorkStopped"
    assert not replay.artifacts("task-failed")
