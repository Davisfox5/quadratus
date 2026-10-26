"""Offline saved-record regressions for the research usage report handoff.

Immutable acceptance definition (SYNTHETIC fixtures, not product performance):
the request was a usage report over saved run records that never publishes a
complete total or total-token-per-verified-requirement unless evidence is
complete. R1-R3 below are fixed and run unchanged against one archived
historical source (a single real historical regression pair) and the current
tool. No engine, provider, git or network.
"""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / 'fixtures' / 'research_handoff'
CURRENT = ROOT / 'tools' / 'research_usage_report.py'
TRUTH = ROOT / 'tools' / 'truth_check.py'
REQUIRED_IDS = ('R1', 'R2', 'R3')
ACCEPTANCE = (
    ('R1', 'malformed ledger line keeps known 120, no complete total/ratio, nonzero exit, parse problem shown'),
    ('R2', 'closed_as=closed task counts 1; task completion alone gives no verified denominator without audit'),
    ('R3', 'audit without auditor is incomplete not corrupt: total 7 kept, exit 0, unverified, no denominator'),
)
PINNED = {
    'early_report': ('early_report.py.txt', '9dca3b10e21ca96566b7a60cf6431b2a01d1ab0c'),
}
PINNED_SHA = '5c5fb6d0b111f6c5dc002326da412985a2e318e732b0f448dbe704e2fb839a2c'
# SYNTHETIC metadata only; says nothing about what any real author claimed.
SYNTHETIC_CLAIM = {'synthetic': True, 'claim': 'SYNTHETIC: handoff marked complete'}
NO_AUDITOR = [{'auditor': None, 'result': 'no auditor available'}]
SYNTH_AUDIT = [{'auditor': 'SYNTHETIC-fixture-auditor', 'verdicts': {'R1': {'met': True}}}]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_archive(name, data):
    """Production-independent integrity check for an archived predecessor.

    Raises AssertionError with a clear message unless data matches the pinned SHA.
    """
    if name not in PINNED:
        raise AssertionError(f'unknown archive {name!r}; pinned: {sorted(PINNED)}')
    actual = sha(data)
    if actual != PINNED_SHA:
        raise AssertionError(
            f'archive {name!r} integrity failure: sha256 {actual} != pinned {PINNED_SHA} ({len(data)} bytes)')
    return data


def source_bytes(name):
    if name == 'current':
        return CURRENT.read_bytes()
    return validate_archive(name, (FIX / PINNED[name][0]).read_bytes())


def record(audits):
    src = 'a' * 64
    receipt = dict(id='check', required=True, status='passed', tests=3)
    return dict(
        completed=True, checks=[dict(passed=True, command='pytest')], source_fingerprint=src,
        requirements=dict(listed={'R1': 'synthetic requirement'}, status={'R1': 'met (synthetic)'}, audits=audits),
        workflow=dict(tasks=[dict(task_id='t1', closed_as='closed',
                                  checks=[dict(passed=True, source=src, receipts=[receipt])])]))


def row(i, o):
    return json.dumps(dict(task='t1', role='lead', invoked=True, input_tokens=i, output_tokens=o))


def write_run(path, lines, audits):
    path.mkdir(parents=True)
    (path / 'invocations.jsonl').write_bytes(('\n'.join(lines) + '\n').encode())
    (path / 'result.json').write_text(json.dumps(record(audits), sort_keys=True))
    (path / 'changes.diff').write_text('+++ b/app.py\n')
    (path / 'handoff_claim.json').write_text(json.dumps(SYNTHETIC_CLAIM))
    return path


def tree_hash(path):
    h = hashlib.sha256()
    for p in sorted(path.iterdir()):
        h.update(p.name.encode() + b'\0' + p.read_bytes())
    return h.hexdigest()


def run_tool(tmp_path, source, run):
    tools = Path(tempfile.mkdtemp(dir=tmp_path)) / 'tools'
    tools.mkdir()
    script = tools / 'research_usage_report.py'
    script.write_bytes(source)
    (tools / 'truth_check.py').write_bytes(TRUTH.read_bytes())
    before = sha(script.read_bytes())
    proc = subprocess.run([sys.executable, '-I', str(script), str(run), '--json'], cwd=tools.parent,
                          capture_output=True, text=True, timeout=60)
    assert sha(script.read_bytes()) == before == sha(source)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    return proc.returncode, data, proc.stderr[:500]


def ok_r1(rc, d):
    g, o = d['usage']['global'], d['outcome']
    return (rc != 0 and o['verdict'] == 'verified' and g['known_total_tokens'] == 120
            and g['total_tokens'] is None and o['total_tokens_per_verified_requirement'] is None
            and any('malformed JSON' in p for p in d['problems']))


def ok_r2(rc, d):
    o = d['outcome']
    return (o['tasks_completed_as_recorded'] == 1 and o['verified_requirements'] is None
            and o['total_tokens_per_verified_requirement'] is None
            and o['known_tokens_per_verified_requirement_lower_bound'] is None)


def ok_r3(rc, d):
    o = d['outcome']
    return (rc == 0 and not d['problems'] and o['verdict'] == 'unverified'
            and d['usage']['global']['total_tokens'] == 7 and o['verified_requirements'] is None
            and o['total_tokens_per_verified_requirement'] is None
            and o['known_tokens_per_verified_requirement_lower_bound'] is None)


SCENARIOS = {  # id -> (ledger lines, audits, check)
    'R1': ([row(100, 20), '{bad'], SYNTH_AUDIT, ok_r1),
    'R2': ([row(100, 20)], [], ok_r2),
    'R3': ([row(3, 4)], NO_AUDITOR, ok_r3),
}


def failed_ids(tmp_path, name):
    failed = set()
    for rid, (lines, audits, check) in SCENARIOS.items():
        run = write_run(tmp_path / f'{name}-{rid}', lines, audits)
        rc, data, _ = run_tool(tmp_path, source_bytes(name), run)
        try:
            good = data is not None and check(rc, data)
        except (KeyError, TypeError, IndexError):
            good = False
        if not good:
            failed.add(rid)
    return failed


def checklist_complete(ids):
    return tuple(sorted(ids)) == REQUIRED_IDS


# Observed (executed) expectations. The archived source already has the R1/R2
# fixes and still misclassifies a no-auditor audit as corrupt (R3).
@pytest.mark.parametrize('name,expected', [
    ('early_report', {'R3'}),  # negative control: the historical no-auditor bug
    ('current', set()),
])
def test_exact_failure_sets(tmp_path, name, expected):
    assert failed_ids(tmp_path, name) == expected


@pytest.mark.parametrize('name', ['early_report', 'current'])
def test_clean_completed_positive_control(tmp_path, name):
    run = write_run(tmp_path / 'clean', [row(100, 20)], SYNTH_AUDIT)  # synthetic audit, not product evidence
    rc, data, err = run_tool(tmp_path, source_bytes(name), run)
    assert rc == 0, err
    o = data['outcome']
    assert o['verdict'] == 'verified' and o['verified_requirements'] == 1
    assert o['tasks_completed_as_recorded'] == 1 and o['total_tokens_per_verified_requirement'] == 120
    assert not data['problems']


def test_inherited_state_same_bytes_before_and_after_repair(tmp_path):
    run = write_run(tmp_path / 'inherited', [row(3, 4)], NO_AUDITOR)
    before = tree_hash(run)
    rc_old, old, _ = run_tool(tmp_path, source_bytes('early_report'), run)
    assert tree_hash(run) == before
    rc_new, new, _ = run_tool(tmp_path, source_bytes('current'), run)
    assert tree_hash(run) == before
    assert SYNTHETIC_CLAIM['synthetic'] and json.loads((run / 'handoff_claim.json').read_text())['synthetic']
    assert rc_old == 3 and any('verdicts' in p for p in old['problems'])
    assert rc_new == 0 and new['problems'] == []
    assert new['outcome']['verdict'] == 'unverified' and new['outcome']['verified_requirements'] is None


def test_no_auditor_is_incomplete_not_corrupt_in_current(tmp_path):
    run = write_run(tmp_path / 'noaud', [row(3, 4)], NO_AUDITOR)
    rc, data, err = run_tool(tmp_path, source_bytes('current'), run)
    assert rc == 0, err
    assert data['problems'] == [] and data['usage']['global']['total_tokens'] == 7
    assert data['outcome']['total_tokens_per_verified_requirement'] is None


def test_fixture_hashes_and_provenance():
    prov = json.loads((FIX / 'provenance.json').read_text(encoding='utf-8'))
    assert prov['engine_invocations'] == 0 and prov['live_agent_claims'].startswith('none')
    by_name = {p['name']: p for p in prov['predecessors']}
    assert set(by_name) == set(PINNED)
    for name, (fname, commit) in PINNED.items():
        entry = by_name[name]
        assert entry['path'] == fname and entry['commit'] == commit
        assert re.fullmatch(r'[0-9a-f]{40}', entry['commit'])
        validate_archive(name, (FIX / fname).read_bytes())
        assert entry['sha256'] == PINNED_SHA


def test_tampered_archive_is_detected():
    data = (FIX / PINNED['early_report'][0]).read_bytes()
    assert validate_archive('early_report', data) == data  # positive control: pristine bytes pass
    assert source_bytes('early_report') == data
    appended = data + b'\n# tamper\n'
    flipped = bytes([data[0] ^ 1]) + data[1:]
    truncated = data[:-1]
    for altered in (appended, flipped, truncated):
        assert altered != data
        with pytest.raises(AssertionError, match='integrity failure'):
            validate_archive('early_report', altered)
    with pytest.raises(AssertionError, match='unknown archive'):
        validate_archive('not_pinned', data)


def test_incomplete_or_substituted_checklist_rejected():
    assert tuple(i for i, _ in ACCEPTANCE) == REQUIRED_IDS
    assert tuple(SCENARIOS) == REQUIRED_IDS
    assert checklist_complete(SCENARIOS)
    assert not checklist_complete(['R1', 'R2'])
    assert not checklist_complete(['R1', 'R2', 'R4'])
    assert not checklist_complete(['R1', 'R2', 'R3', 'R4'])
