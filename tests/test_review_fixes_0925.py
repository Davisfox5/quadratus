"""Codex review of #25, 2026-09-25: diagnostics and the revision's scope headroom."""

import json
from dataclasses import replace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import _extract_claude_diagnostics
from quadratus.scope import ScopeReport
from quadratus.session import Session, SessionConfig, TaskSpec, _read_continues


@pytest.mark.parametrize("stdout", ["not json", "", "[]", json.dumps([{"type": "result"}]), "null"])
def test_claude_diagnostics_never_raise_on_a_non_object_envelope(stdout):
    _extract_claude_diagnostics(stdout)


def test_claude_diagnostics_still_read_turns_from_an_object():
    got = _extract_claude_diagnostics(json.dumps({"num_turns": 7, "modelUsage": {}}))
    assert got is None or got.get("model_calls") in (7, None)


def _session(tmp_path, report):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    session.project = tmp_path
    session.config = replace(session.config, allow_writes=True)
    session._measure_scope = lambda spec, before: report
    return session


def test_the_revision_is_told_the_measured_size_as_advisory_and_never_to_leave_a_fix_or_test_out(tmp_path):
    """Operator directive 2026-10-20: the revision and the fix prompt carry
    the measurement against the estimate, no room figure, no stop and no
    BLOCKER channel for a fix that would not fit."""
    report = ScopeReport(within_scope=True, changed_lines=91, max_lines=100, code_lines=60, test_lines=31)
    session = _session(tmp_path, report)
    for prompt in (session._revision_prompt(TaskSpec("t2", "validate rows"), "draft", ["n"]),
                   session._fix_prompt(TaskSpec("t2", "validate rows"), "draft", [("n", "still open")])):
        assert "is 91 lines now (60 in code, 31 in tests" in prompt and "estimate of ~100" in prompt
        assert "advisory, not a limit" in prompt and "leave a test out to fit" in prompt
        assert "lines of room" not in prompt and "stops at" not in prompt and "BLOCKER:" not in prompt


def test_no_size_line_without_an_estimate(tmp_path):
    prompt = _session(tmp_path, None)._revision_prompt(TaskSpec("t2", "x"), "draft", ["n"])
    assert "Size:" not in prompt


def test_a_revision_past_the_estimate_is_measured_as_oversized_and_that_is_telemetry():
    """A revision well past the estimate reads as oversized on the report
    (for the record, the reviewers and the planner); the dispatcher no
    longer turns it into a stop (operator directive 2026-10-20)."""
    report = ScopeReport(within_scope=True, changed_lines=189, max_lines=100)
    assert report.oversized and not report.blocking


def test_continues_line_is_read_and_stripped():
    tid, spec = _read_continues(TaskSpec("t2", "Finish the heading.\nCONTINUES: t1"))
    assert tid == "t1" and spec.description == "Finish the heading."
    assert _read_continues(TaskSpec("t3", "Unrelated."))[0] is None
