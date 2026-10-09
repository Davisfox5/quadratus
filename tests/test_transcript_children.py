"""A native child that a call's own transcript names is on the delegation
record (batch 2 recovery-v2 on 5d9f5ff: the orchestrator ran an Agent child,
the trace said "Agent: success", and the record said zero native children)."""

import json

from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.session import Session


def test_an_agent_call_in_a_transcript_is_an_unidentified_child_with_nothing_added(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x\n")
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    traces = [
        {"invocation_id": "4221d9a5", "task": "run", "role": "orchestrator", "model": "claude:fable",
         "session_id": "3117d48a", "tool_calls": [{"name": "Read", "outcome": "success"},
                                                  {"name": "Agent", "outcome": "success"}]},
        {"invocation_id": "aa", "task": "t1", "role": "lead", "model": "grok:default",
         "session_id": "s2", "tool_calls": [{"name": "read_file", "outcome": "success"}]},
    ]
    monkeypatch.setattr("quadratus.trace.build_traces", lambda *a, **k: traces)
    result = run_project("g", tmp_path, Settings(backend="cli"), allow_writes=True)
    record = json.loads((result.run_dir / "result.json").read_text())["delegation"]
    assert record["unidentified_native_activity"] == 1 and record["native_children"] == 0
    assert record["native_child_tokens"] == 0, "the child's spend is inside the parent's total; nothing is added"
    ledger = (result.run_dir / "delegation.md").read_text()
    assert "unidentified:transcript:4221d9a5:1 (unknown model) via Agent: usage unknown" in ledger
