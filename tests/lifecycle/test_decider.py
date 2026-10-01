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
        return Verdict(decision=decision.id, answer=self.answers[decision.id], source="fake:jev", confidence=0.77,
                       usage=dict(model="fake", host="fake", input_tokens=300, output_tokens=0, seconds=0.2))


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
    assert all(r["task"] == "t1" and r["confidence"] == 0.77 and r["usage"]["seconds"] == 0.2 for r in record)


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


# ---- Codex review of #44 (3e4528a): per-field delegation, batch admission --

def test_only_the_unstated_field_is_delegated(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    partial = FakeDecider({"task.difficulty": "rote"})
    _run(tmp_path / "a", monkeypatch, "KIND: backend\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py.", partial)
    assert [d.id for d in partial.asked] == ["task.difficulty"]
    degraded = FakeDecider({"task.kind": "backend"})
    replay = _run(tmp_path / "b", monkeypatch, "KIND: typo complex\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py.",
                  degraded)
    assert [d.id for d in degraded.asked] == ["task.kind"]
    assert [(r["decision"], r["answer"]) for r in _record(replay)] == [("task.kind", "backend")]


def _unit_session(tmp_path, decider):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                   config=SessionConfig(decider=decider, max_parallel_tasks=3))


def test_a_rejected_batch_makes_no_paid_decisions(tmp_path):
    decider = FakeDecider()
    session = _unit_session(tmp_path, decider)
    same = "SCOPE: " + json.dumps(T1) + "\nImplement add in app.py."
    assert session._batch_specs([same, same], "seat") is None, "shared files reject the batch"
    assert decider.asked == []
    other = "SCOPE: " + json.dumps(dict(T1, permitted_paths=["README.md"])) + "\nDocument add."
    specs = session._batch_specs([same, other], "seat")
    assert specs is not None and len(specs) == 2
    assert [d.id for d in decider.asked] == ["task.kind", "task.difficulty"] * 2


# ---- Codex on #35 (5923216190): the shared budget bounds Jev calls -------

class _JevAnswer:
    def __init__(self, choice, tokens):
        self.choice, self.confidence, self.probabilities = choice, 0.9, {choice: 0.9}
        self.tokens = tokens


class _JevClient:
    """The SDK duck type, answering each decision in turn with a usage."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def system_one(self, *, state, questions, model=None):
        from types import SimpleNamespace
        self.calls += 1
        (name, _), = questions.items()
        answer = self.answers.pop(0)
        return SimpleNamespace(model="jev-1.13.0", usage=SimpleNamespace(input_tokens=answer.tokens, output_tokens=0),
                               choices={name: answer})


def _jev(answers):
    from quadratus.decisions import JevDecider
    return JevDecider(api_key="ts_test", client=_JevClient(answers))


def _budget(replay):
    return json.loads((Path(replay.result.run_dir) / "budget.json").read_text())


def _one_unlabelled_task():
    plan = [UNLABELLED]
    return Script(orchestrator=lambda c, r: plan.pop(0) if plan else "DONE", lead=_lead)


def test_the_call_limit_stops_the_run_before_a_second_jev_decision(tmp_path, monkeypatch):
    from quadratus.run_budget import RunLimits
    decider = _jev([_JevAnswer("backend", 300), _JevAnswer("rote", 300)])
    replay = H.run(tmp_path, monkeypatch, _one_unlabelled_task(), files=FILES, max_tasks=2, decider=decider,
                   limits=RunLimits(max_calls=2, max_reported_tokens=6_000_000, wall_seconds=600,
                                    max_concurrent_workers=1), record_complete=False)
    # Call 1 is the orchestrator naming the task, call 2 the first decision;
    # the second decision is refused before the lead ever runs.
    assert replay.result.error.startswith("RunBudgetExceeded") and "call_limit" in replay.result.error
    assert decider._client.calls == 1 and replay.of("lead") == [], "the second decision, not the lead, hit the limit"
    budget = _budget(replay)
    assert budget["reserved_attempts"] == 2 and budget["input_tokens"] == 100 + 300
    assert [r["decision"] for r in _record(replay)] == ["task.kind"], "the one decision that ran is on record"


def test_the_token_threshold_counts_jev_usage(tmp_path, monkeypatch):
    from quadratus.run_budget import RunLimits
    decider = _jev([_JevAnswer("backend", 300), _JevAnswer("rote", 300)])
    replay = H.run(tmp_path, monkeypatch, _one_unlabelled_task(), files=FILES, max_tasks=2, decider=decider,
                   limits=RunLimits(max_calls=20, max_reported_tokens=250, wall_seconds=600,
                                    max_concurrent_workers=1), record_complete=False)
    assert "reported_token_threshold" in replay.result.error, replay.result.error
    assert decider._client.calls == 1 and replay.of("lead") == []
    assert _budget(replay)["input_tokens"] == 100 + 300, "the orchestrator's 100 plus Jev's 300"


def test_an_unknown_usage_jev_reply_latches_the_budget_not_the_default_route(tmp_path, monkeypatch):
    from quadratus.run_budget import RunLimits
    decider = _jev([_JevAnswer("backend", None)])
    replay = H.run(tmp_path, monkeypatch, _one_unlabelled_task(), files=FILES, max_tasks=2, decider=decider,
                   limits=RunLimits(max_calls=20, max_reported_tokens=6_000_000, wall_seconds=600,
                                    max_concurrent_workers=1), record_complete=False)
    assert "unknown_usage" in replay.result.error and replay.of("lead") == []
    assert _budget(replay)["unknown_usage_attempts"] == 1


def test_a_bounded_jev_run_completes_and_reconciles_its_ledger(tmp_path, monkeypatch):
    from quadratus.run_budget import RunLimits
    decider = _jev([_JevAnswer("backend", 300), _JevAnswer("rote", 310)])
    replay = H.run(tmp_path, monkeypatch, _one_unlabelled_task(), files=FILES, max_tasks=2, decider=decider,
                   limits=RunLimits(max_calls=20, max_reported_tokens=6_000_000, wall_seconds=600,
                                    max_concurrent_workers=1), record_complete=False)
    assert replay.result.completed, replay.result.error
    budget = _budget(replay)
    assert budget["reserved_attempts"] == 2 + len(replay.calls), "two Jev reservations beside every CLI call"
    record = _record(replay)
    assert [(r["decision"], r["answer"], r["usage"]["input_tokens"]) for r in record] == [
        ("task.kind", "backend", 300), ("task.difficulty", "rote", 310)]
