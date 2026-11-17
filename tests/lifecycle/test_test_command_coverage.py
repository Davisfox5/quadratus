"""Cycle08 run 20261108T121411Z-720c5d46 (root's assessment, PR73
6721926382):

- R17, "All existing tests keep passing and `python3 -m unittest discover -s
  tests` passes on the delivered source", was read as a behaviour for the word
  "keep" (``_PASS_ONLY_WORDS``). t6 was sent back ("COVERS names R17 but no
  proof scenario shows it"), the re-plan cost 148,417 reported tokens, and
  the planner dropped R17 and R21 from COVERS.
- At DONE both were "not covered by any finished task" while the gate's own
  unittest run on the delivered source was passing, and t7's terminal route
  was 95,181 tokens short.

Now ordinary grammatical variants of a pure suite-pass claim are read as one,
a mixed claim's refusal names what else it claims, and a pure test-command
requirement no task covered is covered for the independent audit by the
harness's own exact receipt, never by a missing, stale, failed, filtered or
skipping one. The MET stays the audit's.

Real Session and controller with scripted providers and the project's own
pytest gate, and units on a hand-set record."""
from types import SimpleNamespace

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import Seen, _script

pytestmark = pytest.mark.requirements_ledger

R17 = "All existing tests keep passing and `python3 -m unittest discover -s tests` passes on the delivered source."
R21 = "Run `python3 -m unittest discover -s tests` on the delivered source before declaring completion."


def _session(tmp_path, requirements):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements)
    return session


@pytest.mark.parametrize("text, pure", [
    (R17, True),
    (R21, True),
    ("The suite still passes under `pytest -q`", True),
    ("`python -m unittest discover` continues to pass after each change", True),
    ("All tests keep passing with `pytest -q` and the final run is clean", True),
    ("Existing tests remain green: `python3 -m unittest discover -s tests` passes", True),
    # Mixed, selected or another command: the scenario stays.
    ("`python -m unittest` passes, existing tests are unchanged and existing behaviour is preserved", False),
    ("`pytest -q tests/refunds.py` passes and refunds are atomic when the bank rejects a transfer", False),
    ("Add tests for malformed JSON returning 400; run `pytest -q` before completion", False),
    ("`python -m pytest -k refunds` keeps passing", False),
    ("`python app.py pytest` keeps printing the runner name", False),
])
def test_a_pure_suite_pass_claim_is_read_in_its_ordinary_grammatical_forms(tmp_path, text, pure):
    session = _session(tmp_path, {"R1": text})
    assert session._test_command_requirement("R1") is pure, text


def test_r17s_task_is_admitted_and_a_mixed_one_is_told_what_else_it_claims(tmp_path):
    session = _session(tmp_path, {"R1": "add(a, b) returns the sum", "R17": R17, "R21": R21,
                                  "R30": "`pytest -q` passes and the export stays byte-identical"})
    proof = dict(id="P1", requirements=["R1"], operation="add(1, 2)", state="nothing", expect="3",
                 location="tests/test_app.py")
    spec = SimpleNamespace(scope=SimpleNamespace(proofs=[proof], review_only=False), task_id="t6")
    assert session._proof_problem(spec, ["R1", "R17", "R21"]) == "", "no sendback for the observed t6 shape"
    problem = session._proof_problem(spec, ["R1", "R30"])
    assert "COVERS names R30 but no proof scenario shows it" in problem
    assert "R30 names a test command but also claims 'export byte identical'" in problem, problem
    assert "keep the requirement in COVERS" in problem


# -- coverage from the harness's own exact receipt -------------------------------------------------

def test_an_uncovered_pure_suite_claim_is_covered_only_by_an_exact_current_receipt(tmp_path):
    session = _session(tmp_path, {"R17": R17, "R20": "Add meaningful tests for the dependency workflow",
                                  "R30": "`pytest -q` passes and the export stays byte-identical"})
    receipt = dict(id="check-3", receipt="check", command="python3 -m unittest discover -s tests", source="S",
                   gate="G")
    session._command_receipt_citation = lambda rid: (receipt, "")
    status = session.memory.ledger.requirement_status
    status["R30"] = "open"
    session._cover_by_receipt({"R17", "R20", "R30"})
    assert status["R17"].startswith("covered (harness receipt check-3")
    assert "R20" not in status and status["R30"] == "open", "a behaviour or a mixed claim is never receipt-covered"
    assert session.receipt_coverage == [dict(requirement="R17", receipt="check-3", command=receipt["command"],
                                             source="S", gate="G")]
    # A NOT MET from an audit is the planner's to act on, never re-covered.
    status["R17"] = "NOT MET: the auditor's reason"
    session._cover_by_receipt({"R17"})
    assert status["R17"].startswith("NOT MET") and len(session.receipt_coverage) == 1


@pytest.mark.parametrize("problem", [
    "the harness has no receipt for `python3 -m unittest discover -s tests`",
    "the receipt for `python3 -m unittest discover -s tests` does not count: it ran on another source",
    "the receipt for `python3 -m unittest discover -s tests` does not count: its run failed",
    "the receipt for `python3 -m unittest discover -s tests` does not count: it skipped 2 case(s)",
    "the receipt for `python3 -m unittest discover -s tests` does not count: it ran a selection, not the whole command",
])
def test_a_missing_stale_failed_skipping_or_partial_receipt_covers_nothing(tmp_path, problem):
    session = _session(tmp_path, {"R17": R17})
    session._command_receipt_citation = lambda rid: (None, problem)
    session._cover_by_receipt({"R17"})
    assert "R17" not in session.memory.ledger.requirement_status and session.receipt_coverage == []


# -- lifecycle -------------------------------------------------------------------------------------

SUITE = "All existing tests keep passing and `python -m pytest -q` passes on the delivered source."


def _run(tmp_path, monkeypatch, name, suite):
    plan = _plan(_scope(proofs=_proofs("R1")), requirements=f"R1: add(a, b) returns the sum\nR2: {suite}")
    auditor = judging_scenarios(lambda c, r: f"R1: MET - {CASE}\nR2: MET - the harness's own complete run of "
                                             "that command passed on the delivered source", PROOF_R1, basis=SHOWN)
    replies = iter([plan, "DONE", "DONE", "DONE"])
    script = _script(Seen(), orchestrator=lambda c, r: next(replies, "DONE"), auditor=auditor)
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)


def test_a_suite_claim_no_task_covered_reaches_the_audit_on_the_gates_own_receipt(tmp_path, monkeypatch):
    """The planner covered only R1 (as t6 did after its sendback). R2 is
    covered for the audit by the gate's exact receipt of `python -m pytest -q`
    on the delivered source; the auditor judges it MET and the run completes.
    On d38f6b4 DONE was sent back with R2 "not covered"."""
    replay = _run(tmp_path, monkeypatch, "covered", SUITE)
    record = H.result_json(replay)
    (cover,) = record["receipt_coverage"]
    assert cover["requirement"] == "R2" and cover["command"] == "python -m pytest -q"
    assert cover["source"] == record["source_fingerprint"]
    audit = record["requirements"]["audits"][-1]
    assert audit["verdicts"]["R2"]["met"] and "R2" in audit["citation_receipts"], audit
    assert [c.task for c in replay.of("lead")] == ["t1"], "no task was spent on it"
    assert not any("not covered by any finished task" in c.prompt for c in replay.of("orchestrator"))
    assert replay.result.completed, replay.result.error


def test_a_suite_claim_the_gate_does_not_run_stays_uncovered(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, "absent",
                  "All existing tests keep passing and `python3 -m unittest discover -s tests` passes.")
    record = H.result_json(replay)
    assert record["receipt_coverage"] == []
    assert any("not covered by any finished task: R2" in c.prompt for c in replay.of("orchestrator"))
    assert not replay.result.completed
