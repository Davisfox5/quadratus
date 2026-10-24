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
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2, FILES, FIXED, Script
from tests.lifecycle.test_step2_recovery import _commit, _episode, _interrupted, _plan

CLEAN_T2 = "NO FINDINGS\nR2: MET - README.md"


def _run_dir(replay) -> Path:
    return Path(replay.result.run_dir)


def _not_run_rows(prompt, path):
    """The ids of the lead's NOT RUN obligation (kind not_run: its behavior
    is exactly the item's path), never a declared acceptance scenario
    (behavior "P<n>: ...") that names the same test file in its location
    (root's full suite on c9a665b: the declared P2 row also carries
    tests/test_http.py)."""
    out = []
    for oid in H.obligation_ids(prompt):
        row = prompt.split(f"- {oid} (", 1)[1].split("\n", 1)[0].split("): ", 1)[1]
        if re.split(r"; (?:evidence|runner) ", row, maxsplit=1)[0] == path:
            out.append(oid)
    return out


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
        for oid in _not_run_rows(call.prompt, "tests/test_http.py"):
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
    cycle = [a for a in H.result_json(replay)["plan_admissions"] if a["scope"] == "fix-cycle" and a["task"] == "t2"]
    # Every correction cycle is admitted on its own before its editor
    # writes (admission/2): the first, selected by the review's findings,
    # and the second, selected by the recheck's.
    assert len(cycle) == 2 and all(a["feasible"] for a in cycle)
    assert cycle[0]["stages"] == cycle[1]["stages"] == ["fix revision(editing/write)",
                                                         "recheck by openai:gpt-5.6-sol(reserve)"]

    # The same job, with a second cycle that cannot be paid: it is refused
    # before its revision, recorded, and the findings it would have
    # answered stay open as the reviewer wrote them.
    from quadratus import delivery
    real = Session._fix_cycle_stages
    unpayable = [delivery.PlanStage("fix revision", "editing", editing=True)] + [
        delivery.PlanStage(f"recheck {n}", "reserve") for n in range(300)]
    asked = []

    def second_unpayable(self, spec, unresolved, **kw):
        asked.append(spec.task_id)
        return real(self, spec, unresolved, **kw) if asked.count(spec.task_id) == 1 else unpayable
    (tmp_path / "short").mkdir()
    monkeypatch.setattr(Session, "_fix_cycle_stages", second_unpayable)
    short = H.run(tmp_path / "short", monkeypatch, _recheck_script(replies), files=FILES, max_tasks=2,
                  record_complete=False)
    assert not short.result.completed and short.result.error.startswith("FindingsOpen")
    assert len(short.of("revision")) == 1
    result = H.result_json(short)
    refusal = [r for r in result["finish_refusals"] if r["what"] == "fix round"][-1]
    assert "a further fix cycle cannot be paid whole" in refusal["detail"]
    assert [a["feasible"] for a in result["plan_admissions"] if a["scope"] == "fix-cycle"] == [True, False]
    assert _findings(short)["RFT2-1"]["state"] == "unresolved"

    # The first cycle unpayable (admission/2): the correction the review
    # selected is refused before its editor writes, no revision call is
    # made, the findings stay open as the reviewer wrote them, and the run
    # ends with them open; no review was dropped to fit.
    monkeypatch.setattr(Session, "_fix_cycle_stages", lambda self, spec, unresolved, **kw: unpayable)
    (tmp_path / "first").mkdir()
    first = H.run(tmp_path / "first", monkeypatch, _recheck_script(replies), files=FILES, max_tasks=2,
                  record_complete=False)
    assert not first.result.completed and first.result.error.startswith("FindingsOpen")
    assert not first.of("revision") and not first.of("recheck")
    result = H.result_json(first)
    refusal = [r for r in result["finish_refusals"] if r["what"] == "fix round"][-1]
    assert "the correction the review selected cannot be paid whole" in refusal["detail"]
    assert [a["feasible"] for a in result["plan_admissions"] if a["scope"] == "fix-cycle"] == [False]
    # Never rechecked, so never "unresolved": open, exactly as the reviewer wrote them.
    assert [(f["state"], f["closed"]) for f in result["review_findings"] if f["fid"].startswith("RFT2")] == [
        ("open", False), ("open", False)]
    assert [a["stages"] for a in result["plan_admissions"] if a["task"] == "t2" and a["scope"] == "full"][0] == [
        "lead draft(editing/write)", "review by openai:gpt-5.6-sol(reserve)"]


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
    """A parent whose committed records hold an operation settled after its
    last quiescent point (a reply or a check that settled before the
    process died, with no checkpoint written since): the records are
    consistent, nothing is unsettled, and the resume is still refused
    rather than resumed over what the record never consumed. A read-only
    call's settled reply is a judgment the record must consume under its
    original identity, never re-draw: refused, with its receipt named
    (Astra's readonly_negative control). (An in-session interruption after
    a task's check is refused earlier, on the source the edit moved: the
    same answer.)"""
    replay, first = _interrupted(tmp_path, monkeypatch)
    store = jobstore.JobStore(first)
    late = dict(opid="OPzz-1", purpose="model call", status="settled", owner_id="zz", allowed_effects=["read"],
                outcome="completed", result_digest="d" * 64, receipt="receipt-opzz-1")
    store.publish({"operations-zz": [late], "receipt-opzz-1": dict(opid="OPzz-1", result="R2: NOT MET - no example")},
                  session_unsettled=("zz", []))
    assert jobstore.unsettled_operations(first) == ()
    body = checkpoint.load(first)
    with pytest.raises(checkpoint.CheckpointRefused, match="OPzz-1: receipt-opzz-1"):
        checkpoint._unconsumed(first, body, jobstore.read_budget(first)[0])
    with pytest.raises(checkpoint.CheckpointRefused, match="never consumed by the record"):
        _episode(replay, resume_from=first)
    store.publish({"operations-zz": [late, dict(late, opid="OPzz-2", purpose="check", allowed_effects=["run-check"],
                                                receipt="receipt-opzz-2")],
                   "receipt-opzz-2": dict(opid="OPzz-2", result="1 passed")})
    with pytest.raises(checkpoint.CheckpointRefused, match="check, model call"):
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
    audit = result["requirements"]["audits"][-1]
    # Like with like: the terminal's packet is the one the audit's own
    # dispositions settled, its binding the audit's binding; the packet the
    # auditor was handed is named beside them and differs where the audit
    # closed something.
    assert acceptance["packet"] == audit["packet_settled"] and acceptance["binding"] == audit["binding"]
    assert audit["packet"] and acceptance["packet"] != acceptance["binding"]
    # Every settled operation's receipt is readable from the committed
    # generation: a check's tail and receipts, a seat's whole reply.
    rows = [o for rows in jobstore.operation_records(run_dir).values() for o in rows]
    check = next(o for o in rows if o["purpose"] == "check")
    receipt = jobstore.read_record(run_dir, check["receipt"])
    assert '"passed": true' in receipt["result"] and "1 passed" in receipt["result"]
    lead = next(o for o in rows if o["purpose"] == "model call" and o["allowed_effects"] == ["project-write"])
    assert "CHANGED" in jobstore.read_record(run_dir, lead["receipt"])["result"]
    assert all(jobstore.read_record(run_dir, o["receipt"]) is not None for o in rows if o["status"] == "settled")
    assert all(o["established"] for o in result["evidence_obligations"]) and result["evidence_obligations"]
    assert all(o["status"] == "settled" and o["outcome"] == "completed" for o in result["operations"])
    assert {o["purpose"] for o in result["operations"]} >= {"model call", "check"}
    manifest = jobstore.read_manifest(run_dir)
    assert manifest.clean and {"terminal", "checkpoint", "budget", "authority"} <= set(manifest.records)
    assert checkpoint.load(run_dir)["sealed"] is True
    assert json.loads((run_dir / "result.json").read_text())["completed"] is True


# -- the remaining boundaries (root review of 04a967d, Astra durability review) ------------

@pytest.mark.requirements_ledger
def test_a_settled_judgment_the_record_never_consumed_is_not_asked_again_on_resume(tmp_path, monkeypatch):
    """Astra's readonly_negative control through the real controller: the
    final audit's reply (a NOT MET) settles, both operation layers settle,
    and the coordinator dies before the audit consumer folds it into the
    record. The resume is refused, the reply is retained as the settled
    operation's receipt and named, and no second audit is drawn."""
    original = Session._apply_acceptance
    fired = []

    def die_before_consumption(self, found, audited):
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt
        return original(self, found, audited)
    monkeypatch.setattr(Session, "_apply_acceptance", die_before_consumption)
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["auditor"] = lambda call, replay: ("R1: MET - app.py\nR2: NOT MET - README lacks the example the "
                                                        "goal asks for; at README.md; example: none")
    # Three slots: the planner's DONE after t2 follows a quiescent point, so
    # the source the checkpoint holds is the delivered one and the only
    # thing between the checkpoint and the death is the settled judgment.
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error and replay.of("auditor")
    run_dir = _run_dir(replay)
    assert checkpoint.load(run_dir)["identities"]["source"] == H.result_json(replay)["source_fingerprint"]
    assert checkpoint.load(run_dir)["state"].get("_final_audit") is None, "the audit never reached the record"
    rows = [o for rows in jobstore.operation_records(run_dir).values() for o in rows]
    negative = [o for o in rows if o["status"] == "settled" and o["receipt"]
                and "R2: NOT MET" in (jobstore.read_record(run_dir, o["receipt"]) or {}).get("result", "")]
    assert len(negative) == 1 and negative[0]["purpose"] == "model call", "the NOT MET is retained as a receipt"
    with pytest.raises(checkpoint.CheckpointRefused, match="never consumed by the record") as refused:
        _episode(replay, resume_from=run_dir, max_tasks=3)
    assert negative[0]["receipt"] in str(refused.value) and "not re-drawn" in str(refused.value)
    assert len(replay.of("auditor")) == 1, "no second audit was drawn"
    budget = jobstore.read_budget(run_dir)[0]
    raw = [o for o in budget["operations"]["settled"] if o["identity"]["role"] == "auditor"]
    assert raw and "R2: NOT MET" in jobstore.read_record(run_dir, raw[0]["receipt"])["reply"]


@pytest.mark.requirements_ledger
def test_the_approval_authority_is_committed_before_the_first_call_and_named_by_every_operation(tmp_path, monkeypatch):
    """Astra's ordering control on 04a967d: at the first lead dispatch the
    committed authority record held no requirements. It now holds the
    requirements as revisions, with the cross-vendor review recorded as a
    review of exactly those texts and never as the human's decision, and
    every operation's identity names its digest."""
    seen = {}
    original = Session._invoke_model

    def capture(self, key, prompt, **kw):
        if (invocation_context.get() or {}).get("role") == "lead" and "lead" not in seen:
            seen["lead"] = jobstore.read_record(self.config.job_store.run_dir, "authority")
        return original(self, key, prompt, **kw)
    monkeypatch.setattr(Session, "_invoke_model", capture)
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES, max_tasks=2,
                   record_complete=False)
    assert replay.result.completed, replay.result.error
    record = seen["lead"]
    rows = {r["rid"]: r for r in record["requirements"]}
    assert set(rows) == {"R1", "R2"} and record["policy"] == "authority/3"
    assert all(r["origin"] == "inferred" and r["decision_receipt"] is None and r["approved"] is False
               for r in rows.values()), "a model review is not the human's consent"
    assert all(r["review"]["reviewer"] == "openai:gpt-5.6-sol" and r["review"]["text_digest"] == r["text_digest"]
               for r in rows.values())
    run_dir = _run_dir(replay)
    budget = jobstore.read_budget(run_dir)[0]
    lead = next(o for o in budget["operations"]["settled"] if o["identity"]["role"] == "lead")
    assert lead["identity"]["authority"] == record["digest"]
    rows_ops = [o for rows in jobstore.operation_records(run_dir).values() for o in rows]
    assert rows_ops and all(o["identity"]["authority"] for o in rows_ops)
    assert {o["identity"]["authority"] for o in rows_ops if o["purpose"] == "model call"} >= {record["digest"]}


@pytest.mark.requirements_ledger
def test_the_repair_route_after_a_failed_check_is_admitted_before_the_editor_writes(tmp_path, monkeypatch):
    """Root's narrowed route finding on 04a967d: a hypothetical gate fix is
    not pre-reserved at dispatch, but once the real check has failed the
    repair, the check that judges it and what the task still owes are
    admitted whole before the editor writes. Unpayable, the failed check
    stands and the refusal is recorded; the gate's wall time counts only
    when measured, and says so when it is not."""
    from tests.lifecycle.test_finish_capacity import README

    def lead(call, replay):
        if call.task == "t1":
            return "Implemented add.\nCHANGED: []"
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def gate_fix(call, replay):
        # The repair is the editing call that delivers P1, so it binds it
        # to the case the check runs (the draft changed nothing).
        H.write(call, {"app.py": FIXED})
        return 'Fixed add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["lead"] = lead
    script.overrides["gate-fix"] = gate_fix
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    assert len(replay.of("gate-fix")) == 1
    repair = [a for a in H.result_json(replay)["plan_admissions"] if a["scope"] == "gate-repair"]
    assert len(repair) == 1 and repair[0]["task"] == "t1" and repair[0]["feasible"]
    # The route is the repair alone: the task's record is the harness's and
    # costs no call (2026-10-21), so nothing else is owed after the fix.
    assert repair[0]["stages"] == ["gate fix(editing/write)"]
    assert repair[0]["wall_known"] is True and repair[0]["wall_extra"] > 0, "the failed run measured the gate"
    assert "unmeasured" not in repair[0]["detail"]

    (tmp_path / "short").mkdir()
    from quadratus import delivery
    monkeypatch.setattr(Session, "_gate_repair_stages", lambda self, spec, gate, kind: [
        delivery.PlanStage("gate fix", "editing", editing=True)] + [
        delivery.PlanStage(f"owed {n}", "reserve") for n in range(300)])
    short = H.run(tmp_path / "short", monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert not short.result.completed and not short.of("gate-fix"), "no editor wrote on an unpayable route"
    result = H.result_json(short)
    refusal = [r for r in result["finish_refusals"] if r["what"] == "gate fix"][-1]
    assert "repair route after the failed check cannot be paid whole" in refusal["detail"]
    assert result["checks"][-1]["passed"] is False, "the failed check stands"
    assert (short.project / "app.py").read_text() == FILES["app.py"]


@pytest.mark.requirements_ledger
def test_combined_debt_names_every_reason_and_commits_an_incomplete_terminal(tmp_path, monkeypatch):
    """The composite negative through the real controller (V23): one
    requirement carries an executed obligation the auditor finds
    insufficient and an open review finding the auditor keeps; the other
    is met. Every reason is on the acceptance, the obligation and the
    finding keep their own states, and the committed terminal says
    incomplete with both reasons."""
    script = _http_script("tests/test_http.py - the sandbox denied binding 127.0.0.1")
    script.overrides["collaborator"] = lambda call, replay: (
        "BLOCKING: the handler ignores query parameters | impact: other\n"
        "R1: MET - app.py\nR2: MET - tests/test_http.py")
    script.overrides["recheck"] = lambda call, replay: "RFT1-1: UNRESOLVED still ignores them"

    def auditor(call, replay):
        lines = ["R1: MET - app.py", "R2: MET - tests/test_http.py", "RFT1-1: UNRESOLVED it stands"]
        for oid in _not_run_rows(call.prompt, "tests/test_http.py"):
            lines.append(f"{oid}: INSUFFICIENT the test never exercises the error response")
        return "\n".join(lines)
    script.overrides["auditor"] = auditor
    replay = H.run(tmp_path, monkeypatch, script, files=HTTP_FILES, check=GATE_FILES, max_tasks=1,
                   lead_runs_commands=False, record_complete=False)
    assert not replay.result.completed and replay.result.error.startswith("RequirementsUnmet")
    typed = next(o for o in _obligations(replay) if o["kind"] == "not_run")
    assert typed["execution"] == "ran" and typed["sufficiency"] == "insufficient"
    finding = _findings(replay)["RFT1-1"]
    assert finding["state"] == "unresolved" and finding["audit_carried"] and finding["history"][-1]["role"] == "audit"
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    reasons = audit["acceptance"]["R2"]["reasons"]
    assert any(f"{typed['oid']}: sufficiency insufficient" == r for r in reasons)
    assert any(r.startswith("RFT1-1: open") for r in reasons)
    assert audit["acceptance"]["R1"]["met"] is False, "the obligation and the finding bind R1 too"
    terminal = jobstore.read_terminal(_run_dir(replay))
    assert terminal["completed"] is False and terminal["acceptance"]["requirements"]["R2"]["reasons"] == reasons
    assert "RFT1-1" in replay.result.error or "R2" in replay.result.error


@pytest.mark.requirements_ledger
def test_every_operation_names_an_authority_already_committed_when_it_starts(tmp_path, monkeypatch):
    """Astra's dispatch control on 6bf1afc: the requirements review named
    the newly inferred requirements while the committed record still held
    the pre-intake authority. Now every operation (intake calls included)
    commits the authority it names before it starts: at each operation's
    start publication the committed record's digest is the one the
    operation names."""
    original = jobstore.JobStore.publish
    seen, violations = [], []

    def publish(self, updates, **kw):
        for name, rows in updates.items():
            if name.startswith("operations-"):
                committed = (jobstore.read_record(self.run_dir, "authority") or {}).get("digest")
                for op in rows:
                    if op.get("status") == "started":
                        seen.append((op["purpose"], committed))
                        if op["identity"].get("authority") != committed:
                            violations.append((op["purpose"], op["identity"].get("authority"), committed))
        return original(self, updates, **kw)
    monkeypatch.setattr(jobstore.JobStore, "publish", publish)
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2), files=FILES, max_tasks=2,
                   record_complete=False)
    assert replay.result.completed, replay.result.error
    assert violations == [] and len(seen) >= 10
    assert len({digest for _, digest in seen}) >= 2, "the authority moved at intake and every later call named the new one"
    assert seen[0][1] is not None, "the very first call already ran under a committed authority"
    run_dir = _run_dir(replay)
    budget = jobstore.read_budget(run_dir)[0]
    review = next(o for o in budget["operations"]["settled"] if o["identity"]["role"] == "requirements-review")
    assert review["identity"]["authority"] in {digest for _, digest in seen}


# --- from the author's adversarial review of e688af5 ----------------------------------

@pytest.mark.requirements_ledger
def test_parallel_children_work_under_the_parents_committed_authority_and_the_resume_holds(tmp_path, monkeypatch):
    """Adversarial review of e688af5: a parallel child built its authority
    record from its own empty ledger and committed it over the parent's,
    every child call named that record, the parent's cache went stale and
    the checkpoint written after the wave could never be resumed. A
    child's authority is its parent's: through the wave only the parent's
    record is committed, every operation names the record committed when
    it started, and the resume holds."""
    from tests.lifecycle.test_step2_recovery import _interrupt_after, _simple
    batch = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    published, violations = [], []
    original = jobstore.JobStore.publish

    def publish(self, updates, **kw):
        if "authority" in updates:
            published.append((updates["authority"]["digest"], len(updates["authority"]["requirements"])))
        for name, rows in updates.items():
            if name.startswith("operations-"):
                committed = (jobstore.read_record(self.run_dir, "authority") or {}).get("digest")
                for op in rows:
                    if op.get("status") == "started" and op["identity"].get("authority") != committed:
                        violations.append((op["purpose"], op["identity"].get("authority"), committed))
        return original(self, updates, **kw)
    monkeypatch.setattr(jobstore.JobStore, "publish", publish)
    _interrupt_after(monkeypatch, 2)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead), files=FILES,
                   max_tasks=3, record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error
    run_dir = _run_dir(replay)
    batches = H.result_json(replay).get("parallel_batches") or []
    assert batches and batches[0]["tasks"] == ["t1", "t2"]
    assert violations == []
    assert [n for _, n in published] == [0, 2], "the pre-intake record, then the one with R1/R2, and nothing from a child"
    committed = jobstore.read_record(run_dir, "authority")
    assert len(committed["requirements"]) == 2 and checkpoint.load(run_dir)["authority"] == committed["digest"]
    budget = jobstore.read_budget(run_dir)[0]
    children = [o for o in budget["operations"]["settled"] if o["identity"].get("task") in ("t1", "t2")]
    assert children and all(o["identity"]["authority"] == committed["digest"] for o in children)
    resumed = _episode(replay, resume_from=run_dir.name, max_tasks=3)
    assert "CheckpointRefused" not in (resumed.error or "") and (Path(resumed.run_dir) / "lineage.json").exists()
    assert json.loads((Path(resumed.run_dir) / "lineage.json").read_text())["parent"] == run_dir.name


@pytest.mark.requirements_ledger
def test_a_routing_decision_is_an_operation_under_the_committed_authority(tmp_path, monkeypatch):
    """Adversarial review of e688af5: the Jev decider's reservation (a
    billed call) named no authority, and with no cross-vendor reviewer it
    ran before the record covering the freshly numbered requirements was
    committed. A routing decision now commits the authority first, names
    it on the reservation and on a session operation, and keeps the
    decision or the refusal as that operation's receipt."""
    from quadratus.decisions import JevDecider
    decider = JevDecider(meter=None, budget=None)
    decider.api_key = "fake"
    monkeypatch.setattr(JevDecider, "available", lambda self: "")

    class Offline:
        def system_one(self, **kw):
            raise RuntimeError("offline")
    decider._client = Offline()
    monkeypatch.setattr(Session, "_auditor", lambda self: None)
    seen = []
    original = Session._route_with_decider

    def observe(self, meta, task_id=None):
        committed = jobstore.read_record(self.config.job_store.run_dir, "authority") or {}
        seen.append(dict(ledger=sorted(self.memory.ledger.requirements), committed=committed.get("digest"),
                         rows=len(committed.get("requirements", []))))
        return original(self, meta, task_id)
    monkeypatch.setattr(Session, "_route_with_decider", observe)
    task = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
            "SCOPE: " + json.dumps({"permitted_paths": ["app.py"], "intended_result": "app.py written",
                                    "acceptance": ["app.py meets the task"], "max_lines": 20,
                                    "proofs": H.proofs_for("R1", "app.py")})
            + "\nImplement add in app.py.\nCOVERS: R1")

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(task), lead=lead), files=FILES, max_tasks=1,
                   decider=decider, record_complete=False)
    run_dir = _run_dir(replay)
    assert seen and seen[0]["ledger"] == ["R1", "R2"]
    budget = jobstore.read_budget(run_dir)[0]
    reservations = [o for o in budget["operations"]["settled"] if o["identity"].get("role") == "decider"]
    assert reservations, "the decider was never asked"
    committed = jobstore.read_record(run_dir, "authority")
    assert len(committed["requirements"]) == 2
    assert all(o["identity"]["authority"] == committed["digest"] for o in reservations)
    ops = [o for rows in jobstore.operation_records(run_dir).values() for o in rows
           if o["purpose"] == "routing decision"]
    assert len(ops) == len(reservations) and all(o["status"] == "settled" and o["outcome"] == "failed" for o in ops)
    assert all(o["identity"]["authority"] == committed["digest"] for o in ops)
    receipt = jobstore.read_record(run_dir, ops[0]["receipt"])
    assert receipt["opid"] == ops[0]["opid"] and receipt["full_digest"] == ops[0]["receipt_digest"]
    failure = json.loads(receipt["result"])["failure"]
    assert failure.split(":")[0] in ops[0]["note"], "the receipt keeps the failure the harness reported"
    assert all(d["error"] for d in H.result_json(replay).get("decisions") or [{"error": "x"}])


@pytest.mark.requirements_ledger
def test_a_pre_review_subset_gate_repair_route_carries_the_reviews_ahead_of_it(tmp_path, monkeypatch):
    """Adversarial review of e688af5 (route lens): on the normal dispatch
    path the cheap pre-review gate's repair route listed the gate fix and
    the close-out alone, because the task's reviewers were recorded for
    the route only on a lead recovery. The reviews, the revision and the
    rechecks that follow the repaired subset gate are on the admitted
    route, and the admission precedes the gate-fix call."""
    import sys

    from quadratus import project_run
    from quadratus.integration import GateCommand
    lint = (sys.executable, "-m", "pytest", "-q", "tests/test_app.py", "--quadratus-report={report}")
    monkeypatch.setattr(project_run, "_extra_gate_commands",
                        lambda extra_checks: [GateCommand(id="lint", argv=lint, cheap=True)])

    def lead(call, replay):
        if call.task == "t1":
            return "Implemented add.\nCHANGED: []"
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def gate_fix(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Fixed add.\nCHANGED: ["app.py"]'
    script = _script([PLAN_1, PLAN_2], t2_review=CLEAN_T2)
    script.overrides["lead"] = lead
    script.overrides["gate-fix"] = gate_fix
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    result = H.result_json(replay)
    repair = [a for a in result["plan_admissions"] if a["scope"] == "gate-repair"]
    assert repair and repair[0]["task"] == "t1" and repair[0]["feasible"]
    stages = repair[0]["stages"]
    fix_index = next(i for i, c in enumerate(replay.calls) if c.role == "gate-fix")
    reviewers = {(c.vendor, c.model) for i, c in enumerate(replay.calls)
                 if c.role.startswith("collaborator") and c.task == "t1" and i > fix_index}
    assert reviewers, "the reviews followed the repaired subset gate"
    assert len([s for s in stages if s.startswith("review by")]) == len(reviewers), stages
    assert not any(s.startswith(("revision", "recheck by")) for s in stages), "a correction is admitted when selected"
    assert repair[0]["at_attempt"] <= fix_index, "admitted before the editor wrote"
