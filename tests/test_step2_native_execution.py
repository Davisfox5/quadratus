"""No providers: exercise call isolation and the real stdio/socket bridge."""
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from quadratus import cli_providers as cli
from quadratus.worker_bridge import WorkerBridge
from tools.step2_native_execution import NativeExecution, sha


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    private = tmp_path / 'private'
    work.mkdir()
    private.mkdir()
    config = tmp_path / 'config.toml'
    config.write_text('model="gpt-5.6-sol"\nmodel_reasoning_effort="low"\n'
                      'sandbox_mode="read-only"\n[mcp_servers.ambient]\ncommand="untrusted"\n')
    auth = tmp_path / 'auth.json'
    auth.write_text('disposable-test-auth')
    profile = tmp_path / 'answer.sb'
    profile.write_text('fake-held-profile')
    client = tmp_path / 'codex'
    client.write_text('fake-client')
    worker_mcp = Path(cli.__file__).with_name('worker_mcp.py')
    adapter = NativeExecution(client=client, config=config, config_sha256=sha(config),
                             auth=auth, homes=private / 'homes', protected=[private],
                             readonly_runtime=[], outer_profile=profile, runtime_python=sys.executable,
                             worker_mcp=worker_mcp, worker_mcp_sha256=sha(worker_mcp))
    monkeypatch.setenv('QUADRATUS_NATIVE_DELEGATION', 'off')
    provider = cli.CodexCLIProvider('gpt-5.6-sol', workdir=str(work), allow_writes=True)
    provider._client = str(client)
    return adapter, provider


def fake_call(provider, prompt, system, history):
    argv = provider._build_argv(prompt, system)
    return cli._launch(argv, input=prompt, cwd=provider.workdir, timeout=12,
                       env={'HOME': str(Path.home()), 'PATH': '', 'OPENAI_API_KEY': 'must-not-inherit',
                            'NODE_OPTIONS': 'untrusted-hook', 'QUADRATUS_MVP_OWNERSHIP': 'owned'})


def test_each_call_fresh_config_auth_removed_and_original_command_preserved(boundary, monkeypatch):
    adapter, provider = boundary
    seen = []
    def launch(argv, **kwargs):
        home = Path(kwargs['env']['CODEX_HOME'])
        parsed = tomllib.loads((home / 'config.toml').read_text())
        assert parsed['model'] == 'gpt-5.6-sol'
        assert parsed['model_reasoning_effort'] == 'low'
        assert 'mcp_servers' not in parsed and 'projects' not in parsed
        assert (home / 'auth.json').stat().st_mode & 0o777 == 0o600
        assert 'OPENAI_API_KEY' not in kwargs['env'] and 'NODE_OPTIONS' not in kwargs['env']
        assert kwargs['env']['QUADRATUS_MVP_OWNERSHIP'] == 'owned'
        assert argv[:2] == [str(adapter.client), 'exec']
        assert '--sandbox' not in argv and 'default_permissions="quadratus-step1-workspace-write"' in argv
        assert kwargs['input'] == 'same-natural-request' and kwargs['timeout'] == 12
        (home / 'config.toml').write_text((home / 'config.toml').read_text() + '\n[projects."test"]\ntrust_level="trusted"\n')
        seen.append(home)
        return 'actual-return-value'
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    before = adapter.config.read_bytes()
    with adapter.installed():
        assert provider._call('same-natural-request', '', []) == 'actual-return-value'
        assert provider._call('same-natural-request', '', []) == 'actual-return-value'
    assert len(set(seen)) == 2 and adapter.config.read_bytes() == before
    assert all(not (home / 'auth.json').exists() for home in seen)


def test_exception_and_project_config_appearance_preserved_without_auth(boundary, monkeypatch):
    adapter, provider = boundary
    def launch(argv, **kwargs):
        Path(provider.workdir, '.codex').mkdir()
        raise TimeoutError('actual timeout')
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    with adapter.installed(), pytest.raises(ValueError, match='appeared'):
        provider._call('request', '', [])
    assert Path(provider.workdir, '.codex').is_dir()
    assert adapter.receipts[0]['auth_removed'] and adapter.receipts[0]['project_config_appeared']
    assert adapter.receipts[0]['original_exception'] == 'TimeoutError'


def test_journal_accounting_uses_exact_fresh_home_not_global_home(boundary, monkeypatch):
    adapter, provider = boundary
    monkeypatch.setattr(cli, '_codex_journal_tool_failures', lambda root, *a, **k: root)
    monkeypatch.setattr(cli, '_codex_rollout_usage', lambda root, *a, **k: root)
    import quadratus.native_sessions as native
    monkeypatch.setattr(native, 'codex_children', lambda root, *a, **k: root)
    def call(provider, *args, **kwargs):
        state = adapter.current.get()
        expected = state['home'] / 'sessions'
        assert cli._codex_journal_tool_failures('wrong-global') == expected
        assert cli._codex_rollout_usage('wrong-global') == expected
        assert native.codex_children('wrong-global') == expected
        return 'journal-read'
    monkeypatch.setattr(cli.CLIProvider, '_call', call)
    original = cli._codex_journal_tool_failures
    with adapter.installed():
        assert provider._call('', '', []) == 'journal-read'
    assert cli._codex_journal_tool_failures is original


@pytest.mark.parametrize('mutation', ['command', 'args', 'name', 'extra-env', 'schema', 'token'])
def test_unreviewed_bridge_refused_before_native_launch(boundary, monkeypatch, mutation):
    adapter, provider = boundary
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', lambda *a, **k: pytest.fail('native launch must not occur'))
    with WorkerBridge(lambda a: ('bounded-answer', False)) as bridge:
        provider.worker_tool = bridge.spec()
        if mutation in {'command', 'name'}:
            provider.worker_tool[mutation] = 'untrusted'
        elif mutation == 'args':
            provider.worker_tool['args'] = ['untrusted.py']
        elif mutation == 'extra-env':
            provider.worker_tool['env']['UNTRUSTED'] = 'reader'
        elif mutation == 'schema':
            provider.worker_tool['env']['QUADRATUS_WORKER_SCHEMA'] = '{}'
        else:
            provider.worker_tool['env']['QUADRATUS_WORKER_TOKEN'] = 'invalid'
        with adapter.installed(), pytest.raises((ValueError, FileNotFoundError)):
            provider._call('request', '', [])
        assert all(r['auth_removed'] for r in adapter.receipts)


def test_actual_stdio_bridge_returns_only_held_handler_answer(boundary):
    adapter, provider = boundary
    called = []
    def handler(args):
        called.append(args)
        return 'controller-held bounded answer', False
    with WorkerBridge(handler) as bridge:
        provider.worker_tool = bridge.spec()
        config = adapter._bridge(provider, cli.CLIProvider._worker_tool_argv)
        assert set(config) == {'mcp_servers.quadratus.' + key for key in
                               ('command', 'args', 'env', 'default_tools_approval_mode',
                                'tool_timeout_sec', 'startup_timeout_sec')}
        arguments = {'errand': 'read', 'instruction': 'describe public source'}
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                     'params': {'name': 'commission_worker', 'arguments': arguments}}]
        reply = subprocess.run([sys.executable, adapter.worker_mcp],
                               input=''.join(json.dumps(r) + '\n' for r in requests),
                               capture_output=True, text=True, timeout=5,
                               env=provider.worker_tool['env'], check=True)
        rows = [json.loads(line) for line in reply.stdout.splitlines()]
        assert called == [arguments]
        assert len(rows[1]['result']['tools']) == 1
        assert rows[2]['result'] == {'content': [{'type': 'text', 'text': 'controller-held bounded answer'}],
                                    'isError': False}


def test_config_drift_refused_before_credentials_or_launch(boundary, monkeypatch):
    adapter, provider = boundary
    adapter.config.write_text('model="different"')
    monkeypatch.setattr(cli.CLIProvider, '_call', lambda *a, **k: pytest.fail('launch forbidden'))
    with adapter.installed(), pytest.raises(ValueError, match='Held operator config'):
        provider._call('request', '', [])
    assert list(adapter.homes.iterdir()) == []


def test_non_codex_and_candidate_checks_keep_outer_answer_denial(boundary, monkeypatch):
    adapter, _ = boundary
    import quadratus.integration as integration
    seen = []
    monkeypatch.setattr(cli, '_launch', lambda argv, **k: seen.append(argv))
    monkeypatch.setattr(integration, '_run_owned', lambda argv, **k: seen.append(argv))
    with adapter.installed():
        cli._launch(['claude', 'held-argument'], cwd='held-cwd')
        integration._run_owned(['python', 'held-check'], cwd='held-cwd', timeout=3, env={})
    assert seen == [['/usr/bin/sandbox-exec', '-f', str(adapter.outer_profile), 'claude', 'held-argument'],
                    ['/usr/bin/sandbox-exec', '-f', str(adapter.outer_profile), 'python', 'held-check']]


def test_nested_installation_refused_and_original_hooks_restored(boundary):
    adapter, _ = boundary
    original = cli._launch
    with adapter.installed():
        with pytest.raises(ValueError, match='already installed'):
            with adapter.installed():
                pytest.fail('nested adapter must not install')
    assert cli._launch is original


def test_parallel_calls_do_not_share_home_or_journal_context(boundary, monkeypatch):
    adapter, provider = boundary
    import copy
    import threading
    from concurrent.futures import ThreadPoolExecutor
    barrier = threading.Barrier(2)
    seen = []
    def launch(argv, **kwargs):
        home = Path(kwargs['env']['CODEX_HOME'])
        barrier.wait(timeout=5)
        assert adapter.current.get()['home'] == home
        seen.append(home)
        return kwargs['input']
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    with adapter.installed(), ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(copy.copy(provider)._call, str(i), '', []) for i in range(2)]
        assert [f.result(timeout=6) for f in futures] == ['0', '1']
    assert len(set(seen)) == 2 and all(not (home / 'auth.json').exists() for home in seen)


def test_actual_timeout_output_preserved_and_seed_removed(boundary, monkeypatch):
    adapter, provider = boundary
    def launch(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 12, output=b'actual-partial-native-output', stderr=b'actual-timeout')
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    with adapter.installed(), pytest.raises(subprocess.TimeoutExpired):
        provider._call('original request', '', [])
    home = Path(adapter.receipts[0]['home'])
    assert (home / 'native-output.jsonl').read_text() == 'actual-partial-native-output'
    assert (home / 'native-stderr.log').read_text() == 'actual-timeout'
    assert (home / 'native-input.txt').read_text() == 'original request'
    assert not (home / 'auth.json').exists()


def test_transport_timeout_receipt_is_private_sanitized_and_keeps_unknown_usage(boundary, monkeypatch):
    adapter, provider = boundary
    from tools.native_call_diagnostics import LOG_FILTER
    def launch(argv, **kwargs):
        assert kwargs['env']['RUST_LOG'] == LOG_FILTER
        raw = ('INFO codex_otel.trace_safe: event.name="codex.websocket_connect" '
               'http.response.status_code=429 error.message="SECRET"\nnormal error\n')
        raise subprocess.TimeoutExpired(argv, 12, output='{"type":"turn.started"}', stderr=raw)
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    with adapter.installed(), pytest.raises(subprocess.TimeoutExpired) as failure:
        provider._call('original request', '', [])
    home = Path(adapter.receipts[0]['home'])
    receipt = json.loads((home / 'native-transport.json').read_text())
    assert receipt['outcome'] == 'local_deadline' and receipt['deadline_seconds'] == 12
    assert receipt['events'][0]['http.response.status_code'] == 429
    assert receipt['usage_total'] is None
    assert failure.value.stderr == 'normal error\n'
    assert 'SECRET' not in (home / 'native-transport.json').read_text()
    assert not (home / 'auth.json').exists()


@pytest.mark.parametrize('disposable', [False, True])
def test_temp_files_do_not_enter_measured_source_and_scratch_is_removed(boundary, monkeypatch, disposable):
    adapter, provider = boundary
    from quadratus.project import Project
    provider._disposable = disposable
    before = Project(provider.workdir).fingerprint()
    scratch = []
    def launch(argv, **kwargs):
        path = Path(kwargs['env']['TMPDIR'])
        assert path.is_relative_to(Path(provider.workdir)) is disposable
        (path / 'test-temp.txt').write_text('temporary test output')
        scratch.append(path)
        return 'answer'
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    with adapter.installed():
        provider._call('request', '', [])
    assert not scratch[0].exists()
    assert Project(provider.workdir).fingerprint() == before
    assert adapter.receipts[0]['scratch_removed']


def test_rotated_auth_is_removed_recorded_and_next_call_refused(boundary, monkeypatch):
    adapter, provider = boundary
    original = adapter.auth.read_bytes()
    launches = []
    def launch(argv, **kwargs):
        launches.append(argv)
        Path(kwargs['env']['CODEX_HOME'], 'auth.json').write_text('rotated-test-auth')
        return 'successful-answer'
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    with adapter.installed():
        assert provider._call('request', '', []) == 'successful-answer'
        with pytest.raises(ValueError, match='auth changed'):
            provider._call('request', '', [])
    assert len(launches) == 1 and adapter.auth.read_bytes() == original
    assert adapter.receipts[0]['auth_changed'] and adapter.receipts[0]['auth_removed']
    assert 'rotated-test-auth' not in json.dumps(adapter.receipts)


def test_replaced_scratch_is_not_deleted_and_further_calls_refused(boundary, monkeypatch):
    adapter, provider = boundary
    import shutil
    paths = []
    def launch(argv, **kwargs):
        path = Path(kwargs['env']['TMPDIR'])
        moved = path.with_name(path.name + '-moved')
        path.rename(moved)
        path.mkdir()
        (path / 'replacement.txt').write_text('retain replacement')
        paths.extend([path, moved])
        return 'answer'
    monkeypatch.setattr(cli.CLIProvider, '_call', fake_call)
    monkeypatch.setattr(cli, '_launch', launch)
    try:
        with adapter.installed():
            with pytest.raises(ValueError, match='cleanup failed'):
                provider._call('request', '', [])
            with pytest.raises(ValueError, match='further native calls refused'):
                provider._call('request', '', [])
        assert (paths[0] / 'replacement.txt').read_text() == 'retain replacement'
        assert adapter.receipts[0]['cleanup_errors'] == ['ValueError']
        assert adapter.receipts[0]['auth_removed']
    finally:
        for path in paths:
            shutil.rmtree(path)


def _scratch_policy(copy):
    return ('(version 1)\n(allow default)\n(deny network*)\n(deny file-write*)\n'
            f'(allow file-write* (subpath {json.dumps(str(copy))}) '
            '(literal "/dev/null") (literal "/dev/tty"))\n')


def test_composed_scratch_gate_keeps_one_launcher_and_private_denies(tmp_path):
    from tools.step2_native_execution import protected_gate_argv
    copy = tmp_path / "copy"
    copy.mkdir()
    parent = tmp_path / "outer.sb"
    parent.write_text('(version 1)\n(allow default)\n(deny file-write* (subpath "/private/operator"))\n')
    scratch = _scratch_policy(copy) + '(deny file-read* (subpath "/private/live"))\n'
    result = protected_gate_argv(['/usr/bin/sandbox-exec', '-p', scratch, 'python3', '-m', 'pytest'],
                                 parent, cwd=copy)
    assert result[:2] == ['/usr/bin/sandbox-exec', '-p']
    assert result[3:] == ['python3', '-m', 'pytest']
    assert result[2].count('(version 1)') == result[2].count('(allow default)') == 1
    assert result[2].index('(allow file-write*') < result[2].index('(deny file-write* (subpath')
    assert result[2].rstrip().endswith('(deny file-read* (subpath "/private/live"))')


def test_ordinary_gate_keeps_existing_private_launcher(tmp_path):
    from tools.step2_native_execution import protected_gate_argv
    parent = tmp_path / "outer.sb"
    argv = ['python3', '-m', 'unittest', 'discover']
    assert protected_gate_argv(argv, parent, cwd=tmp_path) == ['/usr/bin/sandbox-exec', '-f', str(parent), *argv]


@pytest.mark.parametrize("bad", ["outside", "relative", "extra_allow"])
def test_unexpected_scratch_gate_policy_is_rejected(tmp_path, bad):
    from tools.step2_native_execution import protected_gate_argv
    copy = tmp_path / "copy"
    copy.mkdir()
    profile = _scratch_policy(Path("relative") if bad == "relative" else copy)
    if bad == "extra_allow":
        profile += '(allow file-write*)\n'
    cwd = tmp_path if bad == "outside" else copy
    with pytest.raises(ValueError):
        protected_gate_argv(['/usr/bin/sandbox-exec', '-p', profile, 'python3'], tmp_path / "outer.sb", cwd=cwd)


@pytest.mark.parametrize("operation", ["file-write*", "network*"])
def test_parent_allow_exception_cannot_expand_scratch_authority(tmp_path, operation):
    from tools.step2_native_execution import protected_gate_argv
    copy = tmp_path / "copy"
    copy.mkdir()
    parent = tmp_path / "outer.sb"
    parent.write_text(f'(version 1)\n(allow default)\n(allow {operation})\n')
    with pytest.raises(ValueError, match="expand scratch authority"):
        protected_gate_argv(['/usr/bin/sandbox-exec', '-p', _scratch_policy(copy), 'python3'], parent, cwd=copy)


@pytest.mark.parametrize("line", [
    '(deny file-read* (subpath "/answers")) (allow file-write* (subpath "/out"))',
    '(allow file-read* (subpath "/x")) (allow network*)',
])
def test_a_second_clause_on_one_parent_line_is_refused(tmp_path, line):
    """Only the first clause of a line was checked, so a write or network
    allow after it was composed after the scratch denies and won."""
    from tools.step2_native_execution import protected_gate_argv
    copy = tmp_path / "copy"
    copy.mkdir()
    parent = tmp_path / "outer.sb"
    parent.write_text(f'(version 1)\n(allow default)\n{line}\n')
    with pytest.raises(ValueError, match="expand scratch authority"):
        protected_gate_argv(['/usr/bin/sandbox-exec', '-p', _scratch_policy(copy), 'python3'], parent, cwd=copy)


@pytest.mark.parametrize("line", [
    '(allow file-read-data (subpath "/x"))',
    '(deny file-read* (subpath "/odd)(path"))',
    '(deny file-read* (subpath "/a\\"b"))',
    '(deny file-read* (subpath "/answers")) ; note (allow file-write*)',
])
def test_one_clause_parent_lines_still_compose(tmp_path, line):
    """Quoted parentheses, an escaped quote and a trailing comment are one
    clause; each composes once, before the scratch read denies."""
    from tools.step2_native_execution import protected_gate_argv
    copy = tmp_path / "copy"
    copy.mkdir()
    parent = tmp_path / "outer.sb"
    parent.write_text(f'(version 1)\n(allow default)\n{line}\n')
    hidden = '(deny file-read* (subpath "/private/live"))\n'
    result = protected_gate_argv(['/usr/bin/sandbox-exec', '-p', _scratch_policy(copy) + hidden, 'python3'],
                                 parent, cwd=copy)
    assert result[2].count(line) == 1
    assert result[2].rstrip().endswith(hidden.strip())
