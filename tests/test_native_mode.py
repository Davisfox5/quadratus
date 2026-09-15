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
