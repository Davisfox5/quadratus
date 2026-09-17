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


def _restricted(cls, model, monkeypatch, *, inside):
    if inside:
        monkeypatch.setenv('QUADRATUS_CONTAINED', '1')
    else:
        monkeypatch.delenv('QUADRATUS_CONTAINED', raising=False)
    provider = cls(model=model)
    return provider.for_seat(model, effort='low', restricted=True)._build_argv('P', 'S')


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


def test_an_unrestricted_codex_seat_is_untouched_by_containment(monkeypatch):
    """Containment substitutes for the *restricted* form only. An agentic seat
    still gets the permission axis it always had."""
    monkeypatch.setenv('QUADRATUS_CONTAINED', '1')
    provider = CodexCLIProvider(model='gpt-6-astra')
    argv = provider.for_seat('gpt-6-astra', effort='low', restricted=False)._build_argv('P', 'S')
    assert argv[argv.index('--sandbox') + 1] == 'read-only'


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


def test_only_the_vendor_that_needs_it_declares_a_substitute():
    substitutes = {v: bool(s.contained_restricted_args) for v, s in CLI_SPECS.items()}
    assert substitutes == {'claude': False, 'openai': True, 'grok': False}


# -- the preflight asks the question that would have caught this -----------

def test_only_the_vendor_with_an_inner_sandbox_declares_a_self_test():
    tests = {v: s.sandbox_selftest_args for v, s in CLI_SPECS.items()}
    assert tests['openai'] == ['sandbox', '--', 'true']
    assert tests['claude'] == [] and tests['grok'] == []


def test_the_preflight_refuses_to_run_on_a_host(monkeypatch, tmp_path):
    """A host preflight proves nothing about the container the run will use."""
    source = (tmp_path / 'preflight.py')
    source.write_text(open('tools/acceptance/preflight.py').read())
    import subprocess
    import sys
    done = subprocess.run([sys.executable, str(source)], capture_output=True, text=True)
    assert done.returncode != 0
    assert 'Run only inside run_isolated' in (done.stderr + done.stdout)


def test_a_vendor_sandbox_that_cannot_start_blocks_a_run_that_relies_on_it(tmp_path):
    """The two readings of the same fact, which is the whole point of the check."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('pf', 'tools/acceptance/preflight.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    work = tmp_path / 'work'
    work.mkdir()
    (work / 'app.py').write_text('x = 1\n')
    assert module._read_check(work)['ok'] is True
    assert module._read_check(tmp_path / 'absent')['ok'] is False

    class _Spec:
        binary = 'definitely-not-installed-anywhere'
        sandbox_selftest_args = ['sandbox', '--', 'true']

    result = module._sandbox_check('openai', _Spec())
    assert result['applicable'] and not result['ok']

    class _NoSandbox(_Spec):
        binary = 'sh'
        sandbox_selftest_args = []

    assert module._sandbox_check('claude', _NoSandbox())['applicable'] is False


def test_the_launcher_asserts_containment_before_any_provider_is_built():
    """The assertion belongs to the launcher that already proved it is inside
    the container, not to a provider guessing from its surroundings."""
    text = open('tools/acceptance/blind_trial.py').read()
    assert "os.environ['QUADRATUS_CONTAINED'] = '1'" in text
    assert text.index('/.dockerenv') < text.index('QUADRATUS_CONTAINED')
    assert json  # the module's own import stays used by the rest of the file
