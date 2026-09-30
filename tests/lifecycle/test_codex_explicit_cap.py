"""Codex review of 357d40a (#42), P2: the exactly-full explicit list must not
ask a terminal goal question. Reproducer kept verbatim, plus the failed-task
boundary and the report wording."""
import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_explicit_tasks import ADD_TASK, README_TASK, _run, _script


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


# Live run 20260930T134226Z-5ce179a1 (Codex on #35): with the ledger on by
# default, the operator's list omitted REQUIREMENTS and COVERS, the first text
# was refused after launch, and the record said t1 "ran" with zero calls.


@pytest.mark.requirements_ledger
def test_a_list_without_requirements_is_refused_whole_before_any_call(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [README_TASK, ADD_TASK], record_complete=False)
    assert replay.calls == [], "nothing was invoked"
    assert replay.result.error.startswith("TaskListInvalid: listed task 1: No requirements are listed yet")
    assert "listed task 2:" in replay.result.error, "every problem at once"
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())["explicit_tasks"]
    assert record["ran"] == [] and record["tasks_closed_clean"] == [] and record["not_run"] == 2
    assert [p["index"] for p in record["invalid"]["all"]] == [1, 2]
    assert replay.result.report.startswith("# Run incomplete")


@pytest.mark.requirements_ledger
def test_a_list_with_requirements_and_covers_runs_under_the_default_ledger(tmp_path, monkeypatch):
    reqs = "REQUIREMENTS:\nR1: README documents add\n"
    replay = _run(tmp_path, monkeypatch, [reqs + README_TASK + "\nCOVERS: R1"], max_tasks=1,
                  script=_script(**{"requirements-review": lambda c, r: "COMPLETE",
                                    "auditor": lambda c, r: "R1: MET - README.md"}))
    assert replay.of("orchestrator") == []
    assert replay.result.completed, replay.result.error
    assert replay.result.report.startswith("# Listed tasks completed; the goal was not judged")


@pytest.mark.requirements_ledger
def test_a_covers_naming_an_unlisted_requirement_is_named_upfront(tmp_path, monkeypatch):
    reqs = "REQUIREMENTS:\nR1: README documents add\n"
    replay = _run(tmp_path, monkeypatch, [reqs + README_TASK + "\nCOVERS: R2"], max_tasks=1,
                  record_complete=False)
    assert replay.calls == []
    assert "COVERS names requirements that do not exist: R2" in replay.result.error
