'''Synthetic-file tests for tools/research_usage_report.py. No engine, no provider.'''

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'research_usage_report.py'
_spec = importlib.util.spec_from_file_location('research_usage_report_under_test', TOOL)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)


def ev(**kw):
    base = dict(task='t1', role='lead', invoked=True)
    base.update(kw)
    return json.dumps(base)


def good_record(**changes):
    src = 'a' * 64
    record = dict(
        completed=True, error='', checks=[dict(passed=True, command='pytest')],
        source_fingerprint=src, in_flight={}, explicit_tasks=None,
        requirements=dict(listed={'R1': 'x', 'R2': 'y'},
                          status={'R1': 'met (audited)', 'R2': 'met (audited)'},
                          audits=[dict(verdicts={'R1': dict(met=True), 'R2': dict(met=True)})]),
        workflow=dict(tasks=[dict(task_id='t1', closed_as='closed', checks=[dict(
            passed=True, source=src, receipts=[dict(id='check', required=True, status='passed', tests=3)])])]))
    record.update(changes)
    return record


def make_run(tmp_path, lines, record=None):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'invocations.jsonl').write_text(chr(10).join(lines) + chr(10))
    if record is not None:
        (run / 'result.json').write_text(json.dumps(record))
        (run / 'changes.diff').write_text('+++ b/app.py' + chr(10))
    return run


def glob(run):
    return tool.build_report(run)['usage']['global']


def test_cached_tokens_are_a_subset_never_added_twice(tmp_path):
    g = glob(make_run(tmp_path, [ev(input_tokens=100, output_tokens=20, cached_input_tokens=60)]))
    assert g['input_tokens_known'] == 100 and g['cached_input_tokens_known'] == 60
    assert g['total_tokens'] == 120 and g['tokens_complete']


def test_cached_above_input_is_unknown_not_added(tmp_path):
    report = tool.build_report(make_run(tmp_path, [ev(input_tokens=10, output_tokens=1, cached_input_tokens=50)]))
    assert report['usage']['global']['cached_unknown_events'] == 1 and report['warnings']


def test_text_labels_cached_as_known_subtotal_with_unknown_count(tmp_path):
    report = tool.build_report(make_run(tmp_path, [ev(input_tokens=10, output_tokens=1)]))
    text = chr(10).join(tool.render(report))
    assert 'KNOWN cached subtotal 0' in text and 'cached_unknown_events 1' in text


def test_partial_input_output_gives_lower_bound_and_null_total(tmp_path):
    g = glob(make_run(tmp_path, [ev(input_tokens=10), ev(input_tokens=5, output_tokens=2)]))
    assert g['input_tokens_known'] == 15 and g['output_tokens_known'] == 2
    assert g['known_total_tokens'] == 17 and g['total_tokens'] is None
    assert g['output_unknown_events'] == 1 and not g['tokens_complete']


@pytest.mark.parametrize('bad', [True, False, -1, '5', 1.5, None])
def test_invalid_token_values_stay_unknown(tmp_path, bad):
    g = glob(make_run(tmp_path, [ev(input_tokens=bad, output_tokens=3)]))
    assert g['input_unknown_events'] == 1 and g['input_tokens_known'] == 0
    assert g['total_tokens'] is None and g['known_total_tokens'] == 3


def test_selected_not_invoked_is_separate(tmp_path):
    lines = [ev(input_tokens=1, output_tokens=1), ev(invoked=False, selected=True, role='collab'),
             ev(invoked=False, selected=False)]
    report = tool.build_report(make_run(tmp_path, lines, good_record()))
    g = report['usage']['global']
    assert g['invocations'] == 1 and g['selected_not_invoked'] == 1 and g['total_tokens'] == 2
    assert report['usage']['ledger_complete'] and not report['problems']
    assert report['usage']['by_role']['collab']['invocations'] == 0
    assert report['usage']['by_role']['collab']['selected_not_invoked'] == 1


def test_retries_are_distinct_invocations(tmp_path):
    same = ev(invocation_id='x', attempt=1, input_tokens=4, output_tokens=1)
    g = glob(make_run(tmp_path, [same, same]))
    assert g['invocations'] == 2 and g['total_tokens'] == 10


def test_grouping_by_task_and_role(tmp_path):
    report = tool.build_report(make_run(tmp_path, [
        ev(task='t1', role='lead', input_tokens=1, output_tokens=1),
        ev(task='t2', role='lead', input_tokens=2, output_tokens=2)]))
    assert report['usage']['by_task']['t2']['total_tokens'] == 4
    assert report['usage']['by_role']['lead']['invocations'] == 2


def test_mixed_malformed_line_and_good_row_fails_closed(tmp_path, capsys):
    run = make_run(tmp_path, ['{bad', ev(input_tokens=1, output_tokens=1), '[1]'], good_record())
    report = tool.build_report(run)
    usage = report['usage']
    assert len(report['problems']) >= 2 and usage['global']['invocations'] == 1
    assert usage['ledger_complete'] is False and usage['invalid_event_lines'] == 2
    g = usage['global']
    assert g['known_total_tokens'] == 2 and g['total_tokens'] is None and g['tokens_complete'] is False
    for group in list(usage['by_task'].values()) + list(usage['by_role'].values()):
        assert group['total_tokens'] is None and group['tokens_complete'] is False
    out = report['outcome']
    assert out['verdict'] == 'verified'
    assert out['known_tokens_per_verified_requirement_lower_bound'] == 1
    assert out['total_tokens_per_verified_requirement'] is None
    assert tool.main([str(run)]) == 3
    assert 'PROBLEM' in capsys.readouterr().out


def test_missing_invocations_file_fails_closed_via_cli(tmp_path, capsys):
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'result.json').write_text(json.dumps(good_record()))
    (run / 'changes.diff').write_text('+++ b/app.py' + chr(10))
    assert tool.main([str(run), '--json']) == 3
    data = json.loads(capsys.readouterr().out)
    g = data['usage']['global']
    assert g['total_tokens'] is None and g['tokens_complete'] is False
    assert data['usage']['ledger_complete'] is False and data['usage']['invalid_event_lines'] is None
    assert data['outcome']['total_tokens_per_verified_requirement'] is None


@pytest.mark.parametrize('line', [
    json.dumps(dict(task='t1', role='lead', input_tokens=9, output_tokens=9)),
    json.dumps(dict(task='t1', role='lead', invoked='yes', input_tokens=9, output_tokens=9)),
    json.dumps(dict(task='t1', role='lead', invoked=1, input_tokens=9, output_tokens=9)),
    json.dumps(dict(task='t1', role='lead', invoked=False, selected='yes')),
])
def test_bad_invoked_is_input_problem_and_keeps_valid_rows(tmp_path, line):
    run = make_run(tmp_path, [ev(input_tokens=1, output_tokens=2), line], good_record())
    report = tool.build_report(run)
    g = report['usage']['global']
    assert g['invocations'] == 1 and g['selected_not_invoked'] == 0
    assert g['known_total_tokens'] == 3 and g['total_tokens'] is None
    assert report['usage']['ledger_complete'] is False and report['usage']['invalid_event_lines'] == 1
    assert report['problems']
    assert report['outcome']['total_tokens_per_verified_requirement'] is None
    assert tool.main([str(run)]) == 3


def test_not_selected_not_invoked_stays_ignored_and_complete(tmp_path):
    report = tool.build_report(make_run(tmp_path, [ev(input_tokens=1, output_tokens=1),
                                                   ev(invoked=False, selected=False)]))
    assert report['usage']['ledger_complete'] and report['usage']['global']['total_tokens'] == 2


def test_zero_requirements_has_no_denominator(tmp_path):
    record = good_record(requirements=dict(listed={}, status={}, audits=[]))
    out = tool.build_report(make_run(tmp_path, [ev(input_tokens=1, output_tokens=1)], record))['outcome']
    assert out['verified_requirements'] is None
    assert out['known_tokens_per_verified_requirement_lower_bound'] is None
    assert out['total_tokens_per_verified_requirement'] is None


def test_unsafe_run_gives_no_denominator_but_shows_audited_claims(tmp_path):
    record = good_record(completed=False)
    out = tool.build_report(make_run(tmp_path, [ev(input_tokens=10, output_tokens=10)], record))['outcome']
    assert out['verdict'] == 'not-met' and out['verified_requirements'] is None
    assert out['known_tokens_per_verified_requirement_lower_bound'] is None
    assert out['audited_met_claims'] == ['R1', 'R2'] and 'not whole-run' in out['audited_met_claims_label']
    assert out['tasks_completed_as_recorded'] == 1


def test_completed_counts_only_closed_as_closed(tmp_path):
    record = good_record()
    record['workflow']['tasks'].append(dict(task_id='t2', closed_as='completed'))
    out = tool.build_report(make_run(tmp_path, [ev(input_tokens=1, output_tokens=1)], record))['outcome']
    assert out['tasks_recorded'] == 2 and out['tasks_completed_as_recorded'] == 1


def test_verified_run_gives_lower_bound_and_total_ratio(tmp_path):
    out = tool.build_report(make_run(tmp_path, [ev(input_tokens=30, output_tokens=10)], good_record()))['outcome']
    assert out['verdict'] == 'verified' and out['verified_requirements'] == 2
    assert out['known_tokens_per_verified_requirement_lower_bound'] == 20
    assert out['total_tokens_per_verified_requirement'] == 20


def test_verified_run_with_unknown_tokens_has_null_total_ratio(tmp_path):
    out = tool.build_report(make_run(tmp_path, [ev(input_tokens=30, output_tokens=10), ev(input_tokens=4)],
                                     good_record()))['outcome']
    assert out['known_tokens_per_verified_requirement_lower_bound'] == 22
    assert out['total_tokens_per_verified_requirement'] is None


def test_verified_with_zero_known_tokens_has_no_lower_bound(tmp_path):
    out = tool.build_report(make_run(tmp_path, [ev()], good_record()))['outcome']
    assert out['verified_requirements'] == 2
    assert out['known_tokens_per_verified_requirement_lower_bound'] is None


def test_missing_result_keeps_usage_and_states_reason(tmp_path):
    run = make_run(tmp_path, [ev(input_tokens=3, output_tokens=4)])
    report = tool.build_report(run)
    assert report['usage']['global']['total_tokens'] == 7
    assert report['outcome']['verified_requirements'] is None
    assert 'no readable result.json' in report['outcome']['verified_requirements_reason']
    assert tool.main([str(run)]) == 3


def _bad_requirements(**kw):
    reqs = dict(listed={'R1': 'x'}, status={'R1': 'met (audited)'},
                audits=[dict(verdicts={'R1': dict(met=True)})])
    reqs.update(kw)
    return good_record(requirements=reqs)


@pytest.mark.parametrize('record', [
    _bad_requirements(audits=42),
    _bad_requirements(listed=[1, 2]),
    _bad_requirements(status=7),
    _bad_requirements(audits=[dict(verdicts={'R1': 5})]),
    _bad_requirements(audits=[42]),
    good_record(requirements=42),
    good_record(workflow=dict(tasks=42)),
    good_record(workflow=42),
    good_record(workflow=dict(tasks=[42])),
])
def test_malformed_result_schema_does_not_crash(tmp_path, record):
    run = make_run(tmp_path, [ev(input_tokens=3, output_tokens=4)], record)
    report = tool.build_report(run)
    out = report['outcome']
    assert report['usage']['global']['total_tokens'] == 7
    assert out['verdict'] != 'verified' and out['verified_requirements'] is None
    assert out['total_tokens_per_verified_requirement'] is None
    assert report['problems']
    assert tool.main([str(run)]) == 3


def test_undecodable_result_bytes_do_not_crash(tmp_path):
    run = make_run(tmp_path, [ev(input_tokens=3, output_tokens=4)], good_record())
    (run / 'result.json').write_bytes(bytes([255, 254, 0]))
    report = tool.build_report(run)
    assert report['usage']['global']['total_tokens'] == 7
    assert report['outcome']['verified_requirements'] is None
    assert any('result.json' in p for p in report['problems'])


def test_json_is_stable_and_overhead_unavailable(tmp_path, capsys):
    run = make_run(tmp_path, [ev(input_tokens=1, output_tokens=1)], good_record())
    assert tool.main([str(run), '--json']) == 0
    text = capsys.readouterr().out
    data = json.loads(text)
    assert text.strip() == json.dumps(data, indent=2, sort_keys=True)
    assert data['overhead_attribution'] == 'unavailable'


def test_runs_isolated_without_quadratus_imports(tmp_path):
    run = make_run(tmp_path, [ev(input_tokens=1, output_tokens=1)], good_record())
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    (cwd / 'quadratus.py').write_text('raise RuntimeError(chr(112) + chr(108) + chr(97) + chr(110) + chr(116))')
    proc = subprocess.run([sys.executable, '-I', str(TOOL), str(run), '--json'], cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)['usage']['global']['total_tokens'] == 2
    tree = ast.parse(TOOL.read_text())
    names = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(name.split('.')[0] == 'quadratus' for name in names)
