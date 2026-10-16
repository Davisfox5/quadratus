"""Research uses supplied source evidence without a nested exploration loop."""

import hashlib
import json
import subprocess
from http.client import IncompleteRead
from pathlib import Path

import pytest

from quadratus.cli_providers import ClaudeCLIProvider
from quadratus.config import Settings
from quadratus.delegation import invocation
from quadratus.providers import ProviderError
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.runtime import Fleet
from tools import research_review as inbox


@pytest.mark.parametrize('role', ['orchestrator', 'lead', 'collaborator', 'verifier', 'audit', 'closeout'])
def test_research_seats_keep_model_effort_budget_but_cannot_explore(tmp_path, monkeypatch, role):
    monkeypatch.setattr('shutil.which', lambda _: '/fake/claude')
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'CLAUDE.md').write_text('large automatically loaded project context')
    owner = ClaudeCLIProvider('', workdir=root, timeout=180, max_retries=4)
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **kw: owner)
    budget = RunBudget(RunLimits(max_calls=36, max_reported_tokens=750_000,
                                max_tokens_per_call=150_000, reserve_tokens_per_call=100_000))
    fleet = Fleet(Settings(backend='cli', supplied_context_only=True), project=root, run_budget=budget)
    observed = []

    def launch(argv, **kwargs):
        observed.append(Path(kwargs['cwd']))
        assert not list(Path(kwargs['cwd']).iterdir())
        assert kwargs['timeout'] == (60 if role == 'closeout' else 180)
        assert argv[argv.index('--tools') + 1] == ''
        assert argv[argv.index('--max-turns') + 1] == '1'
        assert argv[argv.index('--model') + 1] == 'fable'
        assert 'Task' in ' '.join(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            'result': 'assessment', 'is_error': False, 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 100, 'cache_read_input_tokens': 30,
                      'cache_creation_input_tokens': 20, 'output_tokens': 10}}), '')

    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    monkeypatch.setattr(fleet.project, 'snapshot', lambda: pytest.fail('source copy would reload context'))
    selected = fleet.provider_for('claude:fable')
    effort = selected.effort
    with invocation('t1', role):
        assert fleet.invoke('claude:fable', 'supplied evidence') == 'assessment'
    assert selected.effort == effort
    assert not selected.summary_only and owner.max_retries == 4
    assert budget.snapshot()['reported_tokens'] == 160
    assert budget.snapshot()['reserved_attempts'] == 1
    assert budget.snapshot()['shaped_reservations'] == (1 if role == 'closeout' else 0)
    assert len(observed) == 1 and not observed[0].exists()


def test_context_profile_refuses_writes_and_oversized_input_before_call(tmp_path, monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/fake/claude')
    monkeypatch.setattr('quadratus.runtime.build_provider',
                        lambda *a, **kw: ClaudeCLIProvider('', workdir=tmp_path))
    fleet = Fleet(Settings(backend='cli', supplied_context_only=True), project=tmp_path)
    monkeypatch.setattr(fleet, '_generate', lambda *a, **kw: pytest.fail('must refuse before provider'))
    with pytest.raises(ProviderError, match='write grant'):
        fleet.invoke('claude:fable', 'edit', allow_writes=True)
    with pytest.raises(ProviderError, match='128,000-byte'):
        fleet.invoke('claude:fable', 'x' * 128_001)


def test_evidence_is_bounded_measured_source_not_executed(tmp_path, monkeypatch):
    (tmp_path / 'quadratus').mkdir()
    path = tmp_path / 'quadratus/memory.py'
    raw = 'def repair_memory():\n    """Retrieve repair memory."""\n    raise RuntimeError("never execute")\n'
    path.write_text(raw)
    # Opener construction is inert; requests happen only for allowlisted paper IDs.
    class NoNetwork:
        def open(self, *a, **kw):
            pytest.fail('must not fetch arbitrary digest links')
    monkeypatch.setattr(inbox, 'build_opener', lambda *a: NoNetwork())
    context = inbox.research_context(tmp_path, 'repair memory https://attacker.invalid/script.py')
    excerpt = context['source_excerpts'][0]
    assert excerpt['symbol'] == 'repair_memory' and excerpt['line'] == 1
    assert excerpt['file_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert 'never execute' in excerpt['excerpt']
    assert not context['papers']


def test_paper_failure_is_unverified_and_redirects_are_allowlisted(tmp_path, monkeypatch):
    class Failed:
        def open(self, *a, **kw):
            raise OSError('source unavailable')
    monkeypatch.setattr(inbox, 'build_opener', lambda *a: Failed())
    context = inbox.research_context(tmp_path, 'https://arxiv.org/html/2609.20812v1')
    assert context['papers'][0]['status'] == 'unverified'
    assert 'source unavailable' in context['papers'][0]['reason']
    with pytest.raises(ValueError, match='allowlist'):
        inbox.ArxivRedirects().redirect_request(None, None, 302, '', {}, 'https://attacker.invalid/')


def test_truncated_abstract_response_does_not_consume_retry(tmp_path, monkeypatch):
    class BrokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, *args):
            raise IncompleteRead(b'partial')

    class Broken:
        def open(self, *args, **kwargs):
            return BrokenResponse()

    monkeypatch.setattr(inbox, 'build_opener', lambda *args: Broken())
    context = inbox.research_context(tmp_path, 'https://arxiv.org/abs/2609.20812')
    assert context['papers'][0]['status'] == 'unverified'
    assert 'IncompleteRead' in context['papers'][0]['reason']
