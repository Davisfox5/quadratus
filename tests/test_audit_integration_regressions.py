"""Regression coverage for repository discovery and gate counting."""

import json
import os
import sys

import pytest

from quadratus.integration import GateCommand, GateSuite, _test_count
from quadratus.project_run import _gate_plan
from quadratus.repo_scan import scan_repo


def test_polyglot_projects_keep_every_declared_test_runner(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "node --test"}}))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text("def test_app(): pass\n")
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "app"\nversion = "0.1.0"\n')

    scan = scan_repo(tmp_path)
    assert [command[0] for command in scan.declared_checks] == ["npm", sys.executable, "go", "cargo"]
    gates = _gate_plan(scan.check_command, (), scan)
    assert [gate.argv[0] for gate in gates] == ["npm", sys.executable, "go", "cargo"]


def test_go_check_with_an_extra_gate_does_not_require_an_unavailable_test_count(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    scan = scan_repo(tmp_path)
    extra = GateCommand("lint", ("lint",))
    gates = _gate_plan(["lint"], (extra,), scan)
    go = next(gate for gate in gates if gate.argv[0] == "go")
    # Plain `go test` reports package results, without a count of test cases.
    assert go.minimum_tests is None


@pytest.mark.parametrize("last", [
    "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out",
    "test result: ok. 0 passed; 0 failed; 2 ignored; 0 measured; 0 filtered out",
])
def test_rust_unit_tests_count_when_a_later_doc_suite_runs_no_cases(last):
    output = (
        "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        "   Doc-tests app\n" + last + "\n"
    )
    assert _test_count(output) == 2


def test_rust_suite_gate_passes_after_successful_tests_and_empty_doc_tests(tmp_path):
    output = (
        "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )
    command = GateCommand("rust", (sys.executable, "-c", f"print({output!r})"), minimum_tests=1)
    result = GateSuite([command], cwd=tmp_path).run()
    assert result.passed
    assert result.receipts[0].tests == 2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_repo_scan_never_opens_a_named_pipe_as_source(tmp_path, monkeypatch):
    from quadratus import repo_scan

    os.mkfifo(tmp_path / "feed.py")
    (tmp_path / "app.py").write_text("value = 1\n")
    original = repo_scan._count_lines

    def count_regular(path):
        assert path.is_file(), "scanner tried to open a special filesystem entry"
        return original(path)

    monkeypatch.setattr(repo_scan, "_count_lines", count_regular)
    scan = scan_repo(tmp_path)
    assert scan.file_count == 1
    assert scan.principal_files == [("app.py", 1)]
