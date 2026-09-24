"""Cloud Claude's run-wide policy tests, integrated with verified OpenAI controls."""
import pytest

from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
    native_delegation_mode,
)
from quadratus.config import Settings
from quadratus.delegation import invocation
from quadratus.providers import ProviderError
from quadratus.runtime import Fleet


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


#: Every documented route from one claude -p call to work outside itself.
CLAUDE_OFF_DENIALS = ["Task", "Agent", "Workflow", "SendMessage", "ListAgents",
                      "RemoteTrigger", "CronCreate", "mcp__*"]
GROK_OFF_DENIALS = ["Agent", "spawn_subagent", "workflow", "scheduler_create", "use_tool", "search_tool"]


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
    assert _pair(readonly, "--disallowed-tools").split() == [
        "Bash", "Edit", "Write", "NotebookEdit", *CLAUDE_OFF_DENIALS]
    writer = ClaudeCLIProvider(model="opus", allow_writes=True)._build_argv("p", "")
    assert _pair(writer, "--disallowed-tools").split() == CLAUDE_OFF_DENIALS
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
    # grok --help: comma-separated. A space-joined list would be one bogus name.
    assert _pair(argv, "--disallowed-tools") == ",".join(GROK_OFF_DENIALS)
    assert " " not in _pair(argv, "--disallowed-tools")
    # The restricted worker already denies Agent; the fold must not repeat it.
    worker = GrokCLIProvider(model="").for_seat("", restricted=True)._build_argv("p", "")
    assert worker.count("--disallowed-tools") == 1
    assert _pair(worker, "--disallowed-tools") == ",".join(GROK_OFF_DENIALS)
    assert _pair(worker, "--tools") == "read_file,grep,list_dir,web_search,web_fetch", \
        "the worker's read tools are untouched"


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
        "the call sent --disallowed-tools Agent spawn_subagent workflow scheduler_create "
        "use_tool search_tool; "
        "execution and usage unknown")
    assert grok._native_fanout_denied == GROK_OFF_DENIALS
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


# --- Off mode also closes the paths a denied tool name cannot ------------------


def _captured_launch(monkeypatch, stdout):
    from quadratus import cli_providers
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}

        class _Done:
            returncode = 0
            stderr = ""
        _Done.stdout = stdout
        return _Done()
    monkeypatch.setattr(cli_providers, "_launch", fake)
    return seen


def test_off_mode_sets_claude_kill_switches_in_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    seen = _captured_launch(monkeypatch, json.dumps({"type": "result", "result": "ok",
                                                     "usage": {"input_tokens": 1, "output_tokens": 1}}))
    ClaudeCLIProvider(model="opus", allow_writes=True, workdir=tmp_path).generate("p")
    assert seen["env"]["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
    assert seen["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "0"
    denied = _pair(seen["argv"], "--disallowed-tools").split()
    assert {"Workflow", "SendMessage", "ListAgents", "RemoteTrigger", "mcp__*"} <= set(denied)
    for kept in ("Bash", "Edit", "Write", "Read", "WebFetch", "TaskOutput", "ToolSearch"):
        assert kept not in denied


def test_default_mode_sets_no_kill_switches(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_WORKFLOWS", raising=False)
    seen = _captured_launch(monkeypatch, json.dumps({"type": "result", "result": "ok",
                                                     "usage": {"input_tokens": 1, "output_tokens": 1}}))
    ClaudeCLIProvider(model="opus", allow_writes=True, workdir=tmp_path).generate("p")
    assert "CLAUDE_CODE_DISABLE_WORKFLOWS" not in seen["env"]
    assert "--disallowed-tools" not in seen["argv"]


def test_a_denied_grok_meta_tool_is_read_as_attempted_delegation(monkeypatch):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    grok = GrokCLIProvider(model="", allow_writes=True)
    grok._build_argv("p", "")
    grok._observe_output(_grok_envelope(["read_file", "workflow"]))
    assert [c.session_id for c in grok.native_children] == ["unidentified:grok:workflow"]
    assert grok.native_children[0].detail.startswith("CONTROL FAILURE (suspected)")


def test_off_mode_sets_grok_workflow_kill_switch_and_comma_denials(monkeypatch, tmp_path):
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    seen = _captured_launch(monkeypatch, _grok_envelope(["read_file"]))
    GrokCLIProvider(model="", allow_writes=True, workdir=tmp_path).generate("p")
    assert seen["env"]["GROK_WORKFLOWS"] == "0"
    assert _pair(seen["argv"], "--disallowed-tools") == ",".join(GROK_OFF_DENIALS)
    assert _pair(seen["argv"], "--tools") if "--tools" in seen["argv"] else True


def test_fold_respects_the_vendor_separator():
    from quadratus.cli_providers import _fold_disallowed
    assert _fold_disallowed(["--disallowed-tools", "Agent"], "--disallowed-tools",
                            ["Agent", "workflow"], ",") == ["--disallowed-tools", "Agent,workflow"]
    assert _fold_disallowed(["x"], "--disallowed-tools", ["a", "b"], ",") == ["x", "--disallowed-tools", "a,b"]
    assert _fold_disallowed(["--disallowed-tools", "Bash Edit"], "--disallowed-tools",
                            ["Task", "Edit"]) == ["--disallowed-tools", "Bash Edit Task"]


def test_grok_off_mode_argv_is_pinned_byte_for_byte(monkeypatch):
    """The normal and restricted grok argv under off mode, exactly. A change
    here is a change to the live control and must be deliberate."""
    monkeypatch.setenv("QUADRATUS_NATIVE_DELEGATION", "off")
    normal = GrokCLIProvider(model="", allow_writes=True)._build_argv("p", "")
    assert normal[1:] == ["--output-format", "json", "--always-approve",
                          "--disallowed-tools",
                          "Agent,spawn_subagent,workflow,scheduler_create,use_tool,search_tool",
                          "--prompt-file", normal[-1]]
    worker = GrokCLIProvider(model="").for_seat("", restricted=True)._build_argv("p", "")
    assert worker[1:] == ["--output-format", "json",
                          "--tools", "read_file,grep,list_dir,web_search,web_fetch",
                          "--disallowed-tools",
                          "Agent,spawn_subagent,workflow,scheduler_create,use_tool,search_tool",
                          "-p", "p"]
    for argv in (normal, worker):
        assert " " not in argv[argv.index("--disallowed-tools") + 1]


def test_grok_default_mode_argv_is_unchanged_by_the_widening():
    normal = GrokCLIProvider(model="", allow_writes=True)._build_argv("p", "")
    assert "--disallowed-tools" not in normal
    worker = GrokCLIProvider(model="").for_seat("", restricted=True)._build_argv("p", "")
    assert worker[worker.index("--disallowed-tools") + 1] == "Agent"


# -- the verifier's own switch (Q9-v2, 2026-09-23) ------------------------------


def test_a_view_marked_fanout_off_denies_delegation_in_default_mode():
    view = ClaudeCLIProvider(model="opus", allow_writes=False)
    view.native_fanout_off = True
    argv = view._build_argv("p", "")
    assert argv.count("--disallowed-tools") == 1
    assert _pair(argv, "--disallowed-tools").split() == [
        "Bash", "Edit", "Write", "NotebookEdit", *CLAUDE_OFF_DENIALS]


def test_a_view_marked_fanout_off_still_refuses_operator_overrides(monkeypatch):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_CLAUDE", "--allowed-tools Task")
    view = ClaudeCLIProvider(model="opus", allow_writes=False)
    view.native_fanout_off = True
    with pytest.raises(ProviderError):
        view._build_argv("p", "")


def _captured_views(monkeypatch, tmp_path, *, project):
    views = []
    monkeypatch.setattr(Fleet, "_generate",
                        lambda self, key, provider, prompt, system, **kw: views.append(provider) or "ok")
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli"), project=tmp_path if project else None)
    return fleet, views


@pytest.mark.parametrize("project", [True, False])
def test_the_fleet_turns_delegation_off_for_the_verifier_only(monkeypatch, tmp_path, project):
    fleet, views = _captured_views(monkeypatch, tmp_path, project=project)
    with invocation("t2", "verifier"):
        fleet.invoke("claude:opus", "verify this")
    with invocation("t2", "lead"):
        fleet.invoke("claude:opus", "lead this")
    verifier, lead = views
    assert getattr(verifier, "native_fanout_off", False) is True
    assert _pair(verifier._build_argv("p", ""), "--disallowed-tools").split()[-len(CLAUDE_OFF_DENIALS):] \
        == CLAUDE_OFF_DENIALS
    assert getattr(lead, "native_fanout_off", False) is False, "senior seats keep delegation"
    assert getattr(fleet.provider_for("claude:opus"), "native_fanout_off", False) is False, \
        "the shared provider is never marked"
