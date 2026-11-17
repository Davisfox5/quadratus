"""The single-model baseline arm and the run comparison (whole-repo review
2026-11-10, design section 2: no measurement that the pipeline beats one
model alone). Real Fleet, real gate subprocesses; the vendor CLI is a script."""

import json
import shlex
import sys
from pathlib import Path

import pytest

from quadratus import baseline
from quadratus.baseline import run_baseline
from quadratus.config import Settings
from quadratus.project import Project
from quadratus.project_run import _project_lock
from quadratus.providers import ProviderError
from quadratus.run_budget import RunLimits
from tools import compare_runs, truth_check
from tools.jev_stage_b import pairs as P

FAKE_CLI = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ['--version']:
    # The provider reads the client's version once at construction. A real
    # binary answers that without reading a prompt or running a turn, so
    # this one does too and it is not traced as a model call.
    print('fake-vendor 99.0.0')
    sys.exit(0)
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
    stream.write(json.dumps({'cwd': os.getcwd(), 'args': args, 'prompt': prompt}) + '\n')
call = len(trace.read_text().splitlines())
mode = os.environ.get('FAKE_MODE', 'good')
envelope = {'is_error': False,
            'usage': {'input_tokens': int(os.environ.get('FAKE_TOKENS', '100')), 'output_tokens': 10}}
if mode == 'unmetered':
    del envelope['usage']
if mode == 'refuse':
    print(json.dumps(dict(envelope, stop_reason='refusal', stop_details={'category': 'cyber'})))
    sys.exit(0)
if mode == 'request':
    print(json.dumps(dict(envelope, result='FETCH: 2185e3cf5881')))
    sys.exit(0)
changed = ['add.py']
good = mode in ('good', 'unmetered', 'capped', 'forbidden', 'selection') or (mode == 'second' and call > 1)
Path('add.py').write_text('def add(a, b):\n    return a + b\n' if good
                          else f'def add(a, b):\n    return a - b - {call}\n')
if mode == 'forbidden':
    Path('secret').mkdir(exist_ok=True)
    Path('secret/key.txt').write_text('written\n')
    changed.append('secret/key.txt')
if mode == 'weaken':
    Path('test_add.py').write_text('def test_add():\n    assert True\n')
    changed.append('test_add.py')
if mode == 'selection':
    Path('setup.cfg').write_text('[tool:pytest]\naddopts = -q\n')
    changed.append('setup.cfg')
if mode == 'capped':
    print(json.dumps(dict(envelope, subtype='error_max_turns', result='still working')))
    sys.exit(0)
print(json.dumps(dict(envelope, result='Saved.\nNOT RUN: tests/browser/run.js - no browser here\n'
                                       'CHANGED: ' + json.dumps(changed))))
'''

GOAL = 'Make add(a, b) return the sum.\nKeep the signature.'


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'add.py').write_text('def add(a, b):\n    return 0\n')
    (root / 'test_add.py').write_text('from add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    monkeypatch.chdir(tmp_path)
    binary = tmp_path / 'fake-vendor'
    binary.write_text(f'#!{sys.executable}\n' + FAKE_CLI)
    binary.chmod(0o755)
    # Every vendor, so no test can reach a real CLI.
    for vendor in ('CLAUDE', 'OPENAI', 'GROK'):
        monkeypatch.setenv(f'QUADRATUS_CLI_BINARY_{vendor}', str(binary))
    trace = tmp_path / 'calls.jsonl'
    monkeypatch.setenv('FAKE_TRACE', str(trace))
    monkeypatch.delenv('FAKE_MODE', raising=False)
    monkeypatch.delenv('FAKE_TOKENS', raising=False)
    return Project(root), Settings(backend='cli', max_retries=1), trace


def check(report=True):
    argv = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_add.py']
    return shlex.join(argv + (['--quadratus-report={report}'] if report else []))


def calls(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []


def record(result):
    return json.loads((result.run_dir / 'result.json').read_text())


def run(env, **options):
    project, settings, _ = env
    options.setdefault('model', 'claude:opus')
    options.setdefault('check', check())
    return run_baseline(GOAL, project, settings, **options)


# -- the baseline run ---------------------------------------------------------

def test_a_passing_baseline_writes_the_record_and_is_not_judged(env):
    project, _, trace = env
    result = run(env, forbid=('secret/**',))
    assert result.completed is False and result.error == ''
    assert 'return a + b' in (project.root / 'add.py').read_text()
    data = record(result)
    assert data['mode'] == 'baseline' and data['completed'] is False
    assert data['completion'] == 'not judged' and data['requirements'] is None
    assert data['model'] == 'claude:opus' and data['models'] == ['claude:opus']
    info = data['baseline']
    assert info['calls'] == 1 and info['editing_calls'] == 1 and info['reported_tokens'] == 110
    assert info['unknown_usage_calls'] == 0 and info['fix_rounds'] == 1 and info['fix_rounds_used'] == 0
    assert info['gate'] == dict(configured=True, attempts=1, passed=True, observed_after_stop=False)
    assert info['stop_cause'] is None and isinstance(info['seconds'], float)
    # The same shape a project run writes, so the readers of one read the other.
    assert data['checks'][0]['passed'] and data['checks'][0]['cwd'] == str(project.root)
    attempt = data['workflow']['tasks'][0]['checks'][-1]
    assert attempt['passed'] and attempt['source'] == data['source_fingerprint']
    assert data['workflow']['calls_by_task']['baseline']['lead']['calls'] == 1
    assert data['selected_limits']['fix_rounds'] == 1 and data['selected_limits']['mode'] == 'baseline'
    assert data['dependency_identity']['status'] == 'unchanged'
    assert data['scope_reports'][0]['within_scope'] and data['scope_reports'][0]['max_lines'] is None
    assert data['unexecuted_acceptance'][0]['item'] == 'tests/browser/run.js'
    assert (result.run_dir / 'changes.diff').read_text() == result.diff and '+    return a + b' in result.diff
    for name in ('gate-plan.json', 'run-limits.json', 'policy-plan.json', 'usage.jsonl',
                 'invocations.jsonl', 'activity.jsonl', 'report.md'):
        assert (result.run_dir / name).is_file(), name
    row = json.loads((result.run_dir / 'invocations.jsonl').read_text())
    assert (row['task'], row['role'], row['canonical_model']) == ('baseline', 'lead', 'claude:opus')
    assert 'the goal was not judged' in result.report and 'Goal reported complete' not in result.report


def test_the_one_prompt_is_the_goal_the_checks_and_the_forbidden_paths(env):
    _, _, trace = env
    run(env, forbid=('secret/**',))
    (call,) = calls(trace)
    prompt = call['prompt']
    assert f'Goal (verbatim):\n{GOAL}' in prompt
    # The model-facing command: the harness's report token never reaches a model.
    shown = shlex.join([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_add.py'])
    assert f'- {shown}' in prompt and '{report}' not in prompt
    assert 'secret/**' in prompt and '.quadratus/**' in prompt
    assert baseline.ALONE_RULE in prompt
    for word in ('orchestrat', 'reviewer', 'COVERS', 'KIND:', 'SCOPE:', '## Role packet'):
        assert word not in prompt, word
    # The call ran in the project itself, with the editing transport's own text.
    assert call['cwd'] == str(env[0].root)
    assert any('APPROVED CHECK COMMANDS' in a for a in call['args'])


def test_a_failing_gate_buys_exactly_fix_rounds_more_calls_and_then_stops(env, monkeypatch):
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'broken')
    result = run(env, fix_rounds=2)
    made = calls(trace)
    assert len(made) == 3
    assert 'integration check failed' in made[1]['prompt'] and 'fix call 1 of 2' in made[1]['prompt']
    assert 'fix call 2 of 2' in made[2]['prompt'] and GOAL in made[2]['prompt']
    data = record(result)
    assert data['baseline']['fix_rounds_used'] == 2 and data['baseline']['gate']['attempts'] == 3
    assert data['checks'][-1]['passed'] is False and data['completed'] is False
    assert [r['role'] for r in data['baseline']['edit_calls']] == ['lead', 'gate-fix', 'gate-fix']
    assert 'Check FAILED' in result.report


def test_no_fix_round_means_one_call(env, monkeypatch):
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'broken')
    result = run(env, fix_rounds=0)
    assert len(calls(trace)) == 1 and record(result)['checks'][-1]['passed'] is False
    assert 'You get this one call' in calls(trace)[0]['prompt']


def test_the_fix_call_gets_the_gate_output_and_can_pass(env, monkeypatch):
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'second')
    result = run(env)
    made = calls(trace)
    assert len(made) == 2 and 'assert' in made[1]['prompt']
    data = record(result)
    assert data['checks'][-1]['passed'] and data['baseline']['fix_rounds_used'] == 1
    assert [c['passed'] for c in data['workflow']['tasks'][0]['checks']] == [False, True]
    assert data['baseline']['reported_tokens'] == 220 and data['baseline']['calls'] == 2


def test_a_failure_the_application_cannot_repair_buys_no_fix_call(env, monkeypatch):
    """The rule a lead's gate-fix is held to: no declared report, no fix call."""
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'broken')
    result = run(env, check=check(report=False), fix_rounds=3)
    assert len(calls(trace)) == 1
    data = record(result)
    assert result.error.startswith('CheckUnattributable') and data['baseline']['stop_cause'] == 'check not attributable'
    assert data['baseline']['fix_rounds_used'] == 0


def test_a_baseline_cannot_pass_its_gate_by_editing_the_tests(env, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'weaken')
    result = run(env, fix_rounds=0)
    data = record(result)
    assert data['checks'][-1]['passed'] is False
    (original,) = data['original_test_runs']
    assert original['changed'] == ['test_add.py'] and original['passed'] is False
    assert any(r['id'].startswith('original-tests:') for r in data['checks'][-1]['receipts'])


def test_a_check_is_not_a_pass_when_the_run_start_tests_cannot_be_established(env, monkeypatch):
    """The call changed how tests are selected, so the original suite cannot
    be run as it was. Unknown is not a pass, and no fix call is spent on it."""
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'selection')
    result = run(env, fix_rounds=2)
    data = record(result)
    assert data['checks'][-1]['passed'] is False and data['baseline']['gate']['passed'] is False
    assert 'not accepted as a pass' in data['checks'][-1]['output']
    (original,) = data['original_test_runs']
    assert original['passed'] is None and original['changed'] == ['setup.cfg']
    assert len(calls(trace)) == 1 and data['baseline']['stop_cause'] == 'check not attributable'
    assert truth_check.judge(result.run_dir)['verdict'] == truth_check.NOT_MET


def test_a_restored_run_that_skipped_a_command_is_not_a_pass(env, monkeypatch):
    """One required command names a test file the call adds, so it cannot run
    against the run-start files and the copy skips it, with every run-start
    file it also names. The session carries that as an open finding; a
    baseline has none, so its check is not accepted as a pass."""
    project, _, trace = env
    (project.root / 'test_old.py').write_text('def test_old():\n    assert True\n')
    binary = Path(trace.parent / 'fake-vendor')
    binary.write_text(binary.read_text().replace(
        "changed = ['add.py']",
        "changed = ['add.py']\n"
        "Path('test_new.py').write_text('def test_new():\\n    assert True\\n')\n"
        "Path('test_add.py').write_text('def test_add():\\n    assert True\\n')"))
    mixed = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_add.py', 'test_new.py']
    old = shlex.join([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_old.py'])
    result = run(env, check=old, extra_checks=[mixed], declared_checks=False, fix_rounds=2)
    data = record(result)
    # The gate's own commands passed (the weakened test_add.py ran beside the
    # new file); the check as a whole did not.
    assert [c['passed'] for c in data['workflow']['tasks'][0]['checks']] == [False]
    assert data['checks'][-1]['passed'] is False and 'not accepted as a pass' in data['checks'][-1]['output']
    (original,) = data['original_test_runs']
    assert original['passed'] is None and 'original-tests:extra-1 did not run' in original['problem']
    statuses = {r['id']: r['status'] for r in data['checks'][-1]['receipts']}
    assert statuses == {'check': 'passed', 'extra-1': 'passed', 'original-tests:check': 'passed',
                        'original-tests:extra-1': 'skipped', 'original-tests': 'blocked'}
    # Unknown is not the application's to repair: no fix call is spent on it.
    assert len(calls(trace)) == 1 and data['baseline']['stop_cause'] == 'check not attributable'
    assert 'the original tests were not established' in result.report
    assert truth_check.judge(result.run_dir)['verdict'] == truth_check.NOT_MET


def test_no_check_configured_is_recorded_as_no_check(env):
    result = run(env, check='', declared_checks=False)
    data = record(result)
    assert data['checks'] == [] and data['baseline']['gate']['configured'] is False
    assert data['baseline']['gate']['passed'] is None
    assert 'No integration check ran' in result.report


def test_a_request_reply_is_not_served_and_the_prompt_says_so(env, monkeypatch):
    """Prompt parity: the rule and the sentence that states it, together."""
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'request')
    result = run(env, fix_rounds=0)
    assert len(calls(trace)) == 1
    assert 'No FETCH, CONSULT or WORKER request is served' in calls(trace)[0]['prompt']
    data = record(result)
    assert data['baseline']['edit_calls'][0]['unserved_request'] == 'FETCH'
    assert data['checks'][-1]['passed'] is False and data['completed'] is False


# -- limits and stops ---------------------------------------------------------

def test_the_call_limit_stops_the_fix_call(env, monkeypatch):
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'broken')
    result = run(env, run_limits=RunLimits(max_calls=1, max_reported_tokens=1_000_000, wall_seconds=600))
    assert len(calls(trace)) == 1
    data = record(result)
    assert 'RunBudgetExceeded' in result.error and 'call_limit' in result.error
    assert data['baseline']['stop_cause'] == 'budget' and data['budget']['stop_reason'] == 'call_limit'
    assert data['selected_limits']['run_limits']['max_calls'] == 1
    assert data['in_flight']['changed'] == ['add.py']


def test_the_reserve_is_asked_before_the_fix_call(env, monkeypatch):
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'broken')
    monkeypatch.setenv('FAKE_TOKENS', '600')
    result = run(env, run_limits=RunLimits(max_calls=5, max_reported_tokens=1_000, wall_seconds=600,
                                           reserve_tokens_per_call=500))
    assert len(calls(trace)) == 1
    assert record(result)['budget']['stop_reason'] == 'reported_token_reserve'


def test_a_token_threshold_stop_still_shows_what_the_gate_says_of_the_work(env, monkeypatch):
    monkeypatch.setenv('FAKE_TOKENS', '500')
    result = run(env, run_limits=RunLimits(max_calls=5, max_reported_tokens=100, wall_seconds=600))
    data = record(result)
    assert 'reported_token_threshold' in result.error and data['baseline']['stop_cause'] == 'budget'
    assert data['checks'][-1]['passed'] and data['checks'][-1]['observed_after_stop'] is True
    assert data['baseline']['gate']['observed_after_stop'] is True and data['completed'] is False
    assert 'run once after the stop' in result.report


def test_the_per_call_ceiling_derives_the_turn_cap_the_call_carries(env):
    result = run(env, run_limits=RunLimits(max_calls=5, max_reported_tokens=5_000_000, wall_seconds=600,
                                           max_tokens_per_call=1_500_000))
    data = record(result)
    assert data['selected_limits']['lead_max_turns'] == 20
    (call,) = calls(env[2])
    assert '--max-turns' in call['args'] and call['args'][call['args'].index('--max-turns') + 1] == '20'


def test_the_round_cap_a_call_carries_is_stated_in_its_prompt(env, monkeypatch):
    """Prompt parity: the Fleet caps a baseline's calls as it caps a lead's, so
    each prompt says so. A cap the model is not told about measures the cap."""
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'second')
    run(env, run_limits=RunLimits(max_calls=5, max_reported_tokens=5_000_000, wall_seconds=600,
                                  max_tokens_per_call=1_500_000))
    first, fix = calls(trace)
    for call in (first, fix):
        assert call['args'][call['args'].index('--max-turns') + 1] == '20'
        assert 'This call has at most 20 tool rounds.' in call['prompt']
        assert 'no further call follows' in call['prompt']
    # No cap, no sentence: the prompt never states a bound the call does not carry.
    trace.unlink()
    monkeypatch.setenv('FAKE_MODE', 'good')
    run(env)
    (uncapped,) = calls(trace)
    assert '--max-turns' not in uncapped['args'] and 'tool rounds' not in uncapped['prompt']
    # A CLI with no turn flag is not capped, so its model is told nothing.
    assert baseline.round_budget('openai:gpt-5.6-sol', 20) == ''
    assert baseline.round_budget('claude:opus', None) == '' and baseline.round_budget('nope:x', 20) == ''
    assert 'at most 12 tool rounds' in baseline.round_budget('grok:default', 12)


def test_a_changed_dependency_folder_stops_the_baseline_and_the_prompt_names_them(env, monkeypatch):
    """Prompt parity for the dependency watch: the stop and its sentence."""
    from quadratus.deptree import DEPENDENCY_DIRS
    project, _, trace = env
    binary = Path(trace.parent / 'fake-vendor')
    binary.write_text(binary.read_text().replace(
        "changed = ['add.py']", "changed = ['add.py']\nPath('node_modules/shim').mkdir(parents=True)\n"
                                "Path('node_modules/shim/index.js').write_text('module.exports = 1\\n')"))
    result = run(env, fix_rounds=2)
    data = record(result)
    (call,) = calls(trace)
    assert all(name in call['prompt'] for name in DEPENDENCY_DIRS) and baseline.DEPENDENCY_RULE in call['prompt']
    assert result.error.startswith('DependencyTreeChanged') and data['baseline']['stop_cause'] == 'dependency tree'
    assert data['dependency_identity']['status'] == 'changed'
    # No check is accepted over a changed tree, so none is run or recorded.
    assert data['checks'] == [] and data['baseline']['gate']['passed'] is None
    assert truth_check.judge(result.run_dir)['verdict'] == truth_check.NOT_MET


@pytest.mark.parametrize('mode,cause,kind', [
    ('refuse', 'refusal', 'ProviderRefusal'),
    ('capped', 'turn limit', 'TurnLimitReached'),
])
def test_a_refusal_and_a_turn_cap_end_the_baseline_with_the_cause_named(env, monkeypatch, mode, cause, kind):
    _, _, trace = env
    monkeypatch.setenv('FAKE_MODE', mode)
    result = run(env, fix_rounds=2)
    data = record(result)
    assert len(calls(trace)) == 1 and result.error.startswith(kind)
    assert data['baseline']['stop_cause'] == cause and data['completed'] is False
    assert data['turn_limited_tasks'] == (['baseline'] if mode == 'capped' else [])
    # A capped call's work is on disk, so the gate looks at it once; a refusal left nothing to look at.
    assert bool(data['checks']) is (mode == 'capped')
    # The capped call's narration is kept as evidence and is never a reply on the record.
    assert bool(data['baseline']['edit_calls'][0]['reply_artifact']) is (mode == 'capped')
    assert data['baseline']['edit_calls'][0]['outcome'] == kind


def test_a_spent_window_ends_the_baseline_after_one_call_with_the_cause_named(env):
    """No re-ask, no other model: the one seat is the measurement."""
    _, _, trace = env
    binary = Path(trace.parent / 'fake-vendor')
    binary.write_text(binary.read_text().replace(
        "changed = ['add.py']", "sys.stderr.write(\"You've reached your usage limit.\\n\"); sys.exit(1)"))
    result = run(env, fix_rounds=2)
    data = record(result)
    assert len(calls(trace)) == 1 and result.error.startswith('WindowExhausted')
    assert data['baseline']['stop_cause'] == 'window exhausted' and data['checks'] == []
    assert data['baseline']['reported_tokens'] is None and data['completed'] is False


def test_every_stop_is_named_from_its_type_never_from_its_wording():
    from quadratus.deptree import DependencyTreeChanged
    from quadratus.integration import CheckUnattributable
    from quadratus.providers import ProviderRefusal, TurnLimitReached
    from quadratus.run_budget import RunBudgetExceeded
    from quadratus.runtime import WindowExhausted
    named = {
        'window exhausted': WindowExhausted('claude:opus: subscription window exhausted'),
        'budget': RunBudgetExceeded('Run stopped: wall_deadline'),
        'refusal': ProviderRefusal('declined'),
        'turn limit': TurnLimitReached('stopped at its turn limit'),
        'path bounds': baseline.PathBoundsExceeded('outside'),
        'check not attributable': CheckUnattributable('no report'),
        'dependency tree': DependencyTreeChanged('during lead'),
        'interrupted': KeyboardInterrupt(),
        # Wording that names another cause decides nothing.
        'ProviderError': ProviderError('the window is exhausted and the budget refused a turn limit'),
    }
    for cause, exc in named.items():
        assert baseline._stop_cause(exc) == cause


def test_usage_a_call_did_not_report_is_unknown_never_zero(env, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'unmetered')
    info = record(run(env))['baseline']
    assert info['reported_tokens'] is None and info['unknown_usage_calls'] == 1 and info['calls'] == 1


def test_a_forbidden_path_stops_the_baseline_with_the_work_kept(env, monkeypatch):
    project, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'forbidden')
    result = run(env, forbid=('secret/**',))
    data = record(result)
    assert result.error.startswith('PathBoundsExceeded') and data['baseline']['stop_cause'] == 'path bounds'
    assert data['scope_reports'][-1]['out_of_scope'] == ['secret/key.txt']
    assert (project.root / 'secret' / 'key.txt').exists() and len(calls(trace)) == 1


def test_the_operators_declared_paths_fence_the_baseline_too(env, monkeypatch):
    project, _, trace = env
    monkeypatch.setenv('FAKE_MODE', 'forbidden')
    result = run(env, declared_paths=('add.py', 'test_add.py'))
    data = record(result)
    assert 'You may change only these paths: add.py, test_add.py.' in calls(trace)[0]['prompt']
    assert data['baseline']['stop_cause'] == 'path bounds'
    assert data['scope_reports'][-1]['out_of_scope'] == ['secret/key.txt']
    # A fence that names a write-denied path is refused before any call, as a project run refuses it.
    refused = run(env, declared_paths=('.env',))
    assert 'Write denied' in refused.error and len(calls(trace)) == 1


# -- refused before any call --------------------------------------------------

@pytest.mark.parametrize('model,words', [
    ('claude:haiku', 'restricted worker seat'),
    ('grok:worker', 'restricted worker seat'),
    ('claude:nope', 'not in the active roster'),
    ('', 'not in the active roster'),
])
def test_a_restricted_or_unknown_model_is_refused_before_any_call(env, model, words):
    project, _, trace = env
    with pytest.raises(ValueError, match=words):
        run(env, model=model)
    assert not trace.exists() and not (project.root / '.quadratus').exists()


@pytest.mark.parametrize('rounds', [-1, 1.5, True, '1'])
def test_fix_rounds_must_be_a_whole_number(env, rounds):
    with pytest.raises(ValueError, match='fix_rounds'):
        run(env, fix_rounds=rounds)
    assert not env[2].exists()


def test_the_trust_preflights_apply(env):
    project, _, trace = env
    (project.root / '.mcp.json').write_text('{"mcpServers": {}}\n')
    with pytest.raises(ValueError, match='vendor agent configuration'):
        run(env)
    assert not trace.exists()
    result = run(env, allow_project_agent_config=True)
    assert len(calls(trace)) == 1 and result.error == ''


def test_an_unapproved_project_policy_that_grants_is_refused(env):
    from quadratus.policy import PolicyUntrusted, load_policy
    project, _, trace = env
    document = dict(load_policy(project.root).document)
    document['gates'] = [*document['gates'], {'id': 'extra', 'runner': 'command', 'required': True,
                                                'argv': ['true'], 'cwd': '.'}]
    (project.root / '.quadratus').mkdir()
    (project.root / '.quadratus' / 'policy.json').write_text(json.dumps(document))
    with pytest.raises(PolicyUntrusted):
        run(env)
    assert not trace.exists()


def test_a_failing_readiness_probe_stops_before_any_call(env):
    project, _, trace = env
    result = run(env, readiness=[dict(id='ready', argv=['false'])])
    data = record(result)
    assert not trace.exists() and result.error.startswith('CapabilityProbeFailed')
    assert data['workflow']['run']['readiness'][0]['passed'] is False and data['baseline']['calls'] == 0
    with pytest.raises(ValueError, match='readiness'):
        run(env, readiness=[dict(id='ready', argv=[])])
    passing = run(env, readiness=[dict(id='ready', argv=['true'])])
    assert record(passing)['workflow']['run']['readiness'][0]['passed'] is True and len(calls(trace)) == 1


def test_no_check_is_run_over_a_tree_the_project_watch_flagged(env, monkeypatch):
    """A stopped call that left vendor agent configuration behind: the work is
    kept and the gate is not run over it."""
    project, _, trace = env
    binary = Path(trace.parent / 'fake-vendor')
    binary.write_text(binary.read_text().replace(
        "changed = ['add.py']", "changed = ['add.py']\nPath('.mcp.json').write_text('{}')"))
    monkeypatch.setenv('FAKE_MODE', 'capped')
    result = run(env)
    data = record(result)
    assert (project.root / '.mcp.json').exists() and data['watch_findings']
    assert data['checks'] == [] and data['completed'] is False and result.error


def test_the_baseline_takes_the_project_lock(env):
    project, _, trace = env
    with _project_lock(project):
        with pytest.raises(ProviderError, match='Another Quadratus run'):
            run(env)
    assert not trace.exists()


def test_no_billed_fallback_when_the_vendor_is_set_to_the_api(env, monkeypatch):
    project, _, trace = env
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'not-a-real-key')
    result = run_baseline(GOAL, project, Settings(backend='api', max_retries=1), model='claude:opus', check=check())
    assert not trace.exists() and record(result)['baseline']['calls'] == 0
    # Refused by the Fleet before any transport, not failed by a billed attempt.
    assert result.error.startswith('ProviderError') and 'require CLI transport' in result.error


# -- judged from outside: the truth check -------------------------------------

def test_truth_check_says_unverified_for_a_clean_baseline_and_never_verified(env):
    project, _, _ = env
    result = run(env)
    for judged in (truth_check.judge(result.run_dir), truth_check.judge(result.run_dir, project.root)):
        assert judged['verdict'] == truth_check.UNVERIFIED and judged['not_met'] == []
        assert any('baseline run' in reason for reason in judged['unverified'])
    assert truth_check.main([str(result.run_dir)]) == truth_check.EXIT[truth_check.UNVERIFIED]


def test_truth_check_says_not_met_for_a_baseline_that_failed_or_stopped(env, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'broken')
    failed = truth_check.judge(run(env, fix_rounds=0).run_dir)
    assert failed['verdict'] == truth_check.NOT_MET and any('last check failed' in r for r in failed['not_met'])
    monkeypatch.setenv('FAKE_MODE', 'refuse')
    stopped = truth_check.judge(run(env).run_dir)
    assert stopped['verdict'] == truth_check.NOT_MET and any('ProviderRefusal' in r for r in stopped['not_met'])


def test_a_record_marked_baseline_is_never_verified_whatever_else_it_claims(tmp_path):
    """Canary: the record that reads verified for a project run, relabelled."""
    check = dict(passed=True, source='final-source', receipts=[
        dict(id='tests', required=True, status='passed', tests=1)])
    claims = dict(completed=True, checks=[check], source_fingerprint='final-source',
                  requirements=dict(listed={'R1': 'Change it'}, status={'R1': 'met'},
                                    audits=[dict(verdicts={'R1': dict(met=True)})]),
                  workflow=dict(tasks=[dict(checks=[check])]))
    folder = tmp_path / 'run'
    folder.mkdir()
    (folder / 'changes.diff').write_text('+++ b/app.py\n')
    (folder / 'result.json').write_text(json.dumps(claims))
    assert truth_check.judge(folder)['verdict'] == truth_check.VERIFIED
    (folder / 'result.json').write_text(json.dumps(dict(claims, mode='baseline')))
    assert truth_check.judge(folder)['verdict'] == truth_check.UNVERIFIED
    # And a project run that did not complete is still not met: the mode is the only exemption.
    (folder / 'result.json').write_text(json.dumps(dict(claims, completed=False)))
    assert truth_check.judge(folder)['verdict'] == truth_check.NOT_MET


@pytest.mark.parametrize('damage', ['[]', '{"mode": "baseline"}',
                                    '{"mode": "baseline", "baseline": 7, "requirements": []}'])
def test_truth_check_and_compare_runs_do_not_crash_on_a_damaged_baseline_record(tmp_path, damage):
    folder = tmp_path / 'run'
    folder.mkdir()
    (folder / 'result.json').write_text(damage)
    assert truth_check.judge(folder)['verdict'] == truth_check.UNVERIFIED
    assert compare_runs.summarize(folder)['truth_check'] == truth_check.UNVERIFIED
    assert compare_runs.render(compare_runs.compare([folder, folder]))


def test_compare_runs_reads_past_lists_that_are_not_lists(tmp_path):
    folder = tmp_path / 'run'
    folder.mkdir()
    (folder / 'result.json').write_text(json.dumps(dict(
        completed=False, error='', checks=[dict(passed=True, receipts=7)],
        requirements=dict(listed={'R1': 'a'}, audits=7))))
    row = compare_runs.summarize(folder)
    assert (row['gate'], row['gate_executed'], row['requirements_met']) == ('passed', 'unknown', 'unknown')
    assert row['audit'] == 'no audit on record' and compare_runs.render(compare_runs.compare([folder, folder]))


def test_compare_runs_has_no_verdict_for_a_record_the_truth_check_cannot_read(tmp_path, monkeypatch):
    folder = tmp_path / 'run'
    folder.mkdir()
    (folder / 'result.json').write_text('{"mode": "baseline", "checks": "x", "budget": 3, "baseline": []}')
    # The truth check now answers for this record itself (unverified, with why).
    row = compare_runs.summarize(folder)
    assert row['truth_check'] == truth_check.UNVERIFIED and row['gate'] == 'unknown'
    assert any('not a list' in reason for reason in row['truth_check_reasons'])

    # The comparison still has no verdict of its own when that tool fails.
    def broken(run_dir, project=None):
        raise RuntimeError('truth check failed')

    monkeypatch.setattr(compare_runs._truth_check(), 'judge', broken)
    row = compare_runs.summarize(folder)
    assert row['truth_check'] == 'unknown' and 'could not read' in row['truth_check_reasons'][0]
    assert row['gate'] == 'unknown' and 'truth check: unknown' in compare_runs.render(
        compare_runs.compare([folder, folder]))


# -- the monitor reads the folder ---------------------------------------------

def test_the_monitor_renders_a_baseline_run(env, monkeypatch):
    from quadratus.monitor import read_status, render_markdown, render_text, run_history
    from quadratus.monitor_story import render_story
    project, _, _ = env
    monkeypatch.setenv('FAKE_MODE', 'second')
    run(env)
    status = read_status(project.root)
    assert status['finished'] is True and status['live'] is False
    assert status['terminal_status'] == 'baseline, not judged' and 'not judged' in status['stop_reason']
    assert (status['task'], status['role'], status['seat'], status['stage']) == (
        'baseline', 'gate-fix', 'claude:opus', 'gate')
    assert status['calls'] == 2 and status['tokens_reported'] == 220
    assert 'baseline' in render_text(status, run_history(project.root)) and render_markdown(status)
    story = render_story(status)
    assert 'Baseline run: one model alone, goal not judged' in story and 'Unreadable record' not in story
    assert 'goal recorded as met' not in story and '>Passed<' in story
    assert 'Ended, not judged' in story and '<strong>Result:</strong> One model worked alone.' in story
    assert '>Finished<' not in story and 'Why it stopped' not in story
    assert run_history(project.root)[0]['status'] == 'baseline, not judged'


def test_the_monitor_never_calls_a_baseline_complete(tmp_path):
    from quadratus.monitor import read_status
    folder = tmp_path / '.quadratus' / 'runs' / '20261111T000000Z-abcdef12'
    folder.mkdir(parents=True)
    (folder / 'result.json').write_text(json.dumps(dict(mode='baseline', completed=True, error='')))
    assert read_status(tmp_path)['terminal_status'] == 'baseline, not judged'
    (folder / 'result.json').write_text(json.dumps(dict(mode='baseline', completed=False, error='Boom: no')))
    assert read_status(tmp_path)['terminal_status'] == 'error'


# -- tools/compare_runs.py ----------------------------------------------------

def _project_run_folder(tmp_path, name='engine'):
    """A project run's record as the engine writes it, by hand: three calls,
    one with no usage, a parsed gate report and a final audit."""
    folder = tmp_path / name
    folder.mkdir()
    report = dict(state='parsed', counts=dict(passed=7, failed=1, skipped=2, errors=0))
    check = dict(passed=False, source='s', receipts=[
        dict(id='check', required=True, status='failed', tests=8, report=report),
        dict(id='extra-1', required=True, status='passed', tests=3, report=dict(
            state='parsed', counts=dict(passed=3, failed=0, skipped=0, errors=0)))])
    (folder / 'result.json').write_text(json.dumps(dict(
        completed=False, error='', checks=[check], source_fingerprint='s',
        budget=dict(elapsed_seconds=321.44, stop_reason='reported_token_threshold', reserved_attempts=3),
        requirements=dict(listed={'R1': 'a', 'R2': 'b', 'R3': 'c'}, status={},
                          audits=[dict(verdicts={'R1': dict(met=True), 'R2': dict(met=False),
                                                 'R3': dict(met=True)})]),
        workflow=dict(tasks=[dict(checks=[check])]))))
    rows = [dict(task='run', role='orchestrator', canonical_model='claude:fable', invoked=True,
                 input_tokens=1000, output_tokens=200, seconds=10.0),
            dict(task='t1', role='lead', canonical_model='grok:default', invoked=True,
                 input_tokens=5000, output_tokens=800, seconds=30.5),
            dict(task='t1', role='collaborator', canonical_model='claude:opus', invoked=True,
                 input_tokens=None, output_tokens=None, seconds=20.0),
            dict(task='t1', role='collaborator', canonical_model='openai:gpt-5.6-sol', invoked=False)]
    (folder / 'invocations.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\nnot json\n')
    return folder


def test_compare_runs_imports_nothing_from_the_engine():
    source = Path(compare_runs.__file__).read_text()
    assert 'import quadratus' not in source and 'from quadratus' not in source
    # The truth-check verdict is that tool's own function, not a second copy of its rules.
    assert compare_runs._truth_check().judge.__code__.co_filename == truth_check.judge.__code__.co_filename


def test_compare_runs_reads_a_baseline_and_a_project_run(env, tmp_path):
    result = run(env)
    engine = _project_run_folder(tmp_path)
    grade_a, grade_b = tmp_path / 'a.json', tmp_path / 'b.json'
    grade_a.write_text(json.dumps(dict(passed=2, total=4)))
    grade_b.write_text(json.dumps(dict(grade=dict(passed=3, total=4, results=[]))))
    out = compare_runs.compare([result.run_dir, engine], [grade_a, grade_b])
    solo, team = out['runs']
    assert (solo['mode'], solo['models'], solo['calls'], solo['reported_tokens']) == (
        'baseline', 'claude:opus', 1, 110)
    assert solo['claim'] == 'not judged (baseline)' and solo['stop_reason'] == 'none recorded'
    assert (solo['gate'], solo['gate_executed'], solo['gate_failed'], solo['gate_skipped']) == ('passed', 1, 0, 0)
    assert solo['truth_check'] == 'unverified' and solo['audit'].startswith('not applicable')
    assert solo['requirements_met'] == 'unknown' and isinstance(solo['wall_seconds'], float)
    # 110 reported tokens over 2 grader-passed requirements.
    assert (solo['grader_passed'], solo['grader_total'], solo['tokens_per_passed_requirement']) == (2, 4, 55)
    assert (team['mode'], team['calls'], team['reported_tokens'], team['unknown_usage_calls']) == (
        'project run', 3, 7000, 1)
    assert team['models'] == 'claude:fable, claude:opus, grok:default'
    assert (team['wall_seconds'], team['model_seconds']) == (321.4, 60.5)
    assert team['stop_reason'] == 'budget: reported_token_threshold' and team['claim'] == 'incomplete'
    assert (team['gate'], team['gate_executed'], team['gate_failed'], team['gate_skipped']) == ('failed', 11, 1, 2)
    assert (team['requirements_met'], team['requirements_listed'], team['truth_check']) == (2, 3, 'not-met')
    # 7,000 reported tokens over 3 passed requirements, and it says one call is not in that figure.
    assert team['tokens_per_passed_requirement'] == 2333
    assert 'lower bound: 1 call(s) reported no usage' in team['tokens_per_passed_requirement_note']
    fields = out['differences'][0]['fields']
    assert fields['reported_tokens'] == dict(first=110, other=7000, difference=6890, ratio=63.64)
    assert fields['grader_passed'] == dict(first=2, other=3, difference=1, ratio=1.5)
    assert fields['tokens_per_passed_requirement']['difference'] == 2278
    assert fields['mode'] == dict(first='baseline', other='project run', same=False)
    text = compare_runs.render(out)
    assert 'Run 2 against run 1:' in text and 'reported tokens: +6,890, 63.64x (7,000 against 110)' in text
    assert 'graders: 3 of 4 passed' in text and 'final gate: failed (executed 11, failed 1, skipped 2)' in text
    assert 'reported tokens: 7,000 (not counting 1 call(s) that reported no usage)' in text


def test_compare_runs_prints_unknown_for_missing_data_never_zero(tmp_path, capsys):
    empty = tmp_path / 'empty'
    empty.mkdir()
    bare = tmp_path / 'bare'
    bare.mkdir()
    (bare / 'result.json').write_text(json.dumps(dict(completed=False, error='', checks=[dict(passed=True)])))
    out = compare_runs.compare([empty, bare])
    nothing, little = out['runs']
    for key in ('mode', 'models', 'calls', 'reported_tokens', 'wall_seconds', 'model_seconds', 'gate',
                'gate_executed', 'gate_failed', 'gate_skipped', 'requirements_met', 'grader_passed',
                'tokens_per_passed_requirement', 'stop_reason', 'claim'):
        assert nothing[key] == 'unknown', key
    assert nothing['truth_check'] == 'unverified' and nothing['problem'] == 'no readable result.json'
    # A passing check with no count is a pass with unknown counts, not zero cases.
    assert (little['gate'], little['gate_executed'], little['gate_failed'], little['gate_skipped']) == (
        'passed', 'unknown', 'unknown', 'unknown')
    assert (little['calls'], little['reported_tokens'], little['wall_seconds']) == ('unknown',) * 3
    # A check of a source the run then changed is said to be that, not the final word.
    (bare / 'result.json').write_text(json.dumps(dict(
        completed=False, error='', source_fingerprint='final', checks=[dict(passed=True, source='earlier')])))
    assert compare_runs.summarize(bare)['gate'] == 'passed (on an earlier source, not the final one)'
    assert all(f['difference'] == 'unknown' for k, f in out['differences'][0]['fields'].items()
               if k in dict(compare_runs.NUMBERS))
    assert compare_runs.main([str(empty), str(bare)]) == 0
    text = capsys.readouterr().out
    assert 'calls: unknown' in text and 'reported tokens: unknown' in text and ': 0' not in text
    assert 'tokens per grader-passed requirement: unknown (no grade given)' in text


def test_compare_runs_arithmetic_for_a_supplied_grade(tmp_path):
    engine = _project_run_folder(tmp_path)
    grade = tmp_path / 'grade.json'
    for body, per, note in [
        (dict(passed=4, total=4), 1750, 'lower bound'),
        (dict(passed=0, total=4), 'unknown', 'no requirement passed the graders'),
        (dict(passed=5, total=4), 'unknown', 'no usable passed and total'),
        (dict(passed=True, total=4), 'unknown', 'no usable passed and total'),
        (dict(passed='3', total=4), 'unknown', 'no usable passed and total'),
    ]:
        grade.write_text(json.dumps(body))
        row = compare_runs.summarize(engine, grade)
        assert row['tokens_per_passed_requirement'] == per, body
        assert note in row['tokens_per_passed_requirement_note'], body
    grade.write_text('not json')
    assert compare_runs.summarize(engine, grade)['grader_passed'] == 'unknown'
    assert compare_runs.summarize(engine, '-')['grade_problem'] == 'no grade given'
    # Unknown usage on every call: no token figure, so no per-requirement figure.
    (engine / 'invocations.jsonl').write_text(json.dumps(dict(invoked=True, canonical_model='claude:opus')) + '\n')
    grade.write_text(json.dumps(dict(passed=2, total=4)))
    row = compare_runs.summarize(engine, grade)
    assert (row['reported_tokens'], row['tokens_per_passed_requirement']) == ('unknown', 'unknown')
    assert row['tokens_per_passed_requirement_note'] == 'reported tokens are unknown'


def test_compare_runs_json_and_the_command_line(env, tmp_path, capsys):
    import subprocess
    result = run(env)
    engine = _project_run_folder(tmp_path)
    assert compare_runs.main([str(result.run_dir), str(engine), '--json']) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(json.dumps(compare_runs.compare([result.run_dir, engine])))
    with pytest.raises(SystemExit):
        compare_runs.main([str(engine)])
    with pytest.raises(SystemExit):
        compare_runs.main([str(engine), str(engine), '--grade', '-', '--grade', '-', '--grade', '-'])
    # As documented: isolated mode, from any directory, with no engine on the path.
    script = Path(compare_runs.__file__).resolve()
    done = subprocess.run([sys.executable, '-I', str(script), str(result.run_dir), str(engine)],
                          capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert done.returncode == 0 and 'Run 2 against run 1:' in done.stdout, done.stderr


def test_compare_runs_writes_nothing(env, tmp_path):
    result = run(env)
    engine = _project_run_folder(tmp_path)

    def listing():
        return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                      for folder in (result.run_dir, engine) for p in folder.rglob('*'))
    before = listing()
    compare_runs.render(compare_runs.compare([result.run_dir, engine]))
    assert listing() == before


# -- Stage B: the solo arm ----------------------------------------------------

#: A clean engine checkout at a known SHA, as tools/jev_stage_b/test_pairs.py uses.
ENGINE = dict(root='/engine', sha='e' * 40, dirty=False)


def _series(tmp_path, graders=False):
    """A prepared two-task series on a throwaway git project."""
    import subprocess
    repo = tmp_path / 'gametape'
    repo.mkdir()
    (repo / 'add.py').write_text('def add(a, b):\n    return 0\n')
    (repo / 'test_add.py').write_text('from add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@example.invalid',
                    'commit', '-qm', 'baseline'], check=True)
    packet = dict(project=dict(repo=str(repo), base_sha=P.git(repo, 'rev-parse', 'HEAD')),
                  goal='REQUIREMENTS:\nR1 the feature\n', check=check(),
                  limits=dict(max_calls=20, max_reported_tokens=500_000, wall_seconds=600),
                  tasks=[dict(id='feat-0', goal='REQUIREMENTS:\nR1 add returns the sum\n',
                              declared_paths=['add.py', 'test_add.py']),
                         dict(id='feat-1', text='SCOPE: {}\nBuild feature 1.\nCOVERS: R1')])
    if graders:
        folder = tmp_path / 'graders'
        folder.mkdir()
        (folder / 'grade.py').write_text(
            'import os, sys\nfrom pathlib import Path\n'
            "sys.exit(0 if 'a + b' in (Path(os.environ['STAGE_B_PROJECT']) / 'add.py').read_text() else 1)\n")
        (folder / 'never.py').write_text('import sys\nsys.exit(1)\n')
        commands = [[sys.executable, 'grade.py'], [sys.executable, 'never.py']]
        packet['graders'] = dict(dir=str(folder), tasks={'feat-0': commands, 'feat-1': commands})
    out = tmp_path / 'runs'
    P.prepare(packet, out)
    return packet, out


def _fake_solo(launched):
    def launch(cell, packet):
        launched.append(cell['name'])
        run_dir = Path(cell['project']) / '.quadratus' / 'runs' / 'r'
        run_dir.mkdir(parents=True)
        (run_dir / 'result.json').write_text(json.dumps(dict(mode='baseline', completed=False, error='')))
        return dict(run_dir=str(run_dir), completed=False, result_error=None)
    return launch


def test_the_solo_arm_adds_its_cells_and_leaves_the_pair_as_it_was(tmp_path):
    packet, out = _series(tmp_path)
    manifest = (out / 'manifest.json').read_bytes()
    launched = []
    record = P.solo(out, model='claude:opus', launcher=_fake_solo(launched), engine=ENGINE)
    assert launched == ['feat-0/solo-claude-opus-r0', 'feat-1/solo-claude-opus-r0']
    # The pair: same arms, same cells, same manifest bytes, same digest.
    assert P.ARMS == ('jev', 'rule') and (out / 'manifest.json').read_bytes() == manifest
    assert [c['arm'] for c in P.cells_for(packet)] == ['jev', 'rule', 'rule', 'jev']
    assert record['packet_digest'] == json.loads(manifest)['packet_digest']
    first, second = record['cells']
    assert (first['arm'], first['model'], first['state'], first['engine_sha']) == (
        'solo', 'claude:opus', 'ran', ENGINE['sha'])
    assert first['goal'] == packet['tasks'][0]['goal'] and first['declared_paths'] == ['add.py', 'test_add.py']
    # A listed task: the goal the pair gets, then the one frozen task text.
    assert second['goal'] == packet['goal'] + '\n\nThe task (verbatim):\n' + packet['tasks'][1]['text']
    assert (Path(first['project']) / 'add.py').read_text() == 'def add(a, b):\n    return 0\n'
    assert json.loads((out / 'solo.json').read_text()) == record and not (out / P.LOCK_NAME).exists()
    # A cell is never rerun, and another model is another cell.
    assert P.solo(out, model='claude:opus', launcher=_fake_solo(launched), engine=ENGINE) == record
    assert len(launched) == 2
    P.solo(out, model='openai:gpt-5.6-sol', only='feat-1', launcher=_fake_solo(launched), engine=ENGINE)
    assert launched[-1] == 'feat-1/solo-openai-gpt-5.6-sol-r0'


def test_the_solo_arm_refuses_a_restricted_model_a_held_lock_and_a_changed_engine(tmp_path):
    _, out = _series(tmp_path)
    launched = []
    with pytest.raises(ValueError, match='restricted worker seat'):
        P.solo(out, model='claude:haiku', launcher=_fake_solo(launched), engine=ENGINE)
    with pytest.raises(RuntimeError, match='uncommitted changes'):
        P.solo(out, model='claude:opus', launcher=_fake_solo(launched), engine=dict(ENGINE, dirty=True))
    (out / P.LOCK_NAME).write_text('pid=1\n')
    with pytest.raises(RuntimeError, match='another run holds'):
        P.solo(out, model='claude:opus', launcher=_fake_solo(launched), engine=ENGINE)
    assert launched == [] and not (out / 'solo.json').exists()
    assert not any(p.name.startswith('solo-') for p in out.rglob('*'))


def test_a_solo_cell_that_fails_to_launch_stops_the_arm_until_overridden(tmp_path):
    _, out = _series(tmp_path)
    launched = []

    def broken(cell, packet):
        raise OSError('no such CLI')
    record = P.solo(out, model='claude:opus', launcher=broken, engine=ENGINE)
    assert [c['state'] for c in record['cells']] == ['failed']
    assert record['stopped']['reason'].startswith(P.STOP_CELL_ERROR + ':')
    with pytest.raises(RuntimeError, match='the solo arm stopped'):
        P.solo(out, model='claude:opus', launcher=_fake_solo(launched), engine=ENGINE)
    record = P.solo(out, model='claude:opus', launcher=_fake_solo(launched), engine=ENGINE,
                    override_stop='the CLI is installed now')
    assert launched == ['feat-1/solo-claude-opus-r0'] and record['overrides'][0]['reason']
    assert [c['state'] for c in record['cells']] == ['failed', 'ran']


def test_a_grader_changed_after_prepare_is_not_run_for_a_solo_cell(tmp_path):
    packet, out = _series(tmp_path, graders=True)
    launched = []
    grader = Path(packet['graders']['dir']) / 'grade.py'

    def launch(cell, packet):
        outcome = _fake_solo(launched)(cell, packet)
        grader.write_text('import sys\nsys.exit(0)\n')
        return outcome
    record = P.solo(out, model='claude:opus', only='feat-0', launcher=launch, engine=ENGINE)
    (cell,) = record['cells']
    assert cell['grade'] is None and 'frozen inputs changed' in cell['grade_withheld']
    assert record['stopped']['reason'].startswith(P.STOP_INTEGRITY + ':') and P.write_grades(out) == {}


@pytest.mark.parametrize('saved,reason', [
    # The one model's window is spent: every later cell would end the same way.
    (dict(error='WindowExhausted: claude:opus: subscription window exhausted',
          baseline=dict(stop_cause='window exhausted', calls=1),
          budget=dict(reserved_attempts=1, unknown_usage_attempts=0)), 'window-exhausted'),
    # An error saved before any call was answered is a cell error, not a result.
    (dict(error='ProviderError: claude:opus is not usable: claude CLI not found',
          baseline=dict(stop_cause='ProviderError', calls=0),
          budget=dict(reserved_attempts=0, unknown_usage_attempts=0)), 'cell-error'),
    # Usage the CLI did not report: the token comparison would be a guess.
    (dict(error='', baseline=dict(stop_cause=None, calls=1),
          budget=dict(reserved_attempts=1, unknown_usage_attempts=1)), 'unknown-usage'),
    (dict(error='CapabilityUnavailable: claude:opus was denied a command the harness itself requires',
          baseline=dict(stop_cause='CapabilityUnavailable', calls=1),
          budget=dict(reserved_attempts=1, unknown_usage_attempts=0)), 'capability-failure'),
    # A baseline absorbs Ctrl-C to keep its record; the arm must not launch the next cell.
    (dict(error='KeyboardInterrupt: ', baseline=dict(stop_cause='interrupted', calls=1),
          budget=dict(reserved_attempts=1, unknown_usage_attempts=0)), 'interrupted-cell'),
])
def test_the_solo_arm_stops_on_the_pairs_stop_rules_and_on_a_spent_window(tmp_path, saved, reason):
    """A cell is never rerun, so a cell that could not measure the model must
    stop the arm before the next one is spent the same way."""
    _, out = _series(tmp_path)
    launched = []

    def launch(cell, packet):
        launched.append(cell['name'])
        run_dir = Path(cell['project']) / '.quadratus' / 'runs' / 'r'
        run_dir.mkdir(parents=True)
        (run_dir / 'result.json').write_text(json.dumps(dict(saved, mode='baseline', completed=False)))
        return dict(run_dir=str(run_dir), completed=False, result_error=saved['error'] or None)
    record = P.solo(out, model='claude:opus', launcher=launch, engine=ENGINE)
    assert launched == ['feat-0/solo-claude-opus-r0']
    assert record['stopped']['reason'].startswith(reason + ':') and record['stopped']['after'] == launched[0]
    with pytest.raises(RuntimeError, match='the solo arm stopped'):
        P.solo(out, model='claude:opus', launcher=launch, engine=ENGINE)
    assert len(launched) == 1


def test_a_solo_cell_whose_record_cannot_be_read_stops_the_arm_without_a_crash(tmp_path):
    _, out = _series(tmp_path)

    def launch(cell, packet):
        run_dir = Path(cell['project']) / '.quadratus' / 'runs' / 'r'
        run_dir.mkdir(parents=True)
        (run_dir / 'result.json').write_text('[1]')
        return dict(run_dir=str(run_dir), completed=False)
    record = P.solo(out, model='claude:opus', launcher=launch, engine=ENGINE)
    assert len(record['cells']) == 1 and record['stopped']['reason'].startswith('cell-error: the record of')
    assert json.loads((out / 'solo.json').read_text()) == record and not (out / P.LOCK_NAME).exists()


def test_a_solo_cell_that_ran_its_course_does_not_stop_the_arm(tmp_path):
    """A failed gate, a budget stop or a turn cap is the cell's own outcome."""
    _, out = _series(tmp_path)
    launched = []

    def launch(cell, packet):
        launched.append(cell['name'])
        run_dir = Path(cell['project']) / '.quadratus' / 'runs' / 'r'
        run_dir.mkdir(parents=True)
        (run_dir / 'result.json').write_text(json.dumps(dict(
            mode='baseline', completed=False, error='TurnLimitReached: stopped at its turn limit',
            baseline=dict(stop_cause='turn limit', calls=1),
            budget=dict(reserved_attempts=1, unknown_usage_attempts=0))))
        (run_dir / 'invocations.jsonl').write_text(json.dumps(dict(
            task='baseline', role='lead', canonical_model='claude:opus', invoked=True)) + '\n')
        return dict(run_dir=str(run_dir), completed=False, result_error='TurnLimitReached: stopped')
    record = P.solo(out, model='claude:opus', launcher=launch, engine=ENGINE)
    assert len(launched) == 2 and 'stopped' not in record


def test_the_solo_arm_end_to_end_one_model_the_frozen_graders_and_the_comparison(env, tmp_path, capsys,
                                                                                 monkeypatch):
    """The real launcher: run_baseline on the cell's worktree under the
    packet's limits, graded by the frozen graders, compared from the records."""
    import subprocess
    _, _, trace = env
    packet, out = _series(tmp_path, graders=True)
    record = P.solo(out, model='claude:opus', only='feat-0', engine=ENGINE)
    (cell,) = record['cells']
    assert cell['state'] == 'ran' and cell['result_error'] is None and cell['completed'] is False
    data = json.loads((Path(cell['run_dir']) / 'result.json').read_text())
    assert data['mode'] == 'baseline' and data['checks'][-1]['passed']
    assert data['selected_limits']['run_limits']['max_reported_tokens'] == 500_000
    (call,) = calls(trace)
    assert call['cwd'] == cell['project'] and packet['tasks'][0]['goal'] in call['prompt']
    assert 'You may change only these paths: add.py, test_add.py.' in call['prompt']
    assert (cell['grade']['passed'], cell['grade']['total']) == (1, 2)
    grades = P.write_grades(out)
    assert json.loads(Path(grades[cell['name']]).read_text())['passed'] == 1
    assert Path(cell['project']) not in Path(grades[cell['name']]).parents
    # No pair cell has run yet, so there is nothing to compare the solo cell with.
    assert P.compare_commands(out) == []

    def pair_cell(cell, packet):
        run_dir = _project_run_folder(Path(cell['project']), '.pair-run')
        (Path(cell['project']) / 'add.py').write_text('def add(a, b):\n    return a + b\n')
        return dict(run_dir=str(run_dir), completed=False)
    P.run(out, launcher=pair_cell, only='feat-0', engine=ENGINE, versions={})
    (command,) = P.compare_commands(out)
    assert command.count('--grade') == 3 and command[3] == cell['run_dir']
    done = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert 'graders: 1 of 2 passed' in done.stdout and 'tokens per grader-passed requirement: 110' in done.stdout
    assert 'Run 3 against run 1:' in done.stdout and 'mode: differs (project run against baseline)' in done.stdout
    assert 'tokens per grader-passed requirement: +6,890' in done.stdout
    # And from the command line, which prints the comparison command and reruns nothing.
    monkeypatch.setattr(P, 'engine_identity', lambda: ENGINE)
    assert P.main(['solo', '--out', str(out), '--model', 'claude:opus', '--only', 'feat-0']) == 0
    assert 'compare_runs.py' in capsys.readouterr().out and len(calls(trace)) == 1
