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


# Live run 20260930T141209Z-48c0083f (Codex on #35): the favicon task closed
# clean on the direct tier with zero orchestrator calls, and the end-of-list
# audit then marked the goal's baseline requirements NOT MET; the audit
# prompt also never carried the design review's APPROVED verdict.

REQS2 = "REQUIREMENTS:\nR1: README documents add\nR2: the CSV parser rejects malformed input\n"


@pytest.mark.requirements_ledger
def test_an_explicit_run_audits_only_what_its_tasks_claimed(tmp_path, monkeypatch):
    prompts = []

    def auditor(call, replay):
        prompts.append(call.prompt)
        return "R1: MET - README.md"
    replay = _run(tmp_path, monkeypatch, [REQS2 + README_TASK + "\nCOVERS: R1"], max_tasks=1,
                  script=_script(**{"requirements-review": lambda c, r: "COMPLETE", "auditor": auditor}))
    assert replay.of("orchestrator") == []
    assert replay.result.completed, replay.result.error
    assert len(prompts) == 1 and "R1: README documents add" in prompts[0] and "R2:" not in prompts[0]
    assert "not claimed by any task in this run" in prompts[0]
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())["explicit_tasks"]
    assert record["requirements_claimed"] == ["R1"] and record["requirements_unclaimed"] == ["R2"]


@pytest.mark.requirements_ledger
def test_a_claimed_requirement_the_audit_finds_unmet_still_ends_incomplete(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS2 + README_TASK + "\nCOVERS: R1"], max_tasks=1,
                  script=_script(**{"requirements-review": lambda c, r: "COMPLETE",
                                    "auditor": lambda c, r: "R1: NOT MET - README.md never names add"}),
                  record_complete=False)
    assert replay.of("orchestrator") == []
    assert not replay.result.completed and "RequirementsUnmet" in replay.result.error, replay.result.error


@pytest.mark.requirements_ledger
def test_the_audit_prompt_carries_the_design_review_verdict(tmp_path, monkeypatch):
    from tests.lifecycle.test_explicit_tasks import FAVICON_TASK
    prompts = []

    def auditor(call, replay):
        prompts.append(call.prompt)
        return "R1: MET - static/favicon.svg"
    reqs = "REQUIREMENTS:\nR1: GET /favicon.ico returns 200 with an SVG icon\n"
    replay = _run(tmp_path, monkeypatch, [reqs + FAVICON_TASK + "\nCOVERS: R1\nTIER: direct"], max_tasks=1,
                  direct_tier=True,
                  script=_script(**{"requirements-review": lambda c, r: "COMPLETE", "auditor": auditor}))
    assert replay.result.completed, replay.result.error
    assert len(prompts) == 1
    assert "independent design review by" in prompts[0] and "APPROVED" in prompts[0], prompts[0][-1500:]


def test_the_progress_line_at_list_end_names_the_list_not_an_orchestrator(tmp_path, monkeypatch):
    """Live run 20261001T001902Z (Codex on #35): progress.log said "the
    orchestrator reports the goal met" on an explicit run with zero
    orchestrator calls."""
    lines = []
    monkeypatch.setattr("quadratus.session.Session._note",
                        lambda self, message: lines.append(message), raising=True)
    replay = _run(tmp_path, monkeypatch, [README_TASK], max_tasks=1)
    assert replay.result.completed, replay.result.error
    assert not any("orchestrator reports the goal met" in line for line in lines), lines
    assert any("task list ran to its end" in line for line in lines), lines
