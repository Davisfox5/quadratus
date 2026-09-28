"""Native-delegation control on the OpenAI (codex) transport.

Every Quadratus codex call sends ``--disable multi_agent --disable
multi_agent_v2 -c agents.enabled=false`` and refuses an operator override
that would undo or hide it. The ``agents.enabled`` override is the actual
control: the 2026-09-15 live probe showed Sol spawning Sol with both feature
switches reading ``false``, and the codex source at ``rust-v0.154.0``
explains why (the model's declared multi-agent version outranks the feature
flags; only ``agents.enabled = false`` or an enabled ``multi_agent_v2``
outranks the model). Nothing here shells out: ``cli_providers._launch`` is faked, and the one
test that touches a real binary is opt-in and reads configuration only
(``codex features list``), never a model.

What these tests prove is *configuration*: the flags are on the argv and
cannot be out-ordered by ``QUADRATUS_CLI_ARGS_OPENAI``. That a live ``exec``
turn then cannot spawn a child is a separate claim, still owed to a bounded
probe, and is not asserted anywhere in this file.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import (
    CLAUDE_SPEC,
    CODEX_NATIVE_DELEGATION_CONTROL,
    CODEX_SPEC,
    GROK_SPEC,
    ClaudeCLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
    codex_override_conflicts,
)
from quadratus.providers import ProviderError
from quadratus.registry import resolve

CONTROL = ["--disable", "multi_agent", "--disable", "multi_agent_v2",
           "-c", "agents.enabled=false"]
INSTALLED_CODEX = shutil.which('codex')


@pytest.fixture(autouse=True)
def _binaries(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/x")
    monkeypatch.delenv("QUADRATUS_CLI_ARGS_OPENAI", raising=False)


def _codex(key=None, **kw):
    provider = CodexCLIProvider(model="gpt-5.6-sol", **kw)
    if key:
        spec = resolve(key)
        provider = provider.for_seat(spec.alias, effort=spec.effort, restricted=spec.restricted)
    return provider


def _control_positions(argv):
    return [i for i in range(len(argv) - 1) if argv[i] == "--disable"]


def _has_control(argv):
    joined = " ".join(argv)
    return ("--disable multi_agent " in joined + " " and "--disable multi_agent_v2" in joined
            and "-c agents.enabled=false" in joined)


# -- the control is on every permission mode ----------------------------------


def test_the_control_names_both_codex_switches_and_the_agents_override():
    assert CODEX_NATIVE_DELEGATION_CONTROL == CONTROL
    assert CODEX_SPEC.control_args == CONTROL


def test_the_agents_override_is_harness_owned_not_operator_supplied(monkeypatch):
    """An operator ``-c agents.enabled=false`` agrees with the control, but the
    agents table is refused wholesale: the harness sends its own, and the only
    reason to touch the table from .env is to re-admit something."""
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "-c agents.enabled=false")
    with pytest.raises(ProviderError, match="native sub-agents"):
        _codex()._build_argv("p", "s")
    monkeypatch.delenv("QUADRATUS_CLI_ARGS_OPENAI")
    argv = _codex()._build_argv("p", "s")
    assert argv.count("agents.enabled=false") == 1
    assert argv[argv.index("agents.enabled=false") - 1] == "-c"


def test_a_read_only_call_sends_the_control():
    argv = _codex()._build_argv("p", "s")
    assert _has_control(argv)
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_a_writable_call_sends_the_control_and_keeps_its_sandbox():
    argv = _codex(allow_writes=True)._build_argv("p", "s")
    assert _has_control(argv)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "read-only" not in argv


def test_a_granted_directory_view_sends_the_control(tmp_path):
    view = _codex().in_directory(tmp_path, allow_writes=True)
    argv = view._build_argv("p", "s")
    assert _has_control(argv)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"


def test_a_restricted_worker_seat_sends_the_control_and_keeps_effort():
    argv = _codex(key="openai:gpt-5.6-luna")._build_argv("p", "s")
    assert _has_control(argv)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "model_reasoning_effort=low" in argv
    assert "--skip-git-repo-check" in argv


@pytest.mark.parametrize("key", ["openai:gpt-5.6-sol", "openai:gpt-6-astra", "openai:gpt-5.6-terra"])
def test_every_senior_and_escalation_seat_sends_the_control(key):
    spec = resolve(key)
    argv = _codex(key=key)._build_argv("p", "s")
    assert _has_control(argv), key
    assert "--skip-git-repo-check" in argv
    if spec.effort:
        assert f"model_reasoning_effort={spec.effort}" in argv


def test_the_control_is_not_a_permission_grant():
    """Disabling spawning must not touch the write axis in either direction."""
    read_only = _codex()._build_argv("p", "s")
    writable = _codex(allow_writes=True)._build_argv("p", "s")
    assert "workspace-write" not in read_only
    assert "danger-full-access" not in read_only + writable


# -- benign overrides survive, in their old position ---------------------------


def test_benign_overrides_still_land_last(monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "-c model_reasoning_effort=high --profile team")
    argv = _codex()._build_argv("p", "s")
    assert argv[-4:] == ["-c", "model_reasoning_effort=high", "--profile", "team"]
    assert _has_control(argv)
    assert max(_control_positions(argv)) < argv.index("--profile")


def test_an_agreeing_override_is_not_a_conflict(monkeypatch):
    monkeypatch.setenv(
        "QUADRATUS_CLI_ARGS_OPENAI",
        "--disable multi_agent -c features.multi_agent=false -c features.other=true",
    )
    argv = _codex()._build_argv("p", "s")
    assert _has_control(argv)
    assert "features.other=true" in argv


def test_the_sandbox_override_that_already_worked_still_works(monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "--sandbox danger-full-access")
    argv = _codex()._build_argv("p", "s")
    assert argv.index("read-only") < argv.index("danger-full-access")
    assert _has_control(argv)


# -- conflicting overrides are refused, never out-ordered ----------------------

CONFLICTS = [
    "--enable multi_agent",
    "--enable=multi_agent",
    "--enable multi_agent_v2",
    "-c features.multi_agent=true",
    "-cfeatures.multi_agent=true",
    "-c=features.multi_agent=true",
    "--config features.multi_agent=true",
    "--config=features.multi_agent=true",
    "-c 'features.multi_agent = true'",
    "-c 'features.\"multi_agent\"=true'",
    "-c 'features={multi_agent=true}'",
    "-c features.multi_agent_v2=true",
    "-c features.multi_agent=1",
    "-c agents.enabled=true",
    "-c agents.max_threads=4",
    "-c 'agents={max_threads=4}'",
    "--",
    "--sandbox read-only -- --enable multi_agent",
]


@pytest.mark.parametrize("raw", CONFLICTS)
def test_a_conflicting_override_is_refused_before_launch(monkeypatch, raw):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", raw)
    launched = []
    monkeypatch.setattr(cli_providers, "_launch", lambda *a, **k: launched.append(a))
    with pytest.raises(ProviderError, match="native sub-agents"):
        _codex().generate("p")
    assert launched == []


@pytest.mark.parametrize("raw", CONFLICTS)
def test_a_conflict_is_refused_on_every_permission_mode(monkeypatch, raw):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", raw)
    for provider in (_codex(), _codex(allow_writes=True), _codex(key="openai:gpt-5.6-luna")):
        with pytest.raises(ProviderError):
            provider._build_argv("p", "s")


def test_the_refusal_names_the_key_but_not_the_value(monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_OPENAI", "-c 'agents.enabled=[\"secret-role\"]'")
    with pytest.raises(ProviderError) as caught:
        _codex()._build_argv("p", "s")
    assert "agents.enabled" in str(caught.value)
    assert "secret-role" not in str(caught.value)


def test_the_scanner_reports_only_the_offending_tokens():
    found = codex_override_conflicts(
        ["--profile", "x", "--enable", "multi_agent", "-c", "model=o3", "--"]
    )
    assert found == ["--enable multi_agent", "--"]
    assert codex_override_conflicts([]) == []
    assert codex_override_conflicts(["--enable"]) == []  # the CLI rejects it itself


def test_the_scanner_is_the_spec_hook():
    assert CODEX_SPEC.override_conflicts is codex_override_conflicts


# -- native telemetry survives the control --------------------------------------

STREAM_WITH_CHILD = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "parent-1"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "collab_tool_call",
                                        "tool": "spawn_agent", "receiver_thread_ids": ["child-1"]}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "done"}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
])


class _Completed:
    def __init__(self, stdout):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


def test_a_child_seen_despite_the_control_is_recorded_as_a_control_failure(monkeypatch, caplog):
    monkeypatch.setattr(cli_providers, "_launch", lambda *a, **k: _Completed(STREAM_WITH_CHILD))
    monkeypatch.setenv("CODEX_HOME", "/nonexistent")
    provider = _codex()
    assert provider.native_delegation_disabled
    assert provider.generate("p") == "done"
    assert provider.last_usage == {"input_tokens": 100, "output_tokens": 7}
    assert [c.session_id for c in provider.native_children] == ["child-1"]
    assert provider.native_children[0].detail.startswith("CONTROL FAILURE")
    assert "multi_agent" in provider.native_children[0].detail
    assert any("CONTROL FAILURE" in r.message for r in caplog.records)


def test_a_clean_stream_records_no_children(monkeypatch):
    clean = STREAM_WITH_CHILD.replace('"collab_tool_call"', '"reasoning"')
    monkeypatch.setattr(cli_providers, "_launch", lambda *a, **k: _Completed(clean))
    monkeypatch.setenv("CODEX_HOME", "/nonexistent")
    provider = _codex()
    provider.generate("p")
    assert provider.native_children == []


# -- the other vendors: gap documented, not closed ------------------------------


def test_only_codex_carries_a_control_today():
    assert not CLAUDE_SPEC.control_args and CLAUDE_SPEC.override_conflicts is None
    assert not GROK_SPEC.control_args and GROK_SPEC.override_conflicts is None
    assert not ClaudeCLIProvider(model="opus").native_delegation_disabled
    assert not GrokCLIProvider(model="").native_delegation_disabled


def test_senior_claude_and_grok_seats_can_still_delegate_natively():
    """The gap, pinned so it cannot be forgotten: Task is denied only on a
    restricted claude seat, and grok's --always-approve overrides any denial
    of Agent. Closing either is a separate lane."""
    opus = ClaudeCLIProvider(model="opus")._build_argv("p", "s")
    assert "Task" not in opus[opus.index("--disallowed-tools") + 1]
    grok = GrokCLIProvider(model="")._build_argv("p", "s")
    assert "--always-approve" in grok and "--disallowed-tools" not in grok


# -- opt-in configuration probe against the installed binary --------------------


@pytest.mark.skipif(
    not (INSTALLED_CODEX and os.environ.get("QUADRATUS_LIVE_CODEX_FEATURES")),
    reason="set QUADRATUS_LIVE_CODEX_FEATURES=1 with codex on PATH; reads config, no model call",
)
def test_installed_codex_reports_both_switches_off_even_under_enable():
    """``features list`` reads the feature flags only. It does not show
    ``agents.enabled`` and, per the 2026-09-15 finding, ``false`` here does not
    mean the tools are absent; the tool-list probe in the log is the check."""
    argv = [INSTALLED_CODEX, *CONTROL, "--enable", "multi_agent",
            "--enable", "multi_agent_v2", "features", "list"]
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True).stdout
    rows = {line.split()[0]: line.split()[-1] for line in out.splitlines() if line.strip()}
    assert rows["multi_agent"] == "false" and rows["multi_agent_v2"] == "false"


@pytest.mark.parametrize('subcommand', ['resume', 'fork'])
def test_overrides_cannot_reuse_previous_conversation(monkeypatch, subcommand):
    monkeypatch.setenv('QUADRATUS_CLI_ARGS_OPENAI', subcommand + ' --last')
    with pytest.raises(ProviderError):
        _codex()._build_argv('fresh task', 'role')
