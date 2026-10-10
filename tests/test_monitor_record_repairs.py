"""The run monitor and the development record, corrected after Codex's review
of the merged #53 (736e94b, Z1-Z6). Each test fails on 736e94b. Synthetic
evidence: every file below is hand-written."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from quadratus.cli import main as cli_main
from quadratus.monitor import read_status, render_markdown, render_text, run_history
from quadratus.monitor_server import render_html

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import dev_record as R  # noqa: E402

RUN = "20261007T120000Z-0000abcd"


def _run_dir(state: Path) -> Path:
    run_dir = state / "runs" / RUN
    run_dir.mkdir(parents=True)
    return run_dir


def _invocation(**extra) -> str:
    record = dict(task="T1", role="lead", canonical_model="claude:opus", invoked=True, outcome="ok",
                  seconds=1.0, input_tokens=4, output_tokens=4)
    record.update(extra)
    return json.dumps(record) + "\n"


# -- 1. listed-task completion shown as goal completion (Z1) -----------------

def _explicit_result(run_dir: Path) -> None:
    # What project_run writes when tasks=[...] and every listed task closed clean.
    (run_dir / "result.json").write_text(json.dumps(dict(
        completed=True, error=None,
        explicit_tasks=dict(listed=1, ran=[dict(task="T1")], tasks_closed_clean=["T1"],
                            goal_judged=False, completed=True))))
    (run_dir / "report.md").write_text("# Listed tasks completed; the goal was not judged\n")


def test_monitor_status_does_not_call_a_listed_task_run_goal_complete(tmp_path):
    _explicit_result(_run_dir(tmp_path / ".quadratus"))
    status = read_status(tmp_path)
    assert status["stop_reason"] != "goal reported complete"
    assert "goal reported complete" not in render_text(status)


def test_monitor_history_does_not_call_a_listed_task_run_goal_complete(tmp_path):
    _explicit_result(_run_dir(tmp_path / ".quadratus"))
    (row,) = run_history(tmp_path)
    assert row["stop_reason"] != "goal reported complete"


# -- 2. path spelling decides which runs are found (Z2) ----------------------

def test_monitor_finds_runs_under_a_tilde_state_dir_as_run_project_does(tmp_path, monkeypatch, capsys):
    """run_project expands ``~`` in state_dir (project_run.py:107); the
    monitor joins it to the project unexpanded (monitor.py:232), so the
    operator's ``--state-dir=~/qstate`` (zsh leaves ``~`` after ``=``) shows
    no runs. The project is given relative, as ``--project .``."""
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    run_dir = _run_dir(Path("~/qstate").expanduser())  # where run_project writes it
    (run_dir / "invocations.jsonl").write_text(_invocation())
    status = read_status(".", state_dir="~/qstate")
    assert status["run_id"] == RUN, status["unknown"].get("run_id")
    assert cli_main(["--monitor", "--project", ".", "--state-dir", "~/qstate"]) == 0
    assert RUN in capsys.readouterr().out


def test_a_relative_project_finds_its_own_run_under_the_default_state(tmp_path, monkeypatch):
    """Z2: the default state was built as ``project/.quadratus`` and, being
    relative, joined to the project again (``project/project/.quadratus``)."""
    _run_dir(tmp_path / "project" / ".quadratus")
    monkeypatch.chdir(tmp_path)
    assert read_status("project")["run_id"] == RUN
    assert [row["run"] for row in run_history("project")] == [RUN]


def test_the_phone_page_says_which_call_task_and_seat_belong_to(tmp_path):
    """Z4: a fresh budget write (a new call reserved) beside a five-minute-old
    invocation record; the page showed the old Task and Seat with no age."""
    run_dir = _run_dir(tmp_path / ".quadratus")
    log = run_dir / "invocations.jsonl"
    log.write_text(_invocation())
    os.utime(log, (1_000_000_000, 1_000_000_000))
    budget = run_dir / "budget.json"
    budget.write_text(json.dumps(dict(reserved_attempts=2, reported_tokens=8)))
    os.utime(budget, (1_000_000_300, 1_000_000_300))
    status = read_status(tmp_path, now=1_000_000_300)
    page = render_html(status, [])
    assert status["last_call_ended"].endswith("(5 min ago)") and status["last_call_ended"] in page
    assert "a call in progress is not written until it returns" in page


# -- 3. one malformed file breaks the read instead of being reported (Z3) ----

def test_one_deeply_nested_invocation_line_is_skipped_not_fatal(tmp_path):
    """_tail_jsonl catches ValueError only (monitor.py:148); json raises
    RecursionError for deep nesting, which escapes to read_status's
    catch-all and blanks liveness and the terminal status of a finished run."""
    run_dir = _run_dir(tmp_path / ".quadratus")
    (run_dir / "invocations.jsonl").write_text(_invocation() + "[" * 100_000 + "]" * 100_000 + "\n")
    (run_dir / "result.json").write_text(json.dumps(dict(completed=False, error="RunStalled: T1 twice")))
    status = read_status(tmp_path)
    assert "monitor error" not in status["unknown"].get("liveness", ""), status["unknown"].get("liveness")
    assert status["terminal_status"] == "error"


def test_a_deeply_nested_result_json_is_reported_as_unreadable(tmp_path):
    """_read_json catches (OSError, ValueError) (monitor.py:169); RecursionError escapes."""
    run_dir = _run_dir(tmp_path / ".quadratus")
    (run_dir / "result.json").write_text("[" * 100_000 + "]" * 100_000)
    (run_dir / "budget.json").write_text(json.dumps(dict(reported_tokens=123, reserved_attempts=1)))
    status = read_status(tmp_path)
    assert "monitor error" not in status["unknown"].get("liveness", ""), status["unknown"].get("liveness")
    assert status["tokens_reported"] == 123
    (row,) = run_history(tmp_path)
    assert row["tokens"] == 123, row


def test_an_out_of_range_token_count_does_not_break_the_rendered_screen(tmp_path):
    """read_status passes the integer through; _tokens_line (monitor.py:727)
    and render_html divide it into a float and raise OverflowError outside
    every guard: the CLI exits with a traceback, the GUI tab stops updating,
    the phone page is a 500."""
    run_dir = _run_dir(tmp_path / ".quadratus")
    (run_dir / "budget.json").write_text(
        '{"reported_tokens": 1' + "0" * 400 + ', "reserved_attempts": 1, '
        '"limits": {"max_reported_tokens": 1000}}')
    status = read_status(tmp_path)
    render_text(status)
    render_markdown(status)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_result_json_that_is_not_a_regular_file_cannot_stall_the_read(tmp_path):
    """_size of a FIFO is 0, under every bound, so _read_json's read_text
    (monitor.py:168) blocks forever: the GUI timer, the CLI and each phone
    refresh hang on it."""
    run_dir = _run_dir(tmp_path / ".quadratus")
    fifo = run_dir / "result.json"
    os.mkfifo(fifo)
    done = threading.Event()
    worker = threading.Thread(target=lambda: (read_status(tmp_path), done.set()), daemon=True)
    worker.start()
    finished = done.wait(3)
    if not finished:  # release the blocked reader so the thread ends
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(3)
    assert finished, "read_status blocked on a FIFO named result.json"


# -- 4. dev_record integrates over an outdated scope or an open blocker (Z5, Z6)

CANDIDATE, DELIVERY, NEW_CANDIDATE = "c" * 40, "e" * 40, "f" * 40


def _record():
    return dict(version=1, candidate=dict(pr=1, sha=CANDIDATE, gate="g", required_receipts=["ci"]),
                tasks=[], receipts=[])


def _delivered(rec, owns=("quadratus/a.py",)):
    R.claim(rec, task_id="T1", purpose="p", base=CANDIDATE, owns=list(owns), author="claude")
    R.deliver(rec, task_id="T1", sha=DELIVERY, exists=lambda s: True)


def _integrate(rec):
    return R.integrate(rec, task_id="T1", sha=DELIVERY, candidate=NEW_CANDIDATE,
                       exists=lambda s: True, contains=lambda c, s: True)


def test_integrate_refuses_a_review_whose_scope_predates_an_extension():
    """extend (dev_record.py:315) widens owns but leaves state 'reviewed';
    integrate (dev_record.py:281) checks only the state."""
    rec = _record()
    _delivered(rec)
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u")
    R.extend(rec, task_id="T1", owns=["quadratus/b.py"], by="claude")
    with pytest.raises(R.RecordError):
        _integrate(rec)


def test_a_cleared_review_does_not_override_an_unresolved_blocker():
    """review (dev_record.py:247) sets 'reviewed' on a cleared verdict even
    when the task is blocked and its blocker was never resolved; integrate
    then accepts it and readiness (dev_record.py:368) stops listing it."""
    rec = _record()
    _delivered(rec)
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="blocked", evidence="u",
             blocker=dict(requirement="r", failure="f", evidence="e", classification="reachable"))
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u")
    assert rec["tasks"][0]["state"] == "blocked"
    with pytest.raises(R.RecordError):
        _integrate(rec)


def test_an_empty_review_scope_is_refused_not_read_as_everything():
    rec = _record()
    _delivered(rec)
    with pytest.raises(R.RecordError):
        R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u", scope=[])


def test_unblock_then_a_full_review_still_integrates():
    rec = _record()
    _delivered(rec)
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="blocked", evidence="u",
             blocker=dict(requirement="r", failure="f", evidence="e", classification="reachable"))
    R.unblock(rec, task_id="T1", resolution="fixed", by="claude")
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u")
    assert _integrate(rec)["state"] == "integrated"
    assert R.readiness(rec)["blockers"] == []


def test_a_scope_extended_after_integration_reopens_the_task_and_ready():
    """Codex review of 178c193: claim, deliver, review and integrate with
    every candidate receipt passed reads ready; a later extend left the task
    integrated on its old review and ready stayed true."""
    rec = _record()
    rec["candidate"]["required_receipts"] = ["ci"]
    _delivered(rec, owns=("app/a.py",))
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u")
    _integrate(rec)
    R.receipt(rec, kind="ci", sha=NEW_CANDIDATE, state="passed")
    assert R.readiness(rec)["ready"] is True
    R.extend(rec, task_id="T1", owns=["app/b.py"], by="claude")
    assert rec["tasks"][0]["state"] == "delivered"
    assert R.readiness(rec)["ready"] is False


def test_ready_rechecks_an_integrated_task_against_its_current_scope():
    """The record, not the state label: an integrated task whose owned scope
    outgrew its cleared review (a hand edit, or an older tool) keeps the
    candidate from reading ready."""
    rec = _record()
    rec["candidate"]["required_receipts"] = ["ci"]
    _delivered(rec, owns=("app/a.py",))
    R.review(rec, task_id="T1", reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u")
    _integrate(rec)
    R.receipt(rec, kind="ci", sha=NEW_CANDIDATE, state="passed")
    rec["tasks"][0]["owns"].append("app/b.py")
    readiness = R.readiness(rec)
    assert readiness["ready"] is False and readiness["uncovered_integrations"][0]["task"] == "T1"


def test_a_reviewed_task_with_a_historical_open_blocker_can_be_unblocked():
    """Records from before Z6 hold reviewed tasks whose blockers were never
    resolved; unblock records the resolution and the task leaves reviewed
    for delivered without any new review being written."""
    rec = _record()
    _delivered(rec)
    rec["tasks"][0]["blockers"].append(dict(requirement="r", failure="f", evidence="e",
                                            classification="reachable", by="codex", sha=DELIVERY, at="t"))
    rec["tasks"][0]["reviews"].append(dict(reviewer="codex", sha=DELIVERY, verdict="cleared", evidence="u",
                                           scope=["quadratus/a.py"], at="t2"))
    rec["tasks"][0]["state"] = "reviewed"
    R.unblock(rec, task_id="T1", resolution="fixed at the delivery, cleared at t2", by="claude")
    task = rec["tasks"][0]
    assert task["state"] == "delivered" and len(task["reviews"]) == 1
    assert task["blockers"][0]["resolved"]["resolution"].startswith("fixed")
    with pytest.raises(R.RecordError):
        R.unblock(rec, task_id="T1", resolution="again", by="claude")
