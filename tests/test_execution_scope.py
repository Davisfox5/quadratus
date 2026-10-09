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
    skipped, executed = case_outcomes("tests/t.py::test_c XFAIL ([NOTRUN] why)\ntests/t.py::test_d XPASS\n")
    assert not skipped and dict(executed) == {"tests/t.py::test_d": 1}, "an XFAIL may never have run"


# Codex review of 67c9fad, S1-S5.

def _flagged(tmp_path, unit, browser):
    root = _node_project(tmp_path, browser)
    (root / UNIT).write_text(unit)
    (root / "flags.json").write_text('{"old": false, "browser": true}')
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {UNIT} and {BROWSER}")
    session._snapshot_original_tests()
    (root / "flags.json").write_text('{"old": true, "browser": false}')
    return session


FLAGS = ("const t=require('node:test');const f=require('node:fs');"
         "const flags=JSON.parse(f.readFileSync('flags.json','utf8'));\n")


@needs_node
def test_a_name_two_cases_share_is_not_a_historical_skip(tmp_path):
    session = _flagged(tmp_path, FLAGS + "t('case',{skip:!flags.old},()=>{});\n",
                       FLAGS + "t('case',{skip:!flags.browser},()=>{});\n")
    _gate(session, "t1", ["R5"])
    assert session.unexecuted_acceptance[-1]["cases"] == ["case"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@needs_node
def test_names_past_the_output_tail_are_still_read(tmp_path):
    padding = "".join(f"t('padding-{i}-a-long-distinct-case-name-for-the-output-tail',()=>{{}});\n" for i in range(90))
    session = _flagged(tmp_path, "const t=require('node:test');t('unit',()=>{});\n",
                       FLAGS + "t('old-platform',{skip:!flags.old},()=>{});"
                       "t('required-browser',{skip:!flags.browser},()=>{});\n" + padding)
    check = _gate(session, "t1", ["R5"])
    assert "required-browser" not in check["receipts"][0]["output"]
    assert check["receipts"][0]["cases"]["skipped"] == {"required-browser": 1}
    assert session.unexecuted_acceptance[-1]["cases"] == ["required-browser"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_a_collection_hook_or_plugin_is_selection(tmp_path):
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    argv = [sys.executable, "-m", "pytest", "tests/ui/test_b.py"]
    assert config_selection(argv, root, root) == ""
    (root / "tests" / "conftest.py").write_text("def pytest_collection_modifyitems(items):\n    items[:] = []\n")
    assert config_selection(argv, root, root) == "tests/conftest.py: pytest_collection_modifyitems"
    (root / "tests" / "conftest.py").write_text("collect_ignore = ['ui']\n")
    assert config_selection(argv, root, root) == "tests/conftest.py: collect_ignore"
    (root / "tests" / "conftest.py").unlink()
    assert config_selection([*argv, "-p", "no:cacheprovider"], root, root) == ""
    assert config_selection([*argv, "-p", "myfilter"], root, root) == "argv loads plugin myfilter"
    assert config_selection(["node", "--experimental-config-file=c.json", "--test", "a.test.js"], root, root) == (
        "node configuration file: --experimental-config-file=c.json")


def test_a_silent_collection_hook_cannot_discharge_a_named_file(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    named = "tests/test_browser.py"
    (root / named).write_text("def test_unit():\n    assert True\n"
                              "def test_browser():\n    raise AssertionError('browser ran')\n")
    (root / "tests" / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if i.name != 'test_browser']\n")
    gate = GateSuite([GateCommand(id="browser", argv=(*PYTEST, named))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {named}")
    session._snapshot_original_tests()
    _not_run(session, named)
    check = _gate(session, "t1", ["R5"])
    assert check["passed"] and "deselected" not in check["output"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@needs_node
def test_a_single_command_gate_keeps_its_cwd(tmp_path):
    """Session construction binds a gate to the project; one set afterwards,
    as the reviewer's control does, can run in a subdirectory."""
    root = _node_project(tmp_path, "const t=require('node:test');t('browser',()=>{});\n")
    session = _session(tmp_path, root, None, f"R5: MET - {BROWSER}")
    session.config.integration_gate = IntegrationGate((NODE, "--test", "browser.test.js"), cwd=root / "tests" / "ui")
    session._snapshot_original_tests()
    _not_run(session, BROWSER)
    check = _gate(session, "t1", ["R5"])
    assert check["receipts"] == [] and check["command_cwd"] == "tests/ui"
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True


@needs_node
def test_the_joined_primary_receipt_keeps_its_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--test-name-pattern=unit")
    root = _node_project(tmp_path, "const t=require('node:test');t('unit',()=>{});"
                                   "t('browser',()=>{throw Error('browser ran')});\n")
    (root / "tests" / "helper.js").write_text("// original helper\n")
    session = _session(tmp_path, root, IntegrationGate((NODE, "--test", BROWSER), cwd=root), f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / "tests" / "helper.js").write_text("// changed helper\n")
    _not_run(session, BROWSER)
    check = _gate(session, "t1", ["R5"])
    primary = next(r for r in check["receipts"] if r["id"] == "check")
    assert primary["selection"].startswith("NODE_OPTIONS=")
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_an_xfail_that_never_ran_does_not_discharge_a_named_skip(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    named = "tests/test_browser.py"
    body = ("import pytest\ndef test_unit():\n    assert True\n@MARK\n"
            "def test_browser():\n    raise AssertionError('browser ran')\n")
    (root / named).write_text("def test_unit():\n    assert True\ndef test_browser():\n    assert True\n")
    gate = GateSuite([GateCommand(id="browser", argv=(*PYTEST, "-vv", named))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {named}")
    session._snapshot_original_tests()
    (root / named).write_text(body.replace("MARK", "pytest.mark.skip(reason='missing browser')"))
    _gate(session, "t1", ["R5"])
    assert session.unexecuted_acceptance[-1]["cases"] == [named + "::test_browser"]
    (root / named).write_text(body.replace("MARK", "pytest.mark.xfail(run=False, reason='still unavailable')"))
    check = _gate(session, "t2", ["R1"])
    assert "XFAIL" in check["output"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


# Codex review of d157378, T1-T4.

@needs_node
def test_two_long_names_sharing_a_prefix_are_two_cases(tmp_path):
    prefix = "x" * 320
    session = _flagged(tmp_path, "const t=require('node:test');t('unit',()=>{});\n",
                       FLAGS + f"t('{prefix}-old-platform',{{skip:!flags.old}},()=>{{}});"
                       f"t('{prefix}-required-browser',{{skip:!flags.browser}},()=>{{}});\n")
    _gate(session, "t1", ["R5"])
    assert len(session.unexecuted_acceptance) == 1
    assert session.unexecuted_acceptance[-1]["cases"][0].endswith(
        __import__("hashlib").sha256((prefix + "-required-browser").encode()).hexdigest())
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@needs_node
def test_past_the_case_bound_no_skip_is_historical(tmp_path, monkeypatch):
    """Two cases share a name: the one that runs is printed first and pushed
    out of the output tail by padding, the skipped one is printed last. A
    product-only change swaps which acceptance each stands for, and the
    output is the same both times. Over the bound the record is None, and
    the tail, which shows one shared-case, never stands in for identity (T2;
    the bound is lowered here only to keep the test small)."""
    from quadratus import integration
    monkeypatch.setattr(integration, "_MAX_CASES", 3)
    padding = "".join(f"t('padding-{i}-a-long-distinct-case-name-for-the-output-tail',()=>{{}});\n"
                      for i in range(60))
    session = _flagged(tmp_path, "const t=require('node:test');t('unit',()=>{});\n",
                       FLAGS + "t('shared-case',()=>{});\n" + padding
                       + "t('shared-case',{skip:'one of old or browser is unavailable'},()=>{});\n")
    check = _gate(session, "t1", ["R5"])
    receipt = check["receipts"][0]
    assert receipt["cases"] is None
    shown = sum(sum(c.values()) for c in integration.case_outcomes(receipt["output"]))
    assert shown < 62 and integration.case_outcomes(receipt["output"])[0] == {"shared-case": 1}, \
        "the tail shows only part of the run, the skipped shared-case among it"
    assert integration.case_outcomes(receipt["output"])[1]["shared-case"] == 0, "the run one is out of the tail"
    assert session.unexecuted_acceptance and session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_pytest_plugins_in_the_environment_is_selection(monkeypatch, tmp_path):
    argv = [sys.executable, "-m", "pytest", "tests/test_b.py"]
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    assert config_selection(argv, tmp_path, tmp_path) == ""
    monkeypatch.setenv("PYTEST_PLUGINS", "selection_plugin")
    assert config_selection(argv, tmp_path, tmp_path) == "PYTEST_PLUGINS=selection_plugin"
    assert config_selection(["node", "--test", "a.test.js"], tmp_path, tmp_path) == ""


@needs_node
def test_a_case_renamed_in_another_file_cannot_discharge_a_named_skip(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');t('browser',()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text("const t=require('node:test');t('browser',{skip:'missing browser'},()=>{});\n")
    _gate(session, "t1", ["R5"])
    assert session.unexecuted_acceptance[-1]["cases"] == ["browser"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    (root / UNIT).write_text("const t=require('node:test');t('browser',()=>{});\n")
    (root / BROWSER).write_text(
        "const t=require('node:test');t('browser-still-unavailable',{skip:'missing browser'},()=>{});\n")
    _gate(session, "t2", ["R1"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


# Codex review of e530b89, U1-U2.

@needs_node
def test_a_historical_name_reused_in_another_file_is_a_new_skip(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');t('browser',()=>{});\n")
    (root / UNIT).write_text("const t=require('node:test');t('smoke',()=>{});t('legacy-platform',{skip:true},()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text("const t=require('node:test');t('browser',{skip:true},()=>{});\n")
    _gate(session, "t1", ["R5"])
    assert session.unexecuted_acceptance[-1]["case_files"] == {"browser": [BROWSER]}
    (root / UNIT).write_text("const t=require('node:test');t('smoke',()=>{});t('browser',()=>{});\n")
    (root / BROWSER).write_text("const t=require('node:test');t('legacy-platform',{skip:true},()=>{});\n")
    _gate(session, "t2", ["R1"])
    assert session.unexecuted_acceptance[-1]["cases"] == ["legacy-platform"], "the old name in a new file is new"
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@pytest.mark.parametrize("unit_skip", ["historical", "new"])
def test_a_file_qualified_case_recovers_its_own_requirement(tmp_path, unit_skip):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    browser, unit = "tests/test_browser.py", "tests/test_unit.py"
    ran = "def test_browser():\n    assert True\n"
    skipped = ("import pytest\n@pytest.mark.skip(reason='browser unavailable')\n"
               "def test_browser():\n    raise AssertionError('browser did not run')\n")

    def unit_source(skip):
        return ("import pytest\ndef test_smoke():\n    assert True\n"
                + ("@pytest.mark.skip(reason='unit unavailable')\n" if skip else "") + "def test_unit():\n    assert True\n")
    (root / browser).write_text(ran)
    (root / unit).write_text(unit_source(unit_skip == "historical"))
    gate = GateSuite([GateCommand(id="acceptance", argv=(*PYTEST, "-vv", browser, unit))], cwd=root)
    session = _session(tmp_path, root, gate, f"R1: MET - {unit}\nR5: MET - {browser}")
    session._snapshot_original_tests()
    (root / browser).write_text(skipped)
    _gate(session, "t1", ["R5"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    (root / browser).write_text(ran)
    (root / unit).write_text(unit_source(True))
    _gate(session, "t2", ["R1"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True, "the browser case ran under its own file"
    if unit_skip == "new":
        assert session._audit_requirements(ids=["R1"])["R1"][0] is False, "the new unit skip still blocks R1"


# Codex review of 5292fc2, V1-V2.

@needs_node
def test_a_node_name_with_colons_is_not_a_pytest_id(tmp_path):
    name = "browser::acceptance"
    root = _node_project(tmp_path, f"const t=require('node:test');t('{name}',()=>{{}});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text(f"const t=require('node:test');t('{name}',{{skip:true}},()=>{{}});\n")
    check = _gate(session, "t1", ["R5"])
    assert check["receipts"][0]["cases"]["qualified"] == []
    assert session.unexecuted_acceptance[-1]["case_files"] == {name: [BROWSER]}
    (root / UNIT).write_text(f"const t=require('node:test');t('unit',()=>{{}});t('{name}',()=>{{}});\n")
    (root / BROWSER).write_text("const t=require('node:test');t('browser-later',{skip:true},()=>{});\n")
    _gate(session, "t2", ["R1"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@needs_node
def test_an_unchanged_old_skip_beside_a_new_test_stays_historical(tmp_path):
    root = _node_project(tmp_path, "const t=require('node:test');t('old-platform',{skip:true},()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R1: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text((root / BROWSER).read_text() + "t('new-unit-acceptance',()=>{});\n")
    _gate(session, "t1", ["R1"])
    assert session.unexecuted_acceptance == []
    assert session._audit_requirements(ids=["R1"])["R1"][0] is True


def test_a_pytest_case_block_ignores_its_neighbours_and_sees_its_decorators(tmp_path):
    from quadratus.session import _case_block
    key = "tests/test_b.py::test_old"
    old = "import pytest\n@pytest.mark.skip(reason='x')\ndef test_old():\n    pass\n"
    assert _case_block(key, True, old) == _case_block(key, True, old + "def test_new():\n    pass\n")
    assert _case_block(key, True, old) != _case_block(key, True, old.replace("reason='x'", "reason='y'"))
    assert _case_block(key, True, "def test_other():\n    pass\n") is None


# Codex review of f2f66ff, W1-W3.

def test_a_case_line_counts_only_for_its_own_runner():
    from quadratus.integration import case_record, runner_of
    printed = "tests/ui/browser.test.js::test_browser XFAIL\n✔ unit (0.1ms)\nok 2 - tap-case\n"
    assert runner_of(["node", "--test", "a.test.js"]) == "node"
    assert runner_of([sys.executable, "-m", "pytest", "-q"]) == "pytest"
    assert runner_of(["make", "test"]) is None
    node = case_record(printed, "node")
    assert node["qualified"] == [] and node["other"] == {} and node["executed"] == {"unit": 1, "tap-case": 1}
    pytest_record = case_record(printed, "pytest")
    assert pytest_record["qualified"] == ["tests/ui/browser.test.js::test_browser"]
    assert pytest_record["executed"] == {}, "spec and TAP lines are not pytest cases"
    assert case_record(printed)["qualified"] == [], "an unknown runner qualifies nothing"


@needs_node
def test_a_logged_pytest_line_does_not_qualify_a_node_case(tmp_path):
    name = "tests/ui/browser.test.js::test_browser"
    log = f"console.log('{name} XFAIL');"
    root = _node_project(tmp_path, f"const t=require('node:test');t('{name}',()=>{{}});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", "--test-reporter=spec", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text(f"const t=require('node:test');{log}t('{name}',{{skip:true}},()=>{{}});\n")
    check = _gate(session, "t1", ["R5"])
    assert check["receipts"][0]["cases"]["qualified"] == []
    (root / UNIT).write_text(f"const t=require('node:test');{log}t('unit',()=>{{}});t('{name}',()=>{{}});\n")
    (root / BROWSER).write_text("const t=require('node:test');t('browser-still-unavailable',{skip:true},()=>{});\n")
    _gate(session, "t2", ["R1"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@needs_node
def test_a_changed_multiline_body_is_not_historical(tmp_path):
    def browser(body):
        return f"const t=require('node:test');\nt('browser', {{skip: true}}, () => {{\n  {body}\n}});\n"
    root = _node_project(tmp_path, browser("const legacy = 'old platform';"))
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text(browser("throw new Error('required browser acceptance never ran');"))
    _gate(session, "t1", ["R5"])
    assert session.unexecuted_acceptance and session.unexecuted_acceptance[-1]["cases"] == ["browser"]
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


def test_a_pytest_id_resolves_along_its_class_path():
    from quadratus.session import _case_block
    source = ("import pytest\nclass TestBrowser:\n    @pytest.mark.skip\n    def test_acceptance(self):\n        a = 1\n"
              "class TestLegacy:\n    @pytest.mark.skip\n    def test_acceptance(self):\n        b = 2\n")
    swapped = source.replace("a = 1", "TMP").replace("b = 2", "a = 1").replace("TMP", "b = 2")
    key = "tests/test_b.py::TestBrowser::test_acceptance"
    assert _case_block(key, True, source) != _case_block(key, True, swapped)
    assert _case_block("tests/test_b.py::test_acceptance", True, source) is None, "no module-level test of that name"
    assert _case_block(key + "[chromium]", True, source) == _case_block(key, True, source)
    twice = source + "class TestBrowser:\n    pass\n"
    assert _case_block(key, True, twice) is None, "an ambiguous class is unknown"


# Codex review of 02accbd, X1-X2.

def test_a_runner_is_read_from_the_executable_not_an_operand():
    from quadratus.integration import config_selection, runner_of
    assert runner_of(["node", "--test", "tests/pytest", "tests/ui/b.test.js"]) == "node"
    assert runner_of(["node", "--test", "pytest"]) == "node"
    assert runner_of([sys.executable, "-I", "-m", "pytest", "-q"]) == "pytest"
    assert runner_of(["uv", "run", "pytest", "-q"]) == "pytest"
    assert runner_of(["make", "pytest"]) is None
    assert config_selection(["node", "--test", "tests/pytest"], "/nowhere", "/nowhere") == ""


@pytest.mark.parametrize("literal", ["/[)}]/", "/a/", "`${x}`"])
def test_a_call_this_scan_cannot_read_is_unknown(literal):
    from quadratus.session import _case_block
    js = f"const t=require('node:test');\nt('browser',{{skip:true}},()=>{{\n  const v={literal};\n  const x=1;\n}});\n"
    assert _case_block("browser", False, js) is None
    assert _case_block("browser", False, js.replace(literal, "'[)}]'")) is not None, "brackets in a string are skipped"


@needs_node
def test_a_regex_with_brackets_keeps_a_changed_body_new(tmp_path):
    def browser(line):
        return f"const t=require('node:test');\nt('browser', {{skip: true}}, () => {{\n  const re = /[)}}]/;\n  {line}\n}});\n"
    root = _node_project(tmp_path, browser("const legacy = 'old platform';"))
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", UNIT, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text(browser("throw new Error('required browser acceptance never ran');"))
    _gate(session, "t1", ["R5"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False


@needs_node
def test_a_node_file_named_pytest_keeps_node_case_identity(tmp_path, monkeypatch):
    """The reporter comes from NODE_OPTIONS so the operand named pytest sits
    third in argv, where the old runner check looked."""
    monkeypatch.setenv("NODE_OPTIONS", "--test-reporter=spec")
    root = _node_project(tmp_path, "const t=require('node:test');t('browser',()=>{});\n")
    other = "tests/pytest"
    (root / other).write_text("const t=require('node:test');t('unit',()=>{});t('old-platform',{skip:true},()=>{});\n")
    gate = GateSuite([GateCommand(id="ui", argv=(NODE, "--test", other, BROWSER))], cwd=root)
    session = _session(tmp_path, root, gate, f"R5: MET - {BROWSER}")
    session._snapshot_original_tests()
    (root / BROWSER).write_text("const t=require('node:test');t('browser',{skip:true},()=>{});\n")
    check = _gate(session, "t1", ["R5"])
    assert check["receipts"][0]["cases"]["skipped"] == {"browser": 1, "old-platform": 1}
    assert session.unexecuted_acceptance[-1]["cases"] == ["browser"], "the unchanged old skip stays historical"
    assert session._audit_requirements(ids=["R5"])["R5"][0] is False
    (root / BROWSER).write_text("const t=require('node:test');t('browser',()=>{});\n")
    _gate(session, "t2", ["R1"])
    assert session._audit_requirements(ids=["R5"])["R5"][0] is True, "the browser case ran again"
