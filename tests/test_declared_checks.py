"""Every check a project declares runs in the gate (Codex, Run 15).

The operator's check was pytest; the project also declared a package.json
test script, and its Node UI tests never ran in-run.
"""

import json
import sys

from quadratus.project_run import _with_declared_checks
from quadratus.repo_scan import scan_repo


def _project(tmp_path, *, package=None, python=True):
    if package is not None:
        (tmp_path / "package.json").write_text(json.dumps(package))
    if python:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    return scan_repo(tmp_path)


def test_both_a_node_script_and_a_python_suite_are_declared(tmp_path):
    scan = _project(tmp_path, package={"scripts": {"test": "node --test"}})
    assert scan.declared_checks[0] == ["npm", "test", "--silent"]
    assert scan.declared_checks[1][1:] == ["-m", "pytest", "-q"]
    assert scan.check_command == ["npm", "test", "--silent"], "the single-command choice is unchanged"


def test_an_operator_pytest_check_gains_only_the_node_script(tmp_path):
    scan = _project(tmp_path, package={"scripts": {"test": "node --test"}})
    gates = _with_declared_checks(["python", "-m", "pytest", "-q"], scan)
    assert [(g.id, g.argv, g.required) for g in gates] == [
        ("check", ("python", "-m", "pytest", "-q"), True),
        ("declared-npm", ("npm", "test", "--silent"), True)]


def test_nothing_extra_means_the_single_gate_stands(tmp_path):
    assert _with_declared_checks([sys.executable, "-m", "pytest", "-q"], _project(tmp_path)) is None
    node_only = tmp_path / "node-only"
    node_only.mkdir()
    scan = _project(node_only, package={"scripts": {"test": "node --test"}}, python=False)
    assert _with_declared_checks(["npm", "test", "--silent"], scan) is None
    assert _with_declared_checks(None, scan) is None


def test_a_package_without_a_test_script_declares_nothing(tmp_path):
    scan = _project(tmp_path, package={"scripts": {"build": "tsc"}}, python=False)
    assert scan.declared_checks == [] and scan.check_command is None
    malformed = tmp_path / "m"
    malformed.mkdir()
    (malformed / "package.json").write_text("[1, 2]")
    assert scan_repo(malformed).declared_checks == []
