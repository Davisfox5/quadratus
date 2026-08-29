"""Tests for the subscription-backed CLI providers.

Every test fakes ``subprocess.run``; nothing here shells out to a real vendor
CLI or touches the network.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from quadratus.cli_providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    _extract_codex_result,
)
from quadratus.config import Settings
from quadratus.providers import ProviderError, Turn, build_provider


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _claude_envelope(result, is_error=False):
    return json.dumps({"result": result, "is_error": is_error, "type": "result"})


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    return ClaudeCLIProvider(model="opus")


def test_available_when_binary_resolves(claude):
    assert claude.available()


def test_unavailable_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    p = ClaudeCLIProvider(model="opus")
    assert not p.available()
    with pytest.raises(ProviderError, match="not available"):
        p.generate("hi")


def test_extracts_result_from_json_envelope(claude, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeCompleted(_claude_envelope("the answer"))
    )
    assert claude.generate("question") == "the answer"


def test_error_envelope_is_not_retried(claude, monkeypatch):
    calls = []

    def fake_run(*a, **k):
        calls.append(1)
        return _FakeCompleted(_claude_envelope("refused to run", is_error=True))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ProviderError):
        claude.generate("question")
    # ProviderError is terminal: one attempt, no backoff loop.
    assert len(calls) == 1


def test_system_prompt_is_passed_as_a_flag_not_inlined(claude, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return _FakeCompleted(_claude_envelope("ok"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    claude.generate("do the thing", system="You are ARBITER.")

    argv = seen["argv"]
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "You are ARBITER."
    # The role must not also be folded into the user prompt, or it is stated twice.
    assert "Your role and instructions" not in seen["input"]


def test_readonly_tools_denied_by_default(claude, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(argv=argv), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("review this")
    assert "--disallowed-tools" in seen["argv"]


def test_allow_writes_opts_out_of_the_sandbox(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(argv=argv), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    ClaudeCLIProvider(model="opus", allow_writes=True).generate("build it")
    assert "--disallowed-tools" not in seen["argv"]


def test_runs_in_scratch_dir_not_cwd(claude, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(cwd=k.get("cwd")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("review this")
    assert seen["cwd"] and seen["cwd"] != "."
    claude.cleanup()


def test_api_keys_are_stripped_from_child_env(claude, monkeypatch):
    """An inherited key would silently divert the run onto billed transport."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(env=k.get("env")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("question")
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_every_vendors_key_is_stripped_not_just_anthropics(claude, monkeypatch):
    """The Grok keys were missed originally; all four vendors bill the same way."""
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.setenv(var, "sk-should-not-leak")
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(env=k.get("env")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("question")
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "XAI_API_KEY", "GROK_API_KEY"):
        assert var not in seen["env"], var


def test_history_is_rendered_into_the_prompt(claude, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(stdin=k.get("input")), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    claude.generate("continue", history=[Turn("user", "first"), Turn("assistant", "second")])
    assert "first" in seen["stdin"] and "second" in seen["stdin"]


def test_timeout_is_retried_then_surfaces(claude, monkeypatch):
    claude.retry_base_delay = 0.0
    claude.max_retries = 2
    attempts = []

    def fake_run(*a, **k):
        attempts.append(1)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ProviderError):
        claude.generate("question")
    assert len(attempts) == 2


def test_rate_limit_text_is_retryable(claude):
    assert claude._retryable(RuntimeError("exited 1: usage limit reached"))
    assert not claude._retryable(RuntimeError("exited 1: unknown flag --nope"))


def test_for_model_rebinds_without_rebuilding(claude, monkeypatch):
    cheap = claude.for_model("haiku")
    assert cheap.model == "haiku"
    assert claude.model == "opus"
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (seen.update(argv=argv), _FakeCompleted(_claude_envelope("ok")))[1],
    )
    cheap.generate("cheap task")
    assert seen["argv"][seen["argv"].index("--model") + 1] == "haiku"


def test_codex_prompt_goes_on_stdin(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return _FakeCompleted('{"last_agent_message": "codex answer"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = CodexCLIProvider(model="gpt-5.5-codex").generate("task", system="You are LEAD.")

    assert out == "codex answer"
    # No --system-prompt flag exists, so the role must be folded into the prompt.
    assert "You are LEAD." in seen["input"]
    assert "task" not in seen["argv"]


def test_prompt_never_trails_a_variadic_flag(claude, monkeypatch):
    """Regression: --disallowed-tools is variadic and swallowed a positional prompt.

    The live CLI failed with "Input must be provided either through stdin or as
    a prompt argument", so the prompt must go on stdin rather than argv.
    """
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: (
            seen.update(argv=argv, stdin=k.get("input")),
            _FakeCompleted(_claude_envelope("ok")),
        )[1],
    )
    claude.generate("the actual prompt")
    assert seen["stdin"] and "the actual prompt" in seen["stdin"]
    assert "the actual prompt" not in seen["argv"]


def test_codex_extractor_takes_the_last_message():
    stream = "\n".join(
        [
            '{"type": "start"}',
            '{"last_agent_message": "first draft"}',
            "not json at all",
            '{"last_agent_message": "final answer"}',
        ]
    )
    assert _extract_codex_result(stream) == "final answer"


def test_codex_extractor_falls_back_to_raw_stdout():
    assert _extract_codex_result("plain text reply") == "plain text reply"


# -- settings integration ----------------------------------------------------


def test_cli_backend_selected_by_default(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    p = build_provider("claude", Settings(backend="cli"))
    assert isinstance(p, ClaudeCLIProvider)


def test_api_backend_when_requested():
    s = Settings(backend="api", anthropic_api_key=None)
    p = build_provider("claude", s)
    assert not isinstance(p, ClaudeCLIProvider)


def test_per_provider_override_allows_mixing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    s = Settings(backend="cli", backend_overrides={"gemini": "api"})
    assert isinstance(build_provider("claude", s), ClaudeCLIProvider)
    assert s.backend_for("gemini") == "api"


def test_uses_cli_reports_subscription_transport():
    assert Settings(backend="cli").uses_cli()
    assert not Settings(backend="api", backend_overrides={}).uses_cli()


def test_model_for_returns_tiered_cli_alias():
    s = Settings(backend="cli")
    assert s.model_for("claude", "high") == "opus"
    assert s.model_for("claude", "low") == "haiku"


# -- public-sharing guard ----------------------------------------------------


def test_share_refused_on_cli_transport_even_when_opted_in(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.setenv("QUADRATUS_ALLOW_SHARE", "1")
    assert resolve_share(Settings(backend="cli")) is False


def test_share_allowed_on_pure_api_transport_when_opted_in(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.setenv("QUADRATUS_ALLOW_SHARE", "1")
    assert resolve_share(Settings(backend="api", backend_overrides={})) is True


def test_share_off_by_default(monkeypatch):
    from quadratus.gui import resolve_share

    monkeypatch.delenv("QUADRATUS_ALLOW_SHARE", raising=False)
    assert resolve_share(Settings(backend="api", backend_overrides={})) is False
