"""Acceptance that did not run cannot be audited MET (series rule-119c83f
f2, Codex #35 6076219864 and 6076286834).

t3 and t4 said their browser scenario and search commands did not run,
because the approved command list excluded them; the requirements audit
still marked all five requirements met. The audit never saw which test files
no required check named, nor any call's own account of what it could not
run, and a citation resolved for any file that exists."""

from types import SimpleNamespace

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig

SCENARIO = "tests/browser/scenarios/09-project-search.js"
UNIT = "tests/ui/project_search.test.js"


def _session(tmp_path, reply):
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    (root / "tests" / "browser" / "scenarios").mkdir(parents=True)
    (root / UNIT).write_text("// ran through the five-file check\n")
    (root / SCENARIO).write_text("// requested, never executed\n")
    (root / "tests" / "ui" / "new_search.test.js").write_text("// no check names it\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update({
        "R1": "typing filters the cards", "R5": "the list logs no console errors while typing"})
    prompts = []
    session._auditor = lambda: "openai:gpt-5.6-sol"
    session._invoke_model = lambda key, prompt, **kw: prompts.append(prompt) or reply
    return session, prompts


def _not_run(session, task, covers, reply, role="revision"):
    session._active_spec = SimpleNamespace(task_id=task)
    session._current_covers = list(covers)
    session._record_not_run("openai:gpt-5.6-sol", role, reply)
    session._active_spec, session._current_covers = None, []


def test_the_f2_shape_mixed_citations_with_an_unexecuted_scenario_is_refused(tmp_path):
    """The audit cites a unit file that did run beside the scenario that did
    not. The run unit file does not stand in for the scenario."""
    reply = (f"R1: MET - {UNIT}\n"
             f"R5: MET - {UNIT} and {SCENARIO} cover typing with no console errors\n")
    session, prompts = _session(tmp_path, reply)
    _not_run(session, "t3", ["R5"], "Added the scenario.\nNOT RUN: " + SCENARIO
             + " - the approved check commands do not include the browser scenario runner\n"
             + 'CHANGED: ["' + SCENARIO + '"]')
    verdicts = session._audit_requirements()
    assert verdicts["R1"] == (True, UNIT)
    met, why = verdicts["R5"]
    assert met is False and "reported NOT RUN by the covering task: t3: " + SCENARIO in why
    assert "## Not shown to have run" in prompts[0] and "task t3 (revision) reported NOT RUN" in prompts[0]
    assert session.requirement_audits[-1]["lowered"][0]["requirement"] == "R5"
    assert session.requirement_audits[-1]["lowered"][0]["auditor_said"].startswith(UNIT)


def test_a_met_citing_only_unnamed_test_files_is_not_met(tmp_path):
    session, prompts = _session(tmp_path, "R1: MET - tests/ui/new_search.test.js\n")
    session.unnamed_test_files = ["tests/ui/new_search.test.js"]
    met, why = session._audit_requirements(ids=["R1"])["R1"]
    assert met is False and "no required check names" in why
    assert "tests/ui/new_search.test.js" in prompts[0]


def test_a_met_citing_a_run_file_beside_an_unnamed_one_stands(tmp_path):
    """B alone lowers only a MET whose every citation is unnamed; a MET that
    also cites a file that ran is left to C and the auditor's judgement."""
    session, _ = _session(tmp_path, f"R1: MET - {UNIT} and tests/ui/new_search.test.js\n")
    session.unnamed_test_files = ["tests/ui/new_search.test.js"]
    assert session._audit_requirements(ids=["R1"])["R1"][0] is True


def test_not_run_only_lowers_the_requirements_its_task_covers(tmp_path):
    session, _ = _session(tmp_path, f"R1: MET - {UNIT}\nR5: MET - {UNIT}\n")
    _not_run(session, "t4", ["R1"], "NOT RUN: grep -r projectSearch static - not an approved command")
    verdicts = session._audit_requirements()
    assert verdicts["R1"][0] is False and verdicts["R5"][0] is True


def test_not_run_never_raises_a_verdict(tmp_path):
    session, _ = _session(tmp_path, "R5: NOT MET - no console check\n")
    _not_run(session, "t3", ["R5"], "NOT RUN: none")
    assert session.unexecuted_acceptance == [], "'none' is not a report"
    assert session._audit_requirements(ids=["R5"])["R5"] == (False, "no console check")


def test_only_a_later_passing_check_naming_the_item_lifts_a_report(tmp_path):
    session, _ = _session(tmp_path, f"R5: MET - {UNIT}\n")
    _not_run(session, "t3", ["R5"], f"NOT RUN: {SCENARIO} - not approved")
    session.checks.append(dict(passed=True, command="gate suite",
                               receipts=[dict(id="check", command="pytest -q")]))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "an unrelated passing check lifts nothing"
    session.checks.append(dict(passed=False, command="gate suite",
                               receipts=[dict(id="extra-2", command=f"node {SCENARIO}")]))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "a failing run lifts nothing"
    session.checks.append(dict(passed=True, command="gate suite",
                               receipts=[dict(id="extra-2", command=f"node {SCENARIO}")]))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True


def test_prose_is_not_parsed_and_the_editing_call_is_told_the_line(tmp_path):
    """Only the marked line is read. f2's t3 and t4 wrote prose; prose is not
    parsed, so their reports would still have been missed. The editing call
    is now told the line."""
    session, _ = _session(tmp_path, "")
    _not_run(session, "t3", ["R5"], "The browser scenario was not executed because it is not approved.")
    assert session.unexecuted_acceptance == []
    import inspect

    from quadratus import runtime
    assert "'NOT RUN: <command or file> - <why>'" in inspect.getsource(runtime)


def test_editing_calls_record_their_not_run_lines(tmp_path):
    session, _ = _session(tmp_path, "done\nNOT RUN: node tests/browser/run.js - denied\nCHANGED: []")
    session._writes = lambda: False
    session._active_spec = SimpleNamespace(task_id="t3")
    session._current_covers = ["R5"]
    session._edit("openai:gpt-5.6-sol", "prompt", role="lead")
    assert [(e["task"], e["role"], e["item"], e["requirements"]) for e in session.unexecuted_acceptance] == [
        ("t3", "lead", "node tests/browser/run.js", ["R5"])]
