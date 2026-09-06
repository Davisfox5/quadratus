"""Tests for the provider base class and registry."""

from __future__ import annotations

import pytest

from quadratus.config import Settings
from quadratus.providers import (
    XAI_BASE_URL,
    GrokProvider,
    OpenAIProvider,
    ProviderError,
    Turn,
    build_providers,
)

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


class _Recorder:
    """A stand-in OpenAI client that records the one call each provider makes."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        rec = self

        class _Responses:
            def create(self, **kw):
                rec.calls.append(("responses.create", kw))
                return type("R", (), {"output_text": "from responses"})()

        class _Completions:
            def create(self, **kw):
                rec.calls.append(("chat.completions.create", kw))
                msg = type("M", (), {"content": "from chat"})()
                return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

        self.responses = _Responses()
        self.chat = type("Chat", (), {"completions": _Completions()})()


def test_chatgpt_uses_the_responses_api(monkeypatch):
    """gpt-5.5-pro and the other -pro models are not served on Chat Completions."""
    import openai

    made = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: made.append(_Recorder(**kw)) or made[-1])
    p = OpenAIProvider("gpt-5.5-pro", "key")
    out = p.generate("write it", system="be terse", history=[Turn("user", "hi"), Turn("assistant", "yo")])
    assert out == "from responses"
    (endpoint, kw), = made[0].calls
    assert endpoint == "responses.create"
    assert kw["model"] == "gpt-5.5-pro"
    assert kw["instructions"] == "be terse"
    assert kw["input"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
        {"role": "user", "content": "write it"},
    ]
    assert kw["max_output_tokens"] == p.max_tokens
    assert "temperature" not in kw and "max_tokens" not in kw
    assert "base_url" not in made[0].kwargs


def test_grok_uses_chat_completions_on_xai_with_max_completion_tokens(monkeypatch):
    import openai

    made = []
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: made.append(_Recorder(**kw)) or made[-1])
    p = GrokProvider("grok-4.6", "key")
    out = p.generate("write it", system="be terse", history=[Turn("user", "hi")])
    assert out == "from chat"
    (endpoint, kw), = made[0].calls
    assert endpoint == "chat.completions.create"
    assert kw["messages"][0] == {"role": "system", "content": "be terse"}
    assert kw["messages"][-1] == {"role": "user", "content": "write it"}
    assert kw["max_completion_tokens"] == p.max_tokens
    assert "max_tokens" not in kw
    assert made[0].kwargs["base_url"] == XAI_BASE_URL
