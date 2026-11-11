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

from quadratus import baseline as B
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


@pytest.mark.parametrize("name", [
    "build/lib/app/shim.py", "dist/app.js", "build/site.pth", "dist/data.json",
    # Review of the fix set: the first rule went by file type, and each of
    # these read verified. A check reads any of them as readily as a .py
    # file, and the last one is the engine's own mark that names were cut.
    "build/helper.txt", "build/helper", "dist/vendor.zip", "dist/addon.node", "dist/index.html",
    "test-results/login.test.js.bak", "coverage.xml", "and 12 more",
])
def test_a_file_an_editing_call_changed_outside_the_measured_diff_is_unverified(tmp_path, name):
    """run_and_surfaces 1: the check ran with the file present, and no diff
    or source fingerprint covers it, so the pass does not rest on the
    measured source alone. Whatever the file is called."""
    result = truth_check.judge(_truth_run(tmp_path, unmeasured_changes={"t1": [name]}))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("folders the measured diff does not read" in reason and name in reason
               for reason in result["unverified"])


@pytest.mark.parametrize("unmeasured", [{}, {"t1": []}, {"t1": [], "t2": []}, None])
def test_a_run_whose_editing_calls_left_nothing_outside_the_diff_stays_verified(tmp_path, unmeasured):
    """The ordinary run: a lead that ran pytest or node --test leaves only
    tool caches, which the engine does not list here, so the record is
    empty and the verdict is not lowered."""
    assert truth_check.judge(_truth_run(tmp_path, unmeasured_changes=unmeasured))["verdict"] == truth_check.VERIFIED


@pytest.mark.parametrize("unmeasured", ["not a mapping", {"t1": "not a list"}, ["build/x.py"], 1,
                                        {"t1": [], "t2": "build/x.py"}])
def test_an_unreadable_list_of_unmeasured_changes_is_unverified(tmp_path, unmeasured):
    """Missing evidence is never success: a record that does not say, in the
    shape the engine writes, what was left outside the diff has not shown
    that nothing was."""
    result = truth_check.judge(_truth_run(tmp_path, unmeasured_changes=unmeasured))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("unmeasured_changes is not a list of names" in reason for reason in result["unverified"])


def test_the_engine_lists_no_tool_cache_as_an_unmeasured_change(tmp_path):
    """What the any-name rule rests on: the caches every test run rewrites
    (pytest's and the interpreter's) are not in the class the engine records,
    so a lead that only ran the tests does not lower the verdict."""
    from quadratus.project_watch import ProjectWatch

    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    watch = ProjectWatch(root)
    before = watch.listing()
    for name in (".pytest_cache/v/cache/lastfailed", "__pycache__/app.cpython-313.pyc",
                 "tests/__pycache__/test_app.cpython-313.pyc", ".ruff_cache/0.1/data", "build/lib/app.txt"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x")
    sensitive, output = watch.changes(before, watch.listing())
    assert not sensitive and output == ["build/lib/app.txt"]


@pytest.mark.requirements_ledger
def test_canary_a_pass_that_rests_on_an_unmeasured_text_file_is_never_verified(tmp_path, monkeypatch):
    """The truth canary the brief asked for, through the real run with
    scripted replies. The lead's app.py executes build/helper.txt, which no
    diff reads; reviewers and the auditor accept and the engine completes.
    The measured source alone fails its own tests, so the judge outside the
    engine must not say verified. With the file-type rule it did."""
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import FILES, T1, Script

    plan = ["REQUIREMENTS:\nR1: add(1, 2) returns 3\nKIND: architect complex\nSCOPE: " + json.dumps(T1)
            + "\nImplement add in app.py.\nCOVERS: R1"]

    def lead(call, replay):
        H.write(call, {"app.py": "import pathlib\nexec((pathlib.Path(__file__).parent / 'build' / "
                                 "'helper.txt').read_text())\n",
                       "build/helper.txt": "def add(a, b):\n    return a + b\n"})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    script = Script(orchestrator=lambda call, replay: plan.pop(0) if plan else "DONE", lead=lead,
                    **{"requirements-review": lambda call, replay: "COMPLETE",
                       "auditor": lambda call, replay: "R1: MET - app.py"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3)
    assert replay.result.completed, replay.result.error
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())
    assert record["unmeasured_changes"] == {"t1": ["build/helper.txt"]}
    result = truth_check.judge(replay.result.run_dir, replay.project)
    assert result["verdict"] == truth_check.UNVERIFIED, result
    assert any("build/helper.txt" in reason for reason in result["unverified"]), result


def test_a_run_started_over_an_accepted_watched_path_is_unverified(tmp_path):
    """run_and_surfaces 3: the acceptance is the operator's call and is on the
    record; the judge outside the engine still says no diff or check of this
    run covers what that path holds."""
    accepted = [dict(path=".git/hooks/pre-commit", kind="git", what="added")]
    result = truth_check.judge(_truth_run(tmp_path, trust=dict(watched_paths_accepted=accepted)))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("accepted by the operator" in reason and ".git/hooks/pre-commit" in reason
               for reason in result["unverified"])


@pytest.mark.parametrize("trust", [{}, dict(watched_paths_accepted=[]), None,
                                   dict(agent_config_allowed=[], declared_checks_declined=False)])
def test_a_run_that_accepted_no_watched_path_stays_verified(tmp_path, trust):
    assert truth_check.judge(_truth_run(tmp_path, trust=trust))["verdict"] == truth_check.VERIFIED


@pytest.mark.parametrize("trust", [dict(watched_paths_accepted="yes"), dict(watched_paths_accepted=True),
                                   dict(watched_paths_accepted={"path": ".git/hooks/pre-commit"}), "x", ["x"]])
def test_a_trust_section_that_cannot_be_read_is_unverified(tmp_path, trust):
    """Review of the fix set: "watched_paths_accepted": "yes" read verified.
    A record that says something was accepted, or says it in no shape the
    engine writes, has not shown that nothing was."""
    result = truth_check.judge(_truth_run(tmp_path, trust=trust))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("trust section is not readable" in reason for reason in result["unverified"])


def test_the_mark_for_paths_a_record_does_not_name_reads_as_an_accepted_path(tmp_path):
    """The preflight accepts a full record under a name that is not a path.
    The judge counts and names it like any other entry."""
    accepted = [dict(path="(watched paths the record does not name)", kind="", what="not checked")]
    result = truth_check.judge(_truth_run(tmp_path, trust=dict(watched_paths_accepted=accepted)))
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("the record does not name" in reason for reason in result["unverified"])


def test_the_refusal_says_an_acceptance_lasts(tmp_path):
    """run_and_surfaces 3: the message said 'for this run' while later runs
    read the acceptance and did not ask again. It now says what happens."""
    from quadratus import project_run
    source = Path(project_run.__file__).read_text()
    assert "accept them as they are for this run" not in source
    assert "later runs do not ask about the same paths again" in source


FAKE_CLI = r'''
import json, os, sys
from pathlib import Path
Path('add.py').write_text('def add(a, b):\n    return a + b\n')
if os.environ.get('FAKE_BUILD'):
    Path('build').mkdir(exist_ok=True)
    Path('build/helper.txt').write_text('x')
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


def _policy(root, gates, *, path_rules=(), bindings=None):
    """An explicit project policy with these gates, and its sha256 for the
    operator's approval. The default family is pure-logic (scope, unit-tests)."""
    document = {
        "kind": "policy", "schema_version": 1, "repository_id": "fixture/project",
        "library": {"version": "pilot-1"},
        "defaults": {"family": "pure-logic", "required_gates": ["scope"],
                     "scope_max_lines": 50, "overrun_ratio": 1.1, "worker_depth": 1},
        "capability_policy": {"deny_write": [], "sensitive": [],
                              "external_effects": "deny", "native_delegation": "deny"},
        "path_rules": list(path_rules), "gate_bindings": bindings or {},
        "gates": [{"id": "scope", "runner": "builtin:scope", "required": True}, *gates],
        "decisions": [],
    }
    path = root / ".quadratus" / "policy.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(document))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marking_gate(tmp_path, gate_id):
    """A command gate that leaves a marker when it runs. The script and its
    marker live outside the project, so the check changes no source, and the
    argv is a plain script path (a policy refuses an argument that reads as
    a shell expression)."""
    marker = tmp_path / f"{gate_id}-ran.txt"
    script = tmp_path / f"{gate_id}_gate.py"
    script.write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\nprint('1 passed')\n")
    gate = {"id": gate_id, "runner": "command", "required": True,
            "argv": [sys.executable, str(script)], "minimum_tests": 1}
    return gate, marker, script


CHECK = f"{sys.executable} -m pytest -q -p no:cacheprovider test_add.py"


def _baseline_run(tmp_path, monkeypatch, root, digest, *, check=CHECK, **options):
    """One baseline through the fake vendor script: ``(result, record)``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/local/bin", "/usr/bin", "/bin"]))
    result = B.run_baseline("Make add return the sum.", Project(root), Settings(backend="cli", max_retries=1),
                            model="claude:opus", check=check, trust_project_policy=digest, **options)
    return result, json.loads((result.run_dir / "result.json").read_text())


def _ran(record):
    return [receipt["id"] for entry in record["checks"] for receipt in entry.get("receipts", [])]


def _first_prompt(result, record):
    from quadratus.artifacts import ArtifactStore
    return ArtifactStore(result.run_dir / "artifacts").get(record["baseline"]["edit_calls"][0]["prompt_artifact"])


def test_a_baseline_runs_the_command_checks_of_a_policy_the_operator_approved(tmp_path, monkeypatch):
    """run_and_surfaces 4: a project run binds an approved policy's command
    gates to every task. The baseline wrote them into its policy plan and ran
    none of them, so its gate result meant less than its own record said."""
    root = _baseline_project(tmp_path, monkeypatch)
    gate, marker, script = _marking_gate(tmp_path, "unit-tests")
    result, record = _baseline_run(tmp_path, monkeypatch, root, _policy(root, [gate]))
    assert marker.exists(), "the approved policy's required command check did not run"
    assert "unit-tests" in _ran(record)
    plan = json.loads((result.run_dir / "policy-plan.json").read_text())
    listed = [gate["id"] for gate in plan["gates"] if gate.get("runner") == "command"]
    assert set(listed) <= set(_ran(record)), "the policy plan lists a command check the baseline did not run"
    # A baseline is never a completion, whatever its gate says.
    assert record["completed"] is False
    # Review of the fix: the model is told every check the harness runs. The
    # policy's is named by id, as a lead's role packet names it, and its
    # command line is never shown (it can name a file a seat must not open).
    prompt = _first_prompt(result, record)
    assert B.POLICY_CHECK.format(id="unit-tests") in prompt
    assert f"- {CHECK}" in prompt and "operator-check" not in prompt
    assert str(script) not in prompt and script.name not in prompt


def test_a_policy_check_with_no_operator_check_is_not_told_as_none_configured(tmp_path, monkeypatch):
    """Review of the fix: with a policy check and no operator check the gate
    ran and could buy a fix call, while the prompt said "none configured" and
    "you get this one call"."""
    root = _baseline_project(tmp_path, monkeypatch)
    (root / "test_add.py").unlink()     # no test command for the scan to declare
    gate, marker, _ = _marking_gate(tmp_path, "unit-tests")
    result, record = _baseline_run(tmp_path, monkeypatch, root, _policy(root, [gate]), check="")
    assert marker.exists() and _ran(record) == ["unit-tests"]
    prompt = _first_prompt(result, record)
    assert B.POLICY_CHECK.format(id="unit-tests") in prompt
    assert "none configured" not in prompt and "You get this one call" not in prompt
    assert "1 more call to fix it" in prompt


def test_the_prompt_lists_policy_checks_only_when_there_are_some():
    from quadratus.scope import TaskScope

    plain = B.baseline_prompt("Goal.", ("pytest -q",), TaskScope(), 1)
    assert "- pytest -q" in plain and "the project's policy adds" not in plain
    none = B.baseline_prompt("Goal.", (), TaskScope(), 1)
    assert "- none configured" in none and "You get this one call" in none
    both = B.baseline_prompt("Goal.", ("pytest -q",), TaskScope(), 2, policy_checks=("lint", "unit-tests"))
    assert "- pytest -q\n- lint: a check the project's policy adds." in both
    assert "- unit-tests: a check the project's policy adds." in both and "2 more calls" in both


@pytest.mark.parametrize("gate, said", [
    ({"id": "unit-tests", "runner": "command", "required": True, "argv": [sys.executable, "-V"],
      "side_effects": "external"}, "external-effect ruling"),
    ({"id": "unit-tests", "runner": "builtin:grep", "required": True, "pattern": "x"},
     "Required runner is not configured"),
])
def test_a_policy_that_cannot_be_bound_stops_the_baseline_before_any_call(tmp_path, monkeypatch, gate, said):
    """The binding is inside the baseline's try: a policy a project run would
    refuse at dispatch is a recorded stop here, with no model call made and
    no check counted."""
    root = _baseline_project(tmp_path, monkeypatch)
    result, record = _baseline_run(tmp_path, monkeypatch, root, _policy(root, [gate]))
    assert said in record["error"] and said in result.error
    info = record["baseline"]
    assert info["calls"] == 0 and info["editing_calls"] == 0 and info["stop_cause"] == "PolicyError"
    assert info["gate"]["attempts"] == 0 and info["gate"]["passed"] is None and record["checks"] == []
    assert (root / "add.py").read_text() == "def add(a, b):\n    return 0\n", "the model was called"
    assert truth_check.judge(result.run_dir)["verdict"] == truth_check.NOT_MET


def _path_rule_policy(tmp_path, root):
    """The default family binds unit-tests; a rule on add.py selects the
    test-author family, which binds lint. Both leave a marker when run."""
    unit, unit_marker, _ = _marking_gate(tmp_path, "unit-tests")
    lint, lint_marker, _ = _marking_gate(tmp_path, "lint")
    absent = {name: {"absent": True, "because": "not used in this fixture"}
              for name in ("test-both-ways", "typecheck")}
    digest = _policy(root, [unit, lint], bindings=absent,
                     path_rules=[{"paths": ["add.py"], "families": ["test-author"]}])
    return digest, unit_marker, lint_marker


def test_a_baseline_is_refused_when_the_policy_binds_checks_by_path_and_no_path_is_declared(tmp_path, monkeypatch):
    """Review of the fix: the plan was resolved with no paths, so it held the
    default family's checks only. A project task that changes add.py is held
    to lint; the baseline changed add.py, lint never ran, and its gate read
    passed. The brief's rule: where the gate cannot be a project run's,
    refuse before any call and say why."""
    root = _baseline_project(tmp_path, monkeypatch)
    digest, unit_marker, lint_marker = _path_rule_policy(tmp_path, root)
    result, record = _baseline_run(tmp_path, monkeypatch, root, digest)
    assert "binds command checks by path" in record["error"]
    assert "add.py: lint" in record["error"] and "--path" in record["error"]
    info = record["baseline"]
    assert info["calls"] == 0 and info["gate"]["attempts"] == 0 and info["gate"]["passed"] is None
    assert not unit_marker.exists() and not lint_marker.exists()
    assert (root / "add.py").read_text() == "def add(a, b):\n    return 0\n", "the model was called"


def test_declared_paths_bind_the_checks_the_policy_gives_those_paths(tmp_path, monkeypatch):
    """The way out the refusal names: with the paths declared the plan is the
    one a project task declaring them gets, and its checks run."""
    from quadratus.policy import load_policy, task_gate

    root = _baseline_project(tmp_path, monkeypatch)
    digest, _, lint_marker = _path_rule_policy(tmp_path, root)
    declared = ("add.py", "test_add.py")
    result, record = _baseline_run(tmp_path, monkeypatch, root, digest, declared_paths=declared)
    assert record["error"] == "" and lint_marker.exists()
    # The same ids a project task with this scope is bound to, by the
    # session's own function.
    policy = load_policy(root, trust_project_policy=digest)
    bound = task_gate(policy, policy.resolve(declared, writing=True), None)
    assert {c.id for c in bound.commands} <= set(_ran(record)) and "lint" in _ran(record)
    assert B.POLICY_CHECK.format(id="lint") in _first_prompt(result, record)


def test_a_path_rule_that_binds_no_further_check_does_not_refuse_a_baseline(tmp_path, monkeypatch):
    """Only a check the baseline would not run is a reason to refuse: a rule
    that adds none leaves the default plan a project run's too."""
    root = _baseline_project(tmp_path, monkeypatch)
    gate, marker, _ = _marking_gate(tmp_path, "unit-tests")
    digest = _policy(root, [gate], path_rules=[{"paths": ["add.py"], "families": ["pure-logic"]}])
    _, record = _baseline_run(tmp_path, monkeypatch, root, digest)
    assert record["error"] == "" and marker.exists() and "unit-tests" in _ran(record)


def test_a_baseline_record_names_the_files_its_call_left_outside_the_measured_diff(tmp_path, monkeypatch):
    """Review of the fix set: a project run's record names them and the
    baseline's had no such field, so a baseline gate that passed on top of a
    build file read the same as one that passed on the measured source."""
    root = _baseline_project(tmp_path, monkeypatch)
    _, clean = _baseline_run(tmp_path, monkeypatch, root, None)
    assert clean["unmeasured_changes"] == {}
    monkeypatch.setenv("FAKE_BUILD", "1")
    result, record = _baseline_run(tmp_path, monkeypatch, root, None)
    assert record["unmeasured_changes"] == {"baseline": ["build/helper.txt"]}
    assert "build/helper.txt" not in (result.run_dir / "changes.diff").read_text()
    assert any("build/helper.txt" in reason for reason in truth_check.judge(result.run_dir)["unverified"])


def test_a_baseline_report_says_which_watched_paths_were_accepted(tmp_path, monkeypatch):
    """run_and_surfaces 3, the report line: a baseline started over a hook an
    earlier run planted, accepted by the operator, says so in report.md and
    not only in result.json, and the truth check does not call it verified."""
    from quadratus.project_watch import ProjectWatch

    root = _baseline_project(tmp_path, monkeypatch)
    name = ".git/hooks/pre-commit"
    (root / name).parent.mkdir(parents=True)
    (root / name).write_text("#!/bin/sh\necho planted\n")
    listing = ProjectWatch(root).listing()
    earlier = root / ".quadratus" / "runs" / "20260101T000000Z-aaaaaaaa"
    earlier.mkdir(parents=True)
    (earlier / "result.json").write_text(json.dumps({"project_watch": {"findings": [], "paths": [dict(
        path=name, kind=listing.kinds.get(name, ""), what="added", record=list(listing.sensitive[name]),
        before=None, by="t1 lead claude:opus")]}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/local/bin", "/usr/bin", "/bin"]))
    settings = Settings(backend="cli", max_retries=1)
    with pytest.raises(ValueError, match="still in place"):
        B.run_baseline("Make add return the sum.", Project(root), settings, model="claude:opus", check=CHECK)
    result = B.run_baseline("Make add return the sum.", Project(root), settings, model="claude:opus",
                            check=CHECK, accept_watched_paths=True)
    record = json.loads((result.run_dir / "result.json").read_text())
    assert [a["path"] for a in record["trust"]["watched_paths_accepted"]] == [name]
    report = (result.run_dir / "report.md").read_text()
    assert "Watched paths accepted" in report and name in report
    verdict = truth_check.judge(result.run_dir)
    assert verdict["verdict"] != truth_check.VERIFIED
    assert any("accepted by the operator" in reason for reason in verdict["unverified"])
