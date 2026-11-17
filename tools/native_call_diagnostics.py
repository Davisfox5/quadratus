"""Local, allowlisted native transport facts; never a replacement usage bill.

The selected Codex tracing targets expose request status, response-completion
counts and retry waits without enabling protocol-frame or prompt tracing.
Free-text errors, headers, identities and content are never retained here.
"""
from __future__ import annotations

import re

LOG_FILTER = "error,codex_otel.trace_safe=info,codex_core::responses_retry=warn"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FIELDS = re.compile(r'([A-Za-z_][\w.]*)=(?:"([^"\n]*)"|([^\s]+))')
_EVENTS = {"codex.api_request", "codex.websocket_connect", "codex.websocket_request",
           "codex.sse_event", "codex.websocket_event"}
_NUMBERS = {"duration_ms", "http.response.status_code", "attempt", "input_token_count",
            "output_token_count", "cached_token_count", "cache_write_token_count",
            "reasoning_token_count", "total_token_count", "retries", "max_retries",
            # Upstream session_telemetry.rs on response.completed (0.163): time to
            # first token, and tool_token_count, its spelling of usage.total_tokens
            # (never added to input plus output).
            "ttft_ms", "tool_token_count"}
_BOOLS = {"success", "auth.connection_reused"}
#: The provider's request correlation id (auth.request_id upstream): an opaque
#: id, not a credential, kept only in this bounded shape so a later timeout can
#: be traced to one provider request. No endpoint, header or account field.
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,127}")
#: Routine stream progress (text, reasoning and other deltas, in-progress
#: notices, added parts, pings): counted, never kept, so a long response
#: cannot spend the row cap before its completion, failure or usage row.
_ROUTINE_KIND = re.compile(r"(?:response\.[a-z_.]*(?:delta|in_progress|added|done)|ping)")


def transport_facts(stderr: str) -> tuple[dict, str]:
    """Return bounded diagnostic rows and stderr with selected trace lines removed.

    These are observed events, not proof that all backend requests/usage have
    been reported. No event is never equated with zero requests or zero tokens.
    """
    rows, ordinary, seen, dropped, routine = [], [], 0, 0, 0
    for raw in stderr.splitlines(keepends=True):
        line = _ANSI.sub("", raw)
        selected = ("codex_otel.trace_safe" in line or "codex_core::responses_retry" in line)
        if not selected:
            ordinary.append(raw)
            continue
        seen += 1
        if len(line) > 32_768:
            dropped += 1
            continue
        fields = {m[1]: m[2] if m[2] is not None else m[3] for m in _FIELDS.finditer(line)}
        event = fields.get("event.name")
        retry = "codex_core::responses_retry" in line
        if event not in _EVENTS and not retry:
            continue
        kind = fields.get("event.kind", "")
        if (event in ("codex.sse_event", "codex.websocket_event") and _ROUTINE_KIND.fullmatch(kind)
                and "error.message" not in fields and "http.response.status_code" not in fields):
            routine += 1
            continue
        if len(rows) >= 256:
            dropped += 1
            continue
        row = {"event": "native.retry_wait" if retry else event}
        stamp = re.match(r"(\d{4}-\d\d-\d\dT[0-9:.]+Z)\s", line)
        if stamp:
            row["at"] = stamp[1]
        for key in _NUMBERS:
            value = fields.get(key, "")
            if re.fullmatch(r"\d{1,18}", value):
                row[key] = int(value)
        for key in _BOOLS:
            if fields.get(key) in ("true", "false"):
                row[key] = fields[key] == "true"
        request_id = fields.get("auth.request_id", "")
        if _REQUEST_ID.fullmatch(request_id):
            row["request_id"] = request_id
        if re.fullmatch(r"(?:response\.[a-z_.]{1,80}|error|ping|close)", kind):
            row["kind"] = kind
        # Class only: a provider error may contain a URL, credential or prompt.
        if "error.message" in fields or retry:
            row["error_reported"] = True
            from quadratus.cli_providers import failure_facts
            # Extract class/status only using the same bounded product parser.
            row.update(failure_facts(None, fields.get("error.message")
                                     or fields.get("sampling_error") or ""))
        if retry:
            wait = re.search(r" in (\d+(?:\.\d+)?)(ms|s)\)", line)
            if wait:
                row["reported_backoff_seconds"] = float(wait[1]) / (1000 if wait[2] == "ms" else 1)
        rows.append(row)
    return {"schema": 1, "scope": "observed native transport events; not complete backend accounting",
            "selected_trace_lines": seen, "dropped_trace_lines": dropped, "routine_rows_omitted": routine,
            "events": rows, "usage_total": None}, "".join(ordinary)
