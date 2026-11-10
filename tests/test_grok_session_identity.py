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

T0_MS = 1_793_400_000_000  # 2026-10-31, epoch milliseconds


def _phase(phase, second, **extra):
    """The retained grok 1.0.62 schema (root's review of 56ab825): a
    ``phase_changed`` event with the phase and ``ts``."""
    return json.dumps(dict(type="phase_changed", phase=phase, ts=T0_MS + second * 1000, **extra))


def _event(kind, second, **extra):
    return json.dumps(dict(type=kind, ts=T0_MS + second * 1000, **extra))


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
    ([_event("turn_started", 0), _phase("streaming_reasoning", 1), _event("tool_completed", 3, outcome="success",
                                                                           tool_call_id="g1")],
     "observed", "tool_use"),
    ([_phase("streaming_reasoning", 1), '{"type": "phase_changed", "phase": "streaming_reaso'], "partial",
     "reasoning"),
    ([_event("turn_started", 0), _event("loop_started", 0), _phase("waiting_for_model", 1)], "observed", "waiting"),
    ([], "unavailable", "no_events"),
], ids=["tools", "truncated-tail", "waiting-no-output", "empty"])
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


def test_the_retained_schema_reads_as_the_failing_session_did(tmp_path):
    """The shape root found in the retained grok 1.0.62 session: turn and
    loop start, waiting for the model, first token, then streaming
    reasoning, with ``ts`` and no tool event."""
    rows = [_event("turn_started", 0), _event("loop_started", 0), _phase("waiting_for_model", 0),
            _event("first_token", 2)] + [_phase("streaming_reasoning", 2 + n) for n in range(0, 12, 3)]
    progress = _transcript_progress(_session(tmp_path, "0e3b6f2a-0000-4000-8000-0000000000aa", rows))
    assert progress["labels"] == {"first_token": 1, "loop_started": 1, "streaming_reasoning": 4, "turn_started": 1,
                                  "waiting_for_model": 1}
    assert progress["activity"] == "reasoning" and progress["last_phase"] == "streaming_reasoning"
    assert progress["tool_events"] == 0 and progress["timing"] == "observed" and progress["longest_gap_seconds"] == 3.0


@pytest.mark.parametrize("row", [
    {"type": "phase_changed", "phase": "SECRET_NATIVE_EVENT_LABEL_993"},
    {"type": "SECRET_NATIVE_EVENT_LABEL_993"},
    {"type": "phase_changed", "phase": ["SECRET_NATIVE_EVENT_LABEL_993"]},
], ids=["unknown-phase", "unknown-type", "non-string-phase"])
def test_an_unknown_identifier_shaped_label_is_counted_and_never_kept(monkeypatch, tmp_path, row):
    from types import SimpleNamespace
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "0e3b6f2a-0000-4000-8000-0000000000bb"
    _session(tmp_path, sid, [_phase("streaming_reasoning", 1), json.dumps(dict(row, ts=T0_MS + 2000))])
    obs = recovery.safe_observations(_live_call_trace(SimpleNamespace(requested_model="grok:default", session_id=sid)),
                                     project_root=tmp_path)
    assert "SECRET_NATIVE_EVENT_LABEL_993" not in json.dumps(obs)
    other = "other_type" if "phase" not in row else "other_phase"
    assert obs["progress"]["labels"] == {"streaming_reasoning": 1, other: 1} and obs["progress"]["events"] == 2


@pytest.mark.parametrize("ts", [float("inf"), float("-inf"), float("nan"), 1e308, -5, 0, True, "yesterday",
                                "2026-10-31T20:49:01", "9999-12-31T23:59:59+00:00", "2026-13-40T00:00:00Z",
                                {"at": 1}],
                         ids=["inf", "-inf", "nan", "1e308", "negative", "zero", "bool", "word", "naive", "far",
                              "impossible", "object"])
def test_an_unreadable_time_keeps_the_counts_and_marks_timing_partial(tmp_path, ts):
    rows = [_phase("streaming_reasoning", 1), json.dumps({"type": "phase_changed", "phase": "streaming_reasoning",
                                                           "ts": ts}), _phase("streaming_reasoning", 4)]
    progress = _transcript_progress(_session(tmp_path, "0e3b6f2a-0000-4000-8000-0000000000cc", rows))
    assert progress["events"] == 3 and progress["reasoning_events"] == 3 and progress["status"] == "observed"
    assert progress["timing"] == "partial" and progress["timing_invalid"] == 1
    assert progress["span_seconds"] == 3.0, "only the readable times are used"
    safe = recovery._safe_progress(progress)
    assert safe["timing"] == "partial" and safe["first_at"] and safe["first_at"].endswith("+00:00")


@pytest.mark.parametrize("ts", ["1" + "0" * 400, "-" + "1" + "0" * 400, str(10 ** 30)],
                         ids=["10**400", "-10**400", "10**30"])
def test_an_integer_time_too_large_for_a_float_is_unreadable_timing_not_an_error(tmp_path, ts):
    """Root's review of e8f3a40: a valid JSON integer ``ts`` of 10**400
    raised OverflowError before it was range-checked."""
    rows = [_phase("streaming_reasoning", 1), '{"type": "phase_changed", "phase": "streaming_reasoning", "ts": '
            + ts + "}", _phase("streaming_reasoning", 4)]
    progress = _transcript_progress(_session(tmp_path, "0e3b6f2a-0000-4000-8000-0000000000ee", rows))
    assert progress["events"] == 3 and progress["timing"] == "partial" and progress["timing_invalid"] == 1
    assert progress["span_seconds"] == 3.0


def test_the_record_boundary_takes_huge_integer_durations_as_unknown():
    for value in (10 ** 400, -(10 ** 400), MAX_SECONDS + 1):
        safe = recovery._safe_progress(dict(span_seconds=value, longest_gap_seconds=value))
        assert safe["span_seconds"] is None and safe["longest_gap_seconds"] is None, value
    assert recovery._safe_progress(dict(span_seconds=MAX_SECONDS))["span_seconds"] == float(MAX_SECONDS)


MAX_SECONDS = recovery.MAX_PROGRESS_SECONDS


def test_infinity_in_the_only_time_leaves_no_timing_and_does_not_raise(tmp_path):
    rows = ['{"type": "phase_changed", "phase": "streaming_reasoning", "ts": Infinity}']
    progress = _transcript_progress(_session(tmp_path, "0e3b6f2a-0000-4000-8000-0000000000dd", rows))
    assert progress["events"] == 1 and progress["timing"] == "unavailable" and progress["first_at"] is None
    assert progress["span_seconds"] is None and progress["activity"] == "reasoning"


def test_the_record_boundary_refuses_labels_times_and_durations_it_cannot_vouch_for():
    progress = recovery._safe_progress(dict(
        status=["observed"], timing="observed", events=2, labels={"ok_label": 1, "streaming_reasoning": 2,
                                                                  "SECRET_NATIVE_EVENT_LABEL_993": 1},
        first_at="yesterday", last_at="2026-10-31T20:49:01-05:00", activity="lying", last_phase="SECRET",
        span_seconds=float("inf"), longest_gap_seconds=float("nan")))
    assert progress["labels"] == {"streaming_reasoning": 2}
    assert progress["status"] == "unavailable" and progress["first_at"] is None and progress["last_at"] is None
    assert progress["activity"] is None and progress["last_phase"] is None
    assert progress["span_seconds"] is None and progress["longest_gap_seconds"] is None
    assert recovery._safe_progress(dict(span_seconds=1e12))["span_seconds"] is None, "bounded"


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
