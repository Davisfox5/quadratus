"""Tests for task-kind routing.

These lock in the *shape* of the policy rather than any particular model
choice, because the model choices are expected to churn and the shape is not.
The properties worth defending: an unproven preference must not pin a model,
an exclusion must actually exclude, and no routing decision may stall a task.
"""

from __future__ import annotations

from quadratus.registry import MODE_ROSTERS, resolve
from quadratus.task_kinds import (
    DIFFICULTY_LADDER,
    MAX_TASK_LINES,
    ROUTING,
    Confidence,
    KindPolicy,
    TaskKind,
    guidance_for,
    policy_for,
    route,
)

TRUST = MODE_ROSTERS["adversarial"]["peers"]
OPUS = "claude:opus"
SOL = "openai:gpt-5.6-sol"
GROK = "grok:default"
GROK_WORKER = "grok:worker"
FABLE = "claude:fable"


# -- the table is internally coherent ----------------------------------------


def test_every_task_kind_has_a_policy():
    kinds = {v for k, v in vars(TaskKind).items() if not k.startswith("_") and isinstance(v, str)}
    assert kinds == set(ROUTING)


def test_every_referenced_model_resolves():
    for kind, policy in ROUTING.items():
        for key in policy.prefer + policy.exclude:
            assert resolve(key) is not None, f"{kind} -> {key}"


def test_every_policy_states_its_evidence():
    """A preference with no provenance cannot be judged or revised later."""
    for kind, policy in ROUTING.items():
        assert policy.evidence.strip(), kind


def test_a_model_is_never_both_preferred_and_excluded():
    for kind, policy in ROUTING.items():
        assert not (set(policy.prefer) & set(policy.exclude)), kind


# -- confidence controls behaviour, not just documentation -------------------


def test_low_confidence_never_pins_a_model():
    """Pinning on a hunch freezes the hunch and destroys the rotation data
    that would have corrected it."""
    for kind, policy in ROUTING.items():
        if policy.confidence == Confidence.LOW and policy.prefer:
            assert not policy.pins_a_model, kind


def test_low_confidence_kinds_fall_through_to_rotation():
    assert route(TaskKind.DATA, default=GROK, candidates=TRUST) == GROK
    assert route(TaskKind.GENERAL, default=OPUS, candidates=TRUST) == OPUS


def test_high_confidence_kinds_override_rotation():
    assert route(TaskKind.SECURITY, default=GROK, candidates=TRUST) == SOL


# -- exclusions --------------------------------------------------------------


def test_the_orchestrator_is_excluded_from_security_work():
    assert FABLE in policy_for(TaskKind.SECURITY).exclude


def test_an_exclusion_holds_even_when_the_preferred_model_is_down():
    """Security pins Sol and excludes the orchestrator; with Sol down the work
    must still not climb back to the excluded model."""
    got = route(
        TaskKind.SECURITY,
        default=FABLE,
        candidates=[FABLE, OPUS],
        available=lambda k: k != SOL,
    )
    assert got != FABLE


def test_no_exclusion_names_a_model_outside_the_lineup():
    """An exclusion pointing at a model nothing can route to is noise that
    outlives its evidence -- it reads as a live finding and cannot fire."""
    for kind, policy in ROUTING.items():
        for key in policy.exclude:
            assert resolve(key) is not None, f"{kind} excludes a retired model: {key}"


# -- routing never stalls a task ---------------------------------------------


def test_an_unknown_kind_rotates_rather_than_raising():
    assert route("astrology", default=OPUS, candidates=TRUST) == OPUS
    assert policy_for("astrology") is ROUTING[TaskKind.GENERAL]


def test_a_pinned_but_unavailable_model_degrades_gracefully():
    got = route(
        TaskKind.TEST, default=GROK, candidates=TRUST, available=lambda k: k != SOL
    )
    assert got == GROK


def test_everything_unavailable_still_returns_a_lead():
    """A routing preference must never be able to stall the run."""
    got = route(
        TaskKind.MOBILE, default=OPUS, candidates=TRUST, available=lambda _k: False
    )
    assert got == OPUS


def test_route_prefers_earlier_entries_in_the_prefer_list():
    assert route(TaskKind.REVIEW, default=GROK, candidates=TRUST) == SOL


# -- the difficulty ladder ---------------------------------------------------


def test_the_ladder_spans_every_subscription():
    """The point is load-spreading: no vendor window sits idle while another
    exhausts."""
    from quadratus.registry import VENDORS

    providers = {k.split(":")[0] for k in DIFFICULTY_LADDER.values()}
    assert providers == set(VENDORS)


def test_the_doubled_vendor_is_the_least_contended_one():
    """Four rungs across three vendors means somebody takes two. It should be
    the vendor that is not also carrying an orchestrator seat or the pinned
    kinds."""
    from collections import Counter

    from quadratus.registry import ORCHESTRATOR_CHAIN

    counts = Counter(k.split(":")[0] for k in DIFFICULTY_LADDER.values())
    doubled = [vendor for vendor, n in counts.items() if n > 1]
    seat_vendors = {k.split(":")[0] for k in ORCHESTRATOR_CHAIN}
    assert doubled == ["grok"]
    assert "grok" not in seat_vendors


def test_each_difficulty_maps_to_its_rung():
    assert route(TaskKind.BACKEND, default=GROK, difficulty="complex") == OPUS
    assert route(TaskKind.BACKEND, default=GROK, difficulty="standard") == SOL
    assert route(TaskKind.BACKEND, default=OPUS, difficulty="simple") == GROK
    assert route(TaskKind.BACKEND, default=OPUS, difficulty="rote") == GROK_WORKER


def test_an_unavailable_rung_escalates_upward_before_downward():
    """A stronger model can always do easier work; degrading is a last resort."""
    got = route(TaskKind.BACKEND, default=GROK, difficulty="simple",
                available=lambda k: k != GROK)
    assert got == SOL
    got = route(TaskKind.BACKEND, default=GROK, difficulty="complex",
                available=lambda k: k != OPUS)
    assert got == SOL  # nothing above Opus; falls one rung down


def test_an_excluded_rung_is_skipped():
    """Rote security work must not land on the excluded orchestrator; it
    climbs rather than stalling."""
    got = route(TaskKind.SECURITY, default=FABLE, difficulty="rote",
                available=lambda k: k != SOL)
    assert got != FABLE


def test_kind_pins_beat_the_ladder():
    """Security and testing go to Sol whatever the difficulty says."""
    for difficulty in DIFFICULTY_LADDER:
        assert route(TaskKind.SECURITY, default=GROK, difficulty=difficulty) == SOL
        assert route(TaskKind.TEST, default=GROK, difficulty=difficulty) == SOL


def test_no_difficulty_means_no_ladder():
    assert route(TaskKind.BACKEND, default=GROK) == GROK


def test_only_a_handful_of_kinds_pin_at_all():
    """The ladder is the primary axis; pins are the exception, not the rule."""
    pinned = {k for k, p in ROUTING.items() if p.prefer}
    assert pinned == {TaskKind.SCOPE, TaskKind.DECOMPOSE, TaskKind.SECURITY,
                      TaskKind.TEST, TaskKind.REVIEW}


# -- guidance ----------------------------------------------------------------


def test_performance_work_is_told_to_measure_before_reasoning():
    """Near-zero recall on perf defects across every model measured."""
    policy = policy_for(TaskKind.PERF)
    assert policy.tool_first
    assert not policy.prefer  # nobody to prefer; that is the finding
    assert any("profiler" in g for g in guidance_for(TaskKind.PERF))


def test_concurrency_is_gated_rather_than_routed():
    policy = policy_for(TaskKind.CONCURRENCY)
    assert not policy.prefer
    assert policy.gate and policy.human_check


def test_kinds_below_human_performance_ask_for_a_human_look():
    for kind in (TaskKind.REFACTOR, TaskKind.CONCURRENCY):
        assert any("human review" in g for g in guidance_for(kind)), kind


def test_reviewers_are_told_not_to_self_filter():
    assert any("Do not filter" in g for g in guidance_for(TaskKind.REVIEW))


def test_a_kind_with_no_hazards_carries_no_guidance():
    """Guidance is a requirement list; padding it makes real entries cheaper."""
    assert guidance_for(TaskKind.GENERAL) == []


def test_decomposition_carries_the_size_ceiling():
    assert str(MAX_TASK_LINES) in policy_for(TaskKind.DECOMPOSE).gate


# -- the findings that outrank the per-model preferences ---------------------


def test_review_pins_two_models_with_opposite_failure_modes():
    """Neither is a good reviewer alone; the pair covers what each misses."""
    prefer = policy_for(TaskKind.REVIEW).prefer
    assert len(prefer) == 2
    assert set(prefer) == set(MODE_ROSTERS["adversarial"]["reviewers"])


def test_review_stays_pinned_while_debug_rides_the_ladder():
    """Review has measured evidence behind its pair; debug reaches the right
    model through difficulty instead of a pin."""
    assert policy_for(TaskKind.REVIEW).prefer
    assert not policy_for(TaskKind.DEBUG).prefer


def test_no_per_language_routing_exists():
    """No credible per-language data exists for this model generation."""
    for kind in ROUTING:
        assert kind not in ("python", "rust", "go", "typescript", "kotlin")


def test_a_bare_policy_rotates():
    assert not KindPolicy().pins_a_model
