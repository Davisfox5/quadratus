"""Regressions across the seams that failed Mac acceptance of f030fb3."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import CLIProvider, _extract_native_children
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, InvocationEvent, invocation, reconcile
from quadratus.native_sessions import codex_children
from quadratus.project_run import run_project
from quadratus.providers import LLMProvider, ProviderError
from quadratus.runtime import Fleet
from quadratus.scope import TaskScope, read_scope
from quadratus.session import PartialWorkStopped, RunStalled, Session, SessionConfig, TaskSpec

FABLE = 'claude:fable'
OPUS = 'claude:opus'
LUNA = 'openai:gpt-5.6-luna'
SCOPE = dict(permitted_paths=['a.py'], intended_result='Implement a',
             acceptance=['a is correct'], max_lines=10)
DECLARATION = 'KIND: backend standard\nSCOPE: ' + json.dumps(SCOPE) + '\nImplement a.'


@pytest.mark.parametrize('invalid', [{}, dict(SCOPE, permitted_paths=['../other']),
                                    dict(SCOPE, permitted_paths=['/**']),
                                    dict(SCOPE, permitted_paths=['**']),
                                    dict(SCOPE, max_lines=True), dict(SCOPE, max_lines=101),
                                    dict(SCOPE, acceptance=[])])
def test_scope_declarations_reject_missing_or_unbounded_values(invalid):
    with pytest.raises(ValueError):
        read_scope('SCOPE: ' + json.dumps(invalid), max_lines=100)


def test_normal_project_decision_repairs_scope_before_any_editor_runs(tmp_path):
    calls = []
    replies = iter(['KIND: backend standard\nImplement a.', DECLARATION])
    def invoke(key, prompt, **kw):
        calls.append((key, kw))
        return next(replies)
    session = Session('fix', ArtifactStore(tmp_path / '.quadratus'), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    spec = session.next_task()
    assert spec.scope.to_dict() == TaskScope(**SCOPE).to_dict()
    assert len(calls) == 2 and all(not kw['allow_writes'] for _, kw in calls)


def test_failed_scope_correction_cannot_claim_done(tmp_path):
    replies = iter(['KIND: backend standard\nImplement a.', 'DONE'])
    session = Session('fix', ArtifactStore(tmp_path / '.quadratus'), lambda *a, **k: next(replies),
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    with pytest.raises(RunStalled, match='Scope correction'):
        session.next_task()


def test_revision_scope_breach_stops_before_closeout_and_keeps_user_work(tmp_path):
    (tmp_path / 'a.py').write_text('old\n')
    (tmp_path / 'mine.txt').write_text('operator\n')
    def invoke(key, prompt, **kw):
        if 'You are leading' in prompt:
            (tmp_path / 'a.py').write_text('new\n')
            return 'draft'
        if 'contributing an independent read' in prompt:
            return 'BLOCKING: incorrect implementation'
        if kw.get('allow_writes'):
            (tmp_path / 'outside.py').write_text('partial\n')
            return 'revised'
        pytest.fail('Scope breach must prevent closeout')
    session = Session('fix', ArtifactStore(tmp_path / '.quadratus'), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    with pytest.raises(PartialWorkStopped):
        session.run_task(TaskSpec('t1', 'fix', complexity='standard', scope=TaskScope(**SCOPE)))
    assert session.in_flight['task'] == 't1'
    assert session.in_flight['invocation']['role'] == 'revision'
    assert 'outside.py' in session.in_flight['changed']
    assert (tmp_path / 'mine.txt').read_text() == 'operator\n'
    assert (tmp_path / 'outside.py').exists()
    assert not session.history


def test_selection_identity_is_canonical_and_task_specific():
    ledger = DelegationLedger()
    for task in ['t1', 't2']:
        ledger.record(InvocationEvent(task, 'lead', requested_model=OPUS, outcome='selected'))
    ledger.record(InvocationEvent('t1', 'lead', requested_model=OPUS, canonical_model=OPUS,
                                  resolved_model='claude-opus-5', invoked=True))
    assert ledger.selected_never_invoked() == [OPUS]  # t2 still never ran
    ledger.events.pop(1)
    assert ledger.selected_never_invoked() == []


class Attempts(LLMProvider):
    def _build_client(self):
        return object()

    def _call(self, *args):
        self.calls += 1
        if self.calls == 1:
            self.last_usage = {'input_tokens': 5, 'output_tokens': 1}
            raise RuntimeError('transient')
        self.last_usage = {'input_tokens': 7, 'output_tokens': 2}
        return 'ok'

    def _retryable(self, exc):
        return True


def test_retry_attempts_keep_worker_context_and_measured_usage(monkeypatch):
    provider = Attempts('opus', api_key='test', max_retries=2, retry_base_delay=0)
    provider.calls = 0
    monkeypatch.setattr('quadratus.providers.time.sleep', lambda _: None)
    ledger = DelegationLedger()
    fleet = Fleet(Settings(backend='cli'), delegation_ledger=ledger)
    monkeypatch.setattr(fleet, 'provider_for', lambda _: provider)
    with invocation('t3', 'worker:format-1', 'worker'):
        assert fleet.invoke(OPUS, 'edit') == 'ok'
    assert [e.attempt for e in ledger.events] == [1, 2]
    assert [e.outcome for e in ledger.events] == ['RuntimeError', 'ok']
    assert all(e.task == 't3' and e.origin == 'worker' for e in ledger.events)
    assert reconcile(ledger.events, [])['controlled_tokens'] == 15


def test_cancelled_worker_persists_unknown_invocation_and_task_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    calls = []
    def call(self, prompt, system, history):
        calls.append(self.model)
        if self.model == 'fable':
            return DECLARATION.replace('backend standard', 'architect complex')
        if self.model == 'opus':
            if (tmp_path / 'a.py').exists():
                return 'WORKER {"errand":"format","instruction":"second edit","write":true}'
            return 'WORKER {"errand":"format","instruction":"first edit","write":true}'
        if (tmp_path / 'a.py').exists():
            raise KeyboardInterrupt('operator stop')
        return 'PATCH:\n```diff\n--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+partial\n```'
    monkeypatch.setattr(CLIProvider, '_call', call)
    result = run_project('fix', tmp_path, Settings(backend='cli'), allow_writes=True, max_tasks=1)
    data = json.loads((result.run_dir / 'result.json').read_text())
    events = [json.loads(line) for line in (result.run_dir / 'invocations.jsonl').read_text().splitlines()]
    stopped = [e for e in events if e['outcome'] == 'KeyboardInterrupt']
    assert len(stopped) == 1 and stopped[0]['origin'] == 'worker'
    assert stopped[0]['task'] == 't1' and stopped[0]['role'] == 'worker:format-2'
    assert stopped[0]['input_tokens'] is None
    assert len(calls) == 5  # cancellation was not retried
    assert data['in_flight']['changed'] == ['a.py']
    assert data['in_flight']['invocation']['model'] == LUNA
    assert data['in_flight']['scope']['permitted_paths'] == ['a.py']
    assert data['in_flight']['invocation']['prompt_artifact']
    assert (result.run_dir / 'in-flight.json').exists()
    assert (tmp_path / 'a.py').read_text() == 'partial\n'
    assert not result.completed


def test_patch_rejection_is_recorded_after_provider_return(tmp_path, monkeypatch):
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    monkeypatch.setattr(CLIProvider, '_call', lambda *a: 'bad patch')
    ledger = DelegationLedger()
    fleet = Fleet(Settings(backend='cli'), project=tmp_path, allow_writes=True, delegation_ledger=ledger)
    with invocation('t1', 'worker:format-1', 'worker'), pytest.raises(ProviderError):
        fleet.invoke(LUNA, 'edit', allow_writes=True)
    assert ledger.events[0].post_return_failure
    assert ledger.events[0].outcome == 'ProviderError'


def test_real_nested_wait_stream_records_unknown_native_activity():
    path = Path(__file__).parents[1] / 'docs/handoffs/reliability-repair/acceptance-f030fb3/prior-native-stream-excerpt.jsonl'
    children = _extract_native_children(path.read_text())
    assert children and any(c.tool_name == 'wait' and c.total_tokens is None for c in children)


def test_native_session_reconciliation_reads_linked_usage_not_inherited_or_unrelated(tmp_path):
    root = tmp_path / 'sessions'
    day = root / '2026/09/13'
    day.mkdir(parents=True)
    parent = '01a09bfe-a8ab-7422-ab3e-99cfacf53362'
    child = '01a09bfe-d4e3-78d3-bbec-8c0438c6ac62'
    def meta(ident, **more):
        return {'type': 'session_meta', 'payload': dict(id=ident, cwd=str(tmp_path),
                timestamp='2026-09-13T18:19:12+00:00', **more)}
    def usage(ordinal, inputs, outputs):
        return {'ordinal': ordinal, 'type': 'event_msg', 'payload': {'type': 'token_count',
                'info': {'total_token_usage': dict(input_tokens=inputs, output_tokens=outputs)}}}
    def write(name, events):
        path = day / name
        path.write_text('\n'.join(json.dumps(e) for e in events))
        return path
    write(f'rollout-2026-09-13T14-19-12-{parent}.jsonl', [meta(parent)])
    child_meta = meta(child, source={'subagent': {'thread_spawn': {'parent_thread_id': parent}}},
                      subagent_history_start_ordinal=9)
    write(f'rollout-2026-09-13T14-19-24-{child}.jsonl', [child_meta, usage(2, 999999, 99999),
          usage(10, 100, 10), usage(11, 127405, 7700), usage(12, 127405, 7700)])
    write('rollout-2026-09-13T14-19-30-unrelated.jsonl', [meta('unrelated'), usage(10, 900000, 0)])
    children = codex_children(root, parent, str(tmp_path))
    assert len(children) == 1 and children[0].total_tokens == 135105
    assert not codex_children(root, parent, str(tmp_path / 'wrong-cwd'))


@pytest.mark.skipif(not hasattr(os, 'killpg'), reason='POSIX process groups')
@pytest.mark.parametrize('parent_ignores', [False, True])
@pytest.mark.parametrize('stop', ['timeout', 'interrupt'])
def test_stop_kills_term_ignoring_child_even_if_parent_exits(tmp_path, parent_ignores, stop):
    marker = tmp_path / 'pids.json'
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    code = ('import os,json,signal,subprocess,sys,time\n'
            + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if parent_ignores else '')
            + f'child=subprocess.Popen([sys.executable,"-c",{child!r}])\n'
            + f'open({str(marker)!r},"w").write(json.dumps([os.getpgrp(),child.pid]))\n'
            + 'print("partial output",flush=True)\ntime.sleep(60)\n')
    driver = (
        'import os,signal,sys,threading,subprocess\n'
        'from quadratus.cli_providers import _launch\n'
        + ('threading.Timer(1, lambda: os.kill(os.getpid(), signal.SIGINT)).start()\n'
           if stop == 'interrupt' else '')
        + 'try:\n'
        + f'    _launch([sys.executable,"-c",{code!r}], timeout={30 if stop == "interrupt" else 1})\n'
        + 'except (KeyboardInterrupt, subprocess.TimeoutExpired) as exc:\n'
        + '    print(type(exc).__name__, getattr(exc,"output", ""), flush=True)\n'
    )
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, '-c', driver], capture_output=True,
                                text=True, timeout=12)
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - started < 11
        assert ('KeyboardInterrupt' if stop == 'interrupt' else 'TimeoutExpired') in result.stdout
        if stop == 'timeout':
            assert 'partial output' in result.stdout
        pgid, pid = json.loads(marker.read_text())
        proc = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
        assert not proc.stdout.strip() or proc.stdout.strip().startswith('Z')
    finally:
        if marker.exists():
            pgid, _ = json.loads(marker.read_text())
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass


def test_real_cli_write_grant_prevents_timeout_replay(tmp_path, monkeypatch):
    from quadratus.cli_providers import ClaudeCLIProvider
    from quadratus.providers import PartialWorkSuspected
    monkeypatch.setattr(CLIProvider, 'available', lambda _: True)
    # The binary is resolved from PATH when argv is built; CI runners have no
    # Claude CLI, so pin it like the other CLI provider tests do.
    monkeypatch.setattr('shutil.which', lambda _: '/usr/bin/claude')
    count = []
    def launch(*args, **kwargs):
        count.append(1)
        (tmp_path / 'partial.py').write_text('partial')
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    provider = ClaudeCLIProvider('opus', workdir=tmp_path, allow_writes=True, max_retries=3)
    with pytest.raises(PartialWorkSuspected):
        provider.generate('edit')
    assert len(count) == 1 and (tmp_path / 'partial.py').exists()


def test_scope_detects_binary_and_empty_file_expansion():
    report = TaskScope(permitted_paths=['a.py']).assess(
        'Binary file changed: outside.bin\nEmpty file added/deleted: empty.txt\n')
    assert report.out_of_scope == ['empty.txt', 'outside.bin']
