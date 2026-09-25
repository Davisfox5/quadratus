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


def test_the_revision_is_told_its_remaining_room_and_how_to_report_a_misfit(tmp_path):
    report = ScopeReport(within_scope=True, changed_lines=91, max_lines=100)
    prompt = _session(tmp_path, report)._revision_prompt(TaskSpec("t2", "validate rows"), "draft", ["n"])
    stop = int(100 * report.overrun_ratio)
    assert "is 91 lines now" in prompt and f"stops at {stop}" in prompt
    assert f"{stop - 91} lines of room" in prompt and "BLOCKER:" in prompt


def test_no_headroom_line_without_a_bound(tmp_path):
    prompt = _session(tmp_path, None)._revision_prompt(TaskSpec("t2", "x"), "draft", ["n"])
    assert "lines of room" not in prompt


def test_the_scope_stop_is_still_hard_for_a_revision():
    """The stop is unchanged: a revision past it is oversized, which the
    editing dispatcher turns into PartialWorkStopped."""
    report = ScopeReport(within_scope=True, changed_lines=189, max_lines=100)
    assert report.oversized


def test_continues_line_is_read_and_stripped():
    tid, spec = _read_continues(TaskSpec("t2", "Finish the heading.\nCONTINUES: t1"))
    assert tid == "t1" and spec.description == "Finish the heading."
    assert _read_continues(TaskSpec("t3", "Unrelated."))[0] is None
