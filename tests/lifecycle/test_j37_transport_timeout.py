"""J37: a timed-out editing transport preserves and reports its partial writes.

Only the vendor CLI launch is scripted. The real provider translates the
transport timeout, and the whole controller inspects the edited tree and
stops without replaying the writing prompt.
"""

import subprocess

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, Script


def test_j37_timeout_after_writes_is_inspected_and_never_replayed(tmp_path, monkeypatch):
    def orchestrator(call, replay):
        assert len(replay.of("orchestrator")) == 1, "no second task after the timeout"
        return DECL_T1

    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "tests/test_app.py": "unfinished\n"})
        raise subprocess.TimeoutExpired(call.argv, 1)

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead),
                   files=FILES, max_tasks=3)

    assert len(replay.of("lead")) == 1, "the writing prompt must not be replayed"
    assert len(replay.of("orchestrator")) == 1
    assert not replay.result.completed
    assert replay.result.error.startswith("PartialWorkStopped:")
    assert "timed out" in replay.result.error
    assert (replay.project / "app.py").read_text() == FIXED
    assert (replay.project / "tests/test_app.py").read_text() == "unfinished\n"
    assert H.gate_results(replay) == [], "no check receipt for an interrupted draft"

    (task,) = replay.workflow["tasks"]
    assert task["task_id"] == "t1"
    assert task["closed_as"] == "stopped:PartialWorkStopped"
    assert task["primary"] == "integrity"
    assert task["partial"]["inspected"] is True
    assert {"app.py", "tests/test_app.py"} <= set(task["partial"]["changed"])
    stop = [f for f in replay.workflow["run"]["facts"]
            if f["terminal"] and not f["recovered"] and f["legacy"]]
    assert len(stop) == 1
    assert (stop[0]["kind"], stop[0]["legacy"]) == ("integrity", "PartialWorkStopped")
    assert replay.workflow["parity"]["agree"] is True
