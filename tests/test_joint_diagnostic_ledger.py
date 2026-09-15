"""Persist only bounded failure metadata, never raw provider contents."""

import json

import pytest

from quadratus.config import Settings
from quadratus.delegation import DelegationLedger
from quadratus.providers import LLMProvider, ProviderError
from quadratus.runtime import Fleet


def test_failure_diagnostics_are_whitelisted_and_do_not_leak_to_next_call(tmp_path, monkeypatch):
    class Provider(LLMProvider):
        def _build_client(self):
            return object()

        def _call(self, *args):
            self.last_usage = {'input_tokens': 3, 'output_tokens': 1}
            if self.fail:
                self.last_diagnostics = {
                    'stop_reason': 'cancelled', 'model_calls': 2,
                    'attempted_tools': ['read_file', 'Agent', 'read_file', '/private/file',
                                        {'arguments': 'secret'}, 'x' * 500],
                    'raw_response': 'private transcript', 'arguments': 'secret',
                }
                raise ProviderError('cancelled')
            return 'ok'

    provider = Provider('grok', api_key='test', max_retries=1)
    provider.fail = True
    path = tmp_path / 'invocations.jsonl'
    fleet = Fleet(Settings(backend='cli'), delegation_ledger=DelegationLedger(path=path))
    monkeypatch.setattr(fleet, 'provider_for', lambda _: provider)
    with pytest.raises(ProviderError):
        fleet.invoke('grok:worker', 'read')
    provider.fail = False
    fleet.invoke('grok:worker', 'read again')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]['diagnostics'] == {
        'stop_reason': 'cancelled', 'model_calls': 2,
        'attempted_tools': ['read_file', 'Agent'],
    }
    assert rows[1]['diagnostics'] == {}
    assert 'private' not in path.read_text() and 'secret' not in path.read_text()
