'''Verifier probe: behavior-based acceptance of alternative implementations.

SPECIFICATION (written first, before any candidate):

    add(a, b) is a pure function over Python integers. It returns the
    mathematical sum a + b as an int. Zero is the identity (add(0, 7) == 7,
    add(-7, 0) == -7, add(0, 0) == 0). Negative operands follow ordinary
    integer addition (add(-2, -3) == -5, add(-2, 5) == 3, add(2, -5) == -3).
    It has no side effects, so repeated calls agree.

The same executable unittest assertions are run in a fresh subprocess and
temp directory against two structurally different valid implementations and
one deliberately defective one. Each run is bound to the sha256 of the exact
candidate bytes it executed, and tools/truth_check.py judges the receipts.

SCOPE: narrow acceptance behavior only. The auditor verdict in every receipt
is a SYNTHETIC fixture, not a live reviewer; no Session, provider or engine
is invoked. This does not measure live reviewer quality.
'''

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    'truth_check_probe', Path(__file__).resolve().parents[1] / 'tools' / 'truth_check.py')
truth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(truth)

CHECK = r'''
import hashlib, importlib.util, io, json, sys, unittest
from pathlib import Path

path = Path(__file__).with_name('candidate.py')
digest = hashlib.sha256(path.read_bytes()).hexdigest()
spec = importlib.util.spec_from_file_location('candidate', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
add = mod.add


class AddSpec(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_zero_is_identity(self):
        self.assertEqual(add(0, 0), 0)
        self.assertEqual(add(0, 7), 7)
        self.assertEqual(add(-7, 0), -7)

    def test_negative_operands(self):
        self.assertEqual(add(-2, -3), -5)
        self.assertEqual(add(-2, 5), 3)
        self.assertEqual(add(2, -5), -3)

    def test_returns_int(self):
        self.assertIs(type(add(-1, 1)), int)

    def test_pure_and_repeatable(self):
        self.assertEqual(add(-4, 9), add(-4, 9))


suite = unittest.defaultTestLoader.loadTestsFromTestCase(AddSpec)
result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
print(json.dumps(dict(sha256=digest, tests=result.testsRun,
                      failures=len(result.failures) + len(result.errors))))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def src(*lines):
    return chr(10).join(lines) + chr(10)


VALID_A = src('def add(a, b):', '    return a + b')
VALID_B = src('def add(a, b):', '    total = a', '    step = 1 if b > 0 else -1',
              '    for _ in range(abs(b)):', '        total += step', '    return total')
BAD = src('def add(a, b):', '    return abs(a) + abs(b)')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def execute(tmp_path, name, source):
    folder = tmp_path / name
    folder.mkdir()
    data = source.encode()
    (folder / 'candidate.py').write_bytes(data)
    (folder / 'check_add.py').write_text(CHECK)
    proc = subprocess.run([sys.executable, '-I', str(folder / 'check_add.py')], cwd=folder,
                          capture_output=True, text=True, timeout=60)
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report['sha256'] == sha(data), 'the run must be bound to the executed bytes'
    return folder, proc.returncode, report, data


def receipt(folder, executed, report, code, final=None):
    '''Honest receipt of an executed check. SYNTHETIC auditor fixture.'''
    final = executed if final is None else final
    passed = code == 0
    run = folder / 'run'
    run.mkdir()
    record = dict(
        synthetic=True, completed=passed, error='',
        checks=[dict(passed=passed, command='python -I check_add.py')],
        source_fingerprint=sha(final), in_flight={}, explicit_tasks=None,
        requirements=dict(
            listed={'R1': 'add(a, b) returns the integer sum, zero and negatives included'},
            status={'R1': 'met (SYNTHETIC auditor fixture)' if passed else 'NOT MET: executed assertions failed'},
            audits=[dict(auditor='SYNTHETIC fixture, not a live reviewer',
                         verdicts={'R1': dict(met=passed, why='derived from the executed assertions')})]),
        workflow=dict(tasks=[dict(task_id='t1', checks=[dict(
            passed=passed, source=sha(executed),
            receipts=[dict(id='check', required=True, status='passed' if passed else 'failed',
                           tests=report['tests'])])])]))
    (run / 'result.json').write_text(json.dumps(record))
    added = ''.join('+' + line + chr(10) for line in final.decode().splitlines())
    (run / 'changes.diff').write_text('+++ b/candidate.py' + chr(10) + added)
    return run


@pytest.mark.parametrize('name,source', [('valid_a', VALID_A), ('valid_b', VALID_B)])
def test_structurally_distinct_valid_implementations_are_verified(tmp_path, name, source):
    folder, code, report, data = execute(tmp_path, name, source)
    assert code == 0 and report['failures'] == 0 and report['tests'] >= 5
    result = truth.judge(receipt(folder, data, report, code))
    assert result['verdict'] == truth.VERIFIED, result


def test_the_defective_variant_fails_execution_and_the_receipt(tmp_path):
    folder, code, report, data = execute(tmp_path, 'bad', BAD)
    assert code != 0 and report['failures'] > 0
    assert report['sha256'] not in (sha(VALID_A.encode()), sha(VALID_B.encode()))
    result = truth.judge(receipt(folder, data, report, code))
    assert result['verdict'] == truth.NOT_MET, result


def test_a_receipt_for_other_bytes_than_the_final_source_is_unverified(tmp_path):
    folder, code, report, data = execute(tmp_path, 'stale', VALID_A)
    edited = data + b'# edited after the check ran' + chr(10).encode()
    result = truth.judge(receipt(folder, data, report, code, final=edited))
    assert result['verdict'] == truth.UNVERIFIED, result
    assert any('final source' in reason for reason in result['unverified'])
