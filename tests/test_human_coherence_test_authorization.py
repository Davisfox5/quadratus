"""Exact operator authority for intentional changes to existing test contracts."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from quadratus import checkpoint, cli, project_run
from quadratus import integration as I
from quadratus.config import Settings

GOAL = "Change the default limit from 5 to 10"
PATH = "tests/test_defaults.py"
OLD = b"def test_five():\n    assert 5 == 5\n\ndef test_other():\n    assert 3 == 3\n"
NEW = b"def test_ten():\n    assert 10 == 10\n\ndef test_other():\n    assert 3 == 3\n"


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _envelope(root, *, approved=False):
    target = root / PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(OLD)
    change = dict(path=PATH, original_sha256=_digest(OLD), reason="The requested default is now 10",
                  cases=[dict(case="test_five", action="replace",
                              replacements=[dict(path=PATH, case="test_ten")])])
    if approved:
        change["current_sha256"] = _digest(NEW)
    return dict(schema_version=1, goal=GOAL, changes=[change])


def _receipt(*cases, status="passed", required=True, skipped=(), failed=()):
    return [dict(status=status, required=required,
                 cases={"executed": {f"{PATH}::{case}": 1 for case in cases},
                        "skipped": {f"{PATH}::{case}": 1 for case in skipped},
                        "failed": {f"{PATH}::{case}": 1 for case in failed}})]


@pytest.mark.parametrize("approved", [False, True])
def test_migration_binds_goal_original_cases_and_optional_result_bytes(tmp_path, approved):
    raw = _envelope(tmp_path, approved=approved)
    (record,) = I.test_change_authorizations(raw, goal=GOAL, root=tmp_path)
    assert record["goal"] == GOAL and record["original_sha256"] == _digest(OLD)
    assert record["current_sha256"] == (_digest(NEW) if approved else None)
    assert record["cases"][0]["replacements"] == [dict(path=PATH, case="test_ten")]
    assert I.test_change_problems(record, OLD, {PATH: NEW}, _receipt("test_ten", "test_other")) == []


@pytest.mark.parametrize("field,value,match", [
    ("goal", "Fix something else", "goal"),
    ("original_sha256", "0" * 64, "original_sha256"),
    ("current_sha256", "not-a-digest", "current_sha256"),
    ("reason", "", "reason"),
    ("case", "test_missing", "original case"),
    ("path", "tests/*.py", "exact"),
    ("path", "../test_defaults.py", "exact"),
])
def test_invalid_test_authority_is_refused(tmp_path, field, value, match):
    raw = _envelope(tmp_path)
    if field == "goal":
        raw["goal"] = value
    elif field == "case":
        raw["changes"][0]["cases"][0]["case"] = value
    else:
        raw["changes"][0][field] = value
    with pytest.raises(ValueError, match=match):
        I.test_change_authorizations(raw, goal=GOAL, root=tmp_path)


def test_test_authority_cannot_override_forbidden_paths_or_symlink_parents(tmp_path):
    raw = _envelope(tmp_path)
    with pytest.raises(ValueError, match="forbade"):
        I.test_change_authorizations(raw, goal=GOAL, root=tmp_path, forbid=["tests/**"])
    original = tmp_path / "tests"
    original.rename(tmp_path / "real-tests")
    original.symlink_to(tmp_path / "real-tests", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        I.test_change_authorizations(raw, goal=GOAL, root=tmp_path)


def test_post_edit_request_is_bound_to_the_harness_original_snapshot(tmp_path):
    raw = _envelope(tmp_path, approved=True)
    (tmp_path / PATH).write_bytes(NEW)
    with pytest.raises(ValueError, match="original_sha256"):
        I.test_change_authorizations(raw, goal=GOAL, root=tmp_path)
    (record,) = I.test_change_authorizations(raw, goal=GOAL, root=tmp_path, originals={PATH: OLD})
    assert record["original_sha256"] == _digest(OLD)


@pytest.mark.parametrize("receipt", [
    _receipt("test_other"),
    _receipt("test_ten", "test_other", status="failed"),
    _receipt("test_ten", "test_other", required=False),
    _receipt("test_ten", "test_other", skipped=["test_ten"]),
    _receipt("test_ten", "test_other", failed=["test_ten"]),
])
def test_replacement_needs_a_passed_required_unskipped_execution(tmp_path, receipt):
    (record,) = I.test_change_authorizations(_envelope(tmp_path), goal=GOAL, root=tmp_path)
    problems = I.test_change_problems(record, OLD, {PATH: NEW}, receipt)
    assert any("test_ten" in problem for problem in problems)


def _quiet_report():
    return dict(state="parsed", exitstatus=0, collected=2, collect_errors=0,
                counts=dict(passed=2, failed=0, errors=0, skipped=0), truncated=False,
                passed_cases=[f"{PATH}::test_ten", f"{PATH}::test_other"], passed_truncated=False)


def test_quiet_checks_establish_exact_case_execution_through_the_validated_report(tmp_path):
    (record,) = I.test_change_authorizations(_envelope(tmp_path), goal=GOAL, root=tmp_path)
    receipt = dict(status="passed", required=True, cases={}, report=_quiet_report())
    assert I.test_change_problems(record, OLD, {PATH: NEW}, [receipt]) == []


@pytest.mark.parametrize("field,value", [
    ("state", "foreign"), ("state", "unparsable"), ("exitstatus", 1),
    ("passed_truncated", True), ("truncated", True), ("collect_errors", 1),
    ("counts", dict(passed=2, failed=1, errors=0, skipped=0)),
    ("counts", dict(passed=2, failed=0, errors=1, skipped=0)),
    ("counts", dict(passed=2, failed=0, errors=0, skipped=1)),
    ("passed_cases", [f"{PATH}::test_ten"]),
])
def test_quiet_report_cannot_supply_stale_failed_or_incomplete_case_evidence(tmp_path, field, value):
    (record,) = I.test_change_authorizations(_envelope(tmp_path), goal=GOAL, root=tmp_path)
    report = {**_quiet_report(), field: value}
    assert I.test_change_problems(record, OLD, {PATH: NEW}, [
        dict(status="passed", required=True, cases={}, report=report)])


def test_approval_cannot_hide_changed_unlisted_cases_or_later_byte_changes(tmp_path):
    (record,) = I.test_change_authorizations(_envelope(tmp_path), goal=GOAL, root=tmp_path)
    changed = NEW.replace(b"assert 3 == 3", b"assert True")
    assert any("unapproved original case" in p for p in
               I.test_change_problems(record, OLD, {PATH: changed}, _receipt("test_ten", "test_other")))
    record["current_sha256"] = _digest(NEW)
    assert "resulting test bytes" in I.test_change_problems(
        record, OLD, {PATH: NEW + b"\n"}, _receipt("test_ten", "test_other"))[0]


def test_retirement_is_explicit_and_whole_file_deletion_is_refused(tmp_path):
    raw = _envelope(tmp_path)
    raw["changes"][0]["cases"] = [dict(case="test_five", action="retire", replacements=[])]
    (record,) = I.test_change_authorizations(raw, goal=GOAL, root=tmp_path)
    remaining = b"def test_other():\n    assert 3 == 3\n"
    assert I.test_change_problems(record, OLD, {PATH: remaining}, _receipt("test_other")) == []
    assert "whole test-file deletion" in I.test_change_problems(record, OLD, {}, _receipt("test_other"))[0]


def test_replacement_in_a_new_test_file_needs_that_files_case_identity(tmp_path):
    raw = _envelope(tmp_path)
    replacement = "tests/test_new_default.py"
    raw["changes"][0]["cases"][0]["replacements"] = [dict(path=replacement, case="test_ten")]
    (record,) = I.test_change_authorizations(raw, goal=GOAL, root=tmp_path)
    current = {PATH: b"def test_other():\n    assert 3 == 3\n", replacement: b"def test_ten():\n    assert 10 == 10\n"}
    receipts = _receipt("test_other")
    receipts[0]["cases"]["executed"][f"{replacement}::test_ten"] = 1
    assert I.test_change_problems(record, OLD, current, receipts) == []
    receipts[0]["cases"]["executed"] = {"wrong/test.py::test_ten": 1, f"{PATH}::test_other": 1}
    assert any("test_ten" in p for p in I.test_change_problems(record, OLD, current, receipts))


def test_run_project_loads_and_passes_the_validated_operator_file_before_execution(tmp_path, monkeypatch):
    raw = _envelope(tmp_path)
    path = tmp_path / "test-changes.json"
    path.write_text(json.dumps(raw))
    seen = {}

    def run(*args, **kwargs):
        seen.update(kwargs)
        return "not launched"

    monkeypatch.setattr(project_run, "_run", run)
    assert project_run.run_project(GOAL, tmp_path, Settings(), test_changes=path) == "not launched"
    assert seen["test_changes"][0]["path"] == PATH
    assert seen["test_changes"][0]["current_sha256"] is None


def test_invalid_goal_is_refused_before_the_project_lock_or_runner(tmp_path, monkeypatch):
    raw = _envelope(tmp_path)
    raw["goal"] = "Another goal"
    monkeypatch.setattr(project_run, "_project_lock", lambda *a: pytest.fail("must refuse before locking"))
    with pytest.raises(ValueError, match="goal"):
        project_run.run_project(GOAL, tmp_path, Settings(), test_changes=raw)


def test_cli_test_changes_requires_a_project_before_provider_setup(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_build_settings", lambda *a: pytest.fail("providers must not be configured"))
    with pytest.raises(SystemExit) as caught:
        cli.main([GOAL, "--test-changes", "approvals.json"])
    assert caught.value.code == 2 and "--test-changes" in capsys.readouterr().err


def test_cli_forwards_the_operator_test_changes_file(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_build_settings", lambda *a: Settings())

    def run(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(completed=True, report="checked", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    assert cli.main([GOAL, "--project", str(tmp_path), "--test-changes", "approvals.json"]) == 0
    assert seen["test_changes"] == "approvals.json" and callable(seen["ask_operator"])


def test_resume_validates_preapproved_test_changes_against_verified_original_baseline(tmp_path, monkeypatch):
    raw = _envelope(tmp_path, approved=True)
    (tmp_path / PATH).write_bytes(NEW)
    parent = tmp_path / ".quadratus/runs/previous"
    parent.mkdir(parents=True)
    checkpoint.write(parent, dict(version=checkpoint.VERSION,
                                  state=checkpoint._encode({"_original_tests": {PATH: OLD}})))
    seen = {}

    def run(*args, **kwargs):
        seen.update(kwargs)
        return "preflight only; full resume validation still required"

    monkeypatch.setattr(project_run, "_run", run)
    result = project_run.run_project(GOAL, tmp_path, Settings(), test_changes=raw, resume_from="previous")

    assert result.startswith("preflight only")
    assert seen["test_changes"][0]["original_sha256"] == _digest(OLD)
    assert seen["test_changes"][0]["current_sha256"] == _digest(NEW)
    assert seen["resume_from"] == "previous"


@pytest.mark.parametrize("bad_baseline", [None, {PATH: "unencoded text is not original bytes"}])
def test_resume_refuses_missing_or_malformed_original_baseline_before_execution(tmp_path, monkeypatch, bad_baseline):
    raw = _envelope(tmp_path)
    parent = tmp_path / ".quadratus/runs/previous"
    parent.mkdir(parents=True)
    checkpoint.write(parent, dict(version=checkpoint.VERSION,
                                  state=checkpoint._encode({"_original_tests": bad_baseline})))
    monkeypatch.setattr(project_run, "_run", lambda *a, **k: pytest.fail("no resume effect is allowed"))
    with pytest.raises(checkpoint.CheckpointRefused, match="original-test baseline"):
        project_run.run_project(GOAL, tmp_path, Settings(), test_changes=raw, resume_from="previous")


def test_resume_test_authority_cannot_load_a_checkpoint_outside_this_projects_runs(tmp_path, monkeypatch):
    raw = _envelope(tmp_path)
    outside = tmp_path / "another-project-run"
    outside.mkdir()
    monkeypatch.setattr(checkpoint, "load", lambda *a: pytest.fail("must reject the path before reading it"))
    with pytest.raises(checkpoint.CheckpointRefused, match="not a run of this project"):
        project_run.run_project(GOAL, tmp_path, Settings(), test_changes=raw, resume_from=outside)
