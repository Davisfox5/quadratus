"""Merge of the development line into the review batch (2026-11-11),
transport group: logic neither line had alone.

The review batch issues task ids from one counter (``Session._next_task_id``):
an id is spent at dispatch and never issued again. The development line
resumes a job from a checkpoint and used to number tasks from the history.
The counter has to cross the checkpoint, or a resumed episode names a spent
id again.
"""

from quadratus import checkpoint
from quadratus.artifacts import ArtifactStore
from quadratus.memory import TaskSummary
from quadratus.outcome import TaskOutcome
from quadratus.session import Session, SessionConfig


def _session(tmp_path, name):
    return Session("goal", ArtifactStore(tmp_path / name), lambda *a, **k: "", config=SessionConfig())


def _summary(task_id):
    return TaskSummary(task_id=task_id, author="openai:sol", summary="done", reasoning="none")


def test_the_task_id_counter_crosses_a_checkpoint_exactly(tmp_path):
    """A parallel child that was not merged has no history entry and no
    outcome, and its id is still spent: the counter itself is carried, in
    the quiescent checkpoint and in the terminal snapshot."""
    session = _session(tmp_path, "a")
    for task_id in ("t1", "t2", "t3"):
        session._issue_task_id(task_id)
    session.history.append(_summary("t1"))
    state = checkpoint.capture_state(session)
    assert state["ids_issued"] == 3
    assert checkpoint.carried_snapshot(session)["ids_issued"] == 3
    resumed = _session(tmp_path, "b")
    checkpoint.restore_state(resumed, state)
    assert resumed._next_task_id() == "t4", "t2 and t3 were spent though nothing on the record names them"


def test_a_checkpoint_written_before_the_counter_was_carried_resumes_past_its_record(tmp_path):
    """Such a checkpoint holds no counter. The highest id its history or its
    outcomes name stands in, so no task on the record is named again."""
    session = _session(tmp_path, "a")
    session.history.extend([_summary("t1"), _summary("t3")])
    session.task_outcomes.append(TaskOutcome(task_id="t4", intent="a task that failed"))
    state = checkpoint.capture_state(session)
    state.pop("ids_issued")
    resumed = _session(tmp_path, "b")
    checkpoint.restore_state(resumed, state)
    assert resumed._next_task_id() == "t5"


def test_a_record_that_names_a_later_id_than_the_counter_wins(tmp_path):
    """The counter never comes back below an id the record holds."""
    session = _session(tmp_path, "a")
    session._issue_task_id("t1")
    session.history.extend([_summary("t1"), _summary("t6"), _summary("review-of-t9")])
    state = checkpoint.capture_state(session)
    assert state["ids_issued"] == 1
    resumed = _session(tmp_path, "b")
    checkpoint.restore_state(resumed, state)
    assert resumed._next_task_id() == "t7", "only a plain t<n> id counts"
