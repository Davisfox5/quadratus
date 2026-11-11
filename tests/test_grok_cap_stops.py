"""Grok's stop vocabulary at the extractor (recovery handoff repair, 2026-10-25).

The installed grok CLI (1.0.62) stops an agent loop with ``max_turn_requests``;
proof-workflow-01 t4's second revision ended there and was read as a failed,
unmerged task. That stop is the typed cap now, with the envelope's partial
text and turn count kept and its usage read exactly. Everything else keeps
its own class: an explicit decline is a refusal, a bare ``cancelled`` is the
cap only by the configured count, a stop this code has never seen is a
failure, and a cap stop that also carries an ``error`` is a failure, never
a cap. No provider is run: the envelopes are the fixtures.
"""
import json

import pytest

from quadratus import cli_providers
from quadratus.cli_providers import _extract_grok_result, _extract_grok_usage
from quadratus.providers import ProviderError, ProviderRefusal, TurnLimitReached

USAGE = {"input_tokens": 236144, "output_tokens": 7341}
NARRATION = ("The blocking finding is the argparse failure path: non-integer IDs still exit with "
             "plain-text usage instead of JSON. I'll read the CLI ")


def envelope(stop, *, text=NARRATION, num_turns=8, error=None, usage=USAGE):
    body = {"text": text, "stopReason": stop, "num_turns": num_turns, "sessionId": "s-1"}
    if usage is not None:
        body["usage"] = usage
    if error is not None:
        body["error"] = error
    return json.dumps(body)


def test_max_turn_requests_is_the_typed_cap_with_its_partial_text_and_turns():
    with pytest.raises(TurnLimitReached) as caught:
        _extract_grok_result(envelope("max_turn_requests"))
    assert caught.value.partial_text == NARRATION and caught.value.turns == 8
    assert "max_turn_requests" in str(caught.value)


def test_the_older_max_turns_spelling_is_still_the_cap():
    with pytest.raises(TurnLimitReached):
        _extract_grok_result(envelope("max_turns"))


def test_the_cap_envelope_reports_its_exact_usage():
    assert _extract_grok_usage(envelope("max_turn_requests")) == USAGE
    # No usage key: unknown, never zero.
    assert _extract_grok_usage(envelope("max_turn_requests", usage=None)) is None


@pytest.mark.parametrize("error", ["permission denied: write tickets/cli.py", "authentication required"])
def test_a_cap_stop_carrying_an_error_is_a_failure_not_a_cap(error):
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(envelope("max_turn_requests", error=error))
    assert not isinstance(caught.value, TurnLimitReached)
    assert error in str(caught.value)


@pytest.mark.parametrize("stop", ["refusal", "content_filter"])
def test_an_explicit_decline_is_a_refusal_whatever_the_count(stop):
    with pytest.raises(ProviderRefusal):
        _extract_grok_result(envelope(stop, num_turns=8))


@pytest.mark.parametrize("stop", ["cancelled", "future_unknown", "max_turns_reached", "max_tokens"])
def test_other_incomplete_stops_are_failures_at_the_extractor(stop):
    """``max_turns_reached`` is an event name in the binary, not a stop reason;
    read as a stop it is unknown and fails like any other unknown stop."""
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(envelope(stop))
    assert not isinstance(caught.value, (TurnLimitReached, ProviderRefusal))


def _provider(max_turns):
    provider = cli_providers.CLIProvider.__new__(cli_providers.CLIProvider)
    provider.spec = cli_providers.GROK_SPEC
    provider.max_turns = max_turns
    provider.summary_only = False
    return provider


def test_cancelled_at_the_configured_count_is_the_cap_and_below_it_is_a_failure():
    with pytest.raises(TurnLimitReached) as capped:
        _provider(8)._extract(envelope("cancelled", num_turns=8))
    assert capped.value.turns == 8
    with pytest.raises(ProviderError) as failed:
        _provider(14)._extract(envelope("cancelled", num_turns=8))
    assert not isinstance(failed.value, TurnLimitReached)


def test_the_count_rule_never_promotes_an_unknown_stop_or_an_error():
    for stdout in (envelope("future_unknown", num_turns=8),
                   envelope("cancelled", num_turns=8, error="permission denied")):
        with pytest.raises(ProviderError) as caught:
            _provider(8)._extract(stdout)
        assert not isinstance(caught.value, TurnLimitReached)


def test_max_turn_requests_is_the_cap_under_a_provider_at_any_count():
    """The typed stop needs no count: 8 turns under a cap of 14 is the
    vendor saying it stopped, which the count rule alone would have read as
    a failure (the terminal's exact shape)."""
    with pytest.raises(TurnLimitReached) as caught:
        _provider(14)._extract(envelope("max_turn_requests", num_turns=8))
    assert caught.value.turns == 8 and caught.value.partial_text == NARRATION
