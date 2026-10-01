"""J37: a timed-out editing transport preserves and reports its partial writes.

The harness scripts the vendor CLI launch and stubs provider availability,
executable discovery, and lead command capability. The real provider
translates the transport timeout, and the whole controller inspects the
edited tree and stops without replaying the writing prompt.
"""

import json
import subprocess

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, Script


def test_j37_timeout_after_writes_is_inspected_and_never_replayed(tmp_path, monkeypatch):
    """Revised 2026-09-28 (J39): the timeout fails the task, which is handed
    back; the writing prompt is still never replayed. The run then stops on
    the budget's own rule: a timed-out call has no usage, and an unmetered
    call latches the budget before the next call."""
    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "tests/test_app.py": "unfinished\n"})
        raise subprocess.TimeoutExpired(call.argv, 1)

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead),
                   files=FILES, max_tasks=3)

    assert len(replay.of("lead")) == 1, "the writing prompt must not be replayed"
    assert len(replay.of("orchestrator")) == 1, "the budget latched before the next call"
    assert not replay.result.completed
    assert replay.result.error == "RunBudgetExceeded: Run stopped: unknown_usage"
    assert (replay.project / "app.py").read_text() == FIXED
    assert (replay.project / "tests/test_app.py").read_text() == "unfinished\n"
    assert H.gate_results(replay) == [], "no check receipt for an interrupted draft"
    assert (replay.result.run_dir / "report.md").read_text() == replay.result.report

    (task,) = replay.workflow["tasks"]
    assert task["task_id"] == "t1"
    assert task["closed_as"] == "failed"
    assert task["primary"] == "failed"
    fact = next(f for f in task["facts"] if f["kind"] == "failed")
    assert fact["detail"].startswith("transport: ") and "timed out" in fact["detail"]
    assert task["partial"]["inspected"] is True
    assert {"app.py", "tests/test_app.py"} <= set(task["partial"]["changed"])
    record = json.loads(replay.artifact_texts("task-failed")[0])
    assert record["cause"] == "transport" and {"app.py", "tests/test_app.py"} <= set(record["changed"])
    assert replay.workflow["parity"]["agree"] is True
