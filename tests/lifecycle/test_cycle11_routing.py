"""Cycle10 terminal (root's assessment, PR73 6724518561), three engine causes:

A. t4's close-out, frozen in the append-only ledger, listed RFT4-14 as
   unresolved. The post-merge recheck closed it. The planner prompt carried
   only the close-out, so t5 and then t6 named it in RESOLVES, each was
   refused as "already resolved", and the shared correction allowance ended
   the run with three slots and 6.1M tokens left. Now the planner is shown
   the registry's current dispositions, and a closed id in RESOLVES is
   dropped and recorded; the task stands on its COVERS. Nothing reopens.
B. RFT4-14's author kept it open at its penultimate recheck with a concrete
   counterexample (no indirect-only case). The post-merge recheck was shown
   only the original claim and closed it on a generic citation. Now each
   recheck packet carries the author's latest UNRESOLVED reason as the
   question still standing; what answers it stays the reviewer's.
C. "python3 -m unittest discover -s tests passes on the delivered source,
   including every test in tests/test_existing.py and new feature tests"
   was read as a behaviour and demanded a scenario. A population clause is
   read by grammar, and the receipt's own executed cases must show it.

Real Session and controller with scripted providers and the project's own
pytest gate, and units on a hand-set record."""
import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import CODE_FINDING, MET, Seen, _script

pytestmark = pytest.mark.requirements_ledger


def _session(tmp_path, requirements):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements)
    return session


# -- A. current dispositions, and a closed id in RESOLVES ---------------------------------------------

def _registry(tmp_path):
    session = _session(tmp_path, {"R22": "the CLI refuses transitive conflicts", "R26": "the CLI keeps history"})
    resolved = dict(state="resolved", author="openai:sol", role="recheck", why="executed cases now cover it",
                    binding="b", source_digest="s")
    session.review_findings = [
        dict(fid="RFT4-1", author="openai:sol", task="t4", claim="missing proof", closed=True, state="resolved",
             history=[resolved], requirement=["R22", 1]),
        dict(fid="RFT4-14", author="openai:sol", task="t4", claim="missing CLI scenarios", closed=True,
             state="resolved", history=[resolved], requirement=["R22", 1]),
        dict(fid="RFT4-20", author="openai:sol", task="t4", claim="history not shown", closed=False, state="open",
             history=[], requirement=["R26", 1]),
    ]
    return session


def _spec(text, task="t6"):
    from quadratus.session import TaskSpec
    return TaskSpec(task_id=task, description=text)


def test_the_planner_is_shown_the_registrys_current_disposition_beside_the_frozen_close_out(tmp_path):
    session = _registry(tmp_path)
    prompt = session._findings_prompt()
    closed = prompt.split("--- FINDINGS CLOSED NOW (the registry, current) ---", 1)
    assert len(closed) == 2, prompt
    assert "- RFT4-14: RESOLVED by openai:sol" in closed[1] and "- RFT4-1: RESOLVED by openai:sol" in closed[1]
    assert "is history from when that task closed" in closed[1]
    assert "RFT4-20 (by openai:sol on t4)" in closed[0], "the open one is still offered"
    assert "RFT4-14" not in closed[0]


@pytest.mark.parametrize("line, resolves, dropped, covers", [
    ("RESOLVES: RFT4-14", None, ["RFT4-14"], ["R22", "R26"]),
    ("RESOLVES: RFT4-1, RFT4-14", None, ["RFT4-1", "RFT4-14"], ["R22", "R26"]),
    ("RESOLVES: RFT4-14, RFT4-20", ["RFT4-20"], ["RFT4-14"], ["R22", "R26"]),
    ("RESOLVES: RFT4-14, RFT9-9", ["RFT9-9"], ["RFT4-14"], ["R22", "R26"]),
])
def test_a_closed_id_is_dropped_and_recorded_and_everything_else_is_admitted_as_before(tmp_path, line, resolves,
                                                                                      dropped, covers):
    from quadratus.session import _read_covers, _read_resolves
    session = _registry(tmp_path)
    spec = session._normalize_resolves(_spec(f"Add isolated indirect-only CLI tests.\nCOVERS: R22, R26\n{line}"))
    got_covers, spec = _read_covers(spec)
    got, spec = _read_resolves(spec)
    assert got == resolves and got_covers == covers
    assert session.resolves_normalized[-1]["closed_dropped"] == dropped
    problem = session._resolves_problem(got, got_covers, spec)
    if resolves == ["RFT9-9"]:
        assert "RESOLVES names findings that do not exist: RFT9-9" in problem, "an unknown id is never dropped"
    elif resolves is None:
        assert problem == ""
    assert all(r["closed"] for r in session.review_findings if r["fid"] in dropped), "nothing reopens"


def test_a_closed_id_with_no_covers_is_sent_back_for_its_covers_not_for_the_closed_id(tmp_path):
    from quadratus.session import _read_covers
    session = _registry(tmp_path)
    covers, spec = _read_covers(session._normalize_resolves(_spec("Add CLI tests.\nRESOLVES: RFT4-14")))
    assert covers == [] and session._covers_problem(covers) == "The task has no COVERS line."


def _stale_run(tmp_path, monkeypatch, name):
    """t1's reviewer raises RFT1-1 and resolves it at its recheck; the
    planner then names RFT1-1 in RESOLVES for a further task, as t6 did."""
    scope = _scope(proofs=_proofs("R1"))
    further = (f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nAdd a further negative case to app.py.\n"
               "COVERS: R1\nRESOLVES: RFT1-1")
    seen = Seen()

    def orchestrator(call, replay):
        calls = len(replay.of("orchestrator"))
        if calls == 1:
            return _plan(_scope(proofs=_proofs("R1")))
        if any(c.task == "t2" for c in replay.of("lead")):
            return "DONE"
        return further

    def collaborator(call, replay):
        return (CODE_FINDING + MET) if call.task == "t1" else "NO FINDINGS" + MET

    def lead(call, replay):
        body = FIXED if call.task == "t1" else "def add(a, b):\n    # negatives included\n    return a + b\n"
        H.write(call, {"app.py": body})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
    script = _script(seen, orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                     recheck=lambda c, r: "RFT1-1: RESOLVED test_add now runs on this source",
                     auditor=judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN))
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=2, record_complete=False), seen


def test_a_further_task_naming_a_since_closed_finding_is_admitted_and_runs(tmp_path, monkeypatch):
    """On e4d461e the task was refused as resolving an already-resolved
    finding until the correction allowance stopped the run (cycle10 t6)."""
    replay, seen = _stale_run(tmp_path, monkeypatch, "stale")
    record = H.result_json(replay)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the task reached execution"
    assert not any("TASK SENT BACK" in p for p in seen.prompts["orchestrator"])
    second = seen.prompts["orchestrator"][1]
    assert "--- FINDINGS CLOSED NOW (the registry, current) ---" in second and "- RFT1-1: RESOLVED by" in second
    (note,) = record["resolves_normalized"]
    assert note["task"] == "t2" and note["closed_dropped"] == ["RFT1-1"]
    (finding,) = record["review_findings"]
    assert finding["state"] == "resolved" and len(finding["history"]) == 1, "never reopened or re-judged"
    assert replay.result.completed, replay.result.error


# -- B. the post-merge recheck is handed the question still standing ----------------------------------

OBJECTION = ("test_add still adds only positives; it does not isolate a case where only the second operand is "
             "negative, so the refusal path is not shown")


def _parallel(tmp_path, monkeypatch, name, *, last):
    """A parallel child whose reviewer keeps RFT1-1 open twice with a concrete
    counterexample; the child closes with it held, and the same reviewer
    rechecks it after the merge gate (the real post-merge path)."""
    requirements = "REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
    seen = Seen()

    def block(path, covers, proofs):
        paths = [path, "tests/test_app.py"] if path == "app.py" else [path]
        scope = dict(permitted_paths=paths, intended_result=f"{path} written", acceptance=[f"{path} meets the task"],
                     max_lines=20, proofs=proofs)
        return f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nWrite {path}.\nCOVERS: {covers}"
    batch = (requirements + "PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n"
             + block("README.md", "R2", _proofs("R2", location="README.md", evidence="reading")))

    def lead(call, replay):
        if "Write README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        return (CODE_FINDING + "\nR1: MET - app.py") if call.task == "t1" else "NO FINDINGS\nR2: MET - README.md"

    def recheck(call, replay):
        seen.keep("recheck", call)
        return f"RFT1-1: UNRESOLVED {OBJECTION}" if len(seen.prompts["recheck"]) <= 2 else last

    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        return (f"R1: MET - {CASE}\nR2: MET - README.md"
                + "".join(f"\n{o}: SUFFICIENT README.md names add(a, b) and its result" for o in docs))
    script = Script(orchestrator=lambda c, r: batch if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=collaborator, auditor=judging_scenarios(auditor, PROOF_R1, basis=SHOWN),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["recheck"] = recheck
    script.overrides["revision"] = lambda c, r: "Nothing to change.\nCHANGED: []"
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=4, record_complete=False)
    return replay, seen


def _post_merge(seen):
    prompts = [p for p in seen.prompts["recheck"] if "Your findings left open:" in p]
    assert len(prompts) == 1, "one post-merge recheck by the same reviewer"
    return prompts[0].split("Your findings left open:", 1)[1].split("\n\n", 1)[0]


def test_the_post_merge_recheck_carries_the_authors_latest_counterexample(tmp_path, monkeypatch):
    """On e4d461e the post-merge packet showed only the original claim."""
    replay, seen = _parallel(tmp_path, monkeypatch, "answered",
                             last="RFT1-1: RESOLVED tests/test_app.py::test_add now drives a negative second operand")
    shown = _post_merge(seen)
    assert "- RFT1-1: BLOCKING: no negative case covers subtraction of negatives" in shown
    assert f"your latest judgment (recheck, on an earlier source) kept it open: {OBJECTION}" in shown
    assert "that reason is the question still standing: a RESOLVED line answers it" in shown
    finding = next(f for f in H.result_json(replay)["review_findings"] if f["fid"] == "RFT1-1")
    assert [h["state"] for h in finding["history"]] == ["unresolved", "unresolved", "resolved"], "history kept"
    assert finding["history"][1]["why"].startswith("test_add still adds only positives")
    assert replay.result.completed, replay.result.error


def test_an_adverse_post_merge_verdict_stays_binding(tmp_path, monkeypatch):
    replay, seen = _parallel(tmp_path, monkeypatch, "adverse",
                             last="RFT1-1: UNRESOLVED still no case where only the second operand is negative")
    assert OBJECTION in _post_merge(seen)
    finding = next(f for f in H.result_json(replay)["review_findings"] if f["fid"] == "RFT1-1")
    assert finding["state"] == "unresolved" and not replay.result.completed


def test_no_standing_question_is_invented_without_the_authors_own_reason(tmp_path):
    session = _session(tmp_path, {"R1": "add"})
    base = dict(fid="RFT1-1", author="openai:sol", claim="missing case", text="BLOCKING: missing case")
    by_other = dict(state="unresolved", author="anthropic:opus", role="audit", why="other seat", binding="b")
    by_author = dict(state="resolved", author="openai:sol", role="recheck", why="done", binding="b")
    for history in ([], [by_other], [dict(by_other, author="openai:sol", why="")], [by_author]):
        lines = session._finding_lines([dict(base, history=history)])
        assert lines == "- RFT1-1: BLOCKING: missing case", history


# -- C. a population clause is part of the test-command claim -----------------------------------------

R1 = ("python3 -m unittest discover -s tests passes on the delivered source, including every test in "
      "tests/test_existing.py and new feature tests.")


@pytest.mark.parametrize("text, pure", [
    (R1, True),
    ("`pytest -q` passes, including the refund tests", True),
    ("`pytest -q` passes including tests/test_a.py and tests/test_b.py", True),
    # The clause names a behaviour, a selection or nothing: the scenario stays.
    ("`pytest -q` passes, including tests for malformed JSON returning 400", False),
    ("`pytest -q` passes, including tests that check refunds are atomic", False),
    ("`pytest -q` passes, including", False),
    ("`python -m pytest -k refunds` passes, including every test", False),
    ("`pytest -q` passes and refunds are atomic, including new tests", False),
    # Cycle11 review: a backticked path is a path, not a second command.
    ("`pytest -q` passes, including every test in `tests/test_existing.py` and new feature tests.", True),
    ("`pytest -q` and `npm test` pass", False),
    ("`pytest -q` passes and npm test passes", False),
    ("`pytest -q` passes, including tests for `POST /refund` returning 400", False),
    ("`pytest -q tests/test_a.py -k refunds` passes, including `tests/test_a.py`", False),
])
def test_a_population_clause_is_read_by_grammar(tmp_path, text, pure):
    from types import SimpleNamespace
    session = _session(tmp_path, {"R1": text})
    assert session._test_command_requirement("R1") is pure, text
    spec = SimpleNamespace(scope=SimpleNamespace(proofs=None, review_only=False), task_id="t5")
    assert (session._proof_problem(spec, ["R1"]) == "") is pure, "no scenario is demanded for a pure claim"


def test_the_cycle10_r1_population_is_its_named_file_and_the_runs_added_tests(tmp_path):
    session = _session(tmp_path, {"R1": R1})
    assert session._command_requirement("R1") == ["python3", "-m", "unittest", "discover", "-s", "tests"]
    assert [(i["paths"], i["added"]) for i in session._command_population("R1")] == [
        (["tests/test_existing.py"], False), ([], True)]


SUITE = "`python -m pytest -q` passes on the delivered source, including every test in tests/test_app.py and the new tests"
NEW_TEST = "from app import add\n\n\ndef test_negatives():\n    assert add(-1, -2) == -3\n"
#: The run-start test file with one more case appended (cycle11 review).
APPENDED = FILES["tests/test_app.py"] + "\n\ndef test_new_feature():\n    assert add(-1, -2) == -3\n"


def _population_run(tmp_path, monkeypatch, name, suite, *, new_test=False, tests=None):
    scope = _scope(proofs=_proofs("R1"))
    scope["permitted_paths"].append("tests/test_new.py")
    plan = _plan(scope, requirements=f"R1: add(a, b) returns the sum\nR2: {suite}")

    def lead(call, replay):
        files = {"app.py": FIXED, **({"tests/test_new.py": NEW_TEST} if new_test else {}),
                 **({"tests/test_app.py": tests} if tests is not None else {})}
        H.write(call, files)
        return f"Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: {json.dumps(sorted(files))}"
    auditor = judging_scenarios(lambda c, r: f"R1: MET - {CASE}\nR2: MET - the harness's own complete run of that "
                                             "command passed on the delivered source with those files' cases",
                                PROOF_R1, basis=SHOWN)
    replies = iter([plan, "DONE", "DONE", "DONE"])
    script = _script(Seen(), orchestrator=lambda c, r: next(replies, "DONE"), lead=lead, auditor=auditor)
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)


def test_a_population_the_receipt_shows_is_covered_for_the_audit_with_no_task(tmp_path, monkeypatch):
    """On e4d461e R2 was a behaviour: never receipt-covered, DONE refused."""
    replay = _population_run(tmp_path, monkeypatch, "shown", SUITE, new_test=True)
    record = H.result_json(replay)
    (cover,) = record["receipt_coverage"]
    assert cover["requirement"] == "R2" and cover["command"] == "python -m pytest -q"
    assert [c.task for c in replay.of("lead")] == ["t1"], "no task was spent on R2"
    assert replay.result.completed, replay.result.error


def test_a_new_case_appended_to_an_existing_test_file_is_a_new_test(tmp_path, monkeypatch):
    """Cycle11 review: c653d94 refused this as "the run added no test file"."""
    replay = _population_run(tmp_path, monkeypatch, "appended", SUITE, tests=APPENDED)
    record = H.result_json(replay)
    (cover,) = record["receipt_coverage"]
    assert cover["requirement"] == "R2"
    assert [c.task for c in replay.of("lead")] == ["t1"] and replay.result.completed, replay.result.error


@pytest.mark.parametrize("suite, missing", [
    (SUITE, "the run added no test case (the new tests)"),
    ("`python -m pytest -q` passes on the delivered source, including every test in tests/test_gone.py",
     "tests/test_gone.py is not a test file on the current source"),
])
def test_a_population_the_receipt_does_not_show_stays_debt_and_names_what_is_missing(tmp_path, monkeypatch, suite,
                                                                                      missing):
    replay = _population_run(tmp_path, monkeypatch, "missing" + str(len(suite)), suite, new_test=False)
    record = H.result_json(replay)
    assert record["receipt_coverage"] == []
    refusals = [c.prompt for c in replay.of("orchestrator") if "not covered by any finished task: R2" in c.prompt]
    assert refusals and missing in refusals[0], refusals[:1]
    assert not record["requirements"]["status"].get("R2", "").startswith("met"), "no MET without the population"
    assert not replay.result.completed


# -- C (cycle11 review). new tests are new cases, read from run-start and current source ------------

OLD = "from app import add\n\n\ndef test_old():\n    assert add(1, 2) == 3\n"


def _cases(tmp_path, files, passed, *, start=None):
    """A session over a real project, ``start`` as the run-start test bytes,
    and a run whose executed ids are ``passed``."""
    project = tmp_path / "p"
    for path, text in files.items():
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_text(text)
    session = _session(project, {"R1": "`pytest -q` passes, including every test in `tests/test_existing.py` and "
                                       "new feature tests."})
    session._original_tests = None if start is False else {
        k: v.encode() for k, v in (start if start is not None else {"tests/test_existing.py": OLD}).items()}
    run = dict(cwd=".", report_cases=list(passed), qualified_names=set(), executed_names={})
    return session, run


@pytest.mark.parametrize("name, files, passed, start, problem", [
    ("new case in the same file",
     {"tests/test_existing.py": OLD + "\n\ndef test_new_feature():\n    assert add(-1, -2) == -3\n"},
     ["tests/test_existing.py::test_old", "tests/test_existing.py::test_new_feature"], None, ""),
    ("new case in a new file",
     {"tests/test_existing.py": OLD, "tests/test_new.py": NEW_TEST},
     ["tests/test_existing.py::test_old", "tests/test_new.py::test_negatives"], None, ""),
    ("parametrized new case",
     {"tests/test_existing.py": OLD + "\n\nimport pytest\n\n@pytest.mark.parametrize('x', [1])\n"
                                      "def test_new_feature(x):\n    assert add(x, 0) == x\n"},
     ["tests/test_existing.py::test_old", "tests/test_existing.py::test_new_feature[1]"], None, ""),
    ("no new case", {"tests/test_existing.py": OLD}, ["tests/test_existing.py::test_old"], None,
     "the run added no test case"),
    ("changed bytes, no new case", {"tests/test_existing.py": OLD + "\n# a comment\n"},
     ["tests/test_existing.py::test_old"], None, "the run added no test case"),
    ("an old test renamed", {"tests/test_existing.py": OLD.replace("test_old", "test_new_feature")},
     ["tests/test_existing.py::test_new_feature"], None, "renames a run-start test"),
    ("a new case not executed",
     {"tests/test_existing.py": OLD + "\n\ndef test_new_feature():\n    assert add(-1, -2) == -3\n"},
     ["tests/test_existing.py::test_old"], None, "new case tests/test_existing.py::test_new_feature was not executed"),
    ("an unreadable changed file",
     {"tests/test_existing.py": OLD, "tests/ui.test.js": "test('x', () => {})\n"},
     ["tests/test_existing.py::test_old"], {"tests/test_existing.py": OLD, "tests/ui.test.js": "\n"},
     "which cases are new in tests/ui.test.js cannot be read"),
    ("no run-start record", {"tests/test_existing.py": OLD}, ["tests/test_existing.py::test_old"], False,
     "the run-start tests were not recorded"),
])
def test_new_tests_are_new_case_identities_shown_executed(tmp_path, name, files, passed, start, problem):
    session, run = _cases(tmp_path, files, passed, start=start)
    got = session._population_problem("R1", run)
    assert (problem in got) if problem else got == "", (name, got)


def test_an_unknown_finding_survives_closed_normalization_and_a_mixed_claim_keeps_its_scenario(tmp_path):
    from types import SimpleNamespace

    from quadratus.session import _read_resolves
    session = _registry(tmp_path)
    resolves, _spec_ = _read_resolves(session._normalize_resolves(_spec("x\nCOVERS: R22\nRESOLVES: RFT4-14, RFT8-2")))
    assert resolves == ["RFT8-2"]
    session.memory.ledger.requirements["R30"] = ("`pytest -q` passes, including every test in `tests/test_a.py`, "
                                                 "and refunds are atomic")
    spec = SimpleNamespace(scope=SimpleNamespace(proofs=None, review_only=False), task_id="t5")
    assert "declares no proof scenarios" in session._proof_problem(spec, ["R30"])
