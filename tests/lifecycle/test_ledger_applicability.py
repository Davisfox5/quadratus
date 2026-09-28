"""Requirements-ledger semantics are fixed for a dispatched task (map P3.4;
O-NEXT-10 C at 548da3d, Codex ruling 5865627034).

Whether a review-only design check's measured overflow becomes audit debt
or today's stop read live ``requirements_ledger``. The contract now carries
the ledger as dispatched, and debt applies only when that AND the live
setting are on: a live enable adds no debt route, a live disable is not
ignored, a disagreement is recorded and takes today's stop, and nothing
already in the ledger is erased. Whole-controller audit journeys on a real
overflow; the drift is a synthetic write just before the design check, a
controller invariant rather than observed behaviour.
"""

import dataclasses

import pytest

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import AUDIT, REQS, WIDE, _capture, _run

pytestmark = pytest.mark.requirements_ledger


def _ledger_at_dispatch(monkeypatch, value):
    run_task = Session.run_task

    def dispatched(self, spec):
        if spec.task_id == "t1":
            self.config = dataclasses.replace(self.config, requirements_ledger=value)
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", dispatched)


def _ledger_before_the_check(monkeypatch, value):
    check = Session._check_design

    def drifted(self, *args, **kw):
        self.config = dataclasses.replace(self.config, requirements_ledger=value)
        return check(self, *args, **kw)
    monkeypatch.setattr(Session, "_check_design", drifted)


def _t1(replay):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")


def _status(replay):
    return H.result_json(replay)["requirements"]["status"]


# -- a live disable is not ignored, and erases nothing ----------------------------------

def test_a_ledger_disabled_before_the_check_takes_todays_stop_and_keeps_the_ledger(tmp_path, monkeypatch):
    _ledger_before_the_check(monkeypatch, False)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)}, record_complete=False)
    assert replay.findings == [], "no debt route under a disabled ledger"
    assert replay.result.error.startswith("DesignUnverified"), replay.result.error
    assert "requirements_ledger: contract True, legacy False" in _t1(replay)["mismatches"]
    listed = H.result_json(replay)["requirements"]["listed"]
    assert sorted(listed) == ["R1", "R2"], "the recorded requirements are kept"
    assert not any(str(v).startswith("MET") for v in _status(replay).values())


# -- a live enable adds no debt route ---------------------------------------------------

def test_a_ledger_enabled_before_the_check_adds_no_debt_route(tmp_path, monkeypatch):
    _ledger_at_dispatch(monkeypatch, False)
    _ledger_before_the_check(monkeypatch, True)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)}, record_complete=False)
    assert replay.findings == [], "no finding under a ledger the task was not dispatched with"
    assert replay.result.error.startswith("DesignUnverified"), replay.result.error
    assert "requirements_ledger: contract False, legacy True" in _t1(replay)["mismatches"]
    assert not replay.result.completed


# -- controls: no drift, on and off -----------------------------------------------------

def test_with_the_ledger_on_throughout_an_overflow_audit_is_debt(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)}, max_tasks=1)
    (f1,) = replay.findings
    assert f1["id"] == "F1" and f1["status"] == "open" and f1["requirements"] == ["R1", "R2"]
    assert _t1(replay)["mismatches"] == []
    assert all(str(v).startswith("NOT MET") for v in _status(replay).values())
    assert _t1(replay)["contract"]["required"].get("requirements_ledger", "absent") is True


def test_with_the_ledger_off_throughout_an_overflow_audit_is_todays_stop(tmp_path, monkeypatch):
    _ledger_at_dispatch(monkeypatch, False)
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)})
    assert replay.findings == [] and replay.result.error.startswith("DesignUnverified")
    assert _t1(replay)["mismatches"] == []
    assert _t1(replay)["contract"]["required"].get("requirements_ledger", "absent") is False


# -- the record ---------------------------------------------------------------------

BASE = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
            settlement=False, design_collaboration_applicable=False, design_instruction="none",
            security_verdict="none", operator_limits="none")


def _record(required):
    from tests.test_outcome import _closed
    task = _closed()
    task.contract["required"] = required
    return task


def test_an_older_record_without_the_ledger_field_says_so():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE))) == [
        "t1.contract.required.requirements_ledger (absent: recorded before this field existed)"]


def test_a_malformed_ledger_field_is_missing():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE, requirements_ledger="on"))) == [
        "t1.contract.required.requirements_ledger"]
