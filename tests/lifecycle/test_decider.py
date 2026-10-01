"""An external decider on the routing decisions the orchestrator left
unlabelled (quadratus.decisions, 2026-09-30). Scripted decider, no network.
A stated label is never overridden; a refusal keeps the rule's default."""
import json
from pathlib import Path

from quadratus.decisions import Decision, DecisionsUnavailable, Verdict
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, T1, Script

UNLABELLED = "SCOPE: " + json.dumps(T1) + "\nImplement add in app.py."


class FakeDecider:
    def __init__(self, answers=None, refuse=""):
        self.answers = answers or {"task.kind": "backend", "task.difficulty": "rote"}
        self.refuse, self.asked = refuse, []

    def decide(self, decision: Decision) -> Verdict:
        self.asked.append(decision)
        if self.refuse:
            raise DecisionsUnavailable(self.refuse)
        return Verdict(decision=decision.id, answer=self.answers[decision.id], source="fake:jev", confidence=0.77)


def _lead(call, replay):
    H.write(call, {"app.py": FIXED})
    return 'Implemented add.\nCHANGED: ["app.py"]'


def _run(tmp_path, monkeypatch, decl, decider):
    plan = [decl]
    script = Script(orchestrator=lambda c, r: plan.pop(0) if plan else "DONE", lead=_lead)
    return H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, decider=decider)


def _record(replay):
    return json.loads((Path(replay.result.run_dir) / "result.json").read_text())["decisions"]


def test_an_unlabelled_task_is_routed_by_the_decider_and_recorded(tmp_path, monkeypatch):
    decider = FakeDecider()
    replay = _run(tmp_path, monkeypatch, UNLABELLED, decider)
    assert replay.result.completed, replay.result.error
    assert [d.id for d in decider.asked] == ["task.kind", "task.difficulty"]
    assert "Implement add" in decider.asked[0].context
    record = _record(replay)
    assert [(r["decision"], r["answer"], r["source"], r["default"]) for r in record] == [
        ("task.kind", "backend", "fake:jev", "general"), ("task.difficulty", "rote", "fake:jev", "simple")]
    assert all(r["task"] == "t1" and r["confidence"] == 0.77 for r in record)


def test_a_stated_label_is_never_sent_to_the_decider(tmp_path, monkeypatch):
    decider = FakeDecider()
    replay = _run(tmp_path, monkeypatch, DECL_T1, decider)
    assert replay.result.completed, replay.result.error
    assert decider.asked == [] and _record(replay) == []


def test_a_refusing_decider_keeps_the_default_and_records_why(tmp_path, monkeypatch):
    decider = FakeDecider(refuse="no TYPESAFE_API_KEY: Jev is a billed API")
    replay = _run(tmp_path, monkeypatch, UNLABELLED, decider)
    assert replay.result.completed, replay.result.error
    record = _record(replay)
    assert [(r["decision"], r["answer"], r["default"]) for r in record] == [
        ("task.kind", None, "general"), ("task.difficulty", None, "simple")]
    assert all("billed API" in r["error"] for r in record)


def test_the_runner_resolves_the_decider_by_name(tmp_path, monkeypatch):
    import pytest
    with pytest.raises(ValueError, match="unknown decider"):
        _run(tmp_path, monkeypatch, DECL_T1, "luna")


def test_an_unlabelled_listed_task_is_routed_by_the_decider(tmp_path, monkeypatch):
    """The run Codex makes with Jev on: the explicit favicon list with its
    KIND line removed. The decider fires on the listed task exactly as on an
    orchestrated one, and the stated label on a listed task is never sent."""
    decider = FakeDecider({"task.kind": "docs", "task.difficulty": "rote"})
    script = Script(orchestrator=lambda c, r: (_ for _ in ()).throw(AssertionError("orchestrator called")),
                    lead=_lead)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, decider=decider,
                   tasks=[UNLABELLED])
    assert replay.result.completed, replay.result.error
    assert [d.id for d in decider.asked] == ["task.kind", "task.difficulty"]
    record = _record(replay)
    assert [(r["task"], r["decision"], r["answer"]) for r in record] == [
        ("t1", "task.kind", "docs"), ("t1", "task.difficulty", "rote")]
    labelled = FakeDecider()
    (tmp_path / "labelled").mkdir()
    H.run(tmp_path / "labelled", monkeypatch, Script(orchestrator=lambda c, r: "DONE", lead=_lead),
          files=FILES, max_tasks=1, decider=labelled, tasks=[DECL_T1])
    assert labelled.asked == []
