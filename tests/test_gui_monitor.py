"""The Monitor tab reads the run directory and never touches a run."""

from __future__ import annotations

import json
import os

import pytest

from quadratus import gui
from quadratus.config import Settings
from quadratus.monitor import HISTORY_COLUMNS


def _tree(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            stat = os.lstat(path)
            out[path] = (stat.st_size, stat.st_mtime_ns)
    return out


def test_the_view_renders_status_and_history_from_the_run_directory(tmp_path):
    run_dir = tmp_path / ".quadratus" / "runs" / "20261007T120000Z-0badcafe"
    run_dir.mkdir(parents=True)
    (run_dir / "invocations.jsonl").write_text(json.dumps(dict(
        task="T2", role="verifier", canonical_model="openai:gpt-5.6-sol", invoked=True, outcome="ok",
        seconds=3.0, input_tokens=10, output_tokens=5)) + "\n")
    (run_dir / "usage.jsonl").write_text(json.dumps(dict(model="openai:gpt-5.6-sol", input_tokens=10,
                                                         output_tokens=5)) + "\n")
    before = _tree(tmp_path)
    markdown, rows = gui.monitor_view(str(tmp_path))
    assert "**Task:** T2 (stage review)" in markdown
    assert "**Seat:** openai:gpt-5.6-sol" in markdown
    assert "### Run: unknown" in markdown  # a project run records no pid
    assert len(rows) == 1 and len(rows[0]) == len(HISTORY_COLUMNS)
    assert rows[0][0] == "20261007T120000Z-0badcafe" and rows[0][3] == "unfinished" and rows[0][4] == 15
    assert _tree(tmp_path) == before


def test_a_blank_folder_falls_back_to_the_opened_project_and_a_blank_view_explains(tmp_path):
    markdown, rows = gui.monitor_view("", "", "")
    assert "Enter a project folder" in markdown and rows == []
    markdown, rows = gui.monitor_view("", "", str(tmp_path))
    assert str(tmp_path) in markdown and "not running" in markdown and rows == []


def test_a_series_directory_supplies_the_project_and_the_packet_cap(tmp_path):
    project = tmp_path / "cell"
    (project / ".quadratus" / "runs" / "20261007T120000Z-00000001").mkdir(parents=True)
    series = tmp_path / "series"
    series.mkdir()
    (series / "manifest.json").write_text(json.dumps(dict(
        packet=dict(limits=dict(max_reported_tokens=5_000_000)),
        cells=[dict(name="f3/jev-r0", state="running", project=str(project), started_at="2026-10-07T12:00:00Z")])))
    (series / "run.lock").write_text(f"pid={os.getpid()} at=2026-10-07T12:00:00Z\n")
    markdown, rows = gui.monitor_view("", str(series))
    assert "### Run: LIVE" in markdown and "**Cell:** f3/jev-r0 [running]" in markdown
    assert "of 5,000,000" in markdown
    assert len(rows) == 1


def test_bad_input_never_raises(tmp_path):
    markdown, rows = gui.monitor_view(str(tmp_path / "missing"), str(tmp_path / "no-series"))
    assert "unknown" in markdown.lower() and rows == []


def test_the_interface_builds_with_the_monitor_tab():
    gradio = pytest.importorskip("gradio")
    demo = gui.build_interface(Settings())
    assert isinstance(demo, gradio.Blocks)
    labels = {getattr(block, "label", None) for block in demo.blocks.values()}
    assert "Stage B series directory (optional)" in labels
    assert any(isinstance(block, gradio.Timer) for block in demo.blocks.values())
