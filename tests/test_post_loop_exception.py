"""An exception after the task loop is a typed stop (map P3.4, the post-loop
exception seam).

``run`` recorded a typed fact for an exception from the task loop, but not
for one raised after it: the end-of-run dependency check or the final
findings record. ``result.error`` named it while the typed record had no
stop, and the session could still read complete. It is now recorded the
same way as the loop's exceptions, the run is not complete, and it is
re-raised unchanged. The exceptions are injected, a controller invariant.
"""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session
from tests.lifecycle.test_completion_guard import _clean


def _fail_at_end(monkeypatch, exc):
    verify = Session._verify_dependencies

    def failing(self, window):
        if window == "at the end of the run":
            raise exc
        return verify(self, window)
    monkeypatch.setattr(Session, "_verify_dependencies", failing)


def test_an_exception_in_the_end_of_run_check_is_a_typed_stop(tmp_path, monkeypatch):
    _fail_at_end(monkeypatch, OSError("the dependency tree could not be read"))
    replay = _clean(tmp_path, monkeypatch, record_complete=False)
    assert not replay.result.completed, "the otherwise clean run is not complete"
    assert replay.result.error == "OSError: the dependency tree could not be read"
    stop = replay.workflow["run"]["facts"][-1]
    assert (stop["kind"], stop["legacy"]) == ("operator", "OSError")
    assert stop["detail"] == replay.result.error


def test_the_session_does_not_read_complete_after_a_post_loop_exception(tmp_path, monkeypatch):
    _fail_at_end(monkeypatch, OSError("unreadable"))
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    with pytest.raises(OSError) as caught:
        session.run(max_tasks=1)
    assert str(caught.value) == "unreadable", "re-raised unchanged"
    assert not session.completed
    stop = session.run_outcome.stop()
    assert stop is not None and (stop.kind, stop.legacy, stop.detail) == ("operator", "OSError", "OSError: unreadable")


def test_an_exception_in_the_final_findings_record_is_a_typed_stop(tmp_path, monkeypatch):
    def broken(self, why):
        raise KeyError("status")
    monkeypatch.setattr(Session, "_annotate_open_findings", broken)
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "KIND: backend simple\nBuild it.")
    with pytest.raises(KeyError):
        session.run(max_tasks=1)
    stop = session.run_outcome.stop()
    assert (stop.kind, stop.legacy) == ("operator", "KeyError")
    kinds = [f.legacy for f in session.run_outcome.facts if f.legacy]
    assert kinds[-2:] == ["GoalUnconfirmedAtCap", "KeyError"], "the named stop stays as the earlier fact"


# -- controls -----------------------------------------------------------------

def test_a_clean_run_is_unchanged(tmp_path, monkeypatch):
    replay = _clean(tmp_path, monkeypatch)
    assert replay.result.completed and not replay.result.error
    assert not [f for f in replay.workflow["run"]["facts"] if f.get("legacy")]


def test_an_exception_in_the_loop_is_recorded_once(tmp_path, monkeypatch):
    def broken(self, max_tasks):
        raise LookupError("gone")
    monkeypatch.setattr(Session, "_run_tasks", broken)
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    with pytest.raises(LookupError):
        session.run(max_tasks=1)
    assert [f.legacy for f in session.run_outcome.facts] == ["LookupError"]
