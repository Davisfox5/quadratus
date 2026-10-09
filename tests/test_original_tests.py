"""Run-start tests are checked against the delivered source when a task
changes or removes them (Codex decision D, #35 6076286834).

Series rule-119c83f f2: app.js read ``window.location.hash.match`` at load,
t2 changed the original helper tests/ui/load_app.js so the new code loaded
under it, the gate ran the edited helper and passed, and every original
Node UI test failed against the delivered code."""

import shutil
import sys
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import (
    REPORT_TOKEN,
    GateCommand,
    GateSuite,
    IntegrationGate,
    attribute,
    is_test_support,
)
from quadratus.session import Session, SessionConfig

DECLARED = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--quadratus-report={REPORT_TOKEN}"]


def _project(tmp_path):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (root / "tests" / "helpers.py").write_text("def items():\n    return [1, 2, 3]\n")
    (root / "tests" / "conftest.py").write_text("import sys, pathlib\n"
                                                "sys.path[:0] = [str(pathlib.Path(__file__).parent),"
                                                " str(pathlib.Path(__file__).parent.parent)]\n")
    (root / "tests" / "test_app.py").write_text("import app, helpers\n\n"
                                                "def test_total():\n    assert app.total(helpers.items()) == 6\n")
    return root


def _session(tmp_path, root, gate=None):
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, integration_gate=gate))
    session._snapshot_original_tests()
    return session


def test_test_support_covers_helpers_and_fixtures_not_product_files():
    assert is_test_support("tests/ui/load_app.js"), "the f2 helper matches no test-file pattern"
    assert is_test_support("tests/browser/scenarios/09-project-search.js")
    assert is_test_support("tests/ui/bulk_coverage.test.js")
    assert is_test_support("test_app.py") and is_test_support("pkg/conftest.py")
    assert not is_test_support("static/js/app.js") and not is_test_support("app.py")
    assert not is_test_support("templates/index.html")


def test_unchanged_originals_need_no_second_run(tmp_path):
    root = _project(tmp_path)
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate)
    (root / "app.py").write_text("def total(items):\n    return sum(list(items))\n")
    result = session._check(gate)
    assert result.passed and result.receipts == ()
    assert session.original_test_runs == [], "the gate already ran the originals as they are"


def test_an_edited_original_test_cannot_pass_the_gate(tmp_path):
    root = _project(tmp_path)
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate)
    (root / "app.py").write_text("def total(items):\n    return 2 * sum(items)\n")
    (root / "tests" / "test_app.py").write_text("import app, helpers\n\n"
                                                "def test_total():\n    assert app.total(helpers.items()) == 12\n")
    assert IntegrationGate(DECLARED, cwd=root).run().passed, "the edited test passes on its own"
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = session._check(gate)
    assert not result.passed
    assert [(r.id, r.status) for r in result.receipts] == [("check", "passed"),
                                                           ("original-tests:check", "failed")]
    assert attribute(result) == dict(product=True, reasons=[]), "an assertion in an original test is product"
    assert "Original tests (run-start versions of tests/test_app.py)" in result.output
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before, "nothing written to the project"
    assert session.original_test_runs[-1]["changed"] == ["tests/test_app.py"]
    assert session.original_test_runs[-1]["passed"] is False


def test_an_edited_helper_is_restored_and_a_crash_is_not_a_pass(tmp_path):
    root = _project(tmp_path)
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate)
    (root / "app.py").write_text("def total(items):\n    return sum(i['n'] for i in items)\n")
    (root / "tests" / "helpers.py").write_text("def items():\n    return [{'n': 1}, {'n': 2}, {'n': 3}]\n")
    result = session._check(gate)
    assert not result.passed
    verdict = attribute(result)
    assert verdict["product"] is False and "TypeError" in " ".join(verdict["reasons"])


def test_a_passing_original_run_keeps_the_gate_passing(tmp_path):
    root = _project(tmp_path)
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate)
    (root / "tests" / "helpers.py").write_text("def items():\n    return [1, 2, 3]  # unchanged values\n")
    result = session._check(gate)
    assert result.passed
    assert [(r.id, r.status) for r in result.receipts] == [("check", "passed"), ("original-tests:check", "passed")]


def test_a_removed_original_test_is_run_from_its_run_start_bytes(tmp_path):
    root = _project(tmp_path)
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate)
    (root / "app.py").write_text("def total(items):\n    return 0\n")
    (root / "tests" / "test_app.py").unlink()
    (root / "tests" / "test_other.py").write_text("def test_nothing():\n    assert True\n")
    result = session._check(gate)
    assert not result.passed and result.receipts[-1].id == "original-tests:check"
    assert "test_total" in result.output and "test_nothing" not in result.receipts[-1].output, \
        "tests the run added are left to the ordinary gate"


def test_a_command_naming_a_new_test_file_is_skipped_and_recorded_unverified(tmp_path):
    root = _project(tmp_path)
    listed = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_new.py"]
    suite = GateSuite([GateCommand(id="check", argv=tuple(DECLARED)),
                       GateCommand(id="extra-1", argv=tuple(listed))], cwd=root)
    session = _session(tmp_path, root, suite)
    (root / "tests" / "test_new.py").write_text("def test_new():\n    assert True\n")
    (root / "tests" / "helpers.py").write_text("def items():\n    return [3, 2, 1]\n")
    result = session._check(suite)
    assert result.passed
    statuses = {r.id: r.status for r in result.receipts}
    assert statuses["original-tests:check"] == "passed" and statuses["original-tests:extra-1"] == "skipped"
    assert any("original-tests:extra-1 did not run" in f for f in session.open_findings)
    session._check(suite)
    assert sum("original-tests:extra-1 did not run" in f for f in session.open_findings) == 1, "recorded once"


def test_a_gate_that_cannot_be_read_is_unverified_never_passed(tmp_path):
    root = _project(tmp_path)
    fake = SimpleNamespace(cwd=root, run=lambda: IntegrationGate(DECLARED, cwd=root).run())
    session = _session(tmp_path, root, fake)
    (root / "tests" / "helpers.py").write_text("def items():\n    return [6]\n")
    result = session._check(fake)
    assert result.passed, "the gate's own result stands"
    assert any("the original tests were not run" in f for f in session.open_findings)


def test_done_is_sent_back_while_the_original_tests_fail(tmp_path):
    root = _project(tmp_path)
    gate = IntegrationGate(DECLARED, cwd=root)
    session = _session(tmp_path, root, gate)
    (root / "app.py").write_text("def total(items):\n    return 2 * sum(items)\n")
    (root / "tests" / "test_app.py").write_text("import app, helpers\n\n"
                                                "def test_total():\n    assert app.total(helpers.items()) == 12\n")
    refusal = session._original_tests_at_done()
    assert "DONE SENT BACK" in refusal and "original-tests:check: failed" in refusal
    assert session._requirements_satisfied() is False and session._done_refusal == refusal
    (root / "app.py").write_text("def total(items):\n    return sum(items)\n")
    (root / "tests" / "test_app.py").write_text("import app, helpers\n\n"
                                                "def test_total():\n    assert app.total(helpers.items()) == 6\n")
    assert session._original_tests_at_done() == "", "restored bytes need no run"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_f2_shape_a_helper_edited_to_fit_the_code_fails_the_check(tmp_path):
    """The saved f2 cell: the original stub has no location.hash, app.js reads
    it at load, the lead adds it to the stub. The edited stub passes; the
    original one does not, and the check fails."""
    root = tmp_path / "project"
    (root / "static" / "js").mkdir(parents=True)
    (root / "tests" / "ui").mkdir(parents=True)
    (root / "static" / "js" / "app.js").write_text("var shown = 'all';\n")
    (root / "tests" / "ui" / "load_app.js").write_text(
        "const fs = require('fs'); const vm = require('vm');\n"
        "module.exports = function loadApp() {\n"
        "  const window = { location: {} };\n"
        "  const ctx = vm.createContext({ window });\n"
        "  vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/app.js', 'utf8'), ctx);\n"
        "  return ctx;\n};\n")
    (root / "tests" / "ui" / "load_app.test.js").write_text(
        "const test = require('node:test'); const assert = require('node:assert');\n"
        "const loadApp = require('./load_app.js');\n"
        "test('loads', () => { assert.ok(loadApp()); });\n")
    node = ("node", "--test", "tests/ui/load_app.test.js")
    suite = GateSuite([GateCommand(id="extra-1", argv=node)], cwd=root)
    session = _session(tmp_path, root, suite)
    (root / "static" / "js" / "app.js").write_text(
        "function readProjectSearchHash() {\n"
        "  const match = window.location.hash.match(/^#q=(.*)$/);\n"
        "  return match ? match[1] : '';\n}\nvar shown = readProjectSearchHash();\n")
    (root / "tests" / "ui" / "load_app.js").write_text(
        (root / "tests" / "ui" / "load_app.js").read_text().replace("location: {}", "location: { hash: '' }"))
    assert GateSuite([GateCommand(id="extra-1", argv=node)], cwd=root).run().passed, "the edited stub passes"
    result = session._check(suite)
    assert not result.passed
    assert [(r.id, r.status) for r in result.receipts] == [("extra-1", "passed"), ("original-tests:extra-1", "failed")]
    assert "reading 'match'" in result.receipts[-1].output
    assert session.original_test_runs[-1]["changed"] == ["tests/ui/load_app.js"]


def test_the_reviewer_is_told_which_original_tests_changed(tmp_path):
    from quadratus.session import TaskSpec
    root = _project(tmp_path)
    session = _session(tmp_path, root, IntegrationGate(DECLARED, cwd=root))
    assert "Harness record" not in session._collaborator_prompt(TaskSpec("t2", "do it"), "draft", "claude:opus")
    (root / "tests" / "helpers.py").write_text("def items():\n    return [6]\n")
    (root / "tests" / "conftest.py").unlink()
    text = session._collaborator_prompt(TaskSpec("t2", "do it"), "draft", "claude:opus")
    assert "now differ or are gone: tests/conftest.py, tests/helpers.py" in text
    assert "a test or helper changed to fit the code is not evidence" in text


def test_done_runs_the_original_tests_once_per_source(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, root, IntegrationGate(DECLARED, cwd=root))
    (root / "tests" / "helpers.py").write_text("def items():\n    return [1, 2, 3]  # same values\n")
    assert session._original_tests_at_done() == "" and session._original_tests_at_done() == ""
    assert len(session.original_test_runs) == 1, "an unchanged source is not run again"
    (root / "app.py").write_text("def total(items):\n    return sum(items)  # touched\n")
    session._original_tests_at_done()
    assert len(session.original_test_runs) == 2
