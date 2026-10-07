"""``quadratus --monitor`` prints the run status and touches nothing."""

from __future__ import annotations

import json
import os

import pytest

from quadratus.cli import main


def _tree(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            stat = os.lstat(path)
            out[path] = (stat.st_size, stat.st_mtime_ns)
    return out


def test_monitor_prints_one_screen_for_a_project(tmp_path, capsys):
    run_dir = tmp_path / ".quadratus" / "runs" / "20261007T120000Z-0000abcd"
    run_dir.mkdir(parents=True)
    (run_dir / "invocations.jsonl").write_text(json.dumps(dict(
        task="T1", role="lead", canonical_model="claude:opus", invoked=True, outcome="ok",
        seconds=1.0, input_tokens=4, output_tokens=4)) + "\n")
    (run_dir / "result.json").write_text(json.dumps(dict(completed=False, error="RunStalled: T1 twice")))
    (run_dir / "report.md").write_text("# r\n")
    before = _tree(tmp_path)
    assert main(["--monitor", "--project", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Run:       not running" in out
    assert "Task:      T1  stage draft" in out
    assert "Ended:     error: RunStalled: T1 twice" in out
    assert str(run_dir / "report.md") in out
    assert "Recent runs:" in out and "20261007T120000Z-0000abcd" in out
    assert _tree(tmp_path) == before


def test_monitor_reads_a_series_directory_without_a_project(tmp_path, capsys):
    project = tmp_path / "cell"
    (project / ".quadratus" / "runs" / "20261007T120000Z-00000001").mkdir(parents=True)
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text(json.dumps(dict(
        packet=dict(limits=dict(max_reported_tokens=5_000_000, reserve_tokens_per_call=250_000,
                                max_tokens_per_call=1_500_000)),
        cells=[dict(name="f1/rule-r0", state="running", project=str(project), started_at="now")])))
    (series / "run.lock").write_text(f"pid={os.getpid()} at=now\n")
    assert main(["--monitor", "--series", str(series), "--history", "3"]) == 0
    out = capsys.readouterr().out
    assert "Run:       LIVE" in out and f"pid {os.getpid()} is running" in out
    assert "Cell:      f1/rule-r0 [running]" in out
    assert "Per call:  reserve 250,000  ceiling 1,500,000" in out
    assert "unknown of 5,000,000" in out


def test_monitor_needs_a_project_or_a_series(capsys):
    assert main(["--monitor"]) == 1
    assert "--project DIR or --series DIR" in capsys.readouterr().out


def test_monitor_refuses_a_prompt_and_series_needs_monitor(tmp_path):
    with pytest.raises(SystemExit):
        main(["--monitor", "--project", str(tmp_path), "do something"])
    with pytest.raises(SystemExit):
        main(["--series", str(tmp_path), "do something"])
