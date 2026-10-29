"""The PROVES declaration: the rule every writing and judging seat sees,
and the measured reason that comes back when a line is malformed.

The settings-migration run (2026-10-21): the lead's P11 first named a test
class, then a comma-separated case list; the harness rightly rejected both
and the reviewer asked for source revisions, the second of which hit the
vendor's 429. The grammar is unchanged and strict. What changed: the rule
is stated once (scope.PROVES_RULE) in the lead's packet, the revision
prompt and the reviewer's proof rows; the measured fault reaches the
revision with its reason; and a declaration fault alone asks for the
corrected lines, not rewritten code or tests. The same run's independent
source finding (a standard-library-only test that forbade every import
and looped over nothing) is the test rule (scope.TEST_SUBSTANCE_RULE)
beside it. Real Session with scripted replies; every claim is a record."""
import json

import pytest

from quadratus.scope import PROVES_RULE, TEST_SUBSTANCE_RULE
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_proof_binding import (
    CASE,
    NEGATIVE,
    _acceptance,
    _at,
    _default_auditor,
    _lead,
    _plan,
    _proofs,
    _scope,
)

BLOCK = ("BLOCKING: P1 is declared as a class, not a case; redeclare it | basis: P1 | impact: correctness\n"
         "R1: MET - app.py")
NEG_TESTS = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
             "def test_negative():\n    assert add(-1, -2) == -3\n")


def _run(tmp_path, monkeypatch, name, lines, *, collaborator=None, revision=None, tests=None):
    script = Script(orchestrator=lambda c, r: _plan(_scope(proofs=_proofs("R1"))) if len(r.of("orchestrator")) == 1 else "DONE",
                    lead=_lead(lines=lines, tests=tests),
                    collaborator=collaborator or (lambda c, r: "NO FINDINGS\nR1: MET - app.py"),
                    auditor=_default_auditor, **{"requirements-review": lambda c, r: "COMPLETE"})
    if revision is not None:
        script.overrides["revision"] = revision
        script.overrides["recheck"] = lambda c, r: "RFT1-1: RESOLVED redeclared"
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("line, reason", [
    ("PROVES: P1 - tests/test_app.py::TestAdd", "names a test class or object, not a test case"),
    (f"PROVES: P1 - {CASE}, {NEGATIVE}", "lists 2 items on one line (comma-separated)"),
    (f"PROVES: P1 - {CASE},{NEGATIVE}", "lists 2 items on one line (comma-separated)"),
    ("PROVES: P1 - tests/test_app.py", "names a file, not a test case"),
    ("PROVES: P1 - the add cases", "names no single test case"),
])
def test_a_malformed_line_is_rejected_with_the_measured_reason_the_correction_needs(tmp_path, monkeypatch, line, reason):
    replay = _run(tmp_path, monkeypatch, "shape", [line])
    row = _acceptance(replay)[0]
    assert row["binding"].startswith("invalid declaration: ") and reason in row["binding"]
    assert row["cases"] == [] and row["execution"] == "missing" and not replay.result.completed
    problems = H.result_json(replay)["proof_binding_problems"]
    assert len(problems) == 1 and problems[0]["problem"].startswith("P1: ") and reason in problems[0]["problem"]
    assert "one test case per PROVES line" in problems[0]["problem"] or "per PROVES line" in problems[0]["problem"]


@pytest.mark.requirements_ledger
def test_every_writing_and_judging_seat_sees_the_rule_and_the_reviewer_sees_the_measured_fault(tmp_path, monkeypatch):
    seen = {}

    def revision(call, replay):
        seen["revision"] = call.prompt
        return f"Redeclared.\nPROVES: P1 - {CASE}\nCHANGED: []"

    def collaborator(call, replay):
        seen["reviewer"] = call.prompt
        return BLOCK
    replay = _run(tmp_path, monkeypatch, "seats", ["PROVES: P1 - tests/test_app.py::TestAdd"],
                  collaborator=collaborator, revision=revision)
    lead = replay.of("lead")[0].prompt
    assert PROVES_RULE in lead and TEST_SUBSTANCE_RULE in lead
    reviewer = seen["reviewer"]
    assert PROVES_RULE in reviewer and TEST_SUBSTANCE_RULE in reviewer
    assert "binding invalid declaration: 'tests/test_app.py::TestAdd' names a test class" in reviewer
    assert "asks for the corrected PROVES line(s), one exact case per line, and nothing more" in reviewer
    assert "A case that asserts nothing, cannot fail, or forbids what the requirement permits is a BLOCKING finding" in reviewer
    prompt = seen["revision"]
    assert "## Proof binding" in prompt and PROVES_RULE in prompt and TEST_SUBSTANCE_RULE in prompt
    assert ("The harness measured these PROVES lines of your delivery invalid" in prompt
            and "- P1: 'tests/test_app.py::TestAdd' names a test class or object, not a test case" in prompt)
    assert "it calls for no code or test rewrite" in prompt
    # The corrected line alone, with no edit, binds and the check discharges it.
    row = _acceptance(replay)[0]
    assert row["binding"] == "bound" and row["cases"] == [CASE] and row["execution"] == "ran"
    assert [h.get("invalid") is not None for h in row["binding_history"]] == [True, False]
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_scenario_shown_by_two_cases_takes_two_lines_and_binds_both(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, "two", [f"PROVES: P1 - {CASE}", f"PROVES: P1 - {NEGATIVE}"], tests=NEG_TESTS)
    row = _acceptance(replay)[0]
    assert row["binding"] == "bound" and row["cases"] == [CASE, NEGATIVE] and row["discharged_cases"] == [CASE, NEGATIVE]
    assert H.result_json(replay)["proof_binding_problems"] == []


@pytest.mark.requirements_ledger
def test_a_valid_declaration_gets_the_rule_but_no_measured_fault_in_its_revision(tmp_path, monkeypatch):
    seen = {}

    def revision(call, replay):
        seen["revision"] = call.prompt
        H.write(call, {"tests/test_app.py": NEG_TESTS})
        return f"Added the negative case.\nPROVES: P1 - {CASE}\nPROVES: P1 - {NEGATIVE}\nCHANGED: {json.dumps(['tests/test_app.py'])}"
    replay = _run(tmp_path, monkeypatch, "valid", [f"PROVES: P1 - {CASE}"],
                  collaborator=lambda c, r: "BLOCKING: no negative case | basis: R1 | impact: correctness\nR1: MET - app.py",
                  revision=revision)
    prompt = seen["revision"]
    assert "## Proof binding" in prompt and PROVES_RULE in prompt
    assert "measured these PROVES lines" not in prompt and "calls for no code or test rewrite" not in prompt
    assert _acceptance(replay)[0]["cases"] == [CASE, NEGATIVE] and replay.result.completed, replay.result.error


def test_a_task_without_proof_scenarios_carries_no_test_rule_in_its_lead_prompt(tmp_path, monkeypatch):
    """A legacy declaration (no requirements ledger, no scenarios): the
    rules are tied to declared proof scenarios and appear nowhere else."""
    script = Script(orchestrator=lambda c, r: _plan(_scope()) if len(r.of("orchestrator")) == 1 else "DONE",
                    lead=_lead(lines=[]), collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py",
                    auditor=lambda c, r: f"R1: MET - {CASE}", **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(_at(tmp_path, "legacy"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert TEST_SUBSTANCE_RULE not in replay.of("lead")[0].prompt and PROVES_RULE not in replay.of("lead")[0].prompt
