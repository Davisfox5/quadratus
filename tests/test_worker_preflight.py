"""Attempt 3 replay: the impossible errand stops at the WORKER boundary."""

import json

from quadratus.cli_providers import CLIProvider
from quadratus.config import Settings
from quadratus.delegation import invocation_context
from quadratus.project_run import run_project
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.workers import WorkerPool


def test_attempt_three_never_dispatches_or_reserves_worker(tmp_path, monkeypatch):
    # Abridged captured attempt-3 request, with the original capability mismatch.
    errand = {
        'errand': 'code', 'write': False, 'needs': ['patch'],
        'instruction': ('Add the core CSV-parsing helper '
                        '`def _import_preview_rows(project, text):` to app.py '
                        'and create tests/test_import_preview.py covering the four scenarios.'),
    }
    (tmp_path / 'app.py').write_text('x = 1\n')
    prompts, reservations, dispatches = [], [], []
    reserve = RunBudget.reserve
    commission = WorkerPool.commission

    def observed_reserve(self):
        reservations.append(invocation_context.get())
        return reserve(self)

    def observed_commission(self, **kwargs):
        dispatches.append(kwargs)
        return commission(self, **kwargs)

    def call(self, prompt, system, history):
        prompts.append(prompt)
        self.last_usage = {'input_tokens': 10, 'output_tokens': 2}
        if 'Name the single next task' in prompt:
            return 'KIND: backend simple\nSCOPE: ' + json.dumps({
                'permitted_paths': ['app.py'], 'intended_result': 'Add a helper',
                'acceptance': ['the helper exists'], 'max_lines': 20,
            }) + '\nAdd a helper to app.py.'
        if 'You are leading' in prompt:
            if 'was sent without a write grant' not in prompt:
                return 'WORKER ' + json.dumps(errand)
            assert 'write:true' in prompt
            assert 'describe' in prompt
            assert 'stop delegating and produce the work' in prompt
            (tmp_path / 'app.py').write_text('x = 1\ndef helper():\n    return 2\n')
            return 'Implemented'
        return 'ok'

    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    monkeypatch.setattr(CLIProvider, '_call', call)
    monkeypatch.setattr(RunBudget, 'reserve', observed_reserve)
    monkeypatch.setattr(WorkerPool, 'commission', observed_commission)
    run_project('Add a helper', tmp_path, Settings(backend='cli'),
                allow_writes=True, max_tasks=1, run_limits=RunLimits())
    assert any('was sent without a write grant' in p for p in prompts)
    assert dispatches == []
    assert reservations, 'The replay must exercise actual provider reservations.'
    assert not any(context and context['origin'] == 'worker' for context in reservations)
    assert not any('## What you can do' in p for p in prompts)
