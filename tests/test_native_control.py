"""Vendor-native delegation stays off where it can be, and never off by accident.

On 2026-09-13 a Sol review call spawned a second Sol through the Codex CLI's
own ``spawn_agent``; the child never touched WorkerPool, its budget, or the
ledger. These tests pin the transport-level control: Codex seats always send
the documented switch, an operator override that would undo it is refused
before launch, the optional run-wide switch also denies the claude and grok
sub-agent tools on agentic seats without disturbing their other permissions,
and the native telemetry that would expose a failed control keeps working.
"""

from __future__ import annotations

import json

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
    NativeControlOverride,
    _extract_native_children,
    native_delegation_mode,
)
from quadratus.providers import ProviderError


@pytest.fixture(autouse=True)
def _cli_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    monkeypatch.delenv("QUADRATUS_NATIVE_DELEGATION", raising=False)


def _codex(**kw):
    return CodexCLIProvider(model="gpt-5.6-sol", **kw)


def _pair(argv, flag):
    return argv[argv.index(flag) + 1]


# -- codex: always off ---------------------------------------------------------


@pytest.mark.parametrize("allow_writes,restricted", [(False, False), (True, False), (False, True)])
def test_every_codex_seat_sends_multi_agent_off(allow_writes, restricted):
    provider = _codex(allow_writes=allow_writes).for_seat("gpt-5.6-sol", restricted=restricted)
    argv = provider._build_argv("prompt", "")
    pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1)]
    assert ("-c", "features.multi_agent=false") in pairs
    # The seat's other permissions are untouched.
    if restricted or not allow_writes:
        assert _pair(argv, "--sandbox") == "read-only"
    else:
        assert _pair(argv, "--sandbox") == "workspace-write"
    assert "--skip-git-repo-check" in argv


@pytest.mark.parametrize("override", [
    "-c features.multi_agent=true",
    "-c features.multi_agent=1",
    '-c "features.multi_agent = true"',
    "--enable-multi-agent",
    "--multi_agent",
])
def test_an_operator_override_that_re_enables_delegation_is_refused(monkeypatch, override):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", override)
    launched = []
    monkeypatch.setattr(cli_providers, "_launch", lambda *a, **k: launched.append(a))
    with pytest.raises(NativeControlOverride) as caught:
        _codex()._build_argv("prompt", "")
    assert isinstance(caught.value, ProviderError)
    assert "QUADRATUS_CLI_ARGS_OPENAI" in str(caught.value)
    assert launched == [], "refused before the process starts"


def test_a_redundant_off_override_is_allowed(monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "-c features.multi_agent=false")
    argv = _codex()._build_argv("prompt", "")
    assert argv.count("features.multi_agent=false") == 2


def test_unrelated_operator_args_still_go_last(monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "--profile fast")
    argv = _codex()._build_argv("prompt", "")
    assert argv[-2:] == ["--profile", "fast"]
    assert argv.index("features.multi_agent=false") < argv.index("--profile")


# -- the run-wide switch -------------------------------------------------------


def test_default_mode_leaves_claude_and_grok_senior_seats_alone():
    claude = ClaudeCLIProvider(model="opus", allow_writes=False)._build_argv("p", "")
    assert _pair(claude, "--disallowed-tools") == "Bash Edit Write NotebookEdit"
    claude_writer = ClaudeCLIProvider(model="opus", allow_writes=True)._build_argv("p", "")
    assert "--disallowed-tools" not in claude_writer
    grok = GrokCLIProvider(model="", allow_writes=True)._build_argv("p", "")
    assert "--always-approve" in grok and "--disallowed-tools" not in grok


def test_off_mode_folds_task_into_claude_denials_without_dropping_the_others(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    readonly = ClaudeCLIProvider(model="opus", allow_writes=False)._build_argv("p", "")
    assert readonly.count("--disallowed-tools") == 1
    assert _pair(readonly, "--disallowed-tools").split() == ["Bash", "Edit", "Write", "NotebookEdit", "Task"]
    writer = ClaudeCLIProvider(model="opus", allow_writes=True)._build_argv("p", "")
    assert _pair(writer, "--disallowed-tools") == "Task"
    assert "Edit" not in _pair(writer, "--disallowed-tools"), "the write grant survives"


def test_off_mode_does_not_double_up_the_restricted_seat(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    argv = ClaudeCLIProvider(model="haiku").for_seat("haiku", restricted=True)._build_argv("p", "")
    assert argv.count("--disallowed-tools") == 1
    assert _pair(argv, "--disallowed-tools").split().count("Task") == 1


def test_off_mode_asks_grok_to_deny_agent_but_keeps_its_approval(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    argv = GrokCLIProvider(model="", allow_writes=True)._build_argv("p", "")
    assert "--always-approve" in argv
    assert _pair(argv, "--disallowed-tools") == "Agent"
    # The restricted worker already denies Agent; the fold must not repeat it.
    worker = GrokCLIProvider(model="").for_seat("", restricted=True)._build_argv("p", "")
    assert worker.count("--disallowed-tools") == 1
    assert _pair(worker, "--disallowed-tools") == "Agent"


def test_off_mode_changes_nothing_for_codex(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    on = _codex()._build_argv("p", "")
    monkeypatch.delenv("QUADRATUS_NATIVE_DELEGATION")
    assert _codex()._build_argv("p", "") == on


@pytest.mark.parametrize("value,expected", [("", "vendor-default"), ("vendor-default", "vendor-default"), ("OFF", "off")])
def test_mode_values(monkeypatch, value, expected):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", value)
    assert native_delegation_mode() == expected


def test_an_unknown_mode_is_refused_not_read_as_default(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "on")
    with pytest.raises(ProviderError):
        native_delegation_mode()
    with pytest.raises(ProviderError):
        ClaudeCLIProvider(model="opus")._build_argv("p", "")


# -- the control can fail; the record must still see it -------------------------


def test_native_telemetry_still_reports_a_spawn_that_got_through():
    stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "parent"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "collab_tool_call", "tool": "spawn_agent", "id": "item_1",
            "receiver_thread_ids": ["child-1"], "model": "gpt-5.6-sol"}}),
    ])
    children = _extract_native_children(stdout)
    assert children, "a spawned child must stay visible even when the switch is on"
    assert any("child-1" in (c.session_id or "") or "child" in str(c) for c in children)
