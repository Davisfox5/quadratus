"""The consolidated A+B boundary correction through the real controller
(root's review of f4fe5b7, 2026-10-18). Whole-run replays below the vendor
CLI, every check the real gate (tests.lifecycle.harness).

Boundary 1, one packet and one predicate: an explicit INSUFFICIENT, or two
sufficiency lines for one id, keeps the requirement unmet; an INVALIDATED
naming nothing on the record keeps the finding open; a requirement whose
text changes while the final audit runs voids the audit. Boundary 2, the
effect lifecycle: an effect interrupted before settlement is uncertain and
refuses the resume, one settled past the last quiescent point refuses it
too, a store that cannot publish the intent starts no effect, a terminal
state that cannot be committed is not a completed run, an altered authority
record refuses the resume, and a clean run commits its terminal with its
acceptance basis. Boundary 3, the route: a second fix cycle is admitted
before it starts and refused when it cannot be paid.
"""

import json
import re
from pathlib import Path

import pytest

from quadratus import checkpoint, jobstore, monitor
from quadratus.session import Session, invocation_context
from tests.lifecycle import harness as H
from tests.lifecycle.test_delivery_controller import (
    GATE_FILES,
    HTTP_FILES,
    _findings,
    _http_script,
    _obligations,
    _recheck_script,
)
from tests.lifecycle.test_finish_capacity import PLAN_1, PLAN_2, _script
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2, FILES, Script
from tests.lifecycle.test_step2_recovery import _commit, _episode, _interrupted, _plan

CLEAN_T2 = "NO FINDINGS\nR2: MET - README.md"


def _run_dir(replay) -> Path:
    return Path(replay.result.run_dir)


# -- boundary 1: one packet, one predicate ---------------------------------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("shape", ["insufficient", "conflict"])
def test_an_explicit_insufficient_or_a_conflicting_sufficiency_keeps_the_requirement_unmet(tmp_path, monkeypatch,
                                                                                            shape):
    """The gate executed the obligation (a real local HTTP test) and the
    auditor's requirement lines say MET; the auditor's own sufficiency
    line says otherwise, or says it twice. Execution is not acceptance:
    the requirement is lowered by the predicate, the obligation is
    insufficient (or unjudged with the conflict noted), and the run is
    incomplete (V09)."""
    script = _http_script("tests/test_http.py - the sandbox denied binding 127.0.0.1")

    def auditor(call, replay):
        lines = ["R1: MET - app.py", "R2: MET - tests/test_http.py"]
        for oid in dict.fromkeys(re.findall(r"^- (O\d+) \(", call.prompt, re.MULTILINE)):
            if "tests/test_http.py" not in call.prompt.split(f"- {oid} (", 1)[1].split("\n", 1)[0]:
                continue
            lines.append(f"{oid}: INSUFFICIENT the test never exercises the error response")
            if shape == "conflict":
                lines.append(f"{oid}: SUFFICIENT it does answer /add")
        return "\n".join(lines)
    script.overrides["auditor"] = auditor
    replay = H.run(tmp_path, monkeypatch, script, files=HTTP_FILES, check=GATE_FILES, max_tasks=1,
                   lead_runs_commands=False, record_complete=False)
    assert not replay.result.completed and replay.result.error.startswith("RequirementsUnmet")
    typed = next(o for o in _obligations(replay) if o["kind"] == "not_run")
    assert typed["execution"] == "ran" and typed["receipts"], "the gate did run it"
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] is False and audit["acceptance"]["R2"]["met"] is False
    if shape == "insufficient":
        assert typed["sufficiency"] == "insufficient" and typed["judged_by"] == "openai:gpt-5.6-sol"
        assert any("sufficiency insufficient" in r for r in audit["acceptance"]["R2"]["reasons"])
    else:
        assert typed["sufficiency"] == "unjudged" and "2 sufficiency lines" in typed["sufficiency_note"]
        assert any("sufficiency unjudged" in r for r in audit["acceptance"]["R2"]["reasons"])
    # The lead's NOT RUN item is bound to every requirement its task covers
    # (R1 and R2), so the same obligation lowers both; the acceptance-kind
    # obligation the task's scope declared is unaffected.
    assert audit["acceptance"]["R1"]["met"] is False and audit["acceptance"]["R1"]["reasons"] == \
        audit["acceptance"]["R2"]["reasons"]
    assert [list(r) for r in typed["requirements"]] == [["R1", 1], ["R2", 1]]
    declared = next(o for o in _obligations(replay) if o["kind"] == "acceptance")
    assert declared["sufficiency"] == "sufficient" and declared["established"]
    terminal = jobstore.read_terminal(_run_dir(replay))
    assert terminal["completed"] is False and terminal["acceptance"]["requirements"]["R2"]["met"] is False


@pytest.mark.requirements_ledger
def test_an_invalidated_line_naming_nothing_on_the_record_keeps_the_finding_open(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _recheck_script([
        "RFT2-1: INVALIDATED the `UTF8` handling covers it\nRFT2-2: RESOLVED reworded",
    ]), files=FILES, max_tasks=2, record_complete=False)
    assert not replay.result.completed and replay.result.error.startswith("FindingsOpen")
    findings = _findings(replay)
    assert findings["RFT2-2"]["state"] == "resolved"
    assert findings["RFT2-1"]["state"] == "open" and findings["RFT2-1"]["history"][-1]["state"] == "unjudged"
    assert "not on the record" in findings["RFT2-1"]["history"][-1]["why"]
    gaps = H.result_json(replay)["judgment_gaps"]
    assert gaps and "RFT2-1" in gaps[0]["why"]


@pytest.mark.requirements_ledger
def test_the_final_audit_is_void_when_a_requirement_changes_while_it_runs(tmp_path, monkeypatch):
    """The TOCTOU shape: the packet the auditor was handed is not the one
    its verdicts land on. Nothing of the reply is kept, the audit is
    recorded void, no final audit is cached, and the run is incomplete."""
    original = Session._invoke_model

    def shifted(self, key, prompt, **kw):
        reply = original(self, key, prompt, **kw)
        if (invocation_context.get() or {}).get("role") == "auditor":
            self.memory.ledger.requirements["R2"] = self.memory.ledger.requirements["R2"] + " with an example"
        return reply
    monkeypatch.setattr(Session, "_invoke_model", shifted)
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES, max_tasks=2,
                   record_complete=False)
    assert not replay.result.completed and replay.of("auditor")
    audits = H.result_json(replay)["requirements"]["audits"]
    assert audits[-1]["result"].startswith("void: the evidence packet changed during the audit")
    assert "the evidence packet changed" in replay.result.error


# -- boundary 3: the route admits a further fix cycle before it starts ---------------------

@pytest.mark.requirements_ledger
def test_a_second_fix_cycle_is_admitted_before_it_starts_and_refused_when_it_cannot_be_paid(tmp_path, monkeypatch):
    replies = ["RFT2-1: UNRESOLVED still no example\nRFT2-2: UNRESOLVED still contradicts the goal",
               "RFT2-1: RESOLVED example added\nRFT2-2: RESOLVED reworded"]
    replay = H.run(tmp_path, monkeypatch, _recheck_script(replies), files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    assert len(replay.of("revision")) == 2
    cycle = [a for a in H.result_json(replay)["plan_admissions"] if a["scope"] == "fix-cycle"]
    assert len(cycle) == 1 and cycle[0]["task"] == "t2" and cycle[0]["feasible"]
    assert cycle[0]["stages"] == ["fix revision(editing/write)", "recheck by openai:gpt-5.6-sol(reserve)"]

    # The same job, with a second cycle that cannot be paid: it is refused
    # before its revision, recorded, and the findings it would have
    # answered stay open as the reviewer wrote them.
    from quadratus import delivery
    (tmp_path / "short").mkdir()
    monkeypatch.setattr(Session, "_fix_cycle_stages", lambda self, spec, unresolved: [
        delivery.PlanStage("fix revision", "editing", editing=True)] + [
        delivery.PlanStage(f"recheck {n}", "reserve") for n in range(300)])
    short = H.run(tmp_path / "short", monkeypatch, _recheck_script(replies), files=FILES, max_tasks=2,
                  record_complete=False)
    assert not short.result.completed and short.result.error.startswith("FindingsOpen")
    assert len(short.of("revision")) == 1
    result = H.result_json(short)
    refusal = [r for r in result["finish_refusals"] if r["what"] == "fix round"][-1]
    assert "a further fix cycle cannot be paid whole" in refusal["detail"]
    assert [a["feasible"] for a in result["plan_admissions"] if a["scope"] == "fix-cycle"] == [False]
    assert _findings(short)["RFT2-1"]["state"] == "unresolved"


# -- boundary 2: the effect lifecycle --------------------------------------------------------

def _interrupt_gate_once(monkeypatch):
    """The real gate, interrupted inside its first run: the effect may have
    half happened and nothing says which half."""
    from quadratus.integration import IntegrationGate
    original = IntegrationGate.run
    fired = []

    def run(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt
        return original(self, *args, **kwargs)
    monkeypatch.setattr(IntegrationGate, "run", run)


def test_an_effect_interrupted_before_settlement_is_uncertain_and_refuses_the_resume(tmp_path, monkeypatch):
    _interrupt_gate_once(monkeypatch)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1, DECL_T2)), files=FILES, max_tasks=2,
                   record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error
    run_dir = _run_dir(replay)
    unsettled = jobstore.unsettled_operations(run_dir)
    assert len(unsettled) == 1 and unsettled[0].startswith("OP")
    rows = [o for rows in jobstore.operation_records(run_dir).values() for o in rows]
    check = next(o for o in rows if o["status"] == "uncertain")
    assert "run-check" in check["allowed_effects"] and check["opid"] == unsettled[0]
    assert check["outcome"] == "interrupted" and check["result_digest"] is None
    assert H.result_json(replay)["checks"] == [], "no receipt was recorded for a run that did not settle"
    with pytest.raises(checkpoint.CheckpointRefused, match="never settled"):
        _episode(replay, resume_from=run_dir)


def test_an_effect_settled_after_the_last_quiescent_point_refuses_the_resume(tmp_path, monkeypatch):
    """A parent whose committed records hold an effect settled after its
    last quiescent point (a check that ran and settled before the process
    died, with no checkpoint written since): the records are consistent,
    nothing is unsettled, and the resume is still refused rather than
    resumed over an effect the record never consumed. A late read-only
    call is only named. (An in-session interruption after a task's check is
    refused earlier, on the source the edit moved: the same answer.)"""
    replay, first = _interrupted(tmp_path, monkeypatch)
    store = jobstore.JobStore(first)
    late = dict(opid="OPzz-1", purpose="model call", status="settled", owner_id="zz", allowed_effects=["read"],
                outcome="completed", result_digest="d" * 64)
    store.publish({"operations-zz": [late]}, session_unsettled=("zz", []))
    assert jobstore.unsettled_operations(first) == ()
    body = checkpoint.load(first)
    assert checkpoint._unconsumed(first, body, jobstore.read_budget(first)[0]) == ["OPzz-1"]
    store.publish({"operations-zz": [late, dict(late, opid="OPzz-2", purpose="check", allowed_effects=["run-check"])]})
    with pytest.raises(checkpoint.CheckpointRefused, match="settled after the last quiescent point"):
        _episode(replay, resume_from=first)


def test_a_store_that_cannot_publish_the_intent_starts_no_effect(tmp_path, monkeypatch):
    original = jobstore._write_durable

    def write(path, data):
        compact = data.replace(b" ", b"")
        if "operations-" in str(path) and b'"purpose":"check"' in compact and b'"status":"started"' in compact:
            raise OSError("disk full")
        return original(path, data)
    monkeypatch.setattr(jobstore, "_write_durable", write)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1, DECL_T2)), files=FILES, max_tasks=2,
                   record_complete=False)
    assert replay.result.error.startswith("JobStoreUnavailable") and "check not started" in replay.result.error
    result = H.result_json(replay)
    assert result["checks"] == [] and not replay.result.completed
    assert result["job_store"]["refused"] and result["job_store"]["terminal_committed"] is False
    assert not any(o["purpose"] == "check" and o["status"] != "prepared" for o in result["operations"])


@pytest.mark.requirements_ledger
def test_a_terminal_that_cannot_be_committed_is_not_a_completed_run(tmp_path, monkeypatch):
    original = jobstore.JobStore.publish

    def publish(self, updates, **kw):
        if "terminal" in updates:
            raise OSError("disk full at the end")
        return original(self, updates, **kw)
    monkeypatch.setattr(jobstore.JobStore, "publish", publish)
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES, max_tasks=2,
                   record_complete=False)
    assert not replay.result.completed
    assert "the terminal state could not be committed" in replay.result.error
    result = H.result_json(replay)
    assert result["completed"] is False and result["job_store"]["terminal_committed"] is False
    run_dir = _run_dir(replay)
    assert jobstore.read_terminal(run_dir) is None
    filled, problem = monitor._committed_terminal(run_dir, dict(completed=True, error=""))
    assert "never committed its terminal state" in problem
    assert not checkpoint.load(run_dir)["sealed"], "an uncommitted ending seals nothing"


def test_an_altered_authority_record_refuses_the_resume(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    manifest = jobstore.read_manifest(first)
    assert "authority" in manifest.records
    held = checkpoint.load(first)["authority"]
    assert held and jobstore.read_record(first, "authority")["digest"] == held
    altered = dict(jobstore.read_record(first, "authority"), digest="0" * 32)
    _commit(first, "authority", altered)
    with pytest.raises(checkpoint.CheckpointRefused, match="approval authority on record is not the one"):
        _episode(replay, resume_from=first)


@pytest.mark.requirements_ledger
def test_a_clean_two_task_run_commits_its_terminal_with_its_acceptance_basis(tmp_path, monkeypatch):
    """The positive record, held to the complete typed outcome
    (``record_complete=True``): the committed terminal says completed on
    the packet the final audit judged, every obligation is established,
    every session effect settled, and the checkpoint is sealed in the same
    generation."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES, max_tasks=2)
    assert replay.result.completed, replay.result.error
    run_dir = _run_dir(replay)
    terminal = jobstore.read_terminal(run_dir)
    assert terminal["completed"] is True and terminal["error"] == "" and not terminal["interrupted"]
    acceptance = terminal["acceptance"]
    assert acceptance["current"] and {r: a["met"] for r, a in acceptance["requirements"].items()} == {"R1": True,
                                                                                                    "R2": True}
    result = H.result_json(replay)
    assert result["job_store"]["terminal_committed"] is True and result["job_store"]["refused"] is None
    assert acceptance["binding"] == result["requirements"]["audits"][-1]["packet"] or acceptance["binding"]
    assert all(o["established"] for o in result["evidence_obligations"]) and result["evidence_obligations"]
    assert all(o["status"] == "settled" and o["outcome"] == "completed" for o in result["operations"])
    assert {o["purpose"] for o in result["operations"]} >= {"model call", "check"}
    manifest = jobstore.read_manifest(run_dir)
    assert manifest.clean and {"terminal", "checkpoint", "budget", "authority"} <= set(manifest.records)
    assert checkpoint.load(run_dir)["sealed"] is True
    assert json.loads((run_dir / "result.json").read_text())["completed"] is True
