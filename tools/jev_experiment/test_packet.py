"""Offline experiment integrity and real selector controls; no vendor transport."""
import socket
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.jev_experiment import packet as P

ENGINE = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("network attempted during offline preparation")
    monkeypatch.setattr(socket.socket, "connect", fail)
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sample():
    return {"version": 1, "base_sha": P.git(ENGINE, "rev-parse", "HEAD"), "tasks": [{
        "id": "case1", "split": "held_out", "source_refs": [{"path": "README.md", "symbol": "title"}],
        "task_text": "Correct the documented spelling in README.md.",
        "scope": {"permitted_paths": ["README.md"], "forbidden_paths": [".env"], "max_lines": 10,
                  "intended_result": "Spelling corrected", "acceptance": ["Only the named spelling changes"]},
        "required_capabilities": ["patch"], "checks": [{"argv": ["python", "-m", "pytest"],
        "status": "proposed", "purpose": "test-only fixture, never executed"}],
        "acceptable_kinds": ["docs"], "acceptable_difficulties": ["rote", "simple"],
        "rationale": "test-only accepted answer set", "expected_default_lead": "grok:default"}]}


def test_actual_selector_distinguishes_worker_from_execute(sample):
    task = sample["tasks"][0]
    answers = {"task.kind": "docs", "task.difficulty": "rote"}
    assert P.route(task, ENGINE, answers)["lead"] == "grok:worker"
    task["required_capabilities"].append("execute")
    execution = P.route(task, ENGINE, answers)
    assert execution["lead"] == "grok:default"
    assert execution["capabilities_satisfied"]
    assert "execute" in execution["inferred_and_declared_needs"]


def test_acceptance_inference_and_kind_pin_are_not_static_ladder(sample):
    task = sample["tasks"][0]
    task["scope"]["acceptance"] = ["Run `python -m pytest -q` and confirm it passes"]
    assert "execute" in P.route(task, ENGINE)["inferred_and_declared_needs"]
    assert P.route(task, ENGINE, {"task.kind": "security", "task.difficulty": "rote"})["lead"] == "openai:gpt-5.6-sol"


def test_each_arm_uses_fresh_session_and_never_sends_answers(sample):
    task = sample["tasks"][0]
    first = P.route(task, ENGINE, {"task.kind": "docs", "task.difficulty": "simple"})
    assert first == P.route(task, ENGINE, {"task.kind": "docs", "task.difficulty": "simple"})
    assert first["rotation_after"] == 1
    assert first["vendor_load_after"] == {"grok": 1}
    assert all(task["rationale"] not in r["context"] for r in first["requests"])


def test_frozen_packet_detects_answer_tampering(sample):
    frozen = P.prepare(sample, ENGINE)
    assert P.verify_freeze(frozen)["provider_calls"] == 0
    frozen["content"]["packet"]["tasks"][0]["acceptable_difficulties"] = ["complex"]
    with pytest.raises(ValueError, match="changed"):
        P.verify_freeze(frozen)


@pytest.mark.parametrize("bad", ["KIND: docs rote", "LEAD: grok:worker", "NEEDS: execute"])
def test_packet_rejects_answer_or_capability_injection(sample, bad):
    sample["tasks"][0]["task_text"] = bad + "\nChange the spelling."
    P.bind_engine(ENGINE)
    with pytest.raises(ValueError, match="inject"):
        P.validate(sample, ENGINE)


def test_score_preserves_refusals_missing_rows_and_rejects_duplicates(sample):
    frozen = P.prepare(sample, ENGINE)
    obs = {"task_id": "case1", "repeat": 0, "freeze_digest": frozen["freeze_digest"],
           "input_digest": frozen["content"]["rows"][0]["input_digest"],
           "answers": {}, "refusal": "provider unavailable"}
    scored = P.score(frozen, [obs], ENGINE)
    assert not scored["complete"] and len(scored["missing"]) == 2
    assert not scored["rows"][0]["acceptable"]
    with pytest.raises(ValueError, match="duplicate"):
        P.score(frozen, [obs, deepcopy(obs)], ENGINE)
    obs["refusal"] = None
    obs["answers"] = {"task.kind": "docs", "task.difficulty": "complex"}
    row = P.score(frozen, [obs], ENGINE)["rows"][0]
    assert row["lead_changed"] and not row["acceptable"]
    obs["input_digest"] = "changed"
    with pytest.raises(ValueError, match="input differs"):
        P.score(frozen, [obs], ENGINE)


def test_existing_freeze_cannot_be_overwritten(tmp_path):
    path = tmp_path / "freeze.json"
    P.write_new(path, {"old": True})
    with pytest.raises(FileExistsError):
        P.write_new(path, {"new": True})


def test_jev_budget_blocks_second_classification_request():
    from quadratus.decisions import Decision, JevDecider
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
    calls = []
    class SDK:
        def system_one(self, **request):
            calls.append(request)
            name = next(iter(request["questions"]))
            answer = SimpleNamespace(choice="rote", confidence=1.0, probabilities={"rote": 1.0})
            return SimpleNamespace(model="fake", usage=SimpleNamespace(input_tokens=100, output_tokens=1),
                                   choices={name: answer})
    decider = JevDecider(client=SDK(), budget=RunBudget(RunLimits(max_calls=1)))
    decision = Decision(id="task.difficulty", question="?", answers=("rote", "simple"))
    decider.decide(decision)
    with pytest.raises(RunBudgetExceeded):
        decider.decide(decision)
    assert len(calls) == 1


def test_explicit_pairs_do_not_admit_unreviewed_cross_product(sample):
    task = sample["tasks"][0]
    task["acceptable_kinds"] = ["docs", "general"]
    task["acceptable_pairs"] = [{"kind": "docs", "difficulty": "rote"},
                                {"kind": "general", "difficulty": "simple"}]
    assert ("docs", "simple") not in P.accepted_pairs(task)
    assert len(P.prepare(sample, ENGINE)["content"]["rows"][0]["acceptable_routes"]) == 2


def test_long_input_cannot_be_silently_truncated(sample):
    sample["tasks"][0]["task_text"] = "a" * 8001
    with pytest.raises(ValueError, match="context slice"):
        P.prepare(sample, ENGINE)
