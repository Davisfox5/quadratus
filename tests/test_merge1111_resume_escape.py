"""The operator's way out of the resume's "held to its own record" check.

A killed harness process, or a restart, leaves a run folder with a
checkpoint and no ``result.json``. Such a parent is refused by default, the
refusal names ``--accept-unrecorded-parent``, and with that acceptance the
job goes on with every other check of the resume, says so in its record and
report in this episode and every later one, and reads unverified to
``tools/truth_check.py``. A parent whose record names a different committed
state is refused either way. Whole-controller replays below the vendor CLI.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import cli, project_run
from quadratus.checkpoint import CheckpointRefused
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2, FILES, FIXED, Script
from tests.lifecycle.test_step2_recovery import _episode, _interrupt_after, _interrupted, _plan
from tests.lifecycle.test_truth_canaries import truth
from tests.test_merge1111_resume import FORGED, _forge

ENV = project_run.ACCEPT_UNRECORDED_PARENT_ENV
FLAG = "--accept-unrecorded-parent"


@pytest.fixture(autouse=True)
def no_standing_acceptance(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def _killed(tmp_path, monkeypatch, **kwargs):
    """An interrupted run as a killed process leaves it: its checkpoint and
    committed state, and no result.json."""
    replay, first = _interrupted(tmp_path, monkeypatch, **kwargs)
    os.unlink(first / "result.json")
    return replay, first


def _record(run_dir):
    return json.loads((Path(run_dir) / "result.json").read_text())


def test_a_killed_run_is_refused_by_default_and_the_refusal_names_the_way_out(tmp_path, monkeypatch):
    replay, first = _killed(tmp_path, monkeypatch)
    calls = len(replay.calls)
    with pytest.raises(CheckpointRefused) as refused:
        _episode(replay, resume_from=first.name)
    text = str(refused.value)
    assert "no readable record of its own" in text and "killed process" in text
    assert "Nothing in the run folder proves its checkpoint is the one the harness wrote" in text
    assert FLAG in text and f"{ENV}=1" in text and "accept_unrecorded_parent=True" in text
    assert len(replay.calls) == calls and not (first / "resumed-by.json").exists()


def test_an_older_record_without_committed_manifests_is_refused_with_the_same_way_out(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    record = _record(first)
    del record["committed_manifests"]
    (first / "result.json").write_text(json.dumps(record))
    with pytest.raises(CheckpointRefused, match="names no committed state") as refused:
        _episode(replay, resume_from=first.name)
    assert FLAG in str(refused.value)
    second = _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)
    assert _record(second.run_dir)["trust"]["unrecorded_parent_accepted"] == [
        dict(parent=first.name, why="result.json names no committed state", carried=False)]


def test_an_accepted_killed_run_resumes_finishes_its_work_and_says_so(tmp_path, monkeypatch):
    replay, first = _killed(tmp_path, monkeypatch)
    calls = len(replay.calls)
    lines = []
    second = _episode(replay, resume_from=first.name, accept_unrecorded_parent=True, progress=lines.append)
    record = _record(second.run_dir)
    # The job went on from where it stopped: t1 carried, t2 run in this episode.
    assert record["lineage"] == dict(root=first.name, parent=first.name, episode=2)
    assert record["tasks"] == 2 and len(replay.calls) > calls
    assert (replay.project / "app.py").read_text() == FIXED
    assert json.loads((first / "resumed-by.json").read_text())["child"] == Path(second.run_dir).name
    accepted = record["trust"]["unrecorded_parent_accepted"]
    assert accepted == [dict(parent=first.name, why="no readable result.json (FileNotFoundError)", carried=False)]
    report = (Path(second.run_dir) / "report.md").read_text()
    top = report.split("\n## ")[0]
    assert "Unrecorded parent accepted" in top and first.name in top
    assert "Unrecorded parent accepted" in second.report
    assert any("left no record of its own" in line and "Accepted by the operator" in line for line in lines)
    verdict = truth.judge(second.run_dir, replay.project)
    assert verdict["verdict"] == truth.UNVERIFIED
    assert any("left no record of its own" in why and first.name in why for why in verdict["unverified"])


def test_the_environment_setting_is_the_same_acceptance(tmp_path, monkeypatch):
    replay, first = _killed(tmp_path, monkeypatch)
    monkeypatch.setenv(ENV, "1")
    second = _episode(replay, resume_from=first.name)
    assert _record(second.run_dir)["trust"]["unrecorded_parent_accepted"][0]["parent"] == first.name


def test_a_later_episode_carries_the_acceptance_and_is_unverified_too(tmp_path, monkeypatch):
    replay, first = _killed(tmp_path, monkeypatch, script=Script(orchestrator=_plan(DECL_T1, DECL_T2, DECL_T2)),
                            max_tasks=3)
    _interrupt_after(monkeypatch, 2)
    second = _episode(replay, resume_from=first.name, max_tasks=3, accept_unrecorded_parent=True)
    assert "KeyboardInterrupt" in second.error
    second = Path(second.run_dir)
    # Episode 2 left a record of its own, so episode 3 needs no acceptance.
    third = _episode(replay, resume_from=second.name, max_tasks=3)
    record = _record(third.run_dir)
    assert record["lineage"]["episode"] == 3
    assert record["trust"]["unrecorded_parent_accepted"] == [
        dict(parent=first.name, why="no readable result.json (FileNotFoundError)", carried=True)]
    assert "Unrecorded parent accepted" in (Path(third.run_dir) / "report.md").read_text()
    verdict = truth.judge(third.run_dir, replay.project)
    assert verdict["verdict"] == truth.UNVERIFIED
    assert any("left no record of its own" in why for why in verdict["unverified"])


def test_an_earlier_acceptance_that_cannot_be_read_is_carried_as_that():
    for trust in ("yes", dict(unrecorded_parent_accepted="yes"), dict(unrecorded_parent_accepted=["x"])):
        carried = project_run._unrecorded_parents(None, dict(trust=trust))
        assert carried and carried[0]["carried"] and "not readable" in carried[0]["why"]
    assert project_run._unrecorded_parents(None, dict(trust=dict(unrecorded_parent_accepted=[]))) == []
    assert project_run._unrecorded_parents(None, None) == []


def test_truth_check_reads_an_unreadable_record_of_the_acceptance_as_unverified(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1)), files=FILES, max_tasks=2)
    run_dir = Path(replay.result.run_dir)
    record = _record(run_dir)
    assert "unrecorded_parent_accepted" not in record["trust"], "written only when there is one"
    before = truth.judge(run_dir, replay.project)
    assert not any("left no record of its own" in why for why in before["unverified"])
    for value, said in (("yes", "is not readable"),
                        ([dict(parent="20261112T000000Z-aaaaaaaa")], "20261112T000000Z-aaaaaaaa"),
                        (["x"], "unreadable entry")):
        record["trust"]["unrecorded_parent_accepted"] = value
        (run_dir / "result.json").write_text(json.dumps(record))
        verdict = truth.judge(run_dir, replay.project)
        assert verdict["verdict"] == truth.UNVERIFIED
        assert any("left no record of its own" in why and said in why for why in verdict["unverified"])


def test_a_parent_whose_record_names_another_state_is_refused_even_when_accepted(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    _forge(first, FORGED)
    calls = len(replay.calls)
    monkeypatch.setenv(ENV, "1")
    with pytest.raises(CheckpointRefused, match="not the one the run's own record names") as refused:
        _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)
    assert FLAG not in str(refused.value)
    assert len(replay.calls) == calls and not (first / "resumed-by.json").exists()
    # A record whose committed_manifests is not a list names a state badly: no way out either.
    record = _record(first)
    record["committed_manifests"] = "all of them"
    (first / "result.json").write_text(json.dumps(record))
    with pytest.raises(CheckpointRefused, match="not the one the run's own record names"):
        _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)
    # A record that says nothing was committed, beside a committed state: the same.
    record["committed_manifests"] = []
    (first / "result.json").write_text(json.dumps(record))
    with pytest.raises(CheckpointRefused, match="says the run committed nothing") as refused:
        _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)
    assert FLAG not in str(refused.value) and not (first / "resumed-by.json").exists()


def test_every_other_check_of_the_resume_still_runs_under_the_acceptance(tmp_path, monkeypatch):
    replay, first = _killed(tmp_path, monkeypatch)
    (replay.project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(CheckpointRefused, match="source"):
        _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)
    (replay.project / "app.py").write_text(FIXED)
    with pytest.raises(CheckpointRefused, match="workflow"):
        _episode(replay, resume_from=first.name, accept_unrecorded_parent=True, compete="complex")
    assert not (first / "resumed-by.json").exists()
    # Once resumed, the parent is claimed: the acceptance does not resume it twice.
    _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)
    with pytest.raises(CheckpointRefused, match="resumed"):
        _episode(replay, resume_from=first.name, accept_unrecorded_parent=True)


# -- the command line ---------------------------------------------------------------------

@pytest.fixture
def runner(monkeypatch, tmp_path):
    seen = {}

    def run(goal, project, settings, **kwargs):
        seen.update(kwargs, goal=goal)
        return SimpleNamespace(completed=True, report="checked", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    return seen


def _refused(argv, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_build_settings",
                        lambda *a: pytest.fail("refused lines must not reach settings or providers"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    return capsys.readouterr().err


def test_the_flag_reaches_the_runner_only_when_given_and_needs_resume(tmp_path, runner, capsys, monkeypatch):
    assert cli._UNRECORDED_ENV == ENV
    assert cli.main(["goal", "--project", str(tmp_path), "--resume", "20261112T000000Z-aaaaaaaa", FLAG]) == 0
    assert runner["accept_unrecorded_parent"] is True and runner["resume_from"] == "20261112T000000Z-aaaaaaaa"
    runner.clear()
    assert cli.main(["goal", "--project", str(tmp_path), "--resume", "20261112T000000Z-aaaaaaaa"]) == 0
    assert runner and "accept_unrecorded_parent" not in runner
    for line in (["goal", "--project", str(tmp_path), FLAG], ["goal", FLAG], ["goal", "--session", FLAG]):
        assert f"{FLAG} requires --resume" in _refused(line, capsys, monkeypatch)
    error = _refused(["goal", "--project", str(tmp_path), "--baseline", "claude:opus", FLAG], capsys, monkeypatch)
    assert FLAG in error
    monkeypatch.setenv("COLUMNS", "100000")
    assert f"Same as {ENV}=1." in " ".join(cli.build_parser().format_help().split())
