"""Codex review of 1995d02 (#41), P1: check debt must not settle on a gate that
did not rerun the failed check. Reproducer kept verbatim in substance."""
import json
import sys

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_check_debt import IMPLEMENT, MET, REQS, _looked, _plan
from tests.lifecycle.test_lifecycle_matrix import FILES, Script

pytestmark = pytest.mark.requirements_ledger


def test_a_repair_without_the_failed_gate_cannot_settle_check_debt(tmp_path, monkeypatch):
    policy = {
        'kind': 'policy', 'schema_version': 1, 'repository_id': 'fixture/project',
        'library': {'version': 'pilot-1'},
        'defaults': {'family': 'pure-logic', 'required_gates': ['scope'],
                     'scope_max_lines': 50, 'overrun_ratio': 1.1, 'worker_depth': 1},
        'capability_policy': {'deny_write': ['.env', '.git/**'], 'sensitive': [],
                              'external_effects': 'deny', 'native_delegation': 'deny'},
        'path_rules': [{'paths': ['README.md'], 'families': ['test-author']}],
        'gate_bindings': {'unit-tests': {'gate': 'pytest'},
                          **{k: {'absent': True, 'because': 'Fixture only'}
                             for k in ['test-both-ways', 'lint', 'typecheck']}},
        'gates': [{'id': 'scope', 'runner': 'builtin:scope', 'required': True},
                  {'id': 'pytest', 'runner': 'command', 'required': True,
                   'argv': [sys.executable, '-m', 'pytest', '-q', '--quadratus-report={report}',
                            'checks/test_app.py'], 'minimum_tests': 1}],
        'decisions': [],
    }
    repair = ('KIND: docs simple\nSCOPE: ' + json.dumps(dict(
        permitted_paths=['README.md'], intended_result='Document add',
        acceptance=['README describes add'], max_lines=20))
        + '\nDocument add.\nCOVERS: R1\nRESOLVES: F1')
    files = {**{k.replace('tests/', 'checks/'): v for k, v in FILES.items()},
             'checks/test_smoke.py': 'def test_smoke():\n    assert True\n',
             '.quadratus/policy.json': json.dumps(policy)}
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(REQS + IMPLEMENT, repair), lead=_looked,
                                                 **{'requirements-review': lambda c, r: 'COMPLETE',
                                                    'auditor': lambda c, r: MET}),
                   files=files, check=H.GATE + ' checks/test_smoke.py', max_tasks=4, record_complete=False)
    result = H.result_json(replay)
    assert result['findings'][0]['status'] == 'open', (replay.result.completed, result['findings'])
    assert not replay.result.completed
