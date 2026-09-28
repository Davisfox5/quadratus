"""The security verifier's protocol is fixed at dispatch (map P3.4; O-NEXT-10 D
at 548da3d, Codex ruling 5865344590).

``_run_security_task`` chose prose or JSON verification from live
``security_verdict_json`` after the task's gate. ``security_verification``
fixed that verification happens, not how it is asked and parsed. The contract
now carries ``security_verdict`` ("json" | "prose" | "none"), and the verifier
prompt and parser both follow it, with the live option recorded beside it.
The mandatory verification edge and call bounds are unchanged. Whole
controller; the drift is a synthetic write after the gate, a controller
invariant rather than observed behaviour.
"""

import dataclasses

import pytest

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_workflow_contract import _security_run, _structured, _task

JSON_ASK = "Return one JSON object only"
PROSE_ACCEPT = "Accepted: correct, and it cites the function."
PROSE_BLOCK = "BLOCKING: the audit never read the caller."


def _dispatch_json(monkeypatch):
    from quadratus import project_run
    real = project_run.SessionConfig
    monkeypatch.setattr(project_run, "SessionConfig", lambda **kw: real(**{**kw, "security_verdict_json": True}))


def _drift_after_the_gate(monkeypatch, value):
    gate = Session._run_integration_gate

    def drifted(self, lead, spec, task, *args, **kw):
        out = gate(self, lead, spec, task, *args, **kw)
        if spec.task_id == "t1":
            self.config = dataclasses.replace(self.config, security_verdict_json=value)
        return out
    monkeypatch.setattr(Session, "_run_integration_gate", drifted)


def _asked_json(replay):
    asked = [JSON_ASK in c.prompt for c in replay.of("verifier")]
    assert asked, "the verifier was asked"
    return asked


# -- dispatched prose, live JSON after the gate ----------------------------------------

@pytest.mark.parametrize("reply, satisfied", [(PROSE_ACCEPT, True), (PROSE_BLOCK, False)])
def test_a_prose_task_is_verified_in_prose_when_json_drifts_on(tmp_path, monkeypatch, reply, satisfied):
    _drift_after_the_gate(monkeypatch, True)
    replay = _security_run(tmp_path, monkeypatch, reply, record_complete=False)
    t1 = _task(replay, "t1")
    assert not any(_asked_json(replay)), "asked in the dispatched protocol"
    assert t1["edges"]["verification"] is satisfied
    assert "security_verdict: contract 'prose', legacy 'json'" in t1["mismatches"]
    assert not replay.result.completed


# -- dispatched JSON, live prose after the gate ----------------------------------------

@pytest.mark.parametrize("verdict, satisfied", [("accept", True), ("reject", False)])
def test_a_json_task_is_verified_in_json_when_json_drifts_off(tmp_path, monkeypatch, verdict, satisfied):
    _dispatch_json(monkeypatch)
    _drift_after_the_gate(monkeypatch, False)
    replay = _security_run(tmp_path, monkeypatch, _structured(verdict, None), record_complete=False)
    t1 = _task(replay, "t1")
    assert all(_asked_json(replay)), "asked in the dispatched protocol"
    assert t1["edges"]["verification"] is satisfied
    assert "security_verdict: contract 'json', legacy 'prose'" in t1["mismatches"]
    assert not replay.result.completed


def test_a_refusal_under_drift_leaves_verification_unsatisfied(tmp_path, monkeypatch):
    _drift_after_the_gate(monkeypatch, True)
    replay = _security_run(tmp_path, monkeypatch, lambda call, replay: H.claude_refusal("cyber"),
                           record_complete=False)
    t1 = _task(replay, "t1")
    assert "verification" in t1["stages"] and "verification" not in t1["edges"]
    assert "verification" in t1["unsatisfied"]
    assert not any(_asked_json(replay))


# -- controls: no drift ---------------------------------------------------------------

def test_a_prose_task_without_drift_completes_in_prose(tmp_path, monkeypatch):
    replay = _security_run(tmp_path, monkeypatch, PROSE_ACCEPT)
    t1 = _task(replay, "t1")
    assert not any(_asked_json(replay)) and t1["edges"]["verification"] is True
    assert t1["mismatches"] == [] and replay.result.completed
    assert t1["contract"]["required"].get("security_verdict", "absent") == "prose"


def test_a_json_task_without_drift_completes_in_json(tmp_path, monkeypatch):
    _dispatch_json(monkeypatch)
    replay = _security_run(tmp_path, monkeypatch, _structured("accept", None))
    t1 = _task(replay, "t1")
    assert all(_asked_json(replay)) and t1["edges"]["verification"] is True
    assert t1["mismatches"] == [] and replay.result.completed
    assert t1["contract"]["required"].get("security_verdict", "absent") == "json"


def test_a_non_security_task_has_no_verdict_protocol(tmp_path, monkeypatch):
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: DECL_T1), files=FILES)
    assert _task(replay, "t1")["contract"]["required"].get("security_verdict", "absent") == "none"


# -- the record ---------------------------------------------------------------------

BASE = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
            settlement=False, design_collaboration_applicable=False, design_instruction="none")


def _record(required):
    from tests.test_outcome import _closed
    task = _closed()
    task.contract["required"] = required
    return task


def test_an_older_record_without_the_protocol_says_so():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE))) == [
        "t1.contract.required.security_verdict (absent: recorded before this field existed)"]


def test_an_unknown_protocol_is_missing():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE, security_verdict="yaml"))) == ["t1.contract.required.security_verdict"]
