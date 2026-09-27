"""Harness capture reads the current task's own checks (phase 3, map G4).

The prerequisite used to be the run's last check, whatever task it came
from. It is now the current task's last attempt of its full required gate,
against the source being captured; a task whose contract requires no checks
does not inherit an earlier task's failure. Whole-controller cases use the
harness-capture fixtures; the decision function is also tested directly for
the cross-task cases a single loop cannot reach (the loop stops after a task
whose last check failed)."""

from types import SimpleNamespace

import pytest

from quadratus.outcome import TaskOutcome
from quadratus.session import Session
from tests.lifecycle.test_audit_findings import REQS
from tests.lifecycle.test_harness_capture import AUDIT, _no_edit, _profile, _run, browser

pytestmark = pytest.mark.requirements_ledger

SOURCE = "a" * 64


def _record(required_checks=True, *attempts):
    outcome = TaskOutcome("t2", "implementation", contract=dict(required=dict(checks=required_checks)))
    outcome.checks = [dict(passed=p, gate=g, source=s) for p, g, s in attempts]
    return outcome


def _ineligible(outcome, source=SOURCE):
    return Session._capture_ineligible(SimpleNamespace(_outcome=outcome, _source_identity=lambda: source))


def test_the_current_tasks_last_full_attempt_decides():
    assert _ineligible(_record(True, (True, "full", SOURCE))) == ""
    assert _ineligible(_record(True, (False, "full", SOURCE), (True, "full", SOURCE))) == "", "repaired"
    assert _ineligible(_record(True, (True, "full", SOURCE), (False, "full", SOURCE))) == \
        "the task's last required check failed"
    assert _ineligible(_record(True)) == "the task's required checks have not run"


def test_a_subset_or_another_gate_never_grants_eligibility():
    assert _ineligible(_record(True, (True, "subset", SOURCE))) == "the task's required checks have not run"
    assert _ineligible(_record(True, (False, "full", SOURCE), (True, "subset", SOURCE))) == \
        "the task's last required check failed"


def test_the_passing_attempt_must_have_run_against_the_source_being_captured():
    stale = _record(True, (True, "full", "b" * 64))
    assert _ineligible(stale) == "the source changed after the task's last passing required check"
    assert _ineligible(_record(True, (True, "full", "unavailable")), source="unavailable") != ""


def test_a_task_without_required_checks_does_not_inherit_another_tasks_history():
    """Neither an earlier failure (G4) nor an earlier pass is read: only the
    current record exists here, whatever the run's last check was."""
    assert _ineligible(_record(False)) == ""
    assert _ineligible(None) == "the task has no dispatch record"


@browser
def test_a_task_whose_required_gate_did_not_run_is_not_captured(tmp_path, monkeypatch):
    """Old rule: no failed entry anywhere, so the harness captured. Now the
    task's own required gate must have run and passed first."""
    profile, _ = _profile(tmp_path)
    started = []
    from quadratus import preview
    real = preview.running
    monkeypatch.setattr(preview, "running", lambda *a, **k: started.append(1) or real(*a, **k))
    monkeypatch.setattr(Session, "_run_integration_gate", lambda self, lead, spec, task, **kw: "")
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _no_edit}, profile=profile)
    assert not started, "no preview for a task whose required gate never ran"
    assert "the harness did not capture because the task's required checks have not run" in replay.result.error
    assert not replay.result.completed


@browser
def test_an_earlier_unrelated_failure_in_the_run_record_does_not_block_this_tasks_capture(tmp_path, monkeypatch):
    """Old rule: the run's last check entry decided. A failed entry appended
    to the legacy list by something other than this task's gate (standing in
    for another task's history) no longer blocks a task whose own required
    gate passed on this source."""
    profile, _ = _profile(tmp_path)
    started = []
    from quadratus import preview
    real = preview.running
    monkeypatch.setattr(preview, "running", lambda *a, **k: started.append(1) or real(*a, **k))
    gate = Session._run_integration_gate

    def gate_then_foreign_failure(self, lead, spec, task, **kw):
        result = gate(self, lead, spec, task, **kw)
        self.checks.append({"passed": False, "command": "another task's gate", "output": "", "cwd": "",
                            "receipts": []})
        return result
    monkeypatch.setattr(Session, "_run_integration_gate", gate_then_foreign_failure)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _no_edit}, profile=profile)
    assert started, "the harness captured on the task's own passing gate"
    assert replay.result.error == "" or "did not capture" not in replay.result.error
