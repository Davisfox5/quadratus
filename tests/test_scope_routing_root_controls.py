# ruff: noqa: E702 -- root review of b9d136e, its control file kept verbatim
"""Root controls: malformed input and superseded failure identity, no providers."""
from copy import deepcopy

from tests.test_scope_routing_units import CASE, _declare, _result, _session


def test_dot_path_is_rejected_without_crashing_the_turn(tmp_path):
    s, _ = _session(tmp_path)
    _declare(s, 'OUTSIDE SCOPE: RFT1-1 - .')
    assert s.scope_declarations[-1]['status'] == 'rejected'


def test_existing_directory_is_not_accepted_as_a_file(tmp_path):
    s, _ = _session(tmp_path)
    _declare(s, 'OUTSIDE SCOPE: RFT1-1 - lib')
    assert s.scope_declarations[-1]['status'] == 'rejected'


def test_old_observed_failure_is_superseded_by_a_passing_attempt(tmp_path):
    s, _ = _session(tmp_path)
    old = deepcopy(s._outcome.checks[-1]); old.update(task='t1', stage='before-review')
    s.review_checks.append(old)
    current = deepcopy(old); current.update(passed=True, attempt=2)
    current['receipts'] = [dict(id='check', status='passed', report=dict(state='parsed', failures=[], counts=dict(passed=1, failed=0, errors=0)))]
    s._outcome.checks.append(current)
    _declare(s, f'OUTSIDE SCOPE: {CASE} - app.py')
    assert s.scope_declarations[-1]['status'] == 'rejected', s.scope_declarations[-1]


def test_pass_then_new_same_named_failure_does_not_reuse_old_routing(tmp_path):
    s, project = _session(tmp_path)
    _declare(s, f'OUTSIDE SCOPE: {CASE} - app.py')
    assert s._scope_covers(s._active_spec, _result([CASE])) is not None
    old = deepcopy(s._outcome.checks[-1])
    passed = deepcopy(old); passed.update(passed=True, attempt=2, source='fixed-source')
    passed['receipts'] = [dict(id='check', status='passed', report=dict(state='parsed', failures=[], counts=dict(passed=1, failed=0, errors=0)))]
    s._outcome.checks.append(passed)
    # A later edit to an allowed test makes this node fail for a new reason.
    (project / 'tests/test_app.py').write_text('def test_add():\n    assert False, "new in-scope failure"\n')
    new = deepcopy(old); new.update(attempt=3, source=s._source_fingerprint())
    new['receipts'][0]['report']['failures'][0]['message'] = 'new in-scope failure'
    s._outcome.checks.append(new)
    assert s._scope_covers(s._active_spec, _result([CASE])) is None, 'stale declaration suppressed current repair'


def test_mixed_unknown_or_setup_failure_cannot_skip_gate(tmp_path):
    s, _ = _session(tmp_path)
    _declare(s, f'OUTSIDE SCOPE: {CASE} - app.py')
    for result in (_result([CASE, 'tests/other.py::test_new']), _result([CASE], status='timeout'), _result([CASE], when='setup')):
        assert s._scope_covers(s._active_spec, result) is None


def test_routing_keeps_open_owner_debt_and_does_not_expand_grant(tmp_path):
    s, _ = _session(tmp_path)
    original = deepcopy(s._active_spec.scope)
    _declare(s, 'OUTSIDE SCOPE: RFT1-1 - app.py, lib/util.py')
    assert s.review_findings[0]['route'] == 'scope'
    assert not s.review_findings[0]['closed']
    assert s._active_spec.scope == original
    assert not s._active_spec.scope.permits('app.py')
