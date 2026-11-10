"""Actual lifecycle control for human-authorized changes to obsolete tests."""

import hashlib
import json
import re

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import CLOSEOUT, Script
from tools import truth_check

pytestmark = pytest.mark.requirements_ledger
GOAL = "Change the default from 5 to 10 and update its obsolete test."
OLD = "from app import default\n\ndef test_default():\n    assert default() == 5\n"
NEW = OLD.replace("== 5", "== 10")
SENTINEL = "def test_unrelated():\n    assert 2 + 2 == 4\n"


def _run(tmp_path, monkeypatch, *, approve=True, renamed=False, drift=False, unrelated=False):
    questions = []
    delivered = NEW.replace("test_default", "test_new_default") if renamed else NEW
    case = "test_new_default" if renamed else "test_default"
    files = {"app.py": "def default():\n    return 5\n", "tests/test_defaults.py": OLD,
             "tests/test_unrelated.py": SENTINEL}
    scope = dict(permitted_paths=list(files), intended_result="default is 10", max_lines=60,
                 acceptance=["default returns 10"])
    declaration = ("REQUIREMENTS:\nR1: the default returns 10\nKIND: backend standard\nSCOPE: "
                   + json.dumps(scope) + "\nChange the default to 10.\nCOVERS: R1")
    plan = [declaration]

    def lead(call, replay):
        writes = {"app.py": "def default():\n    return 10\n", "tests/test_defaults.py": delivered}
        if unrelated:
            writes["tests/test_unrelated.py"] = SENTINEL.replace("== 4", "== 5")
        H.write(call, writes)
        return (f"Changed the requested default.\nPROVES: P1 - tests/test_defaults.py::{case}\n"
                + "CHANGED: " + json.dumps(sorted(writes)))

    def ask(question):
        questions.append(question)
        if drift:
            (tmp_path / "project" / "app.py").write_text("def default():\n    return 11\n")
        match = re.search(r"Reply exactly (APPROVE TEST CHANGE [0-9a-f]{64})", question)
        assert match, question
        return match.group(1) if approve else "decline"

    original_run = H.run_project

    def run_with_operator(*args, **kwargs):
        return original_run(*args, **dict(kwargs, ask_operator=ask))

    monkeypatch.setattr(H, "run_project", run_with_operator)
    script = Script(orchestrator=lambda c, r: plan.pop(0) if plan else "DONE", lead=lead,
                    closeout=lambda c, r: CLOSEOUT,
                    **{"requirements-review": lambda c, r: "COMPLETE",
                       "auditor": lambda c, r: f"R1: MET - tests/test_defaults.py::{case} shows default returns 10"})
    replay = H.run(tmp_path, monkeypatch, script, files=files, check=H.GATE.replace(" -q ", " -v "), max_tasks=2,
                   goal=GOAL, record_complete=not drift)
    return replay, questions, json.loads((replay.result.run_dir / "result.json").read_text())


@pytest.mark.parametrize("renamed", [False, True])
def test_goal_authorized_test_change_can_finish_after_concrete_approval(tmp_path, monkeypatch, renamed):
    replay, questions, record = _run(tmp_path, monkeypatch, renamed=renamed)
    assert replay.result.completed, replay.result.error
    assert len(questions) == 1 and GOAL in questions[0] and "Full diff:" in questions[0]
    [decision] = record["test_change_decisions"]
    assert decision["status"] == "approved" and decision["original_sha256"] == hashlib.sha256(OLD.encode()).hexdigest()
    assert any(r.get("passed") is False and r.get("superseded_tests") for r in record["original_test_runs"])
    verdict = truth_check.judge(replay.result.run_dir, replay.project)
    assert verdict["verdict"] == truth_check.VERIFIED, verdict


def test_declining_does_not_buy_the_same_approval_question_again(tmp_path, monkeypatch):
    replay, questions, record = _run(tmp_path, monkeypatch, approve=False)
    assert not replay.result.completed and len(questions) == 1
    assert record["test_change_decisions"][0]["status"] == "declined"


def test_source_motion_during_approval_does_not_apply_the_answer(tmp_path, monkeypatch):
    replay, questions, record = _run(tmp_path, monkeypatch, drift=True)
    assert not replay.result.completed and len(questions) == 1
    assert "EvidenceIdentityMismatch" in replay.result.error
    assert not record["test_change_decisions"]


def test_current_failure_cannot_be_approved_away(tmp_path, monkeypatch):
    replay, questions, record = _run(tmp_path, monkeypatch, unrelated=True)
    assert not replay.result.completed and not questions
    assert not record["test_change_decisions"]


def test_independent_checker_rejects_stale_or_forged_test_approval(tmp_path, monkeypatch):
    replay, _, record = _run(tmp_path, monkeypatch)
    record["test_change_decisions"][0]["answer"] = "a model says approved"
    (replay.result.run_dir / "result.json").write_text(json.dumps(record))
    verdict = truth_check.judge(replay.result.run_dir, replay.project)
    assert verdict["verdict"] != truth_check.VERIFIED
    assert any("authorized test change" in p for p in verdict["unverified"])
