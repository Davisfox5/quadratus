"""Operator selection proof; no provider/native model calls."""
import dataclasses
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateCommand, GateSuite
from quadratus.session import Session, SessionConfig, TaskSpec, _gate_identity
from tools.step2_test_selection import MARKER, WholeFileGateSuite, installed, render, whole_files


def command(*extra):
    return GateCommand('public-pytest', (str(Path(__import__('sys').executable)), '-m', 'pytest',
                       '-q', MARKER + 'tests', '--quadratus-report={report}', *extra),
                       minimum_tests=1, timeout=20)


def populate(root, name='test_one.py', body='def test_one():\n    assert 1 == 1\n'):
    target = root / 'tests' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


def test_every_default_file_and_new_inventory_are_named_whole(tmp_path):
    populate(tmp_path, 'test_one.py')
    populate(tmp_path, 'nested/other_test.py')
    populate(tmp_path, '.hidden/test_ignore.py')
    populate(tmp_path, '__pycache__/test_ignore.py')
    c = command()
    first = render(c, tmp_path)
    assert first.argv[3] == '-v' and '-q' not in first.argv
    assert list(first.argv[4:-1]) == ['tests/nested/other_test.py', 'tests/test_one.py']
    assert MARKER + 'tests' in c.argv, 'the configured logical command was not mutated'
    populate(tmp_path, 'test_added.py')
    assert 'tests/test_added.py' in render(c, tmp_path).argv


@pytest.mark.parametrize('directory', ['../private', '/private', ''])
def test_outside_or_empty_selection_is_refused(tmp_path, directory):
    populate(tmp_path)
    with pytest.raises(ValueError):
        whole_files(tmp_path, '.', directory)


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_symlink_cannot_select_private_files(tmp_path, kind):
    populate(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + '-private')
    outside.mkdir()
    private = outside / 'test_private.py'
    private.write_text('def test_private():\n    assert False\n')
    link = tmp_path / 'tests' / ('test_link.py' if kind == 'file' else 'linked')
    link.symlink_to(private if kind == 'file' else outside, target_is_directory=kind == 'directory')
    with pytest.raises(ValueError, match='symlink'):
        whole_files(tmp_path, '.', 'tests')


@pytest.mark.parametrize('name', ['pytest.ini', 'tests/conftest.py'])
def test_custom_discovery_is_not_silently_guessed(tmp_path, name):
    populate(tmp_path)
    (tmp_path / name).write_text('')
    with pytest.raises(ValueError, match='custom pytest'):
        whole_files(tmp_path, '.', 'tests')


def test_filter_refused_before_any_runner_call(tmp_path, monkeypatch):
    import quadratus.integration as integration
    populate(tmp_path)
    calls = []
    monkeypatch.setattr(integration, '_run_owned', lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match='filtered'):
        WholeFileGateSuite([command('-k', 'one')], cwd=tmp_path).run()
    assert not calls


def test_unselected_command_and_all_other_policy_values_are_preserved(tmp_path):
    populate(tmp_path)
    raw = GateCommand('unittest', ('python3', '-m', 'unittest', 'discover', '-s', 'tests'),
                      minimum_tests=12, timeout=60)
    assert render(raw, tmp_path) is raw
    c = dataclasses.replace(command(), minimum_tests=12, timeout=60, cheap=False)
    rendered = render(c, tmp_path)
    assert (rendered.id, rendered.required, rendered.minimum_tests, rendered.timeout, rendered.cwd,
            rendered.cheap, rendered.cacheable, rendered.skip_reason) == (
            c.id, c.required, c.minimum_tests, c.timeout, c.cwd, c.cheap, c.cacheable, c.skip_reason)


def test_logical_identity_is_stable_and_policy_is_bound(tmp_path):
    populate(tmp_path)
    suite = WholeFileGateSuite([command()], cwd=tmp_path)
    held = _gate_identity(suite)
    populate(tmp_path, 'test_added.py')
    assert _gate_identity(suite) == held
    assert _gate_identity(GateSuite([command()], cwd=tmp_path)) != held
    assert _gate_identity(WholeFileGateSuite([dataclasses.replace(command(), minimum_tests=2)],
                                           cwd=tmp_path)) != held


@pytest.mark.parametrize('skip,held', [(False, False), (True, True)])
def test_actual_complete_file_receipt_clears_and_skip_stays_held(tmp_path, skip, held):
    text = 'def test_one():\n    assert 1 == 1\n'
    if skip:
        text += 'import pytest\n@pytest.mark.skip(reason="synthetic negative")\ndef test_two():\n    assert False\n'
    project = tmp_path / 'project'
    populate(project, body=text)
    suite = WholeFileGateSuite([command()], cwd=project)
    result = suite.run()
    assert result.passed
    receipt = result.receipts[0]
    assert 'tests/test_one.py' in receipt.command and MARKER not in receipt.command
    assert receipt.cases['executed']['tests/test_one.py::test_one'] == 1
    # A real session bound to the project and this gate; the NOT RUN report
    # is recorded as a lead would make it and the check as the session
    # records one (source, gate, environment and command cwd), so the
    # discharge reads the actual current receipt identity.
    session = Session('goal', ArtifactStore(tmp_path / 'artifacts'), lambda *a, **k: '',
                      config=SessionConfig(project=project, requirements_ledger=True, integration_gate=suite))
    session.memory.ledger.requirements['R1'] = 'test_one passes'
    session._active_spec, session._current_covers = TaskSpec('inert', 'offline'), ['R1']
    session._record_not_run('openai:sol', 'revision', 'NOT RUN: tests/test_one.py::test_one - inert')
    [entry] = session.unexecuted_acceptance
    assert entry['cases'] == ['tests/test_one.py::test_one']
    session.checks.append({'passed': result.passed, 'command': result.command, 'output': result.output,
                           'receipts': [dataclasses.asdict(receipt)], 'command_cwd': '.',
                           'source': session._source_fingerprint(), 'gate': _gate_identity(suite),
                           'environment': session._environment_id()})
    assert bool(session._standing_not_run()) is held


def test_opt_in_restores_on_controlled_interruption():
    import quadratus.integration as integration
    import quadratus.project_run as project_run
    before = integration.GateSuite, project_run.GateSuite, integration.model_facing
    with pytest.raises(KeyboardInterrupt):
        with installed():
            assert (integration.GateSuite, project_run.GateSuite) == (WholeFileGateSuite, WholeFileGateSuite)
            raise KeyboardInterrupt('synthetic controlled pause')
    assert (integration.GateSuite, project_run.GateSuite, integration.model_facing) == before


def test_workers_are_told_and_granted_the_real_discovery_command(tmp_path, monkeypatch):
    import shlex
    from types import SimpleNamespace

    from quadratus import cli_providers, integration
    from quadratus.cli_providers import ClaudeCLIProvider
    from quadratus.config import Settings
    from quadratus.project import Project
    from quadratus.runtime import Fleet

    populate(tmp_path)
    monkeypatch.setattr('shutil.which', lambda name: f'/fake/bin/{name}')
    provider = ClaudeCLIProvider(model='opus')
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **k: provider)
    observed = {}

    def launch(argv, **kwargs):
        observed['argv'] = argv
        return SimpleNamespace(stdout='{"result": "CHANGED: []", "is_error": false}',
                               stderr='', returncode=0)

    monkeypatch.setattr(cli_providers, '_launch', launch)
    with installed():
        # Same call-time lookup and join as project_run's approved checks.
        plain = shlex.join(integration.model_facing(command().argv))
        assert plain == shlex.join((command().argv[0], '-m', 'pytest', '-q', 'tests'))
        raw = ('python3', '-m', 'unittest', 'discover', '-s', 'tests')
        assert integration.model_facing(raw) == list(raw)
        fleet = Fleet(Settings(backend='cli'), project=Project(tmp_path), allow_writes=True)
        fleet.check_commands = (plain,)
        assert fleet.lead_can_run('claude:opus', plain)
        fleet.invoke('claude:opus', 'Run the configured checks.', allow_writes=True)
    argv = observed['argv']
    system = argv[argv.index('--system-prompt') + 1]
    assert plain in system and 'Run these commands verbatim' in system
    assert f'Bash({plain})' in argv
    assert not any(MARKER in arg or '--quadratus-report' in arg for arg in argv)


def test_real_default_discovery_and_explicit_inventory_have_the_same_population(tmp_path):
    populate(tmp_path, 'test_first.py')
    populate(tmp_path, 'nested/second_test.py', 'def test_second():\n    assert 2 == 2\n')
    populate(tmp_path, '.hidden/test_not_collected.py', 'def test_no():\n    assert False\n')
    default = dataclasses.replace(command(), argv=(command().argv[0], '-m', 'pytest', '-q',
                                                   '--quadratus-report={report}'))
    ordinary = GateSuite([default], cwd=tmp_path).run()
    selected = WholeFileGateSuite([command()], cwd=tmp_path).run()
    assert ordinary.passed and selected.passed
    assert ordinary.receipts[0].tests == selected.receipts[0].tests == 2
    assert set(selected.receipts[0].cases['executed']) == {
        'tests/test_first.py::test_one', 'tests/nested/second_test.py::test_second'}


@pytest.mark.parametrize("cheap", [False, True])
def test_marker_execution_preserves_reporting_environment(tmp_path, cheap):
    """The scratch runner uses report_env; marker expansion must retain it."""
    import sys

    from quadratus.integration import GateCommand
    from tools.step2_test_selection import WholeFileGateSuite
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_report_env.py").write_text(
        'import os\ndef test_environment():\n    assert os.environ.get("PYTEST_ADDOPTS") == "-q"\n')
    command = GateCommand("public", argv=(sys.executable, "-m", "pytest", "-q",
                                           "@all-pytest-files:tests", "--quadratus-report={report}"),
                          timeout=20, minimum_tests=1, cheap=True)
    gate = WholeFileGateSuite([command], cwd=tmp_path, report_env={"PYTEST_ADDOPTS": "-q"})
    result = (gate.cheap() if cheap else gate).run()
    assert result.passed, result.output
    assert result.receipts[0].tests == 1


@pytest.mark.parametrize("cheap", [False, True])
def test_marker_execution_preserves_the_scratch_isolation_prefix(tmp_path, cheap):
    """The scratch runner's sandbox launcher is a GateSuite option too; a
    marker or cheap view that dropped it would run a reviewer's scratch test
    with the gate's authority (root review of 1c14ea9). An inert launcher
    that only sets a variable shows whether the prefix reached the run."""
    import sys

    from quadratus.integration import GateCommand
    from tools.step2_test_selection import WholeFileGateSuite
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_isolation.py").write_text(
        'import os\ndef test_environment():\n    assert os.environ.get("SCRATCH_ISOLATED") == "1"\n')
    command = GateCommand("public", argv=(sys.executable, "-m", "pytest", "-q",
                                           "@all-pytest-files:tests", "--quadratus-report={report}"),
                          timeout=20, minimum_tests=1, cheap=True)
    gate = WholeFileGateSuite([command], cwd=tmp_path, isolation=("/usr/bin/env", "SCRATCH_ISOLATED=1"))
    result = (gate.cheap() if cheap else gate).run()
    assert result.passed, result.output
    assert result.receipts[0].tests == 1
