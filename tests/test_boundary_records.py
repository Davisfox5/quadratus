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


# --- the residual same-boundary contracts (root and Astra on 6bf1afc) ------------------

def test_a_task_check_keeps_the_gate_that_actually_executed(tmp_path, monkeypatch):
    """Root's control on 6bf1afc: a real local command passes under a
    one-test gate; the gate's test minimum is raised at the consumer seam.
    The record keeps the gate that executed (with its selection and cwd),
    names the drift, and is never credited to the new gate."""
    import sys

    from quadratus.artifacts import ArtifactStore
    from quadratus.memory import TaskMemory
    from quadratus.session import SessionConfig, TaskSpec, _gate_identity
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("def add(a, b): return a + b\n")
    # A command of its own: the gate runner caches a command's result by
    # its bytes, and the earlier probe in this file ran the same command.
    gate = IntegrationGate([sys.executable, "-c", 'import sys; assert 1 + 2 == 3; print("1 passed")'], cwd=project,
                           minimum_tests=1)
    s = Session("Implement addition", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                config=SessionConfig(project=project, integration_gate=gate, requirements_ledger=True,
                                     max_gate_fixes=0))
    s.memory.ledger.requirements["R1"] = "Addition returns the sum"
    before = _gate_identity(gate)
    actual = gate.run
    runs = []

    def changed_after_execution():
        result = actual()
        runs.append(result.passed)
        gate.minimum_tests = 2
        return result
    monkeypatch.setattr(gate, "run", changed_after_execution)
    spec = TaskSpec("t1", "Implement addition")
    s._active_spec = spec
    s._run_integration_gate("lead", spec, TaskMemory("t1", "lead", s.store))
    assert runs == [True], "the real local check passed once under the original gate"
    current = _gate_identity(gate)
    assert current != before
    record = s.checks[-1]
    assert record["passed"] and record["gate"] == before, "credited to the gate that executed"
    assert "gate changed" in record["identity_drift"]
    assert record["command_cwd"] == "." and record["cwd"] == str(project) and "selection" in record
    # The same frozen identity reaches an observed check: it launches under
    # the reconfigured gate, fails its two-test minimum honestly, and is
    # credited to the gate that ran it; no consumer reads the gate after.
    s._observe_checks(spec, "review")
    observed = s.review_checks[-1]
    assert observed["gate"] == current and observed["passed"] is False and "gate changed" not in observed["identity_drift"]


def test_the_requirements_review_is_of_exactly_the_texts_its_prompt_sent(tmp_path):
    """Astra's in-flight control on 6bf1afc: the ledger changes while the
    review call is out. The review's receipt names the digests the prompt
    carried, so the revision the reviewer never saw carries no review; the
    review's own ADD line is its proposal, not a review of it (adversarial
    review of e688af5), and it never lands on an id that arrived while the
    review was out."""
    import hashlib
    s, prompts = _session(tmp_path, "")
    s.memory.ledger.requirements.clear()
    s.memory.ledger.requirements["R1"] = "old scope seen in prompt"
    s._requirement_revisions()  # observed, as the authority commit before the call observes it
    sent_digest = hashlib.sha256(b"old scope seen in prompt").hexdigest()

    def invoke(key, prompt, **kw):
        prompts.append(prompt)
        assert "R1: old scope seen in prompt" in prompt
        s.memory.ledger.requirements["R1"] = "new scope never in reviewer prompt"
        s.memory.ledger.requirements["R2"] = "arrived while the review was out"
        return "ADD: the reviewer's own proposal"
    s._invoke_model = invoke
    s._review_requirements()
    review = s.requirement_reviews[-1]
    assert review["result"] == "reviewed" and review["texts"] == {"R1": sent_digest}
    assert review["proposed"] == {"R3": hashlib.sha256(b"the reviewer's own proposal").hexdigest()}
    assert s.memory.ledger.requirements["R2"] == "arrived while the review was out"
    rows = {r["rid"]: r for r in s._authority_record()["requirements"]}
    assert rows["R1"]["revision"] == 2 and rows["R1"]["review"] is None, "the reviewer never saw revision 2"
    assert rows["R2"]["review"] is None and rows["R2"]["proposal"] is None
    assert rows["R3"]["review"] is None, "its own proposal is not a review of it"
    assert rows["R3"]["proposal"] == dict(reviewer="openai:gpt-5.6-sol", text_digest=review["proposed"]["R3"])
    assert all(r["approved"] is False and r["decision_receipt"] is None for r in rows.values())
    # Restoring the sent text makes revision 3 the reviewed bytes again.
    s.memory.ledger.requirements["R1"] = "old scope seen in prompt"
    rows = {r["rid"]: r for r in s._authority_record()["requirements"]}
    assert rows["R1"]["review"]["text_digest"] == sent_digest


def test_readers_use_the_committed_terminal_when_the_export_is_truncated(tmp_path):
    """Astra's truncated-export control on 6bf1afc: a result.json that
    exists but cannot be parsed (the ordinary non-atomic export interrupted
    after the durable terminal) must not hide the committed outcome; the
    export's problem stays a diagnostic."""
    from quadratus import monitor, monitor_story
    root = tmp_path / "project"
    run_dir = root / ".quadratus" / "runs" / "20261018T000002Z-abcd1234"
    run_dir.mkdir(parents=True)
    store = jobstore.JobStore(run_dir)
    body = dict(version=checkpoint.VERSION, lineage={}, sealed=False, resumable=True, uncarried=[], consumed=None,
                identities={}, state={}, written="now")
    assert checkpoint.write(run_dir, body, store=store, budget_snapshot={"reserved_attempts": 3})
    terminal = dict(completed=True, error="", interrupted=False, goal="Build the feature.", explicit_tasks=None,
                    tasks=[dict(task_id="t1", outcome="closed")], budget_stop_reason="", acceptance=None)
    assert checkpoint.seal(run_dir, "completed", store=store, extra={"terminal": terminal})
    (run_dir / "result.json").write_text('{"completed":')
    status = monitor.read_status(root)
    assert status["finished"] is True and status["terminal_status"] == "complete"
    assert status["stop_reason"] == "goal reported complete"
    assert "export unreadable, committed terminal used" in status["unknown"]["result.json"]
    row = monitor._history_row(run_dir)
    assert row["status"] == "complete" and "export unreadable" in row["export"]
    assert "Completed" in monitor_story.render_story(status)
    # A malformed export with no committed terminal is still unknown, never
    # completed.
    bare = tmp_path / "bare" / ".quadratus" / "runs" / "20261018T000003Z-00000000"
    bare.mkdir(parents=True)
    jobstore.JobStore(bare).publish({"budget": {"reserved_attempts": 0}})
    (bare / "result.json").write_text('{"completed":')
    assert monitor._history_row(bare)["status"] == "unknown"
    assert monitor.read_status(tmp_path / "bare")["terminal_status"] == "unknown"


def test_concurrent_operations_never_share_an_id_or_a_receipt_name(tmp_path):
    """Adversarial review of e688af5: a lead's concurrent worker errands call
    the session from a thread pool, and an id counted off the list could
    be issued twice, so one settlement re-pointed the other's receipt. Ids
    are issued under a lock, and a receipt name is written once."""
    import threading

    from quadratus.artifacts import ArtifactStore
    from quadratus.session import JobStoreUnavailable, SessionConfig
    store = jobstore.JobStore(tmp_path / "run")
    s = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(job_store=store))
    gate = threading.Barrier(6)

    def effect(n):
        with s._operation("model call", inputs=dict(n=n), effects=("read",)) as op:
            gate.wait(timeout=10)
            op["result"] = f"reply {n}"
    threads = [threading.Thread(target=effect, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    ids = [o["opid"] for o in s.operations]
    assert len(ids) == 6 and len(set(ids)) == 6 and len({o["receipt"] for o in s.operations}) == 6
    manifest = jobstore.read_manifest(tmp_path / "run")
    replies = sorted(jobstore.read_record(tmp_path / "run", o["receipt"])["result"] for o in s.operations)
    assert replies == [f"reply {n}" for n in range(6)] and manifest.unsettled == ()
    # A receipt name the manifest already holds is never re-pointed.
    last = s.operations[-1]
    original = jobstore.read_record(tmp_path / "run", last["receipt"])["result"]
    last["status"] = "started"
    with pytest.raises(JobStoreUnavailable, match="already committed"):
        s._publish_operations(last, before=False, receipt=dict(opid="x", result="other bytes"))
    assert jobstore.read_record(tmp_path / "run", last["receipt"])["result"] == original


def test_a_checkpoint_without_a_consumption_watermark_resumes_only_when_nothing_settled(tmp_path):
    store = jobstore.JobStore(tmp_path)
    empty = {"operations": {"started": [], "settled": []}}
    store.publish({"budget": empty})
    assert checkpoint._unconsumed(tmp_path, dict(state={}), empty) == []
    settled = {"operations": {"started": [], "settled": [dict(opid="O-1", receipt="receipt-o-1")]}}
    store.publish({"budget": settled, "receipt-o-1": dict(opid="O-1", reply="R1: NOT MET - x")})
    with pytest.raises(checkpoint.CheckpointRefused, match="no consumption watermark"):
        checkpoint._unconsumed(tmp_path, dict(state={}), settled)
    with pytest.raises(checkpoint.CheckpointRefused, match="never consumed"):
        checkpoint._unconsumed(tmp_path, dict(watermark=dict(budget_settled=0, session_settled={})), settled)


def test_a_reassessment_is_read_on_the_binding_it_was_handed_and_the_notes_move_the_audit_cache(tmp_path):
    """Adversarial review of e688af5: digesting an evidence request's own
    judgment fields made every reassessment drift between its binding and
    its reading. Those fields are the judgment, not the evidence; the
    proof itself stays in the packet. Reviewer notes and judgment gaps
    reach the audit prompt, so they move the audit cache without moving a
    judgment binding."""
    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    s.evidence_requests.append(dict(id="E1", kind="audit", target="R5", key=["audit", "x"], identity="i",
                                    file="tests/test_e.py", test="def test_x(): pass", stage="intent",
                                    receipt=dict(status="passed", reason="", cases=["test_x"])))
    before = s._packet().digest
    s.evidence_requests[0].update(stage="reassessment started", reassessment="R5: MET - x",
                                  disposition="MET on reassessment", acceptance_binding="b")
    assert s._packet().digest == before, "a judgment written on the request is not new evidence"
    s.evidence_requests[0]["receipt"]["status"] = "failed"
    assert s._packet().digest != before, "the proof's outcome is evidence"
    s._audit_requirements(final=True)
    assert len(prompts) == 1 and s._final_audit_current()
    s.review_notes.append(dict(task="t1", peer="openai:x", text="R1 wording is loose", requirements=["R1"],
                               association="named", source=s._source_fingerprint()))
    assert not s._final_audit_current(), "a note the auditor would be shown is a different audit"
    s._audit_requirements(final=True)
    assert len(prompts) == 2
    from types import SimpleNamespace
    binding = s._judgment_binding(SimpleNamespace(task_id="t1"), "openai:x", "recheck")
    s.judgment_gaps.append(dict(task="t1", peer="openai:x", kind="recheck", why="no line"))
    assert s._judgment_binding(SimpleNamespace(task_id="t1"), "openai:x", "recheck")["identity"] == binding["identity"]
    assert not s._final_audit_current()


# --- from the author's adversarial review of e688af5 ----------------------------------

def test_the_acceptance_on_record_stays_current_as_it_consumes_each_served_evidence(tmp_path):
    """Adversarial review of e688af5: once the packet digested evidence
    records whole, every served challenge left the final audit 'not
    current', a run completed on a reassessed MET committed a terminal
    saying so, and a resume at the cap asked the final audit again. The
    acceptance on record now consumes each done record: its binding moves
    to the packet as the record and its judgment left it, a reassessment
    MET replaces the verdict, the auditor's own packet stays named, and a
    record the source moved under carries nothing forward."""
    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    s._audit_requirements(final=True)
    audited = s._final_audit["packet"]
    assert s._final_audit_current() is True

    def record(rid, **over):
        identity = s._evidence_identity()
        body = dict(id=rid, target="R5", kind="audit", reviewer="openai:gpt-5.6-sol", task=None,
                    finding="R5: NOT MET - no console check", original_disposition="NOT MET", claim="",
                    file="tests/test_e1.py", file_sha256="x", source=s._source_fingerprint(), identity=identity,
                    key=["audit", "R5", identity], receipt=dict(status="failed", reason="1 failed", cases=[]),
                    execution=None, reassessment=None, stage="intent", test="def test_e1(): pass\n",
                    disposition="held: interrupted before the evidence ran")
        body.update(over)
        # Exactly what _serve_evidence records at intent.
        body["audit_current"] = s._final_audit_current()
        s.evidence_requests.append(body)
        return body
    # A failed proof: the finding stands, the record is consumed.
    first = record("E1", disposition="confirmed: the test failed on this source; the finding stands", stage="done")
    assert s._final_audit_current() is False, "the record moved the packet"
    s._consume_evidence(first)
    assert s._final_audit_current() is True and s._final_audit["consumed"] == ["E1"]
    assert s._final_audit["packet_audited"] == audited and s._final_audit["packet"] != audited
    assert s._final_audit["verdicts"]["R5"][0] is False
    # A passing proof reassessed MET, in the order _reassess and
    # _serve_evidence write it: the binding is stamped, then the
    # disposition and stage.
    second = record("E2", receipt=dict(status="passed", reason="1 passed", cases=[]),
                    reassessment="R5: MET - tests/test_e1.py", stage="reassessment started",
                    disposition="held: reassessment not made", key=["audit", "R5", "other"])
    second["acceptance_binding"] = s._audit_binding(None)
    s.memory.ledger.requirement_status["R5"] = "met (reassessed on evidence E2)"
    second["disposition"] = "MET on reassessment by the same auditor: tests/test_e1.py"
    second["stage"] = "done"
    s._consume_evidence(second)
    assert s._final_audit_current() is True and s._final_audit["consumed"] == ["E1", "E2"]
    assert s._final_audit["verdicts"]["R5"][0] is True
    acceptance = s._acceptance_record()
    assert acceptance["current"] is True and acceptance["consumed"] == ["E1", "E2"]
    assert acceptance["packet_audited"] == audited and acceptance["packet"] == s._packet().digest
    # Resumed at the cap: the final audit is not asked again and the
    # reassessed MET is carried over the restored status.
    before = len(prompts)
    verdicts = s._audit_requirements(final=True)
    assert len(prompts) == before and verdicts["R5"][0] is True
    s.memory.ledger.requirement_status["R5"] = "NOT MET: no console check"
    s._finish_pending_evidence()
    assert s.memory.ledger.requirement_status["R5"].startswith("met (reassessed")
    # A record the source moved under carries nothing forward: the
    # acceptance is left not current, never relabelled.
    third = record("E3", key=["audit", "R5", "third"])
    (tmp_path / "project" / "moved.txt").write_text("moved\n")
    third["disposition"] = "held: the source or checks changed after the evidence was bound"
    third["stage"] = "done"
    s._consume_evidence(third)
    assert s._final_audit_current() is False and s._final_audit["consumed"] == ["E1", "E2"]


def test_a_receipt_that_is_not_the_settled_operations_own_refuses_the_resume(tmp_path):
    """Adversarial review of e688af5: nothing cross-checked a receipt read
    back against the operation naming it. The receipt carries the
    operation's id and the row the receipt's digest; a late settled
    operation whose receipt is another operation's, or carries other bytes
    than its settlement recorded, refuses the resume on its own."""
    import hashlib
    store = jobstore.JobStore(tmp_path)
    budget = {"operations": {"started": [], "settled": []}}
    digest = hashlib.sha256(b"R1: NOT MET").hexdigest()

    def op(n, receipt_digest=None):
        row = dict(opid=f"OPcd-{n}", purpose="model call", status="settled", owner_id="cd", allowed_effects=["read"],
                   outcome="completed", receipt=f"receipt-opcd-{n}")
        if receipt_digest:
            row["receipt_digest"] = receipt_digest
        return row
    body = dict(watermark=dict(budget_settled=0, session_settled={"cd": 0}))
    store.publish({"budget": budget, "operations-cd": [op(1, digest)],
                   "receipt-opcd-1": dict(opid="OPcd-1", result="R1: NOT MET", full_digest=digest)},
                  session_unsettled=("cd", []))
    with pytest.raises(checkpoint.CheckpointRefused, match="never consumed"):
        checkpoint._unconsumed(tmp_path, body, budget)
    store.publish({"operations-cd": [op(2, digest)], "receipt-opcd-2": dict(opid="OPcd-9", result="R1: NOT MET",
                                                                           full_digest=digest)},
                  session_unsettled=("cd", []))
    with pytest.raises(checkpoint.CheckpointRefused, match="is operation OPcd-9's"):
        checkpoint._unconsumed(tmp_path, body, budget)
    store.publish({"operations-cd": [op(3, digest)], "receipt-opcd-3": dict(opid="OPcd-3", result="R1: MET",
                                                                           full_digest="0" * 64)},
                  session_unsettled=("cd", []))
    with pytest.raises(checkpoint.CheckpointRefused, match="does not carry the digest its settlement recorded"):
        checkpoint._unconsumed(tmp_path, body, budget)


def test_the_repair_route_lists_what_follows_the_repaired_gate(tmp_path, monkeypatch):
    """Adversarial review of e688af5 (route lens): the subset gate's repair
    route carried its reviews, revision and rechecks only after a lead
    recovery, because the peers were set in that branch alone and never
    cleared; the merge gate's route listed a close-out no model makes and
    not the post-merge rechecks that follow it. Each task's peers are its
    own; the merge route lists its rechecks and capped reviews; no route
    lists a close-out since every record is the harness's (2026-10-21); a
    refused repair is recorded and no editor is called."""
    from types import SimpleNamespace

    from quadratus.session import TaskSpec
    s, prompts = _session(tmp_path, "")
    spec = TaskSpec("t1", "Implement addition")
    gate = s.config.integration_gate
    s._max_tasks, s._slots_used = 4, 0
    s._route_peers = ["openai:gpt-5.6-sol", "xai:grok:default"]
    names = [r.name for r in s._gate_repair_stages(spec, gate, "subset")]
    # The reviews ahead are known; the correction they may select is
    # admitted when they do (admission/2), never costed here.
    assert names == ["gate fix", "review by openai:gpt-5.6-sol", "review by xai:grok:default"]
    assert [r.name for r in s._gate_repair_stages(spec, gate, "full")] == ["gate fix"]
    # Every repair carries the task's known remainder (root's control on
    # fef1e27): a required design review still ahead is on the fix cycle's,
    # the gate fix's and the render fix's routes; nothing is listed twice.
    # No route lists a close-out any more: every task's record is the
    # harness's and costs no call (Davis, 2026-10-21), on any slot.
    assert [r.name for r in s._fix_cycle_stages(spec, [("openai:gpt-5.6-sol", "BLOCKING: x")])] == [
        "fix revision", "recheck by openai:gpt-5.6-sol"]
    s._contract = SimpleNamespace(task_id="t1", required=SimpleNamespace(design_review=True))
    assert s._mandatory_stages(spec, ["openai:gpt-5.6-sol"]) == ["lead draft", "review by openai:gpt-5.6-sol",
                                                                  "design review"]
    assert [r.name for r in s._fix_cycle_stages(spec, [("openai:gpt-5.6-sol", "BLOCKING: x")])] == [
        "fix revision", "recheck by openai:gpt-5.6-sol", "design review"]
    assert [r.name for r in s._gate_repair_stages(spec, gate, "full")] == ["gate fix", "design review"]
    assert [r.name for r in s._gate_repair_stages(spec, gate, "subset")] == [
        "gate fix", "review by openai:gpt-5.6-sol", "review by xai:grok:default", "design review"]
    assert [r.name for r in s._design_repair_stages(spec, None)] == ["design fix", "design review"]
    assert [r.name for r in s._design_repair_stages(spec, "openai:independent")] == [
        "design fix", "design review by openai:independent"]
    assert [r.name for r in s._known_remainder(spec, after="design")] == []
    # The task the job committed to end after (admission/3) and any other
    # task close the same way, factually: repairs owe the design review and
    # the security verification still, never a close-out.
    s._terminal_route = dict(task="t1", reason="r", selected="s", slots_used=1, relinquished=3, max_tasks=4)
    assert s._closes_factually(spec) and s._closes_factually(TaskSpec("t9", "other"))
    assert [r.name for r in s._fix_cycle_stages(spec, [("openai:gpt-5.6-sol", "BLOCKING: x")])] == [
        "fix revision", "recheck by openai:gpt-5.6-sol", "design review"]
    assert [r.name for r in s._gate_repair_stages(spec, gate, "full")] == ["gate fix", "design review"]
    assert [r.name for r in s._security_repair_stages(spec, "openai:gpt-5.6-sol")] == [
        "security fix", "security verification by openai:gpt-5.6-sol"]
    assert [r.name for r in s._design_repair_stages(spec, None)] == ["design fix", "design review"]
    # One final-work boundary for the capped-source handoffs (Astra A2 on
    # 0ae5bb6): the committed task or the numeric last slot, never an
    # explicit list or a parallel child.
    assert s._final_work_slot(spec) and not s._final_work_slot(TaskSpec("t9", "other"))
    s._batch_child = True
    assert not s._final_work_slot(spec)
    s._batch_child = False
    s._explicit_tasks = []
    assert not s._final_work_slot(spec)
    s._explicit_tasks = None
    s._terminal_route = None
    assert not s._final_work_slot(spec)
    s._slots_used = 3
    assert s._final_work_slot(spec)
    assert [r.name for r in s._fix_cycle_stages(spec, [("openai:gpt-5.6-sol", "BLOCKING: x")])] == [
        "fix revision", "recheck by openai:gpt-5.6-sol", "design review"]
    s._slots_used = 0
    s._contract = None
    s._route_after_merge = dict(rechecks=["openai:gpt-5.6-sol"], capped=["t2"])
    assert [r.name for r in s._gate_repair_stages(TaskSpec("t2-merge", "merge"), gate, "full")] == [
        "gate fix", "post-merge recheck by openai:gpt-5.6-sol", "capped final review of t2"]
    s._route_after_merge = None
    assert [r.name for r in s._security_repair_stages(spec, "openai:gpt-5.6-sol")] == [
        "security fix", "security verification by openai:gpt-5.6-sol"]
    assert [r.name for r in s._design_repair_stages(spec, "openai:independent")] == [
        "design fix", "design review by openai:independent"]
    # The subset route's wall counts the full gate after the revision only
    # where it was measured; the unmeasured half is reported, not guessed.
    s._check_walls = {}
    assert s._gate_repair_wall(spec, gate, "subset") == (0.0, False)
    s._note_check_wall(gate, 2.5)
    assert s._gate_repair_wall(spec, gate, "full") == (2.5, True)
    # A refused repair route: recorded, no editor called, the failure stands.
    monkeypatch.setattr(s, "_budget", lambda: object())
    monkeypatch.setattr(s, "_admit_plan", lambda *a, **k: SimpleNamespace(feasible=False, detail="1 call short"))
    monkeypatch.setattr(s, "_edit", lambda *a, **k: (_ for _ in ()).throw(AssertionError("an editor was called")))
    record = {}
    assert s._design_fix_edit(spec, None, record, "claude:opus", "fix it") is False
    assert "repair route after the failed render cannot be paid whole" in record["fix_refused"]
    assert s.finish_refusals[-1]["what"] == "design fix" and "1 call short" in s.finish_refusals[-1]["detail"]
    assert s._admit_repair(spec, "security fix", "security-repair", [], after="security verification") is False
    assert s.finish_refusals[-1]["what"] == "security fix"
    assert "after the failed security verification" in s.finish_refusals[-1]["detail"]


def test_readers_say_when_the_committed_state_cannot_be_read(tmp_path):
    """Adversarial review of e688af5 (reader lens): a run with no export
    whose committed manifest is altered read as a plain 'unfinished' run,
    the corruption reason dropped; and the liveness line claimed a
    result.json that did not exist. Completion is still never claimed."""
    from quadratus import monitor
    root = tmp_path / "p"
    run_dir = root / ".quadratus" / "runs" / "20261018T000004Z-aaaaaaaa"
    run_dir.mkdir(parents=True)
    jobstore.JobStore(run_dir).publish({"budget": {"reserved_attempts": 1}})
    manifest = run_dir / "manifest.json"
    body = json.loads(manifest.read_text())
    body["digest"] = "0" * 64
    manifest.write_text(json.dumps(body))
    status = monitor.read_status(root)
    assert status["finished"] is False and status["terminal_status"] == monitor.UNKNOWN
    assert "committed state is corrupt" in status["unknown"]["terminal_status"]
    row = monitor._history_row(run_dir)
    assert row["status"] == monitor.UNKNOWN and "committed state is corrupt" in row["stop_reason"]
    other = tmp_path / "q"
    run_dir = other / ".quadratus" / "runs" / "20261018T000005Z-bbbbbbbb"
    run_dir.mkdir(parents=True)
    store = jobstore.JobStore(run_dir)
    assert checkpoint.write(run_dir, dict(version=checkpoint.VERSION, lineage={}, sealed=False, resumable=True,
                                          uncarried=[], consumed=None, identities={}, state={}, written="now"),
                            store=store, budget_snapshot={"reserved_attempts": 3})
    terminal = dict(completed=False, error="", interrupted=False, goal="g", explicit_tasks=None, tasks=[],
                    budget_stop_reason="wall_deadline", acceptance=None)
    assert checkpoint.seal(run_dir, "incomplete", store=store, extra={"terminal": terminal})
    status = monitor.read_status(other)
    assert status["finished"] is True and status["terminal_status"] == "incomplete"
    assert status["stop_reason"] == "budget: wall_deadline"
    assert status["liveness"] == "latest run committed its terminal state; it is over"
    # A corrupt committed state beside an unreadable export: both named.
    both = tmp_path / "r"
    run_dir = both / ".quadratus" / "runs" / "20261018T000006Z-cccccccc"
    run_dir.mkdir(parents=True)
    jobstore.JobStore(run_dir).publish({"budget": {"reserved_attempts": 1}})
    (run_dir / "manifest.json").write_text('{"records": {')
    (run_dir / "result.json").write_text('{"completed":')
    status = monitor.read_status(both)
    assert status["terminal_status"] == monitor.UNKNOWN
    assert "result.json unreadable" in status["unknown"]["terminal_status"]
    assert "committed state is corrupt" in status["unknown"]["terminal_status"]
    row = monitor._history_row(run_dir)
    assert row["status"] == monitor.UNKNOWN and "committed state is corrupt" in row["stop_reason"]


def test_a_settlement_is_committed_only_with_its_receipt_whatever_another_operation_publishes(tmp_path, monkeypatch):
    """Adversarial review of e688af5 (concurrency lens): an operation's row
    turned 'settled' outside the operation lock, so a sibling's settlement
    published under the lock carried it as settled in a generation holding
    no receipt for it; a crash on its own generation then left the record
    saying settled, naming a receipt nothing held, flagged by nothing. The
    row settles under the lock with its receipt, so no generation ever says
    settled without the receipt, and a crashed settlement stays unsettled."""
    import threading
    import time

    from quadratus.artifacts import ArtifactStore
    from quadratus.session import JobStoreUnavailable, SessionConfig
    run_dir = tmp_path / "run"
    fail_next = {"on": False}

    def fault(stage):
        if fail_next["on"] and stage == "record-write":
            raise OSError("disk gone")
    store = jobstore.JobStore(run_dir, fault=fault)
    s = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(job_store=store))
    a_inside, b_in_lock = threading.Event(), threading.Event()
    original_receipt = s._receipt

    def receipt(record, payload):
        if record["purpose"] == "B":
            b_in_lock.set()
            time.sleep(0.2)   # A reaches its settlement while B holds the lock
        return original_receipt(record, payload)
    s._receipt = receipt
    seen = []
    original_publish = jobstore.JobStore.publish

    def publish(self, updates, **kw):
        current = self.current()
        committed = dict(current.records) if current else {}
        for name, rows in updates.items():
            if name.startswith("operations-"):
                for op in rows:
                    if op["purpose"] == "A":
                        seen.append((op["status"], op["receipt"] in updates or op["receipt"] in committed))
                        if op["receipt"] in updates:
                            fail_next["on"] = True   # A's own settlement generation: crash it
        return original_publish(self, updates, **kw)
    monkeypatch.setattr(jobstore.JobStore, "publish", publish)
    errors = {}

    def a():
        try:
            with s._operation("A", inputs=dict(n=1), effects=("read",)) as op:
                a_inside.set()
                assert b_in_lock.wait(10)
                op["result"] = "reply A"
        except BaseException as exc:  # noqa: BLE001 -- inspected below
            errors["A"] = exc

    def b():
        assert a_inside.wait(10)
        with s._operation("B", inputs=dict(n=2), effects=("read",)) as op:
            op["result"] = "reply B"
    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    for thread in (ta, tb):
        thread.start()
    for thread in (ta, tb):
        thread.join(30)
    assert seen and not any(status == "settled" and not readable for status, readable in seen), seen
    assert isinstance(errors.get("A"), JobStoreUnavailable)
    a_op = next(o for o in s.operations if o["purpose"] == "A")
    committed_a = next(o for rows in jobstore.operation_records(run_dir).values() for o in rows
                       if o["purpose"] == "A")
    assert committed_a["status"] == "started" and jobstore.read_record(run_dir, a_op["receipt"]) is None
    assert a_op["opid"] in jobstore.unsettled_operations(run_dir)


def test_a_settlement_refused_over_a_committed_receipt_name_is_uncertain_and_keeps_the_harness_outcome(tmp_path):
    """Adversarial review of e688af5 (concurrency lens): the receipt-name
    refusal left the row 'settled' in memory, so the owner's next publish
    committed it settled against another operation's bytes, and the
    refusal replaced the harness's own failure. The row is committed
    uncertain and unsettled, the foreign bytes stay untouched, and a
    reported failure keeps its own exception."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import JobStoreUnavailable, SessionConfig, TaskFailed
    run_dir = tmp_path / "run"
    store = jobstore.JobStore(run_dir)
    s = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(job_store=store))
    owner = s._operation_owner
    foreign = dict(opid="other", result="OTHER BYTES")
    store.publish({f"receipt-op{owner}-1": foreign, f"receipt-op{owner}-2": foreign})
    with pytest.raises(JobStoreUnavailable, match="already committed"):
        with s._operation("first", inputs=dict(n=1), effects=("read",)) as op:
            op["result"] = "mine"
    with pytest.raises(TaskFailed):
        with s._operation("second", inputs=dict(n=2), effects=("read",)) as op:
            op["result"] = "partial"
            raise TaskFailed("scope", "the harness's own outcome")
    rows = [o for rows in jobstore.operation_records(run_dir).values() for o in rows]
    assert [(o["status"], o["receipt_digest"]) for o in rows] == [("uncertain", None), ("uncertain", None)]
    assert all("already committed" in o["note"] for o in rows)
    assert set(jobstore.unsettled_operations(run_dir)) == {o["opid"] for o in rows}
    assert jobstore.read_record(run_dir, f"receipt-op{owner}-1") == foreign
    assert jobstore.read_record(run_dir, f"receipt-op{owner}-2") == foreign


def test_an_evidence_intent_a_resumed_job_never_runs_is_closed_and_consumed(tmp_path):
    """Adversarial review of e688af5 (critic): a record checkpointed at
    stage 'intent' and never run (the interrupt fell before the evidence
    run started) stayed in the packet unconsumed, so the acceptance on
    record was never current again and the final audit was asked a second
    time on resume. The intent is closed as it stands and consumed."""
    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    s._audit_requirements(final=True)
    identity = s._evidence_identity()
    record = dict(id="E1", target="R5", kind="audit", reviewer="openai:gpt-5.6-sol", task=None,
                  finding="R5: NOT MET - no console check", original_disposition="NOT MET", claim="",
                  file="tests/test_e1.py", file_sha256="x", source=s._source_fingerprint(), identity=identity,
                  key=["audit", "R5", identity], receipt=None, execution=None, reassessment=None, stage="intent",
                  test="def test_e1(): pass\n", disposition="held: interrupted before the evidence ran",
                  audit_current=s._final_audit_current())
    s.evidence_requests.append(record)
    assert s._final_audit_current() is False
    before = len(prompts)
    s._finish_pending_evidence()
    assert record["stage"] == "done" and record["disposition"].startswith("held: interrupted")
    assert s._final_audit_current() is True and s._final_audit["consumed"] == ["E1"]
    assert s._audit_requirements(final=True)["R5"][0] is False and len(prompts) == before


def test_a_failed_routing_decisions_receipts_keep_why_the_call_failed(tmp_path, monkeypatch):
    """Adversarial review of e688af5 (critic): a Jev transport failure was
    followed by the budget's own stop on unknown usage, which replaced the
    failure, so no committed receipt said why the call failed. The budget
    receipt's reply names the failure, the stop names it as its cause, and
    a session operation's failure receipt keeps that cause."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.decisions import JevDecider, describe
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
    from quadratus.session import JobStoreUnavailable, SessionConfig
    run_dir = tmp_path / "run"
    store = jobstore.JobStore(run_dir)
    budget = RunBudget(RunLimits(max_calls=5, max_reported_tokens=100_000, wall_seconds=600),
                       path=tmp_path / "b.json", store=store)
    decider = JevDecider(meter=None, budget=budget)
    decider.api_key = "fake"
    monkeypatch.setattr(JevDecider, "available", lambda self: "")

    class Offline:
        def system_one(self, **kw):
            raise RuntimeError("offline: connection refused")
    decider._client = Offline()
    with pytest.raises(RunBudgetExceeded, match="unknown_usage") as stop:
        decider.decide(describe("task.kind", ("backend", "frontend"), context="x"))
    assert isinstance(stop.value.__cause__, RuntimeError)
    settled = jobstore.read_budget(run_dir)[0]["operations"]["settled"]
    assert len(settled) == 1 and settled[0]["identity"]["role"] == "decider"
    receipt = jobstore.read_record(run_dir, settled[0]["receipt"])
    assert "offline: connection refused" in json.loads(receipt["reply"])["failure"]
    s = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(job_store=store))
    with pytest.raises(JobStoreUnavailable):
        with s._operation("routing decision", inputs=dict(n=1), effects=("read",)):
            raise JobStoreUnavailable("stopped") from RuntimeError("offline: connection refused")
    op = s.operations[-1]
    failure = json.loads(jobstore.read_record(run_dir, op["receipt"])["result"])
    assert failure["failure"].startswith("JobStoreUnavailable") and "offline: connection refused" in failure["cause"]


# --- root's Mac receipt on bc421f8: the challenge's own proof, and pending evidence at the cap ---

def test_the_harness_run_proof_of_a_challenge_is_an_executed_case_for_that_requirement_alone(tmp_path):
    """Root's Mac receipt on bc421f8: an error-clause MET must cite a case
    shown executed, and the proof the harness ran for the challenge was not
    one of the routes, so no error-clause requirement could be established
    by the challenge the cap offers. The proof's passed case counts for the
    requirement it was made for, on the source and checks it was bound to;
    another target, a failed run, another identity, another source or a
    case that did not pass counts nothing, and the file alone resolves no
    citation."""
    s, prompts = _session(tmp_path, "")
    identity = s._evidence_identity()
    case = "tests/test_evidence_negative.py::test_negative_sum"

    def record(**over):
        body = dict(id="E1", target="R1", kind="audit", reviewer="openai:gpt-5.6-sol", stage="done",
                    receipt=dict(status="passed", reason="1 passed", cases=[f"{case}: passed"]),
                    source=s._source_fingerprint(), identity=identity, disposition="held")
        body.update(over)
        return body
    s.evidence_requests = [record()]
    assert s._executed_cases([case], rid="R1") == [case]
    assert s._executed_cases([case]) == [], "the route needs the requirement the proof was made for"
    assert s._executed_cases([case], rid="R5") == []
    for over in (dict(receipt=dict(status="failed", reason="1 failed", cases=[f"{case}: failed"])),
                 dict(identity="other"), dict(source="other"),
                 dict(receipt=dict(status="passed", reason="", cases=[f"{case}: skipped"]))):
        s.evidence_requests = [record(**over)]
        assert s._executed_cases([case], rid="R1") == [], over
    s.evidence_requests = [record()]
    assert s._executed_cases(["tests/test_evidence_negative.py::test_other"], rid="R1") == []


def test_pending_evidence_is_reconciled_on_the_final_path_before_coverage_is_read(tmp_path):
    """Root on 637a7e2: an evidence record a previous episode left pending
    at the cap moved the packet, so the resumed final path skipped the
    status restore and stopped on 'never covered' before the challenge
    could finish it. The final path now reconciles carried pending evidence
    first (finished once where the record allows, never replayed): an
    intent never run is closed and consumed, the acceptance on record is
    current again, the restore happens and the final audit is not drawn
    again."""
    s, prompts = _session(tmp_path, f"R1: MET - {UNIT}\nR5: NOT MET - no console check\n")
    s._audit_requirements(final=True)
    s._final_audit["status_before"] = {"R5": "covered by t1"}
    identity = s._evidence_identity()
    record = dict(id="E1", target="R5", kind="audit", reviewer="openai:gpt-5.6-sol", task=None,
                  finding="R5: NOT MET - no console check", original_disposition="NOT MET", claim="",
                  file="tests/test_e1.py", file_sha256="x", source=s._source_fingerprint(), identity=identity,
                  key=["audit", "R5", identity], receipt=None, execution=None, reassessment=None, stage="intent",
                  test="def test_e1(): pass\n", disposition="held: interrupted before the evidence ran",
                  audit_current=s._final_audit_current())
    s.evidence_requests.append(record)
    s.memory.ledger.requirement_status["R5"] = "NOT MET: no console check"
    assert s._final_audit_current() is False
    before = len(prompts)
    s._final_check = True
    s._requirements_satisfied(final=True)
    assert record["stage"] == "done" and s._final_audit["consumed"] == ["E1"]
    assert s._final_audit_current() is True
    assert not any("independent auditor" in p for p in prompts[before:]), "the final audit is not drawn again"


def test_the_admission_note_names_the_candidate_the_record_holds(tmp_path):
    """Astra A1 on 0ae5bb6: the obligation view said a task 'was not
    started' whenever its last route admission was infeasible, including
    when its terminal route then ran and when only a later correction was
    refused. The note now follows the record: NOT RUN only when
    ``not_started`` says so; the terminal choice named with its spent and
    relinquished slots and never as execution; a refused correction named
    as a correction after the task had started."""
    s, _prompts = _session(tmp_path, "")
    full = dict(task="t1", scope="full", feasible=False, detail="short by tokens 220")
    assert s._admission_notes() == []
    s.plan_admissions = [full]
    assert s._admission_notes() == ["Plan admission for t1: its selected route could not be paid from what is "
                                    "left after the held finish (short by tokens 220)."]
    s.not_started = [dict(task="t1")]
    assert s._admission_notes()[0].endswith("so it was not started (short by tokens 220).")
    s.not_started = []
    s.plan_admissions = [full, dict(task="t1", scope="terminal", feasible=True, detail="2 call(s)")]
    assert s._admission_notes() == [], "a terminal admission with no commitment on record says nothing"
    s._terminal_route = dict(task="t1", reason="its ordinary route could not be paid: short by tokens 220",
                             selected="2 call(s)", slots_used=1, relinquished=2, max_tasks=3)
    note = s._admission_notes()[0]
    assert "its terminal route (the same stages, closed factually) was admitted" in note
    assert "1 of 3 slot(s) spent, 2 relinquished" in note and "Admission is not execution" in note
    assert "not started" not in note
    s.plan_admissions.append(dict(task="t1", scope="fix-cycle", feasible=False, detail="short by calls 2"))
    note = s._admission_notes()[0]
    assert note.startswith("Plan admission for t1: a correction its review selected could not be paid")
    assert "the task had started" in note and "stay open as written" in note and "not started" not in note
    s.plan_admissions.append(dict(task="t1", scope="fix-cycle", feasible=True, detail="2 call(s)"))
    assert s._admission_notes() == []
