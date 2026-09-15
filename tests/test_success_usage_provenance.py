"""Successful calls retain auxiliary provenance without counting it twice."""
import json
import subprocess

from quadratus.cli_providers import ClaudeCLIProvider
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.runtime import Fleet


def test_successful_claude_call_retains_auxiliary_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr('shutil.which', lambda _: '/fake/claude')
    envelope = {'type': 'result', 'result': 'recorded', 'usage': {
        'input_tokens': 66, 'cache_creation_input_tokens': 60660,
        'cache_read_input_tokens': 23500, 'output_tokens': 2922}, 'modelUsage': {
            'claude-fable-5-1': {'inputTokens': 66, 'cacheCreationInputTokens': 60660,
                               'cacheReadInputTokens': 23500, 'outputTokens': 2922},
            'claude-haiku-4-5-20251001': {'inputTokens': 2801, 'outputTokens': 14}}}
    monkeypatch.setattr('quadratus.cli_providers._launch', lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 0, json.dumps(envelope), ''))
    provider = ClaudeCLIProvider(model='fable', workdir=tmp_path, max_retries=1)
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **kw: provider)
    budget = RunBudget(RunLimits(), path=tmp_path / 'budget.json')
    ledger = DelegationLedger(path=tmp_path / 'invocations.jsonl')
    fleet = Fleet(Settings(backend='cli'), run_budget=budget, delegation_ledger=ledger)
    assert fleet.invoke('claude:fable', 'Record the supplied result') == 'recorded'
    event = ledger.events[-1]
    assert event.outcome == 'ok'
    assert event.diagnostics == {'auxiliary_models': ['claude-haiku-4-5-20251001'],
                                 'auxiliary_tokens': 2815}
    assert budget.snapshot()['reported_tokens'] == 89963
    assert event.input_tokens + event.output_tokens == 89963
    assert len(ledger.events) == 1  # auxiliary metadata is not another billable event
    persisted = json.loads((tmp_path / 'invocations.jsonl').read_text())
    assert persisted['diagnostics'] == event.diagnostics
