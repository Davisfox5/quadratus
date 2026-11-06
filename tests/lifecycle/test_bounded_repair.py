"""Bounded repair with verified outcomes (Davis, 2026-11-06, PR73
6693873055), whole runs below the vendor CLIs with the real pytest gate.

The old rule stopped every failure that was not a plain assertion with zero
repair calls. acceptance-routing-01 stopped that way on a generated fixture
whose class-wide mock broke its own state read, with 3.2M tokens left. Now
a failure buys the task's bounded repair unless no edit to the project can
change it, and the existing protections judge the result: run-start tests
restored and run, scope, declared scenarios bound to executed cases, and the
original reviewer comparing any generated test the repair changed.

The project is the generic store of test_boundary_regressions; nothing here
is the ticket application. Fault injection targets the path the action uses,
and the state is read through a handle the injection does not touch.
"""
import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_boundary_regressions import FILES, HAPPY, LEAKING, SOUND, VERBOSE
from tests.lifecycle.test_lifecycle_matrix import Script, judging

R1 = ("Removing an item through the command line reports a storage error as a JSON error object with exit "
      "status 1 and leaves the store unchanged")
FAILURES = "tests/test_store_failures.py"
CASE = f"{FAILURES}::test_a_storage_failure_is_a_json_error_and_changes_nothing"
PROOF = H.proofs_for("R1", FAILURES, operation="remove item 1 through the command line with storage unavailable",
                     state="one stored item", expect="exit status 1, a JSON error, the item kept")[0]
PLAN = ("REQUIREMENTS:\nR1: " + R1 + "\nKIND: backend simple\nSCOPE: "
        + json.dumps({"permitted_paths": ["store.py", "tests/test_store.py", FAILURES],
                      "intended_result": "remove reports storage errors", "acceptance": ["storage errors are JSON"],
                      "max_lines": 260, "proofs": [PROOF]})
        + "\nImplement remove with its storage error and a test.\nCOVERS: R1")
SHOWN = f"the project check executed {CASE} on the delivered source, which keeps the item on a storage error"

_HEAD = '''import json
import sqlite3

import store


def _db(tmp_path):
    path = str(tmp_path / "s.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'a')")
    return path


def _rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT id FROM items").fetchall()


def _unavailable(*args, **kwargs):
    raise sqlite3.OperationalError("unable to open")

'''
#: The generated fixture's own defect: the injection is wider than the path
#: under test, so the test's state read raises before the action runs.
BROKEN = _HEAD + '''
def test_a_storage_failure_is_a_json_error_and_changes_nothing(tmp_path, monkeypatch, capsys):
    path = _db(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", _unavailable)
    before = _rows(path)
    assert store.cli(["1"], path) == 1 and "error" in json.loads(capsys.readouterr().out)
    assert _rows(path) == before
'''
#: The same intent with the injection on the action's own path.
CORRECTED = _HEAD + '''
def test_a_storage_failure_is_a_json_error_and_changes_nothing(tmp_path, monkeypatch, capsys):
    path = _db(tmp_path)
    before = _rows(path)
    monkeypatch.setattr(store, "connect", _unavailable)
    assert store.cli(["1"], path) == 1 and "error" in json.loads(capsys.readouterr().out)
    assert _rows(path) == before
'''
#: A "repair" that removes what the test checked.
WEAKENED = _HEAD + '''
def test_a_storage_failure_is_a_json_error_and_changes_nothing():
    assert True
'''


def _run(tmp_path, monkeypatch, *, source, failures, fixed_source, fixed_failures, comparison=None,
         check=VERBOSE, extra=None):
    def lead(call, replay):
        H.write(call, {"store.py": source, FAILURES: failures, **(extra or {})})
        return (f"Implemented remove.\nPROVES: P1 - {CASE}\n"
                + 'CHANGED: ' + json.dumps(["store.py", FAILURES, *(extra or {})]))

    def gate_fix(call, replay):
        H.write(call, {"store.py": fixed_source, FAILURES: fixed_failures})
        return 'Repaired.\nCHANGED: ["store.py", "' + FAILURES + '"]'

    script = Script(orchestrator=lambda c, r: PLAN if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=lambda c, r: f"NO FINDINGS\nR1: MET - {CASE}",
                    auditor=judging_scenarios(judging(lambda c, r: f"R1: MET - {CASE}"), PROOF, basis=SHOWN),
                    **{"requirements-review": lambda c, r: "COMPLETE",
                       "revision": lambda c, r: "Nothing to change.\nCHANGED: []",
                       "gate-fix": gate_fix,
                       **({"test-comparison": comparison} if comparison else {})})
    return H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False, check=check,
                 goal="Add item removal to the store's command line. " + R1)


@pytest.mark.requirements_ledger
def test_a_combined_fault_is_repaired_once_and_the_reviewer_finds_the_fixture_intact(tmp_path, monkeypatch):
    """Combined fault in one gate: a raw application exception and assertion
    failures from the leaking store, plus the generated fixture's own
    OperationalError before its action. One bounded repair fixes both; the
    original reviewer compares the fixture with its pre-repair bytes and
    calls it intact; the run completes with R1 met on the executed case."""
    seen = {}

    def comparison(call, replay):
        seen["prompt"] = call.prompt
        return f"TEST INTACT {FAILURES} - the same assertions; the injection moved onto store.connect"
    replay = _run(tmp_path, monkeypatch, source=LEAKING, failures=BROKEN + "\n\n" + _LEAK_CASES,
                  fixed_source=SOUND, fixed_failures=CORRECTED + "\n\n" + _LEAK_CASES, comparison=comparison)
    (fix,) = replay.of("gate-fix")
    assert "Harness diagnosis" in fix.prompt and "sqlite3.OperationalError" in fix.prompt
    assert "never removed or weakened" in fix.prompt
    assert "-    monkeypatch.setattr(sqlite3, \"connect\", _unavailable)" in seen["prompt"]
    assert "TEST INTACT <path>" in seen["prompt"]
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["path"] == FAILURES and entry["verdict"] == "intact" and entry["reviewer"]
    assert replay.artifacts("generated-test-before-repair")
    assert replay.result.completed, replay.result.error
    assert H.result_json(replay)["requirements"]["audits"][-1]["verdicts"]["R1"]["met"] is True


#: Cases the leaking store fails with a raw exception and an assertion.
_LEAK_CASES = '''
def test_the_64_bit_edge_is_refused(tmp_path, capsys):
    path = _db(tmp_path)
    assert store.cli([str(2 ** 63)], path) == 2
    assert _rows(path) == [(1,)]
'''


@pytest.mark.requirements_ledger
def test_a_repair_that_weakens_the_generated_test_never_counts_as_recovery(tmp_path, monkeypatch):
    """The repair empties the fixture so the check passes. The original
    reviewer says it is weakened: that is its blocking finding, the run does
    not complete, and the saved record says so."""
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=WEAKENED,
                  comparison=lambda c, r: f"TEST WEAKENED {FAILURES} - the storage failure case asserts nothing now")
    assert len(replay.of("gate-fix")) == 1 and H.result_json(replay)["checks"][-1]["passed"]
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["verdict"] == "weakened"
    findings = H.result_json(replay)["review_findings"]
    assert any("the repair weakened" in f["text"] and not f.get("closed") for f in findings)
    assert not replay.result.completed


def test_a_repair_with_no_comparison_verdict_stays_unreviewed_and_unverified(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND,
                  fixed_failures=CORRECTED, comparison=lambda c, r: "Looks fine to me.")
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["verdict"] == "unreviewed" and "no TEST INTACT" in entry["why"]
    from tools import truth_check
    lines = truth_check._generated_test_problems(H.result_json(replay))
    assert lines and "no reviewer called it intact" in lines[0]


@pytest.mark.requirements_ledger
def test_a_product_syntax_error_that_empties_the_original_tests_is_repaired(tmp_path, monkeypatch):
    """Root's baseline control 2: a new syntax error in the product makes
    the gate's collection fail and the restored original tests execute
    nothing. Neither is an external blocker; one repair restores both."""
    broken = "def remove(path, raw)\n    return 0\n"
    extra = {"tests/test_store.py": HAPPY + "\n\ndef test_remove_twice(tmp_path):\n    assert True\n"}
    replay = _run(tmp_path, monkeypatch, source=broken, failures=CORRECTED, fixed_source=SOUND,
                  fixed_failures=CORRECTED, extra=extra)
    checks = H.result_json(replay)["workflow"]["tasks"][0]["checks"]
    first = checks[0]
    assert first["passed"] is False and first["repair_route"]["repairable"] is True
    assert first["attribution"]["product"] is False
    assert len(replay.of("gate-fix")) == 1
    assert replay.result.completed, replay.result.error


def test_a_runner_that_does_not_exist_is_still_handed_to_the_operator(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=CORRECTED, fixed_source=SOUND,
                  fixed_failures=CORRECTED, check=f"{tmp_path}/no-such-runner -v")
    assert replay.result.error.startswith("CheckUnattributable:"), replay.result.error
    assert "runner unavailable" in replay.result.error and not replay.of("gate-fix")


# -- a capped revision that made progress continues on the rest -------------------------------------------

@pytest.mark.requirements_ledger
def test_a_capped_revision_that_settled_a_finding_continues_on_only_the_rest(tmp_path, monkeypatch):
    """Two findings; the revision stops at its turn cap after fixing one.
    Its checked, in-scope source goes to the reviewer, who resolves one and
    not the other. With a fix cycle left, one more bounded fix call carries
    only the open finding, its recheck resolves it, and the task closes
    clean; no cap, budget or allowance grew."""
    from quadratus.config import Settings
    from tests.lifecycle.test_lifecycle_matrix import FILES as ADD_FILES
    from tests.lifecycle.test_lifecycle_matrix import FIXED
    from tests.lifecycle.test_quality_completion import FINDING, REQS, T1, TYPED, _auditor, _plan
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    second = "BLOCKING: add has no docstring saying it raises TypeError naming the argument."
    documented = TYPED.replace("def add(a, b):\n", 'def add(a, b):\n    """Raise TypeError naming the argument."""\n')

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def revision(call, replay):
        if len(replay.of("revision")) == 1:
            H.write(call, {"app.py": TYPED})
            return H.grok_ok("Now checking the docstring", stop="cancelled", num_turns=8)
        H.write(call, {"app.py": documented})
        return 'Added the docstring.\nCHANGED: ["app.py"]'

    def recheck(call, replay):
        if len(replay.of("recheck")) == 1:
            return "RFT1-1: RESOLVED the TypeError is raised\nRFT1-2: UNRESOLVED no docstring yet"
        return "RESOLVED"

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    revision=revision,
                    collaborator=lambda c, r: FINDING + "\n" + second + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                    recheck=recheck, auditor=_auditor({"R1": "MET - app.py"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=ADD_FILES, max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    revisions = replay.of("revision")
    assert [c.role for c in replay.calls if c.task == "t1" and c.role in ("lead", "revision")] == \
        ["lead", "revision", "revision"]
    assert "add has no docstring" in revisions[1].prompt and FINDING not in revisions[1].prompt, \
        "the follow-up carries only the open finding"
    (handoff,) = result["capped_handoffs"]
    assert handoff["continued"] is True and handoff["outcome"].startswith("continued: 1 settled, 1 open")
    assert result["turn_limited_tasks"] == []
    assert replay.result.completed, replay.result.error
