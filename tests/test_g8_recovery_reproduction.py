"""G8 recovery reproduction at 7d294ce (tests only; see
docs/review/g8-recovery-reproduction.md).

Audit 23d6460 (probe E) found that a later verified design check for a task
id clears the legacy ``_design_unverified`` list while an earlier typed
``verified=False`` outcome with the same id still names ``DesignUnverified``.
These cases pin which part of that is reachable through the real loop:

- Same-task recovery (one ``_check_design`` call: fail, design-fix, verified
  recheck) is reachable and clean on both readers. The split never forms,
  because a task has one outcome and its evidence is the final record.
- Two outcomes sharing an id only arise when the task counter reuses the id
  of an unmerged parallel child. The counter does reach that state, but the
  unmerged child's open finding stops the loop in the same iteration, so the
  reused id is never dispatched.

No synthetic injection and no prospective xfail: root (5862969439) ruled out
synthetic latest-id hardening, and no reachable journey justifies a
latest-evidence expectation. Session objects are only observed, never altered.
"""

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import _design_files, _design_script
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _observe(monkeypatch):
    sessions = []
    run = Session.run

    def recording_run(self, *args, **kw):
        sessions.append(self)
        return run(self, *args, **kw)
    monkeypatch.setattr(Session, "run", recording_run)
    return sessions


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches if m.startswith("design debt")]


# -- same-task recovery: reachable through run_project ---------------------------------

def test_a_recovered_design_check_leaves_no_debt_on_either_reader(tmp_path, monkeypatch):
    sessions = _observe(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, _design_script("Renders refreshed.\nCHANGED: []"),
                   files=_design_files())
    session, = sessions
    assert len(replay.of("design-fix")) == 1, "the first check failed and one fix call ran"
    assert [o.task_id for o in session.task_outcomes] == ["t1"], "one outcome for the task id"
    record = session.task_outcomes[0].evidence
    assert record["verified"] is True and record["first_problem"], "the outcome keeps the final record"
    assert session._design_unverified == [] and session._design_debt() == []
    assert not _mismatches(session)
    assert H.ended_at_cap(replay, 1)


def test_control_an_unrecovered_design_check_is_debt_on_both_readers(tmp_path, monkeypatch):
    sessions = _observe(monkeypatch)
    script = _design_script("unused")
    script.overrides["design-fix"] = lambda call, replay: "Looked.\nCHANGED: []"
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files())
    session, = sessions
    assert session.task_outcomes[0].evidence["verified"] is False
    assert [t for t, _ in session._design_debt()] == [t for t, _ in session._design_unverified] == ["t1"]
    assert not _mismatches(session)
    assert replay.result.error.startswith("DesignUnverified: task t1 ")


# -- id reuse after an unmerged parallel child: counter state only ---------------------

FOLLOW_UP = "KIND: backend simple\n" + _block("c.py").split("\n", 1)[1]


def _batch_then_follow_up(tmp_path, rogue):
    script = Orchestrated([BATCH, FOLLOW_UP, "DONE"], lead_delay=0, rogue=rogue)
    session, _ = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    return session, script


def test_an_unmerged_child_stops_the_loop_before_its_id_can_be_reused(tmp_path):
    session, script = _batch_then_follow_up(tmp_path, rogue="b.py")
    assert [o.task_id for o in session.task_outcomes] == ["t1", "t2"]
    assert [s.task_id for s in session.history] == ["t1"], "the unmerged child is not in history"
    # Pinned hazard: the counter's next id is the unmerged child's id ...
    assert f"t{len(session.history) + 1}" == "t2"
    # ... but the loop stopped in the batch's own iteration, so it is never used.
    assert script.plan == [FOLLOW_UP, "DONE"], "the follow-up task was never asked for"
    assert session.stop_reason.startswith("FindingsOpen: ") and not session.completed
    assert [o.task_id for o in session.task_outcomes].count("t2") == 1


def test_an_unmerged_first_child_points_the_counter_at_its_merged_sibling(tmp_path):
    session, script = _batch_then_follow_up(tmp_path, rogue="a.py")
    assert [s.task_id for s in session.history] == ["t2"]
    assert f"t{len(session.history) + 1}" == "t2", "the collision is with the merged sibling"
    assert script.plan == [FOLLOW_UP, "DONE"] and session.stop_reason.startswith("FindingsOpen: ")


def test_control_a_fully_merged_batch_moves_the_counter_past_every_outcome(tmp_path):
    script = Orchestrated([BATCH, "DONE"], lead_delay=0)
    session, _ = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    ids = [o.task_id for o in session.task_outcomes]
    assert ids == ["t1", "t2"] and [s.task_id for s in session.history] == ids
    assert f"t{len(session.history) + 1}" not in ids
    assert session.completed
