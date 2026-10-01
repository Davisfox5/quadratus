import json
from pathlib import Path

import pytest

from tests.lifecycle.test_explicit_tasks import README_TASK, _run, _script


@pytest.mark.requirements_ledger
@pytest.mark.parametrize('rid,complete', [('R2', True), ('R1', False)])
def test_ambiguity_is_scoped_to_explicit_claims(tmp_path, monkeypatch, rid, complete):
    replay = _run(tmp_path, monkeypatch,
        ['REQUIREMENTS:\nR1: README documents add\nR2: unrelated CSV quoting behavior\n' + README_TASK + '\nCOVERS: R1'],
        max_tasks=1, record_complete=False,
        script=_script(**{'requirements-review': lambda c,r: f'AMBIGUOUS: {rid} - two incompatible readings',
                         'auditor': lambda c,r: 'R1: MET - README.md'}))
    assert replay.of('orchestrator') == []
    assert replay.result.completed is complete, replay.result.error
    if complete:
        record=json.loads((Path(replay.result.run_dir)/'result.json').read_text())['explicit_tasks']
        assert record['requirements_unclaimed'] == ['R2']
    else:
        assert 'ambiguous' in replay.result.error


@pytest.mark.requirements_ledger
def test_failed_claim_is_not_reclassified_as_unclaimed(tmp_path, monkeypatch):
    from quadratus.session import Session

    original = Session._requirements_satisfied
    attempts = []

    def repeat(session):
        first = original(session)
        second = original(session)
        attempts.append((first, second))
        return second

    monkeypatch.setattr(Session, '_requirements_satisfied', repeat)
    replay = _run(tmp_path, monkeypatch,
        ['REQUIREMENTS:\nR1: README documents add\nR2: unrelated CSV behavior\n'
         + README_TASK + '\nCOVERS: R1'], max_tasks=1, record_complete=False,
        script=_script(**{'requirements-review': lambda c, r: 'COMPLETE',
                         'auditor': lambda c, r: 'R1: NOT MET - README.md lacks the contract'}))
    assert attempts == [(False, False)]
    assert not replay.result.completed
    record = json.loads((Path(replay.result.run_dir) / 'result.json').read_text())['explicit_tasks']
    assert record['requirements_claimed'] == ['R1']
    assert record['requirements_unclaimed'] == ['R2']
    assert len(replay.of('auditor')) == 1
