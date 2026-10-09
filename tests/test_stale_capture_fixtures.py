"""Capture-only samples from an earlier run never stand in for this run's
(Codex review comment 4226680384 on #53): task ids restart at t1, and an
existing file under .quadratus/capture-fixtures/t1/ counted as supplied.
Retirement is bound to the project's own state directory (Codex review of
391f3c8: a pathname move followed a .quadratus replaced by a link)."""

import json
import os

from quadratus import project_run
from quadratus.config import Settings
from quadratus.project_run import _retire_stale_fixtures, run_project
from quadratus.session import Session


def _sample(root, text):
    folder = root / ".quadratus" / "capture-fixtures" / "t1"
    folder.mkdir(parents=True)
    (folder / "rows.csv").write_text(text)


def test_an_earlier_runs_samples_are_retired_before_any_call(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x\n")
    _sample(tmp_path, "dictated for a different task\n")
    seen = {}

    def run(self, **kwargs):
        seen["present"] = (tmp_path / ".quadratus" / "capture-fixtures").exists()
        return None
    monkeypatch.setattr(Session, "run", run)
    notes = []
    result = run_project("g", tmp_path, Settings(backend="cli"), allow_writes=True, progress=notes.append)
    assert seen["present"] is False, "the session starts with no samples it did not write"
    retired = f".quadratus/capture-fixtures.retired-{result.run_dir.name}"
    assert (tmp_path / retired / "t1" / "rows.csv").read_text() == "dictated for a different task\n"
    record = json.loads((result.run_dir / "result.json").read_text())
    assert record["stale_capture_fixtures"]["path"] == retired
    assert record["stale_capture_fixtures"]["resolves"] is True
    assert any("from an earlier run retired" in n for n in notes)
    assert not result.diff, "the samples are harness state, not project source"


def test_nothing_to_retire_records_none(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert _retire_stale_fixtures(tmp_path, run_dir) is None
    (tmp_path / ".quadratus" / "capture-fixtures").mkdir(parents=True)
    assert _retire_stale_fixtures(tmp_path, run_dir) is None
    assert (tmp_path / ".quadratus" / "capture-fixtures").is_dir(), "an empty folder is left as it is"


def test_a_linked_fixture_folder_or_state_folder_is_left_alone_and_named(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "t1").mkdir(parents=True)
    (elsewhere / "t1" / "rows.csv").write_text("outside\n")
    project = tmp_path / "project"
    (project / ".quadratus").mkdir(parents=True)
    os.symlink(elsewhere, project / ".quadratus" / "capture-fixtures")
    notes = []
    assert _retire_stale_fixtures(project, tmp_path / "run", notes.append) is None
    assert (elsewhere / "t1" / "rows.csv").read_text() == "outside\n"
    assert notes and "capture-fixtures is a symlink" in notes[0]

    linked = tmp_path / "linked"
    linked.mkdir()
    other_state = tmp_path / "other" / ".quadratus"
    _sample(tmp_path / "other", "other project sample\n")
    os.symlink(other_state, linked / ".quadratus")
    notes = []
    assert _retire_stale_fixtures(linked, tmp_path / "run", notes.append) is None
    assert (other_state / "capture-fixtures" / "t1" / "rows.csv").read_text() == "other project sample\n"
    assert notes and ".quadratus is a symlink" in notes[0]


def test_a_state_folder_replaced_by_a_link_after_it_is_bound_moves_nothing_foreign(tmp_path, monkeypatch):
    """The race Codex reproduced on 391f3c8: .quadratus is swapped for a link
    to another project's state right after it was checked. The rename acts
    on the directory handle already held, so the foreign fixtures stay."""
    project = tmp_path / "project"
    _sample(project, "owned stale sample\n")
    other = tmp_path / "other"
    _sample(other, "other project active sample\n")
    parked = tmp_path / "parked"
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == ".quadratus":
            os.rename(project / ".quadratus", parked)
            os.symlink(other / ".quadratus", project / ".quadratus")
        return fd
    monkeypatch.setattr(project_run.os, "open", swapping_open)
    where = _retire_stale_fixtures(project, tmp_path / "run-1")
    monkeypatch.setattr(project_run.os, "open", real_open)
    assert (other / ".quadratus" / "capture-fixtures" / "t1" / "rows.csv").read_text() == \
        "other project active sample\n", "the foreign project's fixtures are untouched"
    assert where["path"] == ".quadratus/capture-fixtures.retired-run-1"
    assert where["resolves"] is False, "the path now names the other project's state; the record says so"
    assert (parked / "capture-fixtures.retired-run-1" / "t1" / "rows.csv").read_text() == "owned stale sample\n"
    info = os.stat(parked)
    assert where["state_directory"] == {"device": info.st_dev, "inode": info.st_ino}


def test_an_unreadable_state_folder_stops_the_run_before_any_call(tmp_path, monkeypatch):
    """Codex review of 6a338a1: a read-permission failure returned quietly and
    the session started with the stale samples in place."""
    import errno

    import pytest
    project = tmp_path / "project"
    _sample(project, "stale\n")
    real_open = os.open

    def refusing_open(path, flags, *args, **kwargs):
        if path == ".quadratus":
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(project_run.os, "open", refusing_open)
    with pytest.raises(ValueError, match="could not be retired .*PermissionError"):
        _retire_stale_fixtures(project, tmp_path / "run")
    monkeypatch.setattr(project_run.os, "open", real_open)
    assert (project / ".quadratus" / "capture-fixtures" / "t1" / "rows.csv").read_text() == "stale\n"

    calls = []
    monkeypatch.setattr(project_run.os, "open", refusing_open)
    monkeypatch.setattr(Session, "run", lambda self, **kw: calls.append(1))
    with pytest.raises(ValueError, match="could not be retired"):
        run_project("g", project, Settings(backend="cli"), allow_writes=True)
    assert calls == [], "no session ran"
