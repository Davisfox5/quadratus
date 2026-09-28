"""Capability routing and one safe recovery through real project orchestration."""

import json
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import CLIProvider
from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.providers import ProviderError, ProviderRefusal
from quadratus.scope import TaskScope
from quadratus.session import PartialWorkStopped, RunStalled, Session, SessionConfig, TaskSpec


def test_requirements_include_acceptance_even_for_rote_docs(tmp_path):
    spec = TaskSpec('t1', 'Correct the results in docs/BULK_EDIT.md', kind='docs',
                    complexity='rote', scope=TaskScope(
                        permitted_paths=['docs/BULK_EDIT.md'], max_lines=10,
                        acceptance=['Run node tests/ui/mutation_check.js twice'],
                    ))
    assert 'execute' in spec.needs
    session = Session('fix', ArtifactStore(tmp_path), lambda *a: 'unused')
    assert session._pick_lead(spec) != 'grok:worker'


@pytest.mark.parametrize('label', ['["execute"]', '["patch", "execute"]'])
def test_explicit_needs_survive_decomposition(tmp_path, label):
    session = Session('fix', ArtifactStore(tmp_path),
                      lambda *a: f'KIND: docs rote\nNEEDS: {label}\nUpdate the results.')
    spec = session.next_task()
    assert 'execute' in spec.needs
    assert 'NEEDS:' not in spec.description


@pytest.mark.parametrize('label', ['["shell"]', 'execute', '[true]', '{}',
                                  '["execute"]\nNEEDS: ["patch"]'])
def test_unknown_or_ambiguous_needs_cannot_dispatch(tmp_path, label):
    calls = []

    def invoke(*args):
        calls.append(args)
        return f'KIND: docs rote\nNEEDS: {label}\nUpdate the results.'

    session = Session('fix', ArtifactStore(tmp_path), invoke)
    with pytest.raises(RunStalled, match='requirements'):
        session.next_task()
    assert len(calls) == 1


@pytest.mark.parametrize('pinned', [None, 'grok:worker'])
def test_unfit_only_available_seat_cannot_receive_execution(tmp_path, pinned):
    session = Session('fix', ArtifactStore(tmp_path), lambda *a: pytest.fail('No dispatch'),
                      available=lambda key: key == 'grok:worker')
    with pytest.raises((RunStalled, ValueError)):
        session._pick_lead(TaskSpec('t1', 'Run node tests/ui/mutation_check.js',
                                    kind='docs', complexity='rote', lead=pinned,
                                    needs=frozenset({'execute'})))


@pytest.mark.parametrize('failure', ['unchanged', 'second-failure', 'partial', 'refusal', 'interrupt'])
def test_recovery_is_bounded_and_preserves_work(tmp_path, monkeypatch, failure):
    (tmp_path / 'mine.txt').write_text('user work\n')
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    leads = []

    def call(self, prompt, system, history):
        self.last_usage = {'input_tokens': 7, 'output_tokens': 2}
        if 'Name the single next task' in prompt:
            return 'KIND: docs rote\nSCOPE: ' + json.dumps({
                'permitted_paths': ['a.md'], 'intended_result': 'Correct the heading',
                'acceptance': ['Heading reads Hello'], 'max_lines': 10,
            }) + '\nCorrect the heading in a.md.'
        if 'You are leading' in prompt:
            leads.append(self.restricted)
            if self.restricted:
                if failure == 'partial':
                    (tmp_path / 'a.md').write_text('partial\n')
                if failure == 'refusal':
                    raise ProviderRefusal('declined')
                if failure == 'interrupt':
                    raise KeyboardInterrupt('stop')
                raise ProviderError('cancelled')
            if failure == 'second-failure':
                raise ProviderError('second cancellation')
            (Path(self.workdir) / 'a.md').write_text('# Hello\n')
            return 'Heading corrected\nCHANGED: ["a.md"]'
        if 'The task is finished' in prompt:
            return 'SUMMARY: heading corrected\nREASONING: inspected'
        if 'The task cap for this run has been reached' in prompt:
            return 'DONE'
        pytest.fail('Unexpected model call')

    monkeypatch.setattr(CLIProvider, '_call', call)
    result = run_project('Correct heading', tmp_path, Settings(backend='cli'),
                         allow_writes=True, max_tasks=1)
    rows = [json.loads(line) for line in
            (result.run_dir / 'invocations.jsonl').read_text().splitlines()]
    lead_rows = [r for r in rows if r['invoked'] and r['role'] == 'lead']
    expected = 2 if failure in {'unchanged', 'second-failure'} else 1
    assert len(leads) == len(lead_rows) == expected
    assert leads[0] is True
    if expected == 2:
        assert leads[1] is False
    assert sum(r['input_tokens'] + r['output_tokens'] for r in lead_rows) == 9 * expected
    assert (tmp_path / 'mine.txt').read_text() == 'user work\n'
    if failure == 'unchanged':
        assert not result.error
        assert (tmp_path / 'a.md').read_text() == '# Hello\n'
        data = json.loads((result.run_dir / 'result.json').read_text())
        assert data['tasks'] == 1
        assert 'recovery' in result.report.lower()
    else:
        assert result.error and not result.completed
        if failure == 'partial':
            assert (tmp_path / 'a.md').read_text() == 'partial\n'
            assert 'PartialWorkStopped' in result.error


def test_unknown_source_inspection_cannot_recover(tmp_path, monkeypatch):
    calls = []

    def invoke(key, prompt, **kw):
        calls.append(key)
        raise ProviderError('cancelled')

    session = Session('fix', ArtifactStore(tmp_path / '.quadratus'), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    monkeypatch.setattr(session, '_inspect_partial_edits', lambda _: {
        'inspected': False, 'changed': [], 'changed_lines': 0, 'note': 'Inspection unavailable',
    })
    spec = TaskSpec('t1', 'Correct heading in a.md', kind='docs', complexity='rote',
                    scope=TaskScope(permitted_paths=['a.md'], max_lines=10))
    with pytest.raises(PartialWorkStopped):
        session.run_task(spec)
    assert calls == ['grok:worker']
    assert session.in_flight['inspected'] is False


def test_consultant_failure_cannot_reseat_the_lead(tmp_path):
    calls = []

    def invoke(key, prompt, **kw):
        calls.append(key)
        if 'You are leading' in prompt:
            return 'CONSULT Opus: Is this heading clear?'
        raise ProviderError('consultant failed')

    session = Session('fix', ArtifactStore(tmp_path / '.quadratus'), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    spec = TaskSpec('t1', 'Correct heading in a.md', kind='docs', complexity='rote',
                    scope=TaskScope(permitted_paths=['a.md'], max_lines=10))
    with pytest.raises(ProviderError, match='consultant failed'):
        session.run_task(spec)
    assert calls == ['grok:worker', 'claude:opus']
