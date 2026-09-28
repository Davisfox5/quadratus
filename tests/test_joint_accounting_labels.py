"""Arithmetic totals do not establish vendor parent/child non-overlap."""

from quadratus.delegation import DelegationLedger, InvocationEvent, NativeChild, reconcile


def test_combined_report_is_explicitly_conditional_and_auxiliary_scope_is_named():
    parent = InvocationEvent('t1', 'lead', invoked=True, outcome='ok',
                             input_tokens=100, output_tokens=10, session_id='parent')
    child = NativeChild('child', parent_session_id='parent', input_tokens=20, output_tokens=5)
    totals = reconcile([parent], [child])
    assert totals['combined_reported_tokens'] == 135
    assert totals['parent_child_overlap'] == 'unverified'
    assert totals['known_minimum_tokens'] == 135  # legacy compatibility, explicitly qualified
    assert totals['auxiliary_tokens'] == 0
    assert 'InvocationEvent' in totals['auxiliary_tokens_scope']
    ledger = DelegationLedger(events=[parent], native_children={'child': child})
    report = ledger.render_report()
    assert 'Known minimum:' not in report
    assert 'conditional' in report.lower() and 'overlap' in report.lower()


def test_report_does_not_add_a_native_session_already_counted_as_controlled():
    event = InvocationEvent('t1', 'worker', invoked=True, outcome='ok',
                            input_tokens=20, output_tokens=5, session_id='same')
    child = NativeChild('same', input_tokens=20, output_tokens=5)
    ledger = DelegationLedger(events=[event], native_children={'same': child})
    report = ledger.render_report()
    assert 'Quadratus-dispatched: 25 tokens' in report
    assert '50 tokens' not in report
    assert reconcile([event], [child])['parent_child_overlap'] == 'not_applicable'
