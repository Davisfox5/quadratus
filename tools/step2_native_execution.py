"""Explicit development execution boundary; importing this launches nothing.

Codex receives a fresh single-use config and one native permission profile.
Other providers and candidate checks retain the separately reviewed outer
answer-denial profile. The bounded worker bridge is the only permitted MCP.
This adapter is not benchmark admission or a change to product defaults.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import shutil
import stat
import tempfile
import threading
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
        import json
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
            try:
                fd = os.open(home / 'auth.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(self.auth.read_bytes())
                return call(provider, *args, **kwargs)
            finally:
                (home / 'auth.json').unlink(missing_ok=True)
                self.current.reset(token)
                violation = os.path.lexists(Path(provider.workdir) / '.codex')
                self.receipts.append({'home': str(home), 'launched': state['launched'],
                                      'auth_removed': not (home / 'auth.json').exists(),
                                      'project_config_appeared': violation})
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
                                        tempdir=str(Path(provider.workdir) / '.quadratus-tmp'),
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
            child_env['TMPDIR'] = str(Path(cwd) / '.quadratus-tmp')
            Path(child_env['TMPDIR']).mkdir(exist_ok=True)
            return launch(argv, input=input, timeout=timeout, cwd=cwd, env=child_env)

        def bounded_gate(argv, *, cwd, timeout, env):
            return gate(['/usr/bin/sandbox-exec', '-f', str(self.outer_profile), *argv],
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
