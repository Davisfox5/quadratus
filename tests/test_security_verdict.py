"""Strict security verdicts and repair control, with no live model calls."""

import json
import re

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateResult
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec
from quadratus.structured import StructuredError, parse_security_verdict


def verdict(snapshot='a' * 64, decision='accept'):
    return dict(schema_version=1, verdict=decision, snapshot_hash=snapshot,
                acceptance_results=[dict(criterion='tenant isolation', status='passed',
                                         evidence='fixture test excludes the other tenant')],
                blocking_findings=['missing filter'] if decision == 'reject' else [],
                limitations=['no test evidence'] if decision == 'insufficient_evidence' else [])


@pytest.mark.parametrize('transform', [
    lambda s: '```json\n' + s + '\n```', lambda s: 'Approved: ' + s,
    lambda s: s + s, lambda s: s.replace('"schema_version": 1', '"schema_version": true'),
    lambda s: s.replace('"schema_version": 1', '"schema_version": 2'),
    lambda s: s.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
    lambda s: s.replace('"limitations": []', '"limitations": [], "extra": 0'),
    lambda s: s.replace('"limitations": []', '"limitations": ["unverified"]'),
    lambda s: s.replace('"blocking_findings": []', '"blocking_findings": ["leak"]'),
    lambda s: s.replace('"status": "passed"', '"status": "failed"'),
    lambda s: s.replace('"acceptance_results": [', '"acceptance_results": [null,'),
    lambda s: s.replace('fixture test excludes the other tenant', ''),
    lambda s: s.replace('"verdict": "accept"', '"verdict": "yes"'),
    lambda s: s.replace('"limitations": []', '"limitations": NaN'),
    lambda s: s.replace('"criterion": "tenant isolation"', '"criterion": "other"'),
])
def test_invalid_contracts_are_rejected_without_recovery(transform):
    with pytest.raises(StructuredError):
        parse_security_verdict(transform(json.dumps(verdict())), snapshot_hash='a' * 64,
                               acceptance=['tenant isolation'])


def exercise(tmp_path, decisions, *, budget=1, gate=None, enabled=True, mutate=False):
    store = ArtifactStore(tmp_path / '.quadratus' / 'artifacts')
    (tmp_path / 'app.py').write_text('x = 1\n')
    calls, raw = [], []

    def invoke(model, prompt, **kw):
        calls.append((model, prompt))
        if 'verifying security work' in prompt:
            choice = decisions.pop(0)
            if not enabled or choice == 'malformed':
                answer = 'BLOCKING: still broken' if not enabled else 'Not JSON'
            else:
                snapshot = re.search(r'Snapshot hash: ([0-9a-f]+)', prompt)[1]
                answer = json.dumps(verdict('b' * 64 if choice == 'stale' else snapshot,
                                           'accept' if choice == 'stale' else choice))
            raw.append(answer)
            if mutate:
                (tmp_path / 'app.py').write_text('x = 2\n')
            return answer
        return 'SUMMARY: reviewed\nREASONING: fixture\nDEAD ENDS: none'

    if gate:
        gate.cwd = tmp_path
    session = Session('check isolation', store, invoke, config=SessionConfig(
        project=tmp_path, integration_gate=gate, security_verdict_json=enabled,
        max_gate_fixes=budget))
    session.run_task(TaskSpec('security-1', 'Review tenant isolation', kind='security',
                             scope=TaskScope(acceptance=['tenant isolation'])))
    for answer in raw:
        assert any(p.read_text() == answer for p in store.root.glob('*.txt'))
    assert {m for m, p in calls if 'verifying security work' in p} == {'claude:opus'}
    assert all(m != 'claude:fable' for m, _ in calls)
    return session, calls


@pytest.mark.parametrize('decision', ['malformed', 'stale', 'insufficient_evidence'])
def test_missing_or_stale_evidence_ends_incomplete_without_a_fix(tmp_path, decision):
    session, calls = exercise(tmp_path, [decision])
    assert session.open_findings
    assert len([p for _, p in calls if 'verifying security work' in p]) == 1


def test_accept_closes_without_findings(tmp_path):
    session, _ = exercise(tmp_path, ['accept'])
    assert not session.open_findings


def test_mutation_during_verification_invalidates_accept(tmp_path):
    session, _ = exercise(tmp_path, ['accept'], mutate=True)
    assert 'Source changed' in session.open_findings[0]


@pytest.mark.parametrize('decisions,budget,expected_calls,open_findings', [
    (['reject', 'accept'], 1, 2, False),
    (['reject', 'reject'], 5, 2, True),
    (['reject'], 0, 1, True),
])
def test_reject_gets_at_most_one_fix(tmp_path, decisions, budget, expected_calls, open_findings):
    session, calls = exercise(tmp_path, decisions, budget=budget)
    assert bool(session.open_findings) == open_findings
    assert len([p for _, p in calls if 'verifying security work' in p]) == expected_calls


def test_gate_repair_spends_the_shared_allowance(tmp_path):
    class Gate:
        calls = 0

        def run(self):
            self.calls += 1
            return GateResult(self.calls > 1, 'fixture', 0 if self.calls > 1 else 1, 'check')

    gate = Gate()
    session, calls = exercise(tmp_path, ['reject'], gate=gate)
    assert gate.calls == 2
    assert session.open_findings
    assert len([p for _, p in calls if 'verifying security work' in p]) == 1


def test_prose_fallback_is_flag_selected_not_parse_failure(tmp_path):
    session, _ = exercise(tmp_path, ['reject'], enabled=False)
    assert session.open_findings == ['BLOCKING: still broken']


def test_missing_acceptance_rows_fail_closed():
    doc = verdict()
    doc['acceptance_results'] = []
    with pytest.raises(StructuredError, match='every acceptance'):
        parse_security_verdict(json.dumps(doc), snapshot_hash='a' * 64,
                               acceptance=['tenant isolation'])


def test_cli_flag_reaches_project_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from quadratus.cli import main

    seen = []

    def run(goal, project, settings, **kwargs):
        seen.append(kwargs['security_verdict_json'])
        return SimpleNamespace(report='fixture', run_dir=tmp_path, completed=True)

    monkeypatch.setattr('quadratus.project_run.run_project', run)
    assert main(['--session', 'Review', '--project', str(tmp_path),
                 '--security-verdict-json']) == 0
    assert seen == [True]


def test_gate_and_security_repairs_use_one_counter_after_base_merge(tmp_path):
    class Gate:
        calls = 0

        def run(self):
            self.calls += 1
            return GateResult(self.calls > 1, 'fixture', int(self.calls == 1), 'check')

    gate = Gate()
    session, calls = exercise(tmp_path, ['reject', 'accept'], budget=2, gate=gate)
    assert session._gate_fixes_used == 2
    assert gate.calls == 3
    assert not session.open_findings
    assert len([p for _, p in calls if 'verifying security work' in p]) == 2


def test_failed_post_verdict_gate_cannot_buy_an_extra_repair(tmp_path):
    class Gate:
        calls = 0

        def run(self):
            self.calls += 1
            return GateResult(self.calls == 1, 'fixture', int(self.calls != 1), 'check')

    gate = Gate()
    session, calls = exercise(tmp_path, ['reject', 'accept'], budget=5, gate=gate)
    assert session._gate_fixes_used == 1
    assert gate.calls == 2
    assert not session.checks[-1]['passed']
    assert not any("The project's own integration check failed" in p for _, p in calls)


@pytest.mark.parametrize('reply,blocked', [
    ('**Verdict: accept the code change.**\n\n**Not blocking, worth noting**\nA frozen fixture assumption.', False),
    ('### Non-blocking: follow-up notes\nThe fixture is intentionally small.', False),
    ('**Not blocking**\nNo required changes.', False),
    ('BLOCKING: a tenant can read another tenant record.', True),
    ('This authorization defect is BLOCKING.', True),
    ('UNRESOLVED: the test evidence is missing.', True),
    ('**Not blocking, worth noting**\nBLOCKING: a separate tenant leak.', True),
    ('Not blocking, but UNRESOLVED: missing evidence.', True),
    ('BLOCKING: do not label the tenant leak not blocking.', True),
])
def test_security_prose_heading_does_not_invent_a_finding(tmp_path, reply, blocked):
    """Exercise the real security path; preserve actual findings after a note."""
    def invoke(model, prompt, **kwargs):
        if 'verifying security work' in prompt:
            return reply
        return 'SUMMARY: reviewed\nREASONING: fixture\nDEAD ENDS: none'

    session = Session('check isolation', ArtifactStore(tmp_path / 'artifacts'), invoke)
    session.run_task(TaskSpec('security-1', 'Review tenant isolation', kind='security'))
    assert bool(session.open_findings) is blocked
