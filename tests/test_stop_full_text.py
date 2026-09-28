"""A typed stop keeps its whole text (map P3.4, the full-stop-text seam).

``Fact.detail`` is cut at 400 characters (300 for an exception's message),
so a long stop reached the typed record only in part while ``stop_reason``
and ``result.error`` kept it whole. The fact now carries ``full`` when, and
only when, its detail was cut. ``detail`` itself, the stop names, kinds and
routes are unchanged. The long reason is a real ledger refusal naming
many unmet requirements; the long exception is injected, a controller invariant.
"""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.test_requirements_ledger import Script as LedgerScript


def _whole(fact):
    """The typed record's whole text for a fact, however it is carried."""
    fact = fact if isinstance(fact, dict) else vars(fact)
    return fact.get("full") or fact["detail"]


def _unmet(tmp_path, count):
    """``count`` requirements, all covered, R1 met and the rest found not met."""
    ids = [f"R{n}" for n in range(1, count + 1)]
    plan = ("REQUIREMENTS:\n" + "".join(f"{r}: the import page shows control {r}\n" for r in ids)
            + "KIND: backend simple\nBuild the page.\nCOVERS: " + ", ".join(ids))
    audit = "\n".join(f"{r}: MET - app.py" if r == "R1" else f"{r}: NOT MET - no control" for r in ids)
    script = LedgerScript([plan, "DONE", "DONE", "DONE", "DONE"], audits=[audit] * 4)
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script,
                      config=SessionConfig(max_requirement_reopens=2))
    session.run(max_tasks=6)
    return session


@pytest.mark.requirements_ledger
def test_a_long_named_stop_keeps_its_whole_text_on_the_fact(tmp_path):
    session = _unmet(tmp_path, 40)
    stop = session.run_outcome.stop()
    assert len(session.stop_reason) > 400 and stop.legacy == "RequirementsUnmet"
    assert stop.detail == session.stop_reason[:400], "detail is unchanged"
    assert _whole(stop) == session.stop_reason


@pytest.mark.requirements_ledger
def test_a_short_named_stop_has_no_second_copy(tmp_path):
    session = _unmet(tmp_path, 3)
    stop = session.run_outcome.stop()
    assert stop.detail == session.stop_reason and getattr(stop, "full", None) is None


def test_a_long_exception_keeps_its_whole_text_on_the_fact(tmp_path, monkeypatch):
    message = "no such seat record: " + "x" * 500
    monkeypatch.setattr(Session, "_run_tasks", lambda self, max_tasks: (_ for _ in ()).throw(LookupError(message)))
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    with pytest.raises(LookupError):
        session.run(max_tasks=1)
    fact = session.run_outcome.facts[-1]
    assert fact.detail == f"LookupError: {message[:300]}", "detail is unchanged"
    assert _whole(fact) == f"LookupError: {message}"


def test_the_whole_controller_error_and_the_fact_agree(tmp_path, monkeypatch):
    """Through ``project_run``: ``result.error`` is the fact's whole text."""
    message = "the orchestrator seat record is gone: " + "y" * 500

    def broken(self, max_tasks):
        raise LookupError(message)
    monkeypatch.setattr(Session, "_run_tasks", broken)
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, record_complete=False)
    stop = replay.workflow["run"]["facts"][-1]
    assert replay.result.error == f"LookupError: {message}"
    assert _whole(stop) == replay.result.error and stop["detail"] == replay.result.error[:len("LookupError: ") + 300]


def test_a_short_exception_has_no_second_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(Session, "_run_tasks", lambda self, max_tasks: (_ for _ in ()).throw(LookupError("gone")))
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    with pytest.raises(LookupError):
        session.run(max_tasks=1)
    fact = session.run_outcome.facts[-1]
    assert fact.detail == "LookupError: gone" and getattr(fact, "full", None) is None


def test_a_long_task_fact_keeps_its_whole_text():
    from quadratus.outcome import TaskOutcome
    outcome = TaskOutcome("t1", "build")
    fact = outcome.note("product", "z" * 450)
    assert fact.detail == "z" * 400 and _whole(fact) == "z" * 450
    assert getattr(outcome.note("product", "short"), "full", None) is None
