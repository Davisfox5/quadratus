import json

import pytest

from quadratus.cli_providers import _extract_codex_diagnostics, _extract_codex_usage
from tools.native_call_diagnostics import transport_facts


def stream(*events):
    return '\n'.join(json.dumps(e) for e in events)


def done(i=100, o=10, **extra):
    return {'type': 'turn.completed', 'usage': {'input_tokens': i, 'output_tokens': o, **extra}}


def test_multiple_completions_are_one_cumulative_thread_total_never_summed():
    """Upstream 0.163 emits the thread's running total on every
    turn.completed (usage_from_last_total): the last snapshot is the
    account; summing 100 and 200 would double-count."""
    data = stream(done(100, 10, cached_input_tokens=90),
                  {'type': 'item.completed', 'usage': {'input_tokens': 999999}},
                  done(200, 20, cached_input_tokens=150))
    assert _extract_codex_usage(data) == {'input_tokens': 200, 'output_tokens': 20}
    facts = _extract_codex_diagnostics(data)
    assert facts['cached_input_tokens'] == 150 and facts['completed_turns'] == 2
    assert facts['usage_completeness'] == 'complete'


def test_a_repeated_total_charges_nothing_extra():
    data = stream(done(200, 20, cached_input_tokens=150), done(200, 20, cached_input_tokens=150))
    assert _extract_codex_usage(data) == {'input_tokens': 200, 'output_tokens': 20}
    assert _extract_codex_diagnostics(data)['cached_input_tokens'] == 150


@pytest.mark.parametrize('later', [done(150, 25), done(250, 15), done(250, 25, cached_input_tokens=100)])
def test_a_falling_cumulative_total_is_unresolved_with_the_last_valid_snapshot_as_lower_bound(later):
    data = stream(done(200, 20, cached_input_tokens=150), later)
    assert _extract_codex_usage(data) is None
    facts = _extract_codex_diagnostics(data)
    assert facts['usage_completeness'] == 'unresolved' and facts['completed_turns'] == 1
    assert facts['usage_lower_bound'] == {'input_tokens': 200, 'output_tokens': 20,
                                          'usage_source': 'last cumulative thread snapshot; incomplete call'}


def test_a_second_thread_in_one_stream_is_not_the_same_cumulative_account():
    data = stream({'type': 'thread.started', 'thread_id': 'a'}, done(100, 10),
                  {'type': 'thread.started', 'thread_id': 'b'}, done(300, 30))
    assert _extract_codex_usage(data) is None
    facts = _extract_codex_diagnostics(data)
    assert facts['usage_completeness'] == 'unresolved' and facts['completed_turns'] == 2
    assert facts['usage_lower_bound']['input_tokens'] == 300, 'the last valid snapshot, observed, never a sum'
    one = stream({'type': 'thread.started', 'thread_id': 'a'}, done(100, 10), done(300, 30))
    assert _extract_codex_usage(one) == {'input_tokens': 300, 'output_tokens': 30}


@pytest.mark.parametrize('tail', [
    {'type': 'turn.started'}, {'type': 'turn.failed', 'error': {'message': 'HTTP 429'}},
    {'type': 'turn.completed'}, done(-1), done(True), done('12'),
    done(10, 2, cached_input_tokens=11),
])
def test_partial_or_invalid_call_never_spends_only_the_good_prefix(tail):
    data = stream(done(), tail)
    assert _extract_codex_usage(data) is None
    facts = _extract_codex_diagnostics(data)
    assert facts['usage_completeness'] == 'unresolved'
    assert facts['usage_lower_bound']['input_tokens'] == 100


def test_reported_zero_is_distinct_from_absent_and_malformed():
    assert _extract_codex_usage(stream(done(0, 0))) == {'input_tokens': 0, 'output_tokens': 0}
    assert _extract_codex_usage(stream({'type': 'turn.started'})) is None
    assert _extract_codex_usage(stream(done()) + '\n{"type":') is None


def test_transport_keeps_only_allowlisted_facts_not_headers_errors_or_prompts():
    raw = ('ordinary stderr\n'
           '2026-10-21T12:00:00.000Z INFO codex_otel.trace_safe: '
           'event.name="codex.api_request" duration_ms=45 http.response.status_code=429 '
           'attempt=1 error.message="Bearer SECRET" user.email="private@example.com"\n'
           '2026-10-21T12:00:01.000Z WARN codex_core::responses_retry: '
           'retries=1 max_retries=5 sampling_error="SECRET" '
           'stream disconnected - retrying sampling request (1/5 in 2s)...\n')
    facts, clean = transport_facts(raw)
    assert clean == 'ordinary stderr\n'
    assert facts['events'][0]['http.response.status_code'] == 429
    assert facts['events'][1]['reported_backoff_seconds'] == 2
    assert 'SECRET' not in json.dumps(facts) and 'private@' not in json.dumps(facts)
    assert facts['usage_total'] is None


def test_request_id_ttft_and_tool_token_count_are_kept_in_bounded_form_only():
    line = ('INFO codex_otel.trace_safe: event.name="codex.sse_event" event.kind="response.completed" '
            'auth.request_id="req_01HXYZabc-123:7" ttft_ms=812 tool_token_count=1234 input_token_count=1000 '
            'output_token_count=200 auth.account_id="acct_SECRET" http.request.header.authorization="Bearer X" '
            'url="https://chatgpt.com/backend-api/codex/responses"\n')
    facts, clean = transport_facts(line)
    (row,) = facts['events']
    assert row['request_id'] == 'req_01HXYZabc-123:7' and row['ttft_ms'] == 812 and row['tool_token_count'] == 1234
    assert row['input_token_count'] == 1000 and row['output_token_count'] == 200
    assert facts['usage_total'] is None, 'tool_token_count is the upstream total, never summed into a bill'
    dumped = json.dumps(facts)
    assert 'acct_SECRET' not in dumped and 'Bearer' not in dumped and 'chatgpt.com' not in dumped
    assert 'account_id' not in dumped and 'url' not in dumped and 'authorization' not in dumped


@pytest.mark.parametrize('value', ['', 'abc', 'x' * 129, 'req 01', 'req/01', 'Bearer=tok', '"quoted"', 'req\x1b[0m'])
def test_a_request_id_outside_the_opaque_shape_is_dropped(value):
    line = f'INFO codex_otel.trace_safe: event.name="codex.api_request" auth.request_id="{value}" attempt=1\n'
    facts, _ = transport_facts(line)
    assert 'request_id' not in facts['events'][0] and facts['events'][0]['attempt'] == 1


def test_hundreds_of_deltas_never_crowd_out_the_completion_row():
    delta = ('INFO codex_otel.trace_safe: event.name="codex.sse_event" event.kind="response.output_text.delta"\n')
    reasoning = ('INFO codex_otel.trace_safe: event.name="codex.sse_event" event.kind="response.reasoning_text.delta"\n')
    progress = ('INFO codex_otel.trace_safe: event.name="codex.websocket_event" event.kind="response.in_progress"\n')
    completed = ('INFO codex_otel.trace_safe: event.name="codex.sse_event" event.kind="response.completed" '
                 'input_token_count=3 output_token_count=1 tool_token_count=4 ttft_ms=90\n')
    connect = ('INFO codex_otel.trace_safe: event.name="codex.websocket_connect" http.response.status_code=101 '
               'auth.connection_reused=false\n')
    failed_delta = ('INFO codex_otel.trace_safe: event.name="codex.sse_event" event.kind="response.output_text.delta" '
                    'error.message="stream reset"\n')
    facts, clean = transport_facts(connect + delta * 400 + reasoning * 100 + progress * 50 + failed_delta + completed)
    kinds = [(e["event"], e.get("kind")) for e in facts["events"]]
    assert kinds == [("codex.websocket_connect", None), ("codex.sse_event", "response.output_text.delta"),
                     ("codex.sse_event", "response.completed")]
    assert facts["events"][1]["error_reported"] is True, "a delta carrying an error is kept"
    assert facts["events"][2]["tool_token_count"] == 4 and facts["events"][2]["ttft_ms"] == 90
    assert facts["routine_rows_omitted"] == 550 and facts["dropped_trace_lines"] == 0
    assert facts["selected_trace_lines"] == 553 and facts["usage_total"] is None and clean == ""


def test_silent_transport_does_not_claim_zero_requests_or_usage():
    facts, clean = transport_facts('Reading prompt from stdin...\n')
    assert facts['events'] == [] and facts['usage_total'] is None
    assert clean == 'Reading prompt from stdin...\n'


def test_transport_output_bounded_without_turning_truncation_into_complete_usage():
    line = 'INFO codex_otel.trace_safe: event.name="codex.sse_event" event.kind="response.completed" input_token_count=3 output_token_count=1\n'
    facts, clean = transport_facts(line * 300)
    assert len(facts['events']) == 256 and facts['dropped_trace_lines'] == 44
    assert facts['usage_total'] is None and clean == ''
