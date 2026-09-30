"""The task contract, validated once at dispatch (phase 2, #25).

A ``TaskContract`` states what a task is for and what must happen before it
can count: its intent, its ledger references, the scope and authority it was
granted, the capabilities it depends on, the checks and evidence it requires,
and the transitions its intent allows. It is built once when the task is
dispatched and frozen for that invocation. A changed scope or intent is a new
task with a new contract, never an edit of this one.

It references grants; it never makes them. ``authority`` records the
operator's write grant and the task's declared edit budget as they already
stand, and nothing reads it to widen access.

Phase 2 records the contract and checks it against the legacy applicability
decisions at the points they are made (``Session._contract_agrees``). Those
decisions still decide; phase 3 switches them to read the contract and
removes the duplicates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional, Tuple

INTENTS = ("audit", "implementation", "repair")


@dataclass(frozen=True)
class Required:
    """What a task must satisfy, derived once from the task and the run's
    configuration. ``design_evidence``: "harness" (the harness captures),
    "self" (the lead captures), "disabled" (the operator turned design
    verification off, which is recorded, not satisfied) or "none"."""

    checks: bool = False
    design_evidence: str = "none"
    design_review: bool = False
    security_verification: bool = False
    settlement: bool = False
    #: Design collaboration applies (``design_cross_check`` and a design
    #: task), fixed at dispatch: a cross-vendor collaborator, the render set
    #: collaborators see and the review prompt's design lens. Not the same as
    #: ``design_review``, which needs evidence (Codex, 5864370275).
    design_collaboration_applicable: bool = False
    #: The design instruction the lead is given, fixed at dispatch: "harness"
    #: (the harness captures), "self" (capture it yourself) or "none". Computed
    #: with the prompt's own predicate, not ``design_evidence``'s: a design task
    #: with no writable project has evidence "none" and is still asked for
    #: renders (O-NEXT-01, 8ab7333; Codex, 5864880543).
    design_instruction: str = "none"
    #: The security verifier's protocol, fixed at dispatch for a security
    #: task: "json" (structured verdict) or "prose"; "none" otherwise.
    #: ``security_verification`` fixes that verification happens, this fixes
    #: how it is asked and parsed (O-NEXT-10 D; Codex, 5865344590).
    security_verdict: str = "none"
    #: The operator's outer path limit applied over the task's own scope,
    #: fixed at dispatch: "none" or "sha256:<hex>" of its canonical form. It
    #: is a ceiling: a live limit may tighten a task further, never loosen it
    #: (O-NEXT-10 E; Codex, 5865344590).
    operator_limits: str = "none"
    #: Whether the requirements ledger governs this task, fixed at dispatch.
    #: A live enable adds no route; a live disable erases nothing; a
    #: disagreement is recorded and fails closed (O-NEXT-10 C; Codex,
    #: 5865627034).
    requirements_ledger: bool = False
    #: The execution tier fixed at dispatch (direct-execution slice,
    #: 2026-09-30): "normal", or "direct" for a task the orchestrator labelled
    #: TIER: direct and the deterministic admission accepted. Direct drops the
    #: collaborator review (and so the revision round) and the model
    #: close-out; every check, capture, review-of-evidence, scope and stop
    #: stays. ``tier_refused`` is why a requested direct tier was refused.
    tier: str = "normal"
    tier_refused: str = ""


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    intent: str
    #: The lead (or security worker) selected for this task, bound at the
    #: selection point, before the first model call. Never a placeholder:
    #: a task stopped before selection has no contract.
    owner: str = ""
    covers: Tuple[str, ...] = ()
    resolves: Tuple[str, ...] = ()
    continues: str = ""
    scope: Optional[str] = None           # canonical JSON of the validated TaskScope
    authority: Tuple[Tuple[str, str], ...] = ()
    capabilities: Tuple[str, ...] = ()    # readiness probes that passed at run start
    required_checks: Tuple[str, ...] = ()
    intended_state: Optional[str] = None  # canonical JSON: capture page and steps
    acceptance: Tuple[str, ...] = ()
    required: Required = field(default_factory=Required)
    allowed_next: Tuple[str, ...] = ()
    #: For a CONTINUES task: the predecessor, its intended state and the debt
    #: it left open, carried into this contract (canonical JSON).
    inherits: Optional[str] = None
    #: The full page URL (origin + path) a "harness" design instruction names,
    #: fixed at dispatch so the prompt never reads the live capture profile.
    capture_page: Optional[str] = None
    #: The capture profile a "harness" capture runs under, fixed at dispatch,
    #: as "sha256:<hex>" of its canonical form. Only the digest is recorded:
    #: the profile's env values never reach the record. The session holds the
    #: profile itself in memory (O-NEXT-13; Codex, 5865903915).
    capture_profile: Optional[str] = None

    def __post_init__(self):
        if self.intent not in INTENTS:
            raise ValueError(f"unknown task intent {self.intent!r}")

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def to_dict(self) -> dict:
        data = asdict(self)
        for name in ("scope", "intended_state", "inherits"):
            if data[name] is not None:
                data[name] = json.loads(data[name])
        data["authority"] = dict(self.authority)
        data["digest"] = self.digest
        return data


def canonical(value) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True)


def stages_for(required: Required, intent: str) -> Tuple[str, ...]:
    """The stages this contract expects, in order. A security task runs in
    its excursion: no collaborator review, a mandatory verification."""
    if required.security_verification:
        return tuple(["draft"] + (["checks"] if required.checks else []) + ["verification", "closeout"])
    stages = ["draft"] + ([] if required.tier == "direct" else ["review"])
    if required.checks:
        stages.append("checks")
    if required.design_evidence in ("harness", "self"):
        stages.append("design")
    stages.append("closeout")
    if required.settlement:
        stages.append("settlement")
    return tuple(stages)
