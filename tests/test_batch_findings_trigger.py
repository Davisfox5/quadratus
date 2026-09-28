"""The batch open-findings stop reads the typed record too (map P3.4; Sol
inventory 5863608874).

After a parallel batch the run stopped on the legacy ``open_findings`` list
alone. It now uses the same ``_findings_stop_due`` as the serial stop: the
projected active findings or the legacy list, failing closed, with a
disagreement recorded. The unmerged child is real (it wrote outside its
scope); the legacy loss is injected after the batch, a controller invariant.
"""

from quadratus.session import Session
from tests.test_parallel_tasks import BATCH, Orchestrated
from tests.test_parallel_tasks import _session as parallel_session


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches if m.startswith("open findings:")]


def test_an_unmerged_child_lost_from_the_legacy_list_still_stops_the_batch(tmp_path, monkeypatch):
    batch = Session._run_batch

    def losing(self, specs):
        ran = batch(self, specs)
        self.open_findings.clear()
        return ran
    monkeypatch.setattr(Session, "_run_batch", losing)
    script = Orchestrated([BATCH, "DONE"], rogue="b.py", lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert script.plan == ["DONE"], "the run stopped after the batch; DONE was never read"
    assert session.stop_reason.startswith("FindingsOpen: a task closed with open work"), session.stop_reason
    assert "open findings: typed True, legacy False" in _mismatches(session)
    assert not session.completed and not (project / "b.py").exists()


# -- controls -----------------------------------------------------------------------

def test_an_unmerged_child_stops_the_batch_the_same_way_with_the_legacy_list(tmp_path):
    script = Orchestrated([BATCH, "DONE"], rogue="b.py", lead_delay=0)
    session, _ = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert script.plan == ["DONE"]
    assert session.stop_reason.startswith("FindingsOpen: a task closed with open work"), session.stop_reason
    assert _mismatches(session) == []


def test_a_clean_batch_does_not_stop(tmp_path):
    script = Orchestrated([BATCH, "DONE"], lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert session.completed and (project / "a.py").exists() and (project / "b.py").exists()
    assert _mismatches(session) == []
