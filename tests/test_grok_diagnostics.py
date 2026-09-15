"""What a grok turn was doing when it stopped, in a form the ledger may keep.

The 2026-09-14 restricted-worker cancellation left no record of what the turn
attempted; the completion report had to say the trigger was not recorded.
These tests pin the bounded record: stop reason, model-call count, attempted
tool *names*. Nothing else may leak through -- the record is published.
"""

from __future__ import annotations

import json

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import GrokCLIProvider, _extract_grok_diagnostics, _extract_grok_result
from quadratus.providers import ProviderError


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


#: The vendor does not document the transcript shape inside the envelope, so
#: this fixture covers the likely spellings; the walk must read names and
#: nothing else off whichever appears.
_CANCELLED = json.dumps({
    "text": "I'll run the mutation harness now.",
    "stopReason": "cancelled",
    "modelCalls": 2,
    "usage": {"input_tokens": 19670, "cache_read_input_tokens": 17920, "output_tokens": 777},
    "toolCalls": [
        {"name": "bash", "arguments": {"command": "node tests/ui/mutation_check.js", "cwd": "/Users/davis/secret"}},
        {"tool": "read_file", "input": {"path": "/Users/davis/secret/docs/BULK_EDIT.md"}},
        {"function": {"name": "grep", "arguments": "--include=*.md https://example.invalid/token?x=1"}},
        {"name": "bash"},
    ],
})


def test_diagnostics_hold_stop_reason_model_calls_and_tool_names_only():
    got = _extract_grok_diagnostics(_CANCELLED)
    assert got == {"stop_reason": "cancelled", "model_calls": 2,
                   "attempted_tools": ["bash", "read_file", "grep"]}


def test_nothing_but_names_leaks():
    """Arguments, paths and URLs never reach the record."""
    flat = json.dumps(_extract_grok_diagnostics(_CANCELLED))
    for secret in ("mutation_check", "/Users", "secret", "https://", "token", "BULK_EDIT"):
        assert secret not in flat


_TRANSCRIPT_WITH_PEOPLE = json.dumps({
    "stopReason": "cancelled",
    "messages": [
        {"role": "user", "name": "ExamplePerson", "content": "hello"},
        {"role": "assistant", "name": "Grok", "content": "I'll look."},
        {"type": "tool_call", "name": "read_file", "arguments": {"path": "/Users/example/x"}},
        {"role": "tool", "toolName": "grep", "content": "..."},
        {"type": "text", "name": "Not-a-tool"},
    ],
    "events": [{"name": "turn.started"}, {"kind": "log", "name": "Another Person"}],
    "steps": [{"function": {"name": "list_dir"}}],
})


def test_message_authors_and_event_labels_are_not_tools():
    """Codex's finding on b73f827: a bare ``name`` on a message is an author,
    possibly a person; only entries the envelope marks as tool calls count."""
    got = _extract_grok_diagnostics(_TRANSCRIPT_WITH_PEOPLE)
    assert got["attempted_tools"] == ["read_file", "grep", "list_dir"]
    flat = json.dumps(got)
    for private in ("ExamplePerson", "Another", "Grok", "turn.started", "Not-a-tool", "/Users"):
        assert private not in flat


def test_a_tool_call_container_needs_no_type_marker():
    got = _extract_grok_diagnostics(json.dumps({"stopReason": "cancelled",
                                                "tool_calls": [{"name": "bash"}], "toolUses": [{"name": "grep"}]}))
    assert got["attempted_tools"] == ["bash", "grep"]


def test_a_completed_turn_is_recorded_too():
    got = _extract_grok_diagnostics(json.dumps({"text": "DONE", "stopReason": "end_turn", "modelCalls": 1}))
    assert got == {"stop_reason": "end_turn", "model_calls": 1}


def test_an_absent_transcript_is_not_reported_as_no_tools():
    got = _extract_grok_diagnostics(json.dumps({"text": "x", "stopReason": "cancelled"}))
    assert got == {"stop_reason": "cancelled"}
    assert "attempted_tools" not in got


def test_narration_and_junk_yield_no_record():
    assert _extract_grok_diagnostics("I'll implement add(a, b).") is None
    assert _extract_grok_diagnostics(json.dumps([1, 2])) is None
    assert _extract_grok_diagnostics(json.dumps({"stopReason": 7, "modelCalls": True})) is None


def test_the_tool_list_is_bounded_and_deduplicated():
    calls = [{"name": f"tool{i % 5}"} for i in range(200)] + [{"name": f"x{i}"} for i in range(200)]
    got = _extract_grok_diagnostics(json.dumps({"stopReason": "cancelled", "toolCalls": calls}))
    assert len(got["attempted_tools"]) == cli_providers._MAX_DIAGNOSTIC_TOOLS
    assert got["attempted_tools"][:5] == ["tool0", "tool1", "tool2", "tool3", "tool4"]


def test_names_that_are_not_names_are_dropped():
    got = _extract_grok_diagnostics(json.dumps({"stopReason": "cancelled", "toolCalls": [
        {"name": "rm -rf /"}, {"name": "/etc/passwd"}, {"name": "ok_tool"}, {"name": "x" * 80}]}))
    assert got["attempted_tools"] == ["ok_tool"]


def test_the_cancellation_error_names_the_attempted_tools():
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(_CANCELLED)
    assert "attempted tools: bash, read_file, grep" in str(caught.value)
    assert "/Users" not in str(caught.value)


def test_the_provider_exposes_diagnostics_and_resets_them_per_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    replies = iter([_CANCELLED, "not json at all", json.dumps({"stopReason": "end_turn", "text": "DONE", "modelCalls": 1})])
    monkeypatch.setattr(cli_providers, "_launch", lambda argv, **kw: _FakeCompleted(next(replies)))
    provider = GrokCLIProvider(model="x", workdir=str(tmp_path))
    try:
        assert provider.last_diagnostics is None
        with pytest.raises(ProviderError) as caught:
            provider._call("go", "", [])
        assert caught.value.diagnostics == provider.last_diagnostics
        assert provider.last_diagnostics["attempted_tools"] == ["bash", "read_file", "grep"]
        with pytest.raises(ProviderError):
            provider._call("go", "", [])
        assert provider.last_diagnostics is None, "a stale record must not describe a later call"
        assert provider._call("go", "", []) == "DONE"
        assert provider.last_diagnostics == {"stop_reason": "end_turn", "model_calls": 1}
    finally:
        provider.cleanup()


def test_other_vendors_carry_no_diagnostics():
    assert cli_providers.CLAUDE_SPEC.extract_diagnostics is None
    assert cli_providers.CODEX_SPEC.extract_diagnostics is None
    assert cli_providers.GROK_SPEC.extract_diagnostics is _extract_grok_diagnostics
