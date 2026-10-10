"""Offline canaries for the independent truth check (tools/truth_check.py).

Adopted 2026-10-10 from Codex's review of the efficiency plan: before a live
canary, the acceptance logic is exercised offline with scripted model replies
through the real run (tests/lifecycle/harness.py), so no provider is called.
Each bad case must come out unverified or not met, never verified; the good
case must come out verified, so the check is not passing by refusing
everything. Also pins DT-C1: a direct task whose cheap gate fails closes
mechanically, with no model close-out call.
"""

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

from quadratus.integration import GateCommand
from tests.lifecycle import harness as H
from tests.lifecycle.test_direct_tier import _decl, _lead, _roles, _run

pytestmark = pytest.mark.requirements_ledger

_SPEC = importlib.util.spec_from_file_location(
    "truth_check", Path(__file__).resolve().parents[2] / "tools" / "truth_check.py")
truth = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(truth)


def _judge(replay, project=True):
    return truth.judge(replay.result.run_dir, replay.project if project else None)


def test_good_run_is_verified(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl())
    assert replay.result.completed, replay.result.error
    result = _judge(replay)
    assert result["verdict"] == truth.VERIFIED, result


def test_failing_tests_are_not_met(tmp_path, monkeypatch):
    def lead(call, replay):
        _lead(call, replay)
        H.write(call, {"app.py": "def add(a, b):\n    return 0\n"})
        return 'Added the favicon.\nCHANGED: ["static/favicon.svg", "templates/index.html", "app.py"]'

    def gate_fix(call, replay):
        return "Nothing to fix.\nCHANGED: []"
    replay = _run(tmp_path, monkeypatch, _decl(), lead=lead, roles={"gate-fix": gate_fix})
    assert _judge(replay)["verdict"] == truth.NOT_MET


def test_a_project_with_no_tests_is_unverified(tmp_path, monkeypatch):
    # The engine also runs a test suite the project declares, so the canary
    # project has none: the only check is one that runs no test case.
    from tests.lifecycle.test_direct_tier import _files
    files = {k: v for k, v in _files().items() if not k.startswith("tests/")}
    monkeypatch.setattr("tests.lifecycle.test_direct_tier._files", lambda: files)
    replay = _run(tmp_path, monkeypatch, _decl(), check=shlex.join([sys.executable, "-c", "pass"]))
    result = _judge(replay)
    assert result["verdict"] != truth.VERIFIED, result
    assert any("no test case" in r for r in result["unverified"]), result


def test_an_interrupted_run_is_not_met(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"static/favicon.svg": "<svg/>\n"})
        raise KeyboardInterrupt("operator stop")
    replay = _run(tmp_path, monkeypatch, _decl(), lead=lead)
    assert _judge(replay)["verdict"] == truth.NOT_MET


def test_missing_screenshots_are_never_verified(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"static/favicon.svg": "<svg/>\n",
                       "templates/index.html": '<html><head><link rel="icon" href="/favicon.ico"></head></html>\n'})
        return 'Added the favicon.\nCHANGED: ["static/favicon.svg", "templates/index.html"]'
    replay = _run(tmp_path, monkeypatch, _decl(), lead=lead)
    assert _judge(replay)["verdict"] != truth.VERIFIED


def test_screenshots_removed_after_the_run_are_caught_with_the_project(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl())
    for shot in (replay.project / ".quadratus" / "design-evidence").rglob("page.png"):
        shot.unlink()
    result = _judge(replay)
    assert result["verdict"] == truth.UNVERIFIED and any("page.png is missing" in r for r in result["unverified"])


def test_direct_task_with_a_failed_cheap_gate_has_no_model_closeout(tmp_path, monkeypatch):
    from quadratus import project_run
    from tests.lifecycle.test_direct_tier import _files
    files = {**_files(), "tests/test_cheap.py": "def test_cheap():\n    assert 1 == 2\n"}
    monkeypatch.setattr("tests.lifecycle.test_direct_tier._files", lambda: files)
    plan = project_run._gate_plan
    lint = GateCommand(id="lint", argv=(sys.executable, "-m", "pytest", "-q", "--quadratus-report={report}",
                                        "tests/test_cheap.py"), cheap=True)
    monkeypatch.setattr(project_run, "_gate_plan", lambda command, extras, scan: (
        (plan(command, extras, scan) or [GateCommand(id="check", argv=tuple(command))]) + [lint]))
    replay = _run(tmp_path, monkeypatch, _decl())
    assert any(c["command"] == "gate suite" and not c["passed"] for c in
               json.loads((replay.result.run_dir / "result.json").read_text())["checks"])
    assert _roles(replay).get("closeout", 0) == 0
    assert replay.artifacts("direct-closeout")
    assert _judge(replay)["verdict"] != truth.VERIFIED


# Record-level cases: what a run could write and the check must not accept.

def _record(tmp_path, **changes):
    source = "a" * 64
    record = dict(completed=True, error="", checks=[dict(passed=True, command="pytest")],
                  source_fingerprint=source, in_flight={}, explicit_tasks=None,
                  requirements=dict(listed={"R1": "x"}, status={"R1": "met (audited)"},
                                    audits=[dict(verdicts={"R1": dict(met=True, why="tests/test_a.py")})]),
                  workflow=dict(tasks=[dict(checks=[dict(passed=True, source=source, receipts=[
                      dict(id="check", required=True, status="passed", tests=3)])])]))
    for key, value in changes.items():
        record[key] = value
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps(record))
    (run / "changes.diff").write_text(changes.pop("_diff", "+++ b/app.py\n"))
    return run


def test_record_baseline_is_verified(tmp_path):
    assert truth.judge(_record(tmp_path))["verdict"] == truth.VERIFIED


def test_check_on_a_different_source_is_unverified(tmp_path):
    run = _record(tmp_path, source_fingerprint="b" * 64)
    assert truth.judge(run)["verdict"] == truth.UNVERIFIED


def test_skipped_required_check_is_unverified(tmp_path):
    workflow = dict(tasks=[dict(checks=[dict(passed=True, source="a" * 64, receipts=[
        dict(id="check", required=True, status="passed", tests=3),
        dict(id="declared-npm", required=True, status="skipped", reason="no runner")])])])
    assert truth.judge(_record(tmp_path, workflow=workflow))["verdict"] == truth.UNVERIFIED


def test_ui_change_without_capture_is_unverified(tmp_path):
    run = _record(tmp_path)
    (run / "changes.diff").write_text("+++ b/templates/index.html\n")
    result = truth.judge(run)
    assert result["verdict"] == truth.UNVERIFIED and "UI files" in " ".join(result["unverified"])


def test_explicit_task_run_is_never_verified(tmp_path):
    assert truth.judge(_record(tmp_path, explicit_tasks={"listed": 1}))["verdict"] == truth.UNVERIFIED


def test_unaudited_requirement_is_unverified(tmp_path):
    requirements = dict(listed={"R1": "x"}, status={"R1": "met (audited)"}, audits=[])
    assert truth.judge(_record(tmp_path, requirements=requirements))["verdict"] == truth.UNVERIFIED


def test_standing_not_run_is_unverified(tmp_path):
    entry = dict(task="t1", item="tests/test_flow.py", after_check=0)
    assert truth.judge(_record(tmp_path, unexecuted_acceptance=[entry]))["verdict"] == truth.UNVERIFIED


def test_missing_record_is_unverified(tmp_path):
    assert truth.judge(tmp_path)["verdict"] == truth.UNVERIFIED


# Codex review of 47bfbfd: two records the first checker called verified.

def _with_not_run(tmp_path, check, **entry):
    workflow = dict(tasks=[dict(task_id="t1", checks=[dict(passed=True, source="a" * 64, receipts=[
        dict(id="check", required=True, status="passed", tests=3)])])])
    standing = dict(task="t1", item="tests/test_flow.py", after_check=0, **entry)
    return truth.judge(_record(tmp_path, checks=[check], workflow=workflow, unexecuted_acceptance=[standing]))


def test_a_command_naming_the_file_does_not_clear_not_run(tmp_path):
    check = dict(passed=True, command="python unknown_wrapper.py tests/test_flow.py",
                 receipts=[dict(id="check", status="passed", tests=3)])
    assert _with_not_run(tmp_path, check)["verdict"] == truth.UNVERIFIED


def test_a_discharge_mark_without_execution_does_not_clear_not_run(tmp_path):
    check = dict(passed=True, command="pytest", cases=dict(executed={"tests/test_other.py::test_a": 1}))
    assert _with_not_run(tmp_path, check, discharged_at=0)["verdict"] == truth.UNVERIFIED


def test_an_executed_case_in_the_reported_file_clears_not_run(tmp_path):
    check = dict(passed=True, command="pytest", cases=dict(executed={"tests/test_flow.py::test_a": 1}))
    assert _with_not_run(tmp_path, check)["verdict"] == truth.VERIFIED


def _ui_record(tmp_path, shots, *, bound=True):
    project = tmp_path / "project"
    folder = project / ".quadratus" / "design-evidence" / "t1"
    files = {}
    for view, data in shots.items():
        (folder / view).mkdir(parents=True)
        (folder / view / "page.png").write_bytes(data)
        files[f".quadratus/design-evidence/t1/{view}/page.png"] = __import__("hashlib").sha256(data).hexdigest()
    task = dict(task_id="t1", checks=[dict(passed=True, source="a" * 64, receipts=[
        dict(id="check", required=True, status="passed", tests=3)])])
    if bound:
        task["delivery"] = dict(reviewer="claude:opus", files=files)
    run = _record(tmp_path, workflow=dict(tasks=[task]), design_checks=[dict(
        task="t1", verified=True, final_review=dict(approved=True, verdict="APPROVED"))])
    (run / "changes.diff").write_text("+++ b/templates/index.html\n")
    return run, project


PNG = b"\x89PNG\r\n\x1a\n"


def test_screenshots_replaced_after_approval_are_unverified(tmp_path):
    run, project = _ui_record(tmp_path, {"desktop": PNG + b"desktop", "mobile": PNG + b"mobile"})
    for view in ("desktop", "mobile"):
        (project / ".quadratus/design-evidence/t1" / view / "page.png").write_bytes(PNG + b"not a screenshot")
    result = truth.judge(run, project)
    assert result["verdict"] == truth.UNVERIFIED and "approved bytes" in " ".join(result["unverified"])


def test_approved_renders_with_no_bound_digest_are_unverified(tmp_path):
    run, project = _ui_record(tmp_path, {"desktop": PNG + b"x", "mobile": PNG + b"y"}, bound=False)
    assert truth.judge(run, project)["verdict"] == truth.UNVERIFIED
    assert truth.judge(run)["verdict"] == truth.UNVERIFIED


def test_renders_matching_their_approved_digests_are_verified(tmp_path):
    run, project = _ui_record(tmp_path, {"desktop": PNG + b"x", "mobile": PNG + b"y"})
    assert truth.judge(run, project)["verdict"] == truth.VERIFIED


def test_a_real_run_whose_screenshot_is_swapped_for_another_png_is_unverified(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl())
    shot = replay.project / ".quadratus" / "design-evidence" / "t1" / "desktop" / "page.png"
    shot.write_bytes(shot.read_bytes()[:8] + b"not a screenshot")
    result = _judge(replay)
    assert result["verdict"] == truth.UNVERIFIED and "approved bytes" in " ".join(result["unverified"])
