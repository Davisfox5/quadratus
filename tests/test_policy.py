"""Offline contracts and dispatch checks. No vendor CLI or network calls."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli import main
from quadratus.gui import policy_preview_ui
from quadratus.policy import PolicyError, load_library, load_policy, preview_policy, task_gate
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec


def document():
    return {
        'kind': 'policy', 'schema_version': 1, 'repository_id': 'fixture/project',
        'library': {'version': 'pilot-1'},
        'defaults': {'family': 'pure-logic', 'required_gates': ['scope'],
                     'scope_max_lines': 50, 'overrun_ratio': 1.1, 'worker_depth': 1},
        'capability_policy': {'deny_write': ['.env', '.git/**'], 'sensitive': [],
                              'external_effects': 'deny', 'native_delegation': 'deny'},
        'path_rules': [], 'gate_bindings': {'unit-tests': {'absent': True, 'because': 'Fixture only'}},
        'gates': [{'id': 'scope', 'runner': 'builtin:scope', 'required': True}],
        'decisions': [],
    }


def write_policy(root, doc=None, text=None):
    path = root / '.quadratus/policy.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(text if text is not None else json.dumps(doc or document()))
    return path


def test_installed_library_identity_and_cards():
    origin, schemas, cards = load_library()
    assert len(cards) == 13
    assert origin['version'] == 'pilot-1'
    assert len(origin['digest']) == 64
    assert schemas['policy']['properties']['gate_bindings']
    assert 'runtime_model_policy' in [f['name'] for f in cards['llm-callsite']['adapter_fields']]


def test_preview_no_policy_no_writes_and_missing_adapters(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (tmp_path / 'tests').mkdir()
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*'))
    plan = preview_policy(tmp_path, ['add.py'])
    assert plan['primary_family'] == 'pure-logic'
    assert plan['adapters']['pure-logic']['test_runner']['status'] == 'detected'
    assert plan['adapters']['pure-logic']['typecheck_list']['status'] == 'not_configured'
    assert plan == preview_policy(tmp_path, ['add.py'])
    assert before == sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*'))


def test_unknown_test_runner_stays_unconfigured(tmp_path):
    plan = preview_policy(tmp_path)
    assert plan['adapters']['pure-logic']['test_runner']['value'] is None
    assert plan['absent_gates'][0]['id'] == 'unit-tests'


@pytest.mark.parametrize('change', [
    lambda d: d.update(unknown=True),
    lambda d: d.update(schema_version=1.0),
    lambda d: d['defaults'].update(worker_depth=2),
    lambda d: d['defaults'].update(family='made-up'),
    lambda d: d['defaults'].update(required_gates=['missing']),
    lambda d: d['gate_bindings'].update({'unit-tests': {}}),
    lambda d: d['gate_bindings'].update({'unit-tests': {'gate': 'missing'}}),
    lambda d: d['gate_bindings'].update({'unit-tests': {'absent': True}}),
    lambda d: d['gate_bindings'].clear(),
    lambda d: d['gates'].append(dict(d['gates'][0])),
    lambda d: d.update(instructions=['../elsewhere']),
    lambda d: d['gates'][0].update(cwd='/tmp'),
    lambda d: d['library'].update(digest='0' * 64),
    lambda d: d['library'].update(version='different'),
])
def test_invalid_policy_fails_closed(tmp_path, change):
    doc = document()
    change(doc)
    write_policy(tmp_path, doc)
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


@pytest.mark.parametrize('text', ['{"schema_version":2,"schema_\\u0076ersion":1}',
                                  '{"x":NaN}', '{"x":Infinity}', '{'])
def test_strict_json(tmp_path, text):
    write_policy(tmp_path, text=text)
    with pytest.raises(PolicyError):
        load_policy(tmp_path)


@pytest.mark.parametrize('name', ['../a', '/tmp/a', 'a/../../b', 'a\\b', 'C:/a', '~/.ssh'])
def test_invalid_declared_paths_and_forbids(tmp_path, name):
    with pytest.raises(PolicyError):
        preview_policy(tmp_path, [name])
    with pytest.raises(PolicyError):
        load_policy(tmp_path, forbid=[name])


def test_directory_policy_and_outside_symlink_deny(tmp_path):
    (tmp_path / '.quadratus/policy.json').mkdir(parents=True)
    with pytest.raises(PolicyError):
        load_policy(tmp_path)
    shutil.rmtree(tmp_path / '.quadratus')
    (tmp_path / 'alias').symlink_to(tmp_path.parent)
    with pytest.raises(PolicyError, match='leaves the project'):
        preview_policy(tmp_path, ['alias/new.py'])


def test_composition_and_gate_binding_are_explicit(tmp_path):
    _, _, cards = load_library()
    doc = document()
    doc['path_rules'] = [
        {'paths': ['src/api/'], 'families': ['scoped-endpoint'], 'overlays': ['tenant-isolation']},
        {'paths': ['tests/'], 'families': ['test-author'], 'overlays': ['test-quality']},
    ]
    for family in ['scoped-endpoint', 'test-author']:
        for gate in cards[family]['gate_ids']:
            if gate != 'scope':
                doc['gate_bindings'][gate] = {'absent': True, 'because': 'No fixture gate'}
    doc['gates'].append({'id': 'pytest', 'runner': 'command', 'argv': ['python', '-m', 'pytest'],
                         'required': True, 'minimum_tests': 1})
    doc['gate_bindings']['unit-tests'] = {'gate': 'pytest'}
    write_policy(tmp_path, doc)
    plan = preview_policy(tmp_path, ['tests/new.py', 'src/api/new.py'])
    assert plan['families'] == ['scoped-endpoint', 'test-author']
    assert plan['primary_family'] == 'scoped-endpoint'
    assert plan['overlays'] == ['tenant-isolation', 'test-quality']
    assert [g['id'] for g in plan['gates']] == ['scope', 'pytest']
    assert not plan['blocked']
    assert plan == preview_policy(tmp_path, ['src/api/new.py', 'tests/new.py'])
    doc['gate_bindings']['unit-tests'] = {'absent': True, 'because': 'Changed ruling'}
    write_policy(tmp_path, doc)
    assert plan['hash'] != preview_policy(tmp_path, ['tests/new.py', 'src/api/new.py'])['hash']


def test_incompatible_overlay_is_blocked_before_invocation(tmp_path):
    doc = document()
    doc['path_rules'] = [{'paths': ['logic/'], 'families': ['pure-logic'], 'overlays': ['privacy']}]
    write_policy(tmp_path, doc)
    calls = []
    session = Session('test', ArtifactStore(tmp_path / 'artifacts'),
                      lambda *a, **kw: calls.append(a),
                      config=SessionConfig(project=tmp_path, allow_writes=True,
                                           repository_policy=load_policy(tmp_path)))
    with pytest.raises(PolicyError, match='incompatible'):
        session.run_task(TaskSpec('t1', 'Fix logic', scope=TaskScope(['logic/new.py'])))
    assert calls == []
    assert session.policy_plans[0]['blocked']


def test_forbid_sensitive_alias_and_bounds(tmp_path):
    doc = document()
    doc['capability_policy']['sensitive'] = [
        {'paths': ['auth/'], 'requires': ['cross-vendor-ruling'], 'because': 'Operator owns auth'}]
    (tmp_path / 'auth').mkdir()
    (tmp_path / 'ordinary.py').symlink_to(tmp_path / 'auth/new.py')
    write_policy(tmp_path, doc)
    policy = load_policy(tmp_path, forbid=['keep.py'])
    assert policy.resolve(['ordinary.py'], writing=True)['blocked']
    assert policy.resolve(['keep.py'], writing=True)['blocked']
    assert not policy.resolve(['keep.py'], writing=False)['blocked']
    scope = policy.scope(TaskScope(['*'], max_lines=80))
    assert scope.max_lines == 50 and scope.overrun_ratio == 1.1
    assert not scope.permits('keep.py')
    assert not scope.permits('auth/new.py')
    assert scope.assess('--- a/a.py\n+++ b/a.py\n' + '+x\n' * 56).oversized


def test_gate_runner_never_silently_uses_legacy_for_policy(tmp_path):
    doc = document()
    doc['gates'].append({'id': 'unit-tests', 'runner': 'command', 'argv': ['python', '-m', 'pytest'],
                         'required': True, 'minimum_tests': 1})
    doc['gate_bindings'] = {}
    write_policy(tmp_path, doc)
    policy = load_policy(tmp_path)
    from quadratus import integration
    if not hasattr(integration, 'GateSuite'):
        with pytest.raises(PolicyError, match='require Q3'):
            task_gate(policy, policy.resolve(['a.py']), object())
    else:
        gate = task_gate(policy, policy.resolve(['a.py']), None)
        assert isinstance(gate, integration.GateSuite)


def test_cli_and_gui_preview_do_not_construct_providers(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError('Preview tried to construct a provider')
    monkeypatch.setattr('quadratus.cli.Orchestrator', fail)
    monkeypatch.setattr('quadratus.runtime.Fleet', fail)
    assert main(['--project', str(tmp_path), '--policy-preview', '--path', 'a.py']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['primary_family'] == 'pure-logic'
    assert main(['--project', str(tmp_path), '--policy-preview', '--allow-writes',
                 '--path', 'a.py', '--forbid', 'a.py']) == 1
    capsys.readouterr()
    result = policy_preview_ui(str(tmp_path), 'a.py', 'a.py', True)
    assert 'Write denied' in result and 'No model calls' in result
    assert not (tmp_path / '.quadratus').exists()


def test_wheel_loads_library_outside_source_tree(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Build a disposable source copy so packaging leaves no build/egg-info in the checkout.
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copytree(root / 'quadratus', source / 'quadratus', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('pyproject.toml', 'README.md', 'multi_model_workflow.py', 'chat_gui.py'):
        shutil.copyfile(root / name, source / name)
    wheels = tmp_path / 'wheels'
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation', '--no-index',
                    '--wheel-dir', str(wheels), str(source)], check=True, capture_output=True)
    installed = tmp_path / 'installed'
    wheel = next(wheels.glob('quadratus*.whl'))
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-index',
                    '--target', str(installed), str(wheel)], check=True, capture_output=True)
    shutil.rmtree(source)
    code = ('import sys; sys.path.insert(0, sys.argv[1]); '
            'from quadratus.policy import load_library; '
            'import quadratus; assert quadratus.__file__.startswith(sys.argv[1]); '
            'assert len(load_library()[2]) == 13; print("13 installed cards verified")')
    result = subprocess.run([sys.executable, '-I', '-c', code, str(installed)],
                            cwd=tmp_path, check=True, capture_output=True, text=True)
    assert '13 installed cards verified' in result.stdout


def test_dispatch_forbid_makes_zero_calls(tmp_path):
    calls = []
    session = Session('test', ArtifactStore(tmp_path / 'artifacts'),
                      lambda *a, **kw: calls.append(a),
                      config=SessionConfig(project=tmp_path, allow_writes=True,
                                           repository_policy=load_policy(tmp_path, forbid=['keep.py'])))
    with pytest.raises(PolicyError, match='Write denied'):
        session.run_task(TaskSpec('t1', 'Change keep.py', scope=TaskScope(['keep.py'])))
    assert not calls
    assert session.policy_plans[0]['declared_paths'] == ['keep.py']


def test_project_record_preserves_blocked_plan_without_provider_calls(tmp_path, monkeypatch):
    from quadratus.config import Settings
    from quadratus.project_run import run_project

    calls = []

    class FakeFleet:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    def factory(goal, store, *, config, **kwargs):
        session = Session(goal, store, lambda *a, **kw: calls.append(a), config=config)
        session.run = lambda **kw: session.run_task(
            TaskSpec('t1', 'Change keep.py', scope=TaskScope(['keep.py'])))
        return session

    monkeypatch.setattr('quadratus.runtime.Fleet', FakeFleet)
    monkeypatch.setattr('quadratus.runtime.new_session', factory)
    result = run_project('test', tmp_path, Settings(), allow_writes=True, forbid=['keep.py'])
    assert not calls and not result.completed
    assert 'Write denied' in result.error
    data = json.loads((result.run_dir / 'result.json').read_text())
    assert data['policy_plans'][0]['hash']
    assert data['policy_plans'][0]['blocked']
    preview = json.loads((result.run_dir / 'policy-plan.json').read_text())
    assert preview['hash'] == data['policy_preview']['hash']


def test_gui_registers_preview_controls_without_launch(monkeypatch):
    monkeypatch.setenv('GRADIO_ANALYTICS_ENABLED', 'False')
    from quadratus.config import Settings
    from quadratus.gui import build_interface
    app = build_interface(Settings())
    components = app.get_config_file()['components']
    labels = [c.get('props', {}).get('label') for c in components]
    assert 'Paths that must stay unchanged (one per line)' in labels
    assert 'Paths this task may change (one per line)' in labels
