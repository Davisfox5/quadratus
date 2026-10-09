"""Acceptance that did not run cannot be audited MET (series rule-119c83f
f2, Codex #35 6076219864 and 6076286834).

t3 and t4 said their browser scenario and search commands did not run,
because the approved command list excluded them; the requirements audit
still marked all five requirements met. The audit never saw which test files
no required check named, nor any call's own account of what it could not
run, and a citation resolved for any file that exists."""

from types import SimpleNamespace

import pytest

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
                               receipts=[dict(id="check", status="passed", output="# pass 3\n# fail 0\n# skipped 0\n", command="pytest -q")]))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "an unrelated passing check lifts nothing"
    session.checks.append(dict(passed=False, command="gate suite",
                               receipts=[dict(id="extra-2", status="failed", output="# pass 3\n# fail 0\n# skipped 0\n", command=f"node {SCENARIO}")]))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "a failing run lifts nothing"
    session.checks.append(dict(passed=True, command="gate suite",
                               receipts=[dict(id="extra-2", status="passed", output="# pass 3\n# fail 0\n# skipped 0\n", command=f"node {SCENARIO}")]))
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


def test_a_blocked_line_is_read_like_not_run(tmp_path):
    """gui-sort-v5 t4 (Codex #35 6076559403) said BLOCKED, not NOT RUN."""
    session, _ = _session(tmp_path, f"R5: MET - {UNIT}\n")
    _not_run(session, "t4", ["R5"], "BLOCKED: desktop and mobile browser checks - Playwright unavailable")
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    _not_run(session, "t4", ["R1"], "BLOCKING: a reviewer's finding is not a report")
    assert all(e["requirements"] == ["R5"] for e in session.unexecuted_acceptance)


def _gate_result(skips, *, rid="extra-1", passed=True, required=True):
    from quadratus.integration import GateReceipt, GateResult
    report = dict(state="parsed", counts=dict(passed=37, failed=0, errors=0, skipped=skips))
    receipt = GateReceipt(id=rid, status="passed" if passed else "failed", reason="exit 0", required=required,
                          command="node --test tests/ui/sort.test.js", output=f"# pass 37\n# fail 0\n# skipped {skips}\n",
                          report=report)
    return GateResult(passed, "gate suite", 0, receipt.output, (receipt,))


def _ran(session, task, covers, result):
    import dataclasses
    session._current_covers = list(covers)
    session._note_skips(SimpleNamespace(task_id=task), result)
    session.checks.append(dict(passed=result.passed, command=result.command, output=result.output,
                               receipts=[dataclasses.asdict(r) for r in result.receipts]))
    session._current_covers = []


def test_a_passing_check_that_skips_cases_keeps_the_requirement_open(tmp_path):
    """The v5 shape: Node 37 passed, 2 browser cases skipped, gate green,
    and the audit cites the unit file that ran."""
    session, prompts = _session(tmp_path, f"R5: MET - {UNIT}\n")
    _ran(session, "t1", ["R1"], _gate_result(0))
    _ran(session, "t4", ["R5"], _gate_result(2))
    met, why = session._audit_requirements(ids=["R5"])["R5"]
    assert met is False and "t4: extra-1: 2 skipped test case(s)" in why
    assert "reported NOT RUN: extra-1: 2 skipped" in prompts[-1]
    _ran(session, "t5", ["R1"], _gate_result(1))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "fewer skips is not every case run"
    _ran(session, "t6", ["R5"], _gate_result(0))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True, "a later run with no skips lifts it"


def test_the_first_checks_skips_lower_like_any_other(tmp_path):
    """Codex review of b6ba3ba, F4: first-check uncertainty is not approval."""
    session, _ = _session(tmp_path, f"R1: MET - {UNIT}\n")
    _ran(session, "t1", ["R1"], _gate_result(3))
    assert session._audit_requirements(ids=["R1"])["R1"][0] is False


def test_an_optional_commands_skips_record_nothing(tmp_path):
    session, _ = _session(tmp_path, f"R1: MET - {UNIT}\n")
    _ran(session, "t1", ["R1"], _gate_result(2, required=False))
    assert session.unexecuted_acceptance == []
    assert session._audit_requirements(ids=["R1"])["R1"][0] is True


def test_skips_in_a_real_node_check_are_counted(tmp_path):
    import shutil

    import pytest

    from quadratus.integration import GateCommand, GateSuite, skipped_count
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    (tmp_path / "a.test.js").write_text(
        "const test = require('node:test');\n"
        "test('unit', () => {});\n"
        "test('browser', { skip: 'Playwright unavailable' }, () => {});\n")
    result = GateSuite([GateCommand(id="extra-1", argv=("node", "--test", "a.test.js"))], cwd=tmp_path).run()
    assert result.passed and skipped_count(result.receipts[0]) == 1


def test_a_report_without_a_path_is_never_lifted_by_command_text(tmp_path):
    """Codex preliminary review of b6ba3ba: a substring match let any pytest
    command lift 'NOT RUN: pytest'. Only whole path arguments lift."""
    session, _ = _session(tmp_path, f"R5: MET - {UNIT}\n")
    _not_run(session, "t3", ["R5"], "NOT RUN: pytest - the browser marker suite was not approved")
    _not_run(session, "t4", ["R5"], "NOT RUN: node tests/browser/run.js - denied")
    session.checks.append(dict(passed=True, command="gate suite", receipts=[
        dict(id="check", status="passed", output="# pass 3\n# fail 0\n# skipped 0\n", command="python -m pytest -q"),
        dict(id="extra-1", status="passed", output="# pass 3\n# fail 0\n# skipped 0\n", command="node --test tests/browser/run.js.bak")]))
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    assert len(session._standing_not_run()) == 2, "neither a runner name nor a longer path lifts"
    session.checks.append(dict(passed=True, command="gate suite", receipts=[
        dict(id="extra-2", status="passed", output="# pass 3\n# fail 0\n# skipped 0\n", command="node tests/browser/run.js")]))
    assert [e["task"] for e in session._standing_not_run()] == ["t3"], "the named file ran; 'pytest' never lifts"


# Real node runs (Codex review of b6ba3ba: F3, F4 and F6 controls).
NODE_UNIT = "tests/ui/unit.test.js"
NODE_BROWSER = "tests/ui/browser.test.js"


def _node_session(tmp_path, commands=None, snapshot=False):
    import shutil

    from quadratus.integration import GateCommand, GateSuite
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    (root / NODE_UNIT).write_text("const test = require('node:test'); test('unit', () => {});\n")
    (root / NODE_BROWSER).write_text("const test = require('node:test');\n"
                                     "test('browser', { skip: 'unavailable' }, () => {});\n")
    commands = [GateCommand(id=c[0], argv=tuple(node if a == "node" else a for a in c[1]), **c[2])
                for c in (commands or [("ui", ("node", "--test", NODE_UNIT, NODE_BROWSER), {})])]
    gate = GateSuite(commands, cwd=root)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True,
                                           integration_gate=gate, max_gate_fixes=0))
    session.memory.ledger.requirements.update({"R1": "unit", "R5": "browser acceptance executes"})
    session._auditor = lambda: "openai:gpt-5.6-sol"
    session._invoke_model = lambda key, prompt, **kw: (
        f"R1: MET - {NODE_UNIT}\nR5: MET - {NODE_UNIT} and {NODE_BROWSER}\n")
    if snapshot:
        session._snapshot_original_tests()
    return root, session


def _gate(session, task, covers):
    from quadratus.memory import TaskMemory
    from quadratus.session import TaskSpec
    spec = TaskSpec(task, "check")
    session._active_spec, session._current_covers = spec, list(covers)
    session._run_integration_gate("openai:gpt-5.6-sol", spec, TaskMemory(task, "x", session.store))
    session._active_spec, session._current_covers = None, []
    return session.checks[-1]


def _report(session, task, covers, text):
    _not_run(session, task, covers, text)



@pytest.mark.parametrize("mode", ["optional-skipped", "substring", "name-filtered"])
def test_a_not_run_needs_the_named_file_actually_run_whole(tmp_path, mode):
    commands = [("unit", ("node", "--test", NODE_UNIT), {})]
    if mode == "optional-skipped":
        commands.append(("browser", ("node", "--test", NODE_BROWSER),
                         dict(required=False, skip_reason="browser unavailable")))
    elif mode == "substring":
        commands.append(("other", ("node", NODE_BROWSER + ".backup.js"), {}))
    else:
        commands.append(("filtered", ("node", "--test", "--test-name-pattern=unit", NODE_BROWSER), {}))
    root, session = _node_session(tmp_path, commands)
    if mode == "substring":
        (root / (NODE_BROWSER + ".backup.js")).write_text("console.log('# pass 1\\n# fail 0')\n")
    if mode == "name-filtered":
        (root / NODE_BROWSER).write_text("const t = require('node:test'); t('unit', () => {});\n"
                                         "t('browser', () => { throw Error('browser did run'); });\n")
    _report(session, "t1", ["R5"], f"NOT RUN: {NODE_BROWSER} - browser unavailable")
    assert _gate(session, "t1", ["R5"])["passed"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_a_genuine_later_run_lifts_and_a_new_report_after_it_stands(tmp_path):
    root, session = _node_session(tmp_path)
    _report(session, "t1", ["R5"], f"NOT RUN: {NODE_BROWSER} - browser unavailable")
    (root / NODE_BROWSER).write_text("const t = require('node:test'); t('browser', () => {});\n")
    _gate(session, "t1", ["R5"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True
    _report(session, "t1", ["R5"], f"NOT RUN: {NODE_BROWSER} - unavailable again after the change")
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "the earlier run predates this report"


def test_an_unrelated_case_running_does_not_cancel_a_required_skip(tmp_path):
    root, session = _node_session(tmp_path)
    browser = root / NODE_BROWSER
    browser.write_text("const t = require('node:test');\nt('old-a', { skip: true }, () => {});\n"
                       "t('old-b', { skip: true }, () => {});\n")
    session._snapshot_original_tests()
    _gate(session, "t0", ["R1"])
    browser.write_text(browser.read_text() + "t('required-browser', { skip: true }, () => {});\n")
    _gate(session, "t1", ["R5"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    browser.write_text(browser.read_text().replace("t('old-a', { skip: true }", "t('old-a', {}"))
    _gate(session, "t2", ["R1"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_a_new_skipped_case_on_the_first_check_is_not_met(tmp_path):
    root, session = _node_session(tmp_path)
    payload = (root / NODE_BROWSER).read_text()
    (root / NODE_BROWSER).unlink()
    session._snapshot_original_tests()
    (root / NODE_BROWSER).write_text(payload)
    assert _gate(session, "t1", ["R5"])["passed"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_a_later_run_reconciles_a_historical_unnamed_file(tmp_path):
    root, session = _node_session(tmp_path, [("unit", ("node", "--test", NODE_UNIT), {})], snapshot=True)
    child = "tests/ui/new.test.js"
    session._task_before = session._capture_source()
    (root / child).write_text("const t = require('node:test'); const a = require('node:assert/strict');\n"
                              "t('actual-child-assertion', () => { a.equal(1, 1); });\n")
    _gate(session, "t1", ["R5"])
    assert child in session.unnamed_test_files
    session._invoke_model = lambda key, prompt, **kw: f"R5: MET - {child}\n"
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "not yet run"
    session._task_before = session._capture_source()
    (root / NODE_UNIT).write_text((root / NODE_UNIT).read_text() + "require('./new.test.js');\n")
    assert "actual-child-assertion" in _gate(session, "t2", ["R5"])["output"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True


def test_parallel_children_bring_their_not_run_reports_to_the_parent(tmp_path):
    """Codex review of b6ba3ba, F5."""
    from tests.test_parallel_tasks import Orchestrated, _block
    from tests.test_parallel_tasks import _session as _parallel_session
    batch = "PARALLEL\n" + _block("a.py") + "\nCOVERS: R1\n---\n" + _block("b.py") + "\nCOVERS: R2"

    class Script(Orchestrated):
        def invoke_for(self, root):
            original = super().invoke_for(root)

            def invoke(model, prompt, system=None, allow_writes=False):
                if "You are an independent auditor." in prompt:
                    return "R1: MET - a.py\nR2: MET - b.py"
                answer = original(model, prompt, system, allow_writes)
                if "You are leading" in prompt:
                    answer = answer.replace("CHANGED:", "NOT RUN: tests/browser/run.js - unavailable\nCHANGED:")
                return answer
            return invoke
    session, _ = _parallel_session(tmp_path, Script([batch, "DONE"], lead_delay=0),
                                   requirements_ledger=True, max_requirement_reopens=0)
    session.memory.ledger.requirements = {"R1": "a works in a browser", "R2": "b works in a browser"}
    session.requirement_reviews.append({"reviewer": "claude:opus", "result": "reviewed"})
    session.run(max_tasks=4)
    assert sorted(e["requirements"][0] for e in session.unexecuted_acceptance) == ["R1", "R2"]
    assert not session.completed
