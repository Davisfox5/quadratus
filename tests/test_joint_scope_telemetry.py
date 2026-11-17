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


def test_a_size_overrun_is_telemetry_and_the_task_goes_on_to_review(tmp_path, monkeypatch):
    """Operator directive 2026-10-20: a change well past the estimate is
    measured and reported, the lead's call is a plain success, no trim call
    is spent, and the task meets its reviewer and close-out as any other."""
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    calls = []

    def call(self, prompt, system, history):
        calls.append(prompt)
        self.last_usage = {'input_tokens': 11, 'output_tokens': 3}
        if 'Name the single next task' in prompt:
            return 'KIND: backend standard\nSCOPE: ' + json.dumps({
                'permitted_paths': ['a.py'], 'intended_result': 'Add a constant',
                'acceptance': ['constant exists'], 'max_lines': 1,
            }) + '\nAdd a constant.' if len(calls) == 1 else 'DONE'
        if 'You are leading' in prompt:
            (tmp_path / 'a.py').write_text('a = 1\nb = 2\nc = 3\n')
            return 'Implemented\nCHANGED: ["a.py"]'
        if 'Bring the change down' in prompt:
            pytest.fail('no trim call is spent on size')
        if 'contributing an independent' in prompt:
            assert "3 lines now" in prompt and "estimate of ~1" in prompt and "stops nothing for size" in prompt
            return 'NO FINDINGS'
        return 'SUMMARY: done\nDECISIONS: none recorded\nDEAD ENDS: none'

    monkeypatch.setattr(CLIProvider, '_call', call)
    result = run_project('Add constant', tmp_path, Settings(backend='cli'), allow_writes=True, max_tasks=1)
    rows = [json.loads(line) for line in (result.run_dir / 'invocations.jsonl').read_text().splitlines()]
    invoked = [row for row in rows if row['invoked']]
    lead = next(row for row in invoked if row['role'] == 'lead')
    assert lead['outcome'] == 'ok' and lead['provider_outcome'] == 'ok' and not lead['post_return_failure']
    assert [row['role'] for row in invoked if row['role'] == 'revision'] == []
    assert any(row['role'].startswith('collaborator') for row in invoked), "the oversized draft met its reviewer"
    data = json.loads((result.run_dir / 'result.json').read_text())
    assert data['failed_tasks'] == []
    assert data['scope_reports'][-1]['oversized'] and data['scope_reports'][-1]['within_scope']


def test_a_path_breach_records_provider_success_and_failed_acceptance(tmp_path, monkeypatch):
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
            (tmp_path / 'outside.py').write_text('a = 1\nb = 2\nc = 3\n')
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
    assert lead['outcome'] == 'TaskFailed'
    assert lead['provider_outcome'] == 'ok'
    assert lead['post_return_failure'] is True
    assert lead['input_tokens'] == 11 and lead['output_tokens'] == 3
    assert len(invoked) == len(calls) == 2
    assert len({row['invocation_id'] for row in invoked}) == 2
    assert sum(row['input_tokens'] + row['output_tokens'] for row in invoked) == 14 * 2
    assert invoked[0]['outcome'] == 'ok'
    assert (tmp_path / 'mine.txt').read_text() == 'operator work\n'
    assert (tmp_path / 'outside.py').exists()
    data = json.loads((result.run_dir / 'result.json').read_text())
    assert data['failed_tasks'] == ['t1'] and 'exceeded its declared scope' in result.report
