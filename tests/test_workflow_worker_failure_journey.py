"""J30: a failed optional errand stays a result in a whole Session journey."""

import json

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, Script


def test_optional_worker_failure_is_recorded_and_the_task_can_complete(tmp_path, monkeypatch):
    """Only the vendor launch is fake; dispatch, edges and DONE are real."""
    request = {"errand": "check", "instruction": "Find the add implementation."}

    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"

    def lead(call, replay):
        calls = [c for c in replay.of("lead") if c.task == "t1"]
        if len(calls) == 1:
            return "WORKER " + json.dumps(request)
        assert "failed and produced nothing" in call.prompt
        assert "worker unavailable" in call.prompt
        H.write(call, {"app.py": FIXED})
        return 'Implemented add myself.\nCHANGED: ["app.py"]'

    def worker(call, replay):
        return H.claude_error("worker unavailable")

    script = Script(orchestrator=orchestrator, lead=lead)

    def respond(call, replay):
        return worker(call, replay) if call.role.startswith("worker:") else script(call, replay)

    replay = H.run(tmp_path, monkeypatch, respond, files=FILES, max_tasks=3)

    assert len(replay.of("worker")) >= 1, "the errand reached the provider boundary"
    assert all(c.vendor == "claude" for c in replay.of("worker"))
    assert len([c for c in replay.of("lead") if c.task == "t1"]) == 2
    assert replay.result.completed, replay.result.error
    assert (replay.project / "app.py").read_text() == FIXED
    assert H.gate_results(replay) == ["PASSED"]
    (task,) = replay.workflow["tasks"]
    assert task["contract"]["required"]["checks"] is True
    assert task["edges"]["checks"] is True and task["unsatisfied"] == []
    assert replay.workflow["parity"]["agree"] is True
