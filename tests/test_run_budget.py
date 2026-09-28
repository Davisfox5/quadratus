"""Run controls are enforced at real provider attempts, not reporting callbacks."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from quadratus.config import Settings
from quadratus.delegation import DelegationLedger
from quadratus.providers import LLMProvider, PartialWorkSuspected
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
from quadratus.runtime import Fleet


class Scripted(LLMProvider):
    def _build_client(self):
        return object()

    def _call(self, *args):
        self.calls.append(self.timeout)
        self.last_usage = self.usage
        if self.action:
            self.action(self)
        return 'answer'

    def _retryable(self, exc):
        return isinstance(exc, TimeoutError)


def provider(budget, *, usage=None, action=None):
    p = Scripted('fake', api_key='fixture', timeout=50, max_retries=3)
    p.run_budget, p.calls, p.action = budget, [], action
    p.usage = usage
    return p


@pytest.mark.parametrize('field,value', [('max_calls', True), ('max_calls', 0),
                                       ('max_reported_tokens', -1),
                                       ('wall_seconds', float('nan')),
                                       ('wall_seconds', float('inf')),
                                       ('wall_seconds', 0),
                                       ('max_concurrent_workers', 0)])
def test_limits_reject_unbounded_or_invalid_values(field, value):
    with pytest.raises(ValueError):
        RunLimits(**{field: value})


def test_parallel_reservations_cannot_exceed_global_call_limit():
    b = RunBudget(RunLimits(max_calls=3))
    def attempt(_):
        try:
            return b.reserve()[0]
        except RunBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        tickets = [x for x in pool.map(attempt, range(30)) if x is not None]
    assert sorted(tickets) == [1, 2, 3]
    assert b.snapshot()['reserved_attempts'] == 3


def test_each_retry_spends_a_reservation_and_third_attempt_never_starts(monkeypatch):
    monkeypatch.setattr('quadratus.providers.time.sleep', lambda _: None)
    b = RunBudget(RunLimits(max_calls=2))
    def timeout(_):
        raise TimeoutError('scripted')
    p = provider(b, usage={'input_tokens': 8, 'output_tokens': 2}, action=timeout)
    with pytest.raises(RunBudgetExceeded, match='call_limit'):
        p.generate('retry')
    assert len(p.calls) == 2
    assert b.snapshot()['reported_tokens'] == 20


@pytest.mark.parametrize('usage', [None, {}, {'input_tokens': 1},
                                  {'input_tokens': True, 'output_tokens': 1},
                                  {'input_tokens': -1, 'output_tokens': 2}])
def test_unknown_usage_stops_success_before_any_further_attempt(usage):
    b = RunBudget(RunLimits())
    p = provider(b, usage=usage)
    with pytest.raises(RunBudgetExceeded, match='unknown_usage'):
        p.generate('hello')
    with pytest.raises(RunBudgetExceeded):
        p.generate('must not run')
    assert len(p.calls) == 1
    assert b.snapshot()['unknown_usage_attempts'] == 1


def test_reported_tokens_include_cache_once_and_inflight_overshoot_is_preserved(tmp_path):
    path = tmp_path / 'budget.json'
    b = RunBudget(RunLimits(max_reported_tokens=10), path=path)
    first, _ = b.reserve()
    second, _ = b.reserve()
    with pytest.raises(RunBudgetExceeded, match='reported_token_threshold'):
        b.finish(first, {'input_tokens': 9, 'cached_input_tokens': 7, 'output_tokens': 1})
    with pytest.raises(RunBudgetExceeded):
        b.finish(second, {'input_tokens': 4, 'output_tokens': 2})
    assert json.loads(path.read_text())['reported_tokens'] == 16
    assert b.snapshot()['in_flight'] == 0
    with pytest.raises(RunBudgetExceeded):
        b.reserve()


def test_deadline_clamps_transport_timeout_and_rejects_late_success():
    now = [0]
    b = RunBudget(RunLimits(wall_seconds=5), clock=lambda: now[0])
    p = provider(b, usage={'input_tokens': 1, 'output_tokens': 0},
                 action=lambda _: now.__setitem__(0, 6))
    with pytest.raises(RunBudgetExceeded, match='wall_deadline'):
        p.generate('slow')
    assert p.calls == [5]
    assert p.timeout == 50


def test_native_child_invalidates_the_bounded_claim():
    b = RunBudget(RunLimits())
    p = provider(b, usage={'input_tokens': 1, 'output_tokens': 0},
                 action=lambda p: p.native_children.append(object()))
    with pytest.raises(RunBudgetExceeded, match='uncontrolled_native_delegation'):
        p.generate('no uncontrolled child')


def test_partial_write_timeout_keeps_file_and_never_replays(tmp_path, monkeypatch):
    import subprocess
    import sys

    from quadratus.cli_providers import ClaudeCLIProvider

    b = RunBudget(RunLimits())
    def write_then_timeout(*args, **kwargs):
        (tmp_path / 'partial.txt').write_text('work')
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setenv('QUADRATUS_CLI_BINARY_CLAUDE', sys.executable)
    calls = []
    def launch(*args, **kwargs):
        calls.append(1)
        return write_then_timeout(*args, **kwargs)
    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    p = ClaudeCLIProvider('fable', workdir=tmp_path, allow_writes=True, max_retries=3)
    p.run_budget = b
    with pytest.raises(PartialWorkSuspected):
        p.generate('edit')
    assert (tmp_path / 'partial.txt').read_text() == 'work'
    assert calls == [1]
    assert b.snapshot()['stop_reason'] == 'unknown_usage'


def test_fleet_records_denial_as_not_invoked_and_shares_budget_across_seats(tmp_path, monkeypatch):
    b = RunBudget(RunLimits(max_calls=1))
    p = provider(b, usage={'input_tokens': 1, 'output_tokens': 1})
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **kw: p)
    ledger = DelegationLedger(path=tmp_path / 'invocations.jsonl')
    fleet = Fleet(Settings(backend='cli'), run_budget=b, delegation_ledger=ledger)
    fleet.invoke('openai:gpt-5.6-sol', 'first')
    with pytest.raises(RunBudgetExceeded):
        fleet.invoke('openai:gpt-5.6-luna', 'second')
    assert len(p.calls) == 1
    assert [e.invoked for e in ledger.events] == [True, False]


def test_budget_failures_cannot_be_swallowed_by_the_observational_ledger():
    b = RunBudget(RunLimits(max_reported_tokens=1))
    p = provider(b, usage={'input_tokens': 2, 'output_tokens': 0})
    p.attempt_observer = lambda *args: (_ for _ in ()).throw(OSError('ledger unavailable'))
    with pytest.raises(RunBudgetExceeded):
        p.generate('still stop')


def test_project_budget_stop_preserves_actual_edit_and_provider_success(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    from quadratus.cli_providers import ClaudeCLIProvider
    from quadratus.project_run import run_project
    from quadratus.session import Session

    (tmp_path / 'app.py').write_text('before\n')
    monkeypatch.setenv('QUADRATUS_CLI_BINARY_CLAUDE', sys.executable)
    def launch(*args, **kwargs):
        (Path(kwargs['cwd']) / 'app.py').write_text('preserved edit\n')
        return subprocess.CompletedProcess([], 0, json.dumps({
            'result': 'saved', 'usage': {'input_tokens': 4, 'output_tokens': 2},
        }), '')
    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    monkeypatch.setattr('quadratus.runtime.build_provider',
                        lambda *a, **kw: ClaudeCLIProvider('fable', **kw))
    def run(self, **kwargs):
        assert self.config.worker_budget.max_concurrent == 2
        self.invoke('claude:fable', 'edit', allow_writes=True)
        pytest.fail('must stop before another task')
    monkeypatch.setattr(Session, 'run', run)
    result = run_project('small change', tmp_path, Settings(backend='cli'),
                         allow_writes=True, run_limits=RunLimits(max_reported_tokens=4))
    assert not result.completed and 'RunBudgetExceeded' in result.error
    assert (tmp_path / 'app.py').read_text() == 'preserved edit\n'
    assert '+preserved edit' in result.diff
    budget = json.loads((result.run_dir / 'budget.json').read_text())
    assert budget['reported_tokens'] == 6
    row = json.loads((result.run_dir / 'invocations.jsonl').read_text())
    assert row['provider_outcome'] == 'ok'
    assert row['outcome'] == 'RunBudgetExceeded' and row['post_return_failure']
    assert (result.run_dir / budget['preserved_responses'][0]).read_text() == 'saved'


def test_storage_failure_stops_before_launch_and_does_not_leak_ticket(tmp_path):
    occupied = tmp_path / 'not-a-directory'
    occupied.write_text('occupied')
    b = RunBudget(RunLimits(), path=occupied / 'budget.json')
    p = provider(b, usage={'input_tokens': 1, 'output_tokens': 0})
    with pytest.raises(RunBudgetExceeded, match='budget_state_unavailable'):
        p.generate('must not run')
    assert p.calls == []
    assert b.snapshot()['in_flight'] == b.snapshot()['reserved_attempts'] == 0


def test_both_inflight_replies_are_kept_on_a_threshold_stop(tmp_path):
    b = RunBudget(RunLimits(max_reported_tokens=1), path=tmp_path / 'budget.json')
    first, _ = b.reserve()
    second, _ = b.reserve()
    for ticket, reply in [(first, 'first response'), (second, 'second response')]:
        with pytest.raises(RunBudgetExceeded):
            b.finish(ticket, {'input_tokens': 2, 'output_tokens': 0}, reply=reply)
    assert [(tmp_path / p).read_text() for p in b.snapshot()['preserved_responses']] == [
        'first response', 'second response',
    ]


def test_fleet_rejects_a_provider_that_skips_the_controlled_attempt_path(monkeypatch):
    class Bypasses(Scripted):
        def _generate_once(self, *args):
            pytest.fail('bypassed the controller')
    p = Bypasses('fake', api_key='fixture')
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **kw: p)
    fleet = Fleet(Settings(backend='cli'), run_budget=RunBudget(RunLimits()))
    with pytest.raises(RunBudgetExceeded, match='observed provider attempt'):
        fleet.invoke('openai:gpt-5.6-sol', 'must not bypass')


def test_unspecified_transport_timeout_is_bounded_and_restored():
    b = RunBudget(RunLimits(wall_seconds=5), clock=lambda: 0)
    p = provider(b, usage={'input_tokens': 1, 'output_tokens': 0})
    p.timeout = None
    assert p.generate('bounded') == 'answer'
    assert p.calls == [5]
    assert p.timeout is None
