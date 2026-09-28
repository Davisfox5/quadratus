"""Tests for classifier-refusal handling (stop_reason "refusal") and fallback.

The Claude API returns a refusal as HTTP 200 with an empty or partial content
array. These tests fake the SDK client and the CLI envelope; nothing here
touches the network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import ClaudeCLIProvider
from quadratus.config import Settings
from quadratus.orchestrator import Orchestrator
from quadratus.providers import ClaudeProvider, ProviderError, ProviderRefusal, build_provider

from .conftest import FakeProvider

# -- a fake Anthropic client ------------------------------------------------


class _FakeMessages:
    def __init__(self, refuse_models, category="cyber"):
        self.refuse_models = set(refuse_models)
        self.category = category
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["model"]
        if model in self.refuse_models:
            return SimpleNamespace(
                stop_reason="refusal",
                stop_details=SimpleNamespace(
                    type="refusal", category=self.category, explanation="policy"
                ),
                content=[],  # pre-output decline: nothing to read
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text=f"answer from {model}")],
        )


class _FakeClient:
    def __init__(self, refuse_models=(), **kw):
        self.messages = _FakeMessages(refuse_models, **kw)


def _claude(monkeypatch, *, refuse_models=(), fallback=None, **kw):
    client = _FakeClient(refuse_models, **kw)

    def build(self):
        self._sdk = SimpleNamespace()  # no exception classes -> nothing retryable
        return client

    monkeypatch.setattr(ClaudeProvider, "_build_client", build)
    provider = ClaudeProvider(
        model="claude-fable-5-1",
        api_key="k",
        max_retries=3,
        retry_base_delay=0.0,
        refusal_fallback_model=fallback,
    )
    return provider, client.messages


# -- API backend ---------------------------------------------------------------


def test_refusal_is_raised_not_reported_as_empty(monkeypatch):
    p, messages = _claude(monkeypatch, refuse_models={"claude-fable-5-1"})
    with pytest.raises(ProviderRefusal) as info:
        p.generate("audit this binary")
    assert info.value.category == "cyber"
    assert info.value.model == "claude-fable-5-1"
    assert "empty response" not in str(info.value)
    # A refusal is deterministic for the request: exactly one attempt.
    assert len(messages.calls) == 1


def test_refusal_is_a_provider_error_for_existing_handlers(monkeypatch):
    p, _ = _claude(monkeypatch, refuse_models={"claude-fable-5-1"})
    with pytest.raises(ProviderError):
        p.generate("x")


def test_refusal_re_sends_once_on_the_fallback_model(monkeypatch):
    p, messages = _claude(
        monkeypatch, refuse_models={"claude-fable-5-1"}, fallback="claude-opus-4-8"
    )
    out = p.generate("audit this binary", system="S")
    assert out == "answer from claude-opus-4-8"
    assert [c["model"] for c in messages.calls] == ["claude-fable-5-1", "claude-opus-4-8"]
    # Same request, different model: prompt-shaping fields are unchanged.
    assert messages.calls[0]["messages"] == messages.calls[1]["messages"]
    assert messages.calls[0]["system"] == messages.calls[1]["system"]


def test_fallback_is_not_sticky(monkeypatch):
    """History is plain text, so nothing binds later turns to the fallback."""
    p, messages = _claude(
        monkeypatch, refuse_models={"claude-fable-5-1"}, fallback="claude-opus-4-8"
    )
    p.generate("first")
    p.generate("second")
    assert p.model == "claude-fable-5-1"
    assert messages.calls[2]["model"] == "claude-fable-5-1"


def test_both_models_refusing_surfaces_as_one_refusal(monkeypatch):
    p, messages = _claude(
        monkeypatch,
        refuse_models={"claude-fable-5-1", "claude-opus-4-8"},
        fallback="claude-opus-4-8",
    )
    with pytest.raises(ProviderRefusal) as info:
        p.generate("x")
    assert "both" in str(info.value)
    assert info.value.model == "claude-opus-4-8"
    assert len(messages.calls) == 2


def test_fallback_equal_to_primary_is_ignored(monkeypatch):
    p, messages = _claude(
        monkeypatch, refuse_models={"claude-fable-5-1"}, fallback="claude-fable-5-1"
    )
    with pytest.raises(ProviderRefusal):
        p.generate("x")
    assert len(messages.calls) == 1


def test_null_stop_details_is_a_valid_refusal(monkeypatch):
    p, messages = _claude(monkeypatch, refuse_models={"claude-fable-5-1"})
    messages.create = lambda **kw: SimpleNamespace(
        stop_reason="refusal", stop_details=None, content=[]
    )
    with pytest.raises(ProviderRefusal) as info:
        p.generate("x")
    assert info.value.category is None


def test_request_shape_carries_no_rejected_parameters(monkeypatch):
    """Fable 5.1 / Opus 5 400 on thinking config, sampling params, prefill and
    forced tool_choice. The API call must send none of them."""
    p, messages = _claude(monkeypatch)
    p.generate("q", history=[])
    sent = messages.calls[0]
    for banned in ("thinking", "temperature", "top_p", "top_k", "tool_choice", "tools"):
        assert banned not in sent, banned
    assert sent["messages"][-1]["role"] == "user"  # never an assistant prefill


# -- CLI backend ---------------------------------------------------------------


def test_cli_envelope_refusal_is_raised_once(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    calls = []
    envelope = json.dumps({
        "type": "result", "is_error": False, "result": "",
        "stop_reason": "refusal", "stop_details": {"category": "cyber"},
    })

    def fake_run(*a, **k):
        calls.append(1)
        return SimpleNamespace(stdout=envelope, stderr="", returncode=0)

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    p = ClaudeCLIProvider(model="fable", retry_base_delay=0.0)
    with pytest.raises(ProviderRefusal) as info:
        p.generate("x")
    assert info.value.category == "cyber"
    assert info.value.model == "fable"
    assert len(calls) == 1


def test_cli_refusal_falls_back_to_the_configured_alias(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    seen = []

    def fake_run(argv, **k):
        model = argv[argv.index("--model") + 1]
        seen.append(model)
        if model == "fable":
            body = {"type": "result", "result": "", "stop_reason": "refusal"}
        else:
            body = {"type": "result", "result": "ok from opus", "stop_reason": "end_turn"}
        return SimpleNamespace(stdout=json.dumps(body), stderr="", returncode=0)

    monkeypatch.setattr(cli_providers, "_launch", fake_run)
    p = ClaudeCLIProvider(model="fable", refusal_fallback_model="opus", retry_base_delay=0.0)
    assert p.generate("x") == "ok from opus"
    assert seen == ["fable", "opus"]


# -- settings ------------------------------------------------------------------


def test_refusal_fallback_settings_follow_the_transport(monkeypatch):
    monkeypatch.setenv("CLAUDE_REFUSAL_FALLBACK_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CLAUDE_CLI_REFUSAL_FALLBACK_MODEL", "opus")
    s = Settings.from_env()
    s.backend = "api"
    assert s.refusal_fallback_for("claude") == "claude-opus-4-8"
    s.backend = "cli"
    assert s.refusal_fallback_for("claude") == "opus"
    assert s.refusal_fallback_for("openai") == ""


def test_refusal_fallback_defaults_to_none(monkeypatch):
    monkeypatch.delenv("CLAUDE_REFUSAL_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_CLI_REFUSAL_FALLBACK_MODEL", raising=False)
    assert Settings.from_env().refusal_fallback_for("claude") == ""


def test_build_provider_wires_the_fallback(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    s = Settings(backend="cli", claude_cli_refusal_fallback_model="opus")
    p = build_provider("claude", s)
    assert p.refusal_fallback_model == "opus"


# -- the pipeline ------------------------------------------------------------------


def test_reviewer_refusal_skips_the_round_and_keeps_the_draft(settings):
    lead = FakeProvider("claude", "Claude")
    refuser = FakeProvider("openai", "ChatGPT", refuse=True)
    okay = FakeProvider("gemini", "Gemini")
    orch = Orchestrator(settings, providers=[lead, refuser, okay])
    result = orch.run("task")
    roles = [s.role for s in result.stages]
    assert any("declined" in r for r in roles)
    assert result.final  # synthesis still ran
    assert "Reviewer/refiner (round 1)" in roles  # the other reviewer still ran


def test_lead_refusal_propagates(settings):
    lead = FakeProvider("claude", "Claude", refuse=True)
    other = FakeProvider("openai", "ChatGPT")
    orch = Orchestrator(settings, providers=[lead, other])
    with pytest.raises(ProviderRefusal):
        orch.run("task")
