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
    assert seat.key == OPUS
    assert seat.reason == SeatReason.DELEGATED_SECURITY


def test_exhausted_primary_halts_the_run():
    """Fable is a hard dependency: it is the only participant that persists
    across the session, so substituting it silently changes the run."""
    with pytest.raises(OrchestratorUnavailable, match="cannot continue"):
        orchestrator_seat(available=_unavailable(FABLE))


# -- the bug this module exists to fix ---------------------------------------


def test_security_delegation_reverts_on_the_next_segment():
    """The original flaw: one security question moved the seat permanently.

    Seating is computed per segment rather than stored, so the next
    unclassified segment gets the primary back with no handback step.
    """
    during = orchestrator_seat(security_segment=True)
    after = orchestrator_seat(security_segment=False)
    assert during.key == OPUS
    assert after.key == FABLE
    assert after.is_primary


def test_security_delegation_is_marked_as_reverting():
    assert orchestrator_seat(security_segment=True).reverts_at_segment_end


def test_halt_message_tells_the_operator_what_to_do():
    with pytest.raises(OrchestratorUnavailable) as exc:
        orchestrator_seat(available=_unavailable(FABLE))
    assert "window" in str(exc.value)


def test_the_seat_never_leaves_the_primary_except_for_security():
    """The only non-primary seating path left."""
    assert orchestrator_seat().is_primary
    assert not orchestrator_seat(security_segment=True).is_primary


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
        orchestrator_seat(security_segment=True, available=_unavailable(FABLE, OPUS))


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
    assert got == SONNET


def test_security_chain_prefers_a_classifier_free_model_over_opus():
    """Sonnet carries no classifier at all, so it cannot false-positive."""
    assert SECURITY_WORK_CHAIN.index(SONNET) < SECURITY_WORK_CHAIN.index(OPUS)


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
    different jobs, and go to different models."""
    seat = orchestrator_seat(security_segment=True)
    worker = route_security_work(OPUS, work_class=WorkClass.SECURITY)
    assert seat.key == OPUS
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
    """Opus takes the seat because Fable's classifiers would refuse the
    subject; Sol does the work because that is the operator's preference."""
    ex = open_security_excursion()
    assert ex.orchestrator == OPUS
    assert ex.worker == SOL


def test_excursion_orchestrator_never_performs_its_own_work():
    ex = open_security_excursion()
    assert ex.worker != ex.orchestrator


def test_excursion_verifier_double_checks_and_is_not_the_worker():
    ex = open_security_excursion()
    assert ex.verifier != ex.worker
    assert ex.verifier == OPUS


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
    assert ex.worker == SONNET
    assert ex.worker != ex.orchestrator
    assert ex.verifier != ex.worker


def test_excursion_refuses_to_form_when_deferral_is_impossible():
    """With every deferral target down, the only candidate is the seat holder.
    Self-performing and self-checking would hand back a security answer
    carrying a verification it never received, so this stops instead."""
    with pytest.raises(ExcursionUnavailable, match="cannot be deferred"):
        open_security_excursion(available=_unavailable(SOL, SONNET))


def test_excursion_failure_names_what_to_restore():
    with pytest.raises(ExcursionUnavailable) as exc:
        open_security_excursion(available=_unavailable(SOL, SONNET))
    assert SOL in str(exc.value) and SONNET in str(exc.value)


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
