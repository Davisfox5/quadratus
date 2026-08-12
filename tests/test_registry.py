"""Tests for the model roster and capability resolution."""

from __future__ import annotations

from multi_llm.registry import (
    CONTROL_PLANE,
    MODE_ROSTERS,
    REFUSAL_CHAIN,
    ROSTER,
    Capability,
    best_for,
    models_for,
    resolve,
)


def test_every_roster_key_is_unique():
    keys = [m.key for m in ROSTER]
    assert len(keys) == len(set(keys))


def test_all_four_providers_present():
    assert {m.provider for m in ROSTER} == {"claude", "openai", "gemini", "grok"}


def test_resolve_round_trips():
    spec = resolve("claude:opus")
    assert spec is not None and spec.label == "Claude Opus 5"
    assert resolve("claude:nope") is None


def test_models_for_filters_by_capability():
    cheap = {m.key for m in models_for(Capability.CHEAP)}
    assert "claude:haiku" in cheap
    assert "claude:fable" not in cheap


def test_models_for_filters_by_provider():
    found = models_for(Capability.CODE, providers=["grok"])
    assert all(m.provider == "grok" for m in found)


def test_min_context_filter_selects_the_widest_windows():
    wide = {m.key for m in models_for(Capability.LONG_CONTEXT, min_context=2000 * 1024)}
    assert wide == {"grok:grok-4.20"}


def test_grok_420_has_the_largest_window():
    widest = max(ROSTER, key=lambda m: m.context)
    assert widest.key == "grok:grok-4.20"


def test_best_for_returns_none_for_unknown_capability():
    assert best_for("telepathy") is None


# -- capability assignments that encode researched findings ------------------


def test_fable_is_excluded_from_security_capable_models():
    """Anthropic documents that benign cyber work can trip Fable's classifiers."""
    unclassified = {m.key for m in models_for(Capability.NO_CLASSIFIER)}
    assert "claude:fable" not in unclassified
    assert {"claude:sonnet", "claude:haiku"} <= unclassified


def test_haiku_is_not_a_long_context_candidate():
    """200K window and Feb 2025 knowledge disqualify it."""
    assert Capability.LONG_CONTEXT not in resolve("claude:haiku").caps


def test_review_and_debug_are_distinct_capabilities():
    """Finding unknown bugs and root-causing a known one are different jobs."""
    reviewers = {m.key for m in models_for(Capability.REVIEW)}
    debuggers = {m.key for m in models_for(Capability.DEBUG)}
    assert reviewers != debuggers
    assert "claude:opus" in debuggers
    assert "openai:gpt-5.6-sol" in reviewers
    assert "openai:gpt-5.6-sol" not in debuggers


# -- refusal chain -----------------------------------------------------------


def test_refusal_chain_entries_all_resolve():
    assert all(resolve(k) is not None for k in REFUSAL_CHAIN)


def test_refusal_chain_never_targets_fable():
    """Routing a declined request into the strictest classifier is pointless."""
    assert "claude:fable" not in REFUSAL_CHAIN


def test_refusal_chain_prefers_anthropic_before_undocumented_vendors():
    """Other vendors' security refusal behaviour is unknown, not known-good."""
    first_non_claude = next(
        i for i, k in enumerate(REFUSAL_CHAIN) if not k.startswith("claude:")
    )
    assert all(k.startswith("claude:") for k in REFUSAL_CHAIN[:first_non_claude])
    assert first_non_claude >= 2


# -- mode rosters ------------------------------------------------------------


def test_mode_rosters_reference_real_models():
    for mode, roles in MODE_ROSTERS.items():
        for role, keys in roles.items():
            for key in keys:
                assert resolve(key) is not None, f"{mode}.{role} -> {key}"


def test_adversarial_pairs_the_precision_and_recall_reviewers():
    reviewers = set(MODE_ROSTERS["adversarial"]["reviewers"])
    assert reviewers == {"claude:opus", "openai:gpt-5.6-sol"}


def test_planning_roster_is_widest_since_it_runs_once_per_run():
    adversarial = MODE_ROSTERS["adversarial"]
    assert len(adversarial["planners"]) > len(adversarial["peers"])


def test_solo_mode_has_no_reviewers():
    assert MODE_ROSTERS["solo"]["reviewers"] == []


def test_modes_do_not_grow_reviewers_beyond_peers():
    """A reviewer that is not a peer would be paying for an extra voice."""
    for mode, roles in MODE_ROSTERS.items():
        assert set(roles["reviewers"]) <= set(roles["peers"]), mode


# -- control plane -----------------------------------------------------------


def test_control_plane_uses_only_cheap_or_fast_models():
    for role, key in CONTROL_PLANE.items():
        spec = resolve(key)
        assert spec is not None, role
        assert Capability.CHEAP in spec.caps, f"{role} -> {key} is not cheap"


def test_convergence_judge_is_not_a_debater_in_any_mode():
    """A model asked whether its own work is done will say yes."""
    judge = CONTROL_PLANE["convergence"]
    for mode, roles in MODE_ROSTERS.items():
        assert judge not in roles["peers"], mode


def test_ledger_extractor_is_not_a_debater_in_any_mode():
    """The ledger survives compaction; its author must have no stake."""
    extractor = CONTROL_PLANE["ledger_extraction"]
    for mode, roles in MODE_ROSTERS.items():
        assert extractor not in roles["peers"], mode


# -- orchestrator seat -------------------------------------------------------

from multi_llm.registry import (  # noqa: E402
    ORCHESTRATOR_CHAIN,
    PEER_SUBSTITUTES,
    peers_for,
)


def test_orchestrator_chain_entries_resolve_and_are_capable():
    for key in ORCHESTRATOR_CHAIN:
        spec = resolve(key)
        assert spec is not None, key
        assert Capability.ORCHESTRATE in spec.caps, key


def test_fable_leads_the_orchestrator_chain():
    assert ORCHESTRATOR_CHAIN[0] == "claude:fable"


def test_orchestrator_chain_has_a_fallback():
    """Fable is the likeliest model to become unavailable, and a stalled
    orchestrator stalls the whole run."""
    assert len(ORCHESTRATOR_CHAIN) >= 2


def test_fable_is_not_a_peer_in_any_mode():
    """It orchestrates; a supervisor must not also compete in the debate."""
    for mode, roles in MODE_ROSTERS.items():
        assert "claude:fable" not in roles["peers"], mode
        assert "claude:fable" not in roles["planners"], mode


def test_primary_orchestrator_leaves_the_brain_trust_intact():
    base = MODE_ROSTERS["adversarial"]["peers"]
    assert peers_for("adversarial", "claude:fable") == base


def test_fallback_orchestrator_is_removed_from_the_brain_trust():
    seated = peers_for("adversarial", "claude:opus")
    assert "claude:opus" not in seated


def test_displaced_peer_is_replaced_not_merely_dropped():
    base = MODE_ROSTERS["adversarial"]["peers"]
    seated = peers_for("adversarial", "claude:opus")
    assert len(seated) == len(base)
    assert seated[-1] in PEER_SUBSTITUTES


def test_substitute_is_never_already_seated():
    for mode in MODE_ROSTERS:
        for orchestrator in ORCHESTRATOR_CHAIN:
            seated = peers_for(mode, orchestrator)
            assert len(seated) == len(set(seated)), (mode, orchestrator)


def test_orchestrator_is_never_seated_in_any_mode_or_chain_position():
    for mode in MODE_ROSTERS:
        for orchestrator in ORCHESTRATOR_CHAIN:
            assert orchestrator not in peers_for(mode, orchestrator)
