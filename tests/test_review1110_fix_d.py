"""Regression tests for the run-lifecycle defects the whole-batch review found
(final review of the 2026-11-10 batch, run_and_surfaces 1, 3 and 4).

Offline: the truth check reads constructed run records, and the baseline is
driven through a fake vendor script, never a real CLI.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from quadratus.config import Settings
from quadratus.project import Project
from tools import truth_check


def _truth_run(tmp_path, **extra):
    """A run record the truth check judges verified, plus whatever is added."""
    check = dict(passed=True, source="final-source", receipts=[
        dict(id="tests", required=True, status="passed", tests=1)])
    record = dict(completed=True, checks=[check], source_fingerprint="final-source",
                  requirements=dict(listed={"R1": "Change the module"}, status={"R1": "met"},
                                    audits=[dict(verdicts={"R1": dict(met=True)})]),
                  workflow=dict(tasks=[dict(checks=[check])]), **extra)
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps(record))
    (run / "changes.diff").write_text("+++ b/app.py\n")
    return run


def test_the_fixture_run_is_verified_before_anything_is_added(tmp_path):
    assert truth_check.judge(_truth_run(tmp_path))["verdict"] == truth_check.VERIFIED


@pytest.mark.parametrize("name", ["build/lib/app/shim.py", "dist/app.js", "build/site.pth", "dist/data.json"])
def test_code_an_editing_call_changed_outside_the_measured_diff_is_unverified(tmp_path, name):
    """run_and_surfaces 1: the check ran with the file present, and no diff
    or source fingerprint covers it, so the pass does not rest on the
    measured source alone."""
    result = truth_check.judge(_truth_run(tmp_path, unmeasured_changes={"t1": [name]}))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("folders the measured diff does not read" in reason and name in reason
               for reason in result["unverified"])


@pytest.mark.parametrize("unmeasured", [
    {},
    {"t1": []},
    {"t1": ["htmlcov/index.html", "coverage.xml", "test-results/trace.zip", "dist/logo.png"]},
    "not a mapping",
    {"t1": "not a list"},
])
def test_report_files_and_an_empty_or_misshapen_record_do_not_lower_the_verdict(tmp_path, unmeasured):
    """Only a file a check can load counts. A coverage report or an image a
    test run wrote is not evidence against the check."""
    assert truth_check.judge(_truth_run(tmp_path, unmeasured_changes=unmeasured))["verdict"] == truth_check.VERIFIED


def test_a_run_started_over_an_accepted_watched_path_is_unverified(tmp_path):
    """run_and_surfaces 3: the acceptance is the operator's call and is on the
    record; the judge outside the engine still says no diff or check of this
    run covers what that path holds."""
    accepted = [dict(path=".git/hooks/pre-commit", kind="git", what="added")]
    result = truth_check.judge(_truth_run(tmp_path, trust=dict(watched_paths_accepted=accepted)))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("accepted by the operator" in reason and ".git/hooks/pre-commit" in reason
               for reason in result["unverified"])


@pytest.mark.parametrize("trust", [{}, dict(watched_paths_accepted=[]), dict(watched_paths_accepted="yes"), "x"])
def test_no_accepted_path_or_a_misshapen_trust_section_does_not_lower_the_verdict(tmp_path, trust):
    assert truth_check.judge(_truth_run(tmp_path, trust=trust))["verdict"] == truth_check.VERIFIED


def test_the_refusal_says_an_acceptance_lasts(tmp_path):
    """run_and_surfaces 3: the message said 'for this run' while later runs
    read the acceptance and did not ask again. It now says what happens."""
    from quadratus import project_run
    source = Path(project_run.__file__).read_text()
    assert "accept them as they are for this run" not in source
    assert "later runs do not ask about the same paths again" in source


FAKE_CLI = r'''
import json, sys
from pathlib import Path
Path('add.py').write_text('def add(a, b):\n    return a + b\n')
print(json.dumps({'is_error': False, 'usage': {'input_tokens': 100, 'output_tokens': 10},
                  'result': 'Saved.\nCHANGED: ["add.py"]'}))
'''


def _baseline_project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "add.py").write_text("def add(a, b):\n    return 0\n")
    (root / "test_add.py").write_text("from add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    binary = tmp_path / "fake-vendor"
    binary.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    binary.chmod(0o755)
    for vendor in ("CLAUDE", "OPENAI", "GROK"):
        monkeypatch.setenv(f"QUADRATUS_CLI_BINARY_{vendor}", str(binary))
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(scratch))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return root


def _policy(root, argv):
    document = {
        "kind": "policy", "schema_version": 1, "repository_id": "fixture/project",
        "library": {"version": "pilot-1"},
        "defaults": {"family": "pure-logic", "required_gates": ["scope"],
                     "scope_max_lines": 50, "overrun_ratio": 1.1, "worker_depth": 1},
        "capability_policy": {"deny_write": [], "sensitive": [],
                              "external_effects": "deny", "native_delegation": "deny"},
        "path_rules": [], "gate_bindings": {},
        "gates": [{"id": "scope", "runner": "builtin:scope", "required": True},
                  {"id": "unit-tests", "runner": "command", "required": True, "argv": argv, "minimum_tests": 1}],
        "decisions": [],
    }
    path = root / ".quadratus" / "policy.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(document))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_baseline_runs_the_command_checks_of_a_policy_the_operator_approved(tmp_path, monkeypatch):
    """run_and_surfaces 4: a project run binds an approved policy's command
    gates to every task. The baseline wrote them into its policy plan and ran
    none of them, so its gate result meant less than its own record said."""
    from quadratus.baseline import run_baseline

    root = _baseline_project(tmp_path, monkeypatch)
    marker = tmp_path / "policy-gate-ran.txt"
    # The check and its marker live outside the project, so it changes no
    # source, and the argv is a plain script path (a policy refuses an
    # argument that reads as a shell expression).
    script = tmp_path / "policy_gate.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\nprint('1 passed')\n")
    digest = _policy(root, [sys.executable, str(script)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/local/bin", "/usr/bin", "/bin"]))
    check = f"{sys.executable} -m pytest -q -p no:cacheprovider test_add.py"
    result = run_baseline("Make add return the sum.", Project(root), Settings(backend="cli", max_retries=1),
                          model="claude:opus", check=check, trust_project_policy=digest)
    record = json.loads((result.run_dir / "result.json").read_text())
    ran = [receipt["id"] for entry in record["checks"] for receipt in entry.get("receipts", [])]
    assert marker.exists(), "the approved policy's required command check did not run"
    assert "unit-tests" in ran
    plan = json.loads((result.run_dir / "policy-plan.json").read_text())
    listed = [gate["id"] for gate in plan["gates"] if gate.get("runner") == "command"]
    assert set(listed) <= set(ran), "the policy plan lists a command check the baseline did not run"
    # A baseline is never a completion, whatever its gate says.
    assert record["completed"] is False
