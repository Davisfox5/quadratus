"""Run the actual shard CLI against tiny disposable repositories."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'tools' / 'ci'
spec = importlib.util.spec_from_file_location('ci_plan_runner_test', TOOLS / 'plan.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


def repo(tmp_path, body):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_one.py').write_text(body)
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'fixture'], cwd=tmp_path, check=True)
    p = plan.build(tmp_path, ['tests/test_one.py'], shards=1)
    (tmp_path / 'plan.json').write_text(json.dumps(p))
    return p


def run(tmp_path):
    return subprocess.run([sys.executable, str(TOOLS / 'run.py'), '--plan', 'plan.json',
                           '--shard', '0', '--output', 'result'], cwd=tmp_path,
                          env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                          capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize('outcome', ['pass', 'fail', 'mutate'])
def test_real_shard_keeps_outcomes_timing_and_source_binding(tmp_path, outcome):
    body = 'def test_one():\n    assert True\n'
    if outcome == 'fail':
        body = 'def test_one():\n    assert False\n'
    if outcome == 'mutate':
        body = "def test_one():\n    from pathlib import Path\n    Path(__file__).write_text('# modified')\n"
    repo(tmp_path, body)
    proc = run(tmp_path)
    receipt = json.loads((tmp_path / 'result/receipt.json').read_text())
    assert proc.returncode == receipt['exit_code']
    assert (proc.returncode == 0) == (outcome == 'pass'), proc.stdout + proc.stderr
    assert receipt['source_unchanged'] == (outcome != 'mutate')
    assert receipt['collected'] == receipt['finished'] == 1
    assert {r['phase'] for r in receipt['phases']} == {'setup', 'call', 'teardown'}
    assert (tmp_path / 'result/junit.xml').is_file()
    assert json.loads((tmp_path / 'result/current.json').read_text())['event'] == 'returned'


def test_changed_source_rejected_before_pytest(tmp_path):
    repo(tmp_path, 'def test_one():\n    assert True\n')
    (tmp_path / 'tests/test_one.py').write_text('raise RuntimeError("must not collect")')
    proc = run(tmp_path)
    assert proc.returncode != 0
    assert 'source inputs changed' in proc.stderr
    assert not (tmp_path / 'result').exists()


def test_empty_shard_never_falls_back_to_full_discovery(tmp_path):
    p = repo(tmp_path, 'raise RuntimeError("must not collect")')
    p['shards'] = [{'index': 0, 'files': []}]
    (tmp_path / 'plan.json').write_text(json.dumps(p))
    proc = run(tmp_path)
    assert proc.returncode != 0
    assert 'Empty shard' in proc.stderr
    assert not (tmp_path / 'result').exists()


def test_local_command_runs_uncommitted_test_change_once(tmp_path):
    import shutil

    repo(tmp_path, 'def test_one():\n    assert True\n')
    shutil.copytree(TOOLS, tmp_path / 'tools/ci', ignore=shutil.ignore_patterns('__pycache__'))
    subprocess.run(['git', 'add', 'tools'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--no-gpg-sign', '-qm', 'runner'], cwd=tmp_path, check=True)
    (tmp_path / 'tests/test_one.py').write_text('def test_one():\n    assert 1 + 1 == 2\n')
    proc = subprocess.run([sys.executable, str(tmp_path / 'tools/ci/check.py'), '--base', 'HEAD',
                           '--output', str(tmp_path / 'local-check')], cwd=tmp_path,
                          env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    p = json.loads((tmp_path / 'local-check/plan.json').read_text())
    assert p['changed'] == ['tests/test_one.py']
    r = json.loads((tmp_path / 'local-check/results/receipt.json').read_text())
    assert r['collected'] == r['finished'] == 1 and r['source_unchanged']


def test_inherited_filter_is_rejected_before_execution(tmp_path, monkeypatch):
    repo(tmp_path, "def test_one(): assert False\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nonexistent")
    proc = run(tmp_path)
    assert proc.returncode != 0 and 'plan controls test selection' in proc.stderr
    assert not (tmp_path / "result").exists()


def test_configuration_filter_cannot_hide_planned_case(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -k nonexistent\n")
    repo(tmp_path, "def test_one(): assert True\n")
    proc = run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads((tmp_path / "result/receipt.json").read_text())["collected"] == 1


def test_an_automatic_full_fallback_saves_its_plan_and_launches_no_pytest(tmp_path):
    """PR73 6716159070: a fallback the selector chose (here an unknown
    contract path) is the eight-shard CI's to run. The local command keeps the
    plan, writes a ci-required receipt and returns non-success without
    starting pytest; --full stays the explicit serial opt-in."""
    import shutil

    repo(tmp_path, 'def test_one():\n    assert True\n')
    shutil.copytree(TOOLS, tmp_path / 'tools/ci', ignore=shutil.ignore_patterns('__pycache__'))
    subprocess.run(['git', 'add', 'tools'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--no-gpg-sign', '-qm', 'runner'], cwd=tmp_path, check=True)
    (tmp_path / 'CONTRACT.md').write_text('read by nothing the selector can see\n')
    subprocess.run(['git', 'add', 'CONTRACT.md'], cwd=tmp_path, check=True)
    (tmp_path / 'tests/test_one.py').write_text("def test_one():\n    assert open('CONTRACT.md')\n")

    def check(out, *extra):
        return subprocess.run([sys.executable, str(tmp_path / 'tools/ci/check.py'), '--base', 'HEAD',
                               '--output', str(tmp_path / out), *extra], cwd=tmp_path,
                              env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                              capture_output=True, text=True, timeout=20)
    proc = check('auto')
    assert proc.returncode == 3, proc.stdout + proc.stderr
    p = json.loads((tmp_path / 'auto/plan.json').read_text())
    assert p['fallback'] is True and p['coverage'] == 'full'
    receipt = json.loads((tmp_path / 'auto/ci-required.json').read_text())
    assert receipt['status'] == 'ci-required' and receipt['reasons'] == p['reasons']
    assert not (tmp_path / 'auto/results').exists(), 'no pytest was launched'
    assert 'no local pytest launched' in proc.stdout
    proc = check('explicit', '--full')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    r = json.loads((tmp_path / 'explicit/results/receipt.json').read_text())
    assert r['collected'] == r['finished'] == 1


def test_a_selection_that_reaches_every_test_is_not_a_fallback(tmp_path):
    p = repo(tmp_path, 'def test_one():\n    assert True\n')
    assert p['coverage'] == 'full' and p['fallback'] is False, p['reasons']


def test_a_test_that_replaces_file_primitives_does_not_break_the_recorder(tmp_path):
    """The recorder writes its progress between a test's call and its teardown,
    while the test's replacements still stand. On the merged tree two tests
    replace Path.open and os.replace for their own run, and each ended its
    shard with an INTERNALERROR although every test had passed."""
    body = (
        'import os\nfrom pathlib import Path\n\n'
        'def test_one(monkeypatch):\n'
        '    def broken(*args, **kwargs):\n'
        '        raise OSError("disk went away")\n'
        '    monkeypatch.setattr(os, "replace", broken)\n'
        '    monkeypatch.setattr(Path, "open", lambda self, *a, **k: object())\n'
        '    assert True\n')
    repo(tmp_path, body)
    result = run(tmp_path)
    assert 'INTERNALERROR' not in result.stdout + result.stderr, result.stdout[-2000:]
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    receipt = json.loads((tmp_path / 'result/receipt.json').read_text())
    assert receipt['exit_code'] == 0 and receipt['finished'] == 1
    assert [p['outcome'] for p in receipt['phases']] == ['passed'] * 3
