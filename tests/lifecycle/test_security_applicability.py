"""Security routing takes the task's contract as a floor (map P3.4,
``security_verification``).

``_run_task`` used to route on the live classification alone. A task whose
contract, fixed at dispatch, says security now keeps the security route
(the deputy seat, the security worker, cross-vendor verification) even if
the live classification drifts; a live security classification is never
routed round it. A disagreement is recorded, so such a run cannot count as
complete. Whole-controller replays; the drift is a synthetic write just
before routing, a controller invariant rather than observed behaviour.
"""

import json

from quadratus.routing import WorkClass
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
from tests.lifecycle.test_workflow_contract import _security_run, _task, _then_done


def _drift(monkeypatch, work_class):
    route = Session._run_task

    def drifted(self, spec):
        if spec.task_id == "t1":
            spec.work_class = work_class
        return route(self, spec)
    monkeypatch.setattr(Session, "_run_task", drifted)


def test_a_security_contract_keeps_the_security_route_when_the_class_drifts(tmp_path, monkeypatch):
    _drift(monkeypatch, WorkClass.GENERAL)
    replay = _security_run(tmp_path, monkeypatch, "Accepted: correct, and it cites the function.",
                           record_complete=False)
    t1 = _task(replay, "t1")
    assert replay.of("verifier"), "the cross-vendor verification still ran"
    assert "verification" in t1["stages"] and t1["edges"]["verification"] is True
    assert "security_verification: contract True, legacy False" in json.dumps(t1)
    assert not replay.result.completed


def test_a_live_security_class_is_never_routed_round(tmp_path, monkeypatch):
    _drift(monkeypatch, WorkClass.SECURITY)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES,
                   max_tasks=3, record_complete=False)
    t1 = _task(replay, "t1")
    assert replay.of("verifier"), "a task now classified as security is verified"
    assert "security_verification: contract False, legacy True" in json.dumps(t1)
    assert not replay.result.completed


# -- same-journey controls -------------------------------------------------------

def test_an_ordinary_security_task_is_unchanged(tmp_path, monkeypatch):
    replay = _security_run(tmp_path, monkeypatch, "Accepted: correct, and it cites the function.")
    t1 = _task(replay, "t1")
    assert t1["contract"]["required"]["security_verification"] is True
    assert t1["edges"]["verification"] is True and replay.result.completed


def test_an_ordinary_standard_task_is_unchanged(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3)
    t1 = _task(replay, "t1")
    assert t1["contract"]["required"]["security_verification"] is False
    assert not replay.of("verifier") and "verification" not in t1["stages"]


def test_a_rejected_security_task_still_stops(tmp_path, monkeypatch):
    replay = _security_run(tmp_path, monkeypatch, "BLOCKING: the audit never read the caller.")
    t1 = _task(replay, "t1")
    assert t1["primary"] == "security" and not replay.result.completed
