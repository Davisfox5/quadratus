"""Real container checks are opt-in; they do not contact model providers."""

import json
import os
import subprocess
import time

import pytest

from quadratus.isolated_run import _credential_tree, run_isolated


def test_requires_immutable_image_and_separate_ordinary_trees(tmp_path):
    work = tmp_path / 'work'
    runtime = tmp_path / 'runtime'
    work.mkdir()
    runtime.mkdir()
    with pytest.raises(ValueError, match='immutable'):
        run_isolated(image='python:3.12-slim', work=work, runtime=runtime, command=['python'])
    with pytest.raises(ValueError, match='separate'):
        run_isolated(image='sha256:' + 'a' * 64, work=work, runtime=work, command=['python'])
    (work / 'leak').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='ordinary'):
        run_isolated(image='sha256:' + 'a' * 64, work=work, runtime=runtime, command=['python'])


@pytest.fixture
def container_trees(tmp_path):
    if os.environ.get('QUADRATUS_TEST_DOCKER') != '1':
        pytest.skip('Set QUADRATUS_TEST_DOCKER=1 for real container checks')
    image = subprocess.check_output(
        ['docker', 'image', 'inspect', 'python:3.12-slim', '--format', '{{.Id}}'], text=True).strip()
    base = tmp_path.resolve()
    work, runtime = base / 'work', base / 'runtime'
    work.mkdir()
    runtime.mkdir()
    return image, work, runtime


def test_real_container_cannot_read_examiner_or_write_runtime(container_trees):
    image, work, runtime = container_trees
    examiner = work.parent / 'withheld.txt'
    examiner.write_text('withheld scoring canary')
    (runtime / 'policy.txt').write_text('ordinary operating policy')
    (work / 'input.txt').write_text('application input')
    (work / 'probe.py').write_text('''import json, os
from pathlib import Path
result = {'application_read': Path('/work/input.txt').read_text() == 'application input',
          'runtime_read': Path('/opt/quadratus/policy.txt').is_file(),
          'examiner_visible': Path(''' + repr(str(examiner)) + ''').exists(),
          'host_home_visible': Path('/Users/davisfox').exists(),
          'fresh_home': not Path(os.environ['HOME']).exists(),
          'nonroot': os.getuid() != 0}
try:
    Path('/opt/quadratus/policy.txt').write_text('changed')
    result['runtime_writable'] = True
except OSError:
    result['runtime_writable'] = False
Path('/work/probe.json').write_text(json.dumps(result))
''')
    result = run_isolated(image=image, work=work, runtime=runtime,
                          command=['python', '/work/probe.py'], wall_seconds=20)
    assert result.outcome == 'success'
    assert json.loads((work / 'probe.json').read_text()) == {
        'application_read': True, 'runtime_read': True, 'examiner_visible': False,
        'host_home_visible': False, 'fresh_home': True, 'runtime_writable': False,
        'nonroot': True,
    }
    assert (runtime / 'policy.txt').read_text() == 'ordinary operating policy'


def test_watchdog_kills_detached_children_and_preserves_partial_files(container_trees):
    image, work, runtime = container_trees
    child = "from pathlib import Path; import time\nwhile True:\n Path('/work/tick').write_text(str(time.time_ns())); time.sleep(.05)"
    (work / 'slow.py').write_text('import subprocess, sys, time\n'
                                  f'subprocess.Popen([sys.executable, "-c", {child!r}], start_new_session=True)\n'
                                  'time.sleep(60)\n')
    result = run_isolated(image=image, work=work, runtime=runtime,
                          command=['python', '/work/slow.py'], wall_seconds=4)
    assert result.outcome == 'wall_deadline'
    tick = (work / 'tick').read_text()
    time.sleep(.2)
    assert (work / 'tick').read_text() == tick
    probe = subprocess.run(['docker', 'inspect', result.container_name], capture_output=True)
    assert probe.returncode != 0


@pytest.mark.parametrize('extra', ['.codex/config.toml', '.claude/CLAUDE.md',
                                 '.codex/sessions', 'examiner.json'])
def test_credentials_reject_configuration_history_and_other_files(tmp_path, extra):
    root = tmp_path / 'auth'
    root.mkdir(mode=0o700)
    extra = root / extra
    extra.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    extra.write_text('must not reach solver')
    extra.chmod(0o600)
    with pytest.raises(ValueError, match='only supported'):
        _credential_tree(root)


def test_credentials_reject_shared_permissions_and_symlinks(tmp_path):
    root = tmp_path / 'auth'
    root.mkdir(mode=0o700)
    vendor = root / '.codex'
    vendor.mkdir(mode=0o700)
    auth = vendor / 'auth.json'
    auth.write_text('{}')
    auth.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        _credential_tree(root)
    auth.unlink()
    auth.symlink_to(tmp_path / 'host-auth')
    with pytest.raises(ValueError, match='ordinary'):
        _credential_tree(root)


def test_real_container_copies_only_auth_into_ephemeral_home(container_trees):
    image, work, runtime = container_trees
    credentials = work.parent / 'auth'
    credentials.mkdir(mode=0o700)
    vendor = credentials / '.codex'
    vendor.mkdir(mode=0o700)
    source = vendor / 'auth.json'
    source.write_text('{"fixture": true}')
    source.chmod(0o600)
    (work / 'auth_probe.py').write_text('''import json, os
from pathlib import Path
home = Path(os.environ['HOME'])
auth = home / '.codex/auth.json'
result = {'seed_read': json.loads(auth.read_text()) == {'fixture': True},
          'home_files': sorted(str(p.relative_to(home)) for p in home.rglob('*') if p.is_file())}
auth.write_text('refreshed fixture')
try:
    Path('/run/solver-auth/.codex/auth.json').write_text('changed')
    result['seed_writable'] = True
except OSError:
    result['seed_writable'] = False
Path('/work/auth-probe.json').write_text(json.dumps(result))
''')
    result = run_isolated(image=image, work=work, runtime=runtime,
                          credentials=credentials, network=True,
                          command=['python', '/work/auth_probe.py'], wall_seconds=20)
    assert result.outcome == 'success'
    assert json.loads((work / 'auth-probe.json').read_text()) == {
        'seed_read': True, 'home_files': ['.codex/auth.json'], 'seed_writable': False}
    assert source.read_text() == '{"fixture": true}'
    assert subprocess.run(['docker', 'inspect', result.container_name], capture_output=True).returncode != 0
