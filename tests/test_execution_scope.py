"""Execution facts keep the scope they were observed in (Codex review of
81adcc7, R1-R5).

R1: an added pytest.ini hid an unchanged original test. R2: a pytest.ini
``-k`` filter made a named file's one selected case look like a whole run.
R3: a product edit turned a case into a skip and nothing was recorded
because no test file changed. R4: a skip the run-start source already had,
or one in the restored original suite, blocked acceptance that did run. R5:
a command run in tests/ui never matched the project path of the file it
named."""

import shutil
import sys

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import (
    REPORT_TOKEN,
    GateCommand,
    GateSuite,
    IntegrationGate,
    case_outcomes,
    config_selection,
    operand_paths,
)
from quadratus.memory import TaskMemory
from quadratus.session import Session, SessionConfig, TaskSpec

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--quadratus-report={REPORT_TOKEN}"]
UNIT, BROWSER = "tests/ui/unit.test.js", "tests/ui/browser.test.js"


def _session(tmp_path, root, gate, reply):
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True,
                                           integration_gate=gate, max_gate_fixes=0))
    session.memory.ledger.requirements = {"R1": "unit requirement", "R5": "browser acceptance executes"}
    session._auditor = lambda: "claude:opus"
    session._invoke_model = lambda *a, **k: reply
    return session


def _gate(session, task, covers):
    spec = TaskSpec(task, "offline check")
    session._active_spec, session._current_covers = spec, list(covers)
    session._run_integration_gate("claude:opus", spec, TaskMemory(task, "claude:opus", session.store))
    return session.checks[-1]


def _not_run(session, item, task="t1", covers=("R5",)):
    session._active_spec, session._current_covers = TaskSpec(task, "offline"), list(covers)
    session._record_not_run("openai:gpt-5.6-sol", "lead", f"NOT RUN: {item} - unavailable\nCHANGED: []")


def _node_project(tmp_path, browser):
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    (root / UNIT).write_text("const test=require('node:test'); test('unit',()=>{});\n")
    (root / BROWSER).write_text(browser)
    return root


@pytest.mark.parametrize("name", ["pytest.ini", ".pytest.ini", "tests/conftest.py"])
def test_an_added_selector_cannot_hide_an_unchanged_original_test(tmp_path, name):
    from tests.test_original_tests import DECLARED, _project
    root = _project(tmp_path)
    if name == "tests/conftest.py":
        (root / name).unlink()
    (root / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate, "")
    session._snapshot_original_tests()
    (root / "app.py").write_text("def total(items):\n    return 12\n")
    if name == "tests/conftest.py":
        (root / name).write_text("import sys, pathlib\n"
                                 "sys.path[:0] = [str(pathlib.Path(__file__).parent),"
                                 " str(pathlib.Path(__file__).parent.parent)]\n"
                                 "collect_ignore = ['test_app.py']\n")
    else:
        (root / name).write_text("[pytest]\naddopts = --ignore=tests/test_app.py\n")
    assert IntegrationGate(DECLARED, cwd=root).run().passed, "the added selector hides the original test"
    result = session._check(gate)
    assert not result.passed and "added test configuration left out: " + name in result.output
    assert session.original_test_runs[-1]["added"] == [name]
    assert "DONE SENT BACK" in session._original_tests_at_done()


def test_configuration_deselection_is_not_a_whole_file_run(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    named = "tests/test_browser.py"
    (root / named).write_text("def test_unit():\n    assert True\n"
                              "def test_browser():\n    raise AssertionError('browser ran')\n")
    (root / "pytest.ini").write_text("[pytest]\naddopts = -k unit\n")
    gate = GateSuite([GateCommand(id="browser", argv=(*PYTEST, named))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {named}")
    session._snapshot_original_tests()
    _not_run(session, named)
    check = _gate(session, "t1", ["R5"])
    assert check["passed"] and "1 deselected" in check["output"]
    assert check["receipts"][0]["selection"] == "pytest.ini: addopts = -k unit"
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_selection_outside_argv_is_found_and_never_misread():
    assert config_selection(["node", "--test", "a.test.js"], "/nowhere", "/nowhere") == ""
    import os
    from unittest import mock
    with mock.patch.dict(os.environ, {"PYTEST_ADDOPTS": "-m 'not browser'"}):
        assert config_selection([sys.executable, "-m", "pytest"], "/nowhere", "/nowhere").startswith(
            "PYTEST_ADDOPTS="), "an addopts that opens with -m is pytest's marker filter"
        assert config_selection(["node", "--test"], "/nowhere", "/nowhere") == ""
    with mock.patch.dict(os.environ, {"NODE_OPTIONS": "--test-name-pattern=unit"}):
        assert config_selection(["node", "--test"], "/nowhere", "/nowhere").startswith("NODE_OPTIONS=")


def test_a_relative_operand_is_read_from_the_command_cwd():
    assert operand_paths(["node", "--test", "browser.test.js"], "/p", "tests/ui") == [BROWSER]
    assert operand_paths(["node", "--test", "../ui/browser.test.js"], "/p", "tests/ui") == [BROWSER]
    assert operand_paths(["node", "--test", "/p/tests/ui/browser.test.js"], "/p", "tests/ui") == [BROWSER]
    assert operand_paths(["node", "--test", "./tests/ui/browser.test.js"], "/p") == [BROWSER]


@needs_node
def test_a_run_in_a_subdirectory_discharges_the_project_path(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');t('browser',()=>{});\n")
    gate = GateSuite([GateCommand(id="browser", argv=(NODE, "--test", "browser.test.js"), cwd="tests/ui")], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    _not_run(session, BROWSER)
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False, "nothing has run yet"
    check = _gate(session, "t1", ["R5"])
    assert check["receipts"][0]["cwd"] == "tests/ui"
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True


@needs_node
def test_a_product_edit_that_skips_a_case_is_recorded(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');const f=require('node:fs');\n"
                                   "t('browser',{skip:!JSON.parse(f.readFileSync('enabled.json','utf8'))},()=>{});\n")
    (root / "enabled.json").write_text("true")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {UNIT} and {BROWSER}")
    session._snapshot_original_tests()
    _gate(session, "t0", ["R1"])
    (root / "enabled.json").write_text("false")
    _gate(session, "t1", ["R5"])
    assert not session._tests_changed_in_run()
    entry = session.unexecuted_acceptance[-1]
    assert entry["cases"] == ["browser"] and "not skipped at run start" in entry["item"]
    assert session.skip_baselines[-1]["receipts"]["ui"] == dict(skipped=0, cases=[])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    (root / "enabled.json").write_text("true")
    _gate(session, "t2", ["R5"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True, "the named case ran"


@needs_node
def test_a_skip_the_run_start_source_had_blocks_nothing(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');t('old-platform',{skip:true},()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R1: MET - {UNIT}")
    session._snapshot_original_tests()
    (root / UNIT).write_text((root / UNIT).read_text() + "test('new-unit',()=>{});\n")
    _gate(session, "t1", ["R1"])
    assert session.unexecuted_acceptance == []
    assert session._audit_requirements(ids=["R1"])["R1"][0] is True


@needs_node
def test_the_restored_original_suite_never_records_a_skip(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');t('old',{skip:true},()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text("const t=require('node:test');t('old',()=>{});t('required-browser',()=>{});\n")
    check = _gate(session, "t1", ["R5"])
    assert [r["id"] for r in check["receipts"]] == ["ui", "original-tests:ui"]
    assert session.unexecuted_acceptance == [] and session._audit_requirements(ids=["R5"])["R5"][0] is True


@needs_node
def test_an_old_skip_that_now_runs_does_not_hide_a_new_one(tmp_path):
    """F4 kept: names, not totals, decide when the runner prints them."""
    root = _node_project(tmp_path, "const t=require('node:test');t('old-a',{skip:true},()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text("const t=require('node:test');t('old-a',()=>{});"
                                "t('required-browser',{skip:true},()=>{});\n")
    _gate(session, "t1", ["R5"])
    assert session.unexecuted_acceptance[-1]["cases"] == ["required-browser"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_case_outcomes_reads_spec_tap_and_pytest_lines():
    spec = "﹣ old (0.2ms) # SKIP\n✔ unit (0.1ms)\n✖ bad (0.3ms)\n﹣ later (0.1ms) # TODO\n"
    skipped, executed = case_outcomes(spec)
    assert dict(skipped) == {"old": 1} and dict(executed) == {"unit": 1, "bad": 1}
    skipped, executed = case_outcomes("ok 1 - a # SKIP why\nok 2 - b\nnot ok 3 - c\nok 4 - d # TODO\n")
    assert dict(skipped) == {"a": 1} and dict(executed) == {"b": 1, "c": 1}
    skipped, executed = case_outcomes("tests/t.py::test_a SKIPPED (x)\ntests/t.py::test_b PASSED\n")
    assert dict(skipped) == {"tests/t.py::test_a": 1} and dict(executed) == {"tests/t.py::test_b": 1}
