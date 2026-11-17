"""Regressions for the 2026-11-10 whole-repo review: the last wiring step.

The contest feature, the baseline arm and the engine rulings were merged with
notes for the command line, the GUI and a few engine points. Each test here
holds one piece of that wiring: the value reaches the shared runner, a line
or a form that leaves it alone makes the call it made before, a flag given
where it would be ignored is refused by name, and nothing here lets a run
read as done when it is not.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import baseline as solo_run
from quadratus import (
    child_env,
    cli,
    contest,
    gui,
    monitor,
    monitor_story,
    policy,
    project_run,
    project_watch,
    readiness,
)
from quadratus.config import Settings
from quadratus.contest import JUDGE_ROLE
from quadratus.integration import IntegrationGate, run_original_tests
from quadratus.project import Project
from quadratus.providers import ProviderError, ProviderRefusal
from quadratus.run_budget import RunLimits
from quadratus.session import CapabilityUnavailable, Session, _never_replaced, _stops_the_run
from tests import test_review1110_baseline as solo_arm
from tests import test_review1110_compete as arena
from tools import compare_runs, truth_check

# -- 4. the monitor names the judge's call ----------------------------------------------------------


def test_the_monitor_reads_the_judges_call_as_the_draft_stage(tmp_path):
    assert monitor.ROLE_STAGES[JUDGE_ROLE] == "draft"
    assert JUDGE_ROLE in monitor_story.ROLE_LABELS
    run_dir = tmp_path / ".quadratus" / "runs" / "20261111T000000Z-abcdef12"
    run_dir.mkdir(parents=True)
    (run_dir / "invocations.jsonl").write_text(json.dumps(dict(
        task="t1", role=JUDGE_ROLE, canonical_model="grok:default", invoked=True)) + "\n")
    status = monitor.read_status(tmp_path)
    assert (status["role"], status["stage"]) == (JUDGE_ROLE, "draft")
    assert "stage" not in status["unknown"], "the judge's call is no longer an unknown stage"


# -- 8. the comparison says whether a run changed any source ----------------------------------------


def _run_folder(tmp_path, name, record):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "result.json").write_text(json.dumps(record))
    return folder


def test_compare_runs_prints_whether_each_run_changed_any_source(tmp_path):
    passed = dict(passed=True, source="s")
    changed = _run_folder(tmp_path, "changed", dict(completed=True, checks=[passed], source_changed=True))
    untouched = _run_folder(tmp_path, "untouched", dict(completed=True, checks=[passed], source_changed=False))
    silent = _run_folder(tmp_path, "silent", dict(completed=True, checks=[passed]))
    words = _run_folder(tmp_path, "words", dict(completed=True, checks=[passed], source_changed="yes"))
    rows = {folder.name: compare_runs.summarize(folder) for folder in (changed, untouched, silent, words)}
    assert rows["changed"]["source_changed"] == "yes" and rows["untouched"]["source_changed"] == "no"
    assert rows["silent"]["source_changed"] == "unknown", "a record that does not say is unknown, never no"
    assert rows["words"]["source_changed"] == "unknown", "only the record's own true or false is read"
    assert compare_runs.summarize(tmp_path / "missing")["source_changed"] == "unknown"
    out = compare_runs.compare([changed, untouched, silent])
    text = compare_runs.render(out)
    # A passing gate on a run that changed nothing reads beside the fact that it changed nothing.
    assert "  source changed: no\n  final gate: passed" in text
    assert "  source changed: yes\n" in text and "  source changed: unknown\n" in text
    assert "  source changed: differs (no against yes)" in text
    assert "  source changed: unknown (unknown against yes)" in text
    fields = out["differences"][0]["fields"]["source_changed"]
    assert fields == dict(first="yes", other="no", same=False)


# -- 9. the truth check answers for a record whose checks are not check records ---------------------


@pytest.mark.parametrize("checks, why", [
    ("x", "not a list (found str)"),
    ({"passed": True}, "not a list (found dict)"),
    (7, "not a list (found int)"),
    (["passed"], "1 of the record's 1 check(s) are not check records"),
    ([{"passed": True, "source": "s"}, None], "1 of the record's 2 check(s) are not check records"),
])
def test_the_truth_check_says_unverified_and_why_for_checks_that_are_not_check_records(tmp_path, checks, why):
    record = dict(completed=True, source_fingerprint="s", checks=checks,
                  requirements=dict(listed={"R1": "a"}, status={"R1": "met"},
                                    audits=[dict(verdicts={"R1": dict(met=True)})]))
    folder = _run_folder(tmp_path, "run", record)
    (folder / "changes.diff").write_text("")
    judged = truth_check.judge(folder)
    assert judged["verdict"] == truth_check.UNVERIFIED and judged["not_met"] == []
    assert any(why in reason for reason in judged["unverified"]), judged["unverified"]
    assert "no check ran" not in judged["unverified"], "the record held something; it was not read"
    assert truth_check.main([str(folder)]) == truth_check.EXIT[truth_check.UNVERIFIED]
    # The comparison now has that verdict where it had none.
    assert compare_runs.summarize(folder)["truth_check"] == truth_check.UNVERIFIED


def test_what_the_record_shows_as_failed_still_counts_beside_misshapen_checks(tmp_path):
    folder = _run_folder(tmp_path, "run", dict(completed=False, error="Boom: stopped", checks="x"))
    judged = truth_check.judge(folder)
    assert judged["verdict"] == truth_check.NOT_MET
    assert any("Boom: stopped" in reason for reason in judged["not_met"])
    assert any("not a list" in reason for reason in judged["unverified"])


@pytest.mark.parametrize("damage", [
    dict(workflow=7), dict(workflow=dict(tasks="t")), dict(workflow=dict(tasks=[7, dict(checks="c")])),
    dict(workflow=dict(tasks=[dict(checks=["not a check"])])),
    dict(requirements=[1]), dict(findings=["open"]), dict(design_checks=[3]), dict(original_test_runs=[None]),
    dict(unexecuted_acceptance=["x"]), dict(test_repairs=[0]),
])
def test_the_truth_check_never_raises_on_a_record_that_is_not_shaped_as_one(tmp_path, damage):
    check = dict(passed=True, source="s", receipts=[dict(id="tests", required=True, status="passed", tests=1)])
    record = dict(completed=True, checks=[check], source_fingerprint="s",
                  requirements=dict(listed={"R1": "a"}, status={"R1": "met"},
                                    audits=[dict(verdicts={"R1": dict(met=True)})]),
                  workflow=dict(tasks=[dict(checks=[check])]))
    folder = _run_folder(tmp_path, "run", record)
    (folder / "changes.diff").write_text("+++ b/app.py\n")
    assert truth_check.judge(folder)["verdict"] == truth_check.VERIFIED, "the canary reads verified whole"
    (folder / "result.json").write_text(json.dumps(dict(record, **damage)))
    judged = truth_check.judge(folder)
    assert judged["verdict"] == truth_check.UNVERIFIED and judged["unverified"], "never verified, never a crash"


def test_the_truth_check_still_imports_nothing_from_the_engine():
    source = Path(truth_check.__file__).read_text()
    assert "import quadratus" not in source and "from quadratus" not in source


# -- 5. a contest is no way around the routed lead's refusal or denied capability -------------------


OPUS, SOL, GROK = arena.OPUS, arena.SOL, arena.GROK
ROUTED_STOPS = [ProviderRefusal("declined by policy"),
                CapabilityUnavailable("denied a command the harness requires")]


def _contest_run(tmp_path, drafts, **config):
    script = arena.Script([arena._task(), "DONE"], dict({OPUS: arena._sub(), SOL: arena._sub(),
                                                         GROK: arena._sub()}, **drafts))
    session, project = arena._session(tmp_path, script, **config)
    return script, session, project


@pytest.mark.parametrize("stop", ROUTED_STOPS, ids=lambda stop: type(stop).__name__)
def test_the_routed_leads_refusal_or_denied_capability_stops_the_run_whatever_the_challenger_delivered(
        tmp_path, stop):
    """Before the ruling a refused routed lead lost on completion and the
    challenger's draft was merged: contests on was a way around a refusal."""
    script, session, project = _contest_run(tmp_path / "contest", {OPUS: arena._raises(stop)}, compete="complex")
    with pytest.raises(type(stop), match=str(stop)) as contested:
        session.run(max_tasks=3)
    record = arena._held(session)
    drafts = arena._by_role(record)
    assert record["winner"] is None and record["winner_model"] is None
    assert record["decided_by"] == contest.ROUTED_STOP and record["merged"] == []
    assert record["stopped"] == f"{type(stop).__name__}: {stop}" == drafts["routed"]["error"]
    # The challenger delivered a usable draft, and none of it reached the project.
    assert drafts["challenger"]["usable"] and drafts["challenger"]["changed"] == ["calc.py"]
    assert (project / "calc.py").read_text() == arena.START, "no draft stands in for the routed lead's"
    assert "def sub" in session.store.get(drafts["challenger"]["diff_artifact"]), "it is kept, as a record"
    assert record["judge"] is None and not script.of("judge")
    assert not script.of("review") and not script.of("closeout"), "no further model work after the stop"
    assert not session.completed
    outcome = session.task_outcomes[-1]
    assert outcome.closed_as == f"stopped:{type(stop).__name__}"
    assert not any(fact.recovered for fact in outcome.facts), "the stop is not recorded as recovered"
    assert not outcome.owner_changes, "the task never changed hands"

    # The same stop a single lead's draft call ends the run on, with the same project.
    script, alone, lone_project = _contest_run(tmp_path / "alone", {OPUS: arena._raises(stop)})
    with pytest.raises(type(stop), match=str(stop)) as single:
        alone.run(max_tasks=3)
    assert type(contested.value) is type(single.value)
    assert (lone_project / "calc.py").read_text() == arena.START and not alone.contests
    assert alone.task_outcomes[-1].closed_as == outcome.closed_as


def test_a_challengers_refusal_only_loses_the_contest(tmp_path):
    stop = ProviderRefusal("declined by policy")
    script, session, project = _contest_run(tmp_path, {SOL: arena._raises(stop)}, compete="complex")
    session.run(max_tasks=3)
    record = arena._held(session)
    assert record["decided_by"] == "completed" and record["winner_model"] == OPUS and record["stopped"] == ""
    assert "def sub" in (project / "calc.py").read_text() and session.completed


def test_a_routed_lead_that_failed_in_an_ordinary_way_is_still_replaced(tmp_path):
    for name, act in (("down", arena._raises(ProviderError("entrant is down"))), ("capped", arena._capped)):
        script, session, project = _contest_run(tmp_path / name, {OPUS: act}, compete="complex")
        session.run(max_tasks=3)
        record = arena._held(session)
        assert record["decided_by"] == "completed" and record["winner_model"] == SOL, name
        assert "def sub" in (project / "calc.py").read_text() and session.completed, name


def test_the_rule_is_the_routed_leads_alone_and_names_both_stops():
    assert _never_replaced(ProviderRefusal("no")) and _never_replaced(CapabilityUnavailable("denied"))
    for ordinary in (ProviderError("down"), arena.TurnLimitReached("cap"), RuntimeError("x"), None):
        assert not _never_replaced(ordinary), ordinary
    # The one rule for a batch child, an entrant and the judge is unchanged.
    assert not _stops_the_run(ProviderRefusal("no")) and _stops_the_run(CapabilityUnavailable("denied"))
    assert "_never_replaced(held)" in inspect.getsource(Session._contest_draft)


def test_the_record_and_the_scoreboard_say_the_routed_lead_stopped_the_run(tmp_path, capsys):
    from tools import scoreboard as tool
    assert tool.ROUTED_STOP == contest.ROUTED_STOP and contest.ROUTED_STOP not in contest.RULES
    record = arena._contest_record(task="t9", winner=None, winner_model=None, decided_by=contest.ROUTED_STOP)
    line = contest.ledger_line(record)
    assert "stops the run as it does with one lead" in line and "no draft was merged" in line
    assert "neither was usable" not in line
    unusable = contest.ledger_line(arena._contest_record(winner=None, winner_model=None, decided_by="none usable"))
    assert "neither was usable" in unusable
    board = tmp_path / "scoreboard.jsonl"
    contest.append_scoreboard(board, contest.scoreboard_rows("run-1", [record], []))
    found, _ = tool.read([board])
    summary = tool.summarise(found)
    for model in summary["models"].values():
        assert (model["contests"], model["won"], model["lost"], model["no_winner"], model["routed_stop"]) == (
            1, 0, 0, 0, 1)
    assert tool.main([str(board)]) == 0
    shown = capsys.readouterr().out
    assert "1 ended by the routed lead's refusal or denied capability" in shown
    assert "with no usable draft" not in shown


# -- 11. a baseline makes the preflights a project run makes, by the same helpers -------------------


#: The baseline tests' fixture: a small project, a fake vendor CLI for every
#: vendor, and a trace of the calls it received. No real CLI can be reached.
env = solo_arm.env


def _plant_hook_in_the_fake_call(trace):
    """Make the fake vendor's editing call also write a git hook: a path
    outside the measured source that no diff or review copy shows."""
    binary = Path(trace.parent / "fake-vendor")
    binary.write_text(binary.read_text().replace(
        "changed = ['add.py']",
        "changed = ['add.py']\nPath('.git/hooks').mkdir(parents=True, exist_ok=True)\n"
        "Path('.git/hooks/pre-commit').write_text('#!/bin/sh\\nexit 0\\n')"))


def test_a_watched_path_a_baseline_left_refuses_the_next_run_of_either_kind(env, monkeypatch):
    project, settings, trace = env
    monkeypatch.delenv("QUADRATUS_ACCEPT_WATCHED_PATHS", raising=False)
    _plant_hook_in_the_fake_call(trace)
    first = solo_arm.run(env)
    data = solo_arm.record(first)
    (path,) = data["project_watch"]["paths"]
    assert (path["path"], path["kind"], path["what"]) == (".git/hooks/pre-commit", "git", "added")
    assert "Watched paths changed" in first.report and "--accept-watched-paths" in first.report
    assert data["checks"] == [], "no check is accepted over a tree the watch flagged"
    made = len(solo_arm.calls(trace))
    runs = sorted((project.root / ".quadratus" / "runs").iterdir())

    # The next baseline is refused before any call, with the way out named.
    with pytest.raises(ValueError) as refused:
        solo_arm.run(env)
    text = str(refused.value)
    assert ".git/hooks/pre-commit [git] (added)" in text and first.run_dir.name in text
    assert "--accept-watched-paths" in text and "QUADRATUS_ACCEPT_WATCHED_PATHS=1" in text
    assert "no model was called" in text
    # So is the next project run: it reads the baseline's record.
    ran = []
    monkeypatch.setattr(Session, "run", lambda self, **kw: ran.append(1))
    with pytest.raises(ValueError, match="pre-commit"):
        project_run.run_project("goal", project, settings, allow_writes=True)
    assert len(solo_arm.calls(trace)) == made and ran == []
    assert sorted((project.root / ".quadratus" / "runs").iterdir()) == runs, "nothing ran"

    # Accepted by the operator, each starts.
    lines = []
    again = solo_arm.run(env, accept_watched_paths=True, progress=lines.append)
    assert len(solo_arm.calls(trace)) == made + 1 and again.run_dir != first.run_dir
    assert any("accepted by the operator for this run" in line for line in lines)


def test_a_baseline_starts_once_the_planted_path_is_gone_or_the_environment_accepts(env, monkeypatch):
    project, _, trace = env
    monkeypatch.delenv("QUADRATUS_ACCEPT_WATCHED_PATHS", raising=False)
    _plant_hook_in_the_fake_call(trace)
    solo_arm.run(env)
    with pytest.raises(ValueError, match="pre-commit"):
        solo_arm.run(env)
    monkeypatch.setenv("QUADRATUS_ACCEPT_WATCHED_PATHS", "1")
    made = len(solo_arm.calls(trace))
    solo_arm.run(env)
    assert len(solo_arm.calls(trace)) == made + 1


def test_a_baseline_refuses_a_scratch_root_inside_the_project_before_anything_is_made(env, monkeypatch):
    project, _, trace = env
    inside = project.root / "scratch"
    inside.mkdir(mode=0o700)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(inside))
    with pytest.raises(ValueError, match="Nothing was run and no model was called"):
        solo_arm.run(env)
    assert not trace.exists() and not (project.root / ".quadratus" / "runs").exists()


def test_a_baseline_sweeps_stale_scratch_copies_as_a_project_run_does(env, tmp_path, monkeypatch):
    from quadratus.providers import scratch_root
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(scratch))
    # Made by the harness, so marked as its own to sweep, and named exactly as
    # it names a copy: a folder the test made, or a looser name, is no longer
    # swept (final review, trust_transport 6; tests/test_review1110_fix_b.py).
    assert scratch_root() == str(scratch)
    stale, fresh = scratch / "quadratus-review-old00000", scratch / "quadratus-review-new00000"
    stale.mkdir()
    fresh.mkdir()
    old = time.time() - 2 * 86400
    os.utime(stale, (old, old))
    lines = []
    solo_arm.run(env, progress=lines.append)
    assert not stale.exists() and fresh.is_dir()
    assert any("Removed 1 leftover scratch folder(s) or file(s)" in line for line in lines)


def test_the_preflights_are_shared_code_not_a_second_copy():
    for helper in ("_refuse_scratch_in_project", "_refuse_standing_watch_findings", "_sweep_stale_scratch",
                   "_refuse_agent_config", "_watch_record", "_watch_report_lines"):
        assert getattr(solo_run, helper) is getattr(project_run, helper), helper
        assert f"{helper}(" in inspect.getsource(solo_run), helper
    source = inspect.getsource(solo_run)
    assert "scratch_inside" not in source and "sweep_scratch(" not in source, "no copy of the rule"
    runner = inspect.getsource(project_run.run_project)
    assert "_refuse_scratch_in_project(project)" in runner and "_sweep_stale_scratch(progress)" in runner
    assert "accept_watched_paths" in inspect.signature(solo_run.run_baseline).parameters
    assert inspect.signature(solo_run.run_baseline).parameters["accept_watched_paths"].default is False


# -- 6. an interrupt during the transcript copy goes on after a baseline's record -------------------


def _interrupt_the_transcript_copy(monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("quadratus.trace.build_traces", interrupted)


def test_a_baseline_writes_its_record_and_then_passes_the_interrupt_on(env, monkeypatch):
    project, _, trace = env
    _interrupt_the_transcript_copy(monkeypatch)
    with pytest.raises(KeyboardInterrupt) as raised:
        solo_arm.run(env)
    assert isinstance(raised.value, project_run.RunInterrupted)
    result = raised.value.result
    (run_dir,) = (project.root / ".quadratus" / "runs").iterdir()
    assert result.run_dir == run_dir and result.completed is False
    data = json.loads((run_dir / "result.json").read_text())
    assert data["trace"]["interrupted"] is True and data["mode"] == "baseline"
    assert (run_dir / "report.md").exists() and (run_dir / "changes.diff").exists()
    assert not [name for name in os.listdir(run_dir) if name.endswith(".tmp")]
    assert len(solo_arm.calls(trace)) == 1


def test_a_project_runs_interrupt_carries_the_finished_result(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    _interrupt_the_transcript_copy(monkeypatch)
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    with pytest.raises(KeyboardInterrupt) as raised:
        project_run.run_project("goal", root, Settings(backend="cli"))
    assert isinstance(raised.value, project_run.RunInterrupted)
    (run_dir,) = (root / ".quadratus" / "runs").iterdir()
    assert raised.value.result.run_dir == run_dir and (run_dir / "result.json").exists()


def test_the_solo_arm_stops_on_an_interrupt_a_baseline_passed_on(tmp_path):
    P = solo_arm.P
    _, out = solo_arm._series(tmp_path)
    launched = []

    def interrupted(cell, packet):
        launched.append(cell["name"])
        kept = Path(cell["project"]) / ".quadratus" / "runs" / "r1"
        raise project_run.RunInterrupted(project_run.ProjectResult(False, "report", kept, "", ""))

    with pytest.raises(KeyboardInterrupt):
        P.solo(out, model="claude:opus", launcher=interrupted, engine=solo_arm.ENGINE)
    assert len(launched) == 1, "Ctrl-C must not start the next cell"
    record = json.loads((out / P.SOLO_RECORD).read_text())
    (cell,) = record["cells"]
    assert cell["state"] == "interrupted" and cell["error"].startswith(P.STOP_INTERRUPTED)
    assert cell["run_dir"].endswith("/.quadratus/runs/r1"), "the record that was written is named"
    assert record["stopped"]["reason"].startswith(P.STOP_INTERRUPTED) and record["stopped"]["after"] == cell["name"]
    # A later launch does not go on to the next cell, or rerun this one, without an override.
    with pytest.raises(RuntimeError, match="the solo arm stopped"):
        P.solo(out, model="claude:opus", launcher=interrupted, engine=solo_arm.ENGINE)
    assert len(launched) == 1


# -- 7. the record says the operator declined the project's declared checks -------------------------


def _declaring_project(tmp_path):
    root = tmp_path / "declaring"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    return root


def _quiet_project_run(monkeypatch, root, **options):
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    said = []
    result = project_run.run_project("goal", root, Settings(backend="cli"), progress=said.append, **options)
    return result, json.loads((result.run_dir / "result.json").read_text()), said


def test_the_gate_plan_and_the_record_name_the_declined_declared_checks(tmp_path, monkeypatch):
    root = _declaring_project(tmp_path)
    declared = [list(c) for c in project_run.scan_repo(root).declared_checks]
    assert declared and declared[0][1:3] == ["-m", "pytest"]
    result, record, said = _quiet_project_run(monkeypatch, root, declared_checks=False)
    plan = json.loads((result.run_dir / "gate-plan.json").read_text())
    (choice,) = plan
    assert choice["id"] == project_run.DECLINED_CHECKS_ID and choice["argv"] == [] and choice["required"] is False
    assert choice["declined"] is True and choice["by"] == "operator" and choice["commands"] == declared
    assert "--no-declared-checks" in choice["note"]
    assert record["declared_checks"] == {k: choice[k] for k in ("declined", "by", "commands", "note")}
    assert record["checks"] == [] and record["completed"] is False, "naming the choice is never a check"
    assert "Declared checks: declined by the operator (--no-declared-checks)." in result.report
    assert any("declared checks were declined by the operator" in line for line in said)
    # The policy plan says why its test gate is absent, in the run's record and in the preview.
    (absent,) = record["policy_preview"]["absent_gates"]
    assert absent["id"] == "unit-tests" and absent["because"] == policy.DECLINED_TEST_COMMAND
    assert "No test command detected" not in json.dumps(record["policy_preview"])
    preview = policy.preview_policy(root, declared_checks=False)
    assert preview["hash"] == record["policy_preview"]["hash"], "the preview is still that run's plan"
    shown = policy.render_preview(preview)
    assert "Not configured: unit-tests (The project declares a test command; the operator declined" in shown
    assert "No test command detected" not in shown


def test_the_operators_own_check_stands_before_the_entry_that_names_the_choice(tmp_path, monkeypatch):
    root = _declaring_project(tmp_path)
    result, record, _ = _quiet_project_run(monkeypatch, root, declared_checks=False, check="true")
    plan = json.loads((result.run_dir / "gate-plan.json").read_text())
    assert [(g["id"], g["argv"]) for g in plan] == [("check", ["true"]), (project_run.DECLINED_CHECKS_ID, [])]
    assert record["declared_checks"]["declined"] is True


def test_a_run_that_declined_nothing_has_the_gate_plan_and_policy_plan_it_always_had(tmp_path, monkeypatch):
    root = _declaring_project(tmp_path)
    result, record, said = _quiet_project_run(monkeypatch, root)
    plan = json.loads((result.run_dir / "gate-plan.json").read_text())
    assert [g["id"] for g in plan] == ["check"] and not any("declined" in g for g in plan)
    assert record["declared_checks"] == {"declined": False}
    assert "Declared checks:" not in result.report
    assert not any("declared checks were declined" in line for line in said)
    assert policy.preview_policy(root) == record["policy_preview"]


def test_a_project_that_declares_nothing_still_says_none_was_detected(tmp_path, monkeypatch):
    root = tmp_path / "bare"
    root.mkdir()
    (root / "notes.txt").write_text("nothing to test\n")
    for declared in (True, False):
        (absent,) = policy.preview_policy(root, declared_checks=declared)["absent_gates"]
        assert absent["because"] == policy.NO_TEST_COMMAND, "declining nothing is not a declined command"
    result, record, _ = _quiet_project_run(monkeypatch, root, declared_checks=False)
    assert record["declared_checks"]["declined"] is True and record["declared_checks"]["commands"] == []
    assert "the project declared none that was read" in record["declared_checks"]["note"]


def test_the_run_the_baseline_and_the_preview_decline_by_one_rule(env):
    for caller in (project_run.run_project, solo_run.run_baseline, policy.preview_policy):
        source = inspect.getsource(caller)
        assert "'declared_checks': False" in source and "check_command=None" not in source, caller.__name__
    assert "without_declared_checks(scan)" in inspect.getsource(policy.load_policy)
    assert "without_declared_checks(scan)" in inspect.getsource(project_run._decline_declared_checks)
    # A baseline's record names the choice in the same shape.
    result = solo_arm.run(env, check="", declared_checks=False)
    data = solo_arm.record(result)
    assert data["declared_checks"]["declined"] is True and data["declared_checks"]["by"] == "operator"
    plan = json.loads((result.run_dir / "gate-plan.json").read_text())
    assert [g["id"] for g in plan] == [project_run.DECLINED_CHECKS_ID]
    assert "Declared checks: declined by the operator" in result.report
    assert solo_arm.record(solo_arm.run(env))["declared_checks"] == {"declined": False}


# -- 1. --baseline MODEL and --fix-rounds N on the command line -------------------------------------


GOAL = "Fix the bug"
DIGEST = "ab" * 32
#: A PATH with no vendor CLI on it, for the one test that runs a whole baseline.
NO_VENDOR_PATH = "/usr/local/bin:/usr/bin:/bin"


@pytest.fixture(autouse=True)
def _no_standing_settings(monkeypatch):
    """The settings of whoever runs the suite must not decide these tests."""
    for name in (policy.TRUST_ENV, project_watch.ALLOW_AGENT_CONFIG_ENV, project_watch.ACCEPT_WATCHED_PATHS_ENV):
        monkeypatch.delenv(name, raising=False)


def _record_dir(tmp_path, gate=None, name="run"):
    """A run folder holding the part of a baseline's record the exit status reads."""
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    if gate is not None:
        (folder / "result.json").write_text(json.dumps(dict(mode="baseline", baseline=dict(gate=gate))))
    return folder


@pytest.fixture
def solo_runner(monkeypatch, tmp_path):
    """Stand in for ``run_baseline`` and record what the command line sent it."""
    seen = {}
    folder = _record_dir(tmp_path, dict(configured=True, passed=True))

    def run(goal, project, settings, **kwargs):
        seen.update(kwargs, goal=goal, project=project, settings=settings)
        return SimpleNamespace(completed=False, report="# Baseline run: the goal was not judged",
                               run_dir=folder, error="")

    monkeypatch.setattr(solo_run, "run_baseline", run)
    monkeypatch.setattr(project_run, "run_project",
                        lambda *a, **k: pytest.fail("a baseline line must not start a project run"))
    return seen


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """Stand in for the shared runner and record what the command line sent it."""
    seen = {}

    def run(goal, project, settings, **kwargs):
        seen.update(kwargs, goal=goal, project=project, settings=settings)
        return SimpleNamespace(completed=True, report="checked", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    return seen


def _refused(argv, capsys, monkeypatch):
    """The argparse refusal for ``argv``: exit 2, nothing configured or started."""
    monkeypatch.setattr(cli, "_build_settings",
                        lambda *a: pytest.fail("refused lines must not reach settings or providers"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    return capsys.readouterr().err


def _flat_help(monkeypatch):
    monkeypatch.setenv("COLUMNS", "100000")
    return " ".join(cli.build_parser().format_help().split())


def test_a_baseline_line_reaches_run_baseline_with_every_flag_it_reads(tmp_path, solo_runner, capsys):
    out = tmp_path / "report.md"
    line = [GOAL, "--project", str(tmp_path / "p"), "--baseline", "claude:opus", "--fix-rounds", "3",
            "--check", "pytest -q", "--extra-check", "node --test a.test.js", "--readiness", "probes.json",
            "--path", "src/app.py", "--forbid", "secret/**", "--state-dir", str(tmp_path / "state"),
            "--max-calls", "9", "--max-reported-tokens", "900000", "--wall-seconds", "600",
            "--reserve-tokens-per-call", "1000", "--max-tokens-per-call", "500000", "--lead-max-turns", "7",
            "--trust-project-policy", DIGEST, "--allow-project-agent-config", "--no-declared-checks",
            "--accept-watched-paths", "-o", str(out)]
    assert cli.main(line) == 0
    seen = solo_runner
    assert seen["goal"] == GOAL and seen["model"] == "claude:opus" and seen["fix_rounds"] == 3
    assert seen["check"] == "pytest -q" and list(seen["extra_checks"]) == ["node --test a.test.js"]
    assert seen["readiness"] == "probes.json" and seen["state_dir"] == str(tmp_path / "state")
    assert seen["declared_paths"] == ["src/app.py"] and seen["forbid"] == ["secret/**"]
    assert seen["run_limits"] == RunLimits(max_calls=9, max_reported_tokens=900000, wall_seconds=600,
                                           reserve_tokens_per_call=1000, max_tokens_per_call=500000)
    assert seen["settings"].lead_max_turns == 7
    assert seen["trust_project_policy"] == DIGEST and seen["allow_project_agent_config"] is True
    assert seen["declared_checks"] is False and seen["accept_watched_paths"] is True
    assert seen["project"].root == (tmp_path / "p").resolve()
    printed = capsys.readouterr().out
    assert "the goal was not judged" in printed and out.read_text().startswith("# Baseline run")
    # Every keyword it was sent is one run_baseline takes.
    taken = set(inspect.signature(solo_arm.run_baseline).parameters)
    assert set(seen) - {"goal", "project", "settings"} <= taken


def test_a_bare_baseline_line_sends_only_the_model_and_the_defaults(tmp_path, solo_runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--baseline", "claude:opus"]) == 0
    assert solo_runner["model"] == "claude:opus" and solo_runner["fix_rounds"] == 1
    assert solo_runner["check"] == "" and list(solo_runner["extra_checks"]) == []
    sent = set(solo_runner) - {"goal", "project", "settings", "progress"}
    assert sent == {"model", "check", "extra_checks", "readiness", "state_dir", "forbid", "declared_paths",
                    "fix_rounds"}, "no limit and no trust choice is passed unless it was given"
    assert cli.main([GOAL, "--project", str(tmp_path), "--baseline", "claude:opus", "--fix-rounds", "0"]) == 0
    assert solo_runner["fix_rounds"] == 0


MUST_REFUSE = [["--session"], ["--tasks", "tasks.json"], ["--mode", "collaborative"], ["--plan-gate"],
               ["--decider", "jev"], ["--capture-profile", "profile.json"], ["--survey-recovery", "2"],
               ["--direct-tier"], ["--repair-test", "tests/test_a.py"], ["--test-changes", "changes.json"],
               ["--compete", "complex"]]


@pytest.mark.parametrize("flag", MUST_REFUSE, ids=lambda flag: flag[0])
def test_a_flag_a_baseline_would_ignore_is_refused_by_name(flag, tmp_path, capsys, monkeypatch, solo_runner):
    error = _refused([GOAL, "--project", str(tmp_path), "--baseline", "claude:opus", "--allow-writes", *flag],
                     capsys, monkeypatch)
    assert "--baseline runs one model alone" in error and f"does not use {flag[0]}" in error
    assert not solo_runner, "refused before anything is opened or called"


def _sample(action):
    """One well-formed spelling of ``action`` on a command line."""
    name = cli._flag_name(action)
    if action.nargs == 0:
        return [name]
    if action.choices:
        return [name, sorted(action.choices)[0]]
    if action.dest == "trust_project_policy":
        return [name, DIGEST]
    return [name, "2"]


def test_every_flag_is_either_read_by_a_baseline_or_refused_with_it(tmp_path):
    """A flag added later is refused with --baseline until it is listed, so
    none can be accepted there and then ignored."""
    parser = cli.build_parser()
    dests = {action.dest for action in parser.flags}
    assert cli._BASELINE_READS <= dests, "every listed name is a real flag"
    refused_by_name = {flag[0] for flag in MUST_REFUSE}
    for action in parser.flags:
        if action.dest in ("baseline", "project") or action.needs == "monitor" or action.dest == "monitor":
            continue
        args = parser.parse_args([GOAL, "--project", str(tmp_path), "--baseline", "claude:opus",
                                  *_sample(action)])
        problem = cli._misplaced(parser, args)
        name = cli._flag_name(action)
        if action.needs == "pipeline":
            assert name in problem, name
        elif action.dest in cli._BASELINE_READS:
            assert problem == "", f"{name}: {problem}"
            assert name not in refused_by_name
        else:
            assert f"does not use {name}" in problem, name
    for name in refused_by_name:
        assert name in {n for a in parser.flags for n in a.option_strings}


def test_fix_rounds_needs_a_baseline_and_a_whole_number(tmp_path, capsys, monkeypatch, solo_runner):
    error = _refused([GOAL, "--project", str(tmp_path), "--fix-rounds", "2"], capsys, monkeypatch)
    assert "--fix-rounds requires --baseline" in error
    assert "--baseline require --project" in _refused([GOAL, "--baseline", "claude:opus"], capsys, monkeypatch)
    for bad in ("-1", "1.5", "two"):
        error = _refused([GOAL, "--project", str(tmp_path), "--baseline", "claude:opus", "--fix-rounds", bad],
                         capsys, monkeypatch)
        assert "--fix-rounds" in error and "whole number of at least 0" in error
    error = _refused(["--monitor", "--project", str(tmp_path), "--baseline", "claude:opus"], capsys, monkeypatch)
    assert "--monitor reads a run and starts nothing" in error and "--baseline" in error
    assert not solo_runner


@pytest.mark.parametrize("gate, error, status, said", [
    (dict(configured=True, passed=True), "", 0, "its last check passed. The goal was not judged."),
    (dict(configured=True, passed=False), "", 2, "last check failed. The goal was not judged."),
    (dict(configured=False, passed=None), "", 2, "No check ran, so nothing was test-verified."),
    (dict(configured=True, passed=None), "", 2, "No check result was recorded."),
    (dict(configured=True, passed=True), "ProviderRefusal: declined", 1, "The baseline stopped: ProviderRefusal"),
    (None, "", 2, "record could not be read"),
])
def test_a_baseline_never_exits_as_complete(tmp_path, monkeypatch, capsys, gate, error, status, said):
    folder = _record_dir(tmp_path, gate, name="judged")

    def run(goal, project, settings, **kwargs):
        return SimpleNamespace(completed=False, report="# Baseline run: claude:opus alone; the goal was not judged",
                               run_dir=folder, error=error)

    monkeypatch.setattr(solo_run, "run_baseline", run)
    assert cli.main([GOAL, "--project", str(tmp_path), "--baseline", "claude:opus"]) == status
    printed = capsys.readouterr().out
    assert said in printed
    shown = "\n".join(line for line in printed.splitlines() if str(tmp_path) not in line)
    assert "complete" not in shown.lower(), "a baseline is never called complete, whatever its check said"


def test_the_exit_status_reads_only_a_true_or_false_gate(tmp_path):
    for shape in ({"baseline": {"gate": {"passed": "yes"}}}, {"baseline": {"gate": []}}, {"baseline": 7}, []):
        folder = tmp_path / f"r{len(list(tmp_path.iterdir()))}"
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(shape))
        status, _ = cli._baseline_status(SimpleNamespace(error="", run_dir=folder))
        assert status == 2, shape


@pytest.mark.parametrize("refusal", [ValueError("Baseline model 'x' is not in the active roster."),
                                     policy.PolicyUntrusted("the policy file asks for more"),
                                     ProviderError("Another Quadratus run is using this project.")])
def test_a_baseline_preflight_refusal_prints_as_the_other_refusals_do(tmp_path, monkeypatch, capsys, refusal):
    def run(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(solo_run, "run_baseline", run)
    assert cli.main([GOAL, "--project", str(tmp_path), "--baseline", "x"]) == 1
    assert capsys.readouterr().out.strip().endswith(f"Error: {refusal}")


def test_the_baseline_help_says_it_writes_and_what_its_exit_status_means(monkeypatch):
    flat = _flat_help(monkeypatch)
    assert "It implies write access to the project: the model edits your files, with or without --allow-writes" in flat
    assert "The goal is not judged and the run never reports complete" in flat
    assert "exit status 0 means only that it ran to its end and its last check passed" in flat
    assert "any other engine flag is refused" in flat and "(default 1, the one fix round a lead gets; 0 for none)" in flat


def test_a_baseline_previews_as_the_writing_run_it_is(tmp_path, monkeypatch, capsys):
    calls = []

    def preview(root, paths=(), **kwargs):
        calls.append(kwargs)
        return {"blocked": []}

    monkeypatch.setattr(policy, "preview_policy", preview)
    assert cli.main(["--project", str(tmp_path), "--policy-preview"]) == 0
    assert cli.main(["--project", str(tmp_path), "--policy-preview", "--baseline", "claude:opus"]) == 0
    assert calls[0]["writing"] is False and calls[1]["writing"] is True


def test_a_whole_baseline_runs_from_the_command_line_and_never_reaches_a_real_cli(env, monkeypatch, capsys):
    project, _, trace = env
    monkeypatch.setenv("PATH", NO_VENDOR_PATH)
    import shutil
    assert not any(shutil.which(name) for name in ("claude", "codex", "grok"))
    monkeypatch.setenv("LLM_BACKEND", "cli")
    status = cli.main([solo_arm.GOAL, "--project", str(project.root), "--baseline", "claude:opus",
                       "--check", solo_arm.check()])
    printed = capsys.readouterr().out
    assert status == 0, printed
    assert len(solo_arm.calls(trace)) == 1 and "return a + b" in (project.root / "add.py").read_text()
    (run_dir,) = (project.root / ".quadratus" / "runs").iterdir()
    data = json.loads((run_dir / "result.json").read_text())
    assert data["mode"] == "baseline" and data["completed"] is False and data["baseline"]["gate"]["passed"] is True
    assert "# Baseline run: claude:opus alone; the goal was not judged" in printed
    assert "The baseline ran to its end and its last check passed. The goal was not judged." in printed
    assert "Goal reported complete" not in printed
    # A failing check that nothing repairs is exit 2, not 0 and not "stopped".
    monkeypatch.setenv("FAKE_MODE", "broken")
    assert cli.main([solo_arm.GOAL, "--project", str(project.root), "--baseline", "claude:opus",
                     "--check", solo_arm.check(), "--fix-rounds", "0"]) == 2
    assert "last check failed" in capsys.readouterr().out


# -- 2. --compete on the command line ----------------------------------------------------------------


def test_compete_reaches_the_runner_only_when_contests_are_on(tmp_path, runner):
    assert cli._COMPETE_MODES == contest.COMPETE_MODES
    assert cli.main([GOAL, "--project", str(tmp_path), "--allow-writes", "--compete", "complex"]) == 0
    assert runner["compete"] == "complex"
    for line in ([], ["--compete", "off"], ["--allow-writes", "--compete", "off"]):
        runner.clear()
        assert cli.main([GOAL, "--project", str(tmp_path), *line]) == 0
        assert runner and "compete" not in runner, "off is the call made before the flag existed"


def test_compete_is_refused_where_it_would_do_nothing(tmp_path, capsys, monkeypatch, runner):
    for engine in ([], ["--session"]):
        assert "--compete require --project" in _refused([GOAL, *engine, "--compete", "complex"], capsys, monkeypatch)
    error = _refused([GOAL, "--project", str(tmp_path), "--compete", "complex"], capsys, monkeypatch)
    assert "--compete complex requires --allow-writes" in error
    error = _refused([GOAL, "--project", str(tmp_path), "--compete", "everything"], capsys, monkeypatch)
    assert "--compete" in error and "invalid choice" in error
    assert not runner


def test_the_compete_help_says_what_it_costs_and_that_a_refusal_still_stops(monkeypatch):
    flat = _flat_help(monkeypatch)
    assert "Contests (default off)." in flat and "needs --allow-writes" in flat
    assert "Costs about one extra draft per contested task" in flat
    assert "The routed lead's own refusal still stops the run." in flat


# -- 3. --accept-watched-paths on the command line ----------------------------------------------------


def test_accept_watched_paths_reaches_the_runner_only_when_given(tmp_path, runner, capsys, monkeypatch):
    assert cli._WATCHED_ENV == project_watch.ACCEPT_WATCHED_PATHS_ENV
    assert cli.main([GOAL, "--project", str(tmp_path), "--accept-watched-paths"]) == 0
    assert runner["accept_watched_paths"] is True
    runner.clear()
    assert cli.main([GOAL, "--project", str(tmp_path)]) == 0
    assert runner and "accept_watched_paths" not in runner
    assert cli._trust(cli.build_parser().parse_args([])) == {}
    for engine in ([], ["--session"]):
        error = _refused([GOAL, *engine, "--accept-watched-paths"], capsys, monkeypatch)
        assert "--accept-watched-paths require --project" in error
    assert f"Same as {project_watch.ACCEPT_WATCHED_PATHS_ENV}=1." in _flat_help(monkeypatch)


def test_the_watched_path_refusal_names_a_flag_the_command_line_takes(tmp_path, monkeypatch, capsys):
    """The message named only a keyword and a setting; the flag it now names
    is one the parser has, and it starts the run."""
    from tests import test_review1110_glue_engine as engine
    root = engine._source(tmp_path)
    engine._quiet_run(monkeypatch, engine._plant_hook)(root)
    ran = []
    monkeypatch.setattr(Session, "run", lambda self, **kw: ran.append(1))
    assert cli.main([GOAL, "--project", str(root), "--allow-writes"]) == 1
    refusal = capsys.readouterr().out
    assert "Error: The last run on this project" in refusal and ".git/hooks/pre-commit [git] (added)" in refusal
    assert "--accept-watched-paths" in refusal and "no model was called" in refusal and not ran
    flags = {name for action in cli.build_parser().flags for name in action.option_strings}
    assert "--accept-watched-paths" in flags
    cli.main([GOAL, "--project", str(root), "--allow-writes", "--accept-watched-paths"])
    assert ran == [1], "the flag the refusal names starts the run"


def test_a_scratch_root_inside_the_project_reads_as_a_refusal_on_the_command_line(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    (root / "scratch").mkdir(parents=True)
    (root / "app.py").write_text("x = 1\n")
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(root / "scratch"))
    assert cli.main([GOAL, "--project", str(root)]) == 1
    printed = capsys.readouterr().out
    assert printed.strip().startswith("Error: ") or "\nError: " in printed
    assert "Nothing was run and no model was called." in printed


# -- 12. the callers handle the interrupt that is passed on after the record ---------------------------


def _interrupting(result):
    def run(*args, **kwargs):
        raise project_run.RunInterrupted(result) if result is not None else KeyboardInterrupt()
    return run


@pytest.mark.parametrize("line, target", [([], "project"), (["--baseline", "claude:opus"], "baseline")])
def test_the_command_line_says_where_the_record_is_and_exits_130(tmp_path, monkeypatch, capsys, line, target):
    folder = tmp_path / "runs" / "20261111T000000Z-abcdef12"
    result = project_run.ProjectResult(False, "report", folder, "", "")
    module, name = (project_run, "run_project") if target == "project" else (solo_run, "run_baseline")
    monkeypatch.setattr(module, name, _interrupting(result))
    assert cli.main([GOAL, "--project", str(tmp_path / "p"), *line]) == cli.INTERRUPTED == 130
    printed = capsys.readouterr().out
    assert f"Interrupted. The run record was written first: {folder}" in printed
    assert str(folder / "report.md") in printed and "Traceback" not in printed
    # An interrupt with no record behind it says so, and is still 130.
    monkeypatch.setattr(module, name, _interrupting(None))
    assert cli.main([GOAL, "--project", str(tmp_path / "p"), *line]) == 130
    assert "Interrupted before a run record was written." in capsys.readouterr().out


def test_a_real_interrupted_project_run_ends_the_command_with_its_record_named(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    _interrupt_the_transcript_copy(monkeypatch)
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    assert cli.main([GOAL, "--project", str(root)]) == 130
    (run_dir,) = (root / ".quadratus" / "runs").iterdir()
    assert f"The run record was written first: {run_dir}" in capsys.readouterr().out
    assert json.loads((run_dir / "result.json").read_text())["trace"]["interrupted"] is True


# -- 2, 3, 12. the same on the Project form ------------------------------------------------------------


def _form_runner(monkeypatch, tmp_path):
    seen = {}

    def run(goal, folder, settings, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(report="report", diff="", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    return seen


def _callback(demo, name):
    for block in demo.fns.values():
        if getattr(block.fn, "__name__", None) == name:
            return block
    raise AssertionError(f"no {name} callback")


def test_the_form_runner_passes_contests_and_the_acceptance_only_when_chosen(tmp_path, monkeypatch):
    seen = _form_runner(monkeypatch, tmp_path)
    list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings(),
                            compete="complex", accept_watched_paths=True))
    assert seen["compete"] == "complex" and seen["accept_watched_paths"] is True
    for chosen in ({}, {"compete": "off"}, {"compete": ""}, {"accept_watched_paths": False}):
        seen.clear()
        list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings(), **chosen))
        assert seen and "compete" not in seen and "accept_watched_paths" not in seen, chosen


def test_the_project_tab_has_a_contests_choice_that_is_off_until_chosen(tmp_path, monkeypatch):
    pytest.importorskip("gradio")
    import gradio as gr
    seen = _form_runner(monkeypatch, tmp_path)
    demo = gui.build_interface(Settings())
    by_label = {getattr(b, "label", None): b for b in demo.blocks.values()}
    choice = by_label[gui.CONTEST_LABEL]
    assert type(choice).__name__ == "Dropdown" and choice.value == "off" and "--compete" in gui.CONTEST_LABEL
    assert [value for _, value in choice.choices] == list(contest.COMPETE_MODES)
    assert choice.info == gui.CONTEST_INFO and "Allow changes to project files" in choice.info
    block = _callback(demo, "run_selected")
    assert getattr(block.inputs[-1], "label", None) == gui.CONTEST_LABEL
    # goal, folder, writes, check, mode, tasks, paths, forbid, repairs, neutral, profile, extra, probes
    common = ("goal", str(tmp_path), True, "", "adversarial", 10, "", "", "", False, "", "", "")
    limits = (False, 24, 500_000, 900, 2, 0, 0, 0)
    trust = ("", False, False, False)
    list(block.fn(*common, *limits, *trust, "complex"))
    assert seen["compete"] == "complex"
    for rest in ((*trust, "off"), trust, ("", False, False), ()):
        seen.clear()
        list(block.fn(*common, *limits, *rest))
        assert seen and "compete" not in seen, "left alone, the call is the one made before the choice existed"
    seen.clear()
    read_only = ("goal", str(tmp_path), False, *common[3:])
    with pytest.raises(gr.Error, match="Contests need 'Allow changes to project files'"):
        list(block.fn(*read_only, *limits, *trust, "complex"))
    assert seen == {}, "refused before the runner, as the command line refuses it"


def test_the_trust_section_carries_the_watched_paths_acceptance(tmp_path, monkeypatch):
    pytest.importorskip("gradio")
    seen = _form_runner(monkeypatch, tmp_path)
    demo = gui.build_interface(Settings())
    by_label = {getattr(b, "label", None): b for b in demo.blocks.values()}
    box = by_label[gui.TRUST_LABELS["accept_watched_paths"]]
    assert type(box).__name__ == "Checkbox" and box.value is False
    assert "--accept-watched-paths" in gui.TRUST_LABELS["accept_watched_paths"]
    assert box.info == gui.TRUST_ALLOWS["accept_watched_paths"] and box.info.startswith("Allows ")
    section = next(b for b in demo.blocks.values()
                   if type(b).__name__ == "Accordion" and getattr(b, "label", None) == "Trust")
    parent = box
    while parent is not None and parent is not section:
        parent = getattr(parent, "parent", None)
    assert parent is section, "it sits in the Trust section, beside its neighbours"
    common = ("goal", str(tmp_path), True, "", "adversarial", 10, "", "", "", False, "", "", "")
    limits = (False, 24, 500_000, 900, 2, 0, 0, 0)
    block = _callback(demo, "run_selected")
    list(block.fn(*common, *limits, "", False, False, True))
    assert seen["accept_watched_paths"] is True
    seen.clear()
    list(block.fn(*common, *limits, "", False, False, False))
    assert seen and "accept_watched_paths" not in seen
    # Opening a project clears it with the rest of the section.
    cleared = _callback(demo, "clear_trust")
    assert cleared.fn() == ("", False, False, False) and box in cleared.outputs
    notes = " ".join(str(getattr(b, "value", "")) for b in demo.blocks.values() if type(b).__name__ == "Markdown")
    assert "QUADRATUS_ACCEPT_WATCHED_PATHS" in notes


def test_the_form_shows_an_interrupted_run_with_its_record(tmp_path, monkeypatch):
    folder = tmp_path / ".quadratus" / "runs" / "20261111T000000Z-abcdef12"
    folder.mkdir(parents=True)
    (folder / "report.md").write_text("# Run incomplete\n")
    result = project_run.ProjectResult(False, "# Run incomplete", folder, "the diff", "")
    monkeypatch.setattr(project_run, "run_project", _interrupting(result))
    *_, last = gui.run_project_ui("goal", str(tmp_path), False, "", "adversarial", 3, Settings())
    report, diff, files = last
    assert report.startswith("**Run interrupted.**") and str(folder) in report and "# Run incomplete" in report
    assert diff == "the diff" and [Path(f).name for f in files] == ["report.md"]
    assert "Run failed" not in report
    monkeypatch.setattr(project_run, "run_project", _interrupting(None))
    *_, last = gui.run_project_ui("goal", str(tmp_path), False, "", "adversarial", 3, Settings())
    assert last == ("**Run interrupted** before its record was written.", "", [])


# -- 10. the smaller notes: plain wiring with one answer ------------------------------------------------


def _edited_test_project(tmp_path):
    root = tmp_path / "edited"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text("VALUE = 1\n")
    original = ("import os\n\n\ndef test_where_it_runs():\n"
                "    open(os.environ['WHERE'], 'w').write(os.getcwd())\n")
    (root / "tests" / "test_where.py").write_text(original + "\n# edited during the run\n")
    gate = IntegrationGate([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_where.py"],
                           cwd=root)
    return root, gate, {"tests/test_where.py": original.encode()}


def test_the_original_tests_copy_is_made_under_the_private_scratch_root(tmp_path, monkeypatch):
    """It holds the project's withheld files and ran from the system temp
    folder, where a vendor sandbox may write. Every other copy the harness
    makes was already under the scratch root."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(scratch))
    where = tmp_path / "where.txt"
    monkeypatch.setenv(child_env.PASS_VARIABLE, "WHERE")
    monkeypatch.setenv("WHERE", str(where))
    root, gate, originals = _edited_test_project(tmp_path)
    result, problem = run_original_tests(gate, root, originals, Project(root).contents())
    assert problem == "" and result.passed, result.output[-600:]
    ran_in = Path(where.read_text())
    assert ran_in.parent == scratch.resolve() and ran_in.name.startswith("quadratus-original-tests-")
    assert list(scratch.iterdir()) == [], "the copy is removed when the run ends"


def test_a_scratch_root_that_cannot_be_used_means_the_original_tests_were_not_established(tmp_path, monkeypatch):
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder")
    monkeypatch.setenv("QUADRATUS_SCRATCH_DIR", str(blocked))
    root, gate, originals = _edited_test_project(tmp_path)
    result, problem = run_original_tests(gate, root, originals, Project(root).contents())
    assert result is None, "never a pass, and never a run in the system temp folder instead"
    assert "private scratch folder could not be used" in problem and "QUADRATUS_SCRATCH_DIR" in problem


def test_the_record_holds_what_the_operator_let_the_run_take_on_trust(tmp_path, monkeypatch):
    root = _declaring_project(tmp_path)
    (root / ".mcp.json").write_text("{}")
    _, record, _ = _quiet_project_run(monkeypatch, root, allow_project_agent_config=True, declared_checks=False)
    assert record["trust"] == dict(
        project_policy=dict(in_project=False, approved=False, sha256=""),
        agent_config_allowed=[".mcp.json"], declared_checks_declined=True, watched_paths_accepted=[])
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("x = 1\n")
    _, record, _ = _quiet_project_run(monkeypatch, plain)
    assert record["trust"] == dict(project_policy=dict(in_project=False, approved=False, sha256=""),
                                   agent_config_allowed=[], declared_checks_declined=False,
                                   watched_paths_accepted=[])


def test_the_record_names_the_watched_paths_a_run_started_over(tmp_path, monkeypatch):
    """Accepted, the run started with only a progress line to say so; the
    next run reads this run's record and is not refused."""
    from tests import test_review1110_glue_engine as engine
    root = engine._source(tmp_path)
    engine._quiet_run(monkeypatch, engine._plant_hook)(root)
    result, record, _ = _quiet_project_run(monkeypatch, root, allow_writes=True, accept_watched_paths=True)
    assert record["trust"]["watched_paths_accepted"] == [
        dict(path=".git/hooks/pre-commit", kind="git", what="added")]
    assert record["project_watch"]["paths"] == [], "this run's own calls changed none"


def test_an_approved_project_policy_is_on_the_record_with_its_hash(tmp_path, monkeypatch):
    from tests import test_review1110_glue_ui as ui
    root = tmp_path / "policied"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    digest = ui._command_policy(root)
    _, record, _ = _quiet_project_run(monkeypatch, root, trust_project_policy=digest)
    assert record["trust"]["project_policy"] == dict(in_project=True, approved=True, sha256=digest)


def test_the_record_names_the_environment_variables_no_child_was_given(tmp_path, monkeypatch, env):
    monkeypatch.setenv("DEPLOY_TOKEN", "never-in-a-record")
    monkeypatch.delenv(child_env.PASS_VARIABLE, raising=False)
    root = tmp_path / "plain"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    result, record, _ = _quiet_project_run(monkeypatch, root)
    assert "DEPLOY_TOKEN" in record["child_environment"]["withheld"]
    assert record["child_environment"]["keep_with"] == child_env.PASS_VARIABLE
    assert record["child_environment"]["withheld"] == child_env.withheld_names()
    assert "never-in-a-record" not in (result.run_dir / "result.json").read_text(), "names only, never a value"
    # A baseline's record carries the same.
    data = solo_arm.record(solo_arm.run(env))
    assert "DEPLOY_TOKEN" in data["child_environment"]["withheld"]
    assert data["trust"]["declared_checks_declined"] is False and data["trust"]["agent_config_allowed"] == []


def test_a_failed_readiness_probe_names_the_variables_it_was_not_given(tmp_path, monkeypatch, env):
    monkeypatch.setenv("REGISTRY_TOKEN", "never-in-a-message")
    monkeypatch.delenv(child_env.PASS_VARIABLE, raising=False)
    receipt = dict(id="docker", reason="exit 1", output="cannot connect")
    message = readiness.failure_message(receipt)
    assert message.startswith("readiness probe docker failed (exit 1); no model call was made. Output tail: ")
    assert "REGISTRY_TOKEN" in message and child_env.PASS_VARIABLE in message
    assert "never-in-a-message" not in message
    # One wording for a project run and a baseline, and both end with the names.
    from quadratus import session as engine
    assert "failure_message(failed[0])" in inspect.getsource(engine.Session._run_readiness)
    assert "failure_message(failed[0])" in inspect.getsource(solo_run._ready)
    result = solo_arm.run(env, readiness=[dict(id="ready", argv=["false"])])
    assert result.error.startswith("CapabilityProbeFailed: readiness probe ready failed")
    assert "REGISTRY_TOKEN" in result.error and "never-in-a-message" not in result.error
    # With nothing withheld the message is the one it always was.
    monkeypatch.setattr(child_env, "withheld_names", lambda base=None: [])
    assert readiness.failure_message(receipt) == (
        "readiness probe docker failed (exit 1); no model call was made. Output tail: cannot connect")


def test_the_truth_check_names_a_watched_path_an_editing_call_changed(tmp_path):
    check = dict(passed=True, source="s", receipts=[dict(id="tests", required=True, status="passed", tests=1)])
    record = dict(completed=True, checks=[check], source_fingerprint="s",
                  requirements=dict(listed={"R1": "a"}, status={"R1": "met"},
                                    audits=[dict(verdicts={"R1": dict(met=True)})]),
                  workflow=dict(tasks=[dict(checks=[check])]))
    folder = _run_folder(tmp_path, "run", dict(record, project_watch=dict(findings=[], paths=[])))
    (folder / "changes.diff").write_text("+++ b/app.py\n")
    assert truth_check.judge(folder)["verdict"] == truth_check.VERIFIED
    planted = dict(findings=[{}], paths=[dict(path=".git/hooks/pre-commit", kind="git", what="added")])
    (folder / "result.json").write_text(json.dumps(dict(record, project_watch=planted)))
    judged = truth_check.judge(folder)
    assert judged["verdict"] == truth_check.UNVERIFIED
    assert any("watched path(s)" in reason and ".git/hooks/pre-commit" in reason for reason in judged["unverified"])
    for odd in ("x", dict(paths="x"), dict(paths=[7])):
        (folder / "result.json").write_text(json.dumps(dict(record, project_watch=odd)))
        assert truth_check.judge(folder)["verdict"] in (truth_check.VERIFIED, truth_check.UNVERIFIED)
