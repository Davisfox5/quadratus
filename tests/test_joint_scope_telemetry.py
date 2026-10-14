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
        if breach == 'size' and "Your draft made this task's change" in prompt:
            # Step 2 r13: a size-only overrun buys one trim from the fix
            # allowance. This one returns normally and still leaves the
            # change past the same stop, so the task fails as before.
            (tmp_path / 'a.py').write_text('a = 1\nb = 2\nc = 3\nd = 4\n')
            return 'Could not fit.\nCHANGED: ["a.py"]'
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
    trims = [row for row in invoked if row['role'] == 'revision']
    expected = 3 if breach == 'size' else 2
    assert len(trims) == expected - 2, "only a size-only overrun buys one trim, and only one"
    for trim in trims:
        # The trim's own provider call succeeded; its scope check failed after.
        assert trim['outcome'] == 'TaskFailed' and trim['provider_outcome'] == 'ok'
        assert trim['post_return_failure'] is True
        assert trim['input_tokens'] == 11 and trim['output_tokens'] == 3
    assert len(invoked) == len(calls) == expected
    assert len({row['invocation_id'] for row in invoked}) == expected
    assert sum(row['input_tokens'] + row['output_tokens'] for row in invoked) == 14 * expected
    assert invoked[0]['outcome'] == 'ok'
    assert (tmp_path / 'mine.txt').read_text() == 'operator work\n'
    assert (tmp_path / ('outside.py' if breach == 'path' else 'a.py')).exists()
    data = json.loads((result.run_dir / 'result.json').read_text())
    assert data['failed_tasks'] == ['t1'] and 'exceeded its declared scope' in result.report
