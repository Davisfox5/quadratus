"""Grok identity and progress capture (root on 113f4e1, 6549956979 item 2).

The P16 Grok lead reasoned for fifteen minutes with zero tool calls and was
stopped by the 900 s transport timeout. Its native session existed, but the
ledger row had no session id: grok had no ``--session-id`` and its final-only
JSON envelope never arrived. The id is now allocated before launch, so a
timeout cannot hide the session, and the session's own ``events.jsonl`` is
read as counts to tell active reasoning, tool execution and a wait with no
output apart. The JSON envelope, usage and stop semantics are unchanged.
"""
import json
import subprocess
from uuid import UUID

import pytest

from quadratus import cli_providers, recovery
from quadratus.cli_providers import CLAUDE_SPEC, CODEX_SPEC, GROK_SPEC, GrokCLIProvider
from quadratus.project_run import _live_call_trace, _transcript_integrity, _transcript_progress
from quadratus.providers import ProviderError
from quadratus.trace import locate, session_roots


def _phase(phase, second, **extra):
    return json.dumps(dict(type="phase", phase=phase, timestamp=f"2026-10-31T20:49:{second:02d}.000Z", **extra))


def _session(root, sid, rows, *, cwd="%2Fwork%2Fproject"):
    folder = root / ".grok" / "sessions" / cwd / sid
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text("\n".join(rows) + ("\n" if rows else ""))
    (folder / "chat_history.jsonl").write_text(json.dumps({"type": "user", "content": "the task"}) + "\n")
    return folder


@pytest.fixture
def grok(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/grok")
    monkeypatch.setenv("HOME", str(tmp_path))
    provider = GrokCLIProvider(model="default", workdir=str(tmp_path / "work"))
    provider.max_retries = 1
    return provider


def test_only_grok_gained_the_flag_and_the_envelope_is_unchanged():
    assert GROK_SPEC.session_id_flag == "--session-id"
    assert GROK_SPEC.output_args == ["--output-format", "json"], "usage and stop semantics stay the envelope's"
    assert CLAUDE_SPEC.session_id_flag == "--session-id" and CODEX_SPEC.session_id_flag is None


def test_a_timed_out_grok_call_keeps_its_session_and_its_native_progress(grok, monkeypatch, tmp_path):
    seen = {}

    def launch(argv, **kwargs):
        sid = argv[argv.index("--session-id") + 1]
        assert str(UUID(sid)) == sid, "a fresh UUID, allocated before launch"
        seen["sid"] = sid
        seen["folder"] = _session(tmp_path, sid, [_phase("streaming_reasoning", n) for n in range(20, 50, 2)])
        raise subprocess.TimeoutExpired(cmd=argv, timeout=900, output="", stderr="")

    monkeypatch.setattr(cli_providers, "_launch", launch)
    with pytest.raises(ProviderError, match="timed out"):
        grok.generate("implement the concurrency proof")
    assert grok.last_session_id == seen["sid"], "the timeout did not hide the session"
    assert grok.last_usage is None, "no usage is invented"
    assert locate("grok", grok.last_session_id, session_roots()) == seen["folder"]
    progress = _transcript_progress(seen["folder"])
    assert progress["status"] == "observed" and progress["activity"] == "reasoning"
    assert progress["events"] == 15 and progress["tool_events"] == 0 and progress["reasoning_events"] == 15
    assert progress["span_seconds"] == 28.0 and progress["longest_gap_seconds"] == 2.0


@pytest.mark.parametrize("rows,status,activity", [
    ([_phase("streaming_reasoning", 1), _phase("tool_started", 2), _phase("tool_completed", 3)],
     "observed", "tool_use"),
    ([_phase("streaming_reasoning", 1), '{"type": "phase", "phase": "streaming_reaso'], "partial", "reasoning"),
    ([], "unavailable", "no_events"),
], ids=["tools", "truncated-tail", "no-output"])
def test_progress_tells_tools_reasoning_and_no_output_apart_and_says_when_it_is_partial(tmp_path, rows, status,
                                                                                         activity):
    folder = _session(tmp_path, "0e3b6f2a-0000-4000-8000-000000000001", rows)
    progress = _transcript_progress(folder)
    assert progress["status"] == status and progress["activity"] == activity
    if status == "partial":
        assert progress["invalid"] == 1


def test_the_live_reader_carries_integrity_and_progress_through_the_allowlist(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "0e3b6f2a-0000-4000-8000-000000000002"
    _session(tmp_path, sid, [_phase("streaming_reasoning", 1, text="SECRET_REASONING_CANARY"),
                             _phase("streaming_reasoning", 7)])
    event = SimpleNamespace(requested_model="grok:default", session_id=sid)
    trace = _live_call_trace(event)
    assert trace["integrity"]["complete"] and trace["progress"]["activity"] == "reasoning"
    obs = recovery.safe_observations(trace, project_root=tmp_path)
    assert obs["status"] == "observed" and obs["progress"]["longest_gap_seconds"] == 6.0
    assert obs["progress"]["labels"] == {"streaming_reasoning": 2}
    assert "SECRET_REASONING_CANARY" not in json.dumps(obs), "counts and labels only, never content"


def test_a_label_or_time_that_is_not_an_identifier_never_reaches_the_record():
    progress = recovery._safe_progress(dict(status="observed", events=2, labels={"ok_label": 1, "rm -rf /": 1},
                                            first_at="yesterday", last_at="2026-10-31T20:49:01+00:00",
                                            activity="lying", span_seconds=-1))
    assert progress["labels"] == {"ok_label": 1} and progress["first_at"] is None
    assert progress["activity"] is None and progress["span_seconds"] is None


def test_integrity_and_progress_agree_on_a_grok_folder_missing_its_event_log(tmp_path):
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "chat_history.jsonl").write_text('{"type": "user"}\n')
    assert not _transcript_integrity(folder)["complete"]
    assert _transcript_progress(folder)["status"] == "unavailable"


def test_a_claude_transcript_file_carries_no_progress_section(monkeypatch, tmp_path):
    """Non-Grok records are unchanged: a transcript file's rows name tool
    use inside their content, which label counts would misread."""
    from types import SimpleNamespace

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": []}}) + "\n")
    monkeypatch.setattr("quadratus.trace.locate", lambda *a, **k: transcript)
    trace = _live_call_trace(SimpleNamespace(requested_model="claude:opus", session_id="abc"))
    assert trace is not None and "integrity" in trace and "progress" not in trace
