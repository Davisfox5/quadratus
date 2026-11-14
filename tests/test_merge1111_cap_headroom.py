"""The limits text says when a round cap leaves a thin margin under the
per-call threshold.

Found by the stockroom benchmark (2026-11-12): 500,000 per call derived the
8-round floor, which plans 480,000; a revision reported 511,672 and the run
stopped on its first task. Nothing warned, because the text spoke only when
the plan was over the threshold.
"""

from quadratus.run_budget import (
    RunLimits,
    describe_limits,
    lead_cap_overrun,
    lead_cap_tight,
    lead_turns_for,
)


def _limits(per_call):
    return RunLimits(max_calls=30, max_reported_tokens=6_000_000, wall_seconds=3600,
                     reserve_tokens_per_call=200_000, max_tokens_per_call=per_call)


def test_the_floor_under_500k_is_named_as_close_to_the_threshold():
    limits = _limits(500_000)
    turns = lead_turns_for(500_000)
    assert turns == 8 and lead_cap_overrun(turns, limits) is None
    assert lead_cap_tight(turns, limits) == 480_000
    text = describe_limits(limits, lead_turns=turns, lead_source="derived")
    assert "close to the 500,000-token per-call threshold" in text
    assert "at least 600,000 tokens" in text


def test_a_cap_with_the_usual_headroom_says_nothing():
    limits = _limits(800_000)
    assert lead_cap_tight(10, limits) is None
    assert "close to the" not in describe_limits(limits, lead_turns=10, lead_source="the operator")


def test_a_plan_over_the_threshold_keeps_its_own_sentence_and_not_this_one():
    limits = _limits(150_000)
    turns = lead_turns_for(150_000)
    assert lead_cap_overrun(turns, limits) and lead_cap_tight(turns, limits) is None
    text = describe_limits(limits, lead_turns=turns, lead_source="derived")
    assert "more than the 150,000-token" in text and "close to the" not in text


def test_no_threshold_or_no_cap_says_nothing():
    assert lead_cap_tight(8, None) is None
    assert lead_cap_tight(None, _limits(500_000)) is None
