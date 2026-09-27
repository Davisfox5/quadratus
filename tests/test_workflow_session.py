"""Typed outcomes on the security and parallel paths (phase 1, Codex review
of 7cbd35f). Session-level: the lifecycle harness does not drive either
path. Parity is computed the way project_run does, and must agree and be
complete. The one route the plan changes here (G7) is asserted in both
directions: prose about findings is clean, a marker as written still stops.
"""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.outcome import parity
from quadratus.session import Session
from tests.test_parallel_tasks import BATCH, Orchestrated
from tests.test_parallel_tasks import _session as parallel_session

SOL = "openai:gpt-5.6-sol"


def _parity(session, error=""):
    history = [f"{s.task_id}:{getattr(s, 'outcome', 'closed')}" for s in session.history]
    return parity(session.run_outcome, session.task_outcomes, open_findings=list(session._open_findings_for(None)),
                  legacy_completed=session.completed and not error, legacy_error=error, history=history)


def _security_session(tmp_path, verdict):
    calls = []

    def invoke(model, prompt, system=None):
        calls.append(prompt)
        if "Name the single next task" in prompt:
            return "KIND: security\naudit the auth flow" if len(calls) == 1 else "DONE"
        if "verifying security work" in prompt:
            return verdict
        if "The task is finished" in prompt:
            return "SUMMARY: audited\nREASONING: read it"
        return "The token check is constant-time."
    return Session("Audit auth", ArtifactStore(tmp_path / "a"), invoke)


def test_an_accepted_security_task_is_clean_and_names_its_worker(tmp_path):
    session = _security_session(tmp_path, "Accepted: the answer is correct and cites the check.")
    session.run(max_tasks=3)
    (outcome,) = session.task_outcomes
    assert outcome.lead == SOL and outcome.closed_as == "closed" and outcome.primary == "clean"
    result = _parity(session, session.stop_reason)
    assert result["agree"] and result["complete"], result


def test_a_rejected_security_task_is_a_security_fact_and_the_run_stops(tmp_path):
    session = _security_session(tmp_path, "BLOCKING: the comparison is not constant-time.")
    session.run(max_tasks=3)
    (outcome,) = session.task_outcomes
    assert outcome.primary == "security" and not session.completed
    result = _parity(session, session.stop_reason)
    assert result["agree"] and result["complete"], result


@pytest.mark.parametrize("verdict", [
    "Accepted; no BLOCKING findings.",
    "Accepted. Two minor notes, neither blocking.",
    "One non-blocking note for the record.",
    "BLOCKING: none",
    "- **Blocking:** n/a",
    "Nothing BLOCKING here; the check is constant-time.",
])
def test_p3_g7_prose_about_findings_is_not_a_security_finding(tmp_path, verdict):
    """Map G7: the substring test read these as findings. Only a marker as
    written counts (the helper reviewed on 90cc5d9)."""
    session = _security_session(tmp_path, verdict)
    session.run(max_tasks=3)
    (outcome,) = session.task_outcomes
    assert outcome.primary == "clean" and outcome.edges["verification"] is True and session.completed
    result = _parity(session, session.stop_reason)
    assert result["agree"] and result["complete"], result


@pytest.mark.parametrize("verdict", [
    "BLOCKING: the comparison is not constant-time.",
    "Review follows.\n\n1. Blocking: the token is logged in plain text.",
    "- **BLOCKING:** tokens are compared with ==.",
    "This defect is BLOCKING.",
    "Mostly fine, but UNRESOLVED: missing evidence for the rate limit.",
    "Accepted; no BLOCKING findings.\nBLOCKING: except the token is logged.",
])
def test_p3_g7_a_marker_as_written_still_stops_the_run(tmp_path, verdict):
    session = _security_session(tmp_path, verdict)
    session.run(max_tasks=3)
    (outcome,) = session.task_outcomes
    assert outcome.primary == "security" and outcome.edges["verification"] is False and not session.completed
    result = _parity(session, session.stop_reason)
    assert result["agree"] and result["complete"], result


def test_a_merged_parallel_batch_records_each_child_with_its_owner(tmp_path):
    session, project = parallel_session(tmp_path, Orchestrated([BATCH, "DONE"]))
    session.run(max_tasks=4)
    children = {o.task_id: o for o in session.task_outcomes}
    assert set(children) == {"t1", "t2"}
    assert all(o.closed_as == "closed" and o.lead and o.primary == "clean" for o in children.values())
    assert session.completed
    result = _parity(session)
    assert result["agree"] and result["complete"], result


def test_an_unmerged_parallel_child_is_integrity_and_its_debt_stays_open(tmp_path):
    session, project = parallel_session(tmp_path, Orchestrated([BATCH, "DONE"], rogue="b.py"))
    session.run(max_tasks=4)
    children = {o.task_id: o for o in session.task_outcomes}
    # Today the child's own scope check stops it inside its copy; the parent
    # then refuses the merge. Both facts are integrity.
    assert children["t2"].closed_as == "stopped:PartialWorkStopped" and children["t2"].primary == "integrity"
    assert [f.stage for f in children["t2"].facts if f.kind == "integrity"][-1] == "merge"
    assert children["t1"].primary == "clean"
    assert not session.completed
    result = _parity(session, session.stop_reason)
    assert result["agree"] and result["complete"], result

