"""The consolidated A+B boundary correction, at the pure and unit level
(root's review of f4fe5b7, 2026-10-18).

Boundary 1, one evidence packet and one acceptance predicate: the packet
digest moves with any field; the predicate lowers a MET for an explicit
INSUFFICIENT, an unjudged or stale sufficiency, a prerequisite with no
runner and an open finding; a cached final audit is void once a check lands
or a requirement's text changes while it runs; an executed obligation goes
stale on gate or environment drift; an INVALIDATED closes only on evidence
the record resolves, and the audit never resolves. Boundary 2, the durable
store: a second writer's stale expectation is refused and latched, a reader
sees the generation on disk, a tampered manifest is corrupt, and a late
effect past the watermark refuses a resume where a late read-only call does
not. Boundary 3: a route's own checks count on the wall.
"""

import json
from dataclasses import replace

import pytest

from quadratus import checkpoint, jobstore, judgment
from quadratus import delivery as D
from quadratus.generations import GenerationConflict
from quadratus.integration import IntegrationGate
from quadratus.session import Session
from tests.test_delivery_records import SRC_A, SRC_B, _finding, _obligation, _receipt
from tests.test_unexecuted_acceptance import (
    SCENARIO,
    UNIT,
    _gate_result,
    _identity,
    _not_run,
    _ran,
    _session,
)

B = "bind-" + "9" * 60


# --- boundary 1: the packet and the predicate ----------------------------------------------

def _packet(**over):
    base = dict(source=SRC_A, requirements=(("R1", 1, "d1"),), gate="g1", environment="e1", evidence_identity="ev1",
                checks=("c1",), review_checks=(), findings=(("RF1", "open", ""),),
                obligations=(("O1", "ran", "sufficient", "r1"),), rulings="ru", decisions="de", authority="au")
    base.update(over)
    return D.EvidencePacket(**base)


@pytest.mark.parametrize("field, value", [
    ("source", SRC_B), ("requirements", (("R1", 2, "d1"),)), ("requirements", (("R1", 1, "d2"),)), ("gate", "g2"),
    ("environment", "e2"), ("evidence_identity", "ev2"), ("checks", ("c1", "c2")), ("review_checks", ("rc1",)),
    ("findings", (("RF1", "open", "b7"),)), ("findings", (("RF1", "resolved", ""),)),
    ("obligations", (("O1", "ran", "unjudged", "r1"),)), ("obligations", (("O1", "ran", "sufficient", "r2"),)),
    ("rulings", "ru2"), ("decisions", "de2"), ("authority", "au2"), ("policy", "packet/0"),
    ("design_checks", ("d1",)), ("scratch_proofs", ("s1",)), ("evidence_requests", ("e1",)),
])
def test_the_packet_digest_moves_with_any_field(field, value):
    base = _packet()
    assert _packet(**{field: value}).digest != base.digest
    assert _packet().digest == base.digest and base.to_dict()["digest"] == base.digest


def _rows(**over):
    base = dict(oid="O1", requirements=[["R1", 1]], runner="gate", execution="ran", sufficiency="sufficient",
                sufficiency_current=True, reason="")
    base.update(over)
    return [base]


@pytest.mark.parametrize("obligation, reason", [
    (dict(runner=None, execution="missing", sufficiency="unjudged", sufficiency_current=False), "no authorized runner"),
    (dict(execution="missing", sufficiency="unjudged", sufficiency_current=False), "execution missing"),
    (dict(execution="stale", reason="executed on another source", sufficiency="sufficient"), "execution stale"),
    (dict(sufficiency="insufficient"), "sufficiency insufficient"),
    (dict(sufficiency="unjudged", sufficiency_current=False), "sufficiency unjudged"),
    (dict(sufficiency_current=False), "judged on other evidence"),
])
def test_the_predicate_lowers_a_met_for_each_obligation_state_that_is_not_established(obligation, reason):
    verdicts = {"R1": (True, "app.py"), "R2": (True, "README.md")}
    out = D.acceptance(verdicts, _rows(**obligation), [], {})
    assert not out["R1"].met and any(reason in r for r in out["R1"].reasons)
    assert out["R2"].met and out["R2"].reasons == (), "an obligation lowers only the requirements it binds"


def test_the_predicate_needs_the_verdict_and_closes_on_open_findings_by_id_or_cover():
    assert D.acceptance({"R1": (True, "app.py")}, _rows(), [], {})["R1"].met
    assert not D.acceptance({"R1": (False, "missing")}, _rows(), [], {})["R1"].met
    named = [dict(fid="RF1", closed=False, state="unresolved", requirement=["R1", 1], task="t9")]
    covered = [dict(fid="RF2", closed=False, state="open", requirement=None, task="t1")]
    closed = [dict(fid="RF3", closed=True, state="resolved", requirement=["R1", 1], task="t1")]
    out = D.acceptance({"R1": (True, "app.py"), "R2": (True, "README.md")}, _rows(), named + covered + closed,
                       {"t1": ["R1"]})
    assert not out["R1"].met and set(out["R1"].reasons) == {"RF1: open (unresolved)", "RF2: open (open)"}
    assert out["R2"].met


def test_refresh_stales_an_execution_on_gate_or_environment_drift():
    ran = D.discharge(_obligation(), _receipt(), current_source=SRC_A, current_requirements={"R8": 1, "R9": 1}).obligation
    same = D.refresh(ran, current_source=SRC_A, current_requirements={"R8": 1, "R9": 1}, current_gate="g1",
                     current_environment="env-a")
    assert same.execution == "ran"
    gate = D.refresh(ran, current_source=SRC_A, current_requirements={"R8": 1, "R9": 1}, current_gate="g2",
                     current_environment="env-a")
    assert gate.execution == "stale" and "gate changed" in gate.reason
    env = D.refresh(ran, current_source=SRC_A, current_requirements={"R8": 1, "R9": 1}, current_gate="g1",
                    current_environment="env-b")
    assert env.execution == "stale" and "environment changed" in env.reason
    missing = D.refresh(_obligation(), current_source=SRC_A, current_requirements={"R8": 1, "R9": 1},
                        current_gate="g2", current_environment="env-b")
    assert missing.execution == "missing", "only an execution can go stale"


def test_a_conflicting_sufficiency_reading_is_no_verdict_and_names_the_conflict():
    verdicts, conflicts = judgment.sufficiency_reading("O1: SUFFICIENT covers it\nO1: INSUFFICIENT misses the error "
                                                       "path\nO2: INSUFFICIENT no run", ["O1", "O2"])
    assert verdicts == {"O2": ("insufficient", "no run")}
    assert set(conflicts) == {"O1"} and "2 sufficiency lines" in conflicts["O1"]
    assert judgment.sufficiency_verdicts("O1: SUFFICIENT ok", ["O1"]) == {"O1": ("sufficient", "ok")}


def test_the_audit_invalidates_or_keeps_a_carried_finding_and_never_resolves_it():
    carried = _finding("RF2", basis=("none", ""), impact="other")
    reading = judgment.recheck_verdicts("RF2: RESOLVED the revision fixed it", ["RF2"])
    after = D.apply_reading((carried,), reading, author="openai:gpt-5.6-sol", role="audit", binding=B, judged_binding=B,
                            resolve=lambda why: True)
    assert after[0].state == "open" and "does not resolve" in after[0].history[-1].why
    kept = judgment.recheck_verdicts("RF2: UNRESOLVED it stands", ["RF2"])
    assert D.apply_reading((carried,), kept, author="openai:gpt-5.6-sol", role="audit", binding=B,
                           judged_binding=B)[0].state == "unresolved"


def test_a_routes_own_checks_count_on_the_wall():
    stages = [D.PlanStage("lead draft", "editing", editing=True), D.PlanStage("review by x", "reserve")]
    fits = D.admit_plan(stages, calls_left=4, tokens_left=0, wall_left=100, exposures={"editing": 0, "reserve": 0},
                        wall_per_stage=0.0, wall_extra=90.0)
    assert fits.feasible and fits.wall_needed == 90.0
    short = D.admit_plan(stages, calls_left=4, tokens_left=0, wall_left=100, exposures={"editing": 0, "reserve": 0},
                         wall_per_stage=0.0, wall_extra=130.0)
    assert not short.feasible and short.short == {"wall_seconds": 30.0}
    with pytest.raises(ValueError):
        D.admit_plan(stages, calls_left=4, tokens_left=0, wall_left=100, exposures={}, wall_per_stage=0.0,
                     wall_extra=-1)


# --- boundary 1 in the session: cache, in-flight change, drift, resolver -----------------

def test_the_final_audit_is_reused_only_on_the_exact_packet_and_a_check_voids_it(tmp_path):
    session, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    first = session._audit_requirements(final=True)
    assert first["R1"][0] is True and len(prompts) == 1 and session._final_audit_current()
    assert session.requirement_audits[-1]["packet"] == session._packet().digest
    assert session._audit_requirements(final=True) == first and len(prompts) == 1, "the exact packet is not asked again"
    _ran(session, "t1", ["R1"], _gate_result(0))
    assert not session._final_audit_current(), "a check that landed is a different packet"
    session._audit_requirements(final=True)
    assert len(prompts) == 2 and session._final_audit_current()
    record = session._acceptance_record()
    assert record["current"] and record["binding"] == session._final_audit["binding"]
    assert record["requirements"]["R1"]["met"] is True and record["requirements"]["R5"]["met"] is False


def test_a_requirement_text_that_changes_while_the_audit_runs_voids_it_and_caches_nothing(tmp_path):
    session, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    reply = f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n"

    def invoke(key, prompt, **kw):
        prompts.append(prompt)
        session.memory.ledger.requirements["R1"] = "typing filters the cards and the count updates"
        return reply
    session._invoke_model = invoke
    verdicts = session._audit_requirements(final=True)
    assert verdicts["R1"] == (False, "the evidence packet changed during the audit")
    assert session.requirement_audits[-1]["result"].startswith("void: the evidence packet changed")
    assert session._final_audit is None and not session._final_audit_current()


def test_a_failed_audit_is_not_a_final_audit_on_record(tmp_path):
    from quadratus.providers import ProviderError
    session, _ = _session(tmp_path, "")

    def failing(key, prompt, **kw):
        raise ProviderError("transport down")
    session._invoke_model = failing
    verdicts = session._audit_requirements(final=True)
    assert all(not ok for ok, _ in verdicts.values())
    assert session.requirement_audits[-1]["failed"] and session._final_audit is None


def test_an_executed_obligation_goes_stale_when_the_gate_or_the_environment_changes(tmp_path, monkeypatch):
    session, _ = _session(tmp_path, f"R5: MET - {UNIT}\n")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    session.checks.append(dict(passed=True, command="gate suite", source=session._source_fingerprint(),
                               **_identity(session), receipts=[dict(id="extra-2", status="passed",
                                                                    output="# pass 3\n# fail 0\n# skipped 0\n",
                                                                    command=f"node {SCENARIO}")]))
    session._refresh_obligations()
    typed = session.evidence_obligations[0]
    assert typed["runner"] == "gate" and typed["execution"] == "ran"
    held = session.config.integration_gate
    session.config = replace(session.config, integration_gate=IntegrationGate(["node", "--test", UNIT],
                                                                              cwd=session.config.integration_gate.cwd))
    session._refresh_obligations()
    assert session.evidence_obligations[0]["execution"] == "stale"
    assert "gate changed" in session.evidence_obligations[0]["reason"]
    session.config = replace(session.config, integration_gate=held)
    session._refresh_obligations()
    assert session.evidence_obligations[0]["execution"] == "ran", "the recorded receipt discharges it again"
    monkeypatch.setattr(Session, "_environment_id", lambda self: "another-interpreter")
    session._refresh_obligations()
    assert session.evidence_obligations[0]["execution"] == "stale"
    assert "environment changed" in session.evidence_obligations[0]["reason"]


def test_a_conflicting_sufficiency_line_leaves_the_obligation_unjudged_in_the_session(tmp_path):
    session, _ = _session(tmp_path, f"R5: MET - {UNIT}\n")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    session.checks.append(dict(passed=True, command="gate suite", source=session._source_fingerprint(),
                               **_identity(session), receipts=[dict(id="extra-2", status="passed",
                                                                    output="# pass 3\n# fail 0\n# skipped 0\n",
                                                                    command=f"node {SCENARIO}")]))
    session._refresh_obligations()
    session._judge_obligations("openai:gpt-5.6-sol", "O1: SUFFICIENT fine\nO1: INSUFFICIENT not fine",
                               {"R5": (True, UNIT)})
    typed = session.evidence_obligations[0]
    assert typed["sufficiency"] == "unjudged" and "2 sufficiency lines" in typed["sufficiency_note"]
    session._judge_obligations("openai:gpt-5.6-sol", "O1: INSUFFICIENT misses the error path", {"R5": (True, UNIT)})
    assert session.evidence_obligations[0]["sufficiency"] == "insufficient"
    found = {"R5": (True, UNIT)}
    lowered = session._apply_acceptance(found, ["R5"])
    assert found["R5"][0] is False and "O1: sufficiency insufficient" in lowered[0]["reason"]


def test_invalidated_evidence_resolves_only_against_the_record(tmp_path):
    session, _ = _session(tmp_path, "")
    assert not session._evidence_resolves("`UTF8` handling covers it")
    assert not session._evidence_resolves("check-1 shows it"), "no check is recorded yet"
    assert session._evidence_resolves(f"{UNIT} shows add(1, 2) is 3")
    _ran(session, "t1", ["R1"], _gate_result(0))
    assert session._evidence_resolves("check-1 passed") and session._evidence_resolves("receipt extra-1 passed")
    assert session._evidence_resolves("R1 is on the ledger")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    assert session._evidence_resolves("O1 ran")
    assert not session._evidence_resolves("O7 ran") and not session._evidence_resolves("RFT9-1 says so")


def test_a_not_run_item_the_gate_does_not_name_whole_has_no_runner(tmp_path):
    session, _ = _session(tmp_path, "")
    assert session._gate_can_run([SCENARIO]) and session._gate_can_run([UNIT, SCENARIO])
    assert not session._gate_can_run(["tests/browser/run.js"]) and not session._gate_can_run([])
    _not_run(session, "t3", ["R5"], "NOT RUN: tests/browser/run.js - denied")
    assert session.evidence_obligations[-1]["runner"] is None
    found = {"R5": (True, UNIT)}
    session._lower_unexecuted(found)
    assert found["R5"][0] is False and "no runner" in found["R5"][1]


# --- boundary 2: the store under a second writer, a reader and a tamper -----------------

def test_a_second_writer_with_a_stale_expectation_is_refused_and_latched_while_the_first_goes_on(tmp_path):
    first, second = jobstore.JobStore(tmp_path), jobstore.JobStore(tmp_path)
    first.publish({"budget": {"n": 1}})
    assert second.current().number == 1, "a reader sees the generation on disk, never its cache"
    first.publish({"budget": {"n": 2}})
    with pytest.raises(jobstore.StoreUnavailable, match="expected generation 1"):
        second.publish({"budget": {"n": 9}}, expected=1)
    assert isinstance(second.refused, GenerationConflict) or "GenerationConflict" in str(second.refused)
    with pytest.raises(jobstore.StoreUnavailable):
        second.publish({"budget": {"n": 10}})
    first.publish({"budget": {"n": 3}})
    assert jobstore.read_budget(tmp_path)[0] == {"n": 3} and jobstore.read_manifest(tmp_path).number == 3


def test_a_tampered_manifest_is_corrupt_not_an_older_generation(tmp_path):
    store = jobstore.JobStore(tmp_path)
    store.publish({"budget": {"n": 1}})
    path = tmp_path / jobstore.MANIFEST
    body = json.loads(path.read_text())
    body["unsettled"] = ["O-99"]
    path.write_text(json.dumps(body))
    with pytest.raises(jobstore.GenerationCorrupt, match="does not hash"):
        jobstore.read_manifest(tmp_path)
    assert jobstore.read_budget(tmp_path)[0] is None
    with pytest.raises(jobstore.GenerationCorrupt):
        store.current()


def test_any_settled_operation_the_record_never_consumed_refuses_the_resume_and_names_its_receipt(tmp_path):
    """A settled read-only call is a judgment as much as an effect is an
    effect: a NOT MET the coordinator died before parsing is not asked
    again as if never given (Astra's readonly_negative control). The
    receipt is named, must be readable from the committed generation, and
    is never re-drawn."""
    store = jobstore.JobStore(tmp_path)
    budget = {"operations": {"started": [], "settled": []}}

    def op(n, purpose, effects, outcome="completed"):
        return dict(opid=f"OPab-{n}", purpose=purpose, status="settled", owner_id="ab", allowed_effects=effects,
                    outcome=outcome, receipt=f"receipt-opab-{n}")
    receipt = dict(opid="OPab-2", result="R1: NOT MET - app.py: observed data-loss on import")
    store.publish({"budget": budget, "operations-ab": [op(1, "check", ["run-check"]), op(2, "model call", ["read"])],
                   "receipt-opab-1": dict(opid="OPab-1", result="1 passed"), "receipt-opab-2": receipt},
                  session_unsettled=("ab", []))
    consumed = dict(watermark=dict(budget_settled=0, session_settled={"ab": 2}))
    assert checkpoint._unconsumed(tmp_path, consumed, budget) == []
    body = dict(watermark=dict(budget_settled=0, session_settled={"ab": 1}))
    with pytest.raises(checkpoint.CheckpointRefused, match="never consumed") as refused:
        checkpoint._unconsumed(tmp_path, body, budget)
    assert "model call" in str(refused.value) and "OPab-2: receipt-opab-2" in str(refused.value)
    assert "not re-drawn" in str(refused.value)
    assert jobstore.read_record(tmp_path, "receipt-opab-2")["result"].startswith("R1: NOT MET")
    # A late effect refuses the same way, and a receipt that cannot be read
    # back from the generation refuses on its own account.
    store.publish({"operations-ab": [op(1, "check", ["run-check"]), op(2, "model call", ["read"]),
                                     op(3, "observed check", ["run-check"])]})
    with pytest.raises(checkpoint.CheckpointRefused, match="observed check"):
        checkpoint._unconsumed(tmp_path, body, budget)
    path = tmp_path / jobstore.G.record_path("receipt-opab-2", jobstore.read_manifest(tmp_path).records["receipt-opab-2"])
    path.write_text("{}")
    with pytest.raises(checkpoint.CheckpointRefused, match="not readable from the committed generation"):
        checkpoint._unconsumed(tmp_path, body, budget)


# --- the remaining boundaries (root review of 04a967d, Astra durability review) ---------

@pytest.mark.parametrize("field", ["design", "scratch", "receipt_output"])
def test_every_evidence_the_audit_consumes_is_in_the_packet(tmp_path, field):
    """Root's three Session controls on 04a967d: a final design review,
    an eligible scratch proof and a nested receipt's output each changed
    under an equal binding and the earlier approval was reused. The packet
    now digests each record whole."""
    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    if field == "design":
        s.design_checks.append(dict(task="t1", screenshots=["actual-desktop.png"], verified=True,
                                    final_review=dict(reviewer="openai:independent", verdict="APPROVED")))
    elif field == "scratch":
        s.scratch_proofs.append(dict(task="t1", peer="openai:independent", status="passed", eligible=True,
                                     requirements=["R1"],
                                     identity=s._scratch_identity(s._source_fingerprint(), s._current_checks_digest()),
                                     cases=["test_unit.py::test_add"]))
    else:
        s.checks.append(dict(passed=True, source=s._source_fingerprint(), command="gate suite", gate="g",
                             receipts=[dict(id="actual-required", status="passed", reason="", tests=1,
                                            command="python -m pytest test_unit.py", output="1 passed, 0 skipped",
                                            required=True)]))
    first = s._audit_requirements(final=True)
    assert first["R1"][0] and len(prompts) == 1
    before = s._audit_binding(None)
    if field == "design":
        s.design_checks[0]["final_review"]["verdict"] = "BLOCKING: control is inaccessible"
    elif field == "scratch":
        s.scratch_proofs[0]["status"] = "failed"
    else:
        s.checks[0]["receipts"][0]["output"] = "1 passed, 1 skipped"
    assert s._audit_binding(None) != before, f"{field} is in the packet"
    s._audit_requirements(final=True)
    assert len(prompts) == 2, f"{field} changed: the independent auditor is asked about the changed evidence"


def test_a_check_is_credited_to_the_requirement_revisions_frozen_when_it_launched(tmp_path, monkeypatch):
    """Root's execution-identity control on 04a967d: a real local command
    passes, the requirement is revised before the result is consumed, and
    the record must not be credited to the revision that did not exist
    when the command ran. The record carries the frozen revision and names
    the drift; a receipt built from it binds the superseded revision."""
    import sys
    from types import SimpleNamespace

    from quadratus.artifacts import ArtifactStore
    from quadratus.session import SessionConfig
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def add(a, b): return a + b\n")
    gate = IntegrationGate([sys.executable, "-c", 'assert 1 + 2 == 3; print("1 passed")'], cwd=project,
                           minimum_tests=1)
    s = Session("Implement addition", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                config=SessionConfig(project=project, integration_gate=gate, requirements_ledger=True))
    s.memory.ledger.requirements["R1"] = "Addition returns the sum"
    before = s._requirement_revisions()["R1"]
    actual = gate.run

    def changed_after_execution():
        result = actual()
        assert result.passed, "the real local check passes"
        s.memory.ledger.requirements["R1"] = "Addition additionally exposes a documented API"
        return result
    monkeypatch.setattr(gate, "run", changed_after_execution)
    s._observe_checks(SimpleNamespace(task_id="t1"), "review")
    assert len(s.review_checks) == 1
    current = s._requirement_revisions()["R1"]
    assert current > before
    record = s.review_checks[0]
    assert record["passed"] and record["requirements"]["R1"] == before, "credited to the revision it ran under"
    assert "R1 1->2" in record["identity_drift"]
    receipt = Session._receipt_identity(record, dict(requirements=[["R1", current]]))
    assert receipt["requirements"] == [["R1", before]], "a receipt from it binds the superseded revision"
    # The task check path freezes the same way: the record appended after
    # the run carries the launch identity, with the drift named.
    s.memory.ledger.requirements["R1"] = "Addition returns the sum, revised again"
    launch = s._requirement_revisions()["R1"]
    monkeypatch.setattr(gate, "run", changed_after_execution)
    result = s._check(gate)
    assert result.passed and s._last_check_identity["requirements"]["R1"] == launch
    consumed = s._consumed_identity(s._last_check_identity)
    assert consumed["requirements"]["R1"] == launch and "R1" in consumed["identity_drift"]


def test_only_an_outcome_the_harness_reported_settles_an_operation_as_failed(tmp_path):
    """A library or OS error inside an effect proves nothing about what the
    effect did: the operation stays uncertain. The harness's own exception
    classes are reported outcomes and settle as failed, with the failure in
    the receipt; a suspected partial turn is not one of them."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.providers import PartialWorkSuspected, ProviderError
    from quadratus.session import PartialWorkStopped, SessionConfig
    store = jobstore.JobStore(tmp_path / "run")
    s = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(job_store=store))
    for exc, expected in ((TimeoutError("no answer"), "uncertain"), (OSError("disk"), "uncertain"),
                          (PartialWorkSuspected("cut off"), "uncertain"),
                          (PartialWorkStopped("stopped by the harness"), "settled"),
                          (ProviderError("the transport reported a failure"), "settled")):
        with pytest.raises(type(exc)):
            with s._operation("probe", inputs=dict(what=repr(exc)), effects=("run-probe",)):
                raise exc
        record = s.operations[-1]
        assert record["status"] == expected, repr(exc)
        if expected == "settled":
            assert record["outcome"] == "failed"
            receipt = jobstore.read_record(tmp_path / "run", record["receipt"])
            assert type(exc).__name__ in receipt["result"] and receipt["identity"]["authority"]
        else:
            assert record["outcome"] == "interrupted" and record["result_digest"] is None
            assert record["opid"] in jobstore.read_manifest(tmp_path / "run").unsettled


def test_a_settled_effects_receipt_is_readable_from_the_settlement_generation(tmp_path):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import SessionConfig
    store = jobstore.JobStore(tmp_path / "run")
    s = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(job_store=store))
    with s._operation("check", inputs=dict(task="t1"), effects=("run-check",)) as op:
        op["result"] = dict(passed=False, output="FAILED tests/test_app.py::test_add - assert 0 == 3 (unique-7f3a)")
    record = s.operations[-1]
    manifest = jobstore.read_manifest(tmp_path / "run")
    assert record["status"] == "settled" and record["receipt"] in manifest.records
    receipt = jobstore.read_record(tmp_path / "run", record["receipt"])
    assert "unique-7f3a" in receipt["result"] and receipt["chars"] == len(receipt["result"])
    assert not receipt["truncated"] and receipt["full_digest"]
    with s._operation("model call", inputs=dict(task="t1"), effects=("read",)) as op:
        op["result"] = "x" * (Session._RECEIPT_CHARS + 10)
    bounded = jobstore.read_record(tmp_path / "run", s.operations[-1]["receipt"])
    assert bounded["truncated"] and len(bounded["result"]) == Session._RECEIPT_CHARS
    assert bounded["chars"] == Session._RECEIPT_CHARS + 10


def test_readers_derive_completion_from_the_committed_terminal_when_no_export_exists(tmp_path):
    """Astra's reader control on 04a967d: the crash boundary between the
    committed terminal and the compatibility export. The monitor, the
    history and the story read the terminal."""
    from quadratus import monitor, monitor_story
    root = tmp_path / "project"
    run_dir = root / ".quadratus" / "runs" / "20261018T000000Z-abcd1234"
    run_dir.mkdir(parents=True)
    store = jobstore.JobStore(run_dir)
    body = dict(version=checkpoint.VERSION, lineage={}, sealed=False, resumable=True, uncarried=[], consumed=None,
                identities={}, state={}, written="now")
    assert checkpoint.write(run_dir, body, store=store, budget_snapshot={"reserved_attempts": 3})
    terminal = dict(completed=True, error="", interrupted=False, goal="Build the feature.", explicit_tasks=None,
                    tasks=[dict(task_id="t1", outcome="closed")], budget_stop_reason="", acceptance=None)
    assert checkpoint.seal(run_dir, "completed", store=store, extra={"terminal": terminal})
    assert not (run_dir / "result.json").exists()
    status = monitor.read_status(root)
    assert status["finished"] is True and status["terminal_status"] == "complete"
    assert status["stop_reason"] == "goal reported complete"
    row = monitor._history_row(run_dir)
    assert row["status"] == "complete" and row["stop_reason"] == "goal reported complete" and row["ended"] != "unknown"
    html = monitor_story.render_story(status)
    assert "Completed" in html and "has not finished" not in html
    # Listed tasks: the committed terminal says the goal was not judged.
    listed = dict(terminal, explicit_tasks=dict(listed=2, goal_judged=False))
    store.publish({"terminal": listed})
    assert monitor._history_row(run_dir)["status"] == "listed tasks complete"
    # No terminal committed: unfinished, never completed.
    bare = tmp_path / "bare" / ".quadratus" / "runs" / "20261018T000001Z-00000000"
    bare.mkdir(parents=True)
    jobstore.JobStore(bare).publish({"budget": {"reserved_attempts": 0}})
    assert monitor._history_row(bare)["status"] == "unfinished"
    assert monitor.read_status(tmp_path / "bare")["finished"] is False
