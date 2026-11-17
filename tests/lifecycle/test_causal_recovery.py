"""Causal recovery accounting (root on 113f4e1, 6549956979 item 3).

The terminal record of the P16 run blamed the OpenAI orchestrator, with zero
calls, for a stop the Grok lead caused: the lead's 900 s transport timeout
left no usage, the budget latched ``unknown_usage``, and the next admission
it refused was the orchestrator's replan. The newest invoked ledger row was
the right call, but the terminal path took its identity from the refused
admission and read no trace.

Each unknown-usage settlement is now named by its budget operation id, the
same id the ledger row carries, and the terminal occurrence binds its
cause(s) by that id. The refused admission is kept apart as
``stopped_call`` (never invoked, never a cost). The failed editing call has
its own occurrence with what it left on disk and its native progress.

Whole-controller replays below the vendor CLIs: scripted replies prove
routing and accounting, never model behaviour.
"""
import json
import subprocess

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, T1, T2, Script

SIMPLE_T1 = "KIND: backend simple\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."
STDERR = "STDERR_CANARY_7731: grok: stream stalled"
REASONING = "REASONING_CANARY_8842"
PROMPT = "PROMPT_CANARY_9953: implement add"


T0_MS = 1_793_400_000_000  # 2026-10-31, epoch milliseconds


def _phase(phase, second):
    """The retained grok 1.0.62 schema: a phase_changed event with ``ts``."""
    return json.dumps(dict(type="phase_changed", phase=phase, ts=T0_MS + second * 1000, text=REASONING))


def _native(home, sid, rows):
    folder = home / ".grok" / "sessions" / "%2Fwork" / sid
    folder.mkdir(parents=True)
    (folder / "events.jsonl").write_text("\n".join(rows) + "\n")
    (folder / "chat_history.jsonl").write_text(json.dumps({"type": "user", "content": PROMPT}) + "\n")


def _timed_out_lead(home, seen, rows, *, stderr=""):
    def lead(call, replay):
        assert call.vendor == "grok", "the Grok lead is selected explicitly (H.grok_lead_routing)"
        sid = call.argv[call.argv.index("--session-id") + 1]
        seen.setdefault("sids", []).append(sid)
        _native(home, sid, rows)
        H.write(call, {"app.py": FIXED, "tests/test_app.py": "unfinished\n"})
        raise subprocess.TimeoutExpired(call.argv, 900, output="", stderr=stderr)
    return lead


def _run(tmp_path, monkeypatch, lead, *decls, max_tasks=3):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return decls[n - 1] if n <= len(decls) else "DONE"
    return H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES,
                 max_tasks=max_tasks)


REASONING_ROWS = [_phase("streaming_reasoning", s) for s in range(0, 60, 3)]


def test_a_grok_timeout_is_the_cause_and_the_refused_replan_is_not_a_call(tmp_path, monkeypatch):
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    seen = {}
    replay = _run(tmp_path, monkeypatch, _timed_out_lead(tmp_path / "home", seen, REASONING_ROWS, stderr=STDERR),
                  SIMPLE_T1)
    result = H.result_json(replay)
    assert replay.result.error == "RunBudgetExceeded: Run stopped: unknown_usage"
    assert len(replay.of("lead")) == 1 and len(replay.of("orchestrator")) == 1, "no retry, no replan"
    assert (replay.project / "app.py").read_text() == FIXED, "partial writes kept"

    (failure,) = [r for r in result["recoveries"] if r["kind"] == "task"]
    assert failure["outcome_class"] == "provider_timeout" and failure["role"] == "lead"
    assert failure["seat"].startswith("grok:") and failure["decision"].startswith("hand_back")
    assert failure["effect"] == dict(inspected=True, changed_files=2, changed_lines=failure["effect"]["changed_lines"])
    assert failure["effect"]["changed_lines"] > 0
    call = failure["failed_call"]
    assert call["session_known"] and call["operation"] and call["tokens"] is None, "unknown stays unknown"
    assert call["stderr_chars"] == len(STDERR), "stderr is a count, never its text"
    progress = failure["observations"]["progress"]
    assert progress["status"] == "observed" and progress["activity"] == "reasoning"
    assert progress["events"] == 20 and progress["tool_events"] == 0 and progress["longest_gap_seconds"] == 3.0

    (stop,) = [r for r in result["recoveries"] if r["kind"] == "run"]
    assert stop["outcome_class"] == "unknown_usage" and stop["decision"].startswith("reconcile")
    assert stop["role"] == "lead" and stop["seat"] == failure["seat"] and stop["task"] == "t1", \
        "the cause is the Grok call, not the refused admission"
    (cause,) = stop["causes"]
    assert cause["operation"] == call["operation"] and cause["editing"] is True
    assert [o["opid"] for o in result["budget"]["unknown_usage_operations"]] == [call["operation"]]
    assert cause["call"]["invocation_id"] == call["invocation_id"]
    assert stop["stopped_call"]["role"] == "orchestrator" and stop["stopped_call"]["invoked"] is False
    assert stop["observations"]["progress"]["activity"] == "reasoning"

    families = sorted(p["family"] for p in result["improvement"]["proposals"])
    assert families == ["provider_timeout|lead|grok", "unknown_usage|lead|grok"], families
    cost = result["improvement"]["cost"]
    assert cost["calls"] == 1 and cost["calls_with_unknown_usage"] == 1 and cost["tokens_known"] is None, \
        "one call counted once across both records; the refused admission is no call; never zero"
    text = json.dumps(result["recoveries"]) + json.dumps(result["improvement"])
    for leaked in (STDERR, REASONING, PROMPT):
        assert leaked not in text, leaked


@pytest.mark.parametrize("rows,status", [
    (REASONING_ROWS[:5] + ['{"type": "phase", "phase": "streaming_reaso'], "partial"),
    (REASONING_ROWS[:3] + ["not json at all"] + REASONING_ROWS[3:5], "partial"),
], ids=["truncated-tail", "malformed-row"])
def test_damaged_native_progress_is_labelled_partial_never_observed(tmp_path, monkeypatch, rows, status):
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    replay = _run(tmp_path, monkeypatch, _timed_out_lead(tmp_path / "home", {}, rows), SIMPLE_T1)
    result = H.result_json(replay)
    (failure,) = [r for r in result["recoveries"] if r["kind"] == "task"]
    progress = failure["observations"]["progress"]
    assert progress["status"] == status and progress["invalid"] == 1 and progress["events"] == 5
    assert failure["failed_call"]["stderr_chars"] == 0


def test_a_later_success_cannot_steal_the_cause(tmp_path, monkeypatch):
    """t1's Grok lead succeeds, t2's times out: the newer successful calls of
    t1 (its review and close-out) never become the cause, and the stop
    names t2's lead."""
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\npartial\n"})
        raise subprocess.TimeoutExpired(call.argv, 900, output="", stderr="")

    decl_t2 = "KIND: docs simple\nSCOPE: " + json.dumps(T2) + "\nDocument add in README.md."
    replay = _run(tmp_path, monkeypatch, lead, SIMPLE_T1, decl_t2)
    result = H.result_json(replay)
    assert "unknown_usage" in (replay.result.error or ""), replay.result.error
    (stop,) = [r for r in result["recoveries"] if r["kind"] == "run"]
    (cause,) = stop["causes"]
    assert cause["task"] == "t2" and stop["task"] == "t2" and stop["role"] == "lead"
    assert stop["failed_call"]["task"] == "t2" and stop["failed_call"]["tokens"] is None


def test_a_normal_completion_writes_no_occurrence_and_grok_still_meters_from_its_envelope(tmp_path, monkeypatch):
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    def lead(call, replay):
        assert "--session-id" in call.argv
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    replay = _run(tmp_path, monkeypatch, lead, SIMPLE_T1)
    result = H.result_json(replay)
    assert replay.result.completed, replay.result.error
    assert not [r for r in result["recoveries"] if r["kind"] in ("task", "run")]
    assert result["budget"]["unknown_usage_operations"] == [] and result["budget"]["unknown_usage_attempts"] == 0
    rows = [json.loads(line) for line in (replay.result.run_dir / "invocations.jsonl").read_text().splitlines()
            if line.strip()]
    grok = [r for r in rows if r.get("invoked") and str(r.get("requested_model", "")).startswith("grok:")]
    assert grok and all(r.get("input_tokens") is not None and r.get("operation") for r in grok)


@pytest.mark.requirements_ledger
def test_two_concurrent_timeouts_are_two_causes_each_counted_once(tmp_path, monkeypatch):
    """A PARALLEL batch whose two leads both time out while both are in
    flight: two settlements without usage, two causes on the one terminal
    record, each bound to its own row, and the cost counts each call once."""
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    import threading

    from tests.lifecycle.test_finish_capacity import BATCH
    home, gate = tmp_path / "home", threading.Barrier(2)

    def lead(call, replay):
        sid = call.argv[call.argv.index("--session-id") + 1] if "--session-id" in call.argv else None
        if sid:
            _native(home, sid, REASONING_ROWS[:4])
        H.write(call, {"app.py": FIXED} if call.task == "t1" else {"README.md": "# app\n\npartial\n"})
        try:
            gate.wait(timeout=10)  # both calls are admitted and running
        except threading.BrokenBarrierError:
            pass
        raise subprocess.TimeoutExpired(call.argv, 900, output="", stderr="")

    replay = _run(tmp_path, monkeypatch, lead, BATCH)
    result = H.result_json(replay)
    assert sorted(c.task for c in replay.of("lead")) == ["t1", "t2"], "each lead ran once, none retried"
    (stop,) = [r for r in result["recoveries"] if r["kind"] == "run"]
    assert stop["outcome_class"] == "unknown_usage", stop
    causes = stop["causes"]
    assert sorted(c["task"] for c in causes) == ["t1", "t2"] and all(c["role"] == "lead" for c in causes)
    assert len({c["operation"] for c in causes}) == 2
    assert all(c["call"] and c["call"]["tokens"] is None for c in causes), "each bound to its own row"
    assert len({c["call"]["invocation_id"] for c in causes}) == 2
    cost = result["improvement"]["cost"]
    assert cost["calls_with_unknown_usage"] == 2 and cost["tokens_known"] is None, cost
    leads = {c["call"]["invocation_id"] for c in causes}
    failures = [r for r in result["recoveries"] if r["kind"] == "task"]
    assert {r["failed_call"]["invocation_id"] for r in failures} == leads, "the task records name the same calls"
