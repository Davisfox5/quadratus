"""Rounds per call, measured and shown (Davis, 2026-11-05).

grok-worker-01's 40 calls recorded no model turns at all: claude's single-seat
envelopes went through ``_ending_only``, which dropped the turn count, the
cached-input subset and the vendor's stated cost; codex reports no turn count.
Claude now keeps its own figures, codex's tool steps are counted from its
event stream, and the monitor shows where the tokens went by role and by call.
"""
import json

import pytest

from quadratus import monitor
from quadratus.cli_providers import (
    CODEX_TOOL_ITEMS,
    _extract_claude_diagnostics,
    _extract_codex_diagnostics,
)
from quadratus.delegation import InvocationEvent, safe_diagnostics


def test_a_single_seat_claude_envelope_keeps_its_turns_cache_and_cost():
    usage = {"input_tokens": 12, "output_tokens": 30, "cache_read_input_tokens": 40000}
    out = json.dumps({"type": "result", "result": "ok", "num_turns": 9, "total_cost_usd": 0.42, "usage": usage,
                      "modelUsage": {"claude-opus": {"inputTokens": 40012, "outputTokens": 30}}})
    got = _extract_claude_diagnostics(out)
    assert got == {"model_calls": 9, "cached_input_tokens": 40000, "vendor_cost_usd": 0.42}
    assert safe_diagnostics(got) == got, "every kept key is one the ledger's whitelist already admits"


def _codex(*items, completed=True):
    events = [{"type": "thread.started", "thread_id": "x"}, {"type": "turn.started"}]
    events += [{"type": "item.completed", "item": {"id": f"i{n}", **item}} for n, item in enumerate(items)]
    if completed:
        events.append({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}})
    return "\n".join(json.dumps(e) for e in events)


def test_codex_tool_steps_are_counted_by_item_type_and_zero_is_measured():
    out = _codex({"type": "reasoning", "text": "r"}, {"type": "command_execution", "command": "pytest -q"},
                 {"type": "file_change", "changes": []}, {"type": "web_search", "query": "q"},
                 {"type": "mcp_tool_call"}, {"type": "agent_message", "text": "done"}, {"type": "todo_list"})
    assert _extract_codex_diagnostics(out)["tool_steps"] == len(CODEX_TOOL_ITEMS)
    assert _extract_codex_diagnostics(_codex({"type": "agent_message", "text": "hi"}))["tool_steps"] == 0
    assert "tool_steps" not in (_extract_codex_diagnostics("not json\n") or {}), "no stream, no figure"
    noisy = _codex({"type": "command_execution"}) + "\n{broken\n" + json.dumps({"type": "item.completed", "item": 3})
    assert _extract_codex_diagnostics(noisy)["tool_steps"] == 1
    assert safe_diagnostics({"tool_steps": 4}) == {"tool_steps": 4}
    assert safe_diagnostics({"tool_steps": -1, "model_calls": True}) == {}
    assert InvocationEvent(task="t1", role="lead", origin="seat", requested_model="openai:sol",
                           tool_steps=3).tool_steps == 3


def _ledger(tmp_path, rows):
    run = tmp_path / "run"
    run.mkdir()
    (run / "invocations.jsonl").write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n")
    return run


def _row(task, role, seat, inp, out, **kw):
    return dict(dict(task=task, role=role, requested_model=seat, invoked=True, input_tokens=inp, output_tokens=out,
                     seconds=10.0), **kw)


def test_the_breakdown_sums_by_role_and_keeps_unknowns_unknown(tmp_path):
    run = _ledger(tmp_path, [
        dict(task="t1", role="lead", requested_model="openai:sol", invoked=False, selected=True),
        _row("t1", "lead", "openai:sol", 90_000, 1_000, tool_steps=6, cached_input_tokens=80_000),
        _row("t1", "collaborator", "claude:opus", 120_000, 3_000, model_turns=8),
        _row("t2", "collaborator", "claude:opus", 60_000, 2_000, model_turns=4),
        _row("t2", "lead", "claude:opus", None, None, model_turns=None),
        "{not json",
    ])
    got = monitor.call_breakdown(run)
    assert len(got["rows"]) == 4, "selection rows are not calls"
    assert "1 malformed line(s) skipped" in got["problem"]
    by = {a["role"]: a for a in got["by_role"]}
    assert by["collaborator"]["tokens"] == 185_000 and by["collaborator"]["turns"] == 12
    assert by["collaborator"]["input_per_turn"] == 180_000 // 12
    assert by["lead"]["tokens"] == 91_000 and by["lead"]["unknown_tokens"] == 1
    assert by["lead"]["tool_steps"] == 6 and by["lead"]["calls_with_steps"] == 1 and by["lead"]["turns"] == 0
    assert [a["role"] for a in got["by_role"]] == ["collaborator", "lead"], "largest first"
    unknown = next(r for r in got["rows"] if r["task"] == "t2" and r["role"] == "lead")
    assert unknown["tokens"] is None and unknown["input_per_turn"] is None, "unknown is not zero"


def test_the_breakdown_is_shown_in_text_markdown_and_html_and_escaped(tmp_path):
    run = _ledger(tmp_path, [_row("t1", "<b>lead</b>", "openai:sol", 50_000, 500, tool_steps=2),
                             _row("t1", "recheck", "claude:opus", 62_000, 300, model_turns=3)])
    status = {"breakdown": monitor.call_breakdown(run), "unknown": {}, "read_at": "now", "project": "p",
              "live": False, "liveness": "x", "run_id": "r", "started": "s", "last_write": "w", "cell": "c",
              "task": "t", "stage": "st", "seat": "se", "role": "ro", "calls": 2, "tokens_reported": 112_800,
              "max_reported_tokens": 6_000_000, "reserve_tokens_per_call": 1, "max_tokens_per_call": 1,
              "last_event": "e", "last_call_ended": "l", "finished": False}
    text = monitor.render_text(status)
    assert "Where the tokens went, by role" in text and "Every call:" not in text
    assert "Every call:" in monitor.render_text(status, calls=True)
    assert "62,000" in monitor.render_text(status, calls=True) and "20,666" in text
    markdown = monitor.render_markdown(status)
    assert "#### Where the tokens went, by role" in markdown and "<details><summary>Every call</summary>" in markdown
    page = monitor.render_breakdown_html(status["breakdown"])
    assert "&lt;b&gt;lead&lt;/b&gt;" in page and "<b>lead</b>" not in page
    assert monitor.render_breakdown_html(None) == "" and monitor.render_breakdown_html({"rows": []}) == ""


def test_an_absent_or_unreadable_ledger_says_so_and_never_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert monitor.call_breakdown(empty) == {"rows": [], "by_role": [], "problem": "invocations.jsonl is absent",
                                             "partial": False}
    (empty / "invocations.jsonl").mkdir()
    assert monitor.call_breakdown(empty)["rows"] == []


def test_read_status_carries_the_breakdown_and_the_cli_flag_needs_monitor(tmp_path, capsys):
    from quadratus import cli
    project = tmp_path / "project"
    runs = project / ".quadratus" / "runs" / "20261105T120000Z-abcd1234"
    runs.mkdir(parents=True)
    (runs / "invocations.jsonl").write_text(json.dumps(_row("t1", "lead", "openai:sol", 10, 1, tool_steps=1)) + "\n")
    status = monitor.read_status(project)
    assert status["breakdown"]["rows"][0]["tool_steps"] == 1
    assert cli.main(["--monitor", "--calls", "--project", str(project)]) == 0
    assert "Every call:" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--calls", "goal"])
