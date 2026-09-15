"""The summary-only call shape for closeout (2026-09-15).

Scored attempt 1 spent 258,413 tokens writing a record because the closeout
ran on the lead's full-agent seat and re-read the project eight times. The
caller (Fleet, Codex's lane) now takes the restricted seat, sets
``summary_only=True`` on a per-call view, gives it an empty directory, one
attempt and sixty seconds. These tests pin what the provider does with that:
per-vendor argv, every refusal, native-off forced, and that the default of
False leaves every other argv untouched.
"""
import json
import os

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
)
from quadratus.providers import ProviderError


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/x")
    for vendor in ("OPENAI", "CLAUDE", "GROK"):
        monkeypatch.delenv(f"QUADRATUS_CLI_ARGS_{vendor}", raising=False)
    monkeypatch.delenv("QUADRATUS_NATIVE_DELEGATION", raising=False)


def _summary(cls, model, tmp_path, **kw):
    provider = cls(model=model, workdir=str(tmp_path / "empty"), timeout=60, max_retries=1, **kw)
    view = provider.for_seat(model, effort="low", restricted=True)
    view.summary_only = True
    return view


def _pair(argv, flag):
    return argv[argv.index(flag) + 1]


def test_claude_summary_argv_is_tool_less_one_turn_and_native_off(tmp_path):
    argv = _summary(ClaudeCLIProvider, "opus", tmp_path)._build_argv("write the record", "")
    assert argv[argv.index("--tools") + 1] == ""
    assert _pair(argv, "--max-turns") == "1"
    denied = _pair(argv, "--disallowed-tools").split()
    assert {"Task", "Agent", "Workflow", "SendMessage", "ListAgents"} <= set(denied)
    assert "--effort" in argv and _pair(argv, "--effort") == "low"


def test_grok_summary_argv_is_one_turn_on_the_read_only_seat(tmp_path):
    argv = _summary(GrokCLIProvider, "", tmp_path)._build_argv("write the record", "")
    assert _pair(argv, "--max-turns") == "1"
    assert _pair(argv, "--tools") == "read_file,grep,list_dir,web_search,web_fetch"
    assert _pair(argv, "--disallowed-tools") == "Agent,spawn_subagent,workflow,scheduler_create,use_tool,search_tool"
    assert "--always-approve" not in argv
    assert argv[-2:] == ["-p", "write the record"]


def test_codex_summary_argv_keeps_read_only_and_controls_and_adds_nothing(tmp_path):
    view = _summary(CodexCLIProvider, "gpt-5.6-sol", tmp_path)
    argv = view._build_argv("write the record", "")
    assert _pair(argv, "--sandbox") == "read-only"
    assert "-c" in argv and "agents.enabled=false" in argv
    assert "--max-turns" not in argv and "--tools" not in argv
    plain = CodexCLIProvider(model="gpt-5.6-sol", workdir=str(tmp_path / "empty"), timeout=60,
                             max_retries=1).for_seat("gpt-5.6-sol", effort="low", restricted=True)
    assert argv == plain._build_argv("write the record", "")


def test_native_off_is_forced_and_its_environment_sent(monkeypatch, tmp_path):
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}

        class _Done:
            returncode, stderr = 0, ""
            stdout = json.dumps({"type": "result", "result": "SUMMARY: done",
                                 "usage": {"input_tokens": 10, "output_tokens": 5}})
        return _Done()
    monkeypatch.setattr(cli_providers, "_launch", fake)
    view = _summary(ClaudeCLIProvider, "opus", tmp_path)
    assert view.generate("write the record") == "SUMMARY: done"
    assert seen["env"]["CLAUDE_CODE_DISABLE_WORKFLOWS"] == "1"
    assert "Workflow" in _pair(seen["argv"], "--disallowed-tools").split()


def test_operator_overrides_are_refused_outright(monkeypatch, tmp_path):
    monkeypatch.setenv("QUADRATUS_CLI_ARGS_CLAUDE", "--verbose")
    with pytest.raises(ProviderError, match="must be empty for a summary-only call"):
        _summary(ClaudeCLIProvider, "opus", tmp_path)._build_argv("p", "")


def test_a_non_empty_directory_is_refused(tmp_path):
    view = _summary(ClaudeCLIProvider, "opus", tmp_path)
    os.makedirs(view.workdir, exist_ok=True)
    (tmp_path / "empty" / "leftover.py").write_text("x")
    with pytest.raises(ProviderError, match="empty working directory"):
        view._build_argv("p", "")


def test_more_than_one_attempt_or_over_a_minute_is_refused(tmp_path):
    view = _summary(ClaudeCLIProvider, "opus", tmp_path)
    view.max_retries = 2
    with pytest.raises(ProviderError, match="one attempt"):
        view._build_argv("p", "")
    view.max_retries = 1
    view.timeout = 61
    with pytest.raises(ProviderError, match="at most 60s"):
        view._build_argv("p", "")


@pytest.mark.parametrize("timeout", [None, 0, -1, float("nan"), float("inf"), -float("inf")])
def test_unbounded_or_nonpositive_timeout_is_refused(tmp_path, timeout):
    view = _summary(ClaudeCLIProvider, "opus", tmp_path)
    view.timeout = timeout
    with pytest.raises(ProviderError, match="at most 60s"):
        view._build_argv("p", "")


@pytest.mark.parametrize("attempts", [0, -1])
def test_zero_or_negative_attempt_count_is_refused(tmp_path, attempts):
    view = _summary(ClaudeCLIProvider, "opus", tmp_path)
    view.max_retries = attempts
    with pytest.raises(ProviderError, match="one attempt"):
        view._build_argv("p", "")


def test_the_unrestricted_seat_cannot_be_summary_only(tmp_path):
    provider = ClaudeCLIProvider(model="opus", workdir=str(tmp_path / "empty"), timeout=60, max_retries=1)
    provider.summary_only = True
    with pytest.raises(ProviderError, match="restricted seat form"):
        provider._build_argv("p", "")


def test_default_false_changes_nothing(tmp_path):
    plain = ClaudeCLIProvider(model="opus", workdir=str(tmp_path / "empty"))
    view = plain.for_seat("opus", effort="low", restricted=True)
    assert view.summary_only is False
    argv = view._build_argv("p", "")
    assert "--max-turns" not in argv and "--tools" not in argv
    assert plain.summary_only is False, "the cached provider is never mutated by a per-call view"
