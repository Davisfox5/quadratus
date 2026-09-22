"""API caps use supplied rates and measured counts, never CLI counterfactuals."""

import json
from types import SimpleNamespace

import pytest

from quadratus.cli_providers import ClaudeCLIProvider
from quadratus.providers import ClaudeProvider, GrokProvider, OpenAIProvider
from quadratus.run_budget import APICostRate, RunBudget, RunBudgetExceeded, RunLimits

RATES = (APICostRate('openai:fixture', 1, 2, 'fixture rates, not vendor pricing'),)


def budget(cap=0.00001, **kwargs):
    return RunBudget(RunLimits(max_cost_usd=cap, api_cost_rates=RATES), **kwargs)


@pytest.mark.parametrize('cap', [True, 0, -1, float('nan'), float('inf')])
def test_cost_limit_is_finite_and_positive(cap):
    with pytest.raises(ValueError):
        RunLimits(max_cost_usd=cap)


def test_api_attempts_stop_at_cost_threshold_and_preserve_inflight_overshoot(tmp_path):
    b = budget(path=tmp_path / 'budget.json')
    one, _ = b.reserve(transport='api', price_key='openai:fixture')
    two, _ = b.reserve(transport='api', price_key='openai:fixture')
    for ticket in (one, two):
        with pytest.raises(RunBudgetExceeded, match='api_cost_threshold'):
            b.finish(ticket, {'input_tokens': 6, 'output_tokens': 2}, reply=f'answer {ticket}')
    snap = json.loads((tmp_path / 'budget.json').read_text())
    assert snap['api_cost_usd'] == pytest.approx(.00002)
    assert snap['api_cost_overshoot_usd'] == pytest.approx(.00001)
    assert len(snap['preserved_responses']) == 2
    assert snap['in_flight'] == 0
    with pytest.raises(RunBudgetExceeded):
        b.reserve(transport='api', price_key='openai:fixture')


def test_cli_never_needs_a_price_and_never_contributes_dollars():
    b = budget()
    for _ in range(3):
        ticket, _ = b.reserve(transport='cli')
        b.finish(ticket, {'input_tokens': 10000, 'output_tokens': 5000})
    assert b.snapshot()['api_cost_usd'] == 0
    assert b.snapshot()['reported_tokens'] == 45000


def test_cli_still_obeys_reported_token_threshold():
    b = RunBudget(RunLimits(max_cost_usd=1, max_reported_tokens=10))
    ticket, _ = b.reserve(transport='cli')
    with pytest.raises(RunBudgetExceeded, match='reported_token_threshold'):
        b.finish(ticket, {'input_tokens': 10, 'output_tokens': 1})


def test_missing_api_rate_stops_before_reservation():
    b = budget()
    with pytest.raises(RunBudgetExceeded, match='api_price_unavailable'):
        b.reserve(transport='api', price_key='openai:unpriced')
    assert b.snapshot()['reserved_attempts'] == 0


def test_unpriced_resolved_model_stops_after_preserving_unknown_cost():
    b = budget()
    ticket, _ = b.reserve(transport='api', price_key='openai:fixture')
    with pytest.raises(RunBudgetExceeded, match='unknown_api_cost'):
        b.finish(ticket, {'input_tokens': 1, 'output_tokens': 1}, price_key='openai:changed')
    assert b.snapshot()['unknown_api_cost_attempts'] == 1


def test_unknown_usage_is_never_priced_as_zero():
    b = budget()
    ticket, _ = b.reserve(transport='api', price_key='openai:fixture')
    with pytest.raises(RunBudgetExceeded, match='unknown_usage'):
        b.finish(ticket, None)
    assert b.snapshot()['unknown_api_cost_attempts'] == 1


def api_provider(monkeypatch, kind, response):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create),
                             responses=SimpleNamespace(create=create),
                             chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(kind, '_build_client', lambda _: client)
    p = kind('fixture', api_key='fixture', max_retries=1)
    return p, calls


@pytest.mark.parametrize('kind,usage,expected', [
    (ClaudeProvider, dict(input_tokens=3, output_tokens=2, cache_read_input_tokens=4,
                          cache_creation_input_tokens=5), 12),
    (OpenAIProvider, dict(input_tokens=12, output_tokens=2,
                          input_tokens_details={'cached_tokens': 4}), 12),
    (GrokProvider, dict(prompt_tokens=12, completion_tokens=2,
                       prompt_tokens_details={'cached_tokens': 4}), 12),
])
def test_api_adapters_normalize_reported_tokens_before_pricing(monkeypatch, kind, usage, expected):
    response = SimpleNamespace(model='fixture', usage=usage, output_text='answer', stop_reason='end_turn',
                               content=[SimpleNamespace(type='text', text='answer')],
                               choices=[SimpleNamespace(message=SimpleNamespace(content='answer'))])
    p, calls = api_provider(monkeypatch, kind, response)
    p.run_budget = RunBudget(RunLimits(max_cost_usd=1, api_cost_rates=(
        APICostRate(f'{p.name}:fixture', 1, 2, 'fixture'),)))
    assert p.generate('fixture') == 'answer'
    assert len(calls) == 1
    assert p.run_budget.snapshot()['api_cost_usd'] == pytest.approx((expected + 4) / 1_000_000)
    assert p.run_budget.snapshot()['reported_tokens'] == expected + 2


def test_real_api_attempt_path_stops_and_keeps_returned_text(monkeypatch):
    response = SimpleNamespace(model='fixture', usage=dict(input_tokens=6, output_tokens=2), output_text='answer')
    p, calls = api_provider(monkeypatch, OpenAIProvider, response)
    p.run_budget = budget()
    with pytest.raises(RunBudgetExceeded, match='api_cost_threshold') as stopped:
        p.generate('fixture')
    assert stopped.value.provider_response == 'answer'
    with pytest.raises(RunBudgetExceeded):
        p.generate('cannot run')
    assert len(calls) == 1


def test_real_cli_attempt_path_ignores_api_price_and_cost(monkeypatch):
    monkeypatch.setattr(ClaudeCLIProvider, 'available', lambda _: True)

    def call(self, *args):
        self.last_usage = dict(input_tokens=100, output_tokens=100)
        return 'fixture'

    monkeypatch.setattr(ClaudeCLIProvider, '_call', call)
    p = ClaudeCLIProvider('fixture')
    p.run_budget = budget()
    assert p.generate('fixture') == 'fixture'
    assert p.run_budget.snapshot()['api_cost_usd'] == 0


def test_sdk_internal_retries_are_disabled(monkeypatch):
    import anthropic
    import openai

    seen = []
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: seen.append(kw) or object())
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: seen.append(kw) or object())
    for kind in (ClaudeProvider, OpenAIProvider, GrokProvider):
        kind('fixture', api_key='fixture')
    assert len(seen) == 3
    assert all(config['max_retries'] == 0 for config in seen)


def test_rate_validation_and_no_implicit_seed_fallback():
    with pytest.raises(ValueError):
        APICostRate('openai:fixture', float('nan'), 1, 'fixture')
    with pytest.raises(ValueError):
        APICostRate('openai:fixture', 1, 1, '')
    with pytest.raises(ValueError):
        RunLimits(api_cost_rates=RATES + RATES)
    b = RunBudget(RunLimits(max_cost_usd=1))
    with pytest.raises(RunBudgetExceeded, match='api_price_unavailable'):
        b.reserve(transport='api', price_key='claude:fable')
    assert b.snapshot()['reserved_attempts'] == 0
