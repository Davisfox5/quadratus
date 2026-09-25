"""Per-call traces read from the vendors' own session transcripts."""

import json
import os

from quadratus import trace


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _roots(tmp_path):
    return {v: tmp_path / "home" / v for v in ("claude", "codex", "grok")}


def _ledger(tmp_path, rows):
    path = tmp_path / "run" / "invocations.jsonl"
    _jsonl(path, rows)
    return path


def test_claude_transcript_tools_outcomes_and_outside_paths(tmp_path):
    roots = _roots(tmp_path)
    sid = "11111111-aaaa-bbbb-cccc-000000000001"
    _jsonl(roots["claude"] / "-proj" / f"{sid}.jsonl", [
        {"type": "assistant", "cwd": "/work/proj", "message": {"content": [
            {"type": "thinking", "thinking": "I could ask: WORKER {\"errand\": \"read\"}"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/work/proj/a.py"}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat /grader/test_contract.py"}},
            {"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "/work/proj/b.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "Permission denied", "is_error": True},
            {"type": "tool_result", "tool_use_id": "t3", "content": "written"},
        ]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    ])
    ledger = _ledger(tmp_path, [
        {"invoked": True, "requested_model": "claude:opus", "session_id": sid, "task": "T1",
         "role": "lead", "outcome": "ok", "seconds": 12.4, "input_tokens": 900, "output_tokens": 50},
        {"invoked": False, "requested_model": "claude:opus", "session_id": None},
    ])
    records = trace.build_traces(tmp_path / "run", ledger, "/work/proj", roots=roots)
    assert len(records) == 1
    r = records[0]
    assert [c["outcome"] for c in r["tool_calls"]] == ["success", "denied", "success"]
    assert r["files_read"] == ["/work/proj/a.py"] and r["files_written"] == ["/work/proj/b.py"]
    assert r["outside_project"] == ["/grader/test_contract.py"]
    assert r["protocol_attempts"][0]["verb"] == "WORKER"
    copied = tmp_path / "run" / r["transcript"]
    assert copied.exists() and oct(os.stat(copied).st_mode & 0o777) == "0o600"
    assert (tmp_path / "run" / "trace.jsonl").read_text().count("\n") == 1
    timeline = trace.render_timeline(records)
    assert "outside the project" in timeline and "never served" in timeline and "Bash 1 (1 denied)" in timeline


def test_codex_rollout_commands_patches_and_rejections(tmp_path):
    roots = _roots(tmp_path)
    tid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
    _jsonl(roots["codex"] / "2026" / "09" / "24" / f"rollout-2026-09-24T10-00-00-{tid}.jsonl", [
        {"type": "session_meta", "payload": {"cwd": "/work/proj"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                                              "input": 'tools.exec_command({"cmd": "pytest -q"})'}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                                              "output": {"exit_code": 1}}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c2", "name": "exec",
                                              "input": "tools.apply_patch('*** Update File: src/x.py')"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2",
                                              "output": "Rejected by policy"}},
    ])
    ledger = _ledger(tmp_path, [{"invoked": True, "requested_model": "openai:gpt-5.6-sol",
                                 "session_id": tid, "task": "T2", "role": "lead"}])
    r = trace.build_traces(tmp_path / "run", ledger, "/work/proj", roots=roots)[0]
    assert [c["outcome"] for c in r["tool_calls"]] == ["error", "denied"]
    assert r["commands"][0] == {"command": "pytest -q", "exit": 1, "outcome": "error"}
    assert r["files_written"] == ["src/x.py"]


def test_grok_session_injected_rules_and_mid_loop_requests(tmp_path):
    roots = _roots(tmp_path)
    sid = "22222222-aaaa-bbbb-cccc-000000000002"
    folder = roots["grok"] / "%2Fwork%2Fproj" / sid
    _jsonl(folder / "chat_history.jsonl", [
        {"type": "user", "content": "<user_rules>\nAlways verify in a browser.\n</user_rules> task"},
        {"type": "assistant", "content": "Let me FETCH: art-123 first",
         "tool_calls": [{"id": "g1", "name": "read_file", "arguments": "{\"target_file\": \"/work/proj/a.py\"}"}]},
        {"type": "assistant", "content": "final answer"},
    ])
    _jsonl(folder / "events.jsonl", [{"type": "tool_completed", "tool_call_id": "g1", "outcome": "success"}])
    ledger = _ledger(tmp_path, [{"invoked": True, "requested_model": "grok:default",
                                 "session_id": sid, "task": "T3", "role": "lead"}])
    r = trace.build_traces(tmp_path / "run", ledger, "/work/proj", roots=roots)[0]
    assert r["cwd"] == "/work/proj"
    assert r["injected_rules"][0]["first_line"] == "Always verify in a browser."
    assert r["protocol_attempts"][0]["verb"] == "FETCH"
    assert r["files_read"] == ["/work/proj/a.py"] and r["outside_project"] == []


def test_missing_transcript_is_listed_and_hostile_ids_are_not_globbed(tmp_path):
    roots = _roots(tmp_path)
    ledger = _ledger(tmp_path, [
        {"invoked": True, "requested_model": "claude:opus", "session_id": "*"},
        {"invoked": True, "requested_model": "grok:default", "session_id": None},
    ])
    records = trace.build_traces(tmp_path / "run", ledger, None, roots=roots)
    assert [r["transcript"] for r in records] == ["missing", "no session id"]
    assert trace.locate("claude", "../../etc", roots) is None


def test_grok_whole_stdout_envelope_yields_session_id():
    from types import SimpleNamespace

    from quadratus.cli_providers import GROK_SPEC, CLIProvider

    provider = CLIProvider.__new__(CLIProvider)
    provider.spec = GROK_SPEC
    provider.last_session_id = None
    provider.last_usage = None
    provider.last_diagnostics = None
    envelope = json.dumps({"sessionId": "33333333-aaaa-bbbb-cccc-000000000003", "text": "hi",
                           "stopReason": "end_turn"}, indent=2)
    try:
        provider._observe_output(envelope, SimpleNamespace(model="grok:default"))
    except TypeError:
        provider._observe_output(envelope)
    assert provider.last_session_id == "33333333-aaaa-bbbb-cccc-000000000003"
