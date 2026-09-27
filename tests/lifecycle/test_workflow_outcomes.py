"""Typed outcomes describe today's routes (phase 1 of the shared workflow plan).

Journey ids are from docs/workflow-map.md section 7. Every whole-controller
replay in tests/lifecycle already asserts parity between the typed record and
the legacy decisions (harness.run). These cases pin the record itself for
each outcome class: the calls made, the task's facts and primary, the run's
stop fact and its legacy error name, and preserved work.

Phase 1 changes no route, so each case asserts today's behaviour, known-wrong
routes included (``legacy_route``). Where the agreed plan changes a route,
the prospective expectation sits beside it as a strict xfail naming its
phase: it fails today by design and must pass when that phase lands.
Scripted replies prove routing, not model judgement.
"""

import json
import shlex
import sys
from pathlib import Path

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import (
    DECL_T1,
    DECL_T2,
    FILES,
    FILES_OK,
    FIXED,
    Script,
    _continuing,
    _design_files,
    _design_script,
    _finish,
)

CAPPED = Settings(backend="cli", lead_max_turns=14)


def _run(tmp_path, monkeypatch, script, **kw):
    kw.setdefault("files", FILES)
    return H.run(tmp_path, monkeypatch, script, **kw)


def _then_done(first):
    """The orchestrator names ``first`` and then reports DONE."""
    def orchestrator(call, replay):
        return first if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


def _task(replay, task_id):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == task_id)


def _facts(task, *, active=None):
    return [f["kind"] for f in task["facts"]
            if active is None or (f["terminal"] and not f["recovered"]) == active]


def _stop(replay):
    stops = [f for f in replay.workflow["run"]["facts"]
             if f["terminal"] and not f["recovered"] and f["legacy"] is not None]
    return stops[-1] if stops else None


def _looked(call, replay):
    return "Looked; left it.\nCHANGED: []"


# -- J1 clean completion ------------------------------------------------------------

def test_j1_a_clean_task_and_done_is_clean_and_complete(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), max_tasks=3)
    assert replay.result.completed and replay.result.error == ""
    t1 = _task(replay, "t1")
    assert t1["primary"] == "clean" and t1["closed_as"] == "closed" and t1["intent"] == "implementation"
    assert t1["stages"][:3] == ["dispatch", "draft", "review"] and "checks" in t1["stages"]
    assert t1["edges"] == {"draft": True, "review": True, "checks": True, "closeout": True}
    (check,) = t1["checks"]
    assert check["passed"] is True and check["attempt"] == 1 and check["output_artifact"]
    assert check["source"] == t1["source_after"], "the check ran against the source the task left"
    assert t1["lead"] and t1["source_before"] != t1["source_after"], "t1 changed app.py"
    assert t1["partial"]["changed"] == ["app.py"] and t1["partial"]["inspected"] is True
    assert t1["dependency"] == "unchanged" and t1["open_at_close"] == {"findings": [], "requirements": []}
    kept = (replay.result.run_dir / "artifacts" / f"{check['output_artifact']}.txt").read_text()
    assert kept == H.result_json(replay)["checks"][0]["output"] and check["output_artifact_error"] is None
    assert replay.workflow["run"]["done_accepted"] is True and _stop(replay) is None
    assert replay.workflow["parity"]["primary"] == "clean"


# -- J2 / J3 product repair ----------------------------------------------------------

def test_j2_a_gate_fix_that_works_leaves_the_task_clean_with_its_attempt_counted(tmp_path, monkeypatch):
    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Fixed add.\nCHANGED: ["app.py"]'
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1), lead=_looked,
                                                **{"gate-fix": gate_fix}), max_tasks=3)
    t1 = _task(replay, "t1")
    assert len(replay.of("gate-fix")) == 1 and t1["attempts"]["gate_fix"] == 1
    assert [c["passed"] for c in t1["checks"]] == [False, True], "every attempt is kept"
    failed = [f for f in t1["facts"] if f["kind"] == "product"]
    assert len(failed) == 1 and failed[0]["recovered"] is True, "the repaired failure is history"
    assert t1["primary"] == "clean" and replay.result.completed


def test_j3_a_gate_still_failing_is_a_legacy_routed_product_fact_and_a_silent_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1), lead=_looked), max_tasks=3)
    t1 = _task(replay, "t1")
    product = [f for f in t1["facts"] if f["kind"] == "product"]
    assert product and product[0]["legacy_route"] is True and product[0]["stage"] == "checks"
    assert t1["primary"] == "product" and t1["edges"]["checks"] is False
    assert replay.result.error == "" and not replay.result.completed
    assert _stop(replay)["legacy"] == "", "today's stop is silent (map G9)"
    assert "still failing at close" in replay.of("closeout")[0].prompt, "the close-out is told the gate failed"


# -- J15 / J16 / J17 / J18 stops that keep their precedence ------------------------------

def test_j15_a_scope_overrun_is_integrity_with_the_work_kept(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "README.md": "# rewritten\n"})
        return 'Did both.\nCHANGED: ["README.md", "app.py"]'
    replay = _run(tmp_path, monkeypatch, Script(lead=lead))
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "stopped:PartialWorkStopped" and t1["primary"] == "integrity"
    assert _stop(replay)["legacy"] == "PartialWorkStopped" and _stop(replay)["kind"] == "integrity"
    assert (replay.project / "README.md").read_text() == "# rewritten\n"


def test_j16_a_dependency_tree_change_is_integrity(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"node_modules/playwright/index.js": "module.exports = 'real';\n"})
        return "Added a helper.\nCHANGED: []"
    replay = _run(tmp_path, monkeypatch, Script(lead=lead), files=FILES_OK)
    t1 = _task(replay, "t1")
    assert t1["primary"] == "integrity" and t1["closed_as"] == "stopped:DependencyTreeChanged"
    assert _stop(replay)["legacy"] == "DependencyTreeChanged"


def test_j17_a_refused_lead_is_a_refusal_never_rerouted(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(lead=lambda call, replay: H.claude_refusal("cyber")))
    assert len(replay.of("lead")) == 1
    t1 = _task(replay, "t1")
    assert t1["primary"] == "refusal" and _stop(replay)["legacy"] == "ProviderRefusal"


def test_j17_a_refused_closeout_is_a_harness_record_not_a_stop(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1),
                                                closeout=lambda call, replay: H.claude_refusal("cyber")),
                  max_tasks=3)
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "closed" and t1["primary"] == "clean"
    (refused,) = [f for f in t1["facts"] if f["kind"] == "refusal"]
    assert refused["stage"] == "closeout" and refused["terminal"] is False and "cyber" in refused["detail"]
    assert "artifact" in refused["detail"] and replay.result.completed


def test_j18_a_denial_at_the_cap_is_a_denial_above_the_cap(tmp_path, monkeypatch):
    def lead(call, replay):
        capped = json.loads(H.claude_cap("Kept trying to capture."))
        capped["permission_denials"] = [dict(tool_name="Bash", tool_input={
            "command": "PYTHONPATH=/pkg python -m quadratus.design_evidence http://localhost:5000/ t1 ."})]
        return json.dumps(capped)
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    replay = _run(tmp_path, monkeypatch, Script(lead=lead), settings=CAPPED)
    t1 = _task(replay, "t1")
    assert t1["primary"] == "denial" and "cap" not in _facts(t1)
    assert _stop(replay)["legacy"] == "CapabilityUnavailable"


# -- J20 / J22 / J23 caps and continuation ------------------------------------------

def test_j20_a_capped_lead_is_a_cap_that_blocks_completion(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
    replay = _run(tmp_path, monkeypatch, Script(lead=lead), settings=CAPPED)
    t1 = _task(replay, "t1")
    assert t1["closed_as"] == "turn_limited" and t1["primary"] == "cap"
    assert t1["partial"] == {"changed": ["app.py"], "changed_lines": 2}
    assert not replay.result.completed


def test_j22_a_continuation_that_finishes_recovers_the_cap_and_keeps_its_record(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
        return _finish(call, replay)
    continuing = _continuing(DECL_T1)

    def orchestrator(call, replay):
        return continuing(call, replay) if len(replay.of("orchestrator")) <= 2 else "DONE"
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), max_tasks=4,
                  settings=CAPPED)
    t1, t2 = _task(replay, "t1"), _task(replay, "t2")
    cap = [f for f in t1["facts"] if f["kind"] == "cap"]
    assert cap and cap[0]["recovered"] is True, "the record stays, the block lifts"
    assert t2["continues"] == "t1" and t2["primary"] == "clean"
    assert replay.result.completed


def test_j23_two_caps_in_a_row_are_the_breaker(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"README.md": "# app\n\npartial\n"})
            return H.grok_ok("I'll finish the README next", stop="cancelled", num_turns=14)
        if call.vendor == "grok":
            return H.grok_ok("Still reading the tests.", stop="cancelled", num_turns=14)
        return H.claude_cap("Still reading the tests.", num_turns=15)
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_continuing(DECL_T2), lead=lead), max_tasks=3,
                  files=FILES_OK, settings=CAPPED)
    assert _stop(replay)["legacy"] == "TurnLimitBreaker" and _stop(replay)["kind"] == "cap"
    assert [_task(replay, t)["closed_as"] for t in ("t1", "t2")] == ["turn_limited", "turn_limited"]


# -- J24 / J25 / J26 budget, transport, operator ------------------------------------------

def test_j24_budget_exhaustion_is_a_budget_stop_with_the_task_stopped(tmp_path, monkeypatch):
    from quadratus.run_budget import RunLimits
    replay = _run(tmp_path, monkeypatch, Script(), limits=RunLimits(max_calls=2, max_reported_tokens=6_000_000,
                                                                    wall_seconds=600, max_concurrent_workers=2))
    assert replay.result.error.startswith("RunBudgetExceeded")
    stop = _stop(replay)
    assert stop["kind"] == "budget" and stop["legacy"] == "RunBudgetExceeded"


def test_j25_a_recovered_transport_failure_is_history_not_a_block(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.vendor == "grok":
            return H.grok_ok("I'll create it", stop="cancelled", num_turns=3)
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T2), lead=lead), max_tasks=3,
                  files=FILES_OK, settings=CAPPED)
    t1 = _task(replay, "t1")
    transport = [f for f in t1["facts"] if f["kind"] == "transport"]
    assert transport and transport[0]["recovered"] is True and t1["attempts"]["lead_recovery"] == 1
    assert t1["primary"] == "clean" and replay.result.completed
    # The contract keeps the dispatched owner; the switch is history and the
    # invoked owner is the recovery lead.
    change, = t1["owner_changes"]
    assert t1["contract"]["owner"] == t1["dispatch"]["owner"] == change["from"] != change["to"]
    assert t1["invoked_owner"] == change["to"] == t1["lead"] and change["reason"] == "lead_recovery"


def test_j26_an_operator_question_with_no_channel_is_an_operator_handoff(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: "ASK: Which database?"))
    assert replay.workflow["tasks"] == []
    stop = _stop(replay)
    assert stop["kind"] == "operator" and stop["legacy"] == "OperatorInputNeeded"


# -- J8 / J9 / J10 evidence -----------------------------------------------------------

def test_j9_unverified_design_evidence_is_invalid_proof_and_a_named_stop(tmp_path, monkeypatch):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    script = _design_script("unused")
    script.overrides["design-fix"] = lambda call, replay: "Looked.\nCHANGED: []"
    replay = _run(tmp_path, monkeypatch, script, files=_design_files())
    t1 = _task(replay, "t1")
    assert "invalid_proof" in _facts(t1, active=True) and t1["attempts"]["design_fix"] == 1
    assert t1["evidence"]["verified"] is False and t1["edges"]["evidence"] is False
    assert _stop(replay)["legacy"] == "DesignUnverified"


def test_j10_a_blocking_design_review_is_unverified(tmp_path, monkeypatch):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    replay = _run(tmp_path, monkeypatch, _design_script("Renders refreshed.\nCHANGED: []",
                                                       review="BLOCKING: the button is clipped"),
                  files=_design_files())
    t1 = _task(replay, "t1")
    assert "unverified" in _facts(t1, active=True) and t1["evidence"]["verified"] is True
    assert not replay.result.completed


# -- prospective: routes the plan changes (strict xfail until their phase) -----------------

@pytest.mark.xfail(strict=True, reason="P3 (J4/G10): a runner that never reached a test is an operator "
                                       "handoff, never a product repair")
def test_p3_j4_a_runner_crash_before_any_test_gets_no_gate_fix(tmp_path, monkeypatch):
    crash = "python -c \"import sys; sys.stderr.write('uv_os_homedir ENOENT'); sys.exit(1)\""
    replay = _run(tmp_path, monkeypatch, Script(lead=_looked), check=crash, files=FILES_OK)
    assert not replay.of("gate-fix")
    assert _task(replay, "t1")["primary"] == "operator"


@pytest.mark.xfail(strict=True, reason="P3 (G12, amendment 3): a failure a later check in the same task "
                                       "repaired is history, not a block on DONE")
def test_p3_g12_a_check_repaired_later_in_the_task_does_not_block_done(tmp_path, monkeypatch):
    """The gate fails, the gate-fix changes nothing, the design-fix then makes
    the gate pass: the task ends with a passing check, yet today DONE reads
    the earlier failed entry and the run ends incomplete with a blank error."""
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    check = shlex.join([sys.executable, "-c", "import pathlib, sys; "
                        "sys.exit(0 if '10px' in pathlib.Path('static/style.css').read_text() else 1)"])
    script = _design_script("unused")

    def design_fix(call, replay):
        H.write(call, {"static/style.css": "#import { padding: 10px; }\n"})
        H.evidence(Path(call.cwd), "t1", age=H.FRESH)
        return 'Padded the button and recaptured.\nCHANGED: ["static/style.css"]'
    script.overrides["design-fix"] = design_fix
    script.overrides["gate-fix"] = _looked
    first = script.overrides["orchestrator"]
    script.overrides["orchestrator"] = lambda call, replay: (first(call, replay)
                                                            if len(replay.of("orchestrator")) == 1 else "DONE")
    replay = _run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3, check=check)
    assert H.gate_results(replay)[-1] == "PASSED" and len(replay.of("gate-fix")) == 1
    assert replay.result.completed


# -- task-close snapshot of ledger references (Codex review of f09454b) ------------------

@pytest.mark.requirements_ledger
def test_a_clean_audit_closes_with_its_covered_requirements_not_open(tmp_path, monkeypatch):
    from tests.lifecycle.test_audit_findings import AUDIT, REQS, _capture
    from tests.lifecycle.test_audit_findings import _run as audit_run
    replay = audit_run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture()})
    t1 = _task(replay, "t1")
    assert replay.result.completed and t1["covers"] == ["R1", "R2"]
    assert t1["open_at_close"] == {"findings": [], "requirements": []}, "taken after coverage"


@pytest.mark.requirements_ledger
def test_audit_debt_leaves_its_requirements_open_at_close(tmp_path, monkeypatch):
    from tests.lifecycle.test_audit_findings import AUDIT, REQS, WIDE, _capture
    from tests.lifecycle.test_audit_findings import _run as audit_run
    replay = audit_run(tmp_path, monkeypatch, [REQS + AUDIT], {"t1": _capture(measured=WIDE)}, max_tasks=1)
    t1 = _task(replay, "t1")
    assert t1["open_at_close"]["requirements"] == ["R1", "R2"], "owed to the open finding"
    assert not replay.result.completed
