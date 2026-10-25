"""Reasoning effort per call, decided from what the harness already knows
(2026-10-24). No model classifies the task; the inputs are the role, the
task's declared difficulty and kind, whether the work is security work,
which fix cycle it is, and the worst impact a recheck is about. The output
is one level on the vendor's own ladder, with the reasons, under one policy
version, recorded on every invocation.

The shape follows the vendors' guidance, read for the CLIs this repo drives
rather than their APIs: OpenAI's deployment checklist (low for extraction,
routing and simple work; medium or high for code, planning and diagnosis;
the top levels only when justified) and its maintenance write-up (higher
effort finds more at greater cost, task by task, not universally); Claude
Code's model-config and effort pages (explicit effort per call; changing it
inside one conversation invalidates the cache, so a seat that persists holds
its level); xAI's reasoning page (effort per model; the multi-agent flavour
is a different agent count, never a role change).

Three rules are not negotiable here:

* A bounded seat (a worker tier, a restricted roster row) keeps the effort
  its roster row fixes. The row is the seat; the policy does not re-open it.
* ``ultra`` is never selected. The Codex catalogue describes it as
  automatic task delegation, which would widen the worker tree in silence.
* Nothing is lowered to save tokens on a judging seat: reviews, rechecks and
  the final audit start at ``medium`` and rise with difficulty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

__all__ = [
    "POLICY_VERSION",
    "EffortDecision",
    "LEVELS",
    "ORDER",
    "VENDOR_LADDERS",
    "ladder_for",
    "select",
    "validate",
]

POLICY_VERSION = "effort/1"
#: The policy's own scale, lowest first. ``ultra`` is deliberately absent.
LEVELS: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
ORDER = {level: index for index, level in enumerate(LEVELS)}
#: The ladders the CLIs document. Codex takes its ladder per model from the
#: installed catalogue (quadratus.catalog); this entry is the documented
#: client scale used only when a resolution carries no per-model ladder.
VENDOR_LADDERS: Mapping[str, Tuple[str, ...]] = {
    "claude": ("low", "medium", "high", "max"),
    "openai": ("low", "medium", "high", "xhigh", "max"),
    "grok": ("low", "medium", "high", "xhigh"),
}

_HARD_KINDS = frozenset({"security", "concurrency", "perf", "architect", "iac"})
_IMMEDIATE = frozenset({"data-loss", "integrity", "security", "irreversible"})
_PLANNING_ROLES = frozenset({"orchestrator", "decide"})
_JUDGING_ROLES = frozenset({"collaborator", "recheck", "design-review", "verifier", "auditor"})
_EDITING_ROLES = frozenset({"lead", "revision", "gate-fix", "security-fix", "design-fix"})
_DECLARATION_ROLES = frozenset({"capture-redeclare", "fixture-supply"})


@dataclass(frozen=True)
class EffortDecision:
    level: str
    reasons: Tuple[str, ...]
    policy: str = POLICY_VERSION
    #: The level the policy wanted before the ladder bounded it, when the
    #: two differ; None when they agree.
    wanted: Optional[str] = None
    #: True when the roster row fixed the level (a bounded seat).
    seat_fixed: bool = False

    def to_dict(self) -> dict:
        return dict(level=self.level, reasons=list(self.reasons), policy=self.policy, wanted=self.wanted,
                    seat_fixed=self.seat_fixed)


def ladder_for(vendor: str, supported: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    """The levels a call may ask for. ``supported`` None means the
    resolution carries no ladder: the vendor's documented scale shapes the
    policy's choice, and :func:`validate` then refuses the call, because an
    unknown ladder is not permission. A listed ladder (``ultra`` removed) is
    used as listed, and a listed-but-empty one stays empty."""
    if supported is None:
        return tuple(VENDOR_LADDERS.get(str(vendor or "").lower(), ("low", "medium", "high")))
    listed = {str(s).lower() for s in supported}
    return tuple(level for level in LEVELS if level in listed)


def validate(level: str, vendor: str, supported: Optional[Sequence[str]] = None) -> str:
    """Why ``level`` cannot be sent to this vendor or model, or "". ``ultra``
    is refused outright; a level outside the ladder is refused by name."""
    want = str(level or "").strip().lower()
    if not want:
        return "no effort level selected"
    if want == "ultra":
        return "ultra is never sent: the catalogue describes it as automatic task delegation"
    if supported is None and str(vendor or "").lower() not in VENDOR_LADDERS:
        return f"the {vendor} effort ladder is unknown; nothing can be validated"
    ladder = ladder_for(vendor, supported)
    if not ladder:
        return f"the resolved {vendor} model lists no usable effort; nothing can be sent at an explicit effort"
    if want not in ladder:
        return f"{want} is not on the {vendor} ladder ({', '.join(ladder)})"
    return ""


def _bounded(level: str, ladder: Sequence[str]) -> str:
    """The highest ladder level at or below ``level`` (never above it)."""
    ranked = [step for step in ladder if step in ORDER]
    below = [step for step in ranked if ORDER[step] <= ORDER.get(level, 0)]
    return max(below, key=ORDER.get) if below else min(ranked, key=ORDER.get)


def _raise(level: str, steps: int = 1) -> str:
    """One step up the policy scale, capped at ``high``: the levels above it
    are reached only by the elevated rule."""
    index = min(ORDER.get(level, 0) + steps, ORDER["high"])
    return LEVELS[index]


def select(*, role: str, vendor: str, complexity: Optional[str] = None, kind: Optional[str] = None,
           work_class: Optional[str] = None, security: bool = False, cycle: int = 0,
           restricted: bool = False, seat_effort: str = "", impact: Optional[str] = None,
           supported: Optional[Sequence[str]] = None) -> EffortDecision:
    """One explicit level for one call, with its reasons.

    ``cycle`` is the fix cycle an editing or recheck call belongs to (0 for
    the draft and the first review, 1 for the first revision and recheck,
    2 and beyond for further cycles). ``impact`` is the worst impact among
    the findings a recheck is about. ``supported`` is the resolved model's
    own ladder when the catalogue gave one.
    """
    role = str(role or "direct").lower()
    vendor = str(vendor or "").lower()
    ladder = ladder_for(vendor, supported)
    reasons = []
    hard = security or str(work_class or "").lower() == "security" or str(kind or "").lower() in _HARD_KINDS
    complex_ = str(complexity or "").lower() == "complex"
    if restricted or seat_effort and role == "worker":
        level = str(seat_effort or "low").lower()
        reasons.append("bounded seat: the roster row fixes its effort")
        if level not in ladder:
            # Not lowered here: a roster effort the model's ladder lacks is a
            # configuration fault, refused by validate(), never papered over.
            reasons.append(f"{level} is not on the {vendor} ladder ({', '.join(ladder)}); refused, not lowered")
        return EffortDecision(level, tuple(reasons), seat_fixed=True)
    if role in _PLANNING_ROLES and role == "decide":
        level, why = "low", "routing label from stated metadata (extraction-class work)"
    elif role in _PLANNING_ROLES:
        level, why = "high", "planning and decomposition; held for the whole session so the seat's cache is kept"
    elif role in ("requirements-review", "consultant"):
        level, why = "medium", f"one bounded {role} reading"
    elif role == "interview":
        level, why = "low", "the interviewer asks what it is given; the orchestrator decides"
    elif role == "worker":
        level, why = "low", "worker errand"
    elif role in _DECLARATION_ROLES:
        level, why = "low", "one declared line, read from the copy"
    elif role == "auditor":
        level, why = "high", "the final audit judges every requirement independently"
    elif role == "verifier":
        level, why = "high", "cross-vendor verification of security work"
    elif role in _JUDGING_ROLES:
        if complex_ or hard or str(kind or "").lower() == "review":
            level, why = "high", "difficult review: complex, security-grade or review-kind work"
        else:
            level, why = "medium", "review floor: independent judgment is never bought cheaply"
        if role == "recheck" and (cycle >= 2 or (impact or "").lower() in _IMMEDIATE):
            level = max(level, "high", key=ORDER.get)
            why += "; a repeated or immediate-risk recheck rises to high"
    elif role in _EDITING_ROLES or role == "lead":
        # Risk outranks the size label: a rote task on security-grade work
        # is security-grade work.
        if complex_ or hard:
            level, why = "high", "complex or security-grade implementation"
        elif str(complexity or "").lower() == "rote":
            level, why = "low", "rote task: mechanical, lead alone"
        else:
            level, why = "medium", "ordinary implementation"
        if role != "lead" and cycle >= 2 and level != "high":
            level = _raise(level)
            why += f"; fix cycle {cycle}: the earlier correction did not settle the finding, one step up"
    else:
        level, why = "medium", f"unclassified role {role!r}: the ordinary level, never the CLI's own default"
    reasons.append(why)
    # The one elevated rule: complex work carrying an immediate risk or a
    # security grade that a first correction did not settle, on a ladder
    # that has a level above high. Evidence, not enthusiasm.
    if level == "high" and complex_ and (hard or (impact or "").lower() in _IMMEDIATE) and cycle >= 2:
        above = [step for step in ladder if ORDER.get(step, 0) > ORDER["high"]]
        if above:
            level = min(above, key=ORDER.get)
            reasons.append(f"elevated to {level}: complex, security or immediate-risk work still open at fix cycle "
                           f"{cycle}, and this ladder has a level above high")
        else:
            reasons.append("elevated work, but this ladder has no level above high; high stands")
    wanted = None
    if level not in ladder and ORDER.get(level, 0) > ORDER["high"]:
        # Only an elevated level is bounded, and only down to high: the base
        # levels are floors, and a ladder that lacks one is refused by
        # validate() rather than quietly served below the policy.
        wanted = level
        level = _bounded(level, ladder)
        reasons.append(f"{wanted} is not on the {vendor} ladder ({', '.join(ladder)}); bounded to {level}")
    return EffortDecision(level, tuple(reasons), wanted=wanted)
