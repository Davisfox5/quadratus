"""Seat assignment and work routing.

Two problems live here, and they share a fix.

**The orchestrator seat.** Fable 5 holds it, but has to yield it in two very
different situations: a security-classified segment, where its classifiers
would refuse to discuss the project it is supervising; and window exhaustion
or an outage, where it simply is not there. An earlier design handed the seat
over as a one-way assignment, which meant nothing ever handed it back -- once
a single security question moved the seat to the fallback, the fallback kept
it for the rest of the session.

The fix is to stop storing who holds the seat. :func:`orchestrator_seat`
computes the holder from the segment in front of it, so reversion is not a
mechanism that has to fire; it is what happens by default on the next segment
that is not security-classified. There is no state to get stuck.

Continuity across a yielded segment is carried by the ledger rather than by
the model's context: decisions made while Fable is out land in the ledger,
and Fable reads them when it resumes. That is the same append-only structure
the compaction design already needs, doing double duty.

The two yield reasons revert differently, which is why they are distinct:

* A security delegation is scoped to one segment and reverts at its end.
* An availability fallback persists -- a spent window does not refill because
  the next work item is about CSS -- and reverts only when a liveness check
  says the primary is back. Claude subscription windows roll over on their own
  schedule, so this is worth re-checking periodically rather than once.

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
    "SECURITY_WORK_CHAIN",
    "route_security_work",
    "WorkClass",
]


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
    #: Yielded because the preferred orchestrator is unavailable.
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
    #: Availability fallbacks do not: a spent window does not refill because
    #: the next work item happens to be unclassified.
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
        chain: Override the seat preference order, primary first.

    Returns:
        The :class:`Seat` for this segment. Never None: the last chain entry
        is seated even if it reports unavailable, because a run with a
        degraded orchestrator is recoverable and a run with none is not.
    """
    order = list(chain if chain is not None else ORCHESTRATOR_CHAIN)
    if not order:
        raise ValueError("orchestrator chain is empty")

    primary = order[0]

    # A security segment skips the primary regardless of its availability,
    # then falls through the same liveness filtering as anyone else.
    candidates = order[1:] if security_segment else order

    for key in candidates:
        if not available(key):
            continue
        if key == primary:
            return Seat(key, SeatReason.PRIMARY, reverts_at_segment_end=False)
        reason = (
            SeatReason.DELEGATED_SECURITY
            if security_segment
            else SeatReason.FALLBACK_UNAVAILABLE
        )
        # A security delegation is scoped to the segment. An availability
        # fallback is not, and is re-tested when liveness is re-checked.
        return Seat(
            key,
            reason,
            reverts_at_segment_end=(reason == SeatReason.DELEGATED_SECURITY),
        )

    # Everything reported unavailable. Seat the last resort rather than
    # stalling: the caller can surface the degradation, but a run with no
    # orchestrator cannot make progress at all.
    last = candidates[-1] if candidates else primary
    return Seat(last, SeatReason.FALLBACK_UNAVAILABLE, reverts_at_segment_end=False)


#: Where security-classified work goes, in preference order.
#:
#: Sol leads on operator preference from direct experience. Sonnet 5 follows
#: because it carries no request-declining classifier at all, so it cannot
#: false-positive on authorized work. Opus 5 anchors the chain: it is not the
#: operator's preference here, but its classifier fires far less often than
#: Fable's and something has to be able to take the work.
SECURITY_WORK_CHAIN: List[str] = [
    "openai:gpt-5.6-sol",
    "claude:sonnet",
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
