"""Unknown failures are an operator handoff, never more model work (map J27).

An exception nothing classifies (no outcome row, no declared errand outcome)
stops the run as ``operator`` as it was raised: the lead is not re-asked to
work around it and no repair is spent. Declared worker failures (a
``ProviderError``, a refused or repeated errand) still go back to the lead.
The unknown exception is injected into the harness's own worker runner: a
controller invariant, not observed vendor behaviour.
"""

import json

import pytest

from quadratus.providers import ProviderError
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

READ = {"errand": "read", "instruction": "Where is add defined?"}


def _t1_leads(replay):
    return [c for c in replay.of("lead") if c.task == "t1"]


def _worker_runner(monkeypatch, behave):
    """Replace the pool's runner: ``behave(prompt)`` returns text or raises."""
    from quadratus.workers import WorkerPool
    calls = []

    def run(self, model_key, prompt, *, allow_writes):
        calls.append(prompt)
        return behave(prompt)
    monkeypatch.setattr(WorkerPool, "_run", run)
    return calls


def _handed_to_operator(replay, name, detail):
    result = replay.result
    assert not result.completed
    assert result.error.startswith(f"{name}: "), result.error
    assert detail in result.error, result.error
    stop = replay.workflow["run"]["facts"][-1]
    assert stop["kind"] == "operator" and stop["legacy"] == name, stop
    assert not replay.of("gate-fix") and not replay.of("revision"), "no repair"
    assert (replay.project / "app.py").read_text() == FIXED, "work preserved"


@pytest.mark.parametrize("failure, detail", [
    (KeyError("summary"), "'summary'"),
    (RuntimeError("worker broke"), "worker broke"),     # a bare RuntimeError names nothing either
])
def test_an_unknown_worker_failure_stops_without_re_asking_the_lead(tmp_path, monkeypatch, failure, detail):
    def behave(prompt):
        raise failure
    calls = _worker_runner(monkeypatch, behave)

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED})
        return "Implemented add; checking the tests with a worker.\nWORKER " + json.dumps(READ)
    replay = H.run(tmp_path, monkeypatch, Script(lead=lead), files=FILES)
    _handed_to_operator(replay, type(failure).__name__, detail)
    assert len(calls) == 1
    assert len(_t1_leads(replay)) == 1, "the lead is not re-asked after an unknown failure"


def test_an_unknown_failure_in_a_sibling_pair_stops_after_both_ran(tmp_path, monkeypatch):
    def behave(prompt):
        if "first try" in prompt:
            raise ProviderError("overloaded")
        if "helper look" in prompt:
            raise AttributeError("'NoneType' object has no attribute 'summary'")
        return "add is in app.py"
    calls = _worker_runner(monkeypatch, behave)

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        n = len(_t1_leads(replay))
        if n == 1:
            H.write(call, {"app.py": FIXED})
            return "Implemented add.\nWORKER " + json.dumps(dict(READ, instruction="first try"))
        assert "failed and produced nothing: overloaded" in call.prompt, "a declared failure returns"
        return "Retrying with a helper.\nWORKER " + json.dumps(dict(
            READ, instruction="second try", retry_of="read-1",
            helper=dict(errand="read", instruction="helper look")))
    replay = H.run(tmp_path, monkeypatch, Script(lead=lead), files=FILES)
    _handed_to_operator(replay, "AttributeError", "no attribute 'summary'")
    assert len(calls) == 3, "both siblings ran to completion"
    assert len(_t1_leads(replay)) == 2, "re-asked after the ProviderError, not after the unknown failure"


def test_an_unknown_failure_in_the_lead_draft_is_not_redrafted(tmp_path, monkeypatch):
    from quadratus.session import Session
    draft = Session._draft_with_channels

    def broken(self, lead, spec, task, *args, **kw):
        if spec.task_id == "t1":
            H.write(type("C", (), {"cwd": str(self.project)})(), {"app.py": FIXED})
            raise LookupError("no such seat record")
        return draft(self, lead, spec, task, *args, **kw)
    monkeypatch.setattr(Session, "_draft_with_channels", broken)
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES)
    _handed_to_operator(replay, "LookupError", "no such seat record")
    assert not _t1_leads(replay), "no recovery redraft"


# -- negative: declared worker failures still go back to the lead ------------

@pytest.mark.parametrize("failure", [ProviderError("overloaded"), ProviderError("PATCH must contain a diff")])
def test_a_declared_worker_failure_returns_to_the_lead(tmp_path, monkeypatch, failure):
    def behave(prompt):
        raise failure
    _worker_runner(monkeypatch, behave)

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        if len(_t1_leads(replay)) == 1:
            return "Asking a worker.\nWORKER " + json.dumps(READ)
        assert "failed and produced nothing" in call.prompt
        H.write(call, {"app.py": FIXED})
        return 'Did it myself.\nCHANGED: ["app.py"]'
    replay = H.run(tmp_path, monkeypatch, Script(lead=lead), files=FILES)
    assert len(_t1_leads(replay)) == 2
    assert H.ended_at_cap(replay, 1), replay.result.error


# -- the in-session worker bridge (seam: the lead's open call is simulated) ----

def test_an_unknown_failure_on_the_bridge_tells_the_lead_to_stop_and_is_re_raised(tmp_path, monkeypatch):
    """A lead's open call cannot be interrupted: it is told to stop, and the
    drafting loop re-raises the original exception when the call returns."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.delegation import invocation
    from quadratus.session import Session, SessionConfig, TaskSpec
    from quadratus.workers import WorkerPool
    from tests.test_worker_tool import OPUS, _call_tool, _memory
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    store = ArtifactStore(tmp_path / "artifacts")
    boom = KeyError("summary")
    answers, lead_calls = [], []

    def fails(model, prompt, allow_writes=False):
        raise boom

    def invoke(model, prompt, system=None, allow_writes=False):
        lead_calls.append(prompt)
        answers.append(_call_tool({"errand": "read", "instruction": "one"}))
        return "Stopped as told."

    session = Session("goal", store, invoke, config=SessionConfig(max_worker_failures=5))
    session.workers = WorkerPool(store=store, run=fails)
    with invocation("t1", "lead"), pytest.raises(KeyError) as caught:
        session._draft_with_channels(OPUS, TaskSpec("t1", "do it"), _memory(store))
    assert caught.value is boom and caught.value.__traceback__ is not None, "the original exception, as raised"
    assert "unclassified failure (KeyError" in answers[0][0] and "make no further changes" in answers[0][0]
    assert len(lead_calls) == 1, "no new lead call after the open one returns"
