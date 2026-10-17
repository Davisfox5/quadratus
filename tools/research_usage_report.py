#!/usr/bin/env python3
'''Offline usage report for one finished run directory.

Usage: python -I tools/research_usage_report.py RUN_DIR [--json]

Reads RUN_DIR/invocations.jsonl and RUN_DIR/result.json. Imports nothing
from quadratus and launches nothing. Whole-run verification comes from
tools/truth_check.py, which also reads changes.diff, loaded by file path
and run on the existing artifacts.

Rules kept here:
- Only events with invoked == true are usage. Selected-but-not-invoked events
  are counted separately and carry no tokens. invoked must be a boolean; a row
  where it is absent or not a boolean is an input problem (the row is not
  used, the ledger is incomplete). selected=false, invoked=false is ignored.
- A token count that is missing, negative, a bool or not an int stays unknown.
  It is never read as zero and never estimated. Known subtotals are lower
  bounds; a complete total exists only when the ledger is complete and every
  invoked event is known.
- Missing, unreadable or malformed ledger lines make the ledger incomplete:
  every total (global, per task, per role) is null and no total ratio is given.
  Known subtotals are kept as lower bounds.
- cached_input_tokens is a subset of input_tokens and is never added again.
- Retries are not deduplicated: every line is a distinct invocation.
- No prices or seeds appear, and nothing here is a subscription cost.
- This is not a full accounting of vendor child processes.
- Overhead attribution is unavailable; it is not estimated.

Exit status: 0 when inputs were readable, 3 when any input was unreadable or
malformed (the report is still printed with its problems).
'''

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

UNKNOWN = '(unknown)'
NOTES = [
    'Only events with invoked=true are counted as usage; retries are separate invocations.',
    'Unknown token counts stay unknown; known totals are lower bounds unless tokens_complete.',
    'An incomplete ledger (missing or malformed lines) nulls every total and total ratio.',
    'Cached input tokens are a subset of input tokens and are not added to totals.',
    'No prices or seeds; nothing here is a subscription cost.',
    'Not a full accounting of vendor child processes.',
]


def _token(value):
    return value if type(value) is int and value >= 0 else None


def _name(value):
    return value if isinstance(value, str) and value else UNKNOWN


def _dict(value):
    return value if isinstance(value, dict) else {}


def _bucket():
    return {
        'invocations': 0,
        'selected_not_invoked': 0,
        'input_tokens_known': 0,
        'input_unknown_events': 0,
        'output_tokens_known': 0,
        'output_unknown_events': 0,
        'cached_input_tokens_known': 0,
        'cached_unknown_events': 0,
    }


def _count(bucket, known_key, unknown_key, value):
    if value is None:
        bucket[unknown_key] += 1
    else:
        bucket[known_key] += value


def _finish(bucket, ledger_complete):
    out = dict(bucket)
    out['known_total_tokens'] = bucket['input_tokens_known'] + bucket['output_tokens_known']
    complete = (ledger_complete and bucket['input_unknown_events'] == 0
                and bucket['output_unknown_events'] == 0)
    out['ledger_complete'] = ledger_complete
    out['tokens_complete'] = complete
    out['total_tokens'] = out['known_total_tokens'] if complete else None
    return out


def read_events(path, problems):
    '''Return (events, invalid_lines). invalid_lines is None when the file is unreadable.'''
    try:
        raw = path.read_bytes()
    except OSError as exc:
        problems.append(f'cannot read invocations.jsonl: {exc}')
        return [], None
    events, invalid = [], 0
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            problems.append(f'invocations.jsonl line {number}: malformed JSON')
            invalid += 1
            continue
        if not isinstance(event, dict):
            problems.append(f'invocations.jsonl line {number}: not a JSON object')
            invalid += 1
            continue
        invoked = event.get('invoked')
        if not isinstance(invoked, bool):
            problems.append(f'invocations.jsonl line {number}: invoked missing or not a boolean')
            invalid += 1
            continue
        if not invoked and not isinstance(event.get('selected', True), bool):
            problems.append(f'invocations.jsonl line {number}: selected is not a boolean')
            invalid += 1
            continue
        events.append((number, event))
    return events, invalid


def aggregate(events, warnings, ledger_complete=True):
    glob, tasks, roles = _bucket(), {}, {}
    for number, event in events:
        task, role = _name(event.get('task')), _name(event.get('role'))
        targets = [glob, tasks.setdefault(task, _bucket()), roles.setdefault(role, _bucket())]
        if event.get('invoked') is True:
            vals = {}
            for key in ('input_tokens', 'output_tokens', 'cached_input_tokens'):
                vals[key] = _token(event.get(key))
                if event.get(key) is not None and vals[key] is None:
                    warnings.append(f'line {number}: invalid {key} treated as unknown')
            inp, out, cached = vals['input_tokens'], vals['output_tokens'], vals['cached_input_tokens']
            if inp is not None and cached is not None and cached > inp:
                warnings.append(f'line {number}: cached_input_tokens exceeds input_tokens, treated as unknown')
                cached = None
            for bucket in targets:
                bucket['invocations'] += 1
                _count(bucket, 'input_tokens_known', 'input_unknown_events', inp)
                _count(bucket, 'output_tokens_known', 'output_unknown_events', out)
                _count(bucket, 'cached_input_tokens_known', 'cached_unknown_events', cached)
        elif event.get('selected', True) is True:
            for bucket in targets:
                bucket['selected_not_invoked'] += 1
    return {
        'global': _finish(glob, ledger_complete),
        'by_task': {k: _finish(v, ledger_complete) for k, v in sorted(tasks.items())},
        'by_role': {k: _finish(v, ledger_complete) for k, v in sorted(roles.items())},
        'event_lines': len(events),
        'ledger_complete': ledger_complete,
    }


def _load_truth():
    path = Path(__file__).resolve().with_name('truth_check.py')
    spec = importlib.util.spec_from_file_location('truth_check_independent', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema_problems(record):
    found = []
    workflow = record.get('workflow')
    if workflow is not None and not isinstance(workflow, dict):
        found.append('workflow is not an object')
    tasks = _dict(workflow).get('tasks')
    if tasks is not None:
        if not isinstance(tasks, list):
            found.append('workflow.tasks is not a list')
        elif not all(isinstance(t, dict) for t in tasks):
            found.append('workflow.tasks has a non-object entry')
    reqs = record.get('requirements')
    if reqs is not None and not isinstance(reqs, dict):
        found.append('requirements is not an object')
    reqs = _dict(reqs)
    for key in ('listed', 'status'):
        if reqs.get(key) is not None and not isinstance(reqs.get(key), dict):
            found.append(f'requirements.{key} is not an object')
    audits = reqs.get('audits')
    if audits is not None:
        if not isinstance(audits, list):
            found.append('requirements.audits is not a list')
        else:
            for audit in audits:
                if not isinstance(audit, dict):
                    found.append('requirements.audits has an entry without an object verdicts')
                    break
                verdicts = audit.get('verdicts')
                if verdicts is None:
                    # Audit recorded without verdicts (e.g. no auditor available):
                    # incomplete evidence, not a schema problem.
                    continue
                if not isinstance(verdicts, dict):
                    found.append('requirements.audits has an entry without an object verdicts')
                    break
                if not all(isinstance(v, dict) for v in verdicts.values()):
                    found.append('requirements.audits verdicts has a non-object value')
                    break
    return found


def summarize_result(run_dir, usage_total, problems):
    record = None
    try:
        record = json.loads((run_dir / 'result.json').read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            raise ValueError('not an object')
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        problems.append(f'cannot read result.json: {exc}')
        record = None
    verdict, reasons = 'unverified', []
    schema = []
    if record is None:
        reasons = ['no readable result.json']
    else:
        try:
            judged = _load_truth().judge(run_dir)
            verdict = judged['verdict']
            reasons = list(judged['not_met']) + list(judged['unverified'])
        except Exception as exc:  # the truth check must not hide the usage
            problems.append(f'truth_check unavailable: {exc}')
            reasons = ['truth_check could not be run']
        schema = _schema_problems(record)
        if schema:
            problems.extend('result.json schema: ' + s for s in schema)
            verdict = 'unverified'
            reasons = ['malformed result.json: ' + schema[0]] + reasons
    record = record or {}
    tasks = _dict(record.get('workflow')).get('tasks')
    tasks = tasks if isinstance(tasks, list) else []
    completed = sum(1 for t in tasks if isinstance(t, dict) and t.get('closed_as') == 'closed')
    reqs = _dict(record.get('requirements'))
    listed = _dict(reqs.get('listed'))
    audits_raw = reqs.get('audits')
    audits_raw = audits_raw if isinstance(audits_raw, list) else []
    audits = [a for a in audits_raw if isinstance(a, dict) and isinstance(a.get('verdicts'), dict)]
    final = audits[-1]['verdicts'] if audits else {}
    audited_met = sorted(r for r in listed if _dict(final.get(r)).get('met') is True)
    verified = len(listed) if verdict == 'verified' and listed else None
    known = usage_total['known_total_tokens']
    lower_bound = known / verified if verified and known > 0 else None
    total = usage_total['total_tokens']
    ratio = total / verified if verified and total is not None else None
    return {
        'verdict': verdict,
        'verdict_reasons': reasons,
        'tasks_recorded': len(tasks),
        'tasks_completed_as_recorded': completed,
        'requirements_listed': len(listed),
        'verified_requirements': verified,
        'verified_requirements_reason': None if verified else 'no verified denominator: ' + (reasons[0] if reasons else 'no requirements recorded'),
        'audited_met_claims': audited_met,
        'audited_met_claims_label': 'independently audited-met claims; not whole-run verification',
        'known_tokens_per_verified_requirement_lower_bound': lower_bound,
        'total_tokens_per_verified_requirement': ratio,
    }


def build_report(run_dir):
    run_dir = Path(run_dir)
    problems, warnings = [], []
    events, invalid = read_events(run_dir / 'invocations.jsonl', problems)
    ledger_complete = invalid == 0
    usage = aggregate(events, warnings, ledger_complete)
    usage['invalid_event_lines'] = invalid  # None means the ledger could not be read at all
    if not ledger_complete:
        warnings.append('invocation ledger incomplete: totals are unknown; known values are lower bounds')
    outcome = summarize_result(run_dir, usage['global'], problems)
    return {
        'run': str(run_dir),
        'usage': usage,
        'outcome': outcome,
        'overhead_attribution': 'unavailable',
        'problems': problems,
        'warnings': warnings,
        'notes': NOTES,
    }


def _line(label, b):
    calls, skipped = b['invocations'], b['selected_not_invoked']
    known, unk_i, unk_o = b['known_total_tokens'], b['input_unknown_events'], b['output_unknown_events']
    cached, unk_c = b['cached_input_tokens_known'], b['cached_unknown_events']
    total = b['total_tokens']
    total_text = 'unknown (incomplete)' if total is None else str(total)
    return (f'{label}: {calls} invoked, {skipped} selected-not-invoked, known tokens >= {known} '
            f'(unknown input on {unk_i}, output on {unk_o}), '
            f'KNOWN cached subtotal {cached} (subset of input; cached_unknown_events {unk_c}), '
            f'complete total {total_text}')


def render(report):
    usage, out = report['usage'], report['outcome']
    invalid = usage.get('invalid_event_lines')
    invalid_text = 'unknown (ledger unreadable)' if invalid is None else str(invalid)
    lines = ['Usage report: ' + report['run'],
             'Ledger complete: ' + ('yes' if usage['ledger_complete'] else 'no')
             + '; invalid/unknown event lines: ' + invalid_text,
             _line('All', usage['global'])]
    lines += [_line('  task ' + k, v) for k, v in usage['by_task'].items()]
    lines += [_line('  role ' + k, v) for k, v in usage['by_role'].items()]
    lines.append('Run verdict (independent truth check): ' + out['verdict'])
    lines.append('Tasks completed (as recorded): ' + str(out['tasks_completed_as_recorded']) + ' of ' + str(out['tasks_recorded']))
    if out['verified_requirements'] is None:
        lines.append('Verified requirements: none (' + '; '.join(out['verdict_reasons'][:3]) + ')')
    else:
        lines.append('Verified requirements: ' + str(out['verified_requirements']))
    lines.append('Independently audited-met claims (not whole-run verification): '
                 + str(len(out['audited_met_claims'])) + ' of ' + str(out['requirements_listed']))
    lb = out['known_tokens_per_verified_requirement_lower_bound']
    tr = out['total_tokens_per_verified_requirement']
    lines.append('Known tokens per verified requirement (lower bound): ' + ('n/a' if lb is None else f'{lb:.1f}'))
    lines.append('Total tokens per verified requirement: ' + ('n/a' if tr is None else f'{tr:.1f}'))
    lines.append('Overhead attribution: unavailable')
    lines += ['WARNING: ' + w for w in report['warnings']]
    lines += ['PROBLEM: ' + p for p in report['problems']]
    lines.append('Not a full accounting of vendor children; no prices, no subscription cost.')
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline usage report for one run directory.')
    parser.add_argument('run_dir')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = build_report(args.run_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in render(report):
            print(line)
    return 3 if report['problems'] else 0


if __name__ == '__main__':
    sys.exit(main())
