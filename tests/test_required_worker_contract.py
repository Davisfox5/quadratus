"""J30, required half: prospective contract for a required worker errand.

Design: docs/review/required-worker-contract-design.md. Nothing here changes
the engine. The xfails are strict and fail at an assertion on today's code;
each encodes one proposed behaviour through a whole ``run_project`` replay
(``tests.lifecycle.harness`` fakes only the vendor CLI launch). The controls
pass today and pin the optional behaviour the proposal must leave alone.

Proposed shape, in brief: a task block line ``REQUIRES-WORKER: check``, bound
into ``Required.worker_errands`` at dispatch, a ``worker:<errand>`` edge per
declared errand, and the existing completion guard refusing an unsatisfied
edge as ``CompletionUnproven`` (kind ``unverified``). No new budget, no new
retry allowance.
"""

import json

import pytest

from quadratus.contract import Required
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, Script

CHECK = {"errand": "check", "instruction": "Find the add implementation."}
#: The proposed declaration: one line in the orchestrator's task block.
DECL_REQUIRED = DECL_T1 + "\nREQUIRES-WORKER: check"

PROPOSED = "proposed required-worker contract (docs/review/required-worker-contract-design.md); not built"


def _run(tmp_path, monkeypatch, *, declaration, worker, lead=None):
    """One task, then DONE. ``worker`` answers every worker call."""

    def orchestrator(call, replay):
        return declaration if len(replay.of("orchestrator")) == 1 else "DONE"

    def default_lead(call, replay):
        calls = [c for c in replay.of("lead") if c.task == "t1"]
        if len(calls) == 1:
            return "WORKER " + json.dumps(CHECK)
        H.write(call, {"app.py": FIXED})
        return 'Implemented add myself.\nCHANGED: ["app.py"]'

    script = Script(orchestrator=orchestrator, lead=lead or default_lead)

    def respond(call, replay):
        return worker(call, replay) if call.role.startswith("worker:") else script(call, replay)

    return H.run(tmp_path, monkeypatch, respond, files=FILES, max_tasks=3)


def _failing_worker(call, replay):
    return H.claude_error("worker unavailable")


def _working_worker(call, replay):
    return "add is defined in app.py and returns 0; tests/test_app.py expects 3."


def _task(replay):
    (task,) = replay.workflow["tasks"]
    return task


def _stop(replay):
    return ((replay.workflow.get("run") or {}).get("facts") or [{}])[-1]


# -- controls: today's optional behaviour, which the proposal keeps ---------


def test_control_required_has_no_worker_field_today():
    """The contract has no way to say an errand is required. The proposal
    adds exactly one field; this pin moves with it."""
    assert set(Required.__dataclass_fields__) == {
        "checks", "design_evidence", "design_review", "security_verification",
        "settlement", "design_collaboration_applicable"}


def test_control_optional_failure_completes_with_no_worker_edge(tmp_path, monkeypatch):
    """An undeclared errand's failure is a result for the lead. It adds no
    edge, so the task completes on its checks alone. Unchanged by design."""
    replay = _run(tmp_path, monkeypatch, declaration=DECL_T1, worker=_failing_worker)
    assert replay.of("worker"), "the errand reached the provider boundary"
    assert replay.result.completed, replay.result.error
    task = _task(replay)
    assert not any(edge.startswith("worker") for edge in task["edges"])
    assert task["unsatisfied"] == []


def test_control_declaration_line_is_inert_today(tmp_path, monkeypatch):
    """Nothing parses REQUIRES-WORKER yet: the run completes exactly as if
    the errand were optional. This pin is what the xfails below reverse."""
    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_failing_worker)
    assert replay.result.completed, replay.result.error
    task = _task(replay)
    assert "worker_errands" not in task["contract"]["required"]
    assert task["unsatisfied"] == []


def test_control_identical_retry_is_still_refused(tmp_path, monkeypatch):
    """RepeatedFailure: the same instruction to the same worker after a
    failure is refused. A required errand must not widen this."""

    def lead(call, replay):
        calls = [c for c in replay.of("lead") if c.task == "t1"]
        if len(calls) <= 2:
            return "WORKER " + json.dumps(CHECK)
        assert "was refused" in call.prompt
        H.write(call, {"app.py": FIXED})
        return 'Implemented add myself.\nCHANGED: ["app.py"]'

    replay = _run(tmp_path, monkeypatch, declaration=DECL_T1, worker=_failing_worker, lead=lead)
    assert len(replay.of("worker")) == 1, "the identical retry never reached a provider"
    assert replay.result.completed, replay.result.error


# -- prospective: the required half of J30 ----------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PROPOSED)
def test_required_errand_is_bound_into_the_contract_at_dispatch(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_working_worker)
    task = _task(replay)
    assert task["contract"]["required"].get("worker_errands") == ["check"]
    lead_prompt = next(c.prompt for c in replay.of("lead") if c.task == "t1")
    assert "REQUIRES-WORKER" not in lead_prompt, "the line is split off the description"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PROPOSED)
def test_required_errand_success_satisfies_its_edge(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_working_worker)
    task = _task(replay)
    assert task["edges"].get("worker:check") is True
    assert task["unsatisfied"] == []
    assert replay.result.completed, replay.result.error


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PROPOSED)
def test_required_errand_failure_records_a_false_edge(tmp_path, monkeypatch):
    """The lead doing the work itself does not satisfy the errand."""
    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_failing_worker)
    assert (replay.project / "app.py").read_text() == FIXED, "the lead's own work is kept"
    task = _task(replay)
    assert task["edges"].get("worker:check") is False
    assert task["unsatisfied"] == ["worker:check"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PROPOSED)
def test_completion_refuses_an_unsatisfied_required_errand(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_failing_worker)
    assert not replay.result.completed
    assert replay.result.error.startswith("CompletionUnproven: "), replay.result.error
    assert "t1.worker:check unsatisfied" in replay.result.error
    assert _stop(replay).get("kind") == "unverified"
    assert _stop(replay).get("legacy") == "CompletionUnproven"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PROPOSED)
def test_required_errand_never_attempted_is_unsatisfied(tmp_path, monkeypatch):
    """A lead that ignores the declaration leaves the edge unattempted
    (absent), which blocks completion the same way a failure does."""

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_working_worker, lead=lead)
    assert not replay.of("worker")
    task = _task(replay)
    assert task["unsatisfied"] == ["worker:check"]
    assert not replay.result.completed


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PROPOSED)
def test_required_errand_does_not_widen_the_failure_allowance(tmp_path, monkeypatch):
    """Four failed attempts close the worker channel exactly as for an
    optional errand (max_worker_failures). The edge stays false."""
    rewrites = iter(range(100))

    def lead(call, replay):
        if "worker channel is now closed" in call.prompt:
            H.write(call, {"app.py": FIXED})
            return 'Implemented add myself.\nCHANGED: ["app.py"]'
        request = dict(CHECK, instruction=f"{CHECK['instruction']} Attempt {next(rewrites)}.")
        return "WORKER " + json.dumps(request)

    replay = _run(tmp_path, monkeypatch, declaration=DECL_REQUIRED, worker=_failing_worker, lead=lead)
    assert len(replay.of("worker")) == 4
    task = _task(replay)
    assert task["edges"].get("worker:check") is False
    assert not replay.result.completed
