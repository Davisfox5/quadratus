"""Capture-only samples from an earlier run never stand in for this run's
(Codex review comment 4226680384 on #53): task ids restart at t1, and an
existing file under .quadratus/capture-fixtures/t1/ counted as supplied."""

import json
import os

from quadratus.config import Settings
from quadratus.project_run import _retire_stale_fixtures, run_project
from quadratus.session import Session


def test_an_earlier_runs_samples_are_moved_into_the_new_run_before_any_call(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x\n")
    old = tmp_path / ".quadratus" / "capture-fixtures" / "t1"
    old.mkdir(parents=True)
    (old / "rows.csv").write_text("dictated for a different task\n")
    seen = {}

    def run(self, **kwargs):
        seen["present"] = (tmp_path / ".quadratus" / "capture-fixtures").exists()
        return None
    monkeypatch.setattr(Session, "run", run)
    notes = []
    result = run_project("g", tmp_path, Settings(backend="cli"), allow_writes=True, progress=notes.append)
    assert seen["present"] is False, "the session starts with no samples it did not write"
    moved = result.run_dir / "stale-capture-fixtures" / "t1" / "rows.csv"
    assert moved.read_text() == "dictated for a different task\n", "moved, never deleted"
    record = json.loads((result.run_dir / "result.json").read_text())
    assert record["stale_capture_fixtures"] == str(result.run_dir / "stale-capture-fixtures")
    assert any("from an earlier run moved" in n for n in notes)
    assert not result.diff, "the samples are harness state, not project source"


def test_nothing_to_retire_records_none(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert _retire_stale_fixtures(tmp_path, run_dir) is None
    (tmp_path / ".quadratus" / "capture-fixtures").mkdir(parents=True)
    assert _retire_stale_fixtures(tmp_path, run_dir) is None
    assert (tmp_path / ".quadratus" / "capture-fixtures").is_dir(), "an empty folder is left as it is"


def test_a_linked_fixture_folder_is_left_alone_and_named(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "t1").mkdir(parents=True)
    (elsewhere / "t1" / "rows.csv").write_text("outside\n")
    project = tmp_path / "project"
    (project / ".quadratus").mkdir(parents=True)
    os.symlink(elsewhere, project / ".quadratus" / "capture-fixtures")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    notes = []
    assert _retire_stale_fixtures(project, run_dir, notes.append) is None
    assert (elsewhere / "t1" / "rows.csv").read_text() == "outside\n"
    assert notes and "is a symlink" in notes[0]
