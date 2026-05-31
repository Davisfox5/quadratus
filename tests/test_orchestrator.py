"""Tests for the collaboration orchestrator."""

from __future__ import annotations

import pytest

from multi_llm.orchestrator import Orchestrator
from multi_llm.providers import ProviderError, Turn

from .conftest import FakeProvider


def test_no_providers_raises(settings):
    orch = Orchestrator(settings, providers=[])
    with pytest.raises(ProviderError):
        orch.run("do something")


def test_single_provider_solo_path(settings):
    claude = FakeProvider("claude", "Claude")
    orch = Orchestrator(settings, providers=[claude])
    result = orch.run("build a parser")
    assert len(result.stages) == 1
    assert result.stages[0].role == "Sole author"
    assert result.final_provider == "claude"
    assert result.final == result.stages[0].content
    assert result.plan == ""  # no planning phase for a solo run


def test_full_pipeline_stage_count(settings):
    settings.rounds = 1
    providers = [
        FakeProvider("claude", "Claude"),
        FakeProvider("openai", "ChatGPT"),
        FakeProvider("gemini", "Gemini"),
    ]
    orch = Orchestrator(settings, providers=providers)
    result = orch.run("implement a cache")
    # consensus + lead + 2 reviewers + synthesis = 5 stages.
    assert len(result.stages) == 5
    assert result.stages[0].role == "Consensus plan & role assignment"
    assert result.stages[1].role == "Lead implementer"
    assert result.stages[-1].role == "Synthesizer"
    assert result.final_provider == "claude"  # coordinator defaults to lead
    assert result.plan  # a consensus plan was produced


def test_multiple_rounds(settings):
    settings.rounds = 2
    providers = [
        FakeProvider("claude", "Claude"),
        FakeProvider("openai", "ChatGPT"),
    ]
    orch = Orchestrator(settings, providers=providers)
    result = orch.run("optimize this")
    # consensus + lead + (1 reviewer * 2 rounds) + synthesis = 5 stages.
    assert len(result.stages) == 5
    roles = [s.role for s in result.stages]
    assert roles[2] == "Reviewer/refiner (round 1)"
    assert roles[3] == "Reviewer/refiner (round 2)"


def test_explicit_synthesizer_choice(settings):
    settings.synthesizer = "gemini"
    providers = [
        FakeProvider("claude", "Claude"),
        FakeProvider("openai", "ChatGPT"),
        FakeProvider("gemini", "Gemini"),
    ]
    orch = Orchestrator(settings, providers=providers)
    result = orch.run("task")
    assert result.final_provider == "gemini"


def test_unavailable_providers_filtered(settings):
    providers = [
        FakeProvider("claude", "Claude", unavailable=True),
        FakeProvider("openai", "ChatGPT"),
    ]
    orch = Orchestrator(settings, providers=providers)
    assert len(orch.available) == 1
    result = orch.run("task")
    assert result.stages[0].role == "Sole author"


def test_progress_callback_invoked(settings):
    messages = []
    providers = [FakeProvider("claude", "Claude"), FakeProvider("openai", "ChatGPT")]
    orch = Orchestrator(settings, providers=providers)
    orch.run("task", progress=messages.append)
    assert messages  # at least one progress update


def test_history_passed_through(settings):
    claude = FakeProvider("claude", "Claude")
    orch = Orchestrator(settings, providers=[claude])
    history = [Turn("user", "earlier question"), Turn("assistant", "earlier answer")]
    orch.run("follow up", history=history)
    assert claude.calls[0]["history"] == history
