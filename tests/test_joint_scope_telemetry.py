"""Acceptance failures must survive the provider/session boundary on disk."""

import json

import pytest

from quadratus.cli_providers import CLIProvider
from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.scope import ScopeReport


def test_oversized_allowed_path_never_renders_as_passed():
    report = ScopeReport(within_scope=True, changed=['a.py'], changed_lines=166, max_lines=100)
    assert report.oversized
    assert 'passed' not in report.render().lower()
    assert '166' in report.render() and '100' in report.render()


@pytest.mark.parametrize('breach', ['size', 'path'])
def test_scope_stop_records_provider_success_and_failed_acceptance(tmp_path, monkeypatch, breach):
    (tmp_path / 'mine.txt').write_text('operator work\n')
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    calls = []

    def call(self, prompt, system, history):
        calls.append(prompt)
        self.last_usage = {'input_tokens': 11, 'output_tokens': 3}
        if 'Name the single next task' in prompt:
            return 'KIND: backend standard\nSCOPE: ' + json.dumps({
                'permitted_paths': ['a.py'], 'intended_result': 'Add a constant',
                'acceptance': ['constant exists'], 'max_lines': 1,
            }) + '\nAdd a constant.'
        if 'You are leading' in prompt:
            name = 'outside.py' if breach == 'path' else 'a.py'
            (tmp_path / name).write_text('a = 1\nb = 2\nc = 3\n')
            return 'Implemented'
        pytest.fail('Scope-stopped task must not reach review or closeout')

    monkeypatch.setattr(CLIProvider, '_call', call)
    result = run_project('Add constant', tmp_path, Settings(backend='cli'),
                         allow_writes=True, max_tasks=1)
    rows = [json.loads(line) for line in
            (result.run_dir / 'invocations.jsonl').read_text().splitlines()]
    invoked = [row for row in rows if row['invoked']]
    lead = next(row for row in invoked if row['role'] == 'lead')
    assert not result.completed
    assert lead['outcome'] == 'PartialWorkStopped'
    assert lead['provider_outcome'] == 'ok'
    assert lead['post_return_failure'] is True
    assert lead['input_tokens'] == 11 and lead['output_tokens'] == 3
    assert len(invoked) == len(calls) == 2
    assert len({row['invocation_id'] for row in invoked}) == 2
    assert sum(row['input_tokens'] + row['output_tokens'] for row in invoked) == 28
    assert invoked[0]['outcome'] == 'ok'
    assert (tmp_path / 'mine.txt').read_text() == 'operator work\n'
    assert (tmp_path / ('outside.py' if breach == 'path' else 'a.py')).exists()
    assert 'PartialWorkStopped' in result.report
