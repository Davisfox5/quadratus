"""Regressions from the Grok/Claude review, across the real caller seams."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.memory import TaskMemory
from quadratus.session import Complexity, RunStalled, Session, SessionConfig, TaskSpec
from quadratus.workers import WorkerPool


@pytest.mark.parametrize('write', [False, True])
def test_lead_worker_request_reaches_runner_and_returns_evidence(tmp_path, write):
    calls = []
    def invoke(key, prompt, *, allow_writes=False):
        calls.append((key, prompt, allow_writes))
        if prompt == 'inspect this function':
            return 'worker evidence: found the cause'
        if 'The task is finished' in prompt:
            return 'SUMMARY: fixed\nREASONING: evidence'
        if 'worker evidence: found the cause' in prompt:
            return 'complete implementation'
        return ('WORKER {"errand":"code","instruction":"inspect this function",'
                '"write":' + str(write).lower() + '}')
    store = ArtifactStore(tmp_path / '.quadratus')
    session = Session('work', store, invoke=invoke, available=lambda key: True,
                      config=SessionConfig(project=tmp_path, allow_writes=write))
    result = session.run_task(TaskSpec('t1', 'fix', complexity=Complexity.SIMPLE))
    worker_call = next(c for c in calls if c[1] == 'inspect this function')
    assert worker_call[0] == 'claude:haiku'
    assert worker_call[2] is write
    assert any(r.author == 'claude:haiku' for r in result.refs)
    assert any('worker evidence' in prompt and 'You are leading' in prompt for _, prompt, _ in calls)


def test_ungranted_worker_edit_is_rejected_before_invocation(tmp_path):
    calls = []
    def invoke(key, prompt, **kwargs):
        calls.append(prompt)
        return 'WORKER {"errand":"code","instruction":"edit","write":true}'
    session = Session('work', ArtifactStore(tmp_path / '.quadratus'), invoke=invoke,
                      config=SessionConfig(project=tmp_path))
    with pytest.raises(RunStalled, match='without an operator write grant'):
        session.run_task(TaskSpec('t1', 'fix', complexity=Complexity.SIMPLE))
    assert len(calls) == 1


def test_internal_runner_typeerror_is_not_retried_without_write_grant(tmp_path):
    calls = []
    def invoke(key, prompt, *, allow_writes=False):
        calls.append(allow_writes)
        raise TypeError('bug inside the runner')
    pool = WorkerPool(store=ArtifactStore(tmp_path), run=invoke)
    with pytest.raises(TypeError, match='inside'):
        pool._run('claude:haiku', 'edit', allow_writes=True)
    assert calls == [True]


def test_failed_ungranted_worker_can_retry_with_a_grant(tmp_path):
    def invoke(key, prompt, *, allow_writes=False):
        if not allow_writes:
            raise PermissionError('needs a write grant')
        return 'done'
    store = ArtifactStore(tmp_path)
    pool = WorkerPool(store=store, run=invoke)
    task = TaskMemory('t1', 'claude:opus', store)
    args = dict(task=task, parent_key='claude:opus', prompt='edit', label='edit')
    with pytest.raises(PermissionError):
        pool.commission(**args)
    assert pool.commission(**args, allow_writes=True).summary == 'done'


def test_kind_prefixed_ask_is_answered_without_becoming_task(tmp_path):
    prompts = []
    def invoke(key, prompt):
        prompts.append(prompt)
        return 'KIND: backend\nASK: Which project feature?' if len(prompts) == 1 else 'DONE'
    session = Session('work', ArtifactStore(tmp_path), invoke=invoke,
                      config=SessionConfig(ask_operator=lambda q: 'addition'))
    assert session.next_task() is None
    assert 'addition' in prompts[1]


def test_project_api_seats_are_unavailable_instead_of_claiming_filesystem_access(tmp_path):
    from quadratus.runtime import Fleet
    fleet = Fleet(Settings(backend='api'), project=tmp_path)
    assert not fleet.available('openai:gpt-6-astra')
