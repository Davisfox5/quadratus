"""Cloud Claude's run-wide policy tests, integrated with verified OpenAI controls."""
import pytest

from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
    native_delegation_mode,
)
from quadratus.providers import ProviderError


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)
    monkeypatch.delenv('QUADRATUS_NATIVE_DELEGATION', raising=False)


def _codex():
    return CodexCLIProvider('gpt-5.6-sol')


def _pair(argv, flag):
    return argv[argv.index(flag) + 1]


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
    assert _pair(readonly, "--disallowed-tools").split() == ["Bash", "Edit", "Write", "NotebookEdit", "Task", "Agent"]
    writer = ClaudeCLIProvider(model="opus", allow_writes=True)._build_argv("p", "")
    assert _pair(writer, "--disallowed-tools") == "Task Agent"
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



@pytest.mark.parametrize('vendor,provider', [('CLAUDE', ClaudeCLIProvider), ('GROK', GrokCLIProvider)])
def test_off_requests_cannot_be_overridden_by_extra_args(monkeypatch, vendor, provider):
    monkeypatch.setenv('QUADRATUS_NATIVE_DELEGATION', 'off')
    monkeypatch.setenv(f'QUADRATUS_CLI_ARGS_{vendor}', '--disallowed-tools ""')
    with pytest.raises(ProviderError, match='must be empty'):
        provider(model='')._build_argv('task', 'role')


# --- Evidence that a folded denial held, or did not -------------------------
#
# Codex reports its own children in its event stream. Claude and grok do not,
# so under off mode the only evidence is the envelope's own record: grok lists
# tool calls, claude lists permission denials. These pin what each is read as.

import json  # noqa: E402

from quadratus.cli_providers import ATTEMPTED_DELEGATION, _fold_disallowed  # noqa: E402
from quadratus.delegation import safe_diagnostics  # noqa: E402


def _grok_envelope(tools):
    return json.dumps({
        "text": "done", "stopReason": "end_turn", "modelCalls": 3,
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "toolCalls": [{"name": name} for name in tools],
    })


def test_grok_agent_call_after_the_denial_is_an_observed_child(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    grok = GrokCLIProvider(model="", allow_writes=True)
    grok._build_argv("p", "")
    assert grok.native_delegation_disabled
    grok._observe_output(_grok_envelope(["read_file", "Agent", "bash"]))
    assert [child.session_id for child in grok.native_children] == ["unidentified:grok:Agent"]
    child = grok.native_children[0]
    assert child.total_tokens is None, "the vendor reports nothing for the child"
    assert child.detail.startswith(
        "CONTROL FAILURE (suspected): a denied fan-out tool was attempted although "
        "the call sent --disallowed-tools Agent; execution and usage unknown")
    assert ATTEMPTED_DELEGATION in child.detail and " ran " not in child.detail


def test_a_cancelled_grok_turn_naming_agent_is_attempted_not_proven(monkeypatch, tmp_path):
    """A denied request and an executed one leave the same tool name in the
    envelope, so the record says attempted; the budget still stops."""
    from quadratus import cli_providers
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits

    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    cancelled = json.dumps({
        "text": "I'll delegate this to a sub-agent.", "stopReason": "cancelled",
        "modelCalls": 1, "usage": {"input_tokens": 40, "output_tokens": 8},
        "toolCalls": [{"name": "Agent", "arguments": {"prompt": "/Users/x/secret"}}],
    })

    class _Completed:
        stdout, stderr, returncode = cancelled, "", 0

    monkeypatch.setattr(cli_providers, "_launch", lambda *a, **k: _Completed())
    grok = GrokCLIProvider(model="", allow_writes=True, workdir=tmp_path)
    grok.run_budget = RunBudget(RunLimits(), path=tmp_path / "budget.json")
    with pytest.raises(ProviderError, match="cancelled"):
        grok.generate("do it")
    (child,) = grok.native_children
    assert child.session_id == "unidentified:grok:Agent" and child.total_tokens is None
    assert child.detail.startswith("CONTROL FAILURE (suspected)")
    assert "native child ran" not in child.detail
    assert "secret" not in child.detail
    assert grok.run_budget.snapshot()["stop_reason"] == "uncontrolled_native_delegation"
    with pytest.raises(RunBudgetExceeded):
        grok.run_budget.reserve()


def test_grok_ordinary_tools_after_the_denial_are_not_children(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    grok = GrokCLIProvider(model="", allow_writes=True)
    grok._build_argv("p", "")
    grok._observe_output(_grok_envelope(["read_file", "bash"]))
    assert grok.native_children == []


def test_grok_agent_call_in_default_mode_is_not_a_control_failure():
    """No denial was sent, so nothing failed; the default is the vendor's."""
    grok = GrokCLIProvider(model="", allow_writes=True)
    grok._build_argv("p", "")
    assert not grok.native_delegation_disabled
    grok._observe_output(_grok_envelope(["Agent"]))
    assert grok.native_children == []


def test_claude_denied_task_is_the_control_holding_not_a_child(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    claude = ClaudeCLIProvider(model="opus", allow_writes=True)
    claude._build_argv("p", "")
    assert claude.native_delegation_disabled
    claude._observe_output(json.dumps({
        "type": "result", "result": "ok", "session_id": "s1",
        "usage": {"input_tokens": 50, "output_tokens": 5},
        "permission_denials": [
            {"tool_name": "Task", "tool_use_id": "t1", "tool_input": {"prompt": "/Users/x/secret"}},
            {"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "rm -rf"}},
        ],
    }))
    assert claude.native_children == []
    assert claude.last_diagnostics == {"denied_tools": ["Task"]}
    assert safe_diagnostics(claude.last_diagnostics) == {"denied_tools": ["Task"]}
    assert "secret" not in json.dumps(claude.last_diagnostics)


def test_claude_envelope_without_denials_records_nothing(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    claude = ClaudeCLIProvider(model="opus", allow_writes=True)
    claude._build_argv("p", "")
    claude._observe_output(json.dumps({"type": "result", "result": "ok",
                                       "usage": {"input_tokens": 1, "output_tokens": 1}}))
    assert claude.native_children == [] and claude.last_diagnostics is None


def test_denial_is_reset_when_the_next_call_is_not_in_off_mode(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    grok = GrokCLIProvider(model="", allow_writes=True)
    grok._build_argv("p", "")
    monkeypatch.delenv("QUADRATUS_NATIVE_DELEGATION")
    grok._build_argv("p", "")
    assert not grok.native_delegation_disabled
    grok._observe_output(_grok_envelope(["Agent"]))
    assert grok.native_children == []


def test_fold_disallowed_with_a_valueless_trailing_flag_appends_a_pair():
    assert _fold_disallowed(["x", "--disallowed-tools"], "--disallowed-tools", ["Task"]) == \
        ["x", "--disallowed-tools", "--disallowed-tools", "Task"]
    assert _fold_disallowed(["--disallowed-tools", "Bash"], "--disallowed-tools", ["Task", "Bash"]) == \
        ["--disallowed-tools", "Bash Task"]


def test_safe_diagnostics_filters_denied_tools_like_attempted_tools():
    got = safe_diagnostics({"denied_tools": ["Task", "bad name", 3, "Agent", "Task"], "attempted_tools": ["bash"]})
    assert got == {"attempted_tools": ["bash"], "denied_tools": ["Task", "Agent"]}
    assert safe_diagnostics({"denied_tools": ["  "]}) == {}
