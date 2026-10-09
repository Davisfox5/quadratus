"""The Project tab can serve a finished run's files (batch 2 gui-ui-v3 on
5d9f5ff: returning project/.quadratus/runs/<id>/report.md directly raised
Gradio's InvalidPathError, which blanked the report, the source changes and
the downloads, and the browser download failed)."""

import tempfile

import pytest

from quadratus import gui
from quadratus.config import Settings


def _layout(tmp_path, monkeypatch):
    run_dir = tmp_path / "project" / ".quadratus" / "runs" / "20261009T044636Z-1a7c3419"
    run_dir.mkdir(parents=True)
    for name in gui.RUN_FILES:
        (run_dir / name).write_text(f"{name} body\n")
    system_temp = tmp_path / "systemtemp"
    system_temp.mkdir()
    gui_cwd = tmp_path / "guicwd"
    gui_cwd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    monkeypatch.chdir(gui_cwd)
    return run_dir, system_temp


def _launched(monkeypatch):
    pytest.importorskip("gradio")
    from gradio.context import LocalContext
    demo = gui.build_interface(Settings())
    monkeypatch.setattr(demo, "has_launched", True, raising=False)
    token = LocalContext.blocks.set(demo)
    return token


def test_gradio_refuses_the_run_folder_and_accepts_the_copies(tmp_path, monkeypatch):
    run_dir, system_temp = _layout(tmp_path, monkeypatch)
    from gradio.context import LocalContext
    from gradio.exceptions import InvalidPathError
    from gradio.processing_utils import _check_allowed
    token = _launched(monkeypatch)
    try:
        with pytest.raises(InvalidPathError):
            _check_allowed(str(run_dir / "report.md"), False)   # the failure the real run hit
        copies, problem = gui.downloadable_files(run_dir)
        assert problem == "" and [p.rsplit("/", 1)[1] for p in copies] == list(gui.RUN_FILES)
        for path in copies:
            _check_allowed(path, False)
            assert path.startswith(str(system_temp))
        assert open(copies[0]).read() == "report.md body\n"
        assert (run_dir / "report.md").read_text() == "report.md body\n", "the originals stay the record"
    finally:
        LocalContext.blocks.reset(token)


def test_run_project_ui_yields_servable_copies(tmp_path, monkeypatch):
    run_dir, system_temp = _layout(tmp_path, monkeypatch)

    class Result:
        report, diff = "report", "diff"
    Result.run_dir = run_dir
    monkeypatch.setattr("quadratus.project_run.run_project", lambda *a, **k: Result())
    *_, last = gui.run_project_ui("goal", str(tmp_path / "project"), True, "", "adversarial", 3, Settings())
    report, diff, files = last
    assert report == "report" and diff == "diff"
    assert files and all(f.startswith(str(system_temp)) for f in files)


def test_missing_files_and_links_are_not_offered_and_a_failed_copy_is_named(tmp_path, monkeypatch):
    run_dir, _ = _layout(tmp_path, monkeypatch)
    import shutil

    def failing(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(shutil, "copyfile", failing)
    files, problem = gui.downloadable_files(run_dir)
    assert files == [] and "could not be offered" in problem and str(run_dir) in problem
    monkeypatch.undo()
    for name in gui.RUN_FILES:
        (run_dir / name).unlink()
    files, problem = gui.downloadable_files(run_dir)
    assert files == [] and problem == ""
    (run_dir / "report.md").symlink_to(tmp_path / "elsewhere")
    files, _ = gui.downloadable_files(run_dir)
    assert files == [], "a link is never copied"


def test_a_linked_run_file_is_never_copied(tmp_path, monkeypatch):
    run_dir, _ = _layout(tmp_path, monkeypatch)
    secret = tmp_path / "secret.txt"
    secret.write_text("not a run file\n")
    (run_dir / "ledger.md").unlink()
    (run_dir / "ledger.md").symlink_to(secret)
    files, _ = gui.downloadable_files(run_dir)
    assert [f.rsplit("/", 1)[1] for f in files] == ["report.md", "changes.diff", "result.json"]
