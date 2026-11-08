"""Cycle07 run 20261108T091024Z-75154efd (root's assessment, PR73
6720016289): R23, "README documents the new Store methods, HTTP endpoints and
CLI commands, including the refusal rules, without narrowing the contract",
is a documentation claim, but every reading proof for it was refused because
``_error_clause`` matched "refusal"; the last task was sent back until the
correction budget stopped the run. The audit and the review hold read the
same word and would have demanded an executed case for a README.

A documentation assertion about behaviour is not the behaviour
(``_behaviour_text``, ``_behavioural_error``): the object of a clause whose
subject is documentation the work writes is what that text says. Behaviour
coordinated after it, behaviour outside it, program output and passive
phrasing stay behaviour.

Real Session and controller with scripted providers and the project's own
pytest gate, and units on each consumer."""
from types import SimpleNamespace

import pytest

from quadratus.session import _behaviour_text, _behavioural_error, _error_clause, boundary_classes
from tests.lifecycle import harness as H
from tests.lifecycle.test_finish_capacity import _script
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_step2_recovery import _simple

pytestmark = pytest.mark.requirements_ledger

R23 = ("README documents the new Store methods, HTTP endpoints and CLI commands, including the refusal rules, "
       "without narrowing the contract.")
DOCS = "README documents add, including the refusal rules for non-numbers, without narrowing the contract"
PLAN_1 = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: " + DOCS + "\n"
          + _simple("app.py", "Implement add in app.py.", "R1"))
PLAN_2 = _simple("README.md", "Document add and its refusal rules in README.md.", "R2")


@pytest.mark.parametrize("text, behavioural", [
    (R23, False),
    (DOCS, False),
    ("docs/api.md lists every error code", False),
    ("The README states that invalid input returns 400", False),
    ("Document rejection and reject invalid input without mutation", True),
    ("Invalid input is rejected and the README documents it", True),
    ("README documents the CLI. The CLI rejects malformed JSON with exit status 2.", True),
    ("README documents the CLI; the API rejects malformed JSON", True),
    ("Refusal rules are documented in the README", True),
    ("The CLI --help explains the refusal rules", True),
    ("GET /tickets/<id> returns 404 for an unknown ticket", True),
    ("docs/errors.md lists every error code", False),
    # Root review of 06c708d (PR73 6720550674) and nearby unseen forms: a
    # predicate after a boundary inside a documentation clause stays behaviour.
    *[(t, True) for t in [
        "README documents rejection and API rejects invalid IDs without mutation",
        "README documents rejection and the HTTP API rejects invalid IDs without mutation",
        "README documents rejection, while the service rejects invalid IDs without mutation",
        "Document the error codes and ensure invalid IDs are rejected without mutation",
        "README documents the refusal rules; malformed IDs must be refused",
        "The README explains the error codes, but the CLI exits non-zero on invalid input",
    ]],
    # The conservative cost, stated: a listed item that itself names error
    # vocabulary after "and" is kept as possible behaviour ("including ..."
    # is the unambiguous documentation form).
    ("README documents the refusal rules and the error codes", True),
    ("README documents the refusal rules, including the error codes", False),
])
def test_a_documentation_assertion_about_behaviour_is_not_the_behaviour(text, behavioural):
    assert _error_clause(text), "every case names error language somewhere"
    assert _behavioural_error(text) is behavioural, _behaviour_text(text)


ROOT_MIXED = [
    "README documents rejection and API rejects invalid IDs without mutation",
    "README documents rejection and the HTTP API rejects invalid IDs without mutation",
    "README documents rejection, while the service rejects invalid IDs without mutation",
    "Document the error codes and ensure invalid IDs are rejected without mutation",
    "The README explains the error codes, but the CLI exits non-zero on an invalid ID",
]


@pytest.mark.parametrize("text", ROOT_MIXED)
def test_a_mixed_requirement_keeps_its_runtime_obligation_in_every_consumer(tmp_path, text):
    """Root's four controls on 22de5f9, and one unseen form: a reading proof
    is refused, an uncited MET is lowered, and the boundary candidates apply."""
    session = _session(tmp_path, {"R1": text})
    problem = session._proof_problem(_spec(_reading("P1", ["R1"])), ["R1"])
    assert "declares reading evidence for R1," in problem, problem
    session._executed_cases = lambda cited, rid=None: []
    found = {"R1": (True, "README.md documents it")}
    assert [x["requirement"] for x in session._lower_unexercised_errors(found, {"R1": text})] == ["R1"]
    assert found["R1"][0] is False
    assert boundary_classes([("R1", text)]), "the integer/identifier candidates still reach its lead and reviewer"


def test_the_boundary_candidates_follow_the_behaviour_not_the_documentation():
    assert boundary_classes([("R1", "README documents that an unknown ticket id returns 404")]) == []
    assert boundary_classes([("R1", "GET /tickets/<id> returns 404 for an unknown ticket")])


# -- admission ------------------------------------------------------------------------------------

def _session(tmp_path, requirements):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements)
    return session


def _reading(pid, rids, location="README.md"):
    return dict(id=pid, requirements=list(rids), operation="read README.md", state="the delivered README",
                expect="the documented rules", location=location, evidence="reading")


def _spec(*proofs):
    return SimpleNamespace(scope=SimpleNamespace(proofs=list(proofs), review_only=False), task_id="t5")


REQS = {"R23": R23,
        "R7": "The API rejects malformed JSON with 400 and leaves the store unchanged",
        "R8": "Document rejection and reject invalid input without mutation",
        "R9": "README documents add(a, b)"}


def test_a_reading_proof_for_a_documentation_claim_is_admitted(tmp_path):
    session = _session(tmp_path, REQS)
    assert session._proof_problem(_spec(_reading("P5", ["R23"])), ["R23"]) == ""


@pytest.mark.parametrize("rids, named, kept", [
    (["R7"], "R7", None),
    (["R8"], "R8", None),
    (["R23", "R7"], "R7", "R23"),
])
def test_behaviour_in_a_reading_proof_is_refused_by_name_with_the_modality_that_fits(tmp_path, rids, named, kept):
    session = _session(tmp_path, REQS)
    problem = session._proof_problem(_spec(_reading("P5", rids)), rids)
    assert f"declares reading evidence for {named}," in problem and "(evidence test)" in problem, problem
    if kept:
        assert f"keep reading evidence for the documentation claim of {kept}" in problem
    if named == "R8":
        assert "reject invalid input without mutation" in problem, "the behavioural part is quoted"


def test_a_source_file_is_never_reading_evidence(tmp_path):
    session = _session(tmp_path, REQS)
    problem = session._proof_problem(_spec(_reading("P5", ["R23"], location="store.py")), ["R23"])
    assert "not a documentation file" in problem


# -- the audit and the review hold ---------------------------------------------------------------

def test_the_audit_lowers_an_uncited_met_only_for_behaviour(tmp_path):
    session = _session(tmp_path, REQS)
    session._executed_cases = lambda cited, rid=None: []
    found = {"R23": (True, "README.md documents every method and the refusal rules"),
             "R8": (True, "README.md documents rejection")}
    lowered = session._lower_unexercised_errors(found, {"R23": R23, "R8": REQS["R8"]})
    assert found["R23"][0] is True and [x["requirement"] for x in lowered] == ["R8"]


def test_the_review_hold_keeps_a_documentation_met_and_holds_a_behaviour_met(tmp_path):
    session = _session(tmp_path, REQS)
    session._covered_texts = lambda: [("R23", R23), ("R7", REQS["R7"])]
    session._executed_cases = lambda cited, rid=None: []
    session._last_scratch_proof = None
    note = "NO FINDINGS\nR23: MET - README.md\nR7: MET - api.py:40\nREVIEW: COMPLETE"
    held = session._hold_unexercised_errors(SimpleNamespace(task_id="t5"), "openai:sol", note,
                                            {"R23": True, "R7": True})
    assert "R23: MET - README.md" in held and "R7: UNVERIFIED" in held


# -- lifecycle: the documentation task is admitted, written, read and judged ------------------------

README = ("# app\n\n`add(a, b)` returns the sum of two numbers.\n\n## Refusal rules\n\n"
          "`add` refuses a non-number with `TypeError`; nothing is stored.\n")


def test_a_readme_about_refusals_is_planned_written_reviewed_and_audited_by_reading(tmp_path, monkeypatch):
    """Before this change the second plan was sent back for the word
    "refusal" in R2, and DONE could not be reached. Now it is dispatched as a
    documentation task with a reading proof, its owner reviews it, the audit
    judges the reading row from the file, and no test is written for R2."""
    script = _script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md")
    lead = script.overrides["lead"]

    def writer(call, replay):
        if call.task == "t2":
            H.write(call, {"README.md": README})
            return 'Documented add and its refusal rules.\nCHANGED: ["README.md"]'
        return lead(call, replay)
    script.overrides["lead"] = writer
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert not any("declares reading evidence for" in c.prompt for c in replay.of("orchestrator"))
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    record = H.result_json(replay)
    (row,) = [o for o in record["evidence_obligations"] if o.get("task") == "t2"]
    assert row["modality"] == "reading" and row["location"] == "README.md" and row["sufficiency"] == "sufficient"
    assert record["requirements"]["status"]["R2"].startswith("met"), record["requirements"]["status"]
    assert (replay.project / "README.md").read_text() == README
    assert not any(p.startswith("tests/") for p in record.get("unnamed_test_files") or [])
    assert replay.result.completed, replay.result.error


def test_the_planner_and_the_judges_are_told_the_rule_the_consumers_apply():
    from quadratus.session import _EXECUTED_ERROR_RULE, _SCOPE_REQUEST
    assert "one that only asks documentation to describe that behaviour is a documentation claim" in " ".join(
        _SCOPE_REQUEST.split())
    assert "behaviour beside it in the same requirement takes its own executed scenario" in _SCOPE_REQUEST
    assert "only asks documentation to describe errors or refusals is a documentation claim" in _EXECUTED_ERROR_RULE
