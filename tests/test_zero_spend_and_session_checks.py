"""Attempt 7: a run ended by an expired login and a budget latch that followed it.

Two separate faults, both cheap to have caught. Grok's subscription session had
expired and nothing asked, so the run reached a lead 82,051 tokens in before
the CLI answered "Not signed in" in 0.37 seconds. That refusal reported no
usage, which latched the run budget as ``unknown_usage``, which then refused
the recovery on Sol that the engine had *already selected*.

A call the vendor refused before contacting the service spent nothing, and
that is a different fact from "this call's spend is unknown".
"""
import json

import pytest

from quadratus.cli_providers import (
    CLI_SPECS,
    _extract_grok_usage,
    _refused_before_the_turn,
)
from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits

#: The envelope grok really returned, from attempt 7's preserved call-003.
ATTEMPT_7 = ('{"type":"error","message":"Not signed in. To authenticate without a browser, '
             'run:\\n  grok login --device-code\\n\\nAlternatively, set the XAI_API_KEY '
             'environment variable or run `grok login` on a machine with a browser."}')


# -- what counts as never having started -----------------------------------

def test_the_real_attempt_7_refusal_reads_as_zero_not_unknown():
    assert _extract_grok_usage(ATTEMPT_7) == {'input_tokens': 0, 'output_tokens': 0}


@pytest.mark.parametrize('payload', [
    {'type': 'error', 'message': 'Not signed in.'},
    {'error': 'unknown model id'},
])
def test_a_refusal_with_nothing_behind_it_is_a_refusal(payload):
    assert _refused_before_the_turn(payload) is True


@pytest.mark.parametrize('payload', [
    pytest.param({'type': 'error', 'message': 'x', 'sessionId': 's1'}, id='a session opened'),
    pytest.param({'type': 'error', 'message': 'x', 'usage': {'input_tokens': 5}}, id='usage'),
    pytest.param({'type': 'error', 'message': 'x', 'text': 'I was saying'}, id='narration'),
    pytest.param({'type': 'error', 'message': 'x', 'stopReason': 'cancelled'}, id='a turn ended'),
    pytest.param({'stopReason': 'end_turn', 'text': 'hi'}, id='not an error at all'),
    pytest.param('not a dict', id='unparseable'),
])
def test_anything_that_might_have_started_stays_unknown(payload):
    """The rule errs towards unknown, because unknown is the safe latch."""
    assert _refused_before_the_turn(payload) is False


def test_a_turn_that_began_and_then_failed_still_reports_unknown_usage():
    assert _extract_grok_usage(json.dumps({'type': 'error', 'message': 'x',
                                           'sessionId': 's1'})) is None


def test_a_completed_turn_is_unaffected():
    assert _extract_grok_usage(json.dumps({
        'stopReason': 'end_turn', 'text': 'hi',
        'usage': {'input_tokens': 10, 'output_tokens': 2}})) == {'input_tokens': 10,
                                                                 'output_tokens': 2}


# -- and what that means for the run ---------------------------------------

def test_a_no_spend_refusal_leaves_the_budget_open_for_the_recovery(tmp_path):
    """The attempt-7 sequence, end to end: the recovery must still be reachable."""
    budget = RunBudget(RunLimits(), path=tmp_path / 'budget.json')
    ticket, _ = budget.reserve()
    budget.finish(ticket, _extract_grok_usage(ATTEMPT_7))
    assert budget.snapshot()['stop_reason'] == ''
    recovery, _ = budget.reserve()            # Sol, which attempt 7 never reached
    budget.finish(recovery, {'input_tokens': 120, 'output_tokens': 40})
    snapshot = budget.snapshot()
    assert snapshot['reported_tokens'] == 160
    assert snapshot['unknown_usage_attempts'] == 0


def test_a_failure_that_might_have_spent_still_stops_the_run(tmp_path):
    """The guard this repair must not weaken.

    A call whose spend is genuinely unknown -- a timeout after partial work,
    say -- latches the budget, and that is correct: the alternative is a run
    that keeps spending against a total nobody can state.
    """
    budget = RunBudget(RunLimits(), path=tmp_path / 'budget.json')
    ticket, _ = budget.reserve()
    with pytest.raises(RunBudgetExceeded):
        budget.finish(ticket, None)
    assert budget.snapshot()['stop_reason'] == 'unknown_usage'


# -- the check that would have caught it before a token was spent ----------

def test_every_vendor_that_can_report_its_session_declares_the_check():
    declared = {v: bool(s.auth_check_args) for v, s in CLI_SPECS.items()}
    assert declared == {'claude': True, 'openai': True, 'grok': True}


def test_the_claude_readout_is_recognised_both_ways():
    """``claude auth status`` reports loggedIn; observed true on 2026-09-22.
    Before it was declared, the preflight called claude's sign-in not
    applicable, which reads as untested rather than untestable."""
    import re
    spec = CLI_SPECS['claude']
    assert spec.auth_check_args == ['auth', 'status']
    assert re.search(spec.auth_ok_pattern, '{"loggedIn": true, "subscriptionType": "max"}')
    assert re.search(spec.auth_failure_pattern, '{"loggedIn": false}')
    assert not re.search(spec.auth_ok_pattern, '{"loggedIn": false}')


def test_the_grok_readout_is_recognised_by_its_failure_wording():
    """`grok models` prints a dead session and exits 0, so the exit code alone
    decides nothing -- which is exactly how attempt 7 got as far as it did."""
    import re
    spec = CLI_SPECS['grok']
    assert spec.auth_check_args == ['models']
    assert re.search(spec.auth_failure_pattern, 'You are not authenticated.\n')
    assert re.search(spec.auth_failure_pattern, 'Error: Not signed in. To authenticate')
    assert not re.search(spec.auth_failure_pattern, 'Default model: grok-4.6')
    assert spec.auth_ok_pattern == '', 'grok prints nothing distinctive for a live session'


def test_the_codex_readout_is_recognised_positively():
    import re
    spec = CLI_SPECS['openai']
    assert spec.auth_check_args == ['login', 'status']
    assert re.search(spec.auth_ok_pattern, 'Logged in using ChatGPT')


def _preflight():
    import importlib.util
    spec = importlib.util.spec_from_file_location('pf', 'tools/acceptance/preflight.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('readout, expected', [
    ('You are not authenticated.', False),
    ('Default model: grok-4.6', True),
])
def test_the_preflight_reads_a_session_out_of_the_readout(readout, expected):
    module = _preflight()

    class _Spec:
        binary = 'printf'
        auth_check_args = [readout]
        auth_ok_pattern = ''
        auth_failure_pattern = r'(?i)not\s+authenticated|not\s+signed\s+in'

    result = module._auth_check(_Spec())
    assert result['applicable'] and result['ok'] is expected
    assert result['proof'] == 'known-failure wording only'


def test_a_vendor_with_no_readout_is_not_reported_as_signed_in():
    module = _preflight()

    class _Spec:
        binary = 'sh'
        auth_check_args = []
        auth_ok_pattern = ''
        auth_failure_pattern = ''

    result = module._auth_check(_Spec())
    assert result['applicable'] is False
    assert 'nothing here establishes that it is signed in' in result['detail']
