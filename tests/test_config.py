"""Tests for configuration loading."""

from __future__ import annotations

from multi_llm.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    Settings,
)


def test_defaults_when_env_unset(monkeypatch):
    for var in (
        "CLAUDE_MODEL",
        "OPENAI_MODEL",
        "GEMINI_MODEL",
        "ROUNDS",
        "PROVIDER_ORDER",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings.from_env()
    assert s.claude_model == DEFAULT_CLAUDE_MODEL
    assert s.openai_model == DEFAULT_OPENAI_MODEL
    assert s.gemini_model == DEFAULT_GEMINI_MODEL
    assert s.rounds == 1
    assert s.provider_order == ["claude", "openai", "gemini"]


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-custom")
    monkeypatch.setenv("ROUNDS", "3")
    monkeypatch.setenv("PROVIDER_ORDER", "gemini, openai")
    s = Settings.from_env()
    assert s.claude_model == "claude-custom"
    assert s.rounds == 3
    assert s.provider_order == ["gemini", "openai"]


def test_invalid_int_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ROUNDS", "not-a-number")
    s = Settings.from_env()
    assert s.rounds == 1


def test_rounds_floor_is_one(monkeypatch):
    monkeypatch.setenv("ROUNDS", "0")
    s = Settings.from_env()
    assert s.rounds == 1


def test_gemini_key_aliases(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini-var")
    s = Settings.from_env()
    assert s.google_api_key == "from-gemini-var"
