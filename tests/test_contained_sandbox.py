"""The container is the boundary; a redundant inner sandbox may stand down.

Attempts 3 and 5 of the blind acceptance each spent a subscription window on an
OpenAI seat that could not read one file. The Codex CLI sandboxes model-run
shell commands with bubblewrap, which needs an unprivileged user namespace, and
the acceptance container denies that through Docker's seccomp profile.

Measured on 2026-09-17 against the acceptance image: `seccomp=unconfined` makes
the inner sandbox work, `cap-add SYS_ADMIN` does not. So the only working route
to keep both sandboxes opens the surface behind most container escapes, for
every process in the container, to enable a second sandbox inside a boundary
that already holds. The operator chose the container and stood the inner
sandbox down -- there, and only there.
"""
import json

import pytest

from quadratus.cli_providers import (
    CLI_SPECS,
    ClaudeCLIProvider,
    CodexCLIProvider,
    GrokCLIProvider,
    contained,
)


def _binary_on_path(monkeypatch):
    # These tests read the argv a seat would run, never the binary itself; a
    # runner with no vendor CLI on PATH must build the same argv as a laptop
    # with all three, or argv[0] comes back None and the comparison is moot.
    monkeypatch.setattr('shutil.which', lambda name: f'/usr/bin/{name}')


def _restricted(cls, model, monkeypatch, *, inside):
    _binary_on_path(monkeypatch)
    if inside:
        monkeypatch.setenv('QUADRATUS_CONTAINED', '1')
    else:
        monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    provider = cls(model=model)
    return provider.for_seat(model, effort='low', restricted=True)._build_argv('P', 'S')


def _agentic(cls, model, monkeypatch, *, inside, writes):
    """A senior seat: an orchestrator, lead, reviewer or consultant."""
    _binary_on_path(monkeypatch)
    if inside:
        monkeypatch.setenv('QUADRATUS_CONTAINED', '1')
    else:
        monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    provider = cls(model=model).for_seat(model, effort='high', restricted=False)
    return provider.in_directory('/tmp', allow_writes=writes)._build_argv('P', 'S')


# -- the assertion itself --------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    ('1', True), ('true', True), ('YES', True), ('Yes', True),
    ('0', False), ('false', False), ('', False), ('  ', False), ('maybe', False),
])
def test_containment_is_asserted_explicitly_and_never_inferred(monkeypatch, value, expected):
    monkeypatch.setenv('QUADRATUS_CONTAINED', value)
    assert contained() is expected


def test_containment_is_off_when_nobody_says_otherwise(monkeypatch):
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    assert contained() is False


# -- codex: the sandbox stands down inside, and only inside ----------------

def test_a_host_codex_seat_keeps_its_own_sandbox(monkeypatch):
    argv = _restricted(CodexCLIProvider, 'gpt-6-astra', monkeypatch, inside=False)
    assert '--sandbox' in argv and argv[argv.index('--sandbox') + 1] == 'read-only'
    assert 'danger-full-access' not in argv


def test_a_contained_codex_seat_stands_its_sandbox_down(monkeypatch):
    argv = _restricted(CodexCLIProvider, 'gpt-6-astra', monkeypatch, inside=True)
    assert argv[argv.index('--sandbox') + 1] == 'danger-full-access'
    assert 'read-only' not in argv


@pytest.mark.parametrize('writes, host_mode', [(False, 'read-only'), (True, 'workspace-write')])
def test_a_host_codex_seat_of_any_rank_keeps_its_own_sandbox(monkeypatch, writes, host_mode):
    argv = _agentic(CodexCLIProvider, 'gpt-6-astra', monkeypatch, inside=False, writes=writes)
    assert argv[argv.index('--sandbox') + 1] == host_mode
    assert 'danger-full-access' not in argv


@pytest.mark.parametrize('writes', [False, True])
def test_a_contained_senior_codex_seat_stands_its_sandbox_down_too(monkeypatch, writes):
    """The regression attempt 6 was stopped by.

    Substituting only on the restricted branch looks like the cautious choice
    and reaches nothing that matters: an orchestrator, lead, reviewer or
    consultant is a senior seat and is never restricted, and those are the
    only seats that had ever been blind. Attempt 6 ran with containment
    asserted and died at the same `bwrap` failure as attempt 5.
    """
    argv = _agentic(CodexCLIProvider, 'gpt-6-astra', monkeypatch, inside=True, writes=writes)
    assert argv[argv.index('--sandbox') + 1] == 'danger-full-access'
    assert 'read-only' not in argv and 'workspace-write' not in argv


def test_the_substitution_reaches_every_rank_of_codex_seat(monkeypatch):
    """Stated as one assertion, because the bug was a seat this never reached."""
    seats = {
        'restricted worker': _restricted(CodexCLIProvider, 'gpt-6-astra',
                                         monkeypatch, inside=True),
        'senior seat reading a fresh source copy':
            _agentic(CodexCLIProvider, 'gpt-6-astra', monkeypatch, inside=True, writes=False),
        'senior seat holding the project':
            _agentic(CodexCLIProvider, 'gpt-6-astra', monkeypatch, inside=True, writes=True),
    }
    assert {name: argv[argv.index('--sandbox') + 1] for name, argv in seats.items()} == {
        'restricted worker': 'danger-full-access',
        'senior seat reading a fresh source copy': 'danger-full-access',
        'senior seat holding the project': 'danger-full-access',
    }


# -- the other vendors are deliberately unchanged --------------------------

@pytest.mark.parametrize('cls, model', [(ClaudeCLIProvider, 'haiku'), (GrokCLIProvider, '')])
def test_a_tool_denial_needs_no_privilege_so_containment_changes_nothing(cls, model, monkeypatch):
    """Claude and Grok bound a restricted seat by removing tools, which costs
    nothing inside a container and still bounds what the model can reach for.
    Only a vendor whose restriction needs a privilege the container denies has
    anything to stand down."""
    outside = _restricted(cls, model, monkeypatch, inside=False)
    inside = _restricted(cls, model, monkeypatch, inside=True)
    assert outside == inside
    assert '--disallowed-tools' in inside


@pytest.mark.parametrize('cls, model', [(ClaudeCLIProvider, 'haiku'), (GrokCLIProvider, '')])
@pytest.mark.parametrize('writes', [False, True])
def test_containment_leaves_the_other_vendors_senior_seats_alone(cls, model, monkeypatch, writes):
    """The widened substitution must not widen to a vendor that declares none."""
    def stable(argv):
        # Grok writes its prompt to a uniquely named temporary file, so the
        # two argvs differ in that one element by design.
        return [x for x in argv if 'quadratus-prompt-' not in x]

    outside = _agentic(cls, model, monkeypatch, inside=False, writes=writes)
    inside = _agentic(cls, model, monkeypatch, inside=True, writes=writes)
    assert stable(outside) == stable(inside)
    assert len(outside) == len(inside)


def test_only_the_vendor_that_needs_it_declares_a_substitute():
    substitutes = {v: bool(s.contained_sandbox_args) for v, s in CLI_SPECS.items()}
    assert substitutes == {'claude': False, 'openai': True, 'grok': False}


def test_the_three_sandbox_modes_are_the_whole_surface():
    """Why there is no setting that keeps a write denial with the sandbox down.

    ``codex exec -s`` accepts read-only, workspace-write and
    danger-full-access, and each of the first two is the same bubblewrap
    mechanism. So the choice inside the container is the sandbox or the
    permission flag, never both, and what carries the permission axis instead
    is the disposable source copy and the container's read-only root.
    """
    spec = CLI_SPECS['openai']
    modes = {tuple(spec.readonly_args), tuple(spec.write_args),
             tuple(spec.contained_sandbox_args)}
    assert modes == {('--sandbox', 'read-only'), ('--sandbox', 'workspace-write'),
                     ('--sandbox', 'danger-full-access')}


# -- the preflight asks the question that would have caught this -----------

def test_only_the_vendor_with_an_inner_sandbox_declares_a_self_test(monkeypatch):
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    tests = {v: s.sandbox_selftest('/work/app.py') for v, s in CLI_SPECS.items()}
    assert tests['claude'] == [] and tests['grok'] == []
    assert tests['openai'] == ['sandbox', '-c', 'sandbox_mode=read-only',
                               '--', 'cat', '/work/app.py']


@pytest.mark.parametrize('inside, mode', [(False, 'read-only'), (True, 'danger-full-access')])
def test_the_self_test_uses_the_mode_the_run_will_really_use(monkeypatch, inside, mode):
    """Testing some other mode answers a question nobody asked.

    The first version tested a fixed mode and carried a note saying no
    model-free check could settle which mode ``exec`` would get. That was
    wrong -- ``codex sandbox`` honours ``-c sandbox_mode=`` like any other
    entry point. Measured in the acceptance container on 2026-09-17, reading
    one file from the mount: read-only and workspace-write both fail with
    bwrap's namespace error, danger-full-access returns the file.
    """
    if inside:
        monkeypatch.setenv('QUADRATUS_CONTAINED', '1')
    else:
        monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    assert CLI_SPECS['openai'].sandbox_selftest('/work/app.py') == [
        'sandbox', '-c', f'sandbox_mode={mode}', '--', 'cat', '/work/app.py']


def test_the_self_test_reads_a_real_file_rather_than_running_true():
    """A command that touches nothing can pass without answering the question."""
    argv = CLI_SPECS['openai'].sandbox_selftest('/work/TASK.md')
    assert argv[-2:] == ['cat', '/work/TASK.md']
    assert 'true' not in argv


def test_the_preflight_refuses_to_run_on_a_host(monkeypatch, tmp_path):
    """A host preflight proves nothing about the container the run will use."""
    source = (tmp_path / 'preflight.py')
    source.write_text(open('tools/acceptance/preflight.py').read())
    import subprocess
    import sys
    done = subprocess.run([sys.executable, str(source)], capture_output=True, text=True)
    assert done.returncode != 0
    assert 'Run only inside run_isolated' in (done.stderr + done.stdout)


def _preflight():
    import importlib.util
    spec = importlib.util.spec_from_file_location('pf', 'tools/acceptance/preflight.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_vendor_that_cannot_read_the_project_blocks_the_run(tmp_path):
    module = _preflight()

    work = tmp_path / 'work'
    work.mkdir()
    (work / 'app.py').write_text('x = 1\n')
    assert module._read_check(work)['ok'] is True
    assert module._read_check(tmp_path / 'absent')['ok'] is False

    class _Spec:
        binary = 'definitely-not-installed-anywhere'

        def sandbox_selftest(self, probe_file, env=None):
            return ['sandbox', '--', 'cat', probe_file]

    result = module._sandbox_check('openai', _Spec(), str(work / 'app.py'))
    assert result['applicable'] and not result['ok']

    class _NoSandbox(_Spec):
        binary = 'sh'

        def sandbox_selftest(self, probe_file, env=None):
            return []

    assert module._sandbox_check('claude', _NoSandbox(),
                                 str(work / 'app.py'))['applicable'] is False


def test_a_real_pass_is_the_mode_this_run_would_use(tmp_path):
    """A sandbox self-test the harness can actually satisfy, end to end.

    ``sh`` stands in for a vendor binary so the shape is exercised without
    needing a container: what is pinned is that a probe file is handed to the
    vendor, the command runs, and reading it counts as the pass.
    """
    module = _preflight()
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'app.py').write_text('x = 1\n')

    class _Spec:
        binary = 'cat'

        def sandbox_selftest(self, probe_file, env=None):
            return [probe_file]

    result = module._sandbox_check('openai', _Spec(), str(work / 'app.py'))
    assert result['applicable'] and result['ok'] and result['exit_code'] == 0


def test_an_unreadable_work_tree_is_a_blocker_and_stops_the_vendor_questions(tmp_path):
    """With no file the harness can read, a vendor check has nothing to ask for.

    It must not then report every vendor as fine by asking nothing.
    """
    module = _preflight()
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert module._read_check(empty)['ok'] is False


def test_the_launcher_asserts_containment_before_any_provider_is_built():
    """The assertion belongs to the launcher that already proved it is inside
    the container, not to a provider guessing from its surroundings."""
    text = open('tools/acceptance/blind_trial.py').read()
    assert "os.environ['QUADRATUS_CONTAINED'] = '1'" in text
    assert text.index('/.dockerenv') < text.index('QUADRATUS_CONTAINED')
    assert json  # the module's own import stays used by the rest of the file


# -- a disposable source copy is its own containment (series rule-b1ff751 f4, 2026-10-06) --

def _in_copy(cls, model, monkeypatch, *, disposable, writes=False):
    _binary_on_path(monkeypatch)
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    provider = cls(model=model).for_seat(model, effort='high', restricted=False)
    return provider.in_directory('/tmp/quadratus-review-x', allow_writes=writes,
                                 disposable=disposable)._build_argv('P', 'S')


def test_a_codex_reviewer_in_a_disposable_copy_may_write_inside_it(monkeypatch):
    """Under read-only, Python found no writable temporary directory and the
    reviewer could not run the project's pytest; it blocked the task on
    "cannot run the required check" with every grader passing."""
    argv = _in_copy(CodexCLIProvider, 'gpt-6-astra', monkeypatch, disposable=True)
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'


def test_the_copy_policy_writes_nowhere_but_the_copy(monkeypatch):
    """workspace-write adds /tmp and $TMPDIR and keeps configured roots by
    default (Codex review of c3abcf0): all three are switched off, so the
    working directory, the copy, is the only writable root."""
    argv = _in_copy(CodexCLIProvider, 'gpt-6-astra', monkeypatch, disposable=True)
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == '-c']
    assert 'sandbox_workspace_write.exclude_slash_tmp=true' in overrides
    assert 'sandbox_workspace_write.exclude_tmpdir_env_var=true' in overrides
    assert 'sandbox_workspace_write.writable_roots=[]' in overrides
    granted = _in_copy(CodexCLIProvider, 'gpt-6-astra', monkeypatch, disposable=False, writes=True)
    assert not [a for a in granted if a.startswith('sandbox_workspace_write.')], "a grant keeps its own policy"


def test_an_ungranted_codex_seat_outside_a_copy_keeps_read_only(monkeypatch):
    argv = _in_copy(CodexCLIProvider, 'gpt-6-astra', monkeypatch, disposable=False)
    assert argv[argv.index('--sandbox') + 1] == 'read-only'


def test_disposable_never_widens_a_granted_call(monkeypatch):
    """The flag describes the copy, not a grant: with writes it is dropped."""
    _binary_on_path(monkeypatch)
    provider = CodexCLIProvider(model='gpt-6-astra').for_seat('gpt-6-astra', effort='high', restricted=False)
    view = provider.in_directory('/tmp/x', allow_writes=True, disposable=True)
    assert view._disposable is False


@pytest.mark.parametrize('cls, model', [(ClaudeCLIProvider, 'opus'), (GrokCLIProvider, 'default')])
def test_the_other_vendors_argv_is_unchanged_by_the_copy(cls, model, monkeypatch):
    def flags(argv):   # grok writes its prompt to a fresh file per call, under the host's temp dir
        import os
        return [a for a in argv if not os.path.basename(a).startswith('quadratus-prompt-')]
    assert (flags(_in_copy(cls, model, monkeypatch, disposable=True))
            == flags(_in_copy(cls, model, monkeypatch, disposable=False)))


def test_only_codex_declares_copy_args():
    assert {vendor: bool(spec.copy_args) for vendor, spec in CLI_SPECS.items()} == {
        'claude': False, 'openai': True, 'grok': False}


def test_a_disposable_call_gets_a_temp_dir_inside_the_copy(monkeypatch, tmp_path):
    import subprocess

    from quadratus.cli_providers import CodexCLIProvider
    _binary_on_path(monkeypatch)
    seen = {}

    def launch(argv, **kwargs):
        seen['env'] = dict(kwargs['env'])
        seen['cwd'] = kwargs.get('cwd')
        return subprocess.CompletedProcess(argv, 0, '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n', '')
    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    copy_dir = tmp_path / 'quadratus-review-abc'
    copy_dir.mkdir()
    provider = CodexCLIProvider(model='gpt-6-astra').for_seat('gpt-6-astra', effort='high', restricted=False)
    view = provider.in_directory(str(copy_dir), allow_writes=False, disposable=True)
    try:
        view.generate('P')
    except Exception:  # noqa: BLE001 -- the envelope shape is not under test
        pass
    assert seen['env']['TMPDIR'] == str(copy_dir / '.quadratus-tmp')
    assert (copy_dir / '.quadratus-tmp').is_dir()


# -- the operator cannot widen or move the copy policy (Codex re-review of 9b8c056) --

@pytest.mark.parametrize('override', [
    '-c sandbox_workspace_write.exclude_slash_tmp=false',
    '-c sandbox_workspace_write.exclude_tmpdir_env_var=false',
    "-c sandbox_workspace_write.writable_roots=['/tmp/outside']",
    '--config sandbox_workspace_write.exclude_slash_tmp=false',
    '-csandbox_mode=danger-full-access',
    '--config=sandbox_mode=workspace-write',
    '-c "sandbox_workspace_write.exclude_slash_tmp" = false',
    '--add-dir /tmp/outside-copy',
    '--add-dir=/tmp/outside-copy',
    '--sandbox danger-full-access',
    '-s workspace-write',
    '--full-auto',
    '--dangerously-bypass-approvals-and-sandbox',
    '--yolo',
    '--cd /tmp/elsewhere',
    '-C /tmp/elsewhere',
    '-c sandbox_workspace_write.exclude_slash_tmp=true',   # agreeing, and still not the operator's to restate
    '--',
])
def test_an_operator_override_of_the_copy_boundary_is_refused_before_dispatch(monkeypatch, override):
    from quadratus.cli_providers import NativeControlOverride
    _binary_on_path(monkeypatch)
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    monkeypatch.setenv('QUADRATUS_CLI_ARGS_OPENAI', override)
    provider = CodexCLIProvider(model='gpt-6-astra').for_seat('gpt-6-astra', effort='high', restricted=False)
    view = provider.in_directory('/tmp/quadratus-review-x', allow_writes=False, disposable=True)
    with pytest.raises(NativeControlOverride, match='disposable source copy'):
        view._build_argv('P', 'S')


@pytest.mark.parametrize('override', ['-c model_reasoning_effort=high', '--profile fast', '-c features.x=false'])
def test_an_unrelated_operator_override_still_reaches_a_copy_call(monkeypatch, override):
    _binary_on_path(monkeypatch)
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    monkeypatch.setenv('QUADRATUS_CLI_ARGS_OPENAI', override)
    provider = CodexCLIProvider(model='gpt-6-astra').for_seat('gpt-6-astra', effort='high', restricted=False)
    argv = provider.in_directory('/tmp/quadratus-review-x', allow_writes=False, disposable=True)._build_argv('P', 'S')
    assert override.split()[-1] in argv or override.split('=')[-1] in ' '.join(argv)


def test_a_granted_call_keeps_the_operators_sandbox_override(monkeypatch):
    """The refusal is the copy's, not a new rule for grants (Codex: preserve granted behaviour)."""
    _binary_on_path(monkeypatch)
    monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    monkeypatch.setenv('QUADRATUS_CLI_ARGS_OPENAI', '--add-dir /tmp/elsewhere')
    provider = CodexCLIProvider(model='gpt-6-astra').for_seat('gpt-6-astra', effort='high', restricted=False)
    argv = provider.in_directory('/tmp/project', allow_writes=True)._build_argv('P', 'S')
    assert '--add-dir' in argv


def test_the_boundary_check_names_only_keys_never_values():
    from quadratus.cli_providers import codex_copy_boundary_conflicts
    found = codex_copy_boundary_conflicts(['-c', 'sandbox_workspace_write.writable_roots=["/Users/secret"]'])
    assert found == ['-c sandbox_workspace_write.writable_roots=…']
    assert codex_copy_boundary_conflicts(['-c', 'model=gpt-6-astra', '--profile', 'x']) == []
