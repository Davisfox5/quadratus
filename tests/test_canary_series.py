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
    assert report["label"] == "live sample: 4 runs per version, below the 5-run reliability threshold"
    assert report["versions"]["baseline"]["label"] == report["label"]
    assert all(r["provenance"].startswith("unknown") for r in report["runs"])
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
import argparse, json, os, pathlib, sys
p = argparse.ArgumentParser()
p.add_argument("--project"); p.add_argument("--allowance-record")
a = p.parse_args()
run = pathlib.Path(a.project) / ".quadratus" / "runs" / "20260922T000000Z-fake"
run.mkdir(parents=True)
if not os.environ.get("FAKE_NO_BUDGET"):
    (run / "budget.json").write_text(json.dumps({
        "reserved_attempts": 2,
        "reported_tokens": int(os.environ.get("FAKE_TOKENS", "10")),
        "unknown_usage_attempts": 0}))
(run / "result.json").write_text(json.dumps({"completed": True}))
(run / "invocations.jsonl").write_text("")
(run / "changes.diff").write_text("")
log = pathlib.Path(a.project).parent.parent / "calls.txt"
log.open("a").write(a.project + "\\n")
seen = pathlib.Path(a.project).parent.parent / "env-seen.txt"
seen.open("a").write(" ".join(sorted(k for k in os.environ if "KEY" in k or k == "PATH")) + "\\n")
"""

TEMPLATE = ROOT / "docs" / "harness-canary" / "allowance.template.json"
BASELINE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
GRADER_SHA = "c" * 64


FAKE_GRADER = "def test_preservation_ok():\n    assert True\n"


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_record(path: Path, **overrides) -> Path:
    """A record that passes load_record, built from the shipped template."""
    record = json.loads(TEMPLATE.read_text())
    record.update(approved=True, authorized_by="Davis", source="test fixture",
                  instruction="run the canary pair", recorded_at="2026-09-22T00:00:00+00:00",
                  environment="container-contained", baseline_sha=BASELINE_SHA,
                  candidate_sha=CANDIDATE_SHA, runs_per_version=2, batch_id="batch-test-1",
                  grader_sha256=GRADER_SHA)
    grader = path.parent / "fixture-instrument" / "test_contract.py"
    if grader.exists():
        record["grader_sha256"] = _sha(grader)
    record.update(overrides)
    path.write_text(json.dumps(record, indent=2))
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("x = 1\n")
    (fixture / ".quadratus").mkdir()
    instrument = tmp_path / "fixture-instrument"
    instrument.mkdir()
    grader = instrument / "test_contract.py"
    grader.write_text(FAKE_GRADER)
    (fixture / ".quadratus" / "fixture-manifest.json").write_text(json.dumps(
        {"grader": str(grader), "instrument_sha256": {"test_contract.py": _sha(grader)}}))
    launcher = tmp_path / "fake_launcher.py"
    launcher.write_text(FAKE_LAUNCHER)
    allowance = write_record(tmp_path / "allowance.json", grader_sha256=_sha(grader))
    monkeypatch.setattr(series, "_commit", lambda runtime: CANDIDATE_SHA)
    monkeypatch.delenv("FAKE_NO_BUDGET", raising=False)
    monkeypatch.delenv("FAKE_TOKENS", raising=False)
    return tmp_path, fixture, launcher, allowance


def _run_args(tmp_path, fixture, launcher, *extra, out="out", version="candidate"):
    return ["run", "--version", version, "--runtime", str(tmp_path), "--fixture",
            str(fixture), "--out", str(tmp_path / out), "--launcher", str(launcher),
            "--python", sys.executable, *extra]


def _ledger(allowance: Path) -> dict:
    return json.loads(series.allowance.ledger_path(allowance).read_text())


def test_run_refuses_without_allowance_record(setup):
    tmp_path, fixture, launcher, allowance = setup
    with pytest.raises(SystemExit, match="allowance record"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1"))
    allowance.write_text(json.dumps({"authorized_by": "someone", "source": "x"}))
    with pytest.raises(SystemExit, match="allowance record"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()


def test_shipped_template_is_refused(setup):
    tmp_path, fixture, launcher, _ = setup
    with pytest.raises(SystemExit, match="refused at approved"):
        series.allowance.load_record(TEMPLATE)
    with pytest.raises(SystemExit, match="refused at approved"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(TEMPLATE)))
    assert not (tmp_path / "out").exists()
    assert not series.allowance.ledger_path(TEMPLATE).exists()


@pytest.mark.parametrize("field, value, where", [
    ("schema", "quadratus-canary-allowance/1", "schema"),
    ("authorized_by", "someone", "authorized_by"),
    ("instruction", " ", "instruction"),
    ("environment", "laptop", "environment"),
    ("candidate_sha", "B" * 40, "candidate_sha"),
    ("candidate_sha", BASELINE_SHA, "candidate_sha"),
    ("runs", ["baseline", "other"], "runs"),
    ("runs_per_version", 6, "runs_per_version"),
    ("max_reported_tokens_batch", 0, "max_reported_tokens_batch"),
    ("max_calls_each", True, "max_calls_each"),
])
def test_record_fields_are_checked(tmp_path, field, value, where):
    path = write_record(tmp_path / "a.json", **{field: value})
    with pytest.raises(SystemExit, match=f"refused at {where}"):
        series.allowance.load_record(path)


def test_record_missing_key_fails_closed(tmp_path):
    path = write_record(tmp_path / "a.json")
    data = json.loads(path.read_text())
    del data["max_reported_tokens_batch"]
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="max_reported_tokens_batch: missing"):
        series.allowance.load_record(path)


def test_run_refuses_count_above_five(setup):
    tmp_path, fixture, launcher, allowance = setup
    with pytest.raises(SystemExit, match="1 to 5"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "6",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()


def test_pair_record_refuses_count_five_before_any_launch(setup):
    tmp_path, fixture, launcher, allowance = setup
    write_record(allowance, runs_per_version=1)
    with pytest.raises(SystemExit, match="--count 5 exceeds the 1 candidate run slot"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "5",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()
    assert not series.allowance.ledger_path(allowance).exists()


def test_rerun_after_pair_is_consumed_refuses(setup, monkeypatch):
    tmp_path, fixture, launcher, allowance = setup
    write_record(allowance, runs_per_version=1)
    args = ("--count", "1", "--allowance-record", str(allowance))
    assert series.main(_run_args(tmp_path, fixture, launcher, *args)) == 0
    monkeypatch.setattr(series, "_commit", lambda runtime: BASELINE_SHA)
    base = _run_args(tmp_path, fixture, launcher, *args, version="baseline")
    assert series.main(base) == 0
    slots = _ledger(allowance)["slots"]
    assert [(s["version"], s["attempt"], s["reported_tokens"]) for s in slots] == [
        ("candidate", 1, 10), ("baseline", 1, 10)]
    with pytest.raises(SystemExit, match="no run slot left for baseline: 1 of 1 consumed"):
        series.main(base)
    monkeypatch.setattr(series, "_commit", lambda runtime: CANDIDATE_SHA)
    with pytest.raises(SystemExit, match="no run slot left for candidate: 1 of 1 consumed"):
        series.main(_run_args(tmp_path, fixture, launcher, *args, out="elsewhere"))
    assert len((tmp_path / "out" / "calls.txt").read_text().split()) == 2
    assert not (tmp_path / "elsewhere").exists()


def test_wrong_runtime_commit_refuses_then_matching_runs(setup, monkeypatch):
    tmp_path, fixture, launcher, allowance = setup
    args = ("--count", "1", "--allowance-record", str(allowance))
    for commit in ("f" * 40, BASELINE_SHA, "unknown"):
        monkeypatch.setattr(series, "_commit", lambda runtime, c=commit: c)
        with pytest.raises(SystemExit, match="is not the candidate commit"):
            series.main(_run_args(tmp_path, fixture, launcher, *args))
    assert not (tmp_path / "out").exists()
    monkeypatch.setattr(series, "_commit", lambda runtime: CANDIDATE_SHA)
    assert series.main(_run_args(tmp_path, fixture, launcher, *args)) == 0


def test_wall_must_match_the_record(setup):
    tmp_path, fixture, launcher, allowance = setup
    with pytest.raises(SystemExit, match="external_wall_seconds_each 900"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance), "--wall-seconds", "1200"))
    assert not (tmp_path / "out").exists()


def test_run_without_budget_blocks_next_admission(setup, monkeypatch):
    tmp_path, fixture, launcher, allowance = setup
    args = ("--count", "1", "--allowance-record", str(allowance))
    monkeypatch.setenv("FAKE_NO_BUDGET", "1")
    assert series.main(_run_args(tmp_path, fixture, launcher, *args)) == 0
    slot = _ledger(allowance)["slots"][0]
    assert slot["finished_at"] and slot["reported_tokens"] is None
    assert slot["unknown_usage_attempts"] is None
    monkeypatch.delenv("FAKE_NO_BUDGET")
    with pytest.raises(SystemExit, match="usage of slot candidate-1 is unknown"):
        series.main(_run_args(tmp_path, fixture, launcher, *args))
    assert len((tmp_path / "out" / "calls.txt").read_text().split()) == 1


def test_unclosed_slot_blocks_next_admission(setup):
    _, _, _, allowance = setup
    record = series.allowance.load_record(allowance)
    series.allowance.claim_slot(allowance, record, "candidate")
    with pytest.raises(SystemExit, match="usage of slot candidate-1 is unknown"):
        series.allowance.claim_slot(allowance, record, "candidate")


def test_record_edited_after_ledger_exists_refuses(setup):
    tmp_path, fixture, launcher, allowance = setup
    args = ("--count", "1", "--allowance-record", str(allowance))
    assert series.main(_run_args(tmp_path, fixture, launcher, *args)) == 0
    write_record(allowance, runs_per_version=5)
    with pytest.raises(SystemExit, match="ledger belongs to a different allowance record"):
        series.main(_run_args(tmp_path, fixture, launcher, *args))
    assert len((tmp_path / "out" / "calls.txt").read_text().split()) == 1


def test_batch_ceiling_refuses(setup):
    tmp_path, fixture, launcher, allowance = setup
    write_record(allowance, max_reported_tokens_each=100, max_reported_tokens_batch=105)
    args = ("--count", "2", "--allowance-record", str(allowance))
    with pytest.raises(SystemExit, match=r"10 reported tokens spent \+ 100 .* 105"):
        series.main(_run_args(tmp_path, fixture, launcher, *args))
    assert len((tmp_path / "out" / "calls.txt").read_text().split()) == 1
    assert [s["attempt"] for s in _ledger(allowance)["slots"]] == [1]
    assert len((tmp_path / "out" / "candidate-runs.txt").read_text().split()) == 1


def test_run_writes_sidecars_and_grader_in_fresh_copies(setup):
    tmp_path, fixture, launcher, allowance = setup
    grader_file = tmp_path / "fixture-instrument" / "test_contract.py"
    grader = (f'{sys.executable} -c "import os, sys; open(sys.argv[1]).read(); '
              f'print(os.environ[\'CANARY_PROJECT\']); print(\'2 passed in 0.01s\')" {grader_file}')
    assert series.main(_run_args(tmp_path, fixture, launcher, "--count", "2",
                                 "--allowance-record", str(allowance),
                                 "--grader-command", grader)) == 0
    calls = (tmp_path / "out" / "calls.txt").read_text().split()
    assert len(calls) == 2 and calls[0] != calls[1]
    runs = [Path(p) for p in (tmp_path / "out" / "candidate-runs.txt").read_text().split()]
    assert len(runs) == 2
    digest = series.allowance.record_sha256(allowance)
    for attempt, run in enumerate(runs, start=1):
        side = json.loads((run / "series.json").read_text())
        assert side["version"] == "candidate" and side["attempt"] == attempt
        assert side["slot"] == attempt and side["allowance_sha256"] == digest
        assert side["launcher_exit_code"] == 0 and side["runtime_commit"] == CANDIDATE_SHA
        assert "2 passed" in (run / "grader.txt").read_text()
        assert str(run.parents[2]) in (run / "grader.txt").read_text()
    ledger = _ledger(allowance)
    assert ledger["record_sha256"] == digest
    assert [s["run_dir"] for s in ledger["slots"]] == [str(r) for r in runs]
    report = series.aggregate(runs)
    assert report["versions"]["candidate"]["pass_rate"] == "2 of 2"
    assert report["versions"]["candidate"]["completed"] == 2


def test_grader_identity_is_bound_to_the_fixture_manifest(setup):
    tmp_path, fixture, launcher, allowance = setup
    grader = tmp_path / "fixture-instrument" / "test_contract.py"
    (fixture / ".quadratus" / "fixture-manifest.json").write_text(json.dumps(
        {"grader": str(grader), "instrument_sha256": {"test_contract.py": "d" * 64}}))
    with pytest.raises(SystemExit, match="grader sha256"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))
    (fixture / ".quadratus" / "fixture-manifest.json").unlink()
    with pytest.raises(SystemExit, match="no readable manifest"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()


def test_ledger_is_bound_to_the_batch_id_and_runs_read_as_live(setup):
    tmp_path, fixture, launcher, allowance = setup
    assert series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                                 "--allowance-record", str(allowance))) == 0
    ledger = _ledger(allowance)
    assert ledger["batch_id"] == "batch-test-1"
    runs = (tmp_path / "out" / "candidate-runs.txt").read_text().split()
    report = series.aggregate(runs)
    assert report["runs"][0]["provenance"].startswith("live launcher run")
    ledger["batch_id"] = "batch-other"
    series.allowance.ledger_path(allowance).write_text(json.dumps(ledger))
    with pytest.raises(SystemExit, match="belongs to batch batch-other"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))


def test_grader_bytes_are_hashed_not_trusted_from_the_manifest(setup):
    tmp_path, fixture, launcher, allowance = setup
    grader = tmp_path / "fixture-instrument" / "test_contract.py"
    grader.write_text("def test_preservation_ok():\n    assert False\n")  # manifest unchanged
    with pytest.raises(SystemExit, match="hashes to"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))
    assert not (tmp_path / "out").exists()


def test_grader_inside_the_solver_tree_is_refused(setup):
    tmp_path, fixture, launcher, allowance = setup
    inside = fixture / "control" / "test_contract.py"
    inside.parent.mkdir()
    inside.write_text(FAKE_GRADER)
    (fixture / ".quadratus" / "fixture-manifest.json").write_text(json.dumps(
        {"grader": str(inside), "instrument_sha256": {"test_contract.py": _sha(inside)}}))
    with pytest.raises(SystemExit, match="inside the solver tree"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance)))


def test_grader_command_must_name_the_manifest_grader_and_default_runs_it(setup):
    tmp_path, fixture, launcher, allowance = setup
    with pytest.raises(SystemExit, match="must run the fixture's grader"):
        series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                              "--allowance-record", str(allowance),
                              "--grader-command", f"{sys.executable} -m pytest -q somewhere_else.py"))
    assert series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                                 "--allowance-record", str(allowance))) == 0
    runs = (tmp_path / "out" / "candidate-runs.txt").read_text().split()
    text = (Path(runs[0]) / "grader.txt").read_text()
    assert "1 passed" in text


def test_a_grader_mutated_between_admission_and_grading_is_not_run(setup, monkeypatch):
    tmp_path, fixture, launcher, allowance = setup
    grader = tmp_path / "fixture-instrument" / "test_contract.py"
    # The fake launcher mutates the grader during the run.
    launcher.write_text(FAKE_LAUNCHER + "\nif os.environ.get('FAKE_MUTATE'):\n"
                        "    pathlib.Path(os.environ['FAKE_MUTATE']).write_text('def test_x():\\n    pass\\n')\n")
    monkeypatch.setenv("FAKE_MUTATE", str(grader))
    assert series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                                 "--allowance-record", str(allowance))) == 0
    runs = (tmp_path / "out" / "candidate-runs.txt").read_text().split()
    text = (Path(runs[0]) / "grader.txt").read_text()
    assert text.startswith("REFUSED:") and "hashes to" in text
    report = series.aggregate(runs)
    assert report["versions"]["candidate"]["ungraded"] == 1


def test_concurrent_claims_admit_exactly_one(setup):
    import threading
    tmp_path, fixture, launcher, allowance = setup
    write_record(allowance, runs_per_version=1, grader_sha256=_sha(
        tmp_path / "fixture-instrument" / "test_contract.py"))
    record = series.allowance.load_record(allowance)
    barrier = threading.Barrier(2)
    outcomes = []

    def claim():
        barrier.wait()
        try:
            outcomes.append(("ok", series.allowance.claim_slot(allowance, record, "candidate")))
        except SystemExit as exc:
            outcomes.append(("refused", str(exc)))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["ok", "refused"], outcomes
    assert len(_ledger(allowance)["slots"]) == 1
    assert series.allowance.lock_path(allowance).exists()


def test_concurrent_claims_across_processes_admit_exactly_one(setup):
    import subprocess as sp
    tmp_path, fixture, launcher, allowance = setup
    write_record(allowance, runs_per_version=1, grader_sha256=_sha(
        tmp_path / "fixture-instrument" / "test_contract.py"))
    script = (
        "import sys, time; sys.path.insert(0, sys.argv[1]); import allowance as A\n"
        "rec = A.load_record(sys.argv[2])\n"
        "start = float(sys.argv[3])\n"
        "time.sleep(max(0, start - time.time()))\n"
        "try:\n    A.claim_slot(sys.argv[2], rec, 'candidate'); print('ok')\n"
        "except SystemExit as e:\n    print('refused', e)\n"
    )
    import time
    start = str(time.time() + 0.5)
    tools = str(ROOT / "tools" / "acceptance")
    procs = [sp.Popen([sys.executable, "-c", script, tools, str(allowance), start],
                      stdout=sp.PIPE, text=True) for _ in range(2)]
    outs = sorted(p.communicate()[0].split()[0] for p in procs)
    assert outs == ["ok", "refused"], outs
    assert len(_ledger(allowance)["slots"]) == 1


def test_api_credentials_never_reach_the_launcher_or_grader(setup, monkeypatch, capsys):
    tmp_path, fixture, launcher, allowance = setup
    monkeypatch.setenv("XAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("GROK_DEPLOYMENT_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "not-a-real-key")
    monkeypatch.setenv("CUSTOM_VENDOR_API_TOKEN", "not-a-real-key")
    monkeypatch.setenv("HARMLESS_KEYBOARD", "kept")
    assert series.main(_run_args(tmp_path, fixture, launcher, "--count", "1",
                                 "--allowance-record", str(allowance))) == 0
    seen = (tmp_path / "out" / "env-seen.txt").read_text().split()
    assert "PATH" in seen and "HARMLESS_KEYBOARD" in seen
    for name in ("XAI_API_KEY", "GROK_DEPLOYMENT_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "CUSTOM_VENDOR_API_TOKEN"):
        assert name not in seen
    out = capsys.readouterr().out
    assert "scrubbed API credentials" in out and "not-a-real-key" not in out


def test_a_failed_launcher_ends_the_series_with_a_nonzero_exit(setup, monkeypatch, capsys):
    tmp_path, fixture, launcher, allowance = setup
    launcher.write_text(FAKE_LAUNCHER + "\nif os.environ.get('FAKE_FAIL'):\n    sys.exit(2)\n")
    monkeypatch.setenv("FAKE_FAIL", "1")
    rc = series.main(_run_args(tmp_path, fixture, launcher, "--count", "2",
                               "--allowance-record", str(allowance)))
    assert rc == 1
    calls = (tmp_path / "out" / "calls.txt").read_text().split()
    assert len(calls) == 1, "no second launch after a failed one"
    runs = (tmp_path / "out" / "candidate-runs.txt").read_text().split()
    assert len(runs) == 1
    side = json.loads((Path(runs[0]) / "series.json").read_text())
    assert side["launcher_exit_code"] == 2
    ledger = _ledger(allowance)
    assert len(ledger["slots"]) == 1 and ledger["slots"][0]["finished_at"]
    assert "launcher exit 2; series stopped" in capsys.readouterr().out


def test_a_wall_kill_ends_the_series_with_a_nonzero_exit(setup, monkeypatch):
    tmp_path, fixture, launcher, allowance = setup
    monkeypatch.setattr(series, "_launch",
                        lambda *a, **k: (None, "launcher killed after 1s\n"))
    rc = series.main(_run_args(tmp_path, fixture, launcher, "--count", "2",
                               "--allowance-record", str(allowance)))
    assert rc == 1
    runs = (tmp_path / "out" / "candidate-runs.txt").read_text().split()
    assert len(runs) == 1
    assert json.loads((Path(runs[0]) / "series.json").read_text())["launcher_exit_code"] is None
