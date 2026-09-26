"""Closeout summarizes evidence; it must not reopen the editing workflow."""

import json
import subprocess
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import GrokCLIProvider
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.providers import ProviderError
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
from quadratus.runtime import Fleet
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec, _closeout_excerpt


def test_closeout_supplies_diff_and_checks_without_edit_scope(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('before\n')
    store = ArtifactStore(tmp_path / 'artifacts')
    calls = []

    def invoke(model, prompt, **kwargs):
        calls.append((model, prompt, kwargs))
        return 'SUMMARY: updated\nREASONING: recorded test passed\nDEAD ENDS: none'

    session = Session('goal', store, invoke, config=SessionConfig(project=root))
    session._task_before = Project(root).contents()
    (root / 'app.py').write_text('after\n')
    scope = TaskScope(permitted_paths=['app.py'], intended_result='update', max_lines=10)
    spec = TaskSpec('t1', 'Change app.py', scope=scope)
    session._active_spec = spec
    session.checks = [{'passed': True, 'command': 'pytest', 'output': '1 passed'}]
    memory = TaskMemory('t1', 'grok:default', store)
    memory.record('user', spec.description)
    memory.record('assistant', 'Changed app.py')
    session._close_out('grok:default', spec, memory)
    key, prompt, kwargs = calls[-1]
    assert key == 'grok:default'
    assert kwargs == {'allow_writes': False}
    assert '-before' in prompt and '+after' in prompt and '1 passed' in prompt
    assert prompt.count(spec.description) == 1
    assert '## Scope of this task' not in prompt
    assert 'You may change only' not in prompt
    assert 'use tools' in prompt  # explicitly forbidden by the closeout instruction
    assert memory.refs[-1].kind == 'closeout-evidence-index'
    # Normal editing/review calls still receive scope constraints.
    session._invoke_model('grok:default', 'review')
    assert '## Scope of this task' in calls[-1][1]


def test_large_unicode_closeout_keeps_explicit_omissions_and_full_evidence(tmp_path):
    store = ArtifactStore(tmp_path / 'artifacts')
    prompts = []
    session = Session('goal', store, lambda key, prompt: prompts.append(prompt) or
                      'SUMMARY: partial\nREASONING: evidence truncated\nDEAD ENDS: none')
    text = '初' * 50_000 + 'FINAL UNRESOLVED FINDING'
    memory = TaskMemory('t1', 'grok:default', store)
    memory.record('assistant', text)
    session._close_out('grok:default', TaskSpec('t1', '任' * 40_000), memory)
    assert len(prompts[0].encode('utf-8')) < 32_000
    assert '[TRUNCATED:' in prompts[0] and 'FINAL UNRESOLVED FINDING' in prompts[0]
    assert any(text in store.get(key) for key in store.ids())
    assert len(_closeout_excerpt(text, 200).encode('utf-8')) <= 200


@pytest.fixture
def closeout_fleet(tmp_path, monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/fake/grok')
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'secret-source.py').write_text('project content must not be copied')
    provider = GrokCLIProvider('', workdir=root, timeout=900, max_retries=4, max_tokens=8000)
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **kw: provider)
    budget = RunBudget(RunLimits(), path=tmp_path / 'budget.json')
    ledger = DelegationLedger(path=tmp_path / 'invocations.jsonl')
    fleet = Fleet(Settings(backend='cli'), project=root, allow_writes=True,
                  run_budget=budget, delegation_ledger=ledger)
    return fleet, provider, budget, ledger, root


def test_closeout_keeps_same_model_and_shared_budget_without_project_snapshot(closeout_fleet, monkeypatch):
    fleet, owner, budget, ledger, root = closeout_fleet
    seen = []

    def launch(argv, **kwargs):
        cwd = Path(kwargs['cwd'])
        assert not list(cwd.iterdir())
        assert kwargs['timeout'] <= 60
        assert '--always-approve' not in argv
        assert argv[argv.index('--effort') + 1] == 'low'
        assert 'Read it to ground' not in ' '.join(argv)
        seen.append(cwd)
        return subprocess.CompletedProcess(argv, 0, json.dumps({
            'text': 'SUMMARY: done\nREASONING: supplied evidence\nDEAD ENDS: none',
            'stopReason': 'end_turn', 'usage': {'input_tokens': 30, 'output_tokens': 10}}), '')

    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    monkeypatch.setattr(fleet.project, 'snapshot', lambda: pytest.fail('source must not be copied'))
    with invocation('t1', 'closeout'):
        fleet.invoke('grok:default', 'summarize recorded work')
    assert budget.snapshot()['reported_tokens'] == 40
    assert budget.snapshot()['reserved_attempts'] == 1
    assert len(seen) == 1 and not seen[0].exists()
    event = ledger.events[-1]
    assert event.requested_model == 'grok:default' and event.role == 'closeout'
    assert owner.timeout == 900 and owner.max_retries == 4 and owner.max_tokens == 8000
    assert not getattr(owner, 'summary_only', False)
    assert Path(owner.workdir) == root


def test_closeout_unknown_usage_stops_budget_and_cleans_scratch(closeout_fleet, monkeypatch):
    fleet, owner, budget, ledger, root = closeout_fleet
    seen = []

    def launch(argv, **kwargs):
        seen.append(Path(kwargs['cwd']))
        return subprocess.CompletedProcess(argv, 0, '{"text":"partial","stopReason":"end_turn"}', '')

    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    with invocation('t1', 'closeout'), pytest.raises(RunBudgetExceeded, match='unknown_usage'):
        fleet.invoke('grok:default', 'summarize')
    assert len(seen) == 1 and not seen[0].exists()
    assert budget.snapshot()['unknown_usage_attempts'] == 1
    assert ledger.events[-1].post_return_failure


def test_closeout_rejects_write_grants_and_oversized_prompts_before_invocation(closeout_fleet):
    fleet, _, budget, _, _ = closeout_fleet
    with invocation('t1', 'closeout'):
        with pytest.raises(ProviderError, match='write grant'):
            fleet.invoke('grok:default', 'summary', allow_writes=True)
        with pytest.raises(ProviderError, match='32,000-byte'):
            fleet.invoke('grok:default', '初' * 11_000)
    assert budget.snapshot()['reserved_attempts'] == 0


def test_closeout_timeout_is_one_attempt_and_normal_provider_stays_unchanged(closeout_fleet, monkeypatch):
    fleet, owner, budget, _, _ = closeout_fleet
    launches = []

    def launch(argv, **kwargs):
        launches.append((Path(kwargs['cwd']), kwargs['timeout']))
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    with invocation('t1', 'closeout'), pytest.raises(ProviderError, match='timed out'):
        fleet.invoke('grok:default', 'summarize')
    assert budget.snapshot()['stop_reason'] == 'unknown_usage'
    with pytest.raises(RunBudgetExceeded, match='unknown_usage'):
        budget.reserve()
    assert len(launches) == 1 and launches[0][1] <= 60
    assert not launches[0][0].exists()
    assert budget.snapshot()['reserved_attempts'] == 1
    assert owner.timeout == 900 and owner.max_retries == 4


def test_closeout_asks_for_recorded_decisions_not_reasoning(tmp_path):
    """GameTape run 10: the close-out's request for 'why' was refused by a
    vendor safeguard. The record asks for what the evidence states."""
    store = ArtifactStore(tmp_path / 'artifacts')
    prompts = []

    def invoke(model, prompt, **kwargs):
        prompts.append(prompt)
        return 'SUMMARY: done\nDECISIONS: kept the parser pure (stated in turn 2)\nDEAD ENDS: none'

    session = Session('goal', store, invoke, config=SessionConfig())
    memory = TaskMemory('t1', 'claude:opus', store)
    summary, decisions, dead_ends = session._close_out('claude:opus', TaskSpec('t1', 'do it'), memory)
    assert 'DECISIONS:' in prompts[-1] and 'REASONING:' not in prompts[-1]
    assert 'why' not in prompts[-1].split('Task description')[0].lower()
    assert summary == 'done' and decisions == 'kept the parser pure (stated in turn 2)'


def test_an_older_reasoning_section_still_parses():
    from quadratus.session import _parse_closeout
    assert _parse_closeout('SUMMARY: s\nREASONING: r')[1] == 'r'


def test_a_refused_closeout_keeps_a_harness_record_without_retrying(tmp_path):
    """The refusal stands: one call, no retry or other model, and the task
    closes on facts the harness measured, labelled as not model-written."""
    from quadratus.providers import ProviderRefusal
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('before\n')
    store = ArtifactStore(tmp_path / 'artifacts')
    calls, notes = [], []

    def invoke(model, prompt, **kwargs):
        calls.append(model)
        raise ProviderRefusal('safeguard declined the request')

    session = Session('goal', store, invoke, config=SessionConfig(project=root, progress=notes.append))
    session._task_before = Project(root).contents()
    (root / 'app.py').write_text('after\n')
    (root / 'new.py').write_text('x\n')
    session.checks = [{'passed': True, 'command': 'python -m pytest -q', 'output': '135 passed'}]
    memory = TaskMemory('t2', 'claude:opus', store)
    summary, decisions, dead_ends = session._close_out('claude:opus', TaskSpec('t2', 'do it'), memory)
    assert calls == ['claude:opus']
    assert summary.startswith('Close-out not written: claude:opus declined')
    assert 'changed files: app.py, new.py' in summary
    assert 'PASSED: python -m pytest -q' in summary and 'artifact' in summary
    assert 'refused' in decisions and dead_ends == []
    assert any(ref.kind == 'closeout-refused' for ref in memory.refs)
    assert any('close-out refused' in n for n in notes)


def test_other_closeout_failures_still_raise(tmp_path):
    store = ArtifactStore(tmp_path / 'artifacts')

    def invoke(model, prompt, **kwargs):
        raise ProviderError('transport failed')

    session = Session('goal', store, invoke, config=SessionConfig())
    with pytest.raises(ProviderError, match='transport failed'):
        session._close_out('claude:opus', TaskSpec('t1', 'do it'), TaskMemory('t1', 'claude:opus', store))
