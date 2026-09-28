"""O-NEXT-10: live readers left after P3.4 (audit reproductions, no engine change).

Each case changes one configuration field after the task's contract was
built at dispatch and shows the task being told, checked or closed from the
live value instead. They are strict xfails: each asserts the behaviour the
contract implies, and fails today. A fix that makes one pass turns it into an
XPASS, which strict mode reports as a failure so the marker is removed with
the fix. The drift is a synthetic write, a controller invariant rather than
observed behaviour, in the style of test_lead_prompt_applicability.py. See
docs/review/o-next-10-live-reader-audit.md.
"""

import dataclasses

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateCommand, GateSuite
from quadratus.outcome import TaskOutcome
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_workflow_contract import _security_run, _task

LEAD = "claude:opus"


def _session(tmp_path, calls, **config):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("def add(a, b):\n    return 0\n")
    (root / "README.md").write_text("# app\n")

    def invoke(key, prompt, allow_writes=False, **kw):
        calls.append(dict(key=key, allow_writes=allow_writes))
        return "Done.\nCHANGED: []"
    return Session("goal", ArtifactStore(tmp_path / "a"), invoke,
                   config=SessionConfig(project=root, **config))


def _dispatch(session, spec):
    outcome = TaskOutcome(spec.task_id, "implementation")
    session._outcome = outcome
    session._contract = session._build_contract(spec, outcome)
    session._active_spec = spec
    session._task_before = session._capture_source()
    return session._contract


def _drift(session, **changes):
    session.config = dataclasses.replace(session.config, **changes)


def _spec(scope=None):
    return TaskSpec("t1", "Implement add in app.py.", kind="refactor",
                    scope=scope or TaskScope(permitted_paths=["app.py"], max_lines=40))


# -- A. the write grant (allow_writes) --------------------------------------------------

@pytest.mark.xfail(strict=True, reason="O-NEXT-10 A: the write grant is read live at each call")
def test_a_read_only_contract_does_not_gain_writes_when_the_grant_drifts_on(tmp_path):
    calls = []
    session = _session(tmp_path, calls, allow_writes=False)
    contract = _dispatch(session, _spec())
    assert dict(contract.authority)["write_grant"] == "none"
    _drift(session, allow_writes=True)
    session._edit(LEAD, "Revise.", role="revision")
    prompt = session._lead_prompt(_spec())
    print("A invoke allow_writes:", [c["allow_writes"] for c in calls])
    print("A lead prompt says:", "edit method" if "edit method in your role" in prompt else "no edit grant")
    print("A revision delivery:", session._revision_delivery()[:60])
    assert [c["allow_writes"] for c in calls] == [False]
    assert "This run has no edit grant" in prompt


@pytest.mark.xfail(strict=True, reason="O-NEXT-10 A: the write grant is read live at each call")
def test_an_editing_contract_is_not_told_read_only_when_the_grant_drifts_off(tmp_path):
    calls = []
    session = _session(tmp_path, calls, allow_writes=True)
    contract = _dispatch(session, _spec())
    assert dict(contract.authority)["write_grant"] == "operator"
    _drift(session, allow_writes=False)
    session._edit(LEAD, "Revise.", role="revision")
    prompt = session._lead_prompt(_spec())
    print("A' invoke allow_writes:", [c["allow_writes"] for c in calls])
    print("A' lead prompt says:", "edit method" if "edit method in your role" in prompt else "no edit grant")
    assert [c["allow_writes"] for c in calls] == [True]
    assert "edit method in your role" in prompt


# -- B. the cheap gate subset reads the live gate, not the bound one --------------------

def _cheap_gate_appears(monkeypatch):
    """After t1's draft, replace the live gate with a suite whose one cheap
    command fails. The contract's gate (bound at dispatch) has no cheap part."""
    assess = Session._assess_scope

    def drifted(self, spec, *args, **kw):
        report = assess(self, spec, *args, **kw)
        if spec.task_id == "t1" and not getattr(self, "_o10_drifted", False):
            self._o10_drifted = True
            self.config.integration_gate = GateSuite(
                [GateCommand("lint", ("false",), cheap=True)], cwd=self.project)
        return report
    monkeypatch.setattr(Session, "_assess_scope", drifted)


@pytest.mark.xfail(strict=True, reason="O-NEXT-10 B: _run_task's cheap subset reads the live gate")
def test_a_cheap_gate_added_after_dispatch_does_not_close_the_task(tmp_path, monkeypatch):
    _cheap_gate_appears(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, record_complete=False)
    t1 = _task(replay, "t1")
    reviewed = [c.role for c in replay.calls if c.task == "t1" and c.role.startswith("collaborator")]
    print("B contract required_checks:", t1["contract"]["required_checks"])
    print("B t1 collaborator calls:", reviewed)
    print("B t1 stages:", t1["stages"])
    print("B gate results:", H.gate_results(replay))
    assert reviewed, "the task reached review, as its contract's stages say"


def test_control_without_drift_t1_reaches_review(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, record_complete=False)
    reviewed = [c.role for c in replay.calls if c.task == "t1" and c.role.startswith("collaborator")]
    print("B control t1 collaborator calls:", reviewed)
    assert reviewed


# -- C. audit debt versus stop reads the live requirements ledger ----------------------

@pytest.mark.xfail(strict=True, reason="O-NEXT-10 C: _audit_debt_applies reads live requirements_ledger")
def test_audit_debt_applicability_is_fixed_at_dispatch(tmp_path):
    session = _session(tmp_path, [], allow_writes=True, requirements_ledger=True)
    spec = _spec(TaskScope(permitted_paths=["app.py"], review_only=True))
    _dispatch(session, spec)
    session._current_covers = ["R1"]
    overflow = [dict(kind="product.overflow")]
    at_dispatch = session._audit_debt_applies(spec, False, overflow)
    _drift(session, requirements_ledger=False)
    after = session._audit_debt_applies(spec, False, overflow)
    print("C audit debt at dispatch:", at_dispatch, "after drift:", after)
    assert after == at_dispatch


# -- D. the security verification protocol reads live security_verdict_json -----------

def _json_verdicts_after_the_gate(monkeypatch):
    gate = Session._run_integration_gate

    def drifted(self, lead, spec, task, *args, **kw):
        out = gate(self, lead, spec, task, *args, **kw)
        if spec.task_id == "t1":
            self.config = dataclasses.replace(self.config, security_verdict_json=True)
        return out
    monkeypatch.setattr(Session, "_run_integration_gate", drifted)


@pytest.mark.xfail(strict=True, reason="O-NEXT-10 D: the verdict protocol is chosen live after the gate")
def test_the_security_verdict_protocol_is_fixed_at_dispatch(tmp_path, monkeypatch):
    _json_verdicts_after_the_gate(monkeypatch)
    replay = _security_run(tmp_path, monkeypatch, "Accepted: correct, and it cites the function.",
                           record_complete=False)
    t1 = _task(replay, "t1")
    asked_json = ["Return one JSON object only" in c.prompt for c in replay.of("verifier")]
    print("D verifier asked for JSON:", asked_json)
    print("D verification edge:", t1["edges"].get("verification"), "unsatisfied:", t1["unsatisfied"])
    print("D completed:", replay.result.completed)
    assert not any(asked_json)
    assert t1["edges"]["verification"] is True


# -- E. operator path limits (default_scope) are read live at every scope check -------

@pytest.mark.xfail(strict=True, reason="O-NEXT-10 E: _measure_scope reads live default_scope")
def test_operator_limits_are_fixed_at_dispatch(tmp_path):
    limits = TaskScope(permitted_paths=["app.py", "README.md"], max_lines=200)
    session = _session(tmp_path, [], allow_writes=True, default_scope=limits)
    spec = _spec()
    contract = _dispatch(session, spec)
    (session.project / "app.py").write_text("def add(a, b):\n    return a + b\n")
    at_dispatch = session._measure_scope(spec, session._task_before)
    _drift(session, default_scope=TaskScope(permitted_paths=["README.md"], max_lines=200))
    after = session._measure_scope(spec, session._task_before)
    print("E contract authority:", dict(contract.authority))
    print("E out_of_scope at dispatch:", at_dispatch.out_of_scope, "after drift:", after.out_of_scope)
    assert after.out_of_scope == at_dispatch.out_of_scope
