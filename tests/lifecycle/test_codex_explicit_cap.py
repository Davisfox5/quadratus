"""Codex review of 357d40a (#42), P2: the exactly-full explicit list must not
ask a terminal goal question. Reproducer kept verbatim, plus the failed-task
boundary and the report wording."""
from tests.lifecycle import harness as H
from tests.lifecycle.test_explicit_tasks import README_TASK, _run, _script


def test_exactly_full_list_never_invokes_orchestrator(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [README_TASK], max_tasks=1, record_complete=False)
    assert replay.of('lead'), 'The listed task must have run'
    assert replay.of('orchestrator') == [], replay.result.error
    assert replay.result.completed, replay.result.error


def test_a_failed_task_that_fills_the_cap_stays_incomplete_without_a_call(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"README.md": "# rewritten\n", "app.py": "def add(a, b):\n    return 0\n"})
        return 'Did both.\nCHANGED: ["README.md", "app.py"]'
    replay = _run(tmp_path, monkeypatch, [README_TASK], max_tasks=1, script=_script(lead=lead),
                  record_complete=False)
    assert replay.of('orchestrator') == []
    assert not replay.result.completed and replay.result.error, replay.result.error
    assert replay.result.report.startswith('# Run incomplete')


def test_the_report_says_the_goal_was_not_judged(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [README_TASK], max_tasks=1)
    assert replay.result.report.startswith('# Listed tasks completed; the goal was not judged')
    assert 'Explicit task list: 1 of 1 listed task(s) ran (t1)' in replay.result.report
