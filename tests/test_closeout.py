"""A task's record is written by the harness from what it holds; no model
summary call is made for it (Davis, 2026-10-21). The runtime's bounded
summary view (``Fleet._closeout``) keeps its own controls below: the
session never routes a task's close-out through it any more, and nothing
here may add a call."""

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import GrokCLIProvider
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.integration import GateReceipt, GateResult
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.providers import ProviderError
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
from quadratus.runtime import Fleet
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec


def _never(model, prompt, **kwargs):
    pytest.fail(f"a task record asked a model: {model}")


def _session_with_edit(tmp_path, **config):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('before\n')
    store = ArtifactStore(tmp_path / 'artifacts')
    session = Session('goal', store, _never, config=SessionConfig(project=root, **config))
    session._task_before = Project(root).contents()
    (root / 'app.py').write_text('after\n')
    (root / 'new.py').write_text('x\n')
    return session, store, root


def test_every_task_record_is_the_harness_s_own_and_asks_no_model(tmp_path):
    session, store, root = _session_with_edit(tmp_path)
    spec = TaskSpec('t1', 'Change app.py', scope=TaskScope(permitted_paths=['app.py', 'new.py'], max_lines=10))
    session._active_spec = spec
    digest = session._source_fingerprint()
    session.checks = [{'passed': True, 'command': 'pytest', 'output': '1 passed', 'source': digest,
                       'receipts': [{'id': 'unit', 'status': 'passed'}]}]
    session.review_dispositions['t1'] = [('claude:opus', 'reviewed: no findings')]
    memory = TaskMemory('t1', 'openai:gpt-5.6-sol', store)
    memory.record('user', spec.description)
    memory.record('assistant', 'Changed app.py\nCHANGED: ["app.py", "new.py"]')
    summary, reasoning, dead_ends = session._task_close_out('openai:gpt-5.6-sol', spec, memory)
    assert summary.startswith('Recorded by the harness, not a model (every task closes on the harness')
    assert 'Changed: app.py, new.py' in summary and f'source {digest[:12]}' in summary
    assert 'last check PASSED [unit: passed] on the current source' in summary
    assert 'Reviews: claude:opus: reviewed: no findings' in summary
    assert 'No decisions or dead ends were written by a model' in summary and dead_ends == []
    assert reasoning.startswith('No model-written decision record')
    kinds = [ref.kind for ref in memory.refs]
    assert 'closeout-evidence-index' in kinds and 'factual-closeout' in kinds
    index = store.get(next(ref.id for ref in memory.refs if ref.kind == 'closeout-evidence-index'))
    assert 'Recorded conversation: artifact' in index and 'Source diff captured by the harness: artifact' in index
    record = json.loads(store.get(next(ref.id for ref in memory.refs if ref.kind == 'factual-closeout')))
    assert record['source'] == digest and record['changed'] == ['app.py', 'new.py'] and record['reviews']
    assert any('-before' in store.get(key) and '+after' in store.get(key) for key in store.ids())


def test_a_check_on_another_source_or_a_failed_one_is_never_promoted(tmp_path):
    session, store, root = _session_with_edit(tmp_path)
    spec = TaskSpec('t1', 'do it')
    session._active_spec = spec
    session.checks = [{'passed': True, 'command': 'pytest', 'output': '1 passed', 'source': 'stale-digest'}]
    stale, _, _ = session._task_close_out('claude:opus', spec, TaskMemory('t1', 'claude:opus', store))
    assert 'last check PASSED (not shown to be on the current source)' in stale
    session.checks = [{'passed': False, 'command': 'pytest', 'output': '1 failed', 'source': session._source_fingerprint()}]
    failed, _, _ = session._task_close_out('claude:opus', spec, TaskMemory('t1', 'claude:opus', store))
    assert 'last check FAILED' in failed and 'PASSED' not in failed
    session.checks = []
    none, _, _ = session._task_close_out('claude:opus', spec, TaskMemory('t1', 'claude:opus', store))
    assert 'Checks: no check recorded' in none


def test_the_record_carries_design_evidence_proof_scenarios_and_open_items(tmp_path):
    session, store, root = _session_with_edit(tmp_path)
    spec = TaskSpec('t1', 'do it')
    session._active_spec = spec
    session.design_checks = [{'task': 't1', 'verified': False, 'problem': 'console error on load'}]
    session.evidence_obligations = [
        {'task': 't1', 'kind': 'acceptance', 'proof_id': 'P1', 'oid': 'o1', 'binding': 'bound',
         'cases': ['tests/test_app.py::test_add'], 'execution': 'ran'},
        {'task': 't1', 'kind': 'acceptance', 'proof_id': 'P2', 'oid': 'o2',
         'binding': "invalid declaration: 'tests/test_app.py::TestAdd' names a test class", 'cases': [],
         'execution': 'missing'},
        {'task': 't2', 'kind': 'acceptance', 'proof_id': 'P1', 'oid': 'o3', 'binding': 'bound', 'cases': ['x'],
         'execution': 'ran'},
    ]
    session.unresolved_reviews = [{'task': 't1', 'text': 'negative sums untested'}]
    session.unexecuted_acceptance = [{'task': 't1', 'item': 'tests/test_cli.py'}]
    summary, _, _ = session._task_close_out('claude:opus', spec, TaskMemory('t1', 'claude:opus', store))
    assert 'Design: design evidence not verified: console error on load' in summary
    assert ('Proof scenarios: P1: binding bound (tests/test_app.py::test_add), execution ran; '
            "P2: binding invalid declaration: 'tests/test_app.py::TestAdd' names a test class, execution missing")
    assert 'P1: binding bound (tests/test_app.py::test_add), execution ran' in summary
    assert "P2: binding invalid declaration" in summary and 'o3' not in summary and ' x' not in summary
    assert 'Unresolved review findings: negative sums untested' in summary
    assert 'NOT RUN reports: tests/test_cli.py' in summary


def test_the_record_is_bounded_and_keeps_the_whole_evidence_as_artifacts(tmp_path):
    store = ArtifactStore(tmp_path / 'artifacts')
    session = Session('goal', store, _never)
    session.unresolved_reviews = [{'task': 't1', 'text': f'finding {i} ' + 'x' * 400} for i in range(20)]
    text = '初' * 50_000 + 'FINAL UNRESOLVED FINDING'
    memory = TaskMemory('t1', 'grok:default', store)
    memory.record('assistant', text)
    summary, _, _ = session._task_close_out('grok:default', TaskSpec('t1', '任' * 40_000), memory)
    assert len(summary.encode('utf-8')) < 6_000 and 'and 12 more' in summary
    assert any(text in store.get(key) for key in store.ids()), 'the whole transcript is kept as evidence'
    assert session.config.codebase_map is None


def test_the_record_never_carries_a_gate_command_to_the_orchestrator(tmp_path):
    """Codex review of 04c73c2 holds for the harness record too: the record
    joins session memory, which renders into the next orchestrator prompt.
    Gate ids and outcomes only."""
    secret = '/private/tmp/examiner-7/grader.py'
    store = ArtifactStore(tmp_path / 'artifacts')
    session = Session('goal', store, _never, config=SessionConfig())
    receipt = GateReceipt(id='api-tests', status='failed', reason='nonzero exit', required=True,
                          command=f'python -m pytest {secret}', returncode=1,
                          output=f'{secret}:82: AssertionError', tests=5)
    result = GateResult(False, f'python -m pytest {secret}', 1, f'{secret}:82: AssertionError', (receipt,))
    session.checks = [{"passed": result.passed, "command": result.command, "output": result.output,
                       "cwd": "", "receipts": [dataclasses.asdict(r) for r in result.receipts]}]
    spec = TaskSpec('t2', 'do it')
    memory = TaskMemory('t2', 'claude:opus', store)
    summary, decisions, dead_ends = session._task_close_out('claude:opus', spec, memory)
    assert 'examiner-7' not in summary and 'grader.py' not in summary
    assert 'api-tests: failed' in summary and 'FAILED' in summary
    session.memory.absorb(memory.close(summary=summary, reasoning=decisions, dead_ends=dead_ends))
    orchestrator_view = session.memory.render(current='')
    assert 'Recorded by the harness' in orchestrator_view
    assert 'examiner-7' not in orchestrator_view and 'grader.py' not in orchestrator_view


def test_a_codebase_map_is_never_amended_by_a_task_record(tmp_path):
    from quadratus.codebase_map import CodebaseMap
    m = CodebaseMap(tmp_path / 'map.jsonl')
    store = ArtifactStore(tmp_path / 'artifacts')
    session = Session('goal', store, _never, config=SessionConfig(codebase_map=m))
    memory = TaskMemory('t1', 'claude:opus', store)
    memory.record('assistant', 'MAP NOTES:\n- conventions: errors are wrapped in AppError')
    summary, _, _ = session._task_close_out('claude:opus', TaskSpec('t1', 'do it'), memory)
    assert len(m) == 0 and 'the codebase map was not amended by this task' in summary


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
