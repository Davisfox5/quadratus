"""Claude's envelope reports the seat model in ``usage`` and every model the CLI
used in ``modelUsage``. Scored attempt 1 (2026-09-15) carried a 2,817-token Haiku
row the run budget never saw. These pin: rows are summed once, never added to
the top-level figure; provenance names the extra rows; malformed rows are
reported as unknown, not zero; behaviour without ``modelUsage`` is unchanged."""
import json

import pytest

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


def test_a_malformed_row_makes_the_whole_figure_unknown():
    """The budget must stop rather than continue on a count known to be partial;
    the seat's own known figure survives in the diagnostics, not as the total."""
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": {"inputTokens": "lots"}})
    assert _extract_claude_usage(out) is None
    got = _extract_claude_diagnostics(out)
    assert got == {"auxiliary_usage": "unknown", "seat_tokens": 62738 + 2827}
    assert safe_diagnostics(got) == got


@pytest.mark.parametrize("bad", [
    {"inputTokens": 2801, "outputTokens": -1},          # negative
    {"inputTokens": 2801.0, "outputTokens": 16},        # float
    {"inputTokens": True, "outputTokens": 16},          # bool is not a count
    {"outputTokens": 16},                               # required field missing
    "not a row",                                        # not a mapping
])
def test_fields_are_validated_never_coerced(bad):
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": bad})
    assert _extract_claude_usage(out) is None
    assert _extract_claude_diagnostics(out)["auxiliary_usage"] == "unknown"


def test_rows_summing_below_the_seat_are_partial_and_unknown():
    out = _envelope(modelUsage={"claude-haiku-4-5-20251001": HAIKU})  # the seat's own row is missing
    assert _extract_claude_usage(out) is None
    assert _extract_claude_diagnostics(out) == {"auxiliary_usage": "unknown", "seat_tokens": 65565}


def test_a_tie_for_the_seat_total_is_not_an_identity():
    twin = dict(FABLE)
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-fable-5-1-twin": twin,
                                "claude-haiku-4-5-20251001": HAIKU})
    got = _extract_claude_diagnostics(out)
    assert got["auxiliary_usage"] == "unattributed"
    assert got["auxiliary_models"] == ["claude-fable-5-1", "claude-fable-5-1-twin", "claude-haiku-4-5-20251001"]
    assert got["auxiliary_tokens"] == (65565 * 2 + 2817) - 65565


def test_the_run_budget_counts_auxiliary_rows_and_stops_on_malformed_ones(monkeypatch, tmp_path):
    from quadratus import cli_providers
    from quadratus.cli_providers import ClaudeCLIProvider
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    envelopes = iter([
        _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": HAIKU}),
        _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": {"inputTokens": "lots"}}),
    ])

    class _Done:
        returncode, stderr = 0, ""

        def __init__(self):
            self.stdout = next(envelopes)
    monkeypatch.setattr(cli_providers, "_launch", lambda *a, **k: _Done())
    provider = ClaudeCLIProvider(model="opus", workdir=tmp_path, max_retries=1)
    budget = RunBudget(RunLimits(), path=tmp_path / "budget.json")
    provider.run_budget = budget
    assert provider.generate("first") == "ok"
    snap = budget.snapshot()
    assert (snap["input_tokens"], snap["output_tokens"]) == (62738 + 2801, 2827 + 16)
    with pytest.raises(RunBudgetExceeded, match="unknown_usage"):
        provider.generate("second")
    assert budget.snapshot()["stop_reason"] == "unknown_usage"
    assert provider.last_diagnostics == {"auxiliary_usage": "unknown", "seat_tokens": 65565}


def test_rows_that_do_not_match_the_seat_are_unattributed_excess():
    other = dict(FABLE, inputTokens=100)  # no row equals the top-level usage
    out = _envelope(modelUsage={"claude-fable-5-1": other, "claude-haiku-4-5-20251001": HAIKU})
    usage = _extract_claude_usage(out)
    assert usage == {"input_tokens": 62772 + 2801, "output_tokens": 2827 + 16}
    got = _extract_claude_diagnostics(out)
    assert got["auxiliary_usage"] == "unattributed"
    assert got["auxiliary_tokens"] == (62772 + 2801 + 2843) - (62738 + 2827)
    assert got["auxiliary_models"] == ["claude-fable-5-1", "claude-haiku-4-5-20251001"]


def test_rows_without_top_level_usage_are_known_but_unattributed():
    out = json.dumps({"type": "result", "result": "ok",
                      "modelUsage": {"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": HAIKU}})
    assert _extract_claude_usage(out) == {"input_tokens": 62738 + 2801, "output_tokens": 2827 + 16}
    got = _extract_claude_diagnostics(out)
    assert got["auxiliary_models"] == ["claude-fable-5-1", "claude-haiku-4-5-20251001"]
    assert got["auxiliary_usage"] == "unattributed"


def test_nothing_reported_stays_unknown():
    assert _extract_claude_usage(json.dumps({"type": "result", "result": "ok"})) is None
    assert _extract_claude_usage("not json") is None


def test_whitelist_filters_provenance_like_tool_names():
    got = safe_diagnostics({"auxiliary_models": ["claude-haiku-4-5", "bad name", 3], "auxiliary_tokens": -1,
                            "auxiliary_usage": "guess"})
    assert got == {"auxiliary_models": ["claude-haiku-4-5"]}


def test_explicit_null_in_a_cache_field_is_unknown_but_absence_is_zero():
    absent = {"inputTokens": 2801, "outputTokens": 16}
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": absent})
    assert _extract_claude_usage(out) == {"input_tokens": 62738 + 2801, "output_tokens": 2827 + 16}
    null = dict(HAIKU, cacheReadInputTokens=None)
    out = _envelope(modelUsage={"claude-fable-5-1": FABLE, "claude-haiku-4-5-20251001": null})
    assert _extract_claude_usage(out) is None
    assert _extract_claude_diagnostics(out)["auxiliary_usage"] == "unknown"
    top_null = json.dumps({"type": "result", "result": "ok",
                           "usage": dict(USAGE, cache_read_input_tokens=None)})
    assert _extract_claude_usage(top_null) is None


def test_consistency_is_checked_per_component_not_by_grand_total():
    # rows' output exceeds the seat's, rows' input falls short: the grand total
    # would pass, the input component does not.
    out = json.dumps({"type": "result", "result": "ok",
                      "usage": {"input_tokens": 100, "output_tokens": 20},
                      "modelUsage": {"seat": {"inputTokens": 1, "outputTokens": 120}}})
    assert _extract_claude_usage(out) is None
    assert _extract_claude_diagnostics(out) == {"auxiliary_usage": "unknown", "seat_tokens": 120}


def test_an_empty_model_usage_map_is_unknown_but_absence_keeps_the_seat():
    assert _extract_claude_usage(_envelope(modelUsage={})) is None
    assert _extract_claude_diagnostics(_envelope(modelUsage={})) == {"auxiliary_usage": "unknown",
                                                                     "seat_tokens": 65565}
    assert _extract_claude_usage(_envelope()) == {"input_tokens": 62738, "output_tokens": 2827}
    # An empty map with no seat figure at all is simply nothing reported.
    assert _extract_claude_usage(json.dumps({"type": "result", "result": "ok", "modelUsage": {}})) is None
