"""Claude's envelope reports the seat model in ``usage`` and every model the CLI
used in ``modelUsage``. Scored attempt 1 (2026-09-15) carried a 2,817-token Haiku
row the run budget never saw. These pin: rows are summed once, never added to
the top-level figure; provenance names the extra rows; malformed rows are
reported as unknown, not zero; behaviour without ``modelUsage`` is unchanged."""
import json

from quadratus.cli_providers import _extract_claude_diagnostics, _extract_claude_usage
from quadratus.delegation import safe_diagnostics

FABLE = {"inputTokens": 66, "outputTokens": 2827, "cacheReadInputTokens": 23978,
         "cacheCreationInputTokens": 38694, "costUSD": 0.92}
HAIKU = {"inputTokens": 2801, "outputTokens": 16, "cacheReadInputTokens": 0,
         "cacheCreationInputTokens": 0, "costUSD": 0.0029}
USAGE = {"input_tokens": 66, "cache_read_input_tokens": 23978,
         "cache_creation_input_tokens": 38694, "output_tokens": 2827}


def _envelope(**extra):
    return json.dumps({"type": "result", "result": "ok", "usage": USAGE, **extra})


def test_attempt_one_envelope_counts_the_haiku_row_once():
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": HAIKU})
    assert _extract_claude_usage(out) == {"input_tokens": 62738 + 2801, "output_tokens": 2827 + 16}
    got = _extract_claude_diagnostics(out)
    assert got == {"auxiliary_models": ["claude-haiku-4-5-20251001"], "auxiliary_tokens": 2817}
    assert safe_diagnostics(got) == got


def test_no_model_usage_keeps_the_old_figure():
    assert _extract_claude_usage(_envelope()) == {"input_tokens": 62738, "output_tokens": 2827}
    assert _extract_claude_diagnostics(_envelope()) is None


def test_seat_only_rows_add_nothing():
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE})
    assert _extract_claude_usage(out) == {"input_tokens": 62738, "output_tokens": 2827}
    assert _extract_claude_diagnostics(out) is None


def test_a_malformed_row_is_unknown_not_zero():
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": {"inputTokens": "lots"}})
    # The seat's figure stays known; the unparseable row is reported as missing.
    assert _extract_claude_usage(out) == {"input_tokens": 62738, "output_tokens": 2827}
    assert _extract_claude_diagnostics(out) == {"auxiliary_usage": "unknown"}
    assert safe_diagnostics({"auxiliary_usage": "unknown"}) == {"auxiliary_usage": "unknown"}


def test_rows_that_do_not_match_the_seat_are_unattributed_excess():
    other = dict(FABLE, inputTokens=100)  # no row equals the top-level usage
    out = _envelope(modelUsage={"claude-fable-5-1": other, "claude-haiku-4-5-20251001": HAIKU})
    usage = _extract_claude_usage(out)
    assert usage == {"input_tokens": 62772 + 2801, "output_tokens": 2827 + 16}
    got = _extract_claude_diagnostics(out)
    assert got["auxiliary_usage"] == "unattributed"
    assert got["auxiliary_tokens"] == (62772 + 2801 + 2843) - (62738 + 2827)
    assert got["auxiliary_models"] == ["claude-fable-5-1", "claude-haiku-4-5-20251001"]


def test_rows_without_top_level_usage_are_still_known():
    out = json.dumps({"type": "result", "result": "ok",
                      "modelUsage": {"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": HAIKU}})
    assert _extract_claude_usage(out) == {"input_tokens": 62738 + 2801, "output_tokens": 2827 + 16}
    assert _extract_claude_diagnostics(out)["auxiliary_models"] == ["claude-fable-5-1", "claude-haiku-4-5-20251001"]


def test_nothing_reported_stays_unknown():
    assert _extract_claude_usage(json.dumps({"type": "result", "result": "ok"})) is None
    assert _extract_claude_usage("not json") is None


def test_whitelist_filters_provenance_like_tool_names():
    got = safe_diagnostics({"auxiliary_models": ["claude-haiku-4-5", "bad name", 3], "auxiliary_tokens": -1,
                            "auxiliary_usage": "guess"})
    assert got == {"auxiliary_models": ["claude-haiku-4-5"]}
