"""How a vendor CLI's failed call is read and recorded (2026-10-21).

Root's offline controls on f1d9ba2 found the boundary: a codex stream whose
earlier agent_message was followed by a turn.failed carrying a 429 came
back as that message, a success; a claude error envelope with a partial
result and ``errors`` lost its status; a grok ``stopReason: error`` with a
status in its error object lost it too. An error now dominates partial
text, a later completed turn is the client's own recovery and is read as
the success it is, and every vendor records the same bounded facts (status,
failure class) only from what it reported as an error. Prose in a
successful answer that mentions 429 or 503 records nothing.
"""
import json

import pytest

from quadratus.cli_providers import (
    FAILURE_CLASSES,
    _extract_claude_diagnostics,
    _extract_claude_result,
    _extract_codex_diagnostics,
    _extract_codex_result,
    _extract_grok_diagnostics,
    _extract_grok_result,
    failure_facts,
)
from quadratus.delegation import safe_diagnostics
from quadratus.providers import ProviderError
from quadratus.runtime import _looks_exhausted

MESSAGE = '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"Implemented the first half."}}'
FAILED_429 = '{"type":"turn.failed","error":{"message":"exceeded retry limit, last status: 429 Too Many Requests"}}'
COMPLETED = '{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":0,"output_tokens":5}}'


# -- codex ------------------------------------------------------------------


def test_a_failed_turn_after_a_progress_message_is_a_failure_with_the_text_as_evidence():
    stream = "\n".join(['{"type":"turn.started"}', MESSAGE, FAILED_429])
    with pytest.raises(ProviderError) as caught:
        _extract_codex_result(stream)
    assert "codex turn failed" in str(caught.value) and "429" in str(caught.value)
    assert caught.value.partial_text == "Implemented the first half."
    assert _extract_codex_diagnostics(stream) == {"stop_reason": "turn.failed", "status": 429,
                                                  "failure_class": "rate_limited"}


def test_a_completed_turn_after_a_failed_one_is_the_clients_recovery_and_a_real_success():
    stream = "\n".join(['{"type":"turn.started"}', FAILED_429, '{"type":"turn.started"}', MESSAGE, COMPLETED])
    assert _extract_codex_result(stream) == "Implemented the first half."
    assert _extract_codex_diagnostics(stream)["stop_reason"] == "turn.completed"
    assert "status" not in _extract_codex_diagnostics(stream)


def test_a_failed_turn_after_a_completed_one_is_still_the_last_word():
    stream = "\n".join([MESSAGE, COMPLETED, '{"type":"turn.started"}', FAILED_429])
    with pytest.raises(ProviderError):
        _extract_codex_result(stream)
    assert _extract_codex_diagnostics(stream)["stop_reason"] == "turn.failed"


@pytest.mark.parametrize("error", [
    {"status": 503},                      # a status and no message (root's review of ace40f8)
    {},                                   # nothing at all
    {"message": ""},                      # an empty message
    {"message": 12},                      # a malformed one
    "not an object",                      # a malformed error field
])
def test_an_explicit_failed_turn_with_no_usable_detail_still_fails(error):
    stream = "\n".join(['{"type":"turn.started"}', MESSAGE, json.dumps({"type": "turn.failed", "error": error})])
    with pytest.raises(ProviderError) as caught:
        _extract_codex_result(stream)
    assert str(caught.value).startswith("codex turn failed: ")
    assert caught.value.partial_text == "Implemented the first half."
    got = _extract_codex_diagnostics(stream)
    assert got["stop_reason"] == "turn.failed"
    assert (got.get("status"), got.get("failure_class")) == ((503, "unavailable") if error == {"status": 503} else (None, None))


def test_a_codex_answer_mentioning_a_status_records_no_failure():
    prose = '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"Handle 429 and 503 by retrying."}}'
    stream = "\n".join(['{"type":"turn.started"}', prose, COMPLETED])
    assert _extract_codex_result(stream) == "Handle 429 and 503 by retrying."
    got = _extract_codex_diagnostics(stream)
    assert got["stop_reason"] == "turn.completed" and "status" not in got and "failure_class" not in got


def test_a_codex_sign_in_rejection_is_auth_and_still_takes_the_vendor_out():
    stream = '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Incorrect API key"}}'
    with pytest.raises(ProviderError) as caught:
        _extract_codex_result(stream)
    assert caught.value.auth_invalid
    assert _extract_codex_diagnostics(stream) == {"stop_reason": "turn.failed", "status": 401, "failure_class": "auth"}


# -- claude -----------------------------------------------------------------


def _claude(**fields):
    base = {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
            "num_turns": 1, "result": "done",
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "output_tokens": 2}}
    return json.dumps({**base, **fields})


def test_a_claude_error_envelope_with_a_partial_result_keeps_its_status_and_is_no_answer():
    out = _claude(is_error=True, subtype="error_during_execution", stop_reason="error",
                  result="Partial draft of the reply", errors=["API Error: 429 temporary capacity"])
    with pytest.raises(ProviderError) as caught:
        _extract_claude_result(out)
    assert "claude reported an error" in str(caught.value)
    assert _extract_claude_diagnostics(out) == {"stop_reason": "error", "status": 429, "failure_class": "rate_limited"}


def test_a_claude_429_with_the_windows_wording_is_a_cap_not_a_rate_limit():
    out = _claude(is_error=True, api_error_status=429, result="You've reached your Fable limit.")
    assert _extract_claude_diagnostics(out) == {"stop_reason": "end_turn", "status": 429, "failure_class": "cap"}
    with pytest.raises(ProviderError) as caught:
        _extract_claude_result(out)
    assert _looks_exhausted(caught.value)


def test_a_claude_503_is_unavailable_and_an_unknown_error_is_unclassified():
    out = _claude(is_error=True, api_error_status=503, result="API Error: 503 overloaded")
    assert _extract_claude_diagnostics(out)["failure_class"] == "unavailable"
    out = _claude(is_error=True, result="something else went wrong")
    assert _extract_claude_diagnostics(out) == {"stop_reason": "end_turn"}


def test_a_claude_answer_mentioning_a_status_records_no_failure():
    out = _claude(result="The handler returns 429 on a burst and 503 when the pool is down.")
    assert _extract_claude_result(out).startswith("The handler returns 429")
    assert _extract_claude_diagnostics(out) is None


def test_a_claude_output_limit_stop_is_not_an_answer():
    out = _claude(stop_reason="max_tokens", result="def add(a, b):\n    ret")
    with pytest.raises(ProviderError) as caught:
        _extract_claude_result(out)
    assert caught.value.partial_text.startswith("def add")


# -- grok -------------------------------------------------------------------


def _grok(**fields):
    base = {"stopReason": "end_turn", "text": "done", "sessionId": "s1", "modelCalls": 1,
            "usage": {"inputTokens": 10, "outputTokens": 2}}
    return json.dumps({**base, **fields})


def test_a_grok_error_stop_with_a_status_in_its_error_object_keeps_it_and_is_no_answer():
    out = _grok(stopReason="error", text="I'll start by", error={"status": 503, "message": "upstream unavailable"})
    with pytest.raises(ProviderError) as caught:
        _extract_grok_result(out)
    assert "grok reported an error" in str(caught.value)
    got = _extract_grok_diagnostics(out)
    assert got["stop_reason"] == "error" and got["status"] == 503 and got["failure_class"] == "unavailable"


def test_a_grok_sign_in_refusal_is_auth_and_an_answer_mentioning_a_status_records_nothing():
    out = json.dumps({"type": "error", "message": "Not signed in. Run `grok login`."})
    got = _extract_grok_diagnostics(out)
    assert got == {"failure_class": "auth"}
    out = _grok(text="Return 503 while the pool is down, 429 on a burst.")
    assert _extract_grok_result(out).startswith("Return 503")
    got = _extract_grok_diagnostics(out)
    assert "status" not in got and "failure_class" not in got


# -- the shared facts and the ledger ------------------------------------------


@pytest.mark.parametrize("status, text, expect", [
    (429, "", {"status": 429, "failure_class": "rate_limited"}),
    (429, "You've reached your Fable limit", {"status": 429, "failure_class": "cap"}),
    (None, "last status: 503 Service Unavailable", {"status": 503, "failure_class": "unavailable"}),
    (None, "API Error: 429 temporary capacity", {"status": 429, "failure_class": "rate_limited"}),
    (403, "forbidden", {"status": 403, "failure_class": "auth"}),
    (None, "Not signed in", {"failure_class": "auth"}),
    (None, "retry after 30 seconds", {}),
    (None, "stream disconnected", {}),
    (200, "", {"status": 200}),
    (None, "HTTP 503 from upstream", {"status": 503, "failure_class": "unavailable"}),
    (None, "status code 502", {"status": 502, "failure_class": "unavailable"}),
    (None, "cannot open file429.py", {}),               # a number in prose is not a status
    (None, "failed at line 503 of the transcript", {}),
    (None, "error 4290 widgets", {}),
    (None, "the 429 handler is missing", {}),
])
def test_failure_facts_are_bounded_and_never_inferred(status, text, expect):
    got = failure_facts(status, text)
    assert got == expect
    assert "retry_after" not in got and set(got) <= {"status", "failure_class"}


def test_the_ledger_keeps_only_a_known_failure_class():
    for value in FAILURE_CLASSES:
        assert safe_diagnostics({"failure_class": value})["failure_class"] == value
    for value in ("timeout", "", 3, "cap; drop table", None):
        assert "failure_class" not in safe_diagnostics({"failure_class": value})
