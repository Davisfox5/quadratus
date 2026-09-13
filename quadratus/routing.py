"""Seat assignment and work routing.

Two problems live here, and they share a fix.

**The orchestrator seat.** Fable 5 holds it, and yields it for exactly two
reasons: a security-classified segment, where its classifiers would refuse to
discuss the project it is supervising, and its own unavailability. An earlier
design handed the seat over as a one-way assignment, which meant nothing ever
handed it back -- once a single security question moved the seat, the deputy
kept it for the rest of the session.

The fix is to stop storing who holds the seat. :func:`orchestrator_seat`
computes the holder from the segment in front of it, so reversion is not a
mechanism that has to fire; it is what happens by default on the next segment
that is not security-classified. There is no state to get stuck.

Continuity across a yielded segment is carried by the ledger rather than by
the model's context: decisions made while Fable is out land in the ledger,
and Fable reads them when it resumes. That is the same append-only structure
the compaction design already needs, doing double duty.

Both yield reasons land on the same deputy, because there is only one:
:data:`quadratus.registry.ORCHESTRATOR_CHAIN` is two models long by operator
directive, Fable and GPT-6 Astra, and no capable third model is quietly
appended. Opus 5 could hold the seat and deliberately does not -- it is a
brain-trust peer and half the pinned reviewer pair, and those roles are worth
more to the run than a second understudy. When neither seat is available the
run stops.

An earlier version of this module halted the run whenever the *primary* was
unavailable, on the reasoning that the orchestrator is the only participant
that persists -- it holds the goal, the policy it authored, and the thread of
what has been decided -- so substituting it would quietly turn the run into a
different run. The reasoning was right about the risk and wrong about where
it bit. "Quietly" was the problem: a substitution that is *recorded* -- the
seat carries :data:`SeatReason.FALLBACK_UNAVAILABLE`, and the ledger says the
seat changed hands and when -- is visible to the operator without costing
them a session with work in flight. So there is one recorded substitution,
and then a full stop.

Continuity across that substitution survives because it was never held in the
model's context. The ledger is append-only, the invariants are re-emitted
verbatim on every render, and the deputy reads the same rendered state the
primary would have. That is the same mechanism a security excursion already
relies on.

Reversion needs no mechanism. The seat is computed per segment rather than
stored, so the segment after the window rolls over seats the primary again by
default.

**Work routing.** Separately from who supervises, security-classified *work*
is routed by operator preference to GPT-5.6 Sol. This is experience-based
rather than benchmark-based, which is the strongest evidence available: no
security-specific capability or refusal data exists publicly for OpenAI's
models, so nothing published could adjudicate it either way. The corollary is
that Sol's own refusal behaviour on security work is undocumented -- an
unknown, not a clean bill of health -- so the chain continues past it.

Routing is not a capability bypass. Exploit generation and penetration
testing are gated by Anthropic's Cyber Verification Program regardless of
which model receives the request, and moving a request between models does
not unlock them. This machinery exists to stop a false positive on
legitimate, authorized work from stalling a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from .registry import ORCHESTRATOR_CHAIN, resolve

__all__ = [
    "SeatReason",
    "Seat",
    "orchestrator_seat",
    "OrchestratorUnavailable",
    "SECURITY_WORK_CHAIN",
    "route_security_work",
    "WorkClass",
    "cross_family_verifier",
    "Excursion",
    "ExcursionUnavailable",
    "open_security_excursion",
    "close_excursion",
]


class ExcursionUnavailable(RuntimeError):
    """No excursion can be formed that both defers the work and verifies it."""


class OrchestratorUnavailable(RuntimeError):
    """Nobody in the orchestrator chain can take the seat.

    Still fatal, and now narrow: a run with no orchestrator is not a run.
    What it no longer means is that the *primary* is down -- that is covered
    by the chain, loudly, with the substitution recorded on the seat. See the
    module docstring for why the halt was narrowed rather than kept.
    """


class WorkClass:
    """Classification of a work item, produced by the spec or the classifier."""

    GENERAL = "general"
    SECURITY = "security"


class SeatReason:
    """Why the current holder has the orchestrator seat."""

    #: The preferred orchestrator is seated.
    PRIMARY = "primary"
    #: Yielded for this segment only because the work is security-classified.
    DELEGATED_SECURITY = "delegated-security"
    #: The model ahead of this one in the chain could not be reached -- an
    #: exhausted subscription window, a withdrawn model, a CLI that is not
    #: signed in. Recorded rather than silent: a run that changed hands is a
    #: different run, and the operator is entitled to know it happened without
    #: having to lose the session to find out.
    FALLBACK_UNAVAILABLE = "fallback-unavailable"


@dataclass(frozen=True)
class Seat:
    """Who orchestrates right now, and why.

    Immutable on purpose. A seat is recomputed per segment rather than
    mutated, which is what makes reversion automatic instead of a mechanism
    someone has to remember to trigger.
    """

    key: str
    reason: str
    #: True when this holder gives the seat back at the end of the segment.
    #: Always true for a security deputy, which is now the only way anyone
    #: other than the primary holds the seat at all.
    reverts_at_segment_end: bool

    @property
    def is_primary(self) -> bool:
        return self.reason == SeatReason.PRIMARY


def _always_available(_key: str) -> bool:
    return True


def orchestrator_seat(
    *,
    security_segment: bool = False,
    available: Callable[[str], bool] = _always_available,
    chain: Optional[List[str]] = None,
) -> Seat:
    """Compute who orchestrates the segment in front of us.

    Args:
        security_segment: True when the current segment is security-classified.
            The preferred orchestrator is skipped for the duration, because
            Anthropic documents that benign cybersecurity work can trigger
            Fable's classifiers, and a supervisor that declines to discuss the
            project it supervises is worse than a slightly weaker one.
        available: Liveness predicate, called per candidate. Should report
            False for an exhausted subscription window or an unreachable
            model. Called lazily, so an expensive check costs nothing when the
            primary is fine.
        chain: Override the seat preference order, primary first. A security
            segment deputises from its tail, so one override covers both.

    Returns:
        The :class:`Seat` for this segment.

    Raises:
        OrchestratorUnavailable: when no one in the chain can take the seat.
        ExcursionUnavailable: if a security segment has no deputy available.
    """
    order = list(chain if chain is not None else ORCHESTRATOR_CHAIN)
    if not order:
        raise ValueError("orchestrator chain is empty")

    primary = order[0]

    if not security_segment:
        if available(primary):
            return Seat(primary, SeatReason.PRIMARY, reverts_at_segment_end=False)
        # The primary is out. Substituting is a real cost -- the seat holds
        # the thread of the run -- but it is a cost the ledger already pays
        # for, and the alternative is ending a session with work in flight.
        # The substitution is recorded on the seat rather than being silent,
        # and it lapses on its own: the next segment recomputes and seats the
        # primary again the moment it is reachable.
        # One recorded substitution, not a cascade. The chain is short on
        # purpose (see ORCHESTRATOR_CHAIN); this loop honours whatever length
        # it has rather than assuming two.
        for key in order[1:]:
            if available(key):
                return Seat(
                    key,
                    SeatReason.FALLBACK_UNAVAILABLE,
                    # Not scoped to this segment -- there is nothing to hand
                    # back to while the primary is down. Reversion happens by
                    # recomputation, not by a flag.
                    reverts_at_segment_end=False,
                )
        raise OrchestratorUnavailable(
            f"{primary} is unavailable and so is its fallback "
            f"({', '.join(order[1:]) or 'none configured'}). The run stops "
            f"here rather than seating a model that has other work to do: "
            f"wait for a subscription window to roll over, sign the CLI back "
            f"in, and resume the session once one of them is reachable."
        )

    # A security segment skips the primary by design: its classifiers would
    # refuse to discuss the subject it is supervising. The deputy holds the
    # seat for that thread only. It deputises from the same order as an
    # unavailable primary does, because the two questions happen to have the
    # same answer -- see ORCHESTRATOR_CHAIN for both reasons.
    bench = order[1:]
    for key in bench:
        if available(key):
            return Seat(key, SeatReason.DELEGATED_SECURITY, reverts_at_segment_end=True)

    raise ExcursionUnavailable(
        f"no deputy is available to take a security segment; "
        f"{primary} cannot, and every alternative ({', '.join(bench) or 'none'}) "
        f"is down."
    )


#: Where security-classified work goes, in preference order.
#:
#: Sol leads on operator preference from direct experience. Opus 5 anchors the
#: chain: it is not the operator's preference here, but its classifier fires
#: far less often than Fable's and something has to be able to take the work.
#:
#: Sonnet sat between them until 2026-09-12, on the reasoning that it carries
#: no request-declining classifier at all and so cannot false-positive on
#: authorized work. Removed by operator directive: Sonnet is a worker-tier
#: model and holds no role in security, verification, or the brain trust. The
#: classifier argument was an argument for *not being refused*, which is not
#: the same as an argument for being trusted with the work -- and the
#: excursion already routes around a refusal by other means. Do not re-add it
#: here on the strength of the classifier tag.
SECURITY_WORK_CHAIN: List[str] = [
    "openai:gpt-5.6-sol",
    "claude:opus",
]


def route_security_work(
    default_key: str,
    *,
    work_class: str = WorkClass.GENERAL,
    available: Callable[[str], bool] = _always_available,
) -> str:
    """Return the model that should handle this work item.

    General work keeps whatever routing already selected. Security-classified
    work is diverted to :data:`SECURITY_WORK_CHAIN` even when the default
    would otherwise be capable, because the operator's measured preference
    outranks the seat's convenience.
    """
    if work_class != WorkClass.SECURITY:
        return default_key
    for key in SECURITY_WORK_CHAIN:
        if resolve(key) is not None and available(key):
            return key
    return SECURITY_WORK_CHAIN[-1]


def cross_family_verifier(
    author: str,
    *,
    candidates: List[str],
    available: Callable[[str], bool] = _always_available,
) -> Optional[str]:
    """Pick a verifier from a different vendor family than the author.

    Same-vendor verification shares the author's training lineage, its blind
    spots, and often its opinions -- Blitzy's audited SWE-Bench Pro record was
    built specifically on one model family checking another's work, and the
    independent auditors credited the harness, not the models. So verification
    crosses vendor lines whenever a candidate exists; returns None when none
    does, which the caller must surface rather than quietly self-verifying.
    """
    author_spec = resolve(author)
    author_provider = author_spec.provider if author_spec else author.split(":", 1)[0]
    for key in candidates:
        spec = resolve(key)
        if key == author or spec is None:
            continue
        if spec.provider != author_provider and available(key):
            return key
    return None


@dataclass(frozen=True)
class Excursion:
    """A bounded side-thread that briefly moves the orchestrator seat.

    A security question arriving mid-session should not reshape the run. It is
    peeled off into an excursion: the fallback orchestrator takes the seat for
    this thread only, hands the work to the model the operator prefers for it,
    verifies the result, writes the outcome to the ledger, and closes. The
    primary orchestrator resumes on the next segment and reads the outcome
    from the ledger rather than from a context it was absent for.

    Two properties make this safe enough to leave the brain trust untouched.
    The excursion is short -- one delegation and one verification, not a
    debate -- so the acting orchestrator is routing rather than judging peers
    it competes with. And it is closed-ended by construction: ``close_excursion``
    is the only way it ends, so a session cannot silently continue under the
    fallback the way an open-ended handover allowed.
    """

    reason: str
    #: Holds the seat for the excursion only.
    orchestrator: str
    #: Performs the work. Never the acting orchestrator: deferring is the point.
    worker: str
    #: Double-checks the worker's output. Never the worker.
    verifier: str
    #: What the seat returns to when this closes.
    returns_to: str

    def __post_init__(self) -> None:
        if self.worker == self.orchestrator:
            raise ValueError(
                "the acting orchestrator must defer the work, not perform it"
            )
        if self.verifier == self.worker:
            raise ValueError("a worker cannot double-check its own output")


def open_security_excursion(
    *,
    available: Callable[[str], bool] = _always_available,
    chain: Optional[List[str]] = None,
) -> Excursion:
    """Peel a security question off into its own bounded thread.

    The seat moves to the fallback because the primary's classifiers would
    refuse to discuss the subject; the work moves to the operator's preferred
    security model; and the acting orchestrator verifies rather than performs,
    so the deferral is real and the result is checked by a second model.
    """
    order = list(chain if chain is not None else ORCHESTRATOR_CHAIN)
    seat = orchestrator_seat(security_segment=True, available=available, chain=order)
    worker = route_security_work(
        seat.key, work_class=WorkClass.SECURITY, available=available
    )
    if worker == seat.key or not available(worker):
        # Nobody is left to hand the work to. Deferral and independent
        # verification are the two things this excursion exists to guarantee,
        # and neither survives here. Fabricating an excursion that quietly
        # self-performs, or one whose named worker cannot answer, would be
        # worse than stopping: the operator would see a security answer
        # carrying a verification it never actually received.
        #
        # Two ways to arrive: the only candidate left is the model already
        # holding the seat, or the chain is down to the last resort
        # ``route_security_work`` returns when everything is unavailable --
        # which it returns rather than raising, correctly for its own caller
        # and so has to be caught here. The second case became reachable when
        # the deputy stopped being a member of SECURITY_WORK_CHAIN; operator
        # decision, 2026-09-12, to refuse rather than let the invocation fail
        # later at transport.
        raise ExcursionUnavailable(
            f"security work cannot be deferred: nothing in "
            f"SECURITY_WORK_CHAIN is available to take it "
            f"({', '.join(k for k in SECURITY_WORK_CHAIN if k != seat.key)}). "
            f"Restore one of them or handle this item outside the run."
        )
    # The acting orchestrator double-checks. It did not author the answer, so
    # this is review rather than self-review.
    verifier = seat.key
    return Excursion(
        reason=SeatReason.DELEGATED_SECURITY,
        orchestrator=seat.key,
        worker=worker,
        verifier=verifier,
        returns_to=order[0],
    )


def close_excursion(excursion: Excursion) -> Seat:
    """End the excursion and return the seat to the primary orchestrator.

    Deliberately unconditional. The failure this exists to prevent is a
    session that degrades once and never recovers, so closing is not
    contingent on the excursion having succeeded -- a failed security lookup
    still ends the excursion, records that it failed, and hands the seat back.
    """
    return Seat(
        excursion.returns_to,
        SeatReason.PRIMARY,
        reverts_at_segment_end=False,
    )
