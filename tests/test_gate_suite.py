"""Gate receipts describe real command outcomes without model calls."""

import sys
from dataclasses import replace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateCommand, GateSuite
from quadratus.session import Session, SessionConfig, TaskSpec


def command(id='check', code='print("3 passed")', **kwargs):
    return GateCommand(id, (sys.executable, '-c', code), **kwargs)


def test_ordered_commands_and_typed_receipts(tmp_path):
    suite = GateSuite([command('first'), command('second', 'raise SystemExit(1)'),
                       GateCommand('unconfigured'), GateCommand('skip', skip_reason='operator disabled')],
                      cwd=tmp_path)
    result = suite.run()
    assert not result.passed
    assert [r.id for r in result.receipts] == ['first', 'second', 'unconfigured', 'skip']
    assert [r.status for r in result.receipts] == ['passed', 'failed', 'blocked', 'skipped']
    assert all(r.reason for r in result.receipts)


@pytest.mark.parametrize('required,passed', [(True, False), (False, True)])
def test_required_skip_is_not_a_pass(tmp_path, required, passed):
    result = GateSuite([GateCommand('skip', required=required, skip_reason='no adapter')], cwd=tmp_path).run()
    assert result.passed is passed
    assert result.receipts[0].status == 'skipped'


@pytest.mark.parametrize('output', ['no tests ran', '# tests 0', 'Ran 0 tests', '0 passed',
                                   '3 skipped', 'Ran 3 tests\nOK (skipped=3)',
                                   '# tests 3\n# skipped 3\n# todo 0'])
def test_zero_or_all_skipped_tests_fail_even_with_exit_zero(tmp_path, output):
    result = GateSuite([command(code=f'print({output!r})')], cwd=tmp_path).run()
    assert not result.passed
    assert result.receipts[0].tests == 0


@pytest.mark.parametrize('output,status', [('2 passed', 'failed'), ('done', 'blocked'), ('3 passed', 'passed')])
def test_minimum_test_count_requires_evidence(tmp_path, output, status):
    result = GateSuite([command(code=f'print({output!r})', minimum_tests=3)], cwd=tmp_path).run()
    assert result.receipts[0].status == status


def test_timeout_and_missing_executable_are_receipts(tmp_path):
    result = GateSuite([command(code='import time; time.sleep(1)', timeout=.01),
                       GateCommand('missing', ('/missing/binary',))], cwd=tmp_path).run()
    assert [r.status for r in result.receipts] == ['error', 'blocked']
    assert not result.passed


def test_gate_mutation_invalidates_result_and_skips_later_commands(tmp_path):
    result = GateSuite([command('mutates', "from pathlib import Path; Path('new.py').write_text('x=1')"),
                       command('later')], cwd=tmp_path).run()
    assert not result.passed
    assert result.receipts[0].status == 'error'
    assert 'source changed' in result.receipts[0].reason
    assert result.receipts[1].status == 'skipped'


def test_cache_key_covers_source_config_and_runner(tmp_path):
    runner = tmp_path / 'runner.py'
    runner.write_text('print("3 passed")')
    c = GateCommand('tests', (sys.executable, str(runner)), cheap=True, cacheable=True)
    suite = GateSuite([c], cwd=tmp_path)
    assert not suite.cheap().run().receipts[0].cached
    assert suite.run().receipts[0].cached
    (tmp_path / 'source.py').write_text('x=1')
    assert not suite.run().receipts[0].cached
    assert suite.run().receipts[0].cached
    suite.commands = (replace(c, minimum_tests=4),)
    assert suite.run().receipts[0].status == 'failed'
    suite.commands = (c,)
    runner.write_text('print("4 passed")')
    receipt = suite.run().receipts[0]
    assert not receipt.cached and receipt.tests == 4


def test_external_runner_change_invalidates_cache(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    script = tmp_path / 'runner.py'
    script.write_text('print("3 passed")')
    suite = GateSuite([GateCommand('test', (sys.executable, str(script)), cacheable=True)], cwd=root)
    first = suite.run().receipts[0]
    assert suite.run().receipts[0].cached
    script.write_text('raise SystemExit(1)')
    last = suite.run().receipts[0]
    assert last.source_hash == first.source_hash
    assert last.runner_hash != first.runner_hash
    assert last.status == 'failed' and not last.cached


def test_caching_is_off_unless_requested(tmp_path):
    suite = GateSuite([command()], cwd=tmp_path)
    suite.run()
    assert not suite.run().receipts[0].cached


@pytest.mark.parametrize('cwd', ['../escape', '/tmp'])
def test_cwd_cannot_leave_the_project(tmp_path, cwd):
    with pytest.raises(ValueError):
        GateSuite([command(cwd=cwd)], cwd=tmp_path)


def session_for(tmp_path, gate, invoke, max_fixes=1):
    return Session('fixture', ArtifactStore(tmp_path / '.quadratus/artifacts'), invoke,
                   config=SessionConfig(project=tmp_path, integration_gate=gate, max_gate_fixes=max_fixes))


def test_cheap_gates_precede_review_and_share_final_repair_allowance(tmp_path, monkeypatch):
    phases = []
    marker = tmp_path / 'source.py'
    marker.write_text('before')
    suite = GateSuite([command('early', cheap=True), command('late')], cwd=tmp_path)
    original = suite.run

    # Keep real command execution, but make one failure per phase.
    early = suite.cheap()
    early.commands = (command('early', 'raise SystemExit(1)', cheap=True),)
    monkeypatch.setattr(suite, 'cheap', lambda: early)
    monkeypatch.setattr(suite, 'run', lambda: phases.append('final gate') or original())

    def invoke(model, prompt, **kw):
        if 'integration check failed' in prompt:
            phases.append('fix')
            early.commands = (command('early', cheap=True),)
            suite.commands = (command('late', 'raise SystemExit(1)'),)
            return 'fixed early failure'
        elif 'contributing an independent' in prompt:
            phases.append('review')
            assert 'fixed early failure' in prompt
        return 'NO FINDINGS'

    s = session_for(tmp_path, suite, invoke)
    s.run_task(TaskSpec('t1', 'Review fixture', complexity='standard'))
    assert phases == ['fix', 'review', 'final gate']
    assert len(s.checks) == 2 and s.checks[0]['passed'] and not s.checks[1]['passed']
    assert s.checks[0]['receipts'][0]['id'] == 'early'


def test_failed_cheap_gate_stops_before_collaborators(tmp_path):
    calls = []
    suite = GateSuite([command(code='raise SystemExit(1)', cheap=True)], cwd=tmp_path)
    s = session_for(tmp_path, suite, lambda model, prompt, **kw: calls.append(prompt) or 'NO FINDINGS', 0)
    s.run_task(TaskSpec('t1', 'Review fixture', complexity='standard'))
    assert s.open_findings
    assert len(calls) == 2, 'Only draft and closeout, no paid review of a failed check'


def test_real_failed_pytest_counts_executions_without_hiding_failure(tmp_path):
    (tmp_path / 'test_fixture.py').write_text(
        'import pytest\n'
        'def test_pass(): assert True\n'
        'def test_fail(): assert False\n'
        '@pytest.mark.skip(reason="not executed")\n'
        'def test_skip(): assert False\n')
    result = GateSuite([GateCommand(
        'pytest', (sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider'),
        minimum_tests=3)], cwd=tmp_path).run()
    receipt = result.receipts[0]
    assert not result.passed
    assert receipt.tests == 2
    assert receipt.returncode == 1
    assert receipt.status == 'failed'
    assert receipt.reason == 'nonzero exit'
    assert '1 failed, 1 passed, 1 skipped' in receipt.output


@pytest.mark.parametrize('output,count', [
    ('3 failed, 6 passed, 1 warning in 0.11s', 9),
    ('3 failed in 0.11s', 3),
    ('2 passed, 3 skipped, 4 deselected, 1 xfailed, 1 xpassed in 0.11s', 4),
    ('99 passed in 1.0s\n3 failed, 6 passed in 0.11s', 9),
])
def test_mixed_runner_summary_counts_executed_cases(output, count):
    from quadratus.integration import _test_count
    assert _test_count(output) == count
