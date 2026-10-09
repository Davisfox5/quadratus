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
    assert "unidentified:transcript:3117d48a:4221d9a5:1 (unknown model) via Agent: usage unknown" in ledger


def _run_with(tmp_path, monkeypatch, traces):
    (tmp_path / "app.py").write_text("x\n")
    monkeypatch.setattr(Session, "run", lambda self, **kw: None)
    monkeypatch.setattr("quadratus.trace.build_traces", lambda *a, **k: traces)
    result = run_project("g", tmp_path, Settings(backend="cli"), allow_writes=True)
    return (json.loads((result.run_dir / "result.json").read_text())["delegation"],
            (result.run_dir / "delegation.md").read_text())


def test_a_refused_spawn_is_the_control_holding_not_a_child(tmp_path, monkeypatch):
    """Codex review of 6a338a1: a permission-denied Agent was filed as a child that ran."""
    record, ledger = _run_with(tmp_path, monkeypatch, [
        {"invocation_id": "i1", "task": "t1", "role": "lead", "model": "claude:opus", "session_id": "s1",
         "tool_calls": [{"name": "Agent", "outcome": "denied", "id": "toolu_1"}]}])
    assert record["unidentified_native_activity"] == 0 and record["native_child_tokens"] == 0
    assert "refused by permissions" in ledger and "no child ran" in ledger


def test_an_unresolved_spawn_is_an_attempt_not_a_run(tmp_path, monkeypatch):
    record, ledger = _run_with(tmp_path, monkeypatch, [
        {"invocation_id": "i1", "task": "t1", "role": "lead", "model": "claude:opus", "session_id": "s1",
         "tool_calls": [{"name": "Task", "outcome": "unknown", "id": "toolu_2"}]}])
    assert record["unidentified_native_activity"] == 1 and record["native_child_tokens"] == 0


def test_one_call_read_twice_is_one_child(tmp_path, monkeypatch):
    """Codex review of 6a338a1: replaying one parent transcript counted the
    same native call twice. Two invocations share session s1 (a resumed
    session), and each trace names the same call id."""
    call = {"name": "Agent", "outcome": "success", "id": "toolu_9"}
    record, _ = _run_with(tmp_path, monkeypatch, [
        {"invocation_id": "i1", "task": "run", "role": "orchestrator", "model": "claude:fable",
         "session_id": "s1", "tool_calls": [call]},
        {"invocation_id": "i2", "task": "run", "role": "orchestrator", "model": "claude:fable",
         "session_id": "s1", "tool_calls": [call, dict(call)]}])
    assert record["unidentified_native_activity"] == 1


def test_the_claude_trace_lists_a_restated_block_once(tmp_path):
    from quadratus.trace import _claude
    block = {"type": "tool_use", "id": "toolu_7", "name": "Agent", "input": {"prompt": "run checks"}}
    rows = [{"type": "assistant", "message": {"content": [block]}},
            {"type": "assistant", "message": {"content": [block]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_7",
                                                      "content": "ok"}]}}]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    calls = _claude(path)["tool_calls"]
    assert [(c["name"], c["outcome"], c["id"]) for c in calls] == [("Agent", "success", "toolu_7")]
