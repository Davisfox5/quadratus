"""The close family (Davis, 2026-10-26), after two live runs ended
FindingsOpen with task and recovery slots unspent.

recovery-handoff-01: Sol's SCENARIO claim on t3 used an em dash, did not
type, went to the editor as code work, stayed open as held review debt,
and the batch close that followed (t4 and t5, both rechecked clean after
the merge) stopped on it because the batch close never consulted the hold
the serial close applies. This file pins the repaired family on the real
controller below the vendor CLIs:

- typed claims type with any normal separator and only with their full
  semantics; a claim that opens as typed but is incomplete, unbound to this
  task's scenarios, or a repeat on a corrected scenario is held for its
  reviewer and is never the editor's instruction; a recheck that restates a
  finding in full is routed before any editor round;
- the planner's correction is one per scenario, a reply with conflicting
  lines changes nothing, and nothing a correction does clears a finding;
- one close policy decides every close, serial or batch: held review debt,
  inherited or the batch's own, continues to the planner while a slot is
  left and goes to the cap's completion decision when none is; an unmerged
  child, a failing check without debt and a lost typed record still stop;
- every close decision is recorded, carried and sealed.

What stays as it was: generic provider failures, unmerged conflicts,
missing usage, budgets, the independent reviews and the audit (no
automatic MET).
"""
import json
from pathlib import Path

import pytest

from quadratus import checkpoint
from quadratus.session import TaskSpec, _typed_finding, _typed_near_miss
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import CASE, _acceptance, _at, _lead, _plan, _proofs, _scope
from tests.lifecycle.test_recovery_handoff import GROK_CAP, NARRATION, _batch_script
from tests.lifecycle.test_workflow_repair import (
    CODE_FINDING,
    CORRECTION_HEAD,
    MET,
    SCENARIO_FINDING,
    Seen,
    _roles,
    _script,
)
from tests.test_parallel_tasks import Orchestrated
from tests.test_parallel_tasks import _session as parallel_session

CONTRADICTION = "the prior state 'two tickets exist' contradicts add, which stores nothing"
PROPOSAL = "proposal: state: nothing is stored"
TAIL = " | basis: R1 | impact: contract"
SOL_RFT3_4 = ("SCENARIO P6 — Low severity, high confidence. Adding prerequisite 1 to ticket 1 is a self edge, not the "
              "stated cycle. The bound test already includes the actual cycle operation. | proposal: operation: "
              "Replace the cycle call with POST /tickets/2/dependencies {\"prerequisite_id\": 1} after edge 1->2 exists.")
SEPARATORS = {"hyphen": " - ", "en-dash": " – ", "em-dash": " — ", "minus": " − ", "colon": ": "}


def _closes(replay):
    return H.result_json(replay)["close_decisions"]


# -- 1. the typed forms, at the parser -------------------------------------------------------

def test_the_live_em_dash_claim_types_in_full():
    kind, fields = _typed_finding(SOL_RFT3_4)
    assert kind == "scenario" and fields["proof_id"] == "P6" and fields["field"] == "operation"
    assert fields["reason"].startswith("Low severity") and fields["proposal"].startswith("Replace the cycle call")


@pytest.mark.parametrize("sep", sorted(SEPARATORS), ids=sorted(SEPARATORS))
def test_every_normal_separator_types_a_complete_claim(sep):
    claim = f"SCENARIO P1{SEPARATORS[sep]}{CONTRADICTION} | {PROPOSAL}"
    assert _typed_finding(claim) == ("scenario", dict(proof_id="P1", reason=CONTRADICTION, field="state",
                                                      proposal="nothing is stored"))
    assert _typed_finding(f"EVIDENCE P1{SEPARATORS[sep]}test_add is not shown executed") == \
        ("evidence", dict(proof_id="P1", reason="test_add is not shown executed"))


@pytest.mark.parametrize("claim, miss", [
    ("SCENARIO P1 -   | proposal: state: nothing stored", "blank contradiction or a blank proposed text"),
    ("SCENARIO P1 - the state is wrong | proposal: state:   ", "blank contradiction or a blank proposed text"),
    ("SCENARIO P1 - the state is wrong", "without its '| proposal:"),
    ("SCENARIO P1 - the state is wrong | proposal: location: tests/other.py", "names no scenario field"),
    ("EVIDENCE P1 -   ", "without saying what is not shown"),
    ("EVIDENCE P1", "without saying what is not shown"),
    ("SCENARIO P٦ - the state is wrong | proposal: state: nothing", "is not P followed by digits"),
    ("SCENARIO Pone - the state is wrong | proposal: state: nothing", "is not P followed by digits"),
])
def test_an_incomplete_typed_claim_is_no_route_and_is_named_as_a_near_miss(claim, miss):
    """Root's old-source control: blank fields typed with empty strings.
    Boundary ids (a non-ASCII digit, a word) are near misses too."""
    assert _typed_finding(claim) == (None, None)
    assert miss in _typed_near_miss(claim)


def test_an_ordinary_code_finding_is_neither_typed_nor_a_near_miss():
    for claim in ("nothing tests negative numbers", "the CLI prints usage text instead of JSON", ""):
        assert _typed_finding(claim) == (None, None) and _typed_near_miss(claim) == ""


# -- 2. the typed forms, through the controller ---------------------------------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("sep", sorted(SEPARATORS), ids=sorted(SEPARATORS))
def test_a_scenario_claim_with_any_normal_separator_reaches_the_planner_not_the_editor(tmp_path, monkeypatch, sep):
    seen = Seen()
    finding = f"BLOCKING: SCENARIO P1{SEPARATORS[sep]}{CONTRADICTION} | {PROPOSAL}{TAIL}"

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            return "SCENARIO P1: state: nothing is stored"
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, sep), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: finding + MET,
                           recheck=lambda c, r: "RFT1-1: RESOLVED the corrected scenario holds"),
                   files=FILES, max_tasks=1, record_complete=False)
    assert len([c for c in replay.calls if CORRECTION_HEAD in c.prompt]) == 1
    assert "revision" not in _roles(replay)
    record = H.result_json(replay)
    assert [c["status"] for c in record["scenario_corrections"]] == ["corrected"]
    assert dict(_acceptance(replay)[0]["scenario"])["state"] == "nothing is stored"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("kind", ["scenario", "evidence"])
def test_a_blank_typed_claim_is_held_and_its_full_restatement_at_the_recheck_is_routed(tmp_path, monkeypatch, kind):
    """No editor round at any point: the blank claim is held and the reviewer
    is told why; the restatement goes to the planner (scenario) or the
    harness (evidence) and comes back to the same reviewer."""
    seen = Seen()
    blank = ("BLOCKING: SCENARIO P1 -   | proposal: state: nothing stored" if kind == "scenario"
             else "BLOCKING: EVIDENCE P1 -   ") + TAIL
    restated = (f"RFT1-1: UNRESOLVED SCENARIO P1 - {CONTRADICTION} | {PROPOSAL}" if kind == "scenario"
                else "RFT1-1: UNRESOLVED EVIDENCE P1 - test_add is not shown executed on this source")

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            return "SCENARIO P1: state: nothing is stored"
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"

    def recheck(call, replay):
        return restated if len(replay.of("recheck")) == 1 else "RFT1-1: RESOLVED shown now"
    replay = H.run(_at(tmp_path, kind), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: blank + MET, recheck=recheck),
                   files=FILES, max_tasks=1, record_complete=False)
    assert "revision" not in _roles(replay), "a blank typed claim is not permission to edit"
    assert _roles(replay)["recheck"] == 2
    first = seen.prompts["recheck"][0]
    assert "held, not routed and not given to the editor" in first and "Restate it in full" in first
    record = H.result_json(replay)
    finding = record["review_findings"][0]
    assert finding["restated"].startswith("SCENARIO P1 -" if kind == "scenario" else "EVIDENCE P1 -")
    assert finding["route_history"][0]["route"] == "unroutable"
    if kind == "scenario":
        assert len([c for c in replay.calls if CORRECTION_HEAD in c.prompt]) == 1
        assert [c["status"] for c in record["scenario_corrections"]] == ["corrected"]
        assert finding["route"] == "scenario: corrected"
    else:
        assert not any(CORRECTION_HEAD in c.prompt for c in replay.calls)
        assert finding["route"] == "evidence"
        assert [r["action"][:12] for r in record["evidence_routes"]] == ["held unrouta", "no new run: "]
    assert finding["state"] == "resolved" and replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("lines, status, state", [
    ("SCENARIO P1: state: nothing is stored\nSCENARIO P1: state: one ticket exists",
     "refused: ambiguous reply (2 conflicting lines for P1); the scenario stays as declared", "two tickets exist"),
    ("SCENARIO P1: state: nothing is stored\nSCENARIO P1: state: nothing is stored", "corrected", "nothing is stored"),
], ids=["conflicting", "repeated"])
def test_conflicting_correction_lines_change_nothing_and_repeated_ones_are_one_line(tmp_path, monkeypatch, lines,
                                                                                    status, state):
    seen = Seen()

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            return lines
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, status[:8]), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: SCENARIO_FINDING + MET,
                           recheck=lambda c, r: ("RFT1-1: RESOLVED corrected" if state == "nothing is stored"
                                                 else "RFT1-1: UNRESOLVED still contradicts itself")),
                   files=FILES, max_tasks=1, record_complete=False)
    record = H.result_json(replay)
    assert [c["status"] for c in record["scenario_corrections"]] == [status]
    assert dict(_acceptance(replay)[0]["scenario"])["state"] == state
    assert "revision" not in _roles(replay)
    if status.startswith("refused"):
        assert record["scenario_corrections"][0]["lines"] == ["state: nothing is stored", "state: one ticket exists"]
        assert "the planner's answer was: refused: ambiguous reply" in seen.prompts["recheck"][0]
        assert record["review_findings"][0]["state"] == "unresolved" and not replay.result.completed
    else:
        assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_each_scenario_gets_one_correction_and_a_repeat_on_a_corrected_one_is_held(tmp_path, monkeypatch):
    """Per scenario, not per task: a second reviewer finding on P2 gets its
    own correction; a restated claim on the already corrected P1 is held."""
    seen = Seen()
    plan = _plan(_scope(covers=("R1", "R2"), proofs=_proofs("R1", "R2")), covers="R1, R2",
                 requirements="R1: add(a, b) returns the sum\nR2: add(0, 0) returns 0")
    corrections = []

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            pid = "P2" if "Scenario P2 as declared" in call.prompt else "P1"
            corrections.append(pid)
            return f"SCENARIO {pid}: state: nothing is stored"
        return plan if len(replay.of("orchestrator")) == 1 else "DONE"

    def recheck(call, replay):
        n = len(replay.of("recheck"))
        if n == 1:
            return (f"RFT1-1: RESOLVED corrected\nNEW: SCENARIO P2 - {CONTRADICTION} | {PROPOSAL} | basis: R2 "
                    "| impact: contract")
        if n == 2:
            return f"RFT1-2: UNRESOLVED SCENARIO P2 - still the same contradiction | {PROPOSAL}"
        return "RFT1-2: UNRESOLVED still wrong"
    replay = H.run(_at(tmp_path, "perscenario"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: SCENARIO_FINDING + MET,
                           recheck=recheck, auditor=lambda c, r: f"R1: MET - {CASE}\nR2: MET - {CASE}"),
                   files=FILES, max_tasks=1, record_complete=False)
    assert corrections == ["P1", "P2"], "one correction per scenario"
    assert "revision" not in _roles(replay)
    record = H.result_json(replay)
    second = next(f for f in record["review_findings"] if f["fid"] == "RFT1-2")
    assert second["route"] == "scenario: corrected" and second["state"] == "unresolved"
    assert any("already corrected once" in c.prompt or "the planner's answer was: corrected" in c.prompt
               for c in replay.of("recheck")[1:])
    assert not replay.result.completed


# -- 3. one close policy: serial, batch, last slot, negatives -------------------------------------

REQS2 = "R1: add(a, b) returns the sum\nR2: README documents add"
DOCS = ("KIND: docs simple\nSCOPE: " + json.dumps(dict(
    permitted_paths=["README.md"], intended_result="README documents add", acceptance=["README names add"],
    max_lines=20, proofs=H.proofs_for("R2", "README.md", evidence="reading"))) + "\nDocument add in README.md.\nCOVERS: R2")
NOTES = ("KIND: docs simple\nSCOPE: " + json.dumps(dict(
    permitted_paths=["NOTES.md"], intended_result="NOTES explains add", acceptance=["NOTES names add"],
    max_lines=20, proofs=H.proofs_for("R3", "NOTES.md", evidence="reading"))) + "\nWrite NOTES.md.\nCOVERS: R3")
DOCS_JUDGED = "SUFFICIENT the file names add(a, b) and states that it returns the sum"


def _debt_script(seen, orchestrator):
    """t1's reviewer raises an ordinary code finding it never lets go: the
    editor answers twice, the recheck stays UNRESOLVED, and t1 closes with
    held review debt. Later tasks are clean docs tasks."""
    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        path = "README.md" if "Document add in README.md." in call.prompt else "NOTES.md"
        H.write(call, {path: "# app\n\n`add(a, b)` returns the sum.\n"})
        return f'Documented add.\nCHANGED: ["{path}"]'

    def collaborator(call, replay):
        if call.task == "t1":
            return CODE_FINDING + MET
        return "NO FINDINGS\n" + "".join(f"{rid}: MET - documented\n" for rid in ("R2", "R3")
                                          if f"{rid}: " in call.prompt) + "REVIEW: COMPLETE"

    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading")
        return (f"R1: MET - {CASE}\nR2: MET - README.md\nR3: MET - NOTES.md"
                + "".join(f"\n{o}: {DOCS_JUDGED}" for o in docs))
    return _script(seen, orchestrator=orchestrator, lead=lead, collaborator=collaborator, auditor=auditor,
                   recheck=lambda c, r: "RFT1-1: UNRESOLVED still nothing tests negative numbers")


def _plan_with(*later, requirements=REQS2 + "\nR3: NOTES explains add"):
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return _plan(_scope(proofs=_proofs("R1")), requirements=requirements)
        return later[n - 2] if n - 2 < len(later) else "DONE"
    return orchestrator


@pytest.mark.requirements_ledger
def test_inherited_review_debt_does_not_stop_a_later_serial_close(tmp_path, monkeypatch):
    seen = Seen()
    replay = H.run(_at(tmp_path, "serial"), monkeypatch, _debt_script(seen, _plan_with(DOCS, requirements=REQS2)),
                   files=FILES, max_tasks=3, record_complete=False)
    assert _roles(replay)["revision"] == 2 and _roles(replay)["recheck"] == 2, "t1's ordinary fix cycles ran"
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the clean docs task ran after the held debt"
    assert len(replay.of("orchestrator")) >= 3, "the planner was asked after t2 and answered DONE"
    closes = _closes(replay)
    assert [(c["site"], c["closed"], c["stop"]) for c in closes[:2]] == [("task", ["t1"], None), ("task", ["t2"], None)]
    assert all(c["holds"] for c in closes[:2]), "t1's own debt, then the same debt inherited by t2's close"
    # DONE is sent back on the unmet requirement behind the open finding, then
    # the cap's decision ends the run over it: no automatic MET anywhere.
    assert not replay.result.completed
    assert replay.result.error.startswith(("DoneWithOpenWork", "RequirementsUnmet")), replay.result.error
    assert any("RFT1-1" in text for text in H.result_json(replay)["open_findings"])


@pytest.mark.requirements_ledger
def test_inherited_review_debt_does_not_stop_a_batch_close(tmp_path, monkeypatch):
    """recovery-handoff-01's shape: held debt from t3, then a clean batch.
    The batch close now consults the same hold and the planner is asked."""
    seen = Seen()
    replay = H.run(_at(tmp_path, "batch"), monkeypatch, _debt_script(seen, _plan_with("PARALLEL\n" + DOCS + "\n---\n" + NOTES)),
                   files=FILES, max_tasks=4, record_complete=False)
    assert sorted(c.task for c in replay.of("lead")) == ["t1", "t2", "t3"]
    batches = H.result_json(replay).get("parallel_batches") or []
    assert batches and batches[0]["tasks"] == ["t2", "t3"]
    assert len(replay.of("orchestrator")) >= 3, "the planner was asked after the batch"
    batch_close = next(c for c in _closes(replay) if c["site"] == "batch")
    assert batch_close["stop"] is None and batch_close["holds"] and batch_close["closed"] == ["t2", "t3", "t3-merge"]
    assert "FindingsOpen" not in (replay.result.error or "")
    assert not replay.result.completed
    assert replay.result.error.startswith(("DoneWithOpenWork", "RequirementsUnmet")), replay.result.error
    assert any("RFT1-1" in text for text in H.result_json(replay)["open_findings"])


@pytest.mark.requirements_ledger
def test_held_debt_in_the_last_slot_goes_to_the_caps_completion_decision(tmp_path, monkeypatch):
    """No slot left: the held debt is not a FindingsOpen stop before the
    finish. The cap's goal question is asked, the audit runs on the goal's
    confirmation, and the run still ends over the open finding."""
    seen = Seen()
    replay = H.run(_at(tmp_path, "lastslot"), monkeypatch,
                   _debt_script(seen, _plan_with(requirements="R1: add(a, b) returns the sum")),
                   files=FILES, max_tasks=1, record_complete=False)
    closes = _closes(replay)
    assert closes[0]["site"] == "task" and closes[0]["stop"] is None and closes[0]["slot_left"] is False
    assert "goes to the cap's completion decision" in closes[0]["reasons"][0]
    assert closes[-1]["site"] == "cap" and closes[-1]["holds"]
    assert len(replay.of("orchestrator")) == 2, "the goal question was asked"
    assert not replay.result.completed and "RFT1-1" in replay.result.error, replay.result.error
    # The goal was confirmed; the requirements check then refuses MET over
    # the review finding its designated judge never closed.
    assert replay.result.error.startswith("RequirementsUnmet"), replay.result.error
    assert "not closed by their designated judge" in replay.result.error
    sealed = checkpoint.load(Path(replay.result.run_dir))
    assert sealed["sealed"] and [c["site"] for c in checkpoint._decode(sealed["state"])["close_decisions"]] == ["task", "cap"]


@pytest.mark.requirements_ledger
def test_a_raised_child_still_stops_the_batch_under_the_shared_policy(tmp_path, monkeypatch):
    failing = H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8, error="permission denied: write README.md")
    replay = H.run(tmp_path, monkeypatch, _batch_script(failing), files=FILES, max_tasks=4, record_complete=False)
    close = next(c for c in _closes(replay) if c["site"] == "batch")
    assert close["stop"] == "FindingsOpen" and not close["holds"]
    assert "was not merged" in close["reasons"][0] and len(replay.of("orchestrator")) == 1


@pytest.mark.requirements_ledger
def test_a_capped_child_beside_a_clean_child_continues_under_the_shared_policy(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _batch_script(GROK_CAP), files=FILES, max_tasks=4, record_complete=False)
    close = next(c for c in _closes(replay) if c["site"] == "batch")
    assert close["stop"] is None and len(replay.of("orchestrator")) >= 3


# -- 4. the hold fails closed on a lost typed record; deferrals keep their separators --------------

def test_the_review_debt_hold_needs_the_typed_record_in_both_directions(tmp_path, monkeypatch):
    """Root's probe, 2026-10-26: a held legacy finding with no typed fact
    behind it passed a vacuous all(); the hold now needs positive
    correspondence both ways."""
    from quadratus.outcome import TaskOutcome
    session, _ = parallel_session(tmp_path, Orchestrated(["DONE"]), requirements_ledger=True)
    monkeypatch.setattr(type(session), "_discovering_checks", lambda self: [object()])
    text = "Task t1: RFT1-1: UNRESOLVED: nothing tests negative numbers"
    session.open_findings = [text]
    session.unresolved_reviews = [dict(task="t1", peer="claude:opus", finding="BLOCKING: nothing tests negative numbers",
                                       text=text, finding_id="RFT1-1")]
    assert session._review_debt_hold() == "", "legacy only: the typed record is lost, so nothing holds"
    outcome = TaskOutcome(task_id="t1", intent="implement add")
    outcome.note("unverified", text)
    session.task_outcomes.append(outcome)
    assert session._review_debt_hold().startswith("1 open finding(s) are held review findings")
    session.open_findings.append("Parallel task t2 was not merged (ProviderError: x); its files are kept in artifact a1.")
    assert session._review_debt_hold() == "", "a finding that is not a held review finding breaks the hold"


@pytest.mark.parametrize("line, recorded", [
    ("DEFERRED: t2 — sibling function must reject invalid input", True),
    ("DEFERRED: t2 - sibling function must reject invalid input", True),
    ("DEFERRED: t2: sibling function must reject invalid input", True),
    ("DEFERRED: t2 —", False),
    ("DEFERRED: t9 — sibling function must reject invalid input", False),
])
def test_a_deferral_keeps_its_condition_with_any_normal_separator(tmp_path, line, recorded):
    """Root's probe, 2026-10-26: an em dash after the sibling id dropped the
    deferred condition in silence. A blank condition or an unknown sibling
    is no deferral."""
    session, _ = parallel_session(tmp_path, Orchestrated(["DONE"]))
    session._batch_siblings = [("t2", ["b.py"], "implement sibling function")]
    spec = TaskSpec(task_id="t1", description="implement a.py")
    session._record_deferred(spec, "openai:sol", line)
    entries = [e for e in session.unresolved_reviews if e.get("deferred")]
    if recorded:
        assert len(entries) == 1 and entries[0]["deferred"] == "t2"
        assert "sibling function must reject invalid input" in entries[0]["text"]
        assert session.open_findings and "deferred by openai:sol to parallel task t2" in session.open_findings[0]
    else:
        assert entries == [] and session.open_findings == []
