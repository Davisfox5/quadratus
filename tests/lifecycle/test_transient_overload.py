"""Cycle06 run 20261108T075040Z-4454b282 (root's assessment, PR73
6719287741): the read-only planner's Claude CLI returned an error envelope,
"API Error: 529 Overloaded", with zero reported usage; ``parse_claude``
raised ProviderError and ``_generate_once`` rethrew every ProviderError
before its retry check, so the run stopped INCOMPLETE with 32 calls, 2.36M
tokens and 3 task slots left, DONE declarations and the audit never reached.

A 529 on a settled envelope is now ``TransientOverload``: re-sent on the
same seat, model and role, only where a replay cannot be a second edit, at
most ``MAX_TRANSIENT_RETRIES`` times, each a separately admitted attempt with
its wait on the ledger, and never when the budget has no attempt or wall time
for it. Everything else that is a ProviderError keeps its behaviour.

Real Session and controller with the fake CLI launcher, and units on the
provider and the claude extractor."""
import json
import uuid
from pathlib import Path

import pytest

from quadratus import providers as P
from quadratus.cli_providers import _extract_claude_result
from quadratus.providers import (
    LLMProvider,
    ProviderError,
    ProviderRefusal,
    TransientOverload,
    TurnLimitReached,
)
from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import Seen, _script

pytestmark = pytest.mark.requirements_ledger

MESSAGE = "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment."
ZERO = {"input_tokens": 0, "output_tokens": 0}


def overloaded(*, typed=True, usage=ZERO, status=529):
    """The claude CLI's error envelope for an overloaded API, as the run saw it."""
    body = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
            "result": MESSAGE.replace("529", str(status)), "stop_reason": None,
            "session_id": str(uuid.uuid4())}
    if typed:
        body["api_error_status"] = status
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body)


def _planner(overloads):
    """The plan, then ``overloads`` 529 envelopes on the next planner call,
    then DONE."""
    sent = []

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return _plan(_scope(proofs=_proofs("R1")))
        if len(sent) < overloads:
            sent.append(n)
            return overloaded()
        return "DONE"
    return orchestrator


@pytest.fixture
def waits(monkeypatch):
    import time
    from types import SimpleNamespace
    slept = []
    # Only the provider's own wait: every other sleep in the run is real.
    monkeypatch.setattr(P, "time", SimpleNamespace(monotonic=time.monotonic, sleep=slept.append))
    return slept


def _ledger(replay):
    path = Path(replay.result.run_dir) / "invocations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run(tmp_path, monkeypatch, name, overloads, **kw):
    script = _script(Seen(), orchestrator=_planner(overloads))
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=2, record_complete=False, **kw)


def test_a_read_only_planners_529_is_re_sent_once_and_the_run_finishes(tmp_path, monkeypatch, waits):
    replay = _run(tmp_path, monkeypatch, "retry", 1)
    assert replay.result.completed, replay.result.error
    planner = [c for c in replay.of("orchestrator")]
    assert len(planner) == 3, "plan, the overloaded attempt, its re-sent attempt (DONE)"
    assert planner[1].prompt == planner[2].prompt and planner[1].model == planner[2].model, "the same call"
    assert len(waits) == 1 and 0 < waits[0] < 4
    rows = [r for r in _ledger(replay) if r.get("role") == "orchestrator"]
    failed = next(r for r in rows if r.get("outcome") == "TransientOverload")
    resent = rows[rows.index(failed) + 1]
    assert failed["diagnostics"].get("status") == 529 and failed["attempt"] == 1
    assert resent["attempt"] == 2 and resent["outcome"] == "ok"
    assert resent["diagnostics"]["retry_cause"] == "overloaded"
    assert resent["diagnostics"]["retry_wait_seconds"] == pytest.approx(waits[0], abs=1e-3)
    budget = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())
    assert budget["reserved_attempts"] == len(replay.calls), "every attempt admitted and counted"


def test_without_the_retry_the_same_529_ends_the_run_as_it_did(tmp_path, monkeypatch, waits):
    """The trial's failure reproduced: no re-send, the run stops on the
    planner's ProviderError with the overload message."""
    monkeypatch.setattr(P, "MAX_TRANSIENT_RETRIES", 0)
    replay = _run(tmp_path, monkeypatch, "old", 1)
    assert not replay.result.completed and "529 Overloaded" in (replay.result.error or ""), replay.result.error
    assert len(replay.of("orchestrator")) == 2 and waits == []


def test_repeated_overloads_stop_after_the_bounded_retries(tmp_path, monkeypatch, waits):
    replay = _run(tmp_path, monkeypatch, "exhausted", 5)
    assert not replay.result.completed and "529 Overloaded" in (replay.result.error or "")
    assert len(replay.of("orchestrator")) == 1 + 1 + P.MAX_TRANSIENT_RETRIES and len(waits) == P.MAX_TRANSIENT_RETRIES
    assert waits[1] > waits[0] - 1, "backoff, not a fixed loop"


def test_no_attempt_left_means_no_wait_and_no_re_send(tmp_path, monkeypatch, waits):
    calibrate = _run(tmp_path, monkeypatch, "calibrate", 1)
    first = next(i for i, c in enumerate(calibrate.calls) if c.role == "orchestrator" and "529" in c.reply)
    # The overloaded attempt is admitted with the goal question and the
    # audit still held (two calls); after it, a re-send would leave too few.
    limits = RunLimits(max_calls=first + 1 + 2, max_reported_tokens=6_000_000, wall_seconds=600,
                       max_concurrent_workers=2)
    waits.clear()
    replay = _run(tmp_path, monkeypatch, "spent", 1, limits=limits)
    assert not replay.result.completed and "529 Overloaded" in (replay.result.error or ""), replay.result.error
    assert len(replay.of("orchestrator")) == 2 and waits == [], "no wait and no re-send"


# -- units: what is and is not a TransientOverload, and when it is re-sent --------------------------

@pytest.mark.parametrize("stdout, kind", [
    (overloaded(), TransientOverload),
    (overloaded(typed=False), TransientOverload),
    (overloaded(usage=None), ProviderError),
    (overloaded(status=503), ProviderError),
    (overloaded(status=500), ProviderError),
])
def test_only_a_settled_529_envelope_is_a_transient_overload(stdout, kind):
    with pytest.raises(ProviderError) as raised:
        _extract_claude_result(stdout)
    assert type(raised.value) is kind


def test_a_model_answer_that_mentions_529_is_an_answer():
    reply = H.claude_ok("The API returned 529 Overloaded once; try again in a moment.")
    assert "529" in _extract_claude_result(reply)


class _Fake(LLMProvider):
    name, label, transport = "claude", "Claude", "cli"

    def __init__(self, failures, **kw):
        super().__init__("opus", "key", **kw)
        self._client = object()
        self.failures = list(failures)
        self.calls = 0

    def _call(self, prompt, system, history):
        self.calls += 1
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        if self.failures:
            raise self.failures.pop(0)
        return "ok"

    def _retryable(self, exc):
        return False


def _overload():
    return TransientOverload("claude reported an error: " + MESSAGE, status=529)


@pytest.mark.parametrize("name, setup, calls", [
    ("read-only", lambda p: None, 2),
    ("write-capable", lambda p: setattr(p, "allow_writes", True), 1),
    ("summary view", lambda p: setattr(p, "max_retries", 1), 1),
    ("usage unknown", lambda p: setattr(p, "_call", _unknown_usage(p)), 1),
])
def test_a_transient_overload_is_re_sent_only_where_a_replay_is_safe_and_settled(waits, name, setup, calls):
    provider = _Fake([_overload()])
    setup(provider)
    if calls == 2:
        assert provider.generate("q") == "ok"
    else:
        with pytest.raises(TransientOverload):
            provider.generate("q")
    assert provider.calls == calls, name


def _unknown_usage(provider):
    def call(prompt, system, history):
        provider.calls += 1
        provider.last_usage = None
        raise _overload()
    return call


@pytest.mark.parametrize("failure", [
    TurnLimitReached("cap", turns=6), ProviderRefusal("declined"), P.PartialWorkSuspected("partial"),
    ProviderError("claude reported an error: API Error: 503 unavailable"),
    ProviderError("claude reported an error: subscription window exhausted"),
])
def test_other_provider_failures_keep_their_behaviour(waits, failure):
    provider = _Fake([failure])
    with pytest.raises(type(failure)):
        provider.generate("q")
    assert provider.calls == 1 and waits == []


class _Budget:
    def __init__(self, **snap):
        self.snap = dict(limits=dict(max_calls=60, wall_seconds=600), reserved_attempts=10, elapsed_seconds=0,
                         finish_protection=dict(held_wall_seconds=0, pending=[]))
        for key, value in snap.items():
            if key in ("max_calls", "wall_seconds"):
                self.snap["limits"][key] = value
            elif key in ("held_wall_seconds", "pending"):
                self.snap["finish_protection"][key] = value
            else:
                self.snap[key] = value

    def snapshot(self):
        return self.snap


@pytest.mark.parametrize("budget, resent", [
    (_Budget(), True),
    (_Budget(reserved_attempts=60), False),
    (_Budget(reserved_attempts=57, pending=["goal", "audit"]), True),
    (_Budget(reserved_attempts=58, pending=["goal", "audit"]), False),
    (_Budget(elapsed_seconds=596), False),
    (_Budget(elapsed_seconds=300, held_wall_seconds=296), False),
])
def test_the_wait_is_bounded_by_the_attempts_and_wall_time_left(budget, resent):
    provider = _Fake([])
    provider.last_usage = {"input_tokens": 0, "output_tokens": 0}
    provider.run_budget = budget
    assert (provider._transient_wait(_overload(), 0) is not None) is resent
