"""Grouped quality/completion batch (Davis on #73, 2026-10-31), at the pure
and unit level. Three shared causes read out of the P12 terminal
(evidence-selector-01-ticket-2ac2f85):

1. Execution debt was judged as acceptance evidence. A lead's NOT RUN
   report and a check's skipped cases are records of what did not run,
   settled by the harness's own execution of exactly the named cases; the
   engine put executed rows to the auditor for sufficiency and lowered six
   METs on its INSUFFICIENT. Now every obligation carries a ``purpose``
   read fail-closed from the record, the predicate settles execution debt
   by execution alone, the prompts ask for no judgment of it, a line about
   it is recorded and applied to nothing, and the packet identity carries
   the purpose.
2. The goal's contract was read through a library's leniency, an argument
   parser's exit, a documentation exception and an error path. One compact
   rule now reaches the planner, the lead, every editing call, every review
   and the audit and reassessments through the prompt routes they already
   have.
3. A slot was spent at the top of the loop, so a refused declaration and a
   DONE sent back each cost a task; the capped-revision handoff applied in
   the last slot only. Slots are charged at dispatch, uncharged events are
   on a carried ledger, and the handoff applies in any serial slot.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quadratus import checkpoint
from quadratus import delivery as D
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig, TaskScope, TaskSpec
from tests.test_delivery_records import SRC_A, _obligation, _receipt
from tests.test_unexecuted_acceptance import SCENARIO, UNIT, _identity, _not_run, _session

CURRENT = {"R8": 1, "R9": 1}


# --- family 1: purpose, read fail closed ------------------------------------------------

@pytest.mark.parametrize("record, purpose", [
    (dict(kind="not_run"), "execution"),
    (dict(kind="skipped"), "execution"),
    (dict(kind="not_run", purpose="execution"), "execution"),
    (dict(kind="not_run", modality="test"), "execution"),
    (dict(kind="acceptance"), "acceptance"),
    (dict(kind="acceptance", purpose="acceptance"), "acceptance"),
    # Mixed or forged: a declared purpose that disagrees with the kind, or
    # a debt row carrying a declared scenario: unknown, held, never credited.
    (dict(kind="not_run", purpose="acceptance"), "unknown"),
    (dict(kind="acceptance", purpose="execution"), "unknown"),
    (dict(kind="not_run", purpose="other"), "unknown"),
    (dict(kind="acceptance", purpose="other"), "unknown"),
    (dict(kind="not_run", proof_id="P1"), "unknown"),
    (dict(kind="skipped", scenario=[["proof", "P1"]]), "unknown"),
    # A non-test modality is never execution debt, whatever the kind says.
    (dict(kind="not_run", modality="reading"), "unknown"),
    (dict(kind="skipped", modality="capture"), "unknown"),
    # No kind, an unknown kind: never settled by execution alone.
    (dict(), "unknown"),
    (dict(purpose="execution"), "unknown"),
    (dict(kind="whatever"), "unknown"),
    (dict(kind="acceptance", modality="reading"), "acceptance"),
])
def test_purpose_is_read_from_the_whole_record_and_fails_closed(record, purpose):
    assert D.purpose_of(record) == purpose


def test_an_execution_purpose_needs_a_debt_kind_a_test_modality_and_no_scenario():
    with pytest.raises(ValueError, match="purpose"):
        _obligation(purpose="other")
    with pytest.raises(ValueError, match="execution debt"):
        _obligation(purpose="execution", kind="not_run", modality="reading", runner=None, cases=())
    with pytest.raises(ValueError, match="execution debt"):
        _obligation(purpose="execution", kind="acceptance")
    with pytest.raises(ValueError, match="execution debt"):
        _obligation(purpose="execution", kind="not_run", scenario=(("proof", "P1"),))
    with pytest.raises(ValueError, match="kind"):
        _obligation(kind="whatever")
    assert _obligation(purpose="execution", kind="not_run").purpose == "execution"
    assert _obligation(purpose="unknown").purpose == "unknown", "a typed row may carry the fail-closed reading"


def test_from_dict_reads_the_purpose_from_the_record_and_round_trips_it():
    legacy = _obligation(kind="not_run").to_dict()
    legacy.pop("purpose")
    assert D.EvidenceObligation.from_dict(legacy).purpose == "execution", "a record written before the field"
    forged = dict(_obligation(kind="not_run").to_dict(), purpose="acceptance")
    assert D.EvidenceObligation.from_dict(forged).purpose == "unknown", "a declared contradiction is never credited"
    foreign = dict(_obligation().to_dict(), kind="whatever")
    assert D.EvidenceObligation.from_dict(foreign).kind == "unknown" and D.EvidenceObligation.from_dict(foreign).purpose == "unknown"
    with pytest.raises(ValueError, match="unknown kind"):
        _obligation(kind="unknown")
    assert D.purpose_of(D.EvidenceObligation.from_dict(foreign).to_dict()) == "unknown", "the typed reading round-trips"
    typed = _obligation(kind="skipped", purpose="execution")
    assert D.EvidenceObligation.from_dict(typed.to_dict()) == typed
    assert typed.to_dict()["purpose"] == "execution"


def test_the_evidence_binding_moves_with_the_purpose():
    debt = _obligation(kind="not_run", purpose="execution")
    claim = _obligation(kind="not_run", purpose="acceptance")
    assert debt.evidence_binding != claim.evidence_binding


def test_execution_debt_is_established_by_execution_alone_and_acceptance_is_not():
    debt = D.discharge(_obligation(kind="not_run", purpose="execution"), _receipt(), current_source=SRC_A,
                       current_requirements=CURRENT).obligation
    assert debt.execution == "ran" and debt.sufficiency == "unjudged" and debt.established
    claim = D.discharge(_obligation(), _receipt(), current_source=SRC_A, current_requirements=CURRENT).obligation
    assert claim.execution == "ran" and not claim.established, "acceptance evidence still needs a judgment"
    assert not _obligation(kind="not_run", purpose="execution").established, "unexecuted debt holds"
    unknown = D.discharge(_obligation(kind="not_run", purpose="unknown"), _receipt(), current_source=SRC_A,
                          current_requirements=CURRENT).obligation
    assert unknown.execution == "ran" and not unknown.established, "an unknown purpose is never credited"
    for verdict in ("sufficient", "insufficient"):
        with pytest.raises(ValueError, match="not judged"):
            D.judge_sufficiency(debt, verdict, judged_by="openai:sol")
        with pytest.raises(ValueError, match="not judged"):
            D.judge_sufficiency(unknown, verdict, judged_by="openai:sol")


def _debt(**over):
    base = dict(oid="O1", kind="not_run", requirements=[["R1", 1]], runner="gate", execution="ran",
                sufficiency="unjudged", sufficiency_current=False, reason="")
    base.update(over)
    return base


@pytest.mark.parametrize("row, holds, reason", [
    (dict(), False, ""),
    (dict(sufficiency="insufficient"), False, ""),
    (dict(kind="skipped", sufficiency="insufficient"), False, ""),
    (dict(execution="pending"), True, "O1: execution pending"),
    (dict(execution="missing"), True, "O1: execution missing"),
    (dict(execution="stale", reason="executed on another source"), True, "execution stale (executed on another"),
    (dict(execution="failed"), True, "O1: execution failed"),
    (dict(execution="skipped"), True, "O1: execution skipped"),
    (dict(execution="invalid"), True, "O1: execution invalid"),
    (dict(runner=None, execution="missing"), True, "no authorized runner (prerequisite)"),
    # A declared purpose that contradicts the kind, or a debt row with a
    # scenario or a non-test modality, is unknown: held, never credited.
    (dict(purpose="acceptance", sufficiency="sufficient", sufficiency_current=True), True, "purpose unknown"),
    (dict(purpose="acceptance"), True, "purpose unknown"),
    (dict(modality="reading"), True, "purpose unknown"),
    (dict(proof_id="P1"), True, "purpose unknown"),
])
def test_the_predicate_settles_execution_debt_by_execution_and_holds_everything_else(row, holds, reason):
    out = D.acceptance({"R1": (True, "app.py"), "R2": (True, "README.md")}, [_debt(**row)], [], {})
    assert out["R1"].met is (not holds), out["R1"].reasons
    if holds:
        assert any(reason in r for r in out["R1"].reasons), out["R1"].reasons
    assert out["R2"].met, "a row lowers only the requirements it binds"


def test_executed_debt_still_holds_a_requirement_whose_revision_moved_or_whose_verdict_is_not_met():
    out = D.acceptance({"R1": (True, "app.py")}, [_debt()], [], {}, current_requirements={"R1": 2})
    assert not out["R1"].met and "bound requirement revision changed" in out["R1"].reasons[0]
    out = D.acceptance({"R1": (False, "the error path is not shown")}, [_debt()], [], {})
    assert not out["R1"].met and out["R1"].reasons == ("verdict: the error path is not shown",)
    superseded = D.acceptance({"R1": (True, "app.py")}, [_debt(execution="pending", superseded_by="O2"),
                                                          _debt(oid="O2")], [], {})
    assert superseded["R1"].met, "a superseded row is judged in its successor's place"


# --- family 1 in the session: registration, packet, prompts, judging ----------------------

def _gate_check(session):
    return dict(passed=True, command="gate suite", source=session._source_fingerprint(), **_identity(session),
                receipts=[dict(id="extra-2", status="passed", output="# pass 3\n# fail 0\n# skipped 0\n",
                               command=f"node {SCENARIO}")])


def test_a_not_run_report_registers_execution_debt_and_a_declared_behavior_registers_acceptance(tmp_path):
    session, _ = _session(tmp_path, "")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    (debt,) = session.evidence_obligations
    assert debt["kind"] == "not_run" and debt["purpose"] == "execution" and D.purpose_of(debt) == "execution"
    row = session._register_obligation(dict(task="t1", requirements=["R1"], behavior="typing filters the cards"),
                                       kind="acceptance")
    assert row["kind"] == "acceptance" and row["purpose"] == "acceptance"
    skipped = session._register_obligation(dict(task="t1", requirements=["R1"], item="skipped: test_x",
                                                cases=["test_x"]), kind="skipped")
    assert skipped["purpose"] == "execution", "skip facts are execution debt, symmetric with NOT RUN"


def test_the_packet_identity_moves_with_a_rows_purpose_and_kind(tmp_path):
    session, _ = _session(tmp_path, "")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    before = session._packet().digest
    session.evidence_obligations[0]["purpose"] = "acceptance"
    changed = session._packet().digest
    assert changed != before, "a cached audit is not reused after a purpose change"
    session.evidence_obligations[0]["purpose"] = "execution"
    assert session._packet().digest == before
    session.evidence_obligations[0]["kind"] = "acceptance"
    assert session._packet().digest not in (before, changed)


def test_the_obligations_block_lists_debt_under_its_own_heading_and_asks_no_judgment_of_it(tmp_path):
    session, _ = _session(tmp_path, "")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    session.checks.append(_gate_check(session))
    session._refresh_obligations()
    assert session.evidence_obligations[0]["execution"] == "ran"
    block = session._obligations_block(list(session.evidence_obligations))
    assert "## Execution debt" in block and "judge nothing about these rows" in block
    assert "- O1 (t3; R5): " in block and "; settled" in block
    assert "## Evidence obligations" not in block and "SUFFICIENT" not in block
    session._register_obligation(dict(task="t1", requirements=["R1"], behavior="typing filters the cards"),
                                 kind="acceptance")
    both = session._obligations_block(list(session.evidence_obligations))
    assert both.index("## Execution debt") < both.index("## Evidence obligations")
    assert "- O2 (t1; R1): typing filters the cards" in both


def test_the_audit_prompt_asks_for_no_sufficiency_line_on_executed_debt_and_the_verdict_stands(tmp_path):
    """The P12 shape: the covering task's NOT RUN items ran at a later check
    and the auditor wrote MET with an INSUFFICIENT line about the rows.
    The line is recorded, applied to nothing, and the requirement is met."""
    reply = f"R5: MET - {UNIT}\nO1: INSUFFICIENT the executed case does not show the console check\n"
    session, prompts = _session(tmp_path, reply)
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    session.checks.append(_gate_check(session))
    verdicts = session._audit_requirements(ids=["R5"])
    assert verdicts["R5"][0] is True, verdicts
    prompt = prompts[0]
    assert "## Execution debt" in prompt and "Each executed acceptance obligation" not in prompt
    (row,) = session.evidence_obligations
    assert row["sufficiency"] == "unjudged" and row["sufficiency_note"].startswith("O1: INSUFFICIENT by openai:sol not applied")
    assert session.requirement_audits[-1]["lowered"] == []


def test_unexecuted_debt_still_lowers_the_verdict_and_a_sufficient_line_credits_nothing(tmp_path):
    reply = f"R5: MET - {UNIT}\nO1: SUFFICIENT the scenario is clearly covered\n"
    session, _ = _session(tmp_path, reply)
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    verdicts = session._audit_requirements(ids=["R5"])
    assert verdicts["R5"][0] is False and "NOT RUN" in verdicts["R5"][1]
    (row,) = session.evidence_obligations
    assert row["execution"] != "ran" and row["sufficiency"] == "unjudged"


def test_a_mixed_record_is_held_and_never_credited(tmp_path):
    """A record whose declared purpose contradicts its kind is unknown: a
    seat's line about it is a note, its execution settles nothing, and the
    predicate holds every requirement it binds."""
    session, _ = _session(tmp_path, "")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    session.checks.append(_gate_check(session))
    session._refresh_obligations()
    session.evidence_obligations[0]["purpose"] = "acceptance"
    assert D.purpose_of(session.evidence_obligations[0]) == "unknown"
    session._judge_obligations("openai:sol", "O1: SUFFICIENT clearly covered", {"R5": (True, UNIT)})
    row = session.evidence_obligations[0]
    assert row["sufficiency"] == "unjudged" and "unknown purpose is held" in row["sufficiency_note"]
    found = {"R5": (True, UNIT)}
    lowered = session._apply_acceptance(found, ["R5"])
    assert found["R5"][0] is False and "O1: purpose unknown" in lowered[0]["reason"]
    block = session._obligations_block(list(session.evidence_obligations))
    assert "UNKNOWN PURPOSE (mixed record; held, never credited)" in block and "; held" in block
    assert session._evidence_route("R5")["kind"] == "execution debt"


def test_the_checkpoint_carries_the_purpose_and_the_obligations_view_names_debt(tmp_path):
    session, _ = _session(tmp_path, "")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    state = checkpoint.capture_state(session)
    fresh = Session("goal", ArtifactStore(tmp_path / "b"), lambda *a, **k: "", config=SessionConfig())
    checkpoint.restore_state(fresh, state)
    assert fresh.evidence_obligations[0]["purpose"] == "execution"
    legacy = dict(state)
    legacy["evidence_obligations"] = [{k: v for k, v in r.items() if k != "purpose"} for r in state["evidence_obligations"]]
    older = Session("goal", ArtifactStore(tmp_path / "c"), lambda *a, **k: "", config=SessionConfig())
    checkpoint.restore_state(older, legacy)
    assert D.purpose_of(older.evidence_obligations[0]) == "execution", "read by kind when the field is absent"


# --- family 1 in the independent checker ----------------------------------------------------

def _record(lowered_reason, row):
    return dict(requirements=dict(audits=[dict(verdicts={"R18": dict(met=False)},
                                               lowered=[dict(requirement="R18", reason=lowered_reason)])]),
                evidence_obligations=[row])


def test_the_checker_names_a_lowering_on_executed_debt_from_the_record_alone():
    from tools import truth_check as T
    row = dict(oid="O49", kind="not_run", execution="ran", runner="gate")
    cite = "acceptance predicate: O49: sufficiency insufficient; O50: sufficiency insufficient"
    assert T._debt_lowering(dict(requirement="R18", reason=cite), _record(cite, row)) == ["O49"]
    # A sufficiency cited on a row of unknown purpose is the same self-disagreement.
    for unknown in (dict(row, purpose="acceptance"), dict(row, modality="reading")):
        assert T._debt_lowering(dict(requirement="R18", reason=cite), _record(cite, unknown)) == ["O49"], unknown
    for other in (dict(row, execution="pending"), dict(row, kind="acceptance"), dict(row, runner=None),
                  dict(row, superseded_by="O51")):
        assert T._debt_lowering(dict(requirement="R18", reason=cite), _record(cite, other)) == [], other
    assert T._debt_lowering(dict(requirement="R18", reason="verdict: not shown"), _record(cite, row)) == []
    # A requirement marked met over unsettled debt or an unknown row is unshown.
    met = dict(requirements=dict(status={"R1": "met (audited)", "R2": "NOT MET: x"}),
               unexecuted_acceptance=[dict(task="t1", item="tests/test_app.py::test_add")],
               evidence_obligations=[
        dict(oid="O1", kind="not_run", execution="pending", runner="gate", requirements=[["R1", 1]]),
        dict(oid="O2", kind="not_run", execution="ran", runner=None, requirements=[["R1", 1]]),
        dict(oid="O3", kind="not_run", task="t1", execution="ran", runner="gate", requirements=[["R1", 1]],
             entry=dict(item="tests/test_app.py::test_add")),
        dict(oid="O4", kind="acceptance", purpose="execution", execution="ran", runner="gate", requirements=[["R1", 1]]),
        dict(oid="O5", kind="not_run", execution="pending", runner="gate", requirements=[["R2", 1]]),
        dict(oid="O6", kind="not_run", execution="pending", runner="gate", requirements=[["R1", 1]], superseded_by="O3"),
        dict(oid="O7", kind="not_run", task="t2", execution="ran", runner="gate", requirements=[["R1", 1]],
             behavior="tests/test_x.py::test_y")])
    assert T._met_over_debt(met) == [
        "R1 is met while execution-debt row O1 reads execution pending",
        "R1 is met while execution-debt row O2 reads execution ran with no authorized runner",
        # O3's entry exists but no recorded check clears it: the engine's
        # "ran" is its own word (root's correction on 0c5c6f1, E1).
        "R1 is met on execution-debt row O3 that reads ran while the record does not show its entry cleared "
        "under the row's requirement revisions, gate, environment and the final source",
        "R1 is met while obligation O4 has an unknown purpose",
        "R1 is met on execution-debt row O7 that no NOT RUN or skip entry backs"]


def test_the_checker_reports_the_lowering_as_unshown_never_as_met(tmp_path):
    from tools import truth_check as T
    row = dict(oid="O49", kind="not_run", execution="ran", runner="gate")
    cite = "acceptance predicate: O49: sufficiency insufficient"
    record = dict(completed=False, error="RequirementsUnmet", checks=[], **_record(cite, row))
    record["requirements"]["listed"] = {"R18": "the error path"}
    record["requirements"]["status"] = {"R18": "NOT MET: acceptance predicate"}
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps(record))
    out = T.judge(run)
    assert out["verdict"] == T.NOT_MET, "the record's own NOT MET stands"
    assert any("lowered on the judged sufficiency of O49" in line for line in out["unverified"])


# --- family 2: one contract rule on every prompt route ----------------------------------------

RULE = S._CONTRACT_BOUNDARY_RULE


def test_the_rule_states_the_four_closed_readings_and_the_open_one():
    for phrase in ("a library's leniency is not the goal's language", "NaN", "Infinity",
                   "parsing and validation that run before the handler or dispatcher",
                   "argument parser's usage exit", "leaves no change behind",
                   "reading the state after the refusal", "documentation describes the goal's contract and never narrows it",
                   "name the ambiguity"):
        assert phrase in RULE, phrase
    assert len(RULE) < 1200, "one compact rule, not a second long paragraph"


def _spec():
    return TaskSpec(task_id="t1", description="Implement add in app.py.", kind="backend", complexity="simple",
                    lead="claude:opus", scope=None)


def test_the_rule_reaches_the_lead_writer_review_and_audit_builders(tmp_path):
    session, _ = _session(tmp_path, "")
    assert RULE in session._lead_prompt(_spec())
    assert RULE in session._writer_goal()
    assert RULE in session._goal_authority()
    assert RULE in session._authority_context({"R1": "typing filters the cards"})
    assert session._lead_prompt(_spec()).count(RULE) == 1, "stated once per prompt"


def test_the_rule_reaches_the_planner_prompt(tmp_path):
    session, prompts = _session(tmp_path, "DONE")
    session.next_task()
    assert prompts and RULE in prompts[0]


# --- family 3: slot accounting and the capped handoff's eligibility ----------------------------

def _bare(tmp_path, **config):
    return Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(**config))


def test_a_slot_event_records_the_count_as_it_stands_and_is_carried(tmp_path):
    session = _bare(tmp_path)
    session._max_tasks = 4
    session._slot_event("sent_back", ["t1"], 0, why="no COVERS line")
    session._slot_event("charged", ["t1"], 1, how="serial")
    session._slot_event("done_sent_back", [], 1, why="R2 not met")
    session._slot_event("charged", ["t2", "t3"], 3, how="batch")
    session._slot_event("not_started", ["t4"], 3, why="cannot pay its route")
    assert [(e["event"], e["slots_used"]) for e in session.slot_ledger] == [
        ("sent_back", 0), ("charged", 1), ("done_sent_back", 1), ("charged", 3), ("not_started", 3)]
    assert session.slot_ledger[3] == dict(event="charged", tasks=["t2", "t3"], slots_used=3, max_tasks=4, how="batch")
    assert session.slot_ledger[0]["why"] == "no COVERS line"
    state = checkpoint.capture_state(session)
    fresh = _bare(tmp_path / "f")
    checkpoint.restore_state(fresh, state)
    assert fresh.slot_ledger == session.slot_ledger
    state.pop("slot_ledger")
    older = _bare(tmp_path / "o")
    checkpoint.restore_state(older, state)
    assert older.slot_ledger == [], "a checkpoint written before the ledger resumes with none"


def test_the_planner_is_told_what_spends_a_slot(tmp_path):
    session = _bare(tmp_path, requirements_ledger=True, max_requirement_reopens=3)
    session._max_tasks, session._slots_used = 5, 2
    note = session._slots_note()
    assert "Task slots: 3 of 5 remain for this job, counting the task you name now." in note
    assert "A slot is spent when a task is dispatched" in note
    assert "spends no slot and counts against the correction allowance of 3" in note
    assert "a DONE sent back spends no slot and is sent back at most 3 times" in note


def test_the_checked_source_eligibility_is_serial_planner_work_only(tmp_path, monkeypatch):
    session = _bare(tmp_path)
    reached = []
    monkeypatch.setattr(session, "_write_ceiling", lambda: reached.append("ceiling") or False)
    spec = _spec()
    session._explicit_tasks = []
    assert session._checked_source(spec, {}, final_only=False) is None
    session._explicit_tasks = None
    session._batch_child = True
    assert session._checked_source(spec, {}, final_only=False) is None
    session._batch_child = False
    session._serial_dispatch = False
    assert session._checked_source(spec, {}, final_only=False) is None
    assert reached == [], "refused before the write ceiling is read"
    session._serial_dispatch = True
    assert session._checked_source(spec, {}, final_only=False) is None and reached == ["ceiling"]
    # The last-slot form still needs the final work slot.
    session._max_tasks, session._slots_used = 3, 0
    assert session._checked_source(spec, {}, final_only=True) is None and reached == ["ceiling"]
    session._slots_used = 2
    assert session._checked_final_source(spec, {}) is None and reached == ["ceiling", "ceiling"]


def test_the_checked_source_reads_only_this_tasks_own_observations(tmp_path, monkeypatch):
    """The passing run that admits a handoff is one of this task's own
    observations of exactly the current source and gate; another task's
    observation of identical bytes, or a failed one, admits nothing."""
    session = _bare(tmp_path)
    spec = replace(_spec(), scope=TaskScope(permitted_paths=("app.py",), intended_result="x", acceptance=("y",)))
    session._serial_dispatch = True
    monkeypatch.setattr(session, "_write_ceiling", lambda: True)
    monkeypatch.setattr(session, "_inspect_partial_edits", lambda before: dict(inspected=True, changed=["app.py"], changed_lines=3))
    monkeypatch.setattr(session, "_measure_scope", lambda spec, before: SimpleNamespace(blocking=False))
    monkeypatch.setattr(session, "_observe_checks", lambda spec, stage: None)
    monkeypatch.setattr(session, "_source_fingerprint", lambda: "src-1")
    monkeypatch.setattr(session, "_current_gate", lambda: object())
    monkeypatch.setattr(S, "_gate_identity", lambda gate: "gate-1")
    session.review_checks = [dict(task="t1", source="src-1", gate="gate-1", passed=True)]
    assert session._checked_source(spec, {}, final_only=False) == (dict(inspected=True, changed=["app.py"], changed_lines=3), "src-1", "gate-1")
    session.review_checks = [dict(task="t9", source="src-1", gate="gate-1", passed=True)]
    assert session._checked_source(spec, {}, final_only=False) is None, "another task's observation is not this task's"
    session.review_checks = [dict(task="t1", source="src-1", gate="gate-1", passed=False)]
    assert session._checked_source(spec, {}, final_only=False) is None, "a failed run is no gate"
    session.review_checks = [dict(task="t1", source="src-0", gate="gate-1", passed=True)]
    assert session._checked_source(spec, {}, final_only=False) is None, "another source is not this one"
    session.checks = [dict(source="src-1", gate="gate-1", passed=True)]
    assert session._checked_source(spec, {}, final_only=False) is None, "a task check record is not an observation"


def test_slots_unaccounted_reports_a_legacy_count_the_ledger_does_not_back(tmp_path):
    from quadratus import project_run as PR
    session = _bare(tmp_path)
    session._slots_used = 3
    session._slot_event("charged", ["t1"], 1, how="serial")
    session._slot_event("sent_back", ["t2"], 1, why="no COVERS")
    session._slot_event("charged", ["t2", "t3"], 3, how="batch")
    record = PR._slot_fields(session)
    assert record["slots_unaccounted"] == 0 and len(record["slot_ledger"]) == 3
    session._slots_used = 5
    assert PR._slot_fields(session)["slots_unaccounted"] == 2, "two slots the old rule counted that no charge backs"


# --- root's correction on 0c5c6f1 (6538988187): Q1, Q2, Q3, C1, C2, E1 at the unit level ---------

def test_the_recheck_contract_binds_each_finding_to_its_own_requirement_and_adds_no_duty(tmp_path):
    """Q1: the two recheck routes carried a generated task and a bare
    requirement id. The contract block carries the verbatim goal, the
    original text of each requirement a named finding was raised under
    (its own requirement, its basis, else the covers of the task that
    raised it), the operator's rulings, the order of authority and the
    boundary rule; never the current task's COVERS, and no full-review
    verdict obligation."""
    from quadratus.outcome import TaskOutcome
    session, _ = _session(tmp_path, "")
    session.memory.ledger.rulings.append("Q: keep add pure? -- A: yes, no logging")
    session.task_outcomes.append(TaskOutcome(task_id="t1", intent="implementation", lead="grok:default",
                                             covers=["R1"], resolves=[], continues=""))
    session._current_covers = ["R5"]
    own = dict(fid="RFT1-1", task="t1", author="claude:opus", claim="x", requirement=["R1", 1], basis=dict(kind="none"))
    by_basis = dict(fid="RFT1-2", task="t1", author="claude:opus", claim="y", requirement=None,
                    basis=dict(kind="requirement", ref="R5@1"))
    by_task = dict(fid="RFT1-3", task="t1", author="claude:opus", claim="z", requirement=None, basis=dict(kind="none"))
    block = session._finding_contract([own])
    assert "## Original goal (verbatim; it outranks the task text where they differ)" in block
    assert "R1 (RFT1-1, raised on t1): typing filters the cards" in block and "R5" not in block, \
        "bound to the finding, labelled with it, not the current COVERS"
    assert "## Operator rulings (asked and answered; do not re-ask)" in block and "- Q: keep add pure? -- A: yes, no logging" in block
    assert S._AUTHORITY_ORDER in block and S._CONTRACT_BOUNDARY_RULE in block
    assert "For each covered requirement give one line" not in block and "NO FINDINGS" not in block
    assert "R5 (RFT1-2, raised on t1): the list logs no console errors while typing" in session._finding_contract([by_basis])
    assert "R1 (RFT1-3, raised on t1): typing filters the cards" in session._finding_contract([by_task]), \
        "the raising task's covers"
    both = session._finding_contract([own, by_basis])
    assert both.count("typing filters the cards") == 1 and both.count("the list logs no console") == 1
    # A basis naming an id the ledger does not hold binds nothing; the
    # raising task's covers stand (workflow refutation of this batch).
    unknown = dict(fid="RFT1-4", task="t1", author="claude:opus", claim="w", requirement=None,
                   basis=dict(kind="requirement", ref="R9@1"))
    assert "R1 (RFT1-4, raised on t1): typing filters the cards" in session._finding_contract([unknown])
    dashed = dict(unknown, fid="RFT1-5", basis=dict(kind="requirement", ref="R-1@1"))
    assert "R1 (RFT1-5, raised on t1): typing filters the cards" in session._finding_contract([dashed])
    # Two findings under one requirement share its line, each named.
    shared = session._finding_contract([own, by_task])
    assert "R1 (RFT1-1, raised on t1; RFT1-3, raised on t1): typing filters the cards" in shared
    # Every ruling is rendered whole, as the ledger renders it.
    for n in range(8):
        session.memory.ledger.rulings.append(f"Q: ruling {n}? -- A: " + "x" * 450)
    whole = session._finding_contract([own])
    assert all(f"- Q: ruling {n}? -- A: " + "x" * 450 in whole for n in range(8)) and "no logging" in whole


def test_the_serial_lead_is_told_the_order_of_authority_once_beside_the_boundary_rule(tmp_path):
    """Q2: the serial lead's render carried the goal and the requirements
    and the boundary rule, never that the goal and the rulings outrank the
    generated task text, plan or recipe."""
    session, _ = _session(tmp_path, "")
    session._current_covers = ["R1"]
    prompt = session._lead_prompt(_spec())
    assert prompt.count(S._AUTHORITY_ORDER) == 1 and prompt.count(S._CONTRACT_BOUNDARY_RULE) == 1
    # A parallel child's lead gets both through the goal block, still once.
    session.memory.ledger.requirements.clear()
    session._requirement_texts = {"R1": "typing filters the cards"}
    child = session._lead_prompt(_spec())
    assert child.count(S._AUTHORITY_ORDER) == 1 and child.count(S._CONTRACT_BOUNDARY_RULE) == 1
    assert "## Original goal (verbatim" in child


def test_the_audit_reassessment_states_the_executed_error_rule_it_enforces(tmp_path, monkeypatch):
    """Q3: the reassessment of an error requirement offered 'a project file
    path or test name' while the lowerer required exact executed case
    identities. The prompt now carries the same rule the final audit does;
    a MET on a bare test name is still held, an exact executed identity
    stands. The lowerer is unchanged."""
    session, _ = _session(tmp_path, "")
    session.memory.ledger.requirements["R7"] = "add rejects a non-number argument with TypeError"
    # The scratch test the harness ran is never a project file: the valid
    # citation names the executed case beside a project file that resolves
    # (workflow refutation of this batch: planting the scratch file in the
    # project proved the case in a state the engine cannot reach).
    (session.config.project / "add.py").write_text("def add(a, b):\n    return a + b\n")
    prompts, replies = [], ["R7: MET - add.py and tests/test_e1.py"]
    monkeypatch.setattr(session, "_invoke_model", lambda key, prompt, **kw: prompts.append(prompt) or replies.pop(0))
    monkeypatch.setattr(session, "_quiescent", lambda: None)
    entry = dict(kind="audit", reviewer="openai:sol", finding="R7: NOT MET - no rejecting case")
    identity = session._evidence_identity()
    record = dict(id="E1", target="R7", kind="audit", reviewer="openai:sol", task=None, finding=entry["finding"],
                  original_disposition="NOT MET", claim="", file="tests/test_e1.py", file_sha256="x",
                  source=session._source_fingerprint(), identity=identity, key=["audit", "R7", identity],
                  receipt=dict(status="passed", reason="", cases=["tests/test_e1.py::test_rejects: passed"]),
                  execution=None, reassessment=None, stage="intent", test="def test_rejects(): pass\n",
                  disposition="")
    session.evidence_requests.append(record)
    held = session._reassess("R7", entry, record, record["test"])
    assert S._EXECUTED_ERROR_RULE in prompts[0] and S._ERROR_PATH_RULE in prompts[0]
    assert "the exact executed case identities the rule above names, beside a project file or test that resolves" in prompts[0]
    assert "the scratch test the harness ran is not a project file" in prompts[0]
    assert ("A citation resolves only as a project-relative file path with an extension, or a test function named "
            "test_* defined in a file whose name starts with test and ends in .py; other names do not resolve") in prompts[0], \
        "the resolution rule the gate enforces is stated (second-round refutation: nothing pinned it here)"
    assert held.startswith("held: an error or refusal requirement: the MET cites no exact test case"), held
    replies.append("R7: MET - tests/test_e1.py::test_rejects")
    alone = session._reassess("R7", entry, record, record["test"])
    assert alone.startswith("held: MET claimed without a project file or test that exists"), alone
    replies.append("R7: MET - add.py and tests/test_e1.py::test_rejects")
    exact = session._reassess("R7", entry, record, record["test"])
    assert exact.startswith("MET on reassessment by the same auditor"), exact


def test_the_budget_renews_a_consumed_finishing_stage_within_the_bound(tmp_path):
    """C1: after the final audit started and settled, the hold is gone and
    ordinary work could spend the capacity a second audit needs. The
    renewal puts the stage back under hold, bounded, without resetting
    anything spent or growing the allowance."""
    from quadratus.run_budget import RunBudget, RunLimits, finishing
    budget = RunBudget(RunLimits(max_calls=20, max_reported_tokens=1_000_000, wall_seconds=600,
                                 max_concurrent_workers=1, reserve_tokens_per_call=10_000),
                       path=tmp_path / "budget.json")
    budget.protect_finish(["goal", "audit"])
    with finishing("audit"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=100, output_tokens=10))
    fin = budget.snapshot()["finish_protection"]
    assert fin["pending"] == ["goal"] and fin["started"] == ["audit"]
    assert budget.renew_finish("audit", why="repair t2 admitted after the final audit", limit=2) is True
    fin = budget.snapshot()["finish_protection"]
    assert fin["pending"] == ["goal", "audit"] and fin["started"] == ["audit"], "history kept, hold renewed"
    assert fin["renewals"] == [dict(stage="audit", why="repair t2 admitted after the final audit", at_attempt=1,
                                     renewal=1, previously_started=True)]
    assert budget.snapshot()["reserved_attempts"] == 1 and budget.snapshot()["reported_tokens"] == 110
    assert budget.renew_finish("audit", why="again", limit=2) is True, "already pending: nothing new"
    assert len(budget.snapshot()["finish_protection"]["renewals"]) == 1
    with finishing("audit"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=100, output_tokens=10))
    assert budget.renew_finish("audit", why="third", limit=2) is True
    with finishing("audit"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=100, output_tokens=10))
    assert budget.renew_finish("audit", why="past the bound", limit=2) is False
    fin = budget.snapshot()["finish_protection"]
    assert "audit" not in fin["pending"] and fin["renewal_refusals"][0]["limit"] == 2
    # protect_finish still never re-holds a started stage on its own.
    budget.protect_finish(["goal", "audit"])
    assert "audit" not in budget.snapshot()["finish_protection"]["pending"]


def test_protect_finish_keeps_a_renewed_audit_held_and_its_capacity_reserved(tmp_path):
    """C1 (root's independent reproduction on the recovered head): after
    renew_finish("audit") the budget held pending [goal, audit] with
    started [audit], and a later protect_finish([goal, audit]) dropped the
    renewed audit because it had started once, freeing its capacity to an
    ordinary call. The renewed stage now stays held while its renewal is
    owed, the call it holds for is still refused to ordinary work, and once
    the renewed audit starts nothing re-holds it. Spend, history and the
    allowance are unchanged throughout."""
    from quadratus.run_budget import FinishReserved, RunBudget, RunLimits, finishing
    budget = RunBudget(RunLimits(max_calls=4, max_reported_tokens=1_000_000, wall_seconds=600,
                                 max_concurrent_workers=1, reserve_tokens_per_call=10_000),
                       path=tmp_path / "budget.json")
    budget.protect_finish(["goal", "audit"])
    with finishing("audit"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=100, output_tokens=10))
    assert budget.renew_finish("audit", why="repair t2 admitted after the final audit", limit=2) is True
    before = budget.snapshot()
    # Positive: the episode's own protect_finish keeps the renewed audit held.
    budget.protect_finish(["goal", "audit"])
    fin = budget.snapshot()["finish_protection"]
    assert fin["pending"] == ["goal", "audit"] and fin["started"] == ["audit"], fin
    assert len(fin["renewals"]) == 1, "idempotent: no second renewal is recorded"
    budget.protect_finish(["goal", "audit"])
    assert budget.snapshot()["finish_protection"]["pending"] == ["goal", "audit"], "idempotent"
    # The held capacity stays reserved: of 4 calls, 1 spent and 2 held, so
    # one ordinary call fits and the next is refused to keep the finish.
    ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=10, output_tokens=1))
    with pytest.raises(FinishReserved):
        budget.reserve()
    after = budget.snapshot()
    assert after["reserved_attempts"] == before["reserved_attempts"] + 1, "nothing reset"
    assert after["finish_protection"]["started"] == ["audit"], "history kept"
    # Negative: once the renewed audit starts, protect_finish does not hold it
    # again, and a stage that was never renewed stays unheld after its start.
    with finishing("audit"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=100, output_tokens=10))
    budget.protect_finish(["goal", "audit"])
    fin = budget.snapshot()["finish_protection"]
    assert fin["pending"] == ["goal"] and fin["started"] == ["audit", "audit"], fin
    with finishing("goal"):
        ticket, _ = budget.reserve()
    budget.finish(ticket, dict(input_tokens=10, output_tokens=1))
    budget.protect_finish(["goal", "audit"])
    assert budget.snapshot()["finish_protection"]["pending"] == [], "a started, unrenewed stage is not re-held"


def test_a_task_admission_is_published_before_the_first_effect_and_a_failed_publication_starts_nothing(tmp_path):
    """C2: the admission names the task, its slot, the committed authority
    and the attempt count, and is committed to the job store under
    ``task-admissions``; a store that cannot commit it stops the run with
    the admission not kept."""
    from quadratus.jobstore import JobStore
    store = JobStore(tmp_path / "run")
    session = _bare(tmp_path, job_store=store)
    session._max_tasks, session._slots_used = 3, 1
    entry = session._admit_dispatch([_spec()], how="task", used=1)
    assert (entry["admission"], entry["tasks"], entry["slot"], entry["slots"], entry["max_tasks"]) == ("A1", ["t1"], 2, 1, 3)
    assert entry["authority"] and len(entry["authority"]) >= 16 and entry["outcome"] is None
    from quadratus import jobstore
    manifest = jobstore.read_manifest(tmp_path / "run")
    committed = jobstore.read_record(tmp_path / "run", "task-admissions", manifest=manifest)
    assert committed == [dict(entry)], "committed before any effect, as the session holds it"
    authority = jobstore.read_record(tmp_path / "run", "authority", manifest=manifest)
    assert authority["digest"] == entry["authority"], "under the committed authority"

    def broken(updates, **kw):
        if "task-admissions" in updates:
            raise OSError("disk full")
        return original(updates, **kw)
    original = store.publish
    store.publish = broken
    with pytest.raises(S.JobStoreUnavailable, match="could not publish its admission"):
        session._admit_dispatch([replace(_spec(), task_id="t2")], how="task", used=2)
    assert [e["admission"] for e in session.task_admissions] == ["A1"], "a refused admission is not kept"


def test_a_batch_child_on_the_security_route_publishes_no_admission_of_its_own(tmp_path, monkeypatch):
    """C2: a parallel child's admission is its wave's, committed by the
    parent before any child ran. A child session on the security route
    must not publish a second record over the parent's (its own list is
    empty and shares the store); the serial route on the same spec does
    admit once."""
    from quadratus.jobstore import JobStore
    store = JobStore(tmp_path / "run")
    stubbed = dict(_renew_audit_hold=lambda self, specs, *, how: None,
                   _refuse_unpayable_dispatch=lambda self, spec, stages, **kw: None,
                   _mandatory_stages=lambda self, spec, **kw: [],
                   _run_security_task=lambda self, spec: "security ran",
                   _authority_before=lambda self, what: "0" * 16)
    for name, fn in stubbed.items():
        monkeypatch.setattr(S.Session, name, fn)
    spec = replace(_spec(), work_class=S.WorkClass.SECURITY)
    parent = _bare(tmp_path, job_store=store)
    parent._max_tasks = 3
    parent.task_admissions.append(dict(admission="A1", tasks=["t1", "t2"], how="batch", slot=1, slots=2, outcome=None))
    store.publish({"task-admissions": list(parent.task_admissions)})
    child = _bare(tmp_path / "c", job_store=store)
    child._batch_child, child._serial_dispatch = True, False
    assert child._run_task(spec) == "security ran"
    assert child.task_admissions == [], "the child admitted nothing"
    from quadratus import jobstore
    assert [a["admission"] for a in jobstore.read_record(tmp_path / "run", "task-admissions")] == ["A1"], \
        "the parent's wave admission is still the committed record"
    serial = _bare(tmp_path / "s", job_store=JobStore(tmp_path / "s-run"))
    serial._max_tasks, serial._serial_dispatch = 3, True
    seen = []

    def first_effect(self, spec):
        # The security route's first call finds the admission committed.
        seen.append([a["admission"] for a in jobstore.read_record(tmp_path / "s-run", "task-admissions")])
        return "security ran"
    monkeypatch.setattr(S.Session, "_run_security_task", first_effect)
    assert serial._run_task(spec) == "security ran"
    assert seen == [["A1"]], "committed before the security route's first effect"
    assert [(a["admission"], a["tasks"], a["how"]) for a in serial.task_admissions] == [("A1", ["t1"], "task")]


def test_a_parallel_wave_whose_admission_the_store_refuses_starts_nothing_and_says_so(tmp_path):
    """C2, batch (second-round refutation): a wave whose admission the store
    cannot commit was charged nothing but recorded nowhere except the
    error. Each task is now recorded not started, with one not_started slot
    event naming the wave, and no child runs."""
    from quadratus.jobstore import JobStore
    from tests.test_parallel_tasks import BATCH, Orchestrated
    from tests.test_parallel_tasks import _session as parallel_session

    class Failing(JobStore):
        def publish(self, updates, **kw):
            if "task-admissions" in updates:
                raise OSError("disk full")
            return super().publish(updates, **kw)

    script = Orchestrated([BATCH, "DONE"], lead_delay=0)
    session, _project = parallel_session(tmp_path, script, job_store=Failing(tmp_path / "run"))
    with pytest.raises(S.JobStoreUnavailable, match="could not publish its admission"):
        session.run(max_tasks=3)
    tasks = [n["task"] for n in session.not_started]
    assert len(tasks) == 2 and [(e["event"], e["tasks"], e["slots_used"]) for e in session.slot_ledger] == [
        ("not_started", tasks, 0)]
    assert session.task_admissions == [] and int(session._slots_used or 0) == 0
    assert script.roots == [], "no child was built or called"


def test_interrupted_admissions_are_counted_as_spent_on_resume_never_dropped(tmp_path):
    """C2: an admission committed after the checkpoint's quiescent point
    (the task interrupted before any effect settled) is read by the resume
    and charged, on the ledger and the admissions record."""
    from quadratus.jobstore import JobStore
    store = JobStore(tmp_path / "run")
    session = _bare(tmp_path, job_store=store)
    session._max_tasks, session._slots_used = 3, 1
    session._slot_event("charged", ["t1"], 1, how="serial", why="A1")
    session.task_admissions.append(dict(admission="A1", tasks=["t1"], how="task", slot=1, slots=1, outcome="charged"))
    body = dict(state=dict(task_admissions=list(session.task_admissions)))
    session._admit_dispatch([replace(_spec(), task_id="t2")], how="task", used=1)
    interrupted = checkpoint._interrupted_admissions(tmp_path / "run", body)
    assert [e["admission"] for e in interrupted] == ["A2"] and interrupted[0]["tasks"] == ["t2"]
    assert checkpoint._interrupted_admissions(tmp_path / "run", dict(state=dict(task_admissions=session.task_admissions))) == []
    assert checkpoint._interrupted_admissions(tmp_path / "elsewhere", body) == [], "no store, no admissions"
    fresh = _bare(tmp_path / "f")
    fresh._max_tasks = 3
    fresh._interrupted_admissions = interrupted
    assert fresh._count_interrupted_admissions(1) == 2
    assert fresh._slots_used == 2 and fresh.task_admissions[-1]["outcome"].startswith("interrupted")
    (event,) = fresh.slot_ledger
    assert (event["event"], event["tasks"], event["slots_used"]) == ("charged", ["t2"], 2) and "interrupted" in event["how"]
    assert "Admitted in an earlier episode and interrupted before closing: t2" in fresh._slots_note()
    assert fresh._interrupted_admissions == [], "counted once"


def test_the_checker_requires_the_clearing_check_to_carry_the_rows_requirement_revisions():
    """E1: a saved record whose typed NOT RUN row binds R1 revision 1 while
    the clearing check ran under revision 2 is contradictory; the
    reconstruction refuses it instead of trusting the row's ``ran``. A
    record with no typed row for the entry keeps its route's identity
    alone; a check that records no revision map clears nothing once a
    typed row exists."""
    from tools import truth_check as T
    typed = dict(kind="not_run", task="t1", entry=dict(item="tests/test_app.py::test_add"), requirements=[["R1", 1]])
    assert T._revisions_match(typed, dict(requirements={"R1": 1})) is True
    assert T._revisions_match(typed, dict(requirements={"R1": 2})) is False
    assert T._revisions_match(typed, dict(requirements={"R2": 1})) is False
    assert T._revisions_match(typed, dict()) is False, "no map: no guessed approval"
    assert T._revisions_match(dict(typed, requirements=[]), dict(requirements={"R1": 1})) is False
    entry = dict(task="t1", item="tests/test_app.py::test_add", binding=dict(kind="case"))
    record = dict(evidence_obligations=[typed])
    assert T._bound_to_row(entry, dict(requirements={"R1": 1}), record) is True
    assert T._bound_to_row(entry, dict(requirements={"R1": 2}), record) is False
    assert T._bound_to_row(entry, dict(), record) is False
    assert T._bound_to_row(entry, dict(), dict(evidence_obligations=[])) is True, "no typed row: the route's identity alone"
    assert T._bound_to_row(dict(entry, task="t9"), dict(), record) is True, "another task's row is not this entry's"
    # Workflow refutation of this batch, four shapes. A null revision never
    # equals a missing map entry; a decoy row beside the live one is held
    # too; a behavior-only or int-task row is this entry's row; a
    # superseded row is not.
    assert T._revisions_match(dict(typed, requirements=[["R1", None]]), dict(requirements={})) is False
    assert T._revisions_match(dict(typed, requirements=[["R1", None]]), dict(requirements={"R1": None})) is False
    assert T._revisions_match(dict(typed, requirements=[["R1", True]]), dict(requirements={"R1": True})) is False
    # A malformed bound reference refuses rather than being dropped (root's
    # independent review of the recovered head); the same row with every
    # reference a pair matches.
    assert T._revisions_match(dict(typed, requirements=[["R1", 1], ["R2"]]), dict(requirements={"R1": 1})) is False
    assert T._revisions_match(dict(typed, requirements=[["R1", 1], "R2"]), dict(requirements={"R1": 1})) is False
    assert T._revisions_match(dict(typed, requirements=[["R1", 1], ["R2", 1]]),
                              dict(requirements={"R1": 1, "R2": 1})) is True
    decoy = dict(typed, oid="O9", requirements=[["R9", 2]])
    two = dict(record, evidence_obligations=[decoy, typed])
    assert T._bound_to_row(entry, dict(requirements={"R1": 2, "R9": 2}), two) is False, "the live row binds R1@1"
    assert T._bound_to_row(entry, dict(requirements={"R1": 1, "R9": 2}), two) is True, "every live row holds"
    behavior_only = {k: v for k, v in typed.items() if k != "entry"}
    behavior_only["behavior"] = entry["item"]
    assert T._typed_not_run_rows(entry, dict(evidence_obligations=[behavior_only])) == [behavior_only]
    assert T._bound_to_row(entry, dict(requirements={"R1": 2}), dict(evidence_obligations=[behavior_only])) is False
    assert T._bound_to_row(dict(entry, task="1"), dict(requirements={"R1": 2}),
                           dict(evidence_obligations=[dict(typed, task=1)])) is False
    superseded = dict(typed, oid="O0", superseded_by="t2", requirements=[["R1", 2]])
    assert T._bound_to_row(entry, dict(requirements={"R1": 2}), dict(evidence_obligations=[superseded, typed])) is False
    # A not_run row the engine never supersedes: one that reads superseded
    # still binds when it is the only row, never identity alone.
    alone = dict(typed, superseded_by="t2")
    assert T._bound_to_row(entry, dict(requirements={"R1": 2}), dict(evidence_obligations=[alone])) is False
    assert T._bound_to_row(entry, dict(requirements={"R1": 1}), dict(evidence_obligations=[alone])) is True
    # The older path route (no binding key on the entry) is held to the row too.
    legacy = dict(task="t1", item="tests/test_app.py", after_check=0)
    path_row = dict(typed, entry=dict(item="tests/test_app.py"))
    legacy_record = dict(source_fingerprint="src", evidence_obligations=[path_row])
    checks = [dict(passed=True, source="src", requirements={"R1": 2},
                   cases=dict(executed=["tests/test_app.py::test_add"]))]
    assert T._historical_paths_cleared(legacy, checks, legacy_record) is False, "the row binds R1@1"
    assert T._historical_paths_cleared(legacy, [dict(checks[0], requirements={"R1": 1})], legacy_record) is True
    assert T._historical_paths_cleared(legacy, checks, dict(legacy_record, evidence_obligations=[])) is True, \
        "no typed row: the older reading stands"
