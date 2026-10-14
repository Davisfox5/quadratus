"""Explicit development execution boundary; importing this launches nothing.

Codex receives a fresh single-use config and one native permission profile.
Other providers and candidate checks retain the separately reviewed outer
answer-denial profile. The bounded worker bridge is the only permitted MCP.
This adapter is not benchmark admission or a change to product defaults.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from tools.step1_native_boundary import (
    isolated_solver_config,
    protected_codex_argv,
    require_no_worker_project_config,
)

_installation = threading.Lock()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cleanup_owned_scratch(home):
    """After owned shutdown, remove only the directory this controller created.

    Its identity receipt lives in the protected, controller-owned call home.
    Never follow a replacement symlink or delete a substituted directory.
    """
    record = Path(home) / 'native-scratch.json'
    if not record.exists():
        return
    held = json.loads(record.read_text())
    path = Path(held['path'])
    if not os.path.lexists(path):
        return
    info = path.lstat()
    if ([info.st_dev, info.st_ino, info.st_uid] != held['identity']
            or not stat.S_ISDIR(info.st_mode) or path.is_symlink()
            or not path.is_absolute() or not path.name.startswith('qw-native-')):
        raise ValueError('Scratch creation identity changed; refuse deletion')
    shutil.rmtree(path)


def protected_gate_argv(argv, outer_profile, *, cwd):
    """One macOS sandbox applies both the answer and scratch-copy policies.

    A nested sandbox refuses to start. Only the engine's restrictive scratch
    profile shape is composable; ordinary gate argv keep their old boundary.
    Copy-write exceptions come before the parent's private denies; scratch
    read denies come last, so neither policy's exceptions weaken the other.
    """
    argv = list(argv)
    if argv[:2] != ["/usr/bin/sandbox-exec", "-p"]:
        return ["/usr/bin/sandbox-exec", "-f", str(outer_profile), *argv]
    if len(argv) < 4 or not isinstance(argv[2], str):
        raise ValueError("Incomplete scratch sandbox argv")
    lines = argv[2].splitlines()
    expected = ["(version 1)", "(allow default)", "(deny network*)", "(deny file-write*)"]
    if lines[:4] != expected or len(lines) not in (5, 6):
        raise ValueError("Unexpected scratch sandbox policy")
    match = re.fullmatch(
        r'\(allow file-write\* \(subpath ("(?:[^"\\]|\\.)*")\) '
        r'\(literal "/dev/null"\) \(literal "/dev/tty"\)\)', lines[4])
    copy = Path(json.loads(match[1])) if match else None
    if copy is None or not copy.is_absolute() or not Path(cwd).resolve().is_relative_to(copy.resolve()):
        raise ValueError("Scratch sandbox does not hold this working directory")
    hidden = lines[5:]
    if hidden and not hidden[0].startswith("(deny file-read* "):
        raise ValueError("Unexpected scratch read policy")
    parent = [line for line in Path(outer_profile).read_text().splitlines()
              if line.strip() not in ("(version 1)", "(allow default)")]
    for line in parent:
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        operation = re.match(r"\((allow|deny)\s+([^\s()]+)", line.strip())
        if operation is None or (operation[1] == "allow" and not operation[2].startswith("file-read")):
            raise ValueError("Parent policy could expand scratch authority")
    profile = "\n".join([*expected, lines[4], *parent, *hidden]) + "\n"
    return ["/usr/bin/sandbox-exec", "-p", profile, *argv[3:]]


class NativeExecution:
    """Install only in an idle, trusted controller; restore hooks on exit.

    All paths/bytes supplied here must be held in the caller's launch manifest.
    The controller, not the candidate, owns auth/config/bridge construction.
    """

    def __init__(self, *, client, config, config_sha256, auth, homes,
                 protected, readonly_runtime, outer_profile, runtime_python,
                 worker_mcp, worker_mcp_sha256):
        self.client = Path(client).resolve(strict=True)
        self.config = Path(config).resolve(strict=True)
        self.config_sha256 = config_sha256
        self.auth = Path(auth)
        self.homes = Path(homes).resolve()
        self.protected = tuple(str(Path(p).resolve(strict=True)) for p in protected)
        self.readonly_runtime = tuple(str(Path(p).resolve(strict=True)) for p in readonly_runtime)
        self.outer_profile = Path(outer_profile).resolve(strict=True)
        self.runtime_python = str(Path(runtime_python).resolve(strict=True))
        self.worker_mcp = str(Path(worker_mcp).resolve(strict=True))
        self.worker_mcp_sha256 = worker_mcp_sha256
        self.current = contextvars.ContextVar('quadratus_native_execution', default=None)
        self.receipts = []
        self.auth_changed = False
        self.cleanup_failed = False

    def _bridge(self, provider, original_tool):
        from quadratus.worker_bridge import TOOL_TIMEOUT, input_schema
        tool = getattr(provider, 'worker_tool', None)
        if not tool or provider.summary_only:
            return {}
        if set(tool) != {'name', 'command', 'args', 'env', 'timeout'}:
            raise ValueError('Unreviewed worker bridge fields')
        if (tool['name'] != 'quadratus' or str(Path(tool['command']).resolve()) != self.runtime_python
                or tool['args'] != [self.worker_mcp] or tool['timeout'] != TOOL_TIMEOUT
                or sha(self.worker_mcp) != self.worker_mcp_sha256):
            raise ValueError('Worker bridge executable/identity drift')
        env = tool['env']
        if set(env) != {'QUADRATUS_WORKER_SOCKET', 'QUADRATUS_WORKER_TOKEN', 'QUADRATUS_WORKER_SCHEMA'}:
            raise ValueError('Unreviewed bridge environment')
        if json.loads(env['QUADRATUS_WORKER_SCHEMA']) != input_schema():
            raise ValueError('Worker bridge schema drift')
        token = env['QUADRATUS_WORKER_TOKEN']
        if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
            raise ValueError('Invalid bridge capability')
        raw_socket = Path(env['QUADRATUS_WORKER_SOCKET'])
        socket = raw_socket.resolve(strict=True)
        if (not raw_socket.is_absolute()
                or not stat.S_ISSOCK(socket.stat().st_mode)
                or stat.S_IMODE(socket.stat().st_mode) != 0o600
                or stat.S_IMODE(socket.parent.stat().st_mode) != 0o700
                or socket.stat().st_uid != os.getuid()):
            raise ValueError('Bridge socket must be private and controller-owned')
        argv = original_tool(provider, tool)
        if not argv or len(argv) % 2 or any(argv[i] != '-c' for i in range(0, len(argv), 2)):
            raise ValueError('Unqualified bridge argv')
        return dict(arg.split('=', 1) for arg in argv[1::2])

    @contextmanager
    def installed(self):
        if not _installation.acquire(blocking=False):
            raise ValueError('Execution adapter already installed in this controller')
        try:
            with self._installed():
                yield self
        finally:
            _installation.release()

    @contextmanager
    def _installed(self):
        import quadratus.cli_providers as cli
        import quadratus.integration as integration
        import quadratus.native_sessions as native
        if self.current.get() is not None:
            raise ValueError('Nested execution adapter')
        self.homes.mkdir(parents=True, exist_ok=False)
        os.chmod(self.homes, 0o700)
        originals = (cli.CLIProvider._call, cli.CLIProvider._build_argv,
                     cli.CLIProvider._worker_tool_argv, cli._launch, integration._run_owned,
                     cli._codex_journal_tool_failures, cli._codex_rollout_usage, native.codex_children)
        call, build, tool, launch, gate, journal, usage, children = originals

        def bounded_call(provider, *args, **kwargs):
            if provider.spec.vendor != 'openai':
                return call(provider, *args, **kwargs)
            if self.auth_changed or self.cleanup_failed:
                raise ValueError('Disposable subscription auth changed or cleanup failed; further native calls refused')
            require_no_worker_project_config(provider.workdir)
            if sha(self.config) != self.config_sha256:
                raise ValueError('Held operator config changed')
            cleaned = isolated_solver_config(self.config.read_text())
            if self.auth.is_symlink() or not self.auth.is_file():
                raise ValueError('Existing cached subscription auth required')
            home = Path(tempfile.mkdtemp(prefix='call-', dir=self.homes))
            os.chmod(home, 0o700)
            (home / 'config.toml').write_text(cleaned)
            os.chmod(home / 'config.toml', 0o600)
            state = {'home': home, 'provider': provider, 'launched': False}
            token = self.current.set(state)
            seed_digest = None
            original_exception = None
            try:
                seed = self.auth.read_bytes()
                seed_digest = hashlib.sha256(seed).digest()
                fd = os.open(home / 'auth.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(seed)
                del seed
                # Writable calls already have the native workspace profile's
                # ordinary TMPDIR grant. Keep their temporary files outside the
                # measured project. Read-only disposable copies retain their
                # existing inside-copy scratch authority; no grant is widened.
                state['scratch'] = Path(tempfile.mkdtemp(prefix='qw-native-',
                    dir=provider.workdir if getattr(provider, '_disposable', False) else None)).resolve()
                info = state['scratch'].lstat()
                (home / 'native-scratch.json').write_text(json.dumps({
                    'path': str(state['scratch']), 'identity': [info.st_dev, info.st_ino, info.st_uid]}))
                return call(provider, *args, **kwargs)
            except BaseException as exc:
                original_exception = type(exc).__name__
                raise
            finally:
                seed_path = home / 'auth.json'
                cleanup_errors = []
                rotated = False
                if seed_digest is not None:
                    try:
                        rotated = (seed_path.is_symlink() or not seed_path.is_file()
                                   or hashlib.sha256(seed_path.read_bytes()).digest() != seed_digest)
                    except OSError:
                        rotated = True
                self.auth_changed = self.auth_changed or rotated
                try:
                    seed_path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_errors.append(type(exc).__name__)
                if state.get('scratch'):
                    try:
                        cleanup_owned_scratch(home)
                    except (OSError, ValueError) as exc:
                        cleanup_errors.append(type(exc).__name__)
                self.cleanup_failed = self.cleanup_failed or bool(cleanup_errors)
                self.current.reset(token)
                violation = os.path.lexists(Path(provider.workdir) / '.codex')
                self.receipts.append({'home': str(home), 'launched': state['launched'],
                                      'native_seconds': state.get('native_seconds'),
                                      'scratch': str(state.get('scratch')),
                                      'scratch_removed': not state.get('scratch') or not state['scratch'].exists(),
                                      'auth_changed': rotated, 'original_exception': original_exception,
                                      'cleanup_errors': cleanup_errors,
                                      'auth_removed': not os.path.lexists(seed_path),
                                      'project_config_appeared': violation})
                if cleanup_errors:
                    raise ValueError('Native credential/scratch cleanup failed; work preserved')
                if violation:
                    raise ValueError('Worker project .codex appeared; work preserved, future calls refused')

        def bounded_argv(provider, prompt, system):
            argv = build(provider, prompt, system)
            if provider.spec.vendor != 'openai':
                return argv
            state = self.current.get()
            if state is None or state['provider'] is not provider:
                raise ValueError('Native argv outside fresh call context')
            trusted = self._bridge(provider, tool)
            return protected_codex_argv(argv, protected=self.protected, cwd=provider.workdir,
                                        readonly_runtime=self.readonly_runtime,
                                        tempdir=str(state['scratch']),
                                        trusted_worker_config=trusted)

        def bounded_launch(argv, *, input=None, timeout=None, cwd=None, env=None):
            state = self.current.get()
            if state is None:
                return launch(['/usr/bin/sandbox-exec', '-f', str(self.outer_profile), *argv],
                              input=input, timeout=timeout, cwd=cwd, env=env)
            if state['launched'] or Path(shutil.which(argv[0], path=(env or {}).get('PATH')) or argv[0]).resolve() != self.client:
                raise ValueError('Native client drift or reused invocation')
            state['launched'] = True
            permitted = ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ',
                         'QUADRATUS_MVP_OWNERSHIP')
            child_env = {k: v for k, v in (env or {}).items() if k in permitted}
            child_env['CODEX_HOME'] = str(state['home'])
            child_env['TMPDIR'] = str(state['scratch'])
            home = state['home']
            (home / 'native-input.txt').write_text(input or '')
            started = time.monotonic()
            def save(name, value):
                if isinstance(value, bytes):
                    value = value.decode(errors='replace')
                with (home / name).open('x') as stream:
                    stream.write(value or '')
            try:
                result = launch(argv, input=input, timeout=timeout, cwd=cwd, env=child_env)
            except subprocess.TimeoutExpired as exc:
                save('native-output.jsonl', exc.output)
                save('native-stderr.log', exc.stderr)
                raise
            else:
                save('native-output.jsonl', getattr(result, 'stdout', ''))
                save('native-stderr.log', getattr(result, 'stderr', ''))
                return result
            finally:
                state['native_seconds'] = time.monotonic() - started

        def bounded_gate(argv, *, cwd, timeout, env):
            return gate(protected_gate_argv(argv, self.outer_profile, cwd=cwd),
                        cwd=cwd, timeout=timeout, env=env)

        def root_for(root):
            state = self.current.get()
            return state['home'] / 'sessions' if state is not None else root

        cli.CLIProvider._call = bounded_call
        cli.CLIProvider._build_argv = bounded_argv
        cli._launch = bounded_launch
        integration._run_owned = bounded_gate
        cli._codex_journal_tool_failures = lambda root, *a, **k: journal(root_for(root), *a, **k)
        cli._codex_rollout_usage = lambda root, *a, **k: usage(root_for(root), *a, **k)
        native.codex_children = lambda root, *a, **k: children(root_for(root), *a, **k)
        try:
            yield self
        finally:
            (cli.CLIProvider._call, cli.CLIProvider._build_argv,
             cli.CLIProvider._worker_tool_argv, cli._launch, integration._run_owned,
             cli._codex_journal_tool_failures, cli._codex_rollout_usage, native.codex_children) = originals
