"""Regression tests for what the independent review of the trust-boundary
fixes left for the integrator (2026-11-11): a run that loses its own record
folder, a capture sample named like code, and the acceptance wording."""
import json
import shutil
from pathlib import Path

import pytest

from quadratus import cli, project_run, session
from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.session import Session, _confined_fixture_target


def _project(tmp_path):
    root = tmp_path / "project"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    (root / "app.py").write_text("x = 1\n")
    return root


def test_a_run_whose_own_record_folder_was_removed_still_writes_its_record(tmp_path, monkeypatch):
    """An editing call can remove the run's own folder. Writing the record
    then raised, no result.json existed, and the next run started over
    whatever the same call had planted."""
    root = _project(tmp_path)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(tmp_path / "scratch"))
    (tmp_path / "scratch").mkdir(mode=0o700)

    def removes_its_own_run_folder(self, **kwargs):
        runs = root / ".quadratus" / "runs"
        for folder in runs.iterdir():
            shutil.rmtree(folder)

    monkeypatch.setattr(Session, "run", removes_its_own_run_folder)
    result = run_project("g", root, Settings(backend="cli"), allow_writes=True)
    record = json.loads((result.run_dir / "result.json").read_text())
    assert "RunRecordLost" in result.error and "RunRecordLost" in record["error"]
    assert record["completed"] is False
    assert (result.run_dir.stat().st_mode & 0o777) == 0o700


def test_the_loss_is_added_to_a_stop_the_run_already_had(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(tmp_path / "scratch"))
    (tmp_path / "scratch").mkdir(mode=0o700)

    def removes_then_fails(self, **kwargs):
        for folder in (root / ".quadratus" / "runs").iterdir():
            shutil.rmtree(folder)
        raise RuntimeError("the call failed after removing the folder")

    monkeypatch.setattr(Session, "run", removes_then_fails)
    result = run_project("g", root, Settings(backend="cli"), allow_writes=True)
    assert "RuntimeError" in result.error and "RunRecordLost" in result.error
    assert (result.run_dir / "result.json").is_file()


def test_a_run_that_kept_its_folder_says_nothing_about_a_loss(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(tmp_path / "scratch"))
    (tmp_path / "scratch").mkdir(mode=0o700)
    monkeypatch.setattr(Session, "run", lambda self, **kwargs: None)
    result = run_project("g", root, Settings(backend="cli"), allow_writes=True)
    assert "RunRecordLost" not in result.error


def test_a_link_left_in_the_run_folders_place_is_not_written_through(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    run_dir = tmp_path / "state" / "runs" / "20261111T000000Z-abcdef12"
    run_dir.parent.mkdir(parents=True)
    run_dir.symlink_to(elsewhere, target_is_directory=True)
    assert project_run._restore_run_folder(run_dir) is True
    assert run_dir.is_dir() and not run_dir.is_symlink()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["conftest.py", "helper.py", "package.json", "jest.config.js", "site.pth",
                                  "pytest.ini", "pyproject.toml"])
def test_a_capture_sample_named_like_code_is_not_written(tmp_path, name):
    """The harness writes a capture-only sample on a model's word, between
    calls, where no listing reads. A name a test runner loads by position
    would be planted code written by the harness itself."""
    target, why = _confined_fixture_target(tmp_path, f".quadratus/capture-fixtures/t1/{name}", "t1")
    assert target is None and "loaded by a test runner" in why


@pytest.mark.parametrize("name", ["sample.csv", "data.json", "upload.png", "notes.txt", "page.html"])
def test_an_ordinary_capture_sample_is_still_written(tmp_path, name):
    target, why = _confined_fixture_target(tmp_path, f".quadratus/capture-fixtures/t1/{name}", "t1")
    assert target == tmp_path / ".quadratus" / "capture-fixtures" / "t1" / name, why


def test_the_capture_rule_states_what_a_sample_may_not_be_named():
    """Prompt parity: the orchestrator declares the sample's name, so its
    capture rule states the rule the name is held to."""
    rule = " ".join(session._CAPTURE_SCOPE_REQUEST.split())
    for said in ("never conftest.py", ".py or .pth", "package.json", "test-runner config"):
        assert said in rule, said


def test_the_help_says_an_acceptance_lasts():
    source = " ".join(Path(cli.__file__).read_text().split())
    assert "Accepts them for this run only" not in source
    assert "later runs do not ask about the same paths again" in source
