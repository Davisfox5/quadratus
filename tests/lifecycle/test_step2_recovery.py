"""Step 2 (2026-10-13, #35): recovery that changes approach, conflicts that
can be continued, and interrupted jobs that resume from a durable quiescent
checkpoint with their record, lineage and spent allowance.

Whole-controller replays below the vendor CLI (tests.lifecycle.harness),
plus parallel-batch controls on the scripted fork session.
"""

import json
from pathlib import Path

import pytest

from quadratus import checkpoint
from quadratus.checkpoint import CheckpointRefused
from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.run_budget import RunLimits
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2, FILES, FIXED, T1, Script

LIMITS = RunLimits(max_calls=120, max_reported_tokens=6_000_000, wall_seconds=600, max_concurrent_workers=2)


def _plan(*replies):
    """Orchestrator replies in order across episodes, then DONE."""
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return replies[n - 1] if n <= len(replies) else "DONE"
    return orchestrator


_NEXT_TASK = Session.next_task


def _interrupt_after(monkeypatch, tasks: int):
    """A KeyboardInterrupt when the planner is asked for the task after
    ``tasks`` slots: the quiescent checkpoint was written just before, and no
    call is in flight."""
    def next_task(self, *args, **kwargs):
        if getattr(self, "_slots_used", len(self.history)) == tasks and not Session._interrupted_once:
            Session._interrupted_once = True
            raise KeyboardInterrupt
        return _NEXT_TASK(self, *args, **kwargs)
    monkeypatch.setattr(Session, "next_task", next_task)
    monkeypatch.setattr(Session, "_interrupted_once", False, raising=False)


def _episode(replay, *, resume_from, goal="Build the feature.", max_tasks=2, limits=LIMITS):
    settings = Settings(backend="cli")
    settings.backend_overrides = {}
    settings.openai_api_key = settings.anthropic_api_key = settings.xai_api_key = None
    return run_project(goal, replay.project, settings, allow_writes=True, check=H.GATE, max_tasks=max_tasks,
                       run_limits=limits, resume_from=resume_from)


def _interrupted(tmp_path, monkeypatch, script=None, max_tasks=2):
    _interrupt_after(monkeypatch, 1)
    replay = H.run(tmp_path, monkeypatch, script or Script(orchestrator=_plan(DECL_T1, DECL_T2)),
                   files=FILES, max_tasks=max_tasks, record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error
    return replay, Path(replay.result.run_dir)


def _budget(run_dir):
    return json.loads((Path(run_dir) / "budget.json").read_text())


# -- interruption recovery, accepted-work preservation, budget carryover ----------

def test_an_interrupted_job_resumes_with_its_record_lineage_and_spent_allowance(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    spent = _budget(first)
    assert spent["in_flight"] == 0 and spent["reserved_attempts"] > 0
    body = checkpoint.load(first)
    assert body["resumable"] and not body["sealed"] and body["state"]["slots_used"] == 1
    assert (replay.project / "app.py").read_text() == FIXED, "t1's accepted work is in the project"

    calls_before = len(replay.calls)
    second = Path(_episode(replay, resume_from=first.name).run_dir)
    lineage = json.loads((second / "lineage.json").read_text())
    assert lineage == dict(root=first.name, parent=first.name, episode=2)
    assert json.loads((first / "resumed-by.json").read_text())["child"] == second.name
    # The planner sees t1's record, and the job's next task is t2: one slot left.
    planner = [c for c in replay.calls[calls_before:] if c.role.startswith("orchestrator")]
    assert "### Task t1" in planner[0].prompt
    result = json.loads((second / "result.json").read_text())
    assert result["tasks"] == 2 and [t["task_id"] for t in result["workflow"]["tasks"]][:1] == ["t1"]
    assert result["lineage"]["episode"] == 2
    # The allowance is the job's: the new episode's totals include the old.
    budget = _budget(second)
    assert budget["carried_from_earlier_episodes"]["calls"] == spent["reserved_attempts"]
    assert budget["reserved_attempts"] == spent["reserved_attempts"] + (len(replay.calls) - calls_before)
    assert budget["reported_tokens"] >= spent["reported_tokens"]
    assert (replay.project / "app.py").read_text() == FIXED


def test_the_resumed_episode_keeps_the_jobs_run_start_test_baseline(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    body = checkpoint.load(first)
    originals = checkpoint._decode(body["state"]["_original_tests"])
    assert originals == {"tests/test_app.py": FILES["tests/test_app.py"].encode()}


# -- stale, in-flight, tampered, sealed and forked resumes fail closed --------------

def test_a_changed_source_refuses_the_resume(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    (replay.project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(CheckpointRefused, match="source"):
        _episode(replay, resume_from=first.name)
    assert not (first / "resumed-by.json").exists()


@pytest.mark.parametrize("change", ["goal", "limits"])
def test_a_different_goal_or_allowance_refuses_the_resume(tmp_path, monkeypatch, change):
    replay, first = _interrupted(tmp_path, monkeypatch)
    kwargs = (dict(goal="Build something else.") if change == "goal"
              else dict(limits=RunLimits(max_calls=999, max_reported_tokens=6_000_000, wall_seconds=600,
                                         max_concurrent_workers=2)))
    with pytest.raises(CheckpointRefused, match=change):
        _episode(replay, resume_from=first.name, **kwargs)


def test_a_call_in_flight_when_the_run_ended_refuses_the_resume(tmp_path, monkeypatch):
    def lead(call, replay):
        raise KeyboardInterrupt   # mid-call: reserved, never finished
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1), lead=lead), files=FILES,
                   max_tasks=2, record_complete=False)
    first = Path(replay.result.run_dir)
    # The transport accounts the interrupted attempt with no usage: unknown.
    assert _budget(first)["unknown_usage_attempts"] == 1
    with pytest.raises(CheckpointRefused, match="unknown_usage"):
        _episode(replay, resume_from=first.name)


def test_an_altered_checkpoint_refuses_the_resume(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    data = json.loads((first / "checkpoint.json").read_text())
    data["body"]["consumed"]["calls"] = 0
    (first / "checkpoint.json").write_text(json.dumps(data))
    with pytest.raises(CheckpointRefused, match="digest"):
        _episode(replay, resume_from=first.name)


def test_a_finished_run_is_sealed_and_never_resumed(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1)), files=FILES, max_tasks=1)
    first = Path(replay.result.run_dir)
    assert checkpoint.load(first)["sealed"]
    with pytest.raises(CheckpointRefused, match="sealed"):
        _episode(replay, resume_from=first.name)


def test_a_parent_is_resumed_once(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch, max_tasks=3)
    _interrupt_after(monkeypatch, 2)
    second = Path(_episode(replay, resume_from=first.name, max_tasks=3).run_dir)
    with pytest.raises(CheckpointRefused, match="already resumed"):
        _episode(replay, resume_from=first.name, max_tasks=3)
    # The latest episode is the one to resume, and the lineage continues.
    third = Path(_episode(replay, resume_from=second.name, max_tasks=3).run_dir)
    assert json.loads((third / "lineage.json").read_text()) == dict(root=first.name, parent=second.name,
                                                                     episode=3)


def test_a_resume_from_outside_the_state_folder_is_refused(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    with pytest.raises(CheckpointRefused, match="not a run of this project state"):
        _episode(replay, resume_from=str(tmp_path))


@pytest.mark.parametrize("field,value,match", [("in_flight", 1, "in flight"),   # a killed process
                                                ("unknown_usage_attempts", 1, "unknown_usage"),
                                                ("reserved_attempts", 0, "fewer calls")])
def test_a_budget_record_with_unknown_or_missing_usage_refuses_the_resume(tmp_path, monkeypatch, field, value,
                                                                          match):
    replay, first = _interrupted(tmp_path, monkeypatch)
    budget = _budget(first)
    budget[field] = value
    (first / "budget.json").write_text(json.dumps(budget))
    with pytest.raises(CheckpointRefused, match=match):
        _episode(replay, resume_from=first.name)


# -- unfinished work: held facts, changed approach -----------------------------------

SCOPE_BREACH = "KIND: backend standard\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."


def test_a_failed_task_hands_the_planner_held_facts_and_an_unchanged_retry_is_refused(tmp_path, monkeypatch):
    retry = SCOPE_BREACH + "\nCONTINUES: t1"
    changed = ("KIND: backend standard\nSCOPE: " + json.dumps(dict(T1, permitted_paths=["app.py", "util.py",
                                                                                         "tests/test_app.py"]))
               + "\nImplement add in app.py, with its helper in util.py.\nCONTINUES: t1")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "util.py": "HELPER = 1\n"})
            return 'Implemented add.\nCHANGED: ["app.py", "util.py"]'
        H.write(call, {"app.py": FIXED, "util.py": "HELPER = 1\n"})
        return 'Implemented add with its helper.\nCHANGED: ["app.py", "util.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(SCOPE_BREACH, retry, changed), lead=lead),
                   files=FILES, max_tasks=3, record_complete=False)
    planners = replay.of("orchestrator")
    assert "Harness record" in planners[1].prompt and "unfinished: failed (scope)" in planners[1].prompt
    assert "a continuation must change the approach" in planners[1].prompt
    assert "with the same task text and scope that did not finish" in planners[2].prompt
    leads = [c.task for c in replay.of("lead")]
    assert leads.count("t1") == 1, "the unchanged retry never reached a lead"
    assert len(set(leads)) == 2, "the changed continuation was dispatched"
