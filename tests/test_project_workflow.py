"""Real subprocesses, source edits and checks; vendor responses are scripted."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli import main
from quadratus.config import Settings
from quadratus.integration import IntegrationGate
from quadratus.project import Project
from quadratus.project_run import _project_lock, run_project
from quadratus.providers import ProviderError
from quadratus.runtime import Fleet
from quadratus.session import Session, SessionConfig

FAKE_CLI = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
def flag(key, default=''):
    return args[args.index(key) + 1] if key in args else default
if '--prompt-file' in args:
    prompt = Path(flag('--prompt-file')).read_text()
elif '-p' in args and not flag('-p').startswith('--'):
    prompt = flag('-p')
else:
    prompt = sys.stdin.read()
trace = Path(os.environ['FAKE_TRACE'])
with trace.open('a') as stream:
    stream.write(json.dumps({'cwd': os.getcwd(), 'args': args, 'prompt': prompt,
                            'source': Path('add.py').read_text() if Path('add.py').exists() else ''}) + '\n')
mode = os.environ.get('FAKE_MODE', 'success')
if 'Name the single next task' in prompt:
    marker = trace.with_suffix('.turn')
    reply = 'DONE' if marker.exists() else 'KIND: backend / standard\nFix add.py so add returns the sum.'
    marker.touch()
elif 'The task is finished' in prompt:
    reply = 'SUMMARY: addition implemented\nREASONING: verified source\nDEAD ENDS: none'
elif 'contributing an independent read' in prompt:
    Path('reviewer-poison.py').write_text('should never reach source')
    reply = 'NO FINDINGS'
elif 'You are leading' in prompt or 'Fix the failure' in prompt:
    if mode == 'crash':
        sys.exit('simulated vendor failure')
    if mode == 'readonly':
        Path('ungranted.py').write_text('should never reach source')
        reply = 'Proposed change: return a + b'
    elif mode == 'broken':
        Path('add.py').write_text('def add(a, b):\n    return a - b\n')
        reply = 'Everything is fixed.'
    else:
        Path('add.py').write_text('def add(a, b):\n    return a + b\n')
        reply = 'Saved add.py'
elif mode == 'patch':
    reply = 'PATCH:\n```diff\n--- a/add.py\n+++ b/add.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return 0\n+    return a + b\n```'
else:
    reply = 'NO FINDINGS'
if 'exec' in args:
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':reply}}))
elif '--output-format' in args and '--system-prompt-override' in args:
    print(json.dumps({'text':reply,'stopReason':'end_turn'}))
else:
    print(json.dumps({'result':reply,'is_error':False}))
'''


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'add.py').write_text('def add(a, b):\n    return 0\n')
    caller = tmp_path / 'unrelated-checkout'
    caller.mkdir()
    (caller / 'add.py').write_text('def add(a, b):\n    return a + b\n')
    monkeypatch.chdir(caller)
    binary = tmp_path / 'fake-vendor'
    binary.write_text(f'#!{sys.executable}\n' + FAKE_CLI)
    binary.chmod(0o755)
    for vendor in ['CLAUDE', 'OPENAI', 'GROK']:
        monkeypatch.setenv(f'QUADRATUS_CLI_BINARY_{vendor}', str(binary))
    trace = tmp_path / 'calls.jsonl'
    monkeypatch.setenv('FAKE_TRACE', str(trace))
    settings = Settings(backend='cli', max_retries=1)
    return Project(root), settings, trace, caller


def check_command():
    return shlex.join([sys.executable, '-B', '-c',
                       'from add import add; assert add(2, 3) == 5'])


def calls(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()]


def test_actual_edits_survive_cleanup_and_checks_target_selected_project(project_env):
    project, settings, trace, caller = project_env
    before_caller = (caller / 'add.py').read_bytes()
    result = run_project('Fix addition', project, settings, allow_writes=True,
                         check=check_command())
    assert result.completed, result.report
    assert 'return a + b' in (project.root / 'add.py').read_text()
    assert '+    return a + b' in result.diff
    assert (caller / 'add.py').read_bytes() == before_caller
    assert not (project.root / 'reviewer-poison.py').exists()
    data = json.loads((result.run_dir / 'result.json').read_text())
    assert data['checks'][0]['passed']
    assert data['checks'][0]['cwd'] == str(project.root)
    reviewers = [c for c in calls(trace) if 'contributing an independent read' in c['prompt']]
    assert reviewers and all('return a + b' in c['source'] for c in reviewers)
    assert all(not Path(c['cwd']).exists() for c in calls(trace) if c['cwd'] != str(project.root))
    assert ArtifactStore(result.run_dir / 'artifacts').ids()
    assert (result.run_dir / 'ledger.md').read_text()
    assert (result.run_dir / 'changes.diff').read_text() == result.diff


def test_clean_unrelated_checkout_cannot_hide_failed_project_changes(project_env, monkeypatch):
    project, settings, trace, caller = project_env
    monkeypatch.setenv('FAKE_MODE', 'broken')
    result = run_project('Fix addition', project, settings, allow_writes=True,
                         check=check_command())
    assert not result.completed
    assert 'Check FAILED' in result.report
    assert 'return a - b' in (project.root / 'add.py').read_text()
    assert '-    return 0' in result.diff
    assert not any('Name the single next task' in c['prompt'] for c in calls(trace)[-1:])


def test_readonly_calls_get_source_copies_and_cannot_change_source_by_relative_write(project_env, monkeypatch):
    project, settings, trace, caller = project_env
    monkeypatch.setenv('FAKE_MODE', 'readonly')
    before = project.contents()
    result = run_project('Review addition', project, settings)
    assert result.completed, result.report
    assert project.contents() == before
    assert not result.diff
    assert 'No source changes' in result.report
    assert 'No integration check ran' in result.report
    assert all(c['cwd'] != str(project.root) for c in calls(trace))


def test_model_failure_preserves_partial_report(project_env, monkeypatch):
    project, settings, trace, caller = project_env
    monkeypatch.setenv('FAKE_MODE', 'crash')
    result = run_project('Fix addition', project, settings, allow_writes=True)
    assert not result.completed and result.error
    assert 'simulated vendor failure' in result.report
    assert (result.run_dir / 'result.json').exists()


def test_restricted_editor_patch_is_applied_without_widening_tool_access(project_env, monkeypatch):
    project, settings, trace, caller = project_env
    monkeypatch.setenv('FAKE_MODE', 'patch')
    fleet = Fleet(settings, project=project, allow_writes=True)
    try:
        reply = fleet.invoke('grok:worker', 'Implement the addition patch', allow_writes=True)
    finally:
        fleet.close()
    assert 'Patch applied' in reply
    assert 'return a + b' in (project.root / 'add.py').read_text()
    argv = calls(trace)[0]['args']
    assert '-p' in argv and '--tools' in argv
    assert '--always-approve' not in argv
    assert '--permission-mode' not in argv


def test_cli_project_flag_selects_workflow_without_session_flag(project_env, monkeypatch, capsys):
    project, settings, trace, caller = project_env
    monkeypatch.setattr('quadratus.cli.Settings.from_env', lambda: settings)
    code = main(['Fix addition', '--project', str(project.root), '--allow-writes',
                 '--check', check_command()])
    assert code == 0
    assert 'Checked folder:' in capsys.readouterr().out
    assert 'return a + b' in (project.root / 'add.py').read_text()


def test_project_task_cap_is_reported_incomplete(project_env):
    project, settings, trace, caller = project_env
    result = run_project('Fix addition', project, settings, allow_writes=True, max_tasks=1)
    assert not result.completed
    assert 'task limit' in result.report


def test_project_lock_rejects_overlapping_runs(tmp_path):
    project = Project(tmp_path)
    with _project_lock(project), pytest.raises(ProviderError, match='Another Quadratus'):
        with _project_lock(project):
            pytest.fail('overlapping run accepted')


def test_gate_cannot_be_bound_to_another_directory(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(ValueError, match='session project'):
        Session('work', ArtifactStore(tmp_path / '.quadratus'), invoke=lambda *a: 'DONE',
                config=SessionConfig(project=tmp_path,
                                     integration_gate=IntegrationGate(['true'], cwd=other)))


def test_gate_that_changes_source_is_not_validated(tmp_path):
    store = ArtifactStore(tmp_path / '.quadratus')
    session = Session('work', store, invoke=lambda *a: 'DONE',
                      config=SessionConfig(project=tmp_path))
    gate = IntegrationGate([sys.executable, '-c',
                            'from pathlib import Path; Path("new.py").write_text("changed")'], cwd=tmp_path)
    result = session._check(gate)
    assert not result.passed
    assert 'source changed' in result.output


@pytest.mark.parametrize('name', ['../outside.py', '.quadratus/result.json', '.git/config', '.env', '/tmp/outside.py'])
def test_patch_rejects_non_source_paths(tmp_path, name):
    patch = f'--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+payload\n'
    with pytest.raises(ProviderError):
        Project(tmp_path).apply_patch(patch)


def test_patch_rejects_symlink_and_extended_git_renames(tmp_path):
    outside = tmp_path.parent / 'outside-source'
    outside.write_text('sentinel\n')
    (tmp_path / 'link.py').symlink_to(outside)
    with pytest.raises(ProviderError):
        Project(tmp_path).apply_patch('--- a/link.py\n+++ b/link.py\n@@ -1 +1 @@\n-sentinel\n+changed\n')
    with pytest.raises(ProviderError):
        Project(tmp_path).apply_patch('diff --git a/source b/.quadratus/result.json\nrename from source\nrename to .quadratus/result.json\n')
    assert outside.read_text() == 'sentinel\n'


def test_new_project_and_branch_are_real_git_tree(tmp_path):
    project = Project.open(tmp_path / 'new', branch='feature/addition')
    assert (project.root / '.git').is_dir()
    assert Project._command(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=project.root).strip() == 'feature/addition'


def test_clone_is_argument_safe_and_requires_empty_target(tmp_path, monkeypatch):
    invoked = []
    def command(argv, *, cwd, **kwargs):
        invoked.append(argv)
        Path(argv[-1]).mkdir()
        return ''
    monkeypatch.setattr(Project, '_command', command)
    target = tmp_path / 'clone'
    Project.open(target, clone_url='https://github.com/example/project.git')
    assert invoked == [['git', 'clone', '--', 'https://github.com/example/project.git', str(target)]]
    (target / 'keep').touch()
    with pytest.raises(ValueError, match='empty'):
        Project.open(target, clone_url='https://github.com/example/project.git')


def test_snapshot_omits_secrets_dependencies_state_and_symlinks(tmp_path):
    for name in ['.git', 'node_modules', '.quadratus']:
        (tmp_path / name).mkdir()
        (tmp_path / name / 'content').write_text('private')
    (tmp_path / '.env').write_text('secret')
    (tmp_path / '.env.example').write_text('example')
    (tmp_path / 'source.py').write_text('code')
    (tmp_path / 'linked').symlink_to(tmp_path / 'source.py')
    with Project(tmp_path).snapshot() as root:
        assert sorted(p.name for p in root.iterdir()) == ['.env.example', 'source.py']


def test_text_diff_roundtrips_files_without_final_newline(tmp_path):
    source = tmp_path / 'source.py'
    source.write_text('before')
    project = Project(tmp_path)
    before = project.contents()
    source.write_text('after')
    patch = project.diff(before)
    source.write_text('before')
    project.apply_patch(patch)
    assert source.read_text() == 'after'


def test_autodetected_python_check_uses_project_environment(tmp_path):
    from quadratus.repo_scan import scan_repo
    (tmp_path / 'tests').mkdir()
    assert scan_repo(tmp_path).check_command[0] == sys.executable
    python = tmp_path / '.venv' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    assert scan_repo(tmp_path).check_command[0] == str(python)
