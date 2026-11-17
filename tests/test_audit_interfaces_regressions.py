"""Reproductions for interface bugs found in the whole-repository audit."""

import json
import queue
import threading

import pytest

from quadratus import cli, gui
from quadratus.project import Project
from tools import truth_check


@pytest.mark.parametrize("option", [
    ["--extra-check", "python -m pytest"],
    ["--capture-profile", "profile.json"],
    ["--readiness", "probes.json"],
    ["--repair-test", "tests/test_app.py"],
    ["--tasks", "tasks.json"],
    ["--rulings", "rulings.json"],
    ["--resume", "prior-run"],
    ["--survey-recovery", "0"],
    ["--direct-tier"],
])
@pytest.mark.parametrize("engine", [[], ["--session"]])
def test_project_controls_cannot_be_silently_ignored_without_a_project(option, engine, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid launch must be rejected before settings or providers are constructed")

    monkeypatch.setattr(cli, "_build_settings", unexpected)
    with pytest.raises(SystemExit) as exc:
        cli.main(["Fix the bug", *engine, *option])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert option[0] in error and "require --project" in error


def test_a_double_answer_cannot_become_the_next_questions_ruling(monkeypatch):
    waiting, release = threading.Event(), threading.Event()
    original_queue = queue.Queue

    class PausingAnswers(original_queue):
        def get(self, *args, **kwargs):
            waiting.set()
            assert release.wait(5)
            return super().get(*args, **kwargs)

    def make_queue(maxsize=0):
        return PausingAnswers(maxsize) if maxsize == 1 else original_queue(maxsize)

    monkeypatch.setattr(gui.queue, "Queue", make_queue)
    channel = gui.OperatorChannel(timeout=1)
    replies = []
    worker = threading.Thread(target=lambda: replies.append(channel.ask("First question?")))
    worker.start()
    try:
        assert waiting.wait(5)
        assert channel.answer("First answer") == "Answer sent; the run continues."
        assert channel.answer("Accidental second submit") == "No question is waiting for an answer."
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and replies == ["First answer"]

    waiting.clear()
    release.clear()
    worker = threading.Thread(target=lambda: replies.append(channel.ask("Second question?")))
    worker.start()
    try:
        assert waiting.wait(5)
        assert channel.answer("Second answer") == "Answer sent; the run continues."
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and replies == ["First answer", "Second answer"]


@pytest.mark.parametrize("diff", [
    '--- a/templates/index.html\n+++ /dev/null\n',
    '--- /dev/null\n+++ "b/templates/odd\\tname.html"\n',
    '--- /dev/null\n+++ "b/templates/\\303\\251.html"\n',
])
def test_deleted_and_git_quoted_ui_changes_still_need_capture_evidence(tmp_path, diff):
    run = _truth_run(tmp_path)
    diff_path = run / "changes.diff"
    assert truth_check.judge(run)["verdict"] == truth_check.VERIFIED
    diff_path.write_text(diff)
    result = truth_check.judge(run)
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("UI files changed with no capture evidence" in reason for reason in result["unverified"])


def _truth_run(tmp_path):
    check = dict(passed=True, source="final-source", receipts=[
        dict(id="tests", required=True, status="passed", tests=1)])
    record = dict(completed=True, checks=[check], source_fingerprint="final-source",
                  requirements=dict(listed={"R1": "Change the UI"}, status={"R1": "met"},
                                    audits=[dict(verdicts={"R1": dict(met=True)})]),
                  workflow=dict(tasks=[dict(checks=[check])]))
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps(record))
    (run / "changes.diff").write_text("+++ b/app.py\n")
    return run


@pytest.mark.parametrize("before,after", [
    ("ordinary notes\n", "ordinary notes\n++ b/fake.html\n"),
    ("ordinary notes\n-- a/fake.html\n", "ordinary notes\n"),
])
def test_header_like_hunk_payload_does_not_require_ui_capture(tmp_path, before, after):
    root = tmp_path / "project"
    root.mkdir()
    notes = root / "notes.txt"
    notes.write_text(before)
    project = Project(root)
    original = project.contents()
    notes.write_text(after)
    diff = project.diff(original)

    run = _truth_run(tmp_path)
    (run / "changes.diff").write_text(diff)
    assert truth_check.judge(run)["verdict"] == truth_check.VERIFIED


@pytest.mark.parametrize("name,before,after,marker", [
    ("templates/empty.html", None, b"", "Empty file added/deleted: "),
    ("templates/empty.html", b"", None, "Empty file added/deleted: "),
    ("static/site.css", None, b"\xff\xfe", "Binary file changed: "),
    ("static/site.css", b"\xff\xfe", b"\xff\xfd", "Binary file changed: "),
    ("static/site.css", b"\xff\xfe", None, "Binary file changed: "),
])
def test_project_diff_empty_and_binary_ui_changes_require_capture(tmp_path, name, before, after, marker):
    root = tmp_path / "project"
    root.mkdir()
    target = root / name
    target.parent.mkdir(parents=True)
    if before is not None:
        target.write_bytes(before)
    project = Project(root)
    original = project.contents()
    if after is None:
        target.unlink()
    else:
        target.write_bytes(after)
    diff = project.diff(original)
    assert diff == f"{marker}{name}\n"

    run = _truth_run(tmp_path)
    (run / "changes.diff").write_text(diff)
    result = truth_check.judge(run)
    assert result["verdict"] == truth_check.UNVERIFIED
    assert any("UI files changed with no capture evidence" in reason for reason in result["unverified"])
