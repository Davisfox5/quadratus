"""Phase 2: the task contract, readiness probes and delivery edges (#25).

Whole-controller replays: only the vendor CLI launch is faked. Readiness
probes are real subprocesses. Parity and completeness (a contract on every
task, no contract/legacy mismatch) are asserted on every replay by
harness.run. Scripted replies prove routing, not model judgement.
"""

import dataclasses
import json
import sys

import pytest

from quadratus.config import Settings
from quadratus.contract import Required, TaskContract
from quadratus.readiness import MAX_PROBES, probes_from
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import (
    DECL_T1,
    FILES,
    FIXED,
    Script,
    _continuing,
    _design_files,
    _design_script,
    _finish,
)

CAPPED = Settings(backend="cli", lead_max_turns=14)


def _task(replay, task_id):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == task_id)


def _then_done(first):
    def orchestrator(call, replay):
        return first if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


def _probe(pid, code, timeout=10):
    return {"id": pid, "argv": [sys.executable, "-c", code], "timeout": timeout}


# -- J7 readiness probes --------------------------------------------------------------

def test_j7_a_failing_probe_stops_the_run_before_any_model_call(tmp_path, monkeypatch):
    probes = [_probe("home", "import sys\nsys.stderr.write('uv_os_homedir ENOENT')\nsys.exit(3)")]
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, readiness=probes)
    assert replay.calls == [], "no model call, no application edit"
    assert replay.result.error.startswith("CapabilityProbeFailed: readiness probe home failed (exit 3)")
    assert "uv_os_homedir ENOENT" in replay.result.error
    (receipt,) = replay.workflow["run"]["readiness"]
    assert receipt["passed"] is False and receipt["returncode"] == 3 and receipt["output_artifact"] != "unavailable"
    stop = replay.workflow["run"]["facts"][-1]
    assert stop["kind"] == "operator" and stop["legacy"] == "CapabilityProbeFailed"
    assert (replay.project / "app.py").read_text() == FILES["app.py"]


def test_j7_the_first_failing_probe_ends_probing(tmp_path, monkeypatch):
    probes = [_probe("one", "raise SystemExit(1)"), _probe("two", "print('never')")]
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, readiness=probes)
    assert [r["id"] for r in replay.workflow["run"]["readiness"]] == ["one"]


def test_passing_probes_prove_readiness_only(tmp_path, monkeypatch):
    probes = [_probe("python", "import json\nprint('ready')")]
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3,
                   readiness=probes)
    assert replay.result.completed
    t1 = _task(replay, "t1")
    assert t1["contract"]["capabilities"] == ["python"]
    assert [c["receipts"] for c in H.result_json(replay)["checks"]] == [[]], "a probe is not a check"
    assert replay.workflow["run"]["readiness"][0]["output"].strip() == "ready"


def test_a_probe_that_writes_project_source_is_an_integrity_stop(tmp_path, monkeypatch):
    probes = [_probe("sneaky", "open('app.py', 'w').write('def add(a, b):\\n    return a + b\\n')")]
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, readiness=probes)
    assert replay.calls == [] and replay.result.error.startswith("PartialWorkStopped: A readiness probe")


def test_a_probe_that_runs_too_long_is_killed_and_fails(tmp_path, monkeypatch):
    probes = [_probe("slow", "import time\ntime.sleep(30)", timeout=1)]
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, readiness=probes)
    (receipt,) = replay.workflow["run"]["readiness"]
    assert receipt["reason"] == "timed out after 1s" and receipt["seconds"] < 10
    assert replay.calls == []


@pytest.mark.parametrize("bad", [
    [],
    [{"id": "x", "argv": ["sh", "-c", "echo $HOME"]}],
    [{"id": "x", "argv": ["true"], "timeout": 0}],
    [{"id": "x", "argv": ["true"], "timeout": 121}],
    [{"id": "X", "argv": ["true"]}],
    [{"id": "x", "argv": ["true"]}, {"id": "x", "argv": ["true"]}],
    [{"id": "x", "argv": ["true"], "retries": 2}],
    [{"id": f"p{i}", "argv": ["true"]} for i in range(MAX_PROBES + 1)],
])
def test_a_malformed_declaration_is_refused_before_the_run(tmp_path, bad):
    with pytest.raises(ValueError):
        probes_from(bad, tmp_path)


# -- the contract --------------------------------------------------------------------

def test_the_contract_is_frozen_and_references_grants(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3)
    contract = _task(replay, "t1")["contract"]
    assert contract["intent"] == "implementation" and contract["authority"] == {
        "write_grant": "operator", "edits": "scoped:40"}
    required = dict(contract["required"])
    # The repository policy's outer limit always applies over a declared scope.
    assert required.pop("operator_limits").startswith("sha256:")
    assert type(required.pop("requirements_ledger")) is bool
    assert required == dict(checks=True, design_evidence="none", design_review=False,
                            security_verification=False, settlement=False,
                            design_collaboration_applicable=False, design_instruction="none",
                            security_verdict="none")
    assert contract["allowed_next"] == ["draft", "review", "checks", "closeout"]
    assert contract["scope"]["permitted_paths"] == ["app.py", "tests/test_app.py"]
    assert len(contract["digest"]) == 64
    frozen = TaskContract(task_id="t1", intent="audit", required=Required())
    with pytest.raises(dataclasses.FrozenInstanceError):
        frozen.intent = "repair"
    with pytest.raises(ValueError):
        TaskContract(task_id="t1", intent="anything")


def test_j22_j34_a_continuation_gets_a_new_contract_inheriting_state_and_debt(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
        return _finish(call, replay)
    continuing = _continuing(DECL_T1)

    def orchestrator(call, replay):
        return continuing(call, replay) if len(replay.of("orchestrator")) <= 2 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES,
                   max_tasks=4, settings=CAPPED)
    t1, t2 = _task(replay, "t1")["contract"], _task(replay, "t2")["contract"]
    assert t2["continues"] == "t1" and t2["digest"] != t1["digest"], "a new contract, not an edited one"
    assert t2["inherits"] == dict(task="t1", found=True, intended_state=None,
                                  open_at_close={"findings": [], "requirements": []},
                                  changed=["app.py"], closed_as="turn_limited")
    assert t1["owner"] == _task(replay, "t1")["lead"] and t2["owner"] == _task(replay, "t2")["lead"]
    assert replay.result.completed


# -- J10 delivery and reviewer response ---------------------------------------------------

def test_an_approved_design_task_records_what_was_delivered_and_the_response(tmp_path, monkeypatch):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    replay = H.run(tmp_path, monkeypatch, _design_script("Renders refreshed.\nCHANGED: []"), files=_design_files())
    t1 = _task(replay, "t1")
    assert t1["contract"]["required"]["design_evidence"] == "self" and t1["contract"]["required"]["design_review"]
    assert t1["edges"]["delivered"] is True and t1["edges"]["reviewer"] is True
    reviewer = replay.of("design-review")[0]
    assert t1["delivery"]["reviewer"].endswith(reviewer.model), "the reviewer actually called"
    assert sorted(t1["delivery"]["files"]) == [".quadratus/design-evidence/t1/desktop/page.png",
                                               ".quadratus/design-evidence/t1/mobile/page.png",
                                               ".quadratus/design-evidence/t1/summary.json"]
    assert "reviewer" not in t1["unsatisfied"]


def test_a_blocking_response_leaves_the_reviewer_edge_unsatisfied(tmp_path, monkeypatch):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    replay = H.run(tmp_path, monkeypatch, _design_script("Renders refreshed.\nCHANGED: []",
                                                         review="BLOCKING: the button is clipped"),
                   files=_design_files())
    t1 = _task(replay, "t1")
    assert t1["edges"]["delivered"] is True and t1["edges"]["reviewer"] is False
    assert "reviewer" in t1["unsatisfied"] and not replay.result.completed


def test_a_design_task_whose_evidence_never_verified_has_no_delivery(tmp_path, monkeypatch):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    script = _design_script("unused")
    script.overrides["design-fix"] = lambda call, replay: "Looked.\nCHANGED: []"
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files())
    t1 = _task(replay, "t1")
    assert t1["delivery"] is None and not replay.of("design-review")
    assert set(t1["unsatisfied"]) >= {"evidence", "delivered", "reviewer"}


def test_the_contract_travels_with_a_passing_check_and_no_design_requirement(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=3)
    t1 = _task(replay, "t1")
    assert t1["mismatches"] == [] and t1["unsatisfied"] == []
    assert json.dumps(t1["contract"])  # serialisable as recorded


# -- security verification is a recorded mandatory edge (Codex, 5857677398) ------------

SECURITY = "KIND: security standard\nSCOPE: " + json.dumps(dict(
    permitted_paths=["app.py"], intended_result="add is audited", acceptance=["add has no injection"],
    max_lines=10, edits="none")) + "\nAudit add in app.py."


def _security(verifier_reply, *, structured=False):
    def lead(call, replay):
        return "add only adds; no input reaches a shell.\nCHANGED: []"
    return Script(orchestrator=_then_done(SECURITY), lead=lead,
                  verifier=verifier_reply if callable(verifier_reply) else (lambda call, replay: verifier_reply),
                  **{"security-fix": lambda call, replay: "Re-read the caller; no change.\nCHANGED: []"})


def _security_run(tmp_path, monkeypatch, reply, **kw):
    return H.run(tmp_path, monkeypatch, _security(reply), files=FILES_OK_SECURITY, max_tasks=3, **kw)


FILES_OK_SECURITY = {**FILES, "app.py": FIXED}


def test_a_security_contract_expects_verification_and_an_accept_satisfies_it(tmp_path, monkeypatch):
    replay = _security_run(tmp_path, monkeypatch, "Accepted: correct, and it cites the function.")
    t1 = _task(replay, "t1")
    assert t1["contract"]["required"]["security_verification"] is True
    assert t1["contract"]["allowed_next"] == ["draft", "checks", "verification", "closeout"]
    assert "verification" in t1["stages"] and t1["edges"]["verification"] is True
    assert t1["unsatisfied"] == [] and replay.result.completed


def test_a_blocking_security_verdict_leaves_verification_unsatisfied(tmp_path, monkeypatch):
    replay = _security_run(tmp_path, monkeypatch, "BLOCKING: the audit never read the caller.")
    t1 = _task(replay, "t1")
    assert t1["edges"]["verification"] is False and "verification" in t1["unsatisfied"]
    assert t1["primary"] == "security" and not replay.result.completed


def test_verification_interrupted_before_a_verdict_is_attempted_but_not_satisfied(tmp_path, monkeypatch):
    replay = _security_run(tmp_path, monkeypatch, lambda call, replay: H.claude_refusal("cyber"))
    t1 = _task(replay, "t1")
    assert "verification" in t1["stages"] and "verification" not in t1["edges"]
    assert "verification" in t1["unsatisfied"] and t1["closed_as"].startswith("stopped:")


def _structured(verdict, snapshot_from):
    def reply(call, replay):
        import re
        snapshot = re.search(r"Snapshot hash: ([0-9a-f]{64})", call.prompt).group(1)
        criteria = json.loads(re.search(r"Acceptance criteria: (\[.*\])", call.prompt).group(1))
        status = "passed" if verdict == "accept" else "failed"
        return H.ENVELOPE[call.vendor](json.dumps(dict(schema_version=1, verdict=verdict, snapshot_hash=snapshot,
                               acceptance_results=[dict(criterion=c, status=status, evidence="read app.py")
                                                   for c in criteria],
                               blocking_findings=[] if verdict == "accept" else ["the caller was not read"],
                               limitations=[])))
    return reply


@pytest.mark.parametrize("verdict, satisfied", [("accept", True), ("reject", False)])
def test_structured_security_verdicts_set_the_same_edge(tmp_path, monkeypatch, verdict, satisfied):
    from quadratus import project_run
    real = project_run.SessionConfig
    monkeypatch.setattr(project_run, "SessionConfig", lambda **kw: real(**{**kw, "security_verdict_json": True}))
    replay = _security_run(tmp_path, monkeypatch, _structured(verdict, None))
    t1 = _task(replay, "t1")
    assert t1["edges"]["verification"] is satisfied
    assert ("verification" in t1["unsatisfied"]) is (not satisfied)



# -- J34 debt survives a cap or an interruption (Codex, 5857789275) ---------------------

def _ledger_run(tmp_path, monkeypatch, plan, leads, **kw):
    from quadratus.session import Session
    from tests.lifecycle.test_audit_findings import _run as audit_run
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    return audit_run(tmp_path, monkeypatch, plan, leads, settings=CAPPED, **kw)


def _capped(call, replay):
    return H.claude_cap("Still reading.", num_turns=14)


@pytest.mark.requirements_ledger
def test_j34_capped_covers_debt_is_snapshotted_and_inherited(tmp_path, monkeypatch):
    from tests.lifecycle.test_audit_findings import REPAIR, REQS
    continuation = REPAIR.replace("Fix the mobile overflow.", "Finish the capped work.\nCONTINUES: t1")
    replay = _ledger_run(tmp_path, monkeypatch, [REQS + REPAIR, continuation],
                         {"t1": _capped, "t2": _capped}, max_tasks=2)
    t1, t2 = _task(replay, "t1"), _task(replay, "t2")
    assert t1["closed_as"] == "turn_limited" and t1["open_at_close"]["requirements"] == ["R1", "R2"]
    inherited = t2["contract"]["inherits"]
    assert inherited["open_at_close"] == t1["open_at_close"], "carried unchanged"
    assert t1["contract"]["owner"] == "claude:opus" == t1["lead"]


@pytest.mark.requirements_ledger
def test_j34_capped_resolves_debt_is_snapshotted_and_inherited(tmp_path, monkeypatch):
    from tests.lifecycle.test_audit_findings import AUDIT, REPAIR, REQS, WIDE, _capture
    repair = REPAIR + "\nRESOLVES: F1"
    continuation = repair.replace("Fix the mobile overflow.", "Finish the repair.\nCONTINUES: t2")
    replay = _ledger_run(tmp_path, monkeypatch, [REQS + AUDIT, repair, continuation],
                         {"t1": _capture(measured=WIDE), "t2": _capped, "t3": _capped}, max_tasks=3)
    t2, t3 = _task(replay, "t2"), _task(replay, "t3")
    assert t2["closed_as"] == "turn_limited" and t2["open_at_close"]["findings"] == ["F1"]
    assert t3["contract"]["inherits"]["open_at_close"]["findings"] == ["F1"]
    assert t3["contract"]["intended_state"]["findings"][0]["finding"] == "F1"


@pytest.mark.requirements_ledger
def test_j34_an_interrupted_task_keeps_its_debt_on_record(tmp_path, monkeypatch):
    from tests.lifecycle.test_audit_findings import REPAIR, REQS
    replay = _ledger_run(tmp_path, monkeypatch, [REQS + REPAIR],
                         {"t1": lambda call, replay: H.claude_refusal("cyber")}, max_tasks=1)
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "stopped:ProviderRefusal"
    assert t1["open_at_close"]["requirements"] == ["R1", "R2"]
    assert t1["contract"]["owner"] == "claude:opus", "bound at selection, before the refused call"


def test_a_task_stopped_before_selection_has_no_contract_and_says_so(tmp_path, monkeypatch):
    from quadratus import session as session_module
    monkeypatch.setattr(session_module.Session, "_pick_lead",
                        lambda self, spec: (_ for _ in ()).throw(session_module.RunStalled("no seat")))
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES)
    t1 = _task(replay, "t1")
    assert t1["contract"] is None and t1["stages"] == ["dispatch"] and t1["closed_as"] == "stopped:RunStalled"
    assert t1["dispatch"] == {"state": "not_dispatched", "reason": "RunStalled: no seat"}
    assert t1["invoked_owner"] == "" and t1["owner_changes"] == []
