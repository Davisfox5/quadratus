"""The canary series tool reports what the run records say and nothing more.

Run trees here start as copies of the real Q9 records under
docs/harness-canary/evidence/*/run, then get edited per case, so the shapes are
the ones run_project actually writes. No model, CLI or network is touched: the
``run`` subcommand is driven with a fake launcher that writes a minimal tree.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "harness-canary" / "evidence"
RECORDS = ("budget.json", "result.json", "invocations.jsonl", "usage.jsonl", "changes.diff")

_spec = importlib.util.spec_from_file_location(
    "canary_series", ROOT / "tools" / "acceptance" / "series.py")
series = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(series)


def make_run(base: Path, name: str, version: str, *, source="candidate", attempt=1,
             grader="3 failed, 6 passed, 1 warning in 0.11s", plan=True) -> Path:
    run = base / name / ".quadratus" / "runs" / f"2026-{name}"
    run.mkdir(parents=True)
    for record in RECORDS:
        shutil.copy(EVIDENCE / source / "run" / record, run / record)
    if plan and source == "candidate":
        shutil.copy(EVIDENCE / "candidate" / "run" / "policy-plan.json", run / "policy-plan.json")
    (run / "changes.diff").write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n--- old comment\n+new\n", encoding="utf-8")
    (run / "series.json").write_text(json.dumps(
        {"version": version, "runtime_commit": "abc123", "attempt": attempt}), encoding="utf-8")
    if grader is not None:
        (run / "grader.txt").write_text(f"....\n===== {grader} =====\n", encoding="utf-8")
    return run


def rewrite_rows(run: Path, edit) -> None:
    path = run / "invocations.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        edit(row)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_real_q9_record_reads_as_recorded(tmp_path):
    run = make_run(tmp_path, "b1", "baseline", source="baseline")
    got = series.summarise_run(run)
    assert got["completed"] is False
    assert got["provider_attempts"] == 5
    assert got["reported_tokens"] == 261259
    assert got["unknown_usage_attempts"] == 0
    assert got["grader"]["passed"] == 6 and got["grader"]["failed"] == 3
    assert got["roles"] == ["orchestrator", "lead", "gate-fix", "verifier", "closeout"]
    assert got["only_declared_paths"] == "no policy plan"
    assert got["policy_plan"] is False
    assert got["missing"] == []


def test_cached_and_fresh_split_never_reports_total_as_fresh(tmp_path):
    run = make_run(tmp_path, "c1", "candidate")
    split = series.summarise_run(run)["input_tokens"]
    # The Q9 rows carry cached input in diagnostics only; input already includes it.
    assert split["input"] == 222716
    assert split["cached"] == 53682 + 40192 + 24320 + 60500 + 10112
    assert split["fresh"] == split["input"] - split["cached"]

    def top_level(row):
        row["cached_input_tokens"] = 100 if row.get("invoked") else None
        row["diagnostics"].pop("cached_input_tokens", None)
    rewrite_rows(run, top_level)
    split = series.summarise_run(run)["input_tokens"]
    assert split["cached"] == 500 and split["fresh"] == 222716 - 500


def test_one_row_without_cached_figure_makes_fresh_unknown(tmp_path):
    run = make_run(tmp_path, "c1", "candidate")

    def drop_lead(row):
        if row["role"] == "lead":
            row["cached_input_tokens"] = None
            row["diagnostics"].pop("cached_input_tokens", None)
    rewrite_rows(run, drop_lead)
    split = series.summarise_run(run)["input_tokens"]
    assert split["input"] == 222716
    assert split["fresh"] is None and split["cached"] is None
    assert split["rows_cached_unknown"] == 1
    text = series.render(series.aggregate([run]))
    assert "fresh unknown" in text
    assert "fresh 222716" not in text


def test_declared_path_check_passes_and_fails(tmp_path):
    ok = make_run(tmp_path, "c1", "candidate")
    assert series.summarise_run(ok)["only_declared_paths"] == {"ok": True, "undeclared": []}
    bad = make_run(tmp_path, "c2", "candidate")
    (bad / "changes.diff").write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- /dev/null\n+++ b/auth.py\n@@ -0,0 +1 @@\n+z\n", encoding="utf-8")
    got = series.summarise_run(bad)["only_declared_paths"]
    assert got == {"ok": False, "undeclared": ["auth.py"]}
    assert "only declared paths changed: no: auth.py" in series.render(series.aggregate([bad]))


def test_stderr_tail_and_tool_failures_surface(tmp_path):
    run = make_run(tmp_path, "c1", "candidate")

    def blocked(row):
        if row["role"] == "lead" and row.get("invoked"):
            row["stderr_tail"] = "bwrap: No permissions to create new namespace" + "x" * 400
            row["tool_failures"] = [{"command": "pytest -q", "exit_code": 1, "output_tail": "E"}]
    rewrite_rows(run, blocked)
    notes = series.summarise_run(run)["cli_evidence"]
    assert len(notes) == 1
    assert notes[0]["role"] == "lead" and notes[0]["model"] == "openai:gpt-5.6-sol"
    assert notes[0]["stderr_tail"].startswith("bwrap: No permissions")
    assert len(notes[0]["stderr_tail"]) == 200
    assert notes[0]["tool_failures"] == [{"command": "pytest -q", "exit_code": 1}]
    text = series.render(series.aggregate([run]))
    assert "tool failure: `pytest -q` exit 1" in text


def test_label_below_and_at_five_runs_per_version(tmp_path):
    few = [make_run(tmp_path, f"b{i}", "baseline", source="baseline", attempt=i) for i in range(4)]
    few += [make_run(tmp_path, f"c{i}", "candidate", attempt=i) for i in range(5)]
    report = series.aggregate(few)
    assert report["label"] == "controller determinism, not live reliability"
    assert report["versions"]["baseline"]["label"] == report["label"]
    assert report["versions"]["candidate"]["label"] == "live reliability: 5 runs per version"
    few.append(make_run(tmp_path, "b9", "baseline", source="baseline", attempt=9,
                        grader="9 passed in 0.10s"))
    report = series.aggregate(few)
    assert report["label"] == "live reliability: 5 runs per version"
    base = report["versions"]["baseline"]
    assert base["pass_rate"] == "1 of 5"
    assert base["median_attempts"] == {"value": 5, "known": 5, "of": 5}
    assert "**live reliability: 5 runs per version**" in series.render(report)


def test_missing_files_are_reported_as_missing(tmp_path):
    run = make_run(tmp_path, "c1", "candidate", grader=None)
    for name in ("budget.json", "invocations.jsonl", "series.json"):
        (run / name).unlink()
    got = series.summarise_run(run)
    assert got["provider_attempts"] is None and got["reported_tokens"] is None
    assert got["input_tokens"] is None and got["roles"] is None
    assert got["grader"] == "missing"
    assert set(got["missing"]) == {"budget.json", "invocations.jsonl", "series.json", "grader.txt"}
    text = series.render(series.aggregate([run]))
    assert "provider attempts: missing (budget.json)" in text
    assert "input tokens: missing (invocations.jsonl)" in text
    assert "grader: missing" in text
    assert "median attempts" in text and "None (0 of 1 known)" not in text


def test_aggregate_writes_both_reports(tmp_path):
    run = make_run(tmp_path, "c1", "candidate")
    assert series.main(["aggregate", "--runs", str(run), "--out", str(tmp_path / "rep")]) == 0
    data = json.loads((tmp_path / "rep" / "series-report.json").read_text())
    assert data["runs"][0]["run_dir"] == str(run)
    assert (tmp_path / "rep" / "series-report.md").read_text().startswith("# Canary series")


# The run subcommand, against a fake launcher.

FAKE_LAUNCHER = """
import argparse, json, pathlib, sys
p = argparse.ArgumentParser()
p.add_argument("--project"); p.add_argument("--allowance-record")
a = p.parse_args()
run = pathlib.Path(a.project) / ".quadratus" / "runs" / "20260922T000000Z-fake"
run.mkdir(parents=True)
(run / "budget.json").write_text(json.dumps({"reserved_attempts": 2, "reported_tokens": 10}))
(run / "result.json").write_text(json.dumps({"completed": True}))
(run / "invocations.jsonl").write_text("")
(run / "changes.diff").write_text("")
log = pathlib.Path(a.project).parent.parent / "calls.txt"
log.open("a").write(a.project + "\\n")
"""


@pytest.fixture
def setup(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("x = 1\n")
    launcher = tmp_path / "fake_launcher.py"
    launcher.write_text(FAKE_LAUNCHER)
    allowance = tmp_path / "allowance.json"
    allowance.write_text(json.dumps({"authorized_by": "Davis", "source": "test"}))
    return tmp_path, fixture, launcher, allowance


def _run_args(tmp_path, fixture, launcher, *extra):
    return ["run", "--version", "candidate", "--runtime", str(tmp_path), "--fixture",
            str(fixture), "--out", str(tmp_path / "out"), "--launcher", str(launcher),
            "--python", sys.executable, *extra]


def test_run_refuses_without_allowance_record(setup):
    tmp_path, fixture, launcher, allowance = setup
    with pytest.raises(SystemExit, match="allowance record"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1"))
    allowance.write_text(json.dumps({"authorized_by": "someone", "source": "x"}))
    with pytest.raises(SystemExit, match="allowance record"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()


def test_run_refuses_count_above_five(setup):
    tmp_path, fixture, launcher, allowance = setup
    with pytest.raises(SystemExit, match="1 to 5"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "6",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()


def test_run_writes_sidecars_and_grader_in_fresh_copies(setup):
    tmp_path, fixture, launcher, allowance = setup
    grader = f'{sys.executable} -c "import os; print(os.environ[\'CANARY_PROJECT\']); print(\'2 passed in 0.01s\')"'
    assert series.main(_run_args(tmp_path, fixture, launcher, "--count", "2",
                                 "--allowance-record", str(allowance),
                                 "--grader-command", grader)) == 0
    calls = (tmp_path / "out" / "calls.txt").read_text().split()
    assert len(calls) == 2 and calls[0] != calls[1]
    runs = [Path(p) for p in (tmp_path / "out" / "candidate-runs.txt").read_text().split()]
    assert len(runs) == 2
    for attempt, run in enumerate(runs, start=1):
        side = json.loads((run / "series.json").read_text())
        assert side["version"] == "candidate" and side["attempt"] == attempt
        assert side["launcher_exit_code"] == 0 and side["runtime_commit"]
        assert "2 passed" in (run / "grader.txt").read_text()
        assert str(run.parents[2]) in (run / "grader.txt").read_text()
    report = series.aggregate(runs)
    assert report["versions"]["candidate"]["pass_rate"] == "2 of 2"
    assert report["versions"]["candidate"]["completed"] == 2
