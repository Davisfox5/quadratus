"""The run monitor reads the run directory and never writes, raises, or guesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from quadratus import monitor
from quadratus.monitor import UNKNOWN, read_status, render_markdown, render_text, run_history

STAMP = "20261007T120000Z"


def _run_dir(project: Path, stamp: str = STAMP, suffix: str = "deadbeef") -> Path:
    run_dir = project / ".quadratus" / "runs" / f"{stamp}-{suffix}"
    run_dir.mkdir(parents=True)
    return run_dir


def _event(task="T1", role="lead", model="claude:opus", **extra) -> dict:
    record = dict(task=task, role=role, origin="seat", requested_model=model, resolved_model=model,
                  canonical_model=model, invoked=True, outcome="ok", seconds=12.5,
                  input_tokens=1000, output_tokens=200, attempt=1, detail="")
    record.update(extra)
    return record


def _append(path: Path, *records) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _tree(root: Path) -> dict:
    """Every path under root with its size and mtime, to prove nothing was written."""
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(base) / name
            stat = path.lstat()
            out[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return out


# -- absence and malformation ------------------------------------------------

def test_an_empty_project_reads_as_unknown_everywhere_with_reasons(tmp_path):
    status = read_status(tmp_path)
    assert status["live"] is False
    assert "no run directories" in status["liveness"]
    for field in ("run_id", "task", "stage", "seat", "tokens_reported", "last_event", "terminal_status"):
        assert status[field] == UNKNOWN
        assert status["unknown"][field]
    assert status["unknown"]["max_reported_tokens"].startswith("no series directory")
    assert run_history(tmp_path) == []


def test_a_missing_project_folder_does_not_raise(tmp_path):
    status = read_status(tmp_path / "nowhere")
    assert status["run_id"] == UNKNOWN
    assert "does not exist" in status["unknown"]["run_id"]
    assert read_status(None)["unknown"]["project"]


def test_malformed_records_are_skipped_and_counted_never_raised(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "invocations.jsonl").write_text('{"task": "T1", "role": "lead"}\nnot json\n[1,2]\n'
                                               + json.dumps(_event(task="T2", role="verifier")) + "\n")
    (run_dir / "usage.jsonl").write_text("garbage\n" + json.dumps(dict(model="x", input_tokens=5,
                                                                        output_tokens=7)) + "\n")
    (run_dir / "result.json").write_text("{not json")
    status = read_status(tmp_path)
    assert status["task"] == "T2" and status["stage"] == "review"
    assert status["tokens_reported"] == 12
    assert status["terminal_status"] == UNKNOWN
    assert "result.json unreadable" in status["unknown"]["terminal_status"]
    assert "malformed" in status["unknown"]["last_event_note"]


def test_the_monitor_never_writes_anything(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "invocations.jsonl", _event())
    _append(run_dir / "usage.jsonl", dict(model="claude:opus", input_tokens=1, output_tokens=2))
    (run_dir / "artifacts").mkdir()
    (run_dir / "artifacts" / ("a" * 12 + ".json")).write_text(json.dumps(dict(kind="draft")))
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text(json.dumps(dict(packet=dict(limits=dict(max_reported_tokens=10)),
                                                          cells=[])))
    (series / "run.lock").write_text(f"pid={os.getpid()} at=now\n")
    before = _tree(tmp_path)
    read_status(tmp_path, series)
    run_history(tmp_path)
    assert _tree(tmp_path) == before


# -- liveness -----------------------------------------------------------------

def test_a_live_series_pid_reads_as_live(tmp_path):
    project = tmp_path / "cell"
    run_dir = _run_dir(project)
    _append(run_dir / "invocations.jsonl", _event())
    series = tmp_path / "series"
    series.mkdir()
    manifest = dict(packet=dict(limits=dict(max_reported_tokens=5_000_000, reserve_tokens_per_call=250_000,
                                            max_tokens_per_call=1_500_000)),
                    cells=[dict(name="f1/jev-r0", state="ran", project=str(tmp_path / "old"),
                                started_at="2026-10-07T10:00:00Z"),
                           dict(name="f1/rule-r0", state="running", project=str(project),
                                started_at="2026-10-07T11:00:00Z")])
    (series / "manifest.json").write_text(json.dumps(manifest))
    (series / "run.lock").write_text(f"pid={os.getpid()} at=2026-10-07T11:00:00Z\n")
    status = read_status(None, series)
    assert status["live"] is True
    assert status["pid"] == os.getpid()
    assert "started 2026-10-07T11:00:00Z" in status["liveness"]
    assert status["project"] == str(project)
    assert status["cell"] == "f1/rule-r0 [running]"
    assert status["max_reported_tokens"] == 5_000_000
    assert status["reserve_tokens_per_call"] == 250_000
    assert status["max_tokens_per_call"] == 1_500_000
    assert status["series"]["cells"] == {"ran": 1, "running": 1}


def test_a_dead_series_pid_reads_as_not_running(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text(json.dumps(dict(packet={}, cells=[])))
    (series / "run.lock").write_text(f"pid={child.pid} at=then\n")
    status = read_status(tmp_path, series)
    assert status["live"] is False
    assert f"pid {child.pid} is not running" in status["liveness"]
    assert status["pid"] == child.pid


def test_a_series_without_a_lock_is_not_running_and_a_bad_lock_is_unknown(tmp_path):
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text(json.dumps(dict(packet={}, cells=[], stopped=dict(
        reason="cell-error: engine saved X", after="f1/jev-r0"))))
    status = read_status(tmp_path, series)
    assert status["live"] is False and "no series lock" in status["liveness"]
    assert status["series"]["stopped"] == "cell-error: engine saved X"
    assert status["unknown"]["max_reported_tokens"]
    (series / "run.lock").write_text("held by someone\n")
    status = read_status(tmp_path, series)
    assert status["pid"] == UNKNOWN and "no pid=" in status["unknown"]["pid"]


def test_a_project_run_without_a_pid_is_unknown_with_its_last_write_age(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "invocations.jsonl", _event())
    now = (run_dir / "invocations.jsonl").stat().st_mtime + 42
    status = read_status(tmp_path, now=now)
    assert status["finished"] is False
    assert status["live"] == UNKNOWN
    assert "records no pid" in status["liveness"] and "42 s ago" in status["liveness"]
    assert status["run_id"] == f"{STAMP}-deadbeef"
    assert status["started"] == "2026-10-07T12:00:00Z"
    assert status["terminal_status"] == UNKNOWN and status["unknown"]["terminal_status"] == "run has not finished"


# -- the latest call -----------------------------------------------------------

def test_the_latest_call_gives_task_seat_role_stage_and_the_event_line(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "invocations.jsonl", _event(task="run", role="orchestrator", model="claude:fable"),
            _event(task="T3", role="gate-fix", model="openai:gpt-5.6-sol", seconds=80.2, attempt=2,
                   detail="Gate failed: 2 tests\nsecond line"))
    status = read_status(tmp_path)
    assert status["task"] == "T3" and status["role"] == "gate-fix" and status["stage"] == "gate"
    assert status["seat"] == "openai:gpt-5.6-sol"
    assert status["last_event"] == ("T3/gate-fix | openai:gpt-5.6-sol | invoked | ok | 80 s | 1,200 tokens"
                                    " | attempt 2 | Gate failed: 2 tests")
    assert status["calls"] == 2


def test_every_recorded_role_maps_to_one_of_the_five_stages_or_plan():
    assert set(monitor.ROLE_STAGES.values()) == {"plan", "draft", "review", "gate", "capture", "close-out"}
    for role in ("lead", "revision", "verifier", "gate-fix", "design-fix", "closeout", "orchestrator"):
        assert role in monitor.ROLE_STAGES


def test_an_unmapped_role_leaves_the_stage_unknown_with_the_role_named(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "invocations.jsonl", _event(role="something-new"))
    status = read_status(tmp_path)
    assert status["stage"] == UNKNOWN
    assert "something-new" in status["unknown"]["stage"]


def test_an_artifact_newer_than_the_last_call_moves_the_stage_on(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "invocations.jsonl", _event(role="lead"))
    old = time.time() - 60
    os.utime(run_dir / "invocations.jsonl", (old, old))
    store = run_dir / "artifacts"
    store.mkdir()
    (store / ("1" * 12 + ".json")).write_text(json.dumps(dict(kind="draft")))
    os.utime(store / ("1" * 12 + ".json"), (old - 1, old - 1))
    (store / ("2" * 12 + ".json")).write_text(json.dumps(dict(kind="design-evidence")))
    status = read_status(tmp_path)
    assert status["stage"] == "capture"
    # An older artifact does not: the call is the latest word.
    os.utime(store / ("2" * 12 + ".json"), (old - 2, old - 2))
    assert read_status(tmp_path)["stage"] == "draft"


def test_the_jsonl_tail_is_bounded_and_drops_the_cut_line(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    path = run_dir / "invocations.jsonl"
    _append(path, *[_event(task=f"T{i}", detail="x" * 300) for i in range(50)])
    monkeypatch.setattr(monitor, "TAIL_BYTES", 2000)
    records, problem = monitor._tail_jsonl(path, max_bytes=2000)
    assert 0 < len(records) < 50 and problem == ""
    assert records[-1]["task"] == "T49"
    status = read_status(tmp_path)
    assert status["task"] == "T49"
    assert status["calls"] == UNKNOWN and "tail bound" in status["unknown"]["calls"]


# -- tokens ---------------------------------------------------------------------

def test_tokens_come_from_budget_json_when_the_run_has_limits(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "budget.json").write_text(json.dumps(dict(
        limits=dict(max_reported_tokens=500_000, reserve_tokens_per_call=0, max_tokens_per_call=100_000),
        reserved_attempts=7, reported_tokens=123_456)))
    _append(run_dir / "usage.jsonl", dict(model="x", input_tokens=1, output_tokens=1))
    status = read_status(tmp_path)
    assert status["tokens_reported"] == 123_456 and status["calls"] == 7
    assert status["max_reported_tokens"] == 500_000  # the run's own limits when no packet is read
    assert "max_reported_tokens" not in status["unknown"]
    assert "123,456 of 500,000 (25%)" in render_text(status)


def test_tokens_are_summed_from_usage_when_there_is_no_budget(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "usage.jsonl", dict(model="a", input_tokens=100, output_tokens=20),
            dict(model="b", input_tokens="bad", output_tokens=5), dict(model="c"))
    assert read_status(tmp_path)["tokens_reported"] == 125


def test_an_oversized_usage_log_is_unknown_rather_than_loaded(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "usage.jsonl", *[dict(model="a", input_tokens=1, output_tokens=1) for _ in range(20)])
    monkeypatch.setattr(monitor, "MAX_USAGE_BYTES", 100)
    status = read_status(tmp_path)
    assert status["tokens_reported"] == UNKNOWN
    assert "read bound" in status["unknown"]["tokens_reported"]


# -- a finished run -----------------------------------------------------------

def _finish(run_dir: Path, **result) -> None:
    payload = dict(completed=False, error="", checks=[], tasks=1)
    payload.update(result)
    (run_dir / "result.json").write_text(json.dumps(payload))
    (run_dir / "report.md").write_text("# report\n")


def test_a_finished_run_reports_its_terminal_status_stop_reason_and_report(tmp_path):
    run_dir = _run_dir(tmp_path)
    _finish(run_dir, completed=False, error="RunStalled: orchestrator named T2 twice\ntrace line")
    status = read_status(tmp_path)
    assert status["finished"] is True and status["live"] is False
    assert "result.json" in status["liveness"]
    assert status["terminal_status"] == "error"
    assert status["stop_reason"] == "RunStalled: orchestrator named T2 twice"
    assert status["report"] == str(run_dir / "report.md")
    text = render_text(status)
    assert "Ended:     error: RunStalled" in text and str(run_dir / "report.md") in text


def test_completion_and_budget_stops_read_as_one_line_each(tmp_path):
    done = _run_dir(tmp_path, "20261007T130000Z")
    _finish(done, completed=True)
    assert read_status(tmp_path)["stop_reason"] == "goal reported complete"
    assert read_status(tmp_path)["terminal_status"] == "complete"
    later = _run_dir(tmp_path, "20261007T140000Z")
    _finish(later, completed=False, budget=dict(stop_reason="max_reported_tokens"))
    status = read_status(tmp_path)
    assert status["run_id"].startswith("20261007T140000Z")
    assert status["terminal_status"] == "incomplete"
    assert status["stop_reason"] == "budget: max_reported_tokens"


def test_a_finished_series_cell_stays_live_while_the_series_process_runs(tmp_path):
    project = tmp_path / "cell"
    _finish(_run_dir(project), completed=True)
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text(json.dumps(dict(packet={}, cells=[
        dict(name="f1/jev-r0", state="ran", project=str(project), started_at="2026-10-07T10:00:00Z")])))
    (series / "run.lock").write_text(f"pid={os.getpid()}\n")
    status = read_status(None, series)
    assert status["live"] is True and status["finished"] is True
    assert status["terminal_status"] == "complete"


# -- history ----------------------------------------------------------------

def test_history_lists_the_last_n_runs_newest_first_with_tokens_and_stop_reason(tmp_path):
    for hour, outcome in ((10, dict(completed=True)), (11, dict(error="RunBudgetExceeded: Run stopped: wall")),
                          (12, {})):
        run_dir = _run_dir(tmp_path, f"20261007T{hour:02d}0000Z", f"{hour:08x}")
        _append(run_dir / "usage.jsonl", dict(model="a", input_tokens=hour * 100, output_tokens=0))
        _finish(run_dir, **outcome)
    live = _run_dir(tmp_path, "20261007T130000Z", "0000000d")
    _append(live / "usage.jsonl", dict(model="a", input_tokens=5, output_tokens=5))
    rows = run_history(tmp_path, limit=3)
    assert [r["run"][:16] for r in rows] == ["20261007T130000Z", "20261007T120000Z", "20261007T110000Z"]
    assert rows[0]["status"] == "unfinished" and rows[0]["ended"] == UNKNOWN and rows[0]["tokens"] == 10
    assert rows[1]["status"] == "incomplete" and rows[1]["stop_reason"] == "open findings, checks or task limit"
    assert rows[2]["status"] == "error" and rows[2]["stop_reason"].startswith("RunBudgetExceeded")
    assert rows[2]["tokens"] == 1100 and rows[2]["ended"] != UNKNOWN
    table = monitor.history_rows(rows)
    assert table[0][0] == rows[0]["run"] and len(table[0]) == len(monitor.HISTORY_COLUMNS)
    assert len(run_history(tmp_path, limit=1)) == 1


def test_a_broken_run_directory_is_one_row_not_a_crash(tmp_path):
    run_dir = _run_dir(tmp_path)
    (run_dir / "result.json").mkdir()  # a directory where a file should be
    rows = run_history(tmp_path)
    assert len(rows) == 1 and rows[0]["status"] == UNKNOWN
    assert rows[0]["stop_reason"]


# -- rendering --------------------------------------------------------------

def test_the_text_and_markdown_renders_carry_every_field(tmp_path):
    run_dir = _run_dir(tmp_path)
    _append(run_dir / "invocations.jsonl", _event(task="T1", role="lead", model="claude:opus"))
    _append(run_dir / "usage.jsonl", dict(model="claude:opus", input_tokens=1000, output_tokens=200))
    status = read_status(tmp_path)
    text = render_text(status, run_history(tmp_path))
    for piece in ("Quadratus monitor", "Run:       unknown", "Task:      T1  stage draft",
                  "Seat:      claude:opus  role lead", "Tokens:    1,200", "Last:      T1/lead",
                  "Unknown:", "Recent runs:", status["run_id"]):
        assert piece in text, piece
    assert "\n" + "Unknown:" in text
    md = render_markdown(status)
    assert "### Run: unknown" in md and "**Task:** T1 (stage draft)" in md
    assert "<details><summary>Unknown fields</summary>" in md
    assert "claude:opus" in md
