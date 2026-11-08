"""Cycle05 trial 20261108T050923Z-c63988ae (root's assessment, PR73
6718066718), the engine causes it showed:

1. t6's lead declared ``PROVES: P1 - ...`` in its binding call, and the harness
   settled P1 on its own run; the owning reviewer's recheck was shown only the
   revised work, read P1's PROVES as absent and asked for a truthful NOT RUN to
   be deleted. The recheck now carries the declaration, its provenance and the
   harness's run as routing facts. The finding stays the reviewer's.
2. R24/R28 asked that the suite pass under unittest; t5/t6 built a test that
   ran the whole suite inside itself. A requirement whose claim is that a named
   test command passes needs no scenario: the harness's exact receipt is cited
   and the audit judges it.
3. At the task cap the final check never reached the DONE declaration route.
4. An unknown-ID 404 requirement never reached the 2**63 boundary candidate,
   and the audit read a JSON-body requirement against HEAD.

Real Session and controller with scripted providers and the project's own
pytest gate, plus units on a hand-set record."""
from types import SimpleNamespace

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_binding_settlement import _run as _binding_run
from tests.lifecycle.test_done_declarations import CLEAN, _auditor, _declaring, _run, _then_done
from tests.lifecycle.test_proof_binding import CASE
from tests.lifecycle.test_reachable_evidence import _item, _lead
from tests.lifecycle.test_workflow_repair import Seen, _script

pytestmark = pytest.mark.requirements_ledger


# -- 1. the owning reviewer is shown the lead's later declaration ---------------------------------

def test_the_recheck_is_shown_the_leads_declaration_its_provenance_and_the_harness_run(tmp_path, monkeypatch):
    seen, calls, replay = _binding_run(tmp_path, monkeypatch, "declared", binding_reply=f"PROVES: P1 - {CASE}",
                                       recheck=lambda c, r: "RFT1-1: RESOLVED observed-1 shows test_add ran")
    (prompt,) = seen.prompts["recheck"]
    facts = prompt.split("Harness routing facts for your findings:", 1)[1].split("The revised work:", 1)[0]
    assert f"'PROVES: P1 - {CASE}'" in facts and "read-only declaration call on this exact source" in facts
    route = H.result_json(replay)["evidence_routes"][-1]
    assert route["declared"] == [f"PROVES: P1 - {CASE}"] and route["declaration_artifact"]
    assert f"artifact {route['declaration_artifact']}" in facts
    assert "execution ran (receipt observed-1)" in facts, facts
    assert "the lead's own working copy, not the harness's approved execution" in facts
    assert "your finding stays yours to settle" in facts
    finding = H.result_json(replay)["review_findings"][0]
    assert finding["history"][-1]["author"] == finding["author"], "the owner settled it, not the harness"


def test_a_declaration_that_bound_nothing_is_not_shown_as_one(tmp_path, monkeypatch):
    seen, calls, replay = _binding_run(tmp_path, monkeypatch, "nothing", binding_reply="I could not find the test.",
                                       recheck=lambda c, r: "RFT1-1: UNRESOLVED still unbound")
    assert all("read-only declaration call" not in p for p in seen.prompts.get("recheck", []))
    finding = H.result_json(replay)["review_findings"][0]
    assert "P1 binds no case" in finding["claim"] and not finding["closed"], "held for its owner, nothing shown"


# -- 2. a test command's pass needs no test of the tests -------------------------------------------

def _session(tmp_path, requirements):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements)
    return session


REQS = {"R1": "add(a, b) returns the sum",
        "R24": "the whole suite passes under `python -m unittest discover`",
        "R25": "pytest -q passes on the delivered source",
        "R26": "`npm test` passes",
        "R5": "`python -m app serve` starts the server and GET /tickets returns 200"}


def test_only_a_test_runner_command_is_a_test_command_requirement(tmp_path):
    session = _session(tmp_path, REQS)
    assert [r for r in REQS if session._test_command_requirement(r)] == ["R24", "R25", "R26"]


#: Root's review of 63fbef7 (PR73 6718399838): a runner name anywhere in argv
#: exempted these behaviours from their scenarios.
ROOT_BEHAVIOURS = [
    "`pytest -q tests/refunds.py` passes and refunds are atomic when the bank rejects a transfer",
    "`python app.py pytest` prints the requested test runner name and exits zero",
    "Add tests for malformed JSON returning 400; run `pytest -q` before completion",
]
OTHER_BEHAVIOURS = [
    "`python -m app serve` starts the server and GET /tickets returns 200",
    "`python -m pytest -k refunds` passes",
    "`pytest` and `npm test` pass",
    "tests are written with unittest",
    "python app.py pytest prints the runner name",
]
PASS_ONLY = [
    "the whole suite passes under `python -m unittest discover`",
    "pytest -q passes on the delivered source",
    "`npm test` passes",
    "Run `pytest -q` before declaring completion",
    "The project's tests run with `python3 -m unittest discover -s tests` and pass",
    "All tests pass when run with `python -m pytest`",
]


@pytest.mark.parametrize("text, exempt", [(t, False) for t in ROOT_BEHAVIOURS + OTHER_BEHAVIOURS]
                         + [(t, True) for t in PASS_ONLY])
def test_only_a_whole_claim_that_a_test_runner_passes_is_exempt_from_its_scenario(tmp_path, text, exempt):
    session = _session(tmp_path, {"R1": text})
    assert session._test_command_requirement("R1") is exempt, text
    spec = SimpleNamespace(scope=SimpleNamespace(proofs=None, review_only=False), task_id="t5")
    problem = session._proof_problem(spec, ["R1"])
    assert (problem == "") is exempt, (text, problem)
    if not exempt:
        assert "declares no proof scenarios" in problem, "the behaviour keeps its scenario mapping"


def test_a_runner_shape_is_the_runner_run_whole():
    from quadratus.session import _test_runner_shape
    assert _test_runner_shape(["python3.12", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])
    assert _test_runner_shape(["pytest", "-q", "tests/test_app.py"]) and _test_runner_shape(["make", "test"])
    for argv in (["python", "app.py", "pytest"], ["python", "-m", "pytest", "-k", "refunds"],
                 ["npm", "test", "--", "--grep", "x"], ["npm", "run", "build"], ["python", "-m", "app"], []):
        assert not _test_runner_shape(argv), argv


def test_a_test_command_requirement_maps_to_no_scenario_and_a_behaviour_still_does(tmp_path):
    from quadratus.session import _SCOPE_REQUEST, _TEST_COMMAND_RULE
    session = _session(tmp_path, REQS)

    def spec(proofs):
        return SimpleNamespace(scope=SimpleNamespace(proofs=proofs, review_only=False), task_id="t5")
    proof = dict(id="P1", requirements=["R1"], operation="add(1, 2)", state="nothing", expect="3",
                 location="tests/test_app.py")
    assert session._proof_problem(spec(None), ["R24"]) == "", "the receipt is its evidence"
    assert session._proof_problem(spec([proof]), ["R1", "R24"]) == ""
    assert "declares no proof scenarios" in session._proof_problem(spec(None), ["R24", "R5"])
    assert "COVERS names R5" in session._proof_problem(spec([proof]), ["R1", "R5"])
    assert _TEST_COMMAND_RULE in _SCOPE_REQUEST, "the planner is told the rule the mapping applies"
    assert "Never write a test that runs the test suite or that command" in _TEST_COMMAND_RULE
    assert "stays not met when the configured checks do not run that command" in _TEST_COMMAND_RULE


# -- 3. the task cap reaches the declaration route -------------------------------------------------

def test_at_the_task_cap_held_declaration_debt_is_offered_to_its_owner_before_the_final_audit(tmp_path,
                                                                                               monkeypatch):
    """One slot: t1 closes with its production-path NOT RUN item, the cap
    asks the goal question, and the final check offers the item to t1's lead
    (one bounded call, no slot) before the audit judges it."""
    seen = Seen()
    script = _script(seen, collaborator=lambda c, r: CLEAN, orchestrator=_then_done(), lead=_lead(),
                     auditor=_auditor())
    script.overrides["proof-binding"] = _declaring()
    replay = _run(tmp_path, monkeypatch, "cap", script, max_tasks=1)
    record = H.result_json(replay)
    (decl,) = record["done_declarations"]
    assert decl["owner"] == "t1" and decl["outcome"] == "declared" and decl["settled"] == [_item(replay)["oid"]]
    (gate,) = [g for g in record["done_gates"] if g["site"] == "cap"]
    assert "asked t1; still held: none" in gate["declaration"]
    roles = [c.role for c in replay.calls]
    assert roles.index("proof-binding") < roles.index("auditor"), "declared before the final audit"
    assert [c.task for c in replay.of("lead")] == ["t1"], "no slot was added"
    assert replay.result.completed, replay.result.error
    # 4b. the protocol's own semantics are stated where requirements are judged.
    from quadratus.session import _PROTOCOL_RULE
    assert _PROTOCOL_RULE in replay.of("auditor")[0].prompt and _PROTOCOL_RULE in replay.of("collaborator")[0].prompt


def test_at_the_cap_an_unsettleable_set_records_why_once_and_asks_no_one(tmp_path):
    session = _session(tmp_path, {"R1": "add"})
    session._deterministic_blockers = lambda: {"R1": "NOT RUN"}
    session._done_declarations = lambda blocked: (None, "a blocker needs a code or test change")
    session._source_fingerprint = lambda: "S"
    session._declare_at_cap()
    session._declare_at_cap()
    assert [(g["site"], g["declaration"]) for g in session.done_gates] == [
        ("cap", "a blocker needs a code or test change")]


# -- 4. representation boundaries reach the review early -------------------------------------------

@pytest.mark.parametrize("text, error", [
    ("GET /tickets/<id> returns 404 for an unknown ticket", True),
    ("a ticket that does not exist responds with 404 and a JSON error", True),
    ("GET /tickets/<id> returns the stored ticket", False),
    ("add(a, b) returns the sum of two numbers", False),
])
def test_a_returned_status_names_an_error_requirement_and_reaches_the_int64_candidate(text, error):
    from quadratus.session import _ERROR_BOUNDARY_LENS, _error_clause, boundary_classes
    assert _error_clause(text) is error
    rows = boundary_classes([("R1", text)])
    assert bool(rows) is (error and "id" in text)
    if rows:
        assert any("9223372036854775808" in c for c in rows[0][1])
        assert any("-9223372036854775809" in c for c in rows[0][1]), "both edges of the signed 64-bit store"
    assert "an integer wider than a storage column or a database binding" in _ERROR_BOUNDARY_LENS

