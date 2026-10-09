
"""Tests for configuration loading."""

from __future__ import annotations

import pytest

from quadratus.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_GROK_MODEL,
    DEFAULT_OPENAI_MODEL,
    Settings,
)
from quadratus.registry import VENDORS


def test_defaults_when_env_unset(monkeypatch):
    for var in (
        "CLAUDE_MODEL",
        "OPENAI_MODEL",
        "GROK_MODEL",
        "ROUNDS",
        "PROVIDER_ORDER",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings.from_env()
    assert s.claude_model == DEFAULT_CLAUDE_MODEL
    assert s.openai_model == DEFAULT_OPENAI_MODEL
    assert s.grok_model == DEFAULT_GROK_MODEL
    assert s.rounds == 1
    assert s.provider_order == ["claude", "openai", "grok"]


def test_the_default_provider_order_is_exactly_the_lineup(monkeypatch):
    """A provider order naming a vendor with no roster is a run that spends a
    phase discovering it has nothing to call."""
    monkeypatch.delenv("PROVIDER_ORDER", raising=False)
    assert Settings.from_env().provider_order == list(VENDORS)


def test_subscription_transport_is_the_default(monkeypatch):
    """The whole point is spending windows that are already paid for; falling
    back to billed keys should be something the operator asks for."""
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    s = Settings.from_env()
    assert s.backend == "cli"
    assert all(s.backend_for(v) == "cli" for v in VENDORS)
    assert s.uses_cli()


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-custom")
    monkeypatch.setenv("ROUNDS", "3")
    monkeypatch.setenv("PROVIDER_ORDER", "grok, openai")
    s = Settings.from_env()
    assert s.claude_model == "claude-custom"
    assert s.rounds == 3
    assert s.provider_order == ["grok", "openai"]


def test_invalid_int_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ROUNDS", "not-a-number")
    s = Settings.from_env()
    assert s.rounds == 1


def test_rounds_floor_is_one(monkeypatch):
    monkeypatch.setenv("ROUNDS", "0")
    s = Settings.from_env()
    assert s.rounds == 1


def test_grok_key_aliases(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("GROK_API_KEY", "from-grok-var")
    assert Settings.from_env().xai_api_key == "from-grok-var"
    monkeypatch.setenv("XAI_API_KEY", "from-xai-var")
    assert Settings.from_env().xai_api_key == "from-xai-var"


def test_grok_model_resolves_on_the_api_backend():
    s = Settings(grok_model="grok-custom", backend="api")
    assert s.model_for("grok") == "grok-custom"



@pytest.mark.parametrize("name, value, message", [
    ("CLI_TIMEOUT", "-1", "CLI_TIMEOUT must be a finite number"),
    ("CLI_TIMEOUT", "nan", "CLI_TIMEOUT must be a finite number"),
    ("CLI_TIMEOUT", "inf", "CLI_TIMEOUT must be a finite number"),
    ("REQUEST_TIMEOUT", "0", "REQUEST_TIMEOUT must be a finite number"),
    ("RETRY_BASE_DELAY", "-0.5", "RETRY_BASE_DELAY must be a finite number"),
    ("MAX_TOKENS", "-1", "MAX_TOKENS must be a whole number of at least 1"),
    ("MAX_TOKENS", "0", "MAX_TOKENS must be a whole number of at least 1"),
    ("MAX_RETRIES", "-2", "MAX_RETRIES must be a whole number of at least 0"),
    ("MAX_FILE_BYTES", "0", "MAX_FILE_BYTES must be a whole number of at least 1"),
])
def test_a_numeric_setting_no_run_can_use_is_refused_by_name(monkeypatch, name, value, message):
    """Codex installation assessment on 4a273a3: these were accepted."""
    from quadratus.config import SettingsError
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError, match=message):
        Settings.from_env()


def test_the_cli_reports_an_invalid_setting_and_exits_2(monkeypatch, capsys):
    from quadratus.cli import main
    monkeypatch.setenv("CLI_TIMEOUT", "nan")
    assert main(["--status"]) == 2
    assert "invalid setting: CLI_TIMEOUT" in capsys.readouterr().err


def test_zero_retries_and_a_zero_delay_are_valid(monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "0")
    monkeypatch.setenv("RETRY_BASE_DELAY", "0")
    s = Settings.from_env()
    assert s.max_retries == 0 and s.retry_base_delay == 0.0
