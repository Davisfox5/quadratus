"""Role isolation, bounded reference data and measured editor delivery."""
import json
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.codebase_map import CodebaseMap
from quadratus.config import Settings
from quadratus.memory import TaskMemory
from quadratus.policy import PolicyError, load_policy
from quadratus.project import Project
from quadratus.runtime import Fleet
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec


@pytest.fixture
def packet_session(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'CLAUDE.md').write_text('Use decimal arithmetic for totals.')
    code_map = CodebaseMap(tmp_path / 'map.jsonl')
    code_map.amend(topic='conventions', note='Round only at the boundary.', author='operator')
    code_map.amend(topic='review', note='OTHER REVIEWER SECRET', author='reviewer')
    seen = []
    session = Session('goal', ArtifactStore(tmp_path / 'artifacts'),
                      lambda key, prompt, **kw: seen.append(prompt) or 'RESOLVED',
                      config=SessionConfig(project=root, repository_policy=load_policy(root),
                                           codebase_map=code_map))
    spec = TaskSpec('t1', 'Calculate totals', scope=TaskScope(['total.py'], acceptance=['2+2=4']))
    return session, spec, seen


def test_every_role_gets_scope_checklist_and_conventions_without_peer_history(packet_session):
    session, spec, seen = packet_session
    prompts = [session._lead_prompt(spec),
               session._collaborator_prompt(spec, 'draft', 'openai:gpt-5.6-sol'),
               session._consult_prompt(spec, 'question', 'openai:gpt-5.6-sol'),
               session._verifier_prompt(spec, 'draft', 'openai:gpt-5.6-sol')]
    task = TaskMemory(spec.description, session.store)
    session._recheck_blocking(spec, [('openai:gpt-5.6-sol', 'BLOCKING: first-private'),
                                   ('claude:opus', 'BLOCKING: second-private')], 'revision', task)
    prompts += seen
    for prompt in prompts:
        assert spec.scope.render() in prompt
        assert 'no network, database or filesystem access' in prompt
        assert 'Use decimal arithmetic' in prompt
        assert 'Round only at the boundary' in prompt
    assert 'second-private' not in seen[0]
    assert 'first-private' not in seen[1]
    for prompt in prompts[1:]:
        assert 'OTHER REVIEWER SECRET' not in prompt


def test_packet_caps_reference_bytes_and_never_cuts_required_scope(packet_session):
    session, spec, _ = packet_session
    policy = session.config.repository_policy
    (policy.root / 'CLAUDE.md').write_text('é' * 50000)
    packet = policy.role_packet(spec.scope, 'reviewer')
    assert len(packet.encode()) <= 12000
    assert 'truncated' in packet
    assert spec.scope.render() in packet
    policy.document['context'] = {'reference_bytes_limit': 1024}
    with pytest.raises(PolicyError, match='Required role packet exceeds'):
        policy.role_packet(spec.scope, 'lead')


def test_instruction_symlink_escape_is_refused(packet_session, tmp_path):
    session, spec, _ = packet_session
    path = session.project / 'CLAUDE.md'
    path.unlink()
    external = tmp_path / 'external'
    external.write_text('outside')
    path.symlink_to(external)
    with pytest.raises(PolicyError, match='leaves the project'):
        session._lead_prompt(spec)


def test_large_unicode_exclusion_list_is_complete_and_still_enforced(packet_session):
    session, spec, _ = packet_session
    policy = session.config.repository_policy
    forbidden = tuple(f'protected/évidence-{i:05d}.json' for i in range(2000))
    spec.scope.forbidden_paths = forbidden
    packet = policy.role_packet(spec.scope, 'lead')
    assert len(packet.encode()) > 12000
    assert spec.scope.render() in packet
    assert forbidden[0] in packet and forbidden[-1] in packet
    for path in forbidden:
        assert not spec.scope.permits(path)
    assert spec.scope.permits('total.py')
    # Many exclusions must not disguise an oversized task description.
    spec.scope.acceptance = ['x' * 13000]
    with pytest.raises(PolicyError, match='Required role packet exceeds'):
        policy.role_packet(spec.scope, 'lead')


@pytest.mark.parametrize('reply,status', [
    ('Updated\nCHANGED: ["a.py", "new.bin", "old.py"]', "match"),
    ('Updated\nCHANGED: ["a.py"]', "undeclared"),
    ('Updated', "missing"),
    ('CHANGED: ["../a.py"]', "undeclared"),
    ('CHANGED: ["a.py", "a.py", "new.bin", "old.py"]', "malformed"),
    ('CHANGED: []\nCHANGED: ["a.py", "new.bin", "old.py"]', "malformed"),
    # A request mid-work after edits is kept, not refused (GameTape run 5,
    # 2026-09-25); the task-level scope check still measures the edits.
    ('FETCH: anything', None),
])
def test_unrestricted_delivery_compares_added_modified_deleted_bytes(tmp_path, monkeypatch, reply, status):
    """The diff is measured over added, modified and deleted bytes; the
    declaration is classified against it and never refused (docs/DIRECTION.md)."""
    from quadratus.delegation import invocation, invocation_context
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.py').write_text('before')
    (root / 'old.py').write_text('delete')
    fleet = Fleet(Settings(backend='cli'), project=Project(root), allow_writes=True)
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **kw: object())
    monkeypatch.setattr(fleet, 'provider_for', lambda key: provider)

    def generate(*args):
        assert 'CHANGED:' in args[-1]
        (root / 'a.py').write_text('after')
        (root / 'old.py').unlink()
        (root / 'new.bin').write_bytes(b'\x00\xff')
        return reply

    monkeypatch.setattr(fleet, '_generate', generate)
    try:
        with invocation("t1", "lead"):
            assert fleet.invoke('claude:opus', 'edit', allow_writes=True) == reply
            report = invocation_context.get().get("changed_report")
        assert (root / 'a.py').read_text() == 'after'
        if status is None:
            assert report is None, "a request is served before any classification"
        else:
            assert report["status"] == status
            assert report["measured"] == ['a.py', 'new.bin', 'old.py']
    finally:
        fleet.close()


@pytest.mark.parametrize('reply', ['CHANGED: []', 'FETCH: artifact-id',
                                  'WORKER ' + json.dumps({'errand': 'read', 'instruction': 'read'})])
def test_no_change_delivery_and_control_requests(tmp_path, monkeypatch, reply):
    fleet = Fleet(Settings(backend='cli'), project=Project(tmp_path), allow_writes=True)
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **kw: object())
    monkeypatch.setattr(fleet, 'provider_for', lambda key: provider)
    monkeypatch.setattr(fleet, '_generate', lambda *a: reply)
    try:
        assert fleet.invoke('claude:opus', 'edit', allow_writes=True) == reply
    finally:
        fleet.close()


@pytest.mark.parametrize('role', ['lead', 'revision', 'gate-fix', 'security-fix', 'design-fix'])
def test_editing_roles_keep_lead_checklist_without_a_write_grant(packet_session, role):
    from quadratus.delegation import invocation
    session, spec, seen = packet_session
    session._active_spec = spec
    with invocation(spec.task_id, role):
        session._invoke_model('claude:opus', 'Revise the proposal')
    assert 'Role: lead' in seen[-1]
    assert 'Stop with an ASK' in seen[-1]
    assert 'Role: reviewer' not in seen[-1]


def test_worker_gets_scope_without_parent_family_packet(packet_session):
    from quadratus.delegation import invocation
    session, spec, seen = packet_session
    session._active_spec = spec
    with invocation(spec.task_id, 'worker:read-1', 'worker'):
        session._invoke_model('claude:haiku', 'Read this one definition')
    assert spec.scope.render() in seen[-1]
    assert '## Role packet' not in seen[-1]
    assert 'Family checklist' not in seen[-1]
