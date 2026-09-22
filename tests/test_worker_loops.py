"""Offline worker continuation and lead-owned sibling retry contracts."""
import json

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import CLIProvider
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation, invocation_context
from quadratus.memory import TaskMemory
from quadratus.providers import ProviderError
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
from quadratus.runtime import Fleet
from quadratus.session import RunStalled, Session, TaskSpec
from quadratus.workers import FanOutExceeded, WorkerBudget, WorkerPool


def setup_pool(tmp_path, monkeypatch, replies, usage=10, shared_calls=10):
    store = ArtifactStore(tmp_path / 'artifacts')
    ledger = DelegationLedger(path=tmp_path / 'ledger.jsonl')
    run_budget = RunBudget(RunLimits(max_calls=shared_calls))
    fleet = Fleet(Settings(backend='cli', max_retries=2), delegation_ledger=ledger,
                  run_budget=run_budget)
    calls = []
    stream = iter(replies)

    def call(self, prompt, system, history):
        calls.append(dict(invocation_context.get()))
        self.last_usage = None if usage is None else {'input_tokens': usage, 'output_tokens': 1}
        answer = next(stream)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(CLIProvider, '_call', call)
    monkeypatch.setattr(CLIProvider, 'available', lambda self: True)
    pool = WorkerPool(store, fleet.invoke, WorkerBudget(max_per_task=6))
    return pool, TaskMemory('task', 'lead', store), calls, run_budget, fleet


def test_loop_charges_every_attempt_to_worker_task_and_shared_budgets(tmp_path, monkeypatch):
    pool, task, calls, shared, fleet = setup_pool(tmp_path, monkeypatch, ['CONTINUE: read second file', 'answer'])
    try:
        result = pool.commission(task=task, parent_key='lead', prompt='Read these two files',
                                 label='read-1', errand='read', steps=2)
        assert result.summary == 'answer'
        assert len(calls) == pool.spawned(task.task_id) == shared.snapshot()['reserved_attempts'] == 2
        assert all(c['origin'] == 'worker' and c['role'] == 'worker:read-1' for c in calls)
        data = [json.loads(line) for line in (tmp_path / 'ledger.jsonl').read_text().splitlines()]
        assert len([r for r in data if r['invoked']]) == 2
    finally:
        fleet.close()


@pytest.mark.parametrize('usage,limit,reason', [(30, 20, 'threshold'), (None, 100, 'unknown')])
def test_worker_token_stop_retains_overshoot_and_never_continues(tmp_path, monkeypatch, usage, limit, reason):
    pool, task, calls, shared, fleet = setup_pool(tmp_path, monkeypatch, ['CONTINUE: more', 'forbidden'], usage)
    try:
        with pytest.raises(RunBudgetExceeded, match=reason):
            pool.commission(task=task, parent_key='lead', prompt='Read files', label='read-1',
                            steps=3, token_limit=limit)
        assert len(calls) == 1
        artifacts = [pool.store.get(ref) for ref in pool.store.ids()]
        assert any('reported_tokens' in a and ('11' in a if usage else 'true' in a) for a in artifacts)
    finally:
        fleet.close()


def test_shared_limit_also_stops_a_worker_with_remaining_steps(tmp_path, monkeypatch):
    pool, task, calls, shared, fleet = setup_pool(tmp_path, monkeypatch, ['CONTINUE: more', 'forbidden'], shared_calls=1)
    try:
        with pytest.raises(RunBudgetExceeded):
            pool.commission(task=task, parent_key='lead', prompt='Read files', label='read-1', steps=3)
        assert len(calls) == 1
    finally:
        fleet.close()


def test_provider_retry_cannot_buy_an_extra_worker_step(tmp_path, monkeypatch):
    pool, task, calls, shared, fleet = setup_pool(tmp_path, monkeypatch,
                                               [ProviderError('fail'), ProviderError('fail'), 'forbidden'])
    try:
        with pytest.raises((ProviderError, RunBudgetExceeded)):
            pool.commission(task=task, parent_key='lead', prompt='Read files', label='read-1', steps=2)
        assert len(calls) <= 2
        assert pool.spawned(task.task_id) == len(calls)
    finally:
        fleet.close()


def test_worker_cannot_hire_even_through_parallel_entrypoint(tmp_path):
    pool = WorkerPool(ArtifactStore(tmp_path), lambda *a: pytest.fail('no calls'))
    task = TaskMemory('task', 'lead', pool.store)
    with invocation(task.task_id, 'worker:outer', 'worker'):
        with pytest.raises(FanOutExceeded):
            pool.commission(task=task, parent_key='worker', prompt='read', label='child')
        with pytest.raises(FanOutExceeded):
            pool.commission_many(task=task, parent_key='worker', jobs=[{'prompt': 'read', 'label': 'child'}])
    assert pool.spawned(task.task_id) == 0


def test_lead_retries_failure_with_two_siblings_and_receives_both(tmp_path):
    seen = []
    lead_calls = []

    def invoke(key, prompt, **kwargs):
        context = invocation_context.get()
        if context['origin'] == 'worker':
            seen.append(context['role'])
            if 'bad first request' in prompt:
                raise ValueError('first failed')
            return 'helper evidence' if 'helper' in context['role'] else 'retry evidence'
        lead_calls.append(prompt)
        if len(lead_calls) == 1:
            return 'WORKER ' + json.dumps({'errand': 'read', 'instruction': 'bad first request'})
        if len(lead_calls) == 2:
            return 'WORKER ' + json.dumps({'errand': 'read', 'instruction': 'rewritten request',
                'retry_of': 'read-1', 'helper': {'errand': 'check', 'instruction': 'independent check'}})
        assert 'helper evidence' in prompt and 'retry evidence' in prompt
        return 'complete draft'

    session = Session('goal', ArtifactStore(tmp_path), invoke)
    task = TaskMemory('t1', 'lead', session.store)
    assert session._draft_with_channels('claude:opus', TaskSpec('t1', 'task'), task) == 'complete draft'
    assert sorted(seen) == ['worker:read-1', 'worker:read-2', 'worker:read-2-helper']
    assert session.workers.spawned('t1') == 3


def test_helper_without_failure_is_refused_before_dispatch(tmp_path):
    request = {'errand': 'read', 'instruction': 'read', 'retry_of': 'unknown',
               'helper': {'errand': 'read', 'instruction': 'helper'}}
    session = Session('goal', ArtifactStore(tmp_path), lambda *a, **kw: 'WORKER ' + json.dumps(request))
    with pytest.raises(RunStalled, match='failed retry_of'):
        session._draft_with_channels('claude:opus', TaskSpec('t1', 'task'), TaskMemory('task', 'lead', session.store))
    assert session.workers.spawned('t1') == 0
