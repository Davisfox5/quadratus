"""Tests for the model roster and capability resolution."""

from __future__ import annotations

import pytest

from quadratus.registry import (
    CONTROL_PLANE,
    MODE_ROSTERS,
    REFUSAL_CHAIN,
    RETIRED_ROSTER,
    ROSTER,
    VENDORS,
    Capability,
    best_for,
    control_plane_model,
    models_for,
    resolve,
)


def test_every_roster_key_is_unique():
    keys = [m.key for m in ROSTER]
    assert len(keys) == len(set(keys))


def test_the_roster_covers_exactly_the_lineup():
    assert {m.provider for m in ROSTER} == set(VENDORS)


def test_nothing_resolves_to_a_retired_model():
    """A roster entry the harness cannot invoke is worse than a missing one,
    because something will eventually pick it."""
    assert RETIRED_ROSTER, "the retired rows are kept for their notes"
    for spec in RETIRED_ROSTER:
        assert resolve(spec.key) is None, spec.key
        assert spec.key not in {m.key for m in ROSTER}, spec.key


def test_a_row_is_retired_for_a_vendor_or_for_a_transport():
    """Retirement used to mean one thing -- the vendor left, so the provider
    is not in VENDORS. Grok 4.1 Fast, 4.3 and 4.20 are the second kind: xAI is
    very much in the lineup, but the subscription CLI will not invoke them.
    Asserting the old proxy would now forbid the case that actually cost a
    broken ladder rung."""
    for spec in RETIRED_ROSTER:
        vendor_gone = spec.provider not in VENDORS
        transport_cannot = spec.provider == "grok"
        assert vendor_gone or transport_cannot, spec.key


def test_retired_rows_keep_the_notes_that_justify_them():
    """The reason they are kept at all: re-deriving this costs a research
    session, and removing a vendor is routinely reversed."""
    for spec in RETIRED_ROSTER:
        assert spec.notes.strip(), spec.key


def test_resolve_round_trips():
    spec = resolve("claude:opus")
    assert spec is not None and spec.label == "Claude Opus"
    assert resolve("claude:nope") is None


def test_models_for_filters_by_capability():
    cheap = {m.key for m in models_for(Capability.CHEAP)}
    assert "claude:haiku" in cheap
    assert "claude:fable" not in cheap


def test_models_for_filters_by_provider():
    found = models_for(Capability.CODE, providers=["grok"])
    assert all(m.provider == "grok" for m in found)


def test_min_context_filter_selects_the_widest_windows():
    wide = {m.key for m in models_for(Capability.LONG_CONTEXT, min_context=1000 * 1024)}
    assert wide == {"openai:gpt-5.6-sol"}


def test_every_roster_model_is_one_the_transport_can_actually_invoke():
    """The rule the retired Grok rows were removed for. A vendor's published
    catalogue is not a statement about what a consumer subscription reaches:
    Grok 4.1 Fast, 4.3 and 4.20 are real xAI models that the Grok Build CLI
    rejects outright, and while they sat in ROSTER the ROTE rung and the
    lookup errand both pointed at nothing."""
    unreachable = {"grok:grok-4-1-fast", "grok:grok-4.3", "grok:grok-4.20"}
    assert unreachable.isdisjoint({m.key for m in ROSTER})
    assert unreachable <= {m.key for m in RETIRED_ROSTER}
    for key in unreachable:
        assert resolve(key) is None


def test_the_unverified_window_rule_outlives_the_row_it_was_written_on():
    """Grok 4.20 is where 'an advertised window is not a capability' was
    argued, and the Astra row cites it by name. Retiring the row must not
    quietly retire the reasoning."""
    retired = {m.key: m for m in RETIRED_ROSTER}
    assert Capability.LONG_CONTEXT not in retired["grok:grok-4.20"].caps
    assert "Grok 4.20" in resolve("openai:gpt-6-astra").notes


def test_the_fallback_orchestrators_window_claim_is_held_to_the_same_rule():
    """A new row with no verification does not get the tag its neighbours had
    to earn -- least of all the row least able to survive the claim being
    wrong."""
    assert Capability.LONG_CONTEXT not in resolve("openai:gpt-6-astra").caps


def test_long_context_candidates_are_all_independently_plausible():
    """Every remaining candidate's window is corroborated by something other
    than the vendor's own marketing."""
    for spec in models_for(Capability.LONG_CONTEXT):
        assert spec.context <= 1100 * 1024, spec.key


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
    assert first_non_claude >= 1


def test_the_refusal_chain_carries_no_worker_tier_model():
    """A model that will not refuse is not thereby a model to hand the work
    to, and re-routing a declined request is where that matters most."""
    for worker_tier in ("claude:sonnet", "claude:haiku", "openai:gpt-5.6-luna",
                        "grok:grok-4.5"):
        assert worker_tier not in REFUSAL_CHAIN


# -- mode rosters ------------------------------------------------------------


def test_mode_rosters_reference_real_models():
    for mode, roles in MODE_ROSTERS.items():
        for role, keys in roles.items():
            for key in keys:
                assert resolve(key) is not None, f"{mode}.{role} -> {key}"


def test_adversarial_pairs_the_precision_and_recall_reviewers():
    reviewers = set(MODE_ROSTERS["adversarial"]["reviewers"])
    assert reviewers == {"claude:opus", "openai:gpt-5.6-sol"}


def test_planning_is_never_narrower_than_the_brain_trust():
    """Planning runs once per run, so it is the cheapest place to be
    inclusive. Excluding a peer from planning the work it will then debate
    would be the expensive kind of saving."""
    for mode, roles in MODE_ROSTERS.items():
        assert set(roles["peers"]) <= set(roles["planners"]), mode


def test_the_brain_trust_carries_exactly_one_model_per_vendor():
    """Independent reads are why this system exists rather than one strong
    model in a loop; two peers from one vendor would not be independent, and a
    vendor with no seat is a read the debate never gets."""
    peers = MODE_ROSTERS["adversarial"]["peers"]
    providers = [resolve(p).provider for p in peers]
    assert sorted(providers) == sorted(VENDORS)


def test_grok_is_a_full_brain_trust_member_not_a_planner_only_guest():
    assert "grok:default" in MODE_ROSTERS["adversarial"]["peers"]


def test_solo_mode_has_no_reviewers():
    assert MODE_ROSTERS["solo"]["reviewers"] == []


def test_modes_do_not_grow_reviewers_beyond_peers():
    """A reviewer that is not a peer would be paying for an extra voice."""
    for mode, roles in MODE_ROSTERS.items():
        assert set(roles["reviewers"]) <= set(roles["peers"]), mode


# -- control plane -----------------------------------------------------------


def test_control_plane_uses_only_cheap_or_fast_models():
    for role, entry in CONTROL_PLANE.items():
        spec = resolve(entry.model)
        assert spec is not None, role
        assert Capability.CHEAP in spec.caps, f"{role} -> {entry.model} is not cheap"


def test_the_interview_is_never_run_unsupervised():
    """Operator directive: Sonnet may be the voice, never the interviewer.
    The questions asked decide what the whole run aims at."""
    interview = CONTROL_PLANE["interview"]
    assert interview.supervised_by == "orchestrator"
    assert interview.mandate.strip()


def test_a_supervised_role_says_what_the_supervisor_owes_it():
    """A supervision flag with no mandate is a flag an implementer satisfies
    by setting a variable."""
    for role, entry in CONTROL_PLANE.items():
        if entry.supervised_by:
            assert entry.mandate.strip(), role


def test_control_plane_model_resolves_roles_and_shrugs_at_the_rest():
    assert control_plane_model("convergence") == "claude:haiku"
    assert control_plane_model("telepathy") is None


def test_convergence_judge_is_not_a_debater_in_any_mode():
    """A model asked whether its own work is done will say yes."""
    judge = CONTROL_PLANE["convergence"].model
    for mode, roles in MODE_ROSTERS.items():
        assert judge not in roles["peers"], mode


def test_ledger_extractor_is_not_a_debater_in_any_mode():
    """The ledger survives compaction; its author must have no stake."""
    extractor = CONTROL_PLANE["ledger_extraction"].model
    for mode, roles in MODE_ROSTERS.items():
        assert extractor not in roles["peers"], mode


# -- orchestrator seat -------------------------------------------------------

from quadratus.registry import (  # noqa: E402
    ORCHESTRATOR_CHAIN,
    peers_for,
    synthesizer_for,
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


def test_brain_trust_is_fixed_regardless_of_who_orchestrates():
    """A weaker brain trust is a worse failure than a supervisor with a stake."""
    base = MODE_ROSTERS["adversarial"]["peers"]
    for orchestrator in ORCHESTRATOR_CHAIN:
        assert peers_for("adversarial", orchestrator) == base


def test_opus_never_leaves_the_brain_trust():
    for mode, roles in MODE_ROSTERS.items():
        if "claude:opus" not in roles["peers"]:
            continue
        for orchestrator in ORCHESTRATOR_CHAIN:
            assert "claude:opus" in peers_for(mode, orchestrator), (mode, orchestrator)


def test_sonnet_is_never_in_the_brain_trust():
    """It is a tier below the seats it would fill."""
    for mode, roles in MODE_ROSTERS.items():
        assert "claude:sonnet" not in roles["peers"], mode
        assert "claude:sonnet" not in peers_for(mode, "claude:opus"), mode


def test_double_hatted_peer_does_not_synthesise():
    """Synthesis decides what survives -- the one place a stake changes the
    artifact, so a peer holding the seat is passed over there and only there."""
    chosen = synthesizer_for("adversarial", "claude:opus")
    assert chosen is not None and chosen != "claude:opus"
    assert chosen in MODE_ROSTERS["adversarial"]["peers"]


def test_synthesiser_is_unrestricted_when_the_primary_orchestrates():
    assert synthesizer_for("adversarial", "claude:fable") == \
        MODE_ROSTERS["adversarial"]["peers"][0]


def test_synthesiser_returns_none_when_double_hatting_is_unavoidable():
    """Solo mode has one peer; the caller must surface this, not hide it."""
    assert synthesizer_for("solo", "claude:opus") is None


def test_double_hatting_is_confined_to_synthesis_in_every_mode():
    """The seat holder may debate, but never picks what survives -- unless the
    mode has nobody else, which the caller is told about explicitly."""
    for mode in MODE_ROSTERS:
        for orchestrator in ORCHESTRATOR_CHAIN:
            chosen = synthesizer_for(mode, orchestrator)
            assert chosen != orchestrator, (mode, orchestrator)


# -- floating aliases --------------------------------------------------------

from quadratus.registry import assert_floating_orchestrators  # noqa: E402


def test_every_orchestrator_seat_addresses_a_model_line():
    """The seat is chosen once and held for a session, so a pinned alias here
    is how a run keeps using last quarter's model for months."""
    for key in ORCHESTRATOR_CHAIN:
        assert resolve(key).floating, key


def test_a_pinned_orchestrator_is_rejected_at_import():
    """The failure mode is silent -- everything works, on a model a generation
    behind -- so it has to be caught when the table is edited."""
    with pytest.raises(ValueError, match="pinned to one release"):
        assert_floating_orchestrators(["grok:default"])


def test_an_unknown_orchestrator_is_rejected():
    with pytest.raises(ValueError, match="unknown model"):
        assert_floating_orchestrators(["claude:nope"])


def test_a_non_orchestrating_model_is_rejected():
    with pytest.raises(ValueError, match="without ORCHESTRATE"):
        assert_floating_orchestrators(["claude:haiku"])


def test_a_floating_label_never_ends_in_a_release_number():
    """"Claude Opus 5" is a claim about today that nobody comes back to
    correct; "Claude Opus" and "GPT-6 Astra" both name lines, and the 6 in the
    second is part of the line's name rather than its release."""
    for spec in ROSTER:
        if not spec.floating:
            continue
        last = spec.label.split()[-1]
        assert not last.replace(".", "").isdigit(), spec.label


def test_the_alias_chain_starts_with_the_line_name():
    for spec in ROSTER:
        if spec.vendor_default:
            assert spec.alias_chain == (), spec.key
            continue
        assert spec.alias_chain[0] == spec.alias
        if not spec.floating:
            assert spec.alias_chain == (spec.alias,), spec.key


def test_a_vendor_default_row_names_no_model_on_the_wire():
    """The strongest form of not pinning: the vendor moves its own pointer and
    the run follows, with no alias to guess and nothing to probe."""
    from quadratus.latest import alias_for

    grok = resolve("grok:default")
    assert grok.vendor_default
    assert alias_for("grok:default", env={}, cache={}) == ""
    assert not grok.floating, "floating walks an alias chain; this has none"


def test_a_vendor_default_row_still_yields_to_the_operator():
    """For the day a vendor's default moves somewhere you did not want."""
    from quadratus.latest import alias_env_var, alias_for

    env = {alias_env_var("grok:default"): "grok-4.5"}
    assert alias_for("grok:default", env=env, cache={}) == "grok-4.5"


# -- the orchestrator fallback ----------------------------------------------


def test_the_seat_has_exactly_one_fallback_and_it_is_cross_vendor():
    """Operator directive, and the shape it buys: one recorded substitution,
    behind a different subscription, then a full stop."""
    assert len(ORCHESTRATOR_CHAIN) == 2
    primary, fallback = (resolve(k) for k in ORCHESTRATOR_CHAIN)
    assert primary.provider != fallback.provider


def test_no_brain_trust_peer_holds_a_seat():
    """Seating a peer would make the supervisor a competitor in the debate it
    supervises, and put the fleet's strongest reviewer on bookkeeping. Opus is
    ORCHESTRATE-capable and still absent from the chain; that is the point."""
    peers = set(MODE_ROSTERS["adversarial"]["peers"])
    assert not peers & set(ORCHESTRATOR_CHAIN)
    assert Capability.ORCHESTRATE in resolve("claude:opus").caps
