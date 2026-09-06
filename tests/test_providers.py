"""Tests for the provider base class and registry."""

from __future__ import annotations

import pytest

from quadratus.config import Settings
from quadratus.providers import XAI_BASE_URL, GrokProvider, ProviderError, build_providers

from .conftest import FakeProvider


def test_unavailable_provider_raises():
    p = FakeProvider("claude", "Claude", unavailable=True)
    assert not p.available()
    with pytest.raises(ProviderError):
        p.generate("hello")


def test_generate_returns_text():
    p = FakeProvider("claude", "Claude")
    out = p.generate("write a function")
    assert "Claude" in out
    assert len(p.calls) == 1


def test_retries_then_succeeds():
    p = FakeProvider("openai", "ChatGPT", fail_times=2, max_retries=5)
    out = p.generate("task")
    assert "ChatGPT" in out
    # Two failures + one success.
    assert p._attempts == 3


def test_gives_up_after_max_retries():
    p = FakeProvider("openai", "ChatGPT", fail_times=10, max_retries=3)
    with pytest.raises(ProviderError):
        p.generate("task")
    assert p._attempts == 3


def test_non_retryable_fails_immediately():
    p = FakeProvider("openai", "ChatGPT", fail_times=10, retryable=False, max_retries=5)
    with pytest.raises(ProviderError):
        p.generate("task")
    assert p._attempts == 1


def test_build_providers_respects_order_and_skips_unknown():
    s = Settings(
        openai_api_key="x",
        anthropic_api_key="x",
        google_api_key="x",
        provider_order=["gemini", "bogus", "claude"],
    )
    providers = build_providers(s)
    names = [p.name for p in providers]
    assert names == ["gemini", "claude"]


def test_grok_is_a_real_api_provider_not_a_cli_only_guest():
    s = Settings(xai_api_key="x", provider_order=["grok"], backend="api")
    (grok,) = build_providers(s)
    assert isinstance(grok, GrokProvider)
    assert (grok.name, grok.label) == ("grok", "Grok")


def test_grok_talks_to_xai_through_the_openai_client(monkeypatch):
    import openai

    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    p = GrokProvider("grok-4.6", "xai-key", timeout=7.0)
    assert p.available()
    assert seen["base_url"] == XAI_BASE_URL
    assert seen["api_key"] == "xai-key"
    assert seen["timeout"] == 7.0


def test_all_four_providers_build_on_the_api_backend():
    s = Settings(openai_api_key="x", anthropic_api_key="x", google_api_key="x",
                 xai_api_key="x", backend="api")
    assert [p.name for p in build_providers(s)] == ["claude", "openai", "gemini", "grok"]
