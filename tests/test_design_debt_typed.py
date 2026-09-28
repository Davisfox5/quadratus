"""Design debt is read from the typed evidence record (map P3.4,
``_design_unverified``).

``_name_findings_stop`` named ``DesignUnverified`` from the legacy
``_design_unverified`` list. It now reads the stopping tasks' ``evidence``
records (a design check that ended ``verified=False``), excludes audit
debt (a record carrying ``findings``, which is requirement debt), compares
the legacy list and records a disagreement; either one names the stop.
"""

from quadratus.artifacts import ArtifactStore
from quadratus.outcome import TaskOutcome
from quadratus.session import Session
from tests.test_design_debt_binding import DesignOrchestrated, _design_block
from tests.test_parallel_tasks import _session as parallel_session

BATCH = "PARALLEL\n" + _design_block("templates/a.html") + "\n---\n" + _design_block("templates/b.html")


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches]


def test_a_parallel_childs_design_debt_agrees_in_both(tmp_path):
    session, _ = parallel_session(tmp_path, DesignOrchestrated([BATCH, "DONE"], lead_delay=0))
    session.run(max_tasks=4)
    assert session.stop_reason.startswith("DesignUnverified: task t"), session.stop_reason
    assert {t for t, _ in session._design_debt()} == {t for t, _ in session._design_unverified}
    assert not _mismatches(session)


def test_design_debt_lost_from_the_legacy_list_still_names_the_stop(tmp_path, monkeypatch):
    """Injected: the legacy list loses the batch's debt before the stop is named."""
    name = Session._name_findings_stop

    def losing(self, stopping=None):
        self._design_unverified = []
        return name(self, stopping)
    monkeypatch.setattr(Session, "_name_findings_stop", losing)
    session, _ = parallel_session(tmp_path, DesignOrchestrated([BATCH, "DONE"], lead_delay=0))
    session.run(max_tasks=4)
    assert session.stop_reason.startswith("DesignUnverified: task t"), session.stop_reason
    assert any(m.startswith("design debt: typed True, legacy False") for m in _mismatches(session))


def _session_with(tmp_path, evidence):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    outcome = TaskOutcome("t1", "audit")
    outcome.evidence = evidence
    session.task_outcomes.append(outcome)
    return session


def test_audit_debt_is_not_design_debt(tmp_path):
    session = _session_with(tmp_path, dict(task="t1", verified=False, problem="overflow", findings=["F1"]))
    assert session._design_debt({"t1"}) == []


def test_an_unverified_design_check_is_design_debt(tmp_path):
    session = _session_with(tmp_path, dict(task="t1", verified=False, problem="no mobile render"))
    assert session._design_debt({"t1"}) == [("t1", "no mobile render")]


def test_a_disabled_or_verified_check_is_not_design_debt(tmp_path):
    for verified in (True, None):
        session = _session_with(tmp_path / str(verified), dict(task="t1", verified=verified, problem="x"))
        assert session._design_debt({"t1"}) == []


# -- parity compares the facts, not only the task ids (Codex, 5862205507) ------

def _with_legacy(tmp_path, legacy):
    session = _session_with(tmp_path, dict(task="t1", verified=False, problem="typed cause"))
    session._design_unverified = legacy
    return session


def test_same_task_with_a_different_problem_is_a_recorded_mismatch(tmp_path):
    session = _with_legacy(tmp_path, [("t1", "different legacy cause")])
    assert session._design_debt({"t1"}) == [("t1", "typed cause")], "the typed fact still names the stop"
    assert session.task_outcomes[0].mismatches == [
        "design debt: typed problem 'typed cause', legacy problem 'different legacy cause'"]


def test_same_task_with_the_same_problem_records_nothing(tmp_path):
    session = _with_legacy(tmp_path, [("t1", "typed cause")])
    assert session._design_debt({"t1"}) == [("t1", "typed cause")]
    assert session.task_outcomes[0].mismatches == []


def test_an_earlier_legacy_entry_for_the_task_is_not_a_mismatch(tmp_path):
    """The legacy list can hold a task's first failure and its recheck; the latest is compared."""
    session = _with_legacy(tmp_path, [("t1", "first capture"), ("t1", "typed cause")])
    session._design_debt({"t1"})
    assert session.task_outcomes[0].mismatches == []


def test_a_disagreement_about_which_task_names_the_stop_is_recorded(tmp_path):
    session = _with_legacy(tmp_path, [])
    second = TaskOutcome("t2", "design")
    second.evidence = dict(task="t2", verified=False, problem="t2 cause")
    session.task_outcomes.append(second)
    session._design_unverified = [("t2", "t2 cause"), ("t1", "typed cause")]
    assert session._design_debt()[-1] == ("t2", "t2 cause")
    assert second.mismatches == ["design debt: typed names task t2, legacy names task t1"]
    assert session.task_outcomes[0].mismatches == []
