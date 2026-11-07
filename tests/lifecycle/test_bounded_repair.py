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
from tests.lifecycle.replies import judging_scenarios, recheck_reply
from tests.lifecycle.test_boundary_regressions import FILES, HAPPY, LEAKING, SOUND, VERBOSE
from tests.lifecycle.test_lifecycle_matrix import Script, judging

R1 = ("Removing an item through the command line reports a storage error as a JSON error object with exit "
      "status 1 and leaves the store unchanged")
FAILURES = "tests/test_store_failures.py"
CASE = f"{FAILURES}::test_a_storage_failure_is_a_json_error_and_changes_nothing"
PROOF = H.proofs_for("R1", FAILURES, operation="remove item 1 through the command line with storage unavailable",
                     state="one stored item", expect="exit status 1, a JSON error, the item kept")[0]
PLAN = ("REQUIREMENTS:\nR1: " + R1 + "\nKIND: backend simple\nSCOPE: "
        + json.dumps({"permitted_paths": ["store.py", "tests/"],
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
         check=VERBOSE, extra=None, settings=None, fixed_extra=None, plan=None, plans=None, later_lead=None,
         recheck=None, max_tasks=1, revision=None, collaborator=None):
    def lead(call, replay):
        if later_lead is not None and call.task != "t1":
            return later_lead(call, replay)
        H.write(call, {"store.py": source, FAILURES: failures, **(extra or {})})
        return (f"Implemented remove.\nPROVES: P1 - {CASE}\n"
                + 'CHANGED: ' + json.dumps(["store.py", FAILURES, *(extra or {})]))

    def gate_fix(call, replay):
        H.write(call, {"store.py": fixed_source, FAILURES: fixed_failures, **(fixed_extra or {})})
        return 'Repaired.\nCHANGED: ' + json.dumps(["store.py", FAILURES, *(fixed_extra or {})])

    sequence = list(plans or [plan or PLAN])

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return sequence[n - 1] if n <= len(sequence) else "DONE"
    script = Script(orchestrator=orchestrator, lead=lead,
                    # Without a requirements ledger a clean review is exactly NO FINDINGS.
                    collaborator=collaborator or (lambda c, r: (f"NO FINDINGS\nR1: MET - {CASE}"
                                                                if "REQUIREMENTS:" in sequence[0] else "NO FINDINGS")),
                    auditor=judging_scenarios(judging(lambda c, r: f"R1: MET - {CASE}"), PROOF, basis=SHOWN),
                    **{"requirements-review": lambda c, r: "COMPLETE",
                       "revision": revision or (lambda c, r: "Nothing to change.\nCHANGED: []"),
                       "gate-fix": gate_fix,
                       **({"test-comparison": comparison} if comparison else {}),
                       **({"recheck": recheck} if recheck else {})})
    _r = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=max_tasks, record_complete=False, check=check,
               goal="Add item removal to the store's command line. " + R1, settings=settings)
    return _r


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
    assert '-    monkeypatch.setattr(sqlite3, "connect", _unavailable)' in seen["prompt"], "every changed line"
    assert "~~~diff" in seen["prompt"] and "TEST INTACT <path>" in seen["prompt"]
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["path"] == FAILURES and entry["verdict"] == "intact" and entry["reviewer"]
    from quadratus.session import _test_repair_verified
    assert _test_repair_verified(entry) and entry["judgment"]["by"] == entry["reviewer"]
    assert entry["judgment"]["after_sha256"] == entry["after_sha256"] and entry["complete"] is True
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


def STILL_WEAK(call, replay):
    """The comparing reviewer's recheck after the correction: still weakened."""
    return recheck_reply(call.prompt, default="UNRESOLVED the case still asserts nothing")


@pytest.mark.requirements_ledger
def test_a_repair_that_weakens_the_generated_test_never_counts_as_recovery(tmp_path, monkeypatch):
    """The repair empties the fixture so the check passes. The original
    reviewer says it is weakened: that is its blocking finding, the run does
    not complete, and the saved record says so."""
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=WEAKENED,
                  comparison=lambda c, r: f"TEST WEAKENED {FAILURES} - the storage failure case asserts nothing now",
                  recheck=STILL_WEAK)
    assert len(replay.of("gate-fix")) == 1 and H.result_json(replay)["checks"][-1]["passed"]
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["verdict"] == "weakened"
    findings = H.result_json(replay)["review_findings"]
    assert any("the repair weakened" in f["text"] and not f.get("closed") for f in findings)
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_root_an_emptied_test_with_no_comparison_verdict_never_completes(tmp_path, monkeypatch):
    """Root review of b49a0cb, R2: the repair reduces the new scenario to
    `assert True` and the reviewer never gives a per-file verdict. The one
    judgment completion is spent, the comparison becomes the owner's owed
    review debt, its settlement on the same bytes gives no verdict either,
    and the run does not complete; live and saved readers agree."""
    calls = []

    def comparison(call, replay):
        calls.append(call)
        return "Looks fine to me."
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=WEAKENED,
                  comparison=comparison)
    result = H.result_json(replay)
    (entry,) = result["generated_test_repairs"]
    assert entry["verdict"] == "pending" and entry["judgment"] is None
    owed = [f for f in result["review_findings"] if f.get("owed_kind") == "test comparison"]
    assert owed and not owed[0].get("closed") and owed[0]["author"] == entry["reviewer"]
    assert 2 <= len(calls) <= 3, "the call, its one completion, at most one settlement: bounded"
    assert not replay.result.completed, replay.result.error
    from tools import truth_check
    assert truth_check._generated_test_problems(result)


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("reply", [
    f"TEST INTACT {FAILURES}.bak - safe",                                   # suffix of another path
    f"TEST INTACT {FAILURES} - same\nTEST WEAKENED {FAILURES} - gone",      # contradictory
    f"TEST INTACT {FAILURES} -",                                            # no reason
], ids=["wrong-path-suffix", "contradictory", "no-reason"])
def test_root_only_one_exact_reasoned_line_certifies(tmp_path, monkeypatch, reply):
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND,
                  fixed_failures=CORRECTED, comparison=lambda c, r: reply)
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["verdict"] == "pending" and not replay.result.completed


def test_root_a_saved_intact_without_the_reviewers_judgment_is_unverified():
    """Root review of b49a0cb, R3: verdict and reviewer name alone pass
    neither reader."""
    from quadratus.session import _test_repair_verified
    from tools import truth_check
    base = dict(task="t1", path=FAILURES, removed=False, before_sha256="a" * 64, after_sha256="b" * 64,
                complete=True, reviewer="claude:opus", verdict="intact")
    good = dict(base, judgment=dict(by="claude:opus", reply_artifact="r1", line=f"TEST INTACT {FAILURES} - same",
                                    after_sha256="b" * 64, before_sha256="a" * 64))
    variants = {"no judgment": dict(base, judgment=None),
                "another seat": dict(good, judgment=dict(good["judgment"], by="openai:sol")),
                "other bytes": dict(good, judgment=dict(good["judgment"], after_sha256="c" * 64)),
                "incomplete": dict(good, complete=False),
                "no reply": dict(good, judgment=dict(good["judgment"], reply_artifact="")),
                "suffix path": dict(good, judgment=dict(good["judgment"], line=f"TEST INTACT {FAILURES}.bak - same")),
                "weakened line": dict(good, judgment=dict(good["judgment"], line=f"TEST WEAKENED {FAILURES} - gone")),
                "no reason": dict(good, judgment=dict(good["judgment"], line=f"TEST INTACT {FAILURES} -"))}
    bullet = dict(good, judgment=dict(good["judgment"], line=f"- TEST INTACT {FAILURES} - same"))
    for ok in (good, bullet):
        assert _test_repair_verified(ok) and truth_check._generated_test_problems(dict(generated_test_repairs=[ok])) == []
    for name, entry in variants.items():
        assert not _test_repair_verified(entry), name
        assert truth_check._generated_test_problems(dict(generated_test_repairs=[entry])), name


def test_root_the_comparison_is_a_capped_reviewer_call_on_the_reserved_route(tmp_path, monkeypatch):
    """Root review of b49a0cb, R4: the comparison role takes the review
    seats' turn cap, and the repair route reserves it."""
    from quadratus.config import Settings
    from quadratus.runtime import REVIEW_CAPPED_ROLES
    assert "test-comparison" in REVIEW_CAPPED_ROLES
    seen = []

    def comparison(call, replay):
        seen.append(call)
        return f"TEST INTACT {FAILURES} - same assertions on the action's own path"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=comparison, settings=Settings(backend="cli", lead_max_turns=8))
    (call,) = seen
    assert call.vendor == "claude", "the task's original reviewer in this fixture"
    assert call.argv[call.argv.index("--max-turns") + 1] == "8", call.argv
    stages = [s for a in H.result_json(replay)["plan_admissions"] for s in a.get("stages") or []]
    assert any("generated test comparison by" in str(s) for s in stages), stages


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


@pytest.mark.requirements_ledger
def test_root_content_not_supplied_whole_is_never_certified_intact(tmp_path, monkeypatch):
    """Root review of b49a0cb, R3, and of 0ffc9e9, item 2: a repair whose
    changes cannot be shown whole is named so, and an INTACT on it
    certifies nothing."""
    from quadratus import session as S
    monkeypatch.setattr(S, "_TEST_COMPARE_CHARS", 50)
    seen = []

    def comparison(call, replay):
        seen.append(call.prompt)
        return f"TEST INTACT {FAILURES} - looks the same"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=comparison)
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["complete"] is False and "exceed 50 characters of diff" in entry["incomplete_why"]
    assert "cannot be shown whole" in seen[0] and entry["verdict"] == "pending"
    assert "INTACT cannot certify changes not shown whole" in entry["why"]
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_root_a_bullet_verdict_is_read_the_same_way_live_and_saved(tmp_path, monkeypatch):
    """Root review of 0ffc9e9, item 1: the accepted '- TEST INTACT' line."""
    from quadratus.session import _test_repair_verified
    from tools import truth_check
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=lambda c, r: f"- TEST INTACT {FAILURES} - same assertions on the action's path")
    result = H.result_json(replay)
    (entry,) = result["generated_test_repairs"]
    assert entry["verdict"] == "intact" and _test_repair_verified(entry)
    assert truth_check._generated_test_problems(result) == []
    assert replay.result.completed, replay.result.error


_PADDING = "".join(f"# fixture note {i}: unchanged context the repair never touches\n" for i in range(1500))


@pytest.mark.requirements_ledger
def test_root_a_small_fix_in_a_large_generated_test_is_judged_on_its_changes(tmp_path, monkeypatch):
    """Root review of 0ffc9e9, item 2: a test file far past any whole-file
    bound gets a one-hunk fix. The reviewer receives every changed line with
    context, not the whole file, and the repair completes within the
    existing budget."""
    seen = []

    def comparison(call, replay):
        seen.append(call.prompt)
        return f"TEST INTACT {FAILURES} - the injection moved onto store.connect; same assertions"
    big_broken, big_fixed = BROKEN + _PADDING, CORRECTED + _PADDING
    assert len(big_broken) > 80_000
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=big_broken, fixed_source=SOUND,
                  fixed_failures=big_fixed, comparison=comparison)
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["complete"] is True and entry["verdict"] == "intact"
    (prompt,) = seen
    assert '-    monkeypatch.setattr(sqlite3, "connect", _unavailable)' in prompt
    assert '+    monkeypatch.setattr(store, "connect", _unavailable)' in prompt
    assert "fixture note 1499" not in prompt and len(prompt) < 40_000, "changes with context, not the file"
    assert replay.result.completed, replay.result.error


SECOND = "tests/test_store_more.py"


@pytest.mark.requirements_ledger
def test_root_each_verdict_is_bound_to_the_reply_that_gave_it(tmp_path, monkeypatch):
    """Root review of 0ffc9e9, item 3: the first reply judges one file, the
    one judgment completion judges the other; each entry's artifact holds
    its own line, and live and saved readers agree."""
    from quadratus.session import _test_repair_verified
    from tools import truth_check
    second_broken = BROKEN.replace("test_a_storage_failure", "test_another_storage_failure")
    second_fixed = CORRECTED.replace("test_a_storage_failure", "test_another_storage_failure")
    calls = []

    def comparison(call, replay):
        calls.append(call)
        if len(calls) == 1:
            return f"TEST INTACT {FAILURES} - same assertions"
        return f"TEST INTACT {SECOND} - same assertions"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=comparison, extra={SECOND: second_broken}, fixed_extra={SECOND: second_fixed})
    result = H.result_json(replay)
    entries = {e["path"]: e for e in result["generated_test_repairs"]}
    assert set(entries) == {FAILURES, SECOND} and len(calls) == 2
    from quadratus.artifacts import ArtifactStore
    store = ArtifactStore(replay.result.run_dir / "artifacts")
    for path, entry in entries.items():
        assert entry["verdict"] == "intact" and _test_repair_verified(entry), path
        assert entry["judgment"]["line"] in store.get(entry["judgment"]["reply_artifact"]), path
    assert entries[FAILURES]["judgment"]["reply_artifact"] != entries[SECOND]["judgment"]["reply_artifact"]
    assert truth_check._generated_test_problems(result) == []
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_root_the_aggregate_bound_splits_files_across_bounded_calls(tmp_path, monkeypatch):
    """Root review of 0ffc9e9, item 2: the bound is on the whole call, not
    only per file. Two changed files that do not fit one call are shown
    whole-diff one per call, each judged, and the run completes."""
    from quadratus import session as S
    one = len(S._test_diff(FAILURES, BROKEN.encode(), CORRECTED.encode()))
    monkeypatch.setattr(S, "_TEST_COMPARE_CHARS", one + 200)
    second_broken = BROKEN.replace("test_a_storage_failure", "test_another_storage_failure")
    second_fixed = CORRECTED.replace("test_a_storage_failure", "test_another_storage_failure")
    prompts = []

    def comparison(call, replay):
        prompts.append(call.prompt)
        path = FAILURES if f"### {FAILURES}" in call.prompt else SECOND
        return f"TEST INTACT {path} - same assertions"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=comparison, extra={SECOND: second_broken}, fixed_extra={SECOND: second_fixed})
    assert len(prompts) == 2
    assert all((f"### {FAILURES}" in p) != (f"### {SECOND}" in p) for p in prompts), "one file's diff per call"
    assert "1 more changed file(s) are judged in a following call" in prompts[0]
    entries = H.result_json(replay)["generated_test_repairs"]
    assert {e["verdict"] for e in entries} == {"intact"}
    assert replay.result.completed, replay.result.error


#: Root's no-peer shape (PR73 6695131414): a simple backend task with no
#: requirements ledger draws no collaborator of its own.
NO_PEER_PLAN = ("KIND: backend simple\nSCOPE: "
                + json.dumps({"permitted_paths": ["store.py", "tests/"], "intended_result": "remove reports storage "
                              "errors", "acceptance": ["storage errors are JSON"], "max_lines": 260})
                + "\nImplement remove with its storage error and a test.")


def test_root_a_task_with_no_reviewer_gets_an_independent_one_for_the_comparison(tmp_path, monkeypatch):
    """Root, PR73 6695131414: a task that drew no collaborator rewrote its
    generated fixture and completed with zero comparisons. Now an available
    brain-trust member of another vendor than the lead compares it (never a
    worker-only seat), and the run completes on that verdict."""
    seen = []

    def comparison(call, replay):
        seen.append(call)
        return f"TEST INTACT {FAILURES} - same assertions on the action's path"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=comparison, plan=NO_PEER_PLAN)
    assert not replay.of("collaborator"), "the task itself drew no reviewer"
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    (call,) = seen
    lead = replay.of("lead")[0]
    assert entry["reviewer"] and call.vendor != lead.vendor and call.vendor != "grok"
    assert entry["verdict"] == "intact" and entry["judgment"]["by"] == entry["reviewer"]
    assert replay.result.completed, replay.result.error


def test_root_with_no_reviewer_anywhere_the_rewrite_holds_completion(tmp_path, monkeypatch):
    from quadratus.session import Session
    monkeypatch.setattr(Session, "_comparison_reviewer", lambda self, lead: None)
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  plan=NO_PEER_PLAN)
    (entry,) = H.result_json(replay)["generated_test_repairs"]
    assert entry["reviewer"] is None and entry["verdict"] == "pending"
    assert "no reviewer is available to compare it" in (replay.result.error or ""), replay.result.error
    assert not replay.result.completed



@pytest.mark.requirements_ledger
def test_root_a_later_weakened_from_the_same_owner_overrides_an_earlier_intact(tmp_path, monkeypatch):
    """Root, PR73 6695349150: the first reply calls A intact and omits B;
    the judgment completion calls A weakened and B intact. A is weakened
    (bound to the completion), that owner's blocking finding holds, and the
    run does not complete."""
    second_broken = BROKEN.replace("test_a_storage_failure", "test_another_storage_failure")
    second_fixed = CORRECTED.replace("test_a_storage_failure", "test_another_storage_failure")
    calls = []

    def comparison(call, replay):
        calls.append(call)
        if len(calls) == 1:
            return f"TEST INTACT {FAILURES} - same assertions"
        return f"TEST WEAKENED {FAILURES} - the cleanup assertion is gone\nTEST INTACT {SECOND} - same assertions"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=comparison, extra={SECOND: second_broken}, fixed_extra={SECOND: second_fixed},
                  plan=None, recheck=STILL_WEAK)
    result = H.result_json(replay)
    entries = {e["path"]: e for e in result["generated_test_repairs"]}
    assert entries[FAILURES]["verdict"] == "weakened" and entries[SECOND]["verdict"] == "intact"
    from quadratus.artifacts import ArtifactStore
    store = ArtifactStore(replay.result.run_dir / "artifacts")
    assert "TEST WEAKENED" in store.get(entries[FAILURES]["judgment"]["reply_artifact"])
    assert any("the repair weakened" in f["text"] and not f.get("closed") for f in result["review_findings"])
    assert not replay.result.completed



STANDARD_NO_LEDGER = NO_PEER_PLAN.replace("KIND: backend simple", "KIND: backend standard")


NO_LEDGER = pytest.mark.parametrize("plan", [NO_PEER_PLAN, STANDARD_NO_LEDGER], ids=["simple-no-peer", "standard"])
WEAKENS = f"TEST WEAKENED {FAILURES} - the storage failure case asserts nothing now"


@NO_LEDGER
def test_root_without_the_ledger_a_weakened_comparison_holds_the_run(tmp_path, monkeypatch, plan):
    """Root, PR73 6695526328: without a requirements ledger the registered
    WEAKENED finding was ignored and the run completed. It now gets the
    ordinary bounded correction; the lead changes nothing, the comparing
    reviewer rechecks it UNRESOLVED, the finding stays open and the run does
    not complete."""
    rechecks = []

    def recheck(call, replay):
        rechecks.append(call)
        return STILL_WEAK(call, replay)
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=WEAKENED,
                  comparison=lambda c, r: WEAKENS, recheck=recheck, plan=plan)
    result = H.result_json(replay)
    (entry,) = result["generated_test_repairs"]
    assert entry["verdict"] == "weakened" and not entry.get("resolved_by")
    (finding,) = [f for f in result["review_findings"] if "the repair weakened" in f["text"]]
    assert not finding.get("closed") and finding["author"] == entry["reviewer"]
    assert rechecks and rechecks[0].vendor == replay.of("test-comparison")[0].vendor, "its own reviewer"
    assert any(finding["fid"] in text for text in result.get("open_findings") or []), result.get("open_findings")
    from tools.truth_check import _generated_test_problems
    assert _generated_test_problems(result)
    assert not replay.result.completed, replay.result.error


@NO_LEDGER
def test_root_a_genuine_correction_resolved_by_the_comparing_reviewer_completes(tmp_path, monkeypatch, plan):
    """The ordinary bounded route, not a permanent veto: the correction
    restores the case, the reviewer who judged the repair rechecks its own
    finding RESOLVED, and the run completes."""
    def revision(call, replay):
        if "the repair weakened" not in call.prompt:
            return "Nothing to change.\nCHANGED: []"
        H.write(call, {FAILURES: CORRECTED})
        return f'Restored the storage failure case.\nCHANGED: ["{FAILURES}"]'
    rechecks = []

    def recheck(call, replay):
        rechecks.append(call)
        return recheck_reply(call.prompt, default="RESOLVED the case asserts the JSON error and the kept item again")
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=WEAKENED,
                  comparison=lambda c, r: WEAKENS, recheck=recheck, revision=revision, plan=plan)
    result = H.result_json(replay)
    (finding,) = [f for f in result["review_findings"] if "the repair weakened" in f["text"]]
    assert finding.get("closed"), finding
    (entry,) = result["generated_test_repairs"]
    assert entry["verdict"] == "weakened" and entry.get("resolved_by")
    assert rechecks and rechecks[0].vendor == replay.of("test-comparison")[0].vendor, "its own reviewer"
    from quadratus.session import _test_repair_settled
    from tools.truth_check import _generated_test_problems
    assert _test_repair_settled(entry, result["review_findings"]) and not _generated_test_problems(result)
    assert replay.result.completed, replay.result.error


@NO_LEDGER
def test_root_the_comparing_reviewer_may_retract_its_own_finding_on_unchanged_bytes(tmp_path, monkeypatch, plan):
    """Root, PR73: no byte-change rule. The lead changes nothing and the
    reviewer retracts its own WEAKENED finding with a reasoned INVALIDATED
    citing the file on the record; that settles the comparison."""
    def recheck(call, replay):
        return recheck_reply(call.prompt, default=f"INVALIDATED {FAILURES} still asserts the JSON error and the "
                                                  "kept item; my comparison misread the diff")
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=BROKEN, fixed_source=SOUND, fixed_failures=CORRECTED,
                  comparison=lambda c, r: f"TEST WEAKENED {FAILURES} - the cleanup assertion looks gone",
                  recheck=recheck, plan=plan)
    result = H.result_json(replay)
    (entry,) = result["generated_test_repairs"]
    assert entry["verdict"] == "weakened" and (entry.get("resolved_by") or {}).get("state") == "invalidated", entry
    from quadratus.session import _test_repair_settled
    from tools.truth_check import _generated_test_problems
    assert _test_repair_settled(entry, result["review_findings"]) and not _generated_test_problems(result)
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_cycle01_a_weakened_repair_inside_a_resolving_task_is_held_never_the_unresolved_stop(tmp_path, monkeypatch):
    """Cycle01 (PR73 6702597850): t2 RESOLVES t1's open finding, its gate
    repair weakens the generated test, and the comparing reviewer keeps that
    open after the correction. t2 closed with a finding of its own, so its
    RESOLVES was never rechecked: both stay held as pending work, the
    planner has its turn, nothing completes over them, and the stop is
    never FindingsUnresolved. Without the ledger the first open finding
    already stops the run FindingsOpen at t1's close, so this route is the
    ledger's."""
    code = "BLOCKING: remove does not report a missing id | basis: R1 | impact: correctness"
    resolving = ("KIND: backend simple\nSCOPE: "
                 + json.dumps({"permitted_paths": ["store.py", "tests/"], "intended_result": "missing ids reported",
                               "acceptance": ["a missing id is an error"], "max_lines": 260, "proofs": [PROOF]})
                 + "\nReport a missing id from remove.\nCOVERS: R1\nRESOLVES: RFT1-1")

    def later(call, replay):
        H.write(call, {FAILURES: BROKEN})
        return f"Reworked the storage case.\nPROVES: P1 - {CASE}\nCHANGED: " + json.dumps([FAILURES])

    def review(call, replay):
        return (code if call.task == "t1" else "NO FINDINGS") + f"\nR1: MET - {CASE}"
    replay = _run(tmp_path, monkeypatch, source=SOUND, failures=CORRECTED, fixed_source=SOUND,
                  fixed_failures=WEAKENED, comparison=lambda c, r: WEAKENS, recheck=STILL_WEAK,
                  plans=[PLAN, resolving], later_lead=later, max_tasks=3, collaborator=review)
    result = H.result_json(replay)
    (entry,) = result["generated_test_repairs"]
    assert entry["task"] == "t2" and entry["verdict"] == "weakened" and not entry.get("resolved_by")
    (row,) = [r for r in result["review_resolutions"] if r["task"] == "t2"]
    assert row["outcomes"] == {"RFT1-1": "not_attempted"} and "closed with open findings" in row["refused"]
    assert len(replay.of("orchestrator")) >= 3, "the planner had its turn after t2"
    assert not (replay.result.error or "").startswith("FindingsUnresolved"), replay.result.error
    open_ids = {f["fid"] for f in result["review_findings"] if not f.get("closed")}
    assert {"RFT1-1", entry["finding_id"]} <= open_ids
    assert not replay.result.completed
