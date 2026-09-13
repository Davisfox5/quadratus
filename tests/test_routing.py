"""Tests for orchestrator seating and security work routing."""

from __future__ import annotations

import dataclasses

import pytest

from quadratus.registry import ORCHESTRATOR_CHAIN
from quadratus.routing import (
    SECURITY_WORK_CHAIN,
    OrchestratorUnavailable,
    Seat,
    SeatReason,
    WorkClass,
    orchestrator_seat,
    route_security_work,
)

FABLE = "claude:fable"
ASTRA = "openai:gpt-6-astra"
OPUS = "claude:opus"
SOL = "openai:gpt-5.6-sol"
SONNET = "claude:sonnet"



def _unavailable(*keys):
    blocked = set(keys)
    return lambda k: k not in blocked


# -- normal seating ----------------------------------------------------------


def test_primary_is_seated_by_default():
    seat = orchestrator_seat()
    assert seat.key == FABLE
    assert seat.is_primary
    assert not seat.reverts_at_segment_end


def test_security_segment_yields_the_seat():
    seat = orchestrator_seat(security_segment=True)
    assert seat.key == ASTRA
    assert seat.reason == SeatReason.DELEGATED_SECURITY


def test_an_exhausted_primary_falls_back_rather_than_ending_the_run():
    """The seat holds the thread of the run, so substituting it is a real
    cost -- but the ledger already carries that thread, and ending a session
    with work in flight is the more expensive failure. The substitution is
    recorded, not silent."""
    seat = orchestrator_seat(available=_unavailable(FABLE))
    assert seat.key == ASTRA
    assert seat.reason == SeatReason.FALLBACK_UNAVAILABLE
    assert not seat.is_primary


def test_there_is_exactly_one_fallback_and_it_is_astra():
    """Operator directive. Opus could hold the seat and deliberately does not:
    it is a brain-trust peer and half the pinned reviewer pair, and those are
    worth more to the run than a second understudy."""
    assert ORCHESTRATOR_CHAIN == [FABLE, ASTRA]
    assert OPUS not in ORCHESTRATOR_CHAIN


def test_a_fallback_seat_lapses_by_recomputation():
    """No handback step: the next segment seats the primary again the moment
    it is reachable."""
    assert orchestrator_seat(available=_unavailable(FABLE)).key == ASTRA
    assert orchestrator_seat().key == FABLE


def test_both_seats_gone_is_a_full_stop_not_a_third_choice():
    """Opus is available in this scenario and is still not seated. The run
    stops rather than borrowing a model that has other work to do."""
    with pytest.raises(OrchestratorUnavailable, match="stops here"):
        orchestrator_seat(available=_unavailable(FABLE, ASTRA))


# -- the bug this module exists to fix ---------------------------------------


def test_security_delegation_reverts_on_the_next_segment():
    """The original flaw: one security question moved the seat permanently.

    Seating is computed per segment rather than stored, so the next
    unclassified segment gets the primary back with no handback step.
    """
    during = orchestrator_seat(security_segment=True)
    after = orchestrator_seat(security_segment=False)
    assert during.key == ASTRA
    assert after.key == FABLE
    assert after.is_primary


def test_security_delegation_is_marked_as_reverting():
    assert orchestrator_seat(security_segment=True).reverts_at_segment_end


def test_halt_message_tells_the_operator_what_to_do():
    with pytest.raises(OrchestratorUnavailable) as exc:
        orchestrator_seat(available=_unavailable(FABLE, ASTRA))
    assert "window" in str(exc.value)
    # Naming who was tried is the difference between "it broke" and "these
    # two are down, so wait or sign one in".
    assert all(key in str(exc.value) for key in (FABLE, ASTRA))


def test_the_seat_never_leaves_the_primary_while_it_is_available():
    """Two ways to lose the seat, and being merely outclassed is not one."""
    assert orchestrator_seat().is_primary
    assert not orchestrator_seat(security_segment=True).is_primary
    assert not orchestrator_seat(available=_unavailable(FABLE)).is_primary


def test_seat_is_immutable():
    seat = orchestrator_seat()
    with pytest.raises(dataclasses.FrozenInstanceError):
        seat.key = OPUS  # type: ignore[misc]


# -- combined conditions -----------------------------------------------------


def test_security_segment_skips_primary_even_when_it_is_available():
    seat = orchestrator_seat(security_segment=True, available=lambda _: True)
    assert seat.key != FABLE


def test_security_segment_with_no_deputy_available_halts():
    with pytest.raises(ExcursionUnavailable, match="no deputy"):
        orchestrator_seat(
            security_segment=True, available=_unavailable(*ORCHESTRATOR_CHAIN)
        )


def test_the_security_deputy_is_the_same_single_deputy():
    """There is one fallback seat and it covers both yield reasons. What makes
    the excursion safe is its structure -- deferred work, cross-vendor check --
    not the deputy's own classifier, which is undocumented."""
    assert orchestrator_seat(security_segment=True).key == ASTRA


def test_everything_unavailable_halts_rather_than_degrading():
    with pytest.raises(OrchestratorUnavailable):
        orchestrator_seat(available=lambda _: False)


def test_empty_chain_is_rejected():
    with pytest.raises(ValueError):
        orchestrator_seat(chain=[])


def test_custom_chain_is_honoured():
    seat = orchestrator_seat(chain=[SONNET, OPUS])
    assert seat.key == SONNET and seat.is_primary


def test_a_security_deputy_always_reverts():
    assert orchestrator_seat(security_segment=True).reverts_at_segment_end


def test_availability_is_not_called_when_primary_is_fine():
    """Liveness checks may be expensive; they should be lazy."""
    calls = []

    def probe(key):
        calls.append(key)
        return True

    orchestrator_seat(available=probe)
    assert calls == [ORCHESTRATOR_CHAIN[0]]


# -- security work routing ---------------------------------------------------


def test_general_work_keeps_its_default_routing():
    assert route_security_work(OPUS, work_class=WorkClass.GENERAL) == OPUS


def test_security_work_is_diverted_to_sol():
    assert route_security_work(OPUS, work_class=WorkClass.SECURITY) == SOL


def test_security_work_is_diverted_even_from_a_capable_default():
    """Operator preference outranks the default being merely adequate."""
    assert route_security_work(SONNET, work_class=WorkClass.SECURITY) == SOL


def test_security_routing_falls_through_when_sol_is_down():
    got = route_security_work(
        OPUS, work_class=WorkClass.SECURITY, available=_unavailable(SOL)
    )
    assert got == OPUS


def test_no_worker_tier_model_sits_in_the_security_chain():
    """Operator directive: the worker tier holds no role in security. Not
    being refused (Sonnet carries no classifier) is not the same as being
    trusted with the work."""
    for worker_tier in (SONNET, "claude:haiku", "openai:gpt-5.6-luna",
                        "grok:grok-4.5"):
        assert worker_tier not in SECURITY_WORK_CHAIN


def test_security_chain_never_routes_to_fable():
    assert FABLE not in SECURITY_WORK_CHAIN


def test_security_routing_returns_last_resort_when_all_are_down():
    got = route_security_work(
        OPUS, work_class=WorkClass.SECURITY, available=lambda _: False
    )
    assert got == SECURITY_WORK_CHAIN[-1]


def test_security_chain_entries_all_resolve():
    from quadratus.registry import resolve

    assert all(resolve(k) is not None for k in SECURITY_WORK_CHAIN)


# -- the seat and the work are routed independently --------------------------


def test_seat_and_work_routing_are_independent():
    """Supervising a security segment and doing the security work are
    different jobs, and go to different models -- even when, as here, both
    land on the same vendor."""
    seat = orchestrator_seat(security_segment=True)
    worker = route_security_work(OPUS, work_class=WorkClass.SECURITY)
    assert seat.key == ASTRA
    assert worker == SOL
    assert seat.key != worker


def test_seat_type_is_a_seat():
    assert isinstance(orchestrator_seat(), Seat)


# -- security excursions -----------------------------------------------------

from quadratus.routing import (  # noqa: E402
    Excursion,
    ExcursionUnavailable,  # noqa: F811
    close_excursion,
    open_security_excursion,
)


def test_excursion_moves_the_seat_but_defers_the_work():
    """Astra takes the seat because Fable's classifiers would refuse the
    subject; Sol does the work because that is the operator's preference."""
    ex = open_security_excursion()
    assert ex.orchestrator == ASTRA
    assert ex.worker == SOL


def test_excursion_orchestrator_never_performs_its_own_work():
    ex = open_security_excursion()
    assert ex.worker != ex.orchestrator


def test_excursion_verifier_double_checks_and_is_not_the_worker():
    ex = open_security_excursion()
    assert ex.verifier != ex.worker
    assert ex.verifier == ex.orchestrator


def test_excursion_rejects_a_self_performing_orchestrator():
    with pytest.raises(ValueError, match="defer the work"):
        Excursion(reason="x", orchestrator=OPUS, worker=OPUS, verifier=SOL,
                  returns_to=FABLE)


def test_excursion_rejects_self_verification():
    with pytest.raises(ValueError, match="double-check its own"):
        Excursion(reason="x", orchestrator=OPUS, worker=SOL, verifier=SOL,
                  returns_to=FABLE)


def test_excursion_knows_where_the_seat_returns_to():
    assert open_security_excursion().returns_to == FABLE


def test_closing_hands_the_seat_back_to_the_primary():
    """The failure this prevents: degrade once, never recover."""
    ex = open_security_excursion()
    after = close_excursion(ex)
    assert after.key == FABLE
    assert after.is_primary


def test_closing_is_unconditional():
    """A failed security lookup still ends the excursion and hands back."""
    ex = open_security_excursion()
    assert close_excursion(ex).key == FABLE
    assert close_excursion(ex).key == FABLE


def test_excursion_still_defers_when_sol_is_unavailable():
    ex = open_security_excursion(available=_unavailable(SOL))
    assert ex.worker == OPUS
    assert ex.worker != ex.orchestrator
    assert ex.worker != ex.orchestrator
    assert ex.verifier != ex.worker


def test_excursion_refuses_to_form_when_the_seat_is_the_only_candidate():
    """Self-performing and self-checking would hand back a security answer
    carrying a verification it never received, so this stops instead."""
    with pytest.raises(ExcursionUnavailable, match="cannot be deferred"):
        open_security_excursion(chain=[FABLE, OPUS], available=_unavailable(SOL))


def test_excursion_refuses_to_form_when_the_named_worker_cannot_answer():
    """route_security_work returns its last resort rather than raising when
    everything is down -- right for its own caller, and caught here. Operator
    decision: refuse, rather than let the invocation fail later at transport.
    """
    with pytest.raises(ExcursionUnavailable, match="cannot be deferred"):
        open_security_excursion(available=_unavailable(*SECURITY_WORK_CHAIN))


def test_excursion_failure_names_what_to_restore():
    with pytest.raises(ExcursionUnavailable) as exc:
        open_security_excursion(available=_unavailable(*SECURITY_WORK_CHAIN))
    assert SOL in str(exc.value) and OPUS in str(exc.value)


def test_excursion_is_immutable():
    ex = open_security_excursion()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ex.worker = OPUS  # type: ignore[misc]


def test_excursion_does_not_touch_the_brain_trust():
    """The whole point of peeling security off into a side-thread."""
    from quadratus.registry import MODE_ROSTERS, peers_for

    ex = open_security_excursion()
    assert peers_for("adversarial", ex.orchestrator) == \
        MODE_ROSTERS["adversarial"]["peers"]
