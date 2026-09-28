"""Make the re-read cost visible, and stop paying it inside the expensive loop.

Attempt 9 reported 532,795 tokens. 402,816 of those -- 76% -- were text the
models were being handed again, because an agent re-sends its whole
conversation on every step. Nothing in the run record showed that; finding it
meant opening the private vendor envelopes.

Two consequences, both tested here. The ledger now carries the split and the
vendor's own price for each call, so the next person does not have to dig. And
the lead is told what exploring actually costs it, because the cheap fix is not
to explore inside a loop whose cost grows with the square of its length -- a
worker answers in one step at a flat price.
"""
import json

from quadratus.cli_providers import (
    _extract_codex_diagnostics,
    _extract_grok_diagnostics,
    _reread_and_cost,
)
from quadratus.delegation import safe_diagnostics
from quadratus.session import _READ_BEFORE_YOU_EXPLORE

# -- the facts a call now reports ------------------------------------------

def test_the_split_counts_every_spelling_of_a_cache_read():
    """Three vendors, three names for the same thing."""
    assert _reread_and_cost({'cache_read_input_tokens': 100,
                             'cache_creation_input_tokens': 5}) == {'cached_input_tokens': 105}
    assert _reread_and_cost({'cached_input_tokens': 42}) == {'cached_input_tokens': 42}


def test_a_call_with_no_re_reads_says_nothing_rather_than_zero():
    """Absent is 'not reported'; zero would be a claim."""
    assert _reread_and_cost({'input_tokens': 10, 'output_tokens': 2}) == {}
    assert _reread_and_cost(None) == {}


def test_the_vendors_own_price_is_carried_beside_our_counterfactual():
    facts = _reread_and_cost({'cache_read_input_tokens': 1}, 0.1293)
    assert facts['vendor_cost_usd'] == 0.1293


def test_a_nonsense_price_is_dropped():
    for bad in (True, 'free', float('nan'), -1):
        assert 'vendor_cost_usd' not in _reread_and_cost({}, bad)


def test_the_real_grok_envelope_reports_its_own_split():
    """Attempt 9's task-1 lead, as the vendor actually returned it."""
    envelope = json.dumps({
        'stopReason': 'end_turn', 'text': 'done', 'modelCalls': 9,
        'usage': {'input_tokens': 70310, 'cache_read_input_tokens': 158336,
                  'cache_creation_input_tokens': 0, 'output_tokens': 13054},
        'total_cost_usd': 0.10135808,
    })
    facts = _extract_grok_diagnostics(envelope)
    assert facts['cached_input_tokens'] == 158336
    assert facts['model_calls'] == 9
    assert round(facts['vendor_cost_usd'], 5) == 0.10136


def test_codex_reports_at_all_now():
    """This vendor had no diagnostics extractor, and it holds the orchestrator
    seat whenever Fable is out -- which by attempt 9 was every run."""
    stream = '\n'.join([
        json.dumps({'type': 'item.completed', 'item': {'id': 'x'}}),
        json.dumps({'type': 'turn.completed',
                    'usage': {'input_tokens': 73000, 'cached_input_tokens': 63360,
                              'output_tokens': 642}}),
    ])
    facts = _extract_codex_diagnostics(stream)
    assert facts == {'stop_reason': 'turn.completed', 'cached_input_tokens': 63360}


def test_nothing_parseable_reports_nothing():
    assert _extract_codex_diagnostics('not json at all') is None


# -- and they survive the durable boundary ---------------------------------

def test_the_ledger_keeps_the_split_and_the_price():
    kept = safe_diagnostics({'stop_reason': 'end_turn', 'cached_input_tokens': 158336,
                             'vendor_cost_usd': 0.10135808})
    assert kept['cached_input_tokens'] == 158336
    assert kept['vendor_cost_usd'] == 0.101358


def test_the_ledger_still_refuses_junk_at_that_boundary():
    kept = safe_diagnostics({'cached_input_tokens': 'lots', 'vendor_cost_usd': 'free'})
    assert 'cached_input_tokens' not in kept and 'vendor_cost_usd' not in kept
    assert safe_diagnostics({'cached_input_tokens': -5}) == {}


# -- what the lead is told about the cost of looking around -----------------

def test_the_lead_is_given_the_measurement_not_an_instruction_to_be_frugal():
    """A lead told merely to 'be efficient' has no way to weigh the choice.

    The numbers are the argument: its own loop is quadratic, a worker is flat.
    """
    assert '241,700' in _READ_BEFORE_YOU_EXPLORE and '450,602' in _READ_BEFORE_YOU_EXPLORE
    assert '7,337' in _READ_BEFORE_YOU_EXPLORE
    assert 'square' in _READ_BEFORE_YOU_EXPLORE


def test_the_lead_keeps_reading_the_code_it_is_editing():
    """The instruction must not push a lead into editing text it has not seen.

    Delegation is for knowledge *about* the code; the exact text of the part
    being changed is the lead's own to read.
    """
    assert 'Do your own reading for the part you are actually editing' in _READ_BEFORE_YOU_EXPLORE
