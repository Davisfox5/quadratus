"""Typed delivery records and their pure transitions (blueprint stage A+B).

Consolidated execution blueprint (Davis, 2026-10-17): the controller owns a
small pure transition function over typed records, and ``session.py`` is the
adapter that invokes models and runners and records what came back. This
module holds the records and the transitions. It performs no I/O, calls no
model, reads no file and decides nothing a model is responsible for; every
function here is ``(records, validated event) -> new records``.

What is here, and what each one refuses:

- :class:`RequirementRevision`: a requirement's exact approved text at one
  revision, with its origin (what the user said, what was inferred, what the
  source showed). An inferred row with no decision receipt is unapproved and
  says so; nothing promotes an origin.
- :class:`Finding` and :class:`Disposition`: a reviewer's finding with a
  stable id, its basis (requirement, reproduction, evidence or none) and its
  claimed impact, and the history of judgments on it. Only the designated
  judge settles it (the seat that raised it, in a judging role, or the
  final audit for a concern carried to it), on the binding it was handed:
  a RESOLVED needs the judge's own account bound to that identity, an
  INVALIDATED needs checkable evidence, and nothing requires the source to
  have changed. An immediate risk (data loss, integrity, security, an
  irreversible effect) holds the affected effect whatever its basis; a
  basis-less concern that is not an immediate risk is carried to the audit by
  id, never dropped as a note.
- :class:`EvidenceObligation`: a behavior that must be established, with
  ``execution`` (did the right thing run) and ``sufficiency`` (does what ran
  establish the behavior) as two fields. A receipt discharges execution only
  when source, gate, runner, environment, the current requirement revisions,
  named cases and completed execution all match; sufficiency comes only from
  a named independent seat and is bound to exactly the evidence it judged,
  so a new receipt, source or requirement text makes it unjudged again.
- :class:`OperationRecord`: one external effect with explicit prepared,
  started, settled and uncertain states; a started operation that did not
  settle is uncertain, never assumed absent and never resumed by this module.
- :class:`PlanStage` and :func:`admit_plan`: the whole remaining path costed
  in the budget's own exposure classes. The exposure figures are inputs from
  ``RunBudget`` semantics; a stage holding the write grant is always exposed
  at the editing figure. This module invents no cheaper class.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .judgment import (
    INVALID,
    INVALIDATED,
    RESOLVED,
    UNRESOLVED,
    RecheckReading,
    checkable_reference,
)

__all__ = [
    "ORIGINS", "RequirementRevision",
    "BASIS_KINDS", "IMMEDIATE_RISKS", "IMPACTS", "JUDGING_ROLES", "FindingBasis", "Disposition",
    "Finding", "designated_judge", "apply_reading", "finding_from_line",
    "EXECUTION_STATES", "SUFFICIENCY_STATES", "EvidenceObligation", "Discharge", "discharge",
    "refresh", "judge_sufficiency",
    "OPERATION_STATES", "OperationRecord", "start_operation", "settle_operation",
    "uncertain_operation",
    "EXPOSURE_CLASSES", "PlanStage", "PlanVerdict", "admit_plan",
]

_ID = re.compile(r"[A-Z][A-Z0-9]*-?[0-9]+\Z")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _check_id(value: str, what: str) -> None:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{what} must look like R1, F12 or O-3, not {value!r}")


def _count(value, what: str, *, minimum: int = 0) -> int:
    """A plain non-negative integer: never a bool, a float or a string."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{what} must be an integer of at least {minimum}, not {value!r}")
    return value


def _amount(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{what} must be a finite non-negative number, not {value!r}")
    return float(value)


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be non-empty text")
    return value


# --- requirements ---------------------------------------------------------

ORIGINS = ("user-said", "inferred", "source-derived")


@dataclass(frozen=True)
class RequirementRevision:
    """One requirement's exact text at one revision. ``origin`` never changes:
    an inferred row stays inferred after the human accepts it; the acceptance
    is the ``decision_receipt``, and the row is ``approved`` only with one."""

    rid: str
    revision: int
    text: str
    origin: str
    decision_receipt: Optional[str] = None
    brief_version: int = 0
    impacts: Tuple[str, ...] = ()

    def __post_init__(self):
        _check_id(self.rid, "requirement id")
        _count(self.revision, "revision", minimum=1)
        _text(self.text, f"{self.rid} revision {self.revision} text")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}, not {self.origin!r}")
        _count(self.brief_version, "brief_version")

    @property
    def text_digest(self) -> str:
        return hashlib.sha256(self.text.strip().encode()).hexdigest()

    @property
    def key(self) -> Tuple[str, int]:
        return (self.rid, self.revision)

    @property
    def approved(self) -> bool:
        """User-said text is approved by being said; the other origins need
        the human's decision receipt."""
        return self.origin == "user-said" or bool(self.decision_receipt)

    def to_dict(self) -> dict:
        return dict(asdict(self), text_digest=self.text_digest, approved=self.approved)


# --- findings -------------------------------------------------------------

BASIS_KINDS = ("requirement", "reproduction", "evidence", "none")
#: Impact categories that hold the affected effect before it happens, whatever
#: the finding's basis (blueprint section 4: never deferred blindly to audit).
IMMEDIATE_RISKS = frozenset({"data-loss", "integrity", "security", "irreversible"})
IMPACTS = IMMEDIATE_RISKS | {"correctness", "contract", "other", "unstated"}
DISPOSITION_STATES = (RESOLVED, UNRESOLVED, INVALIDATED, "unjudged")
#: The roles in which a seat judges a finding. ``recheck`` and
#: ``reassessment`` belong to the seat that raised it; ``audit`` to the final
#: auditor, and only for a concern carried to the audit.
JUDGING_ROLES = ("recheck", "reassessment", "audit")


@dataclass(frozen=True)
class FindingBasis:
    kind: str
    ref: str = ""

    def __post_init__(self):
        if self.kind not in BASIS_KINDS:
            raise ValueError(f"basis kind must be one of {BASIS_KINDS}, not {self.kind!r}")
        if self.kind != "none" and not self.ref.strip():
            raise ValueError(f"a {self.kind} basis names what it rests on")


@dataclass(frozen=True)
class Disposition:
    """One judgment on one finding, by one seat, on one bound identity."""

    fid: str
    state: str
    why: str
    author: str
    role: str
    binding: str
    source_digest: str = ""
    call: str = ""

    def __post_init__(self):
        if self.state not in DISPOSITION_STATES:
            raise ValueError(f"disposition state must be one of {DISPOSITION_STATES}")
        _text(self.author, "disposition author")
        _text(self.binding, "disposition binding")
        _text(self.role, "disposition role")
        if self.state != "unjudged" and self.role not in JUDGING_ROLES:
            # A refused attempt by a non-judging role is recorded as unjudged;
            # a judgment itself comes only from a judging role.
            raise ValueError(f"disposition role must be one of {JUDGING_ROLES}, not {self.role!r}")


@dataclass(frozen=True)
class Finding:
    fid: str
    author: str
    role: str
    claim: str
    basis: FindingBasis
    impact: str
    source_digest: str
    task: str = ""
    requirement: Optional[Tuple[str, int]] = None
    history: Tuple[Disposition, ...] = ()

    def __post_init__(self):
        _check_id(self.fid, "finding id")
        _text(self.claim, f"{self.fid} claim")
        _text(self.author, f"{self.fid} author")
        _text(self.source_digest, f"{self.fid} source")
        if self.impact not in IMPACTS:
            raise ValueError(f"{self.fid} impact must be one of {sorted(IMPACTS)}, not {self.impact!r}")
        if self.requirement is not None:
            _check_id(self.requirement[0], "requirement id")
            _count(self.requirement[1], "requirement revision", minimum=1)

    @property
    def state(self) -> str:
        """``open`` until a disposition other than ``unjudged`` lands."""
        for item in reversed(self.history):
            if item.state != "unjudged":
                return item.state
        return "open"

    @property
    def closed(self) -> bool:
        return self.state in (RESOLVED, INVALIDATED)

    @property
    def holds_effects(self) -> bool:
        """An open immediate risk holds the affected effect, basis or not."""
        return not self.closed and self.impact in IMMEDIATE_RISKS

    @property
    def audit_carried(self) -> bool:
        """Open, no basis, not an immediate risk: examined by the audit by id,
        never a note and never an accepted task."""
        return not self.closed and self.basis.kind == "none" and not self.holds_effects

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(state=self.state, closed=self.closed, holds_effects=self.holds_effects,
                    audit_carried=self.audit_carried)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "Finding":
        basis = data.get("basis") or {}
        requirement = data.get("requirement")
        return cls(fid=data["fid"], author=data["author"], role=data.get("role", ""), claim=data["claim"],
                   basis=FindingBasis(basis.get("kind", "none"), basis.get("ref", "")),
                   impact=data.get("impact", "unstated"), source_digest=data["source_digest"],
                   task=data.get("task", ""),
                   requirement=(requirement[0], int(requirement[1])) if requirement else None,
                   history=tuple(Disposition(**{k: v for k, v in h.items() if k in Disposition.__dataclass_fields__})
                                 for h in data.get("history") or ()))


def designated_judge(finding: Finding, author: str, role: str) -> str:
    """Why ``author`` in ``role`` may not judge ``finding``, or "". The seat
    that raised a finding rechecks and reassesses it; the audit seat judges
    only a concern carried to it; nobody else, whatever they write."""
    if role not in JUDGING_ROLES:
        return f"{role!r} is not a judging role"
    if role == "audit":
        return "" if finding.audit_carried else f"{finding.fid} is not carried to the audit"
    if author != finding.author:
        return f"{finding.fid} was raised by {finding.author}; {author} does not judge it"
    return ""


_SEGMENT = re.compile(r"\s*\|\s*(basis|impact)\s*:\s*", re.IGNORECASE)


def finding_from_line(fid: str, line: str, *, author: str, role: str, source_digest: str, task: str = "",
                      requirement: Optional[Tuple[str, int]] = None) -> Finding:
    """A finding from one reviewer line: ``<claim> | basis: <ref> | impact:
    <impact>``. The segments are optional; an absent basis is ``none`` and an
    absent impact ``unstated``, recorded as such. A basis names a
    requirement id (``requirement``), a command or check (``reproduction``)
    or a receipt, test or file (``evidence``); an unrecognised reference is
    kept as ``evidence`` text, never promoted."""
    parts = _SEGMENT.split(line.strip())
    claim = parts[0].strip()
    fields = {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    ref = fields.get("basis", "")
    if not ref or ref.lower() in ("none", "n/a", "-"):
        basis = FindingBasis("none")
    elif re.fullmatch(r"R-?\d+(?:@\d+)?", ref, re.IGNORECASE):
        basis = FindingBasis("requirement", ref.upper())
    elif re.match(r"(?:`|\$ |pytest|python|node|npm|curl|make)\b", ref):
        basis = FindingBasis("reproduction", ref)
    else:
        basis = FindingBasis("evidence", ref)
    impact = fields.get("impact", "").lower().replace("_", "-").replace(" ", "-")
    impact = impact if impact in IMPACTS else "unstated"
    return Finding(fid, author, role, claim or line.strip(), basis, impact, source_digest, task, requirement)


def _next_number(findings: Iterable[Finding], prefix: str) -> int:
    """The next free number after ``prefix``: only the digits after the
    prefix count, so a task's own digits (RFT2-1) never inflate it."""
    numbers = []
    for finding in findings:
        if finding.fid.startswith(prefix) and finding.fid[len(prefix):].isdigit():
            numbers.append(int(finding.fid[len(prefix):]))
    return max(numbers, default=0) + 1


def apply_reading(findings: Sequence[Finding], reading: RecheckReading, *, author: str, role: str,
                  binding: str, judged_binding: str, source_digest: str = "", call: str = "",
                  prefix: str = "RF", task: str = "") -> Tuple[Finding, ...]:
    """Record one judge's per-id reading against ``findings``.

    ``binding`` is the identity the harness holds now (source, checks,
    requirement texts); ``judged_binding`` is the one the judge was handed.
    When they differ, nothing the judge said settles anything: every expected
    finding gets an ``unjudged`` disposition saying so. A judge who is not
    the finding's designated judge settles nothing either, per finding.
    Otherwise every expected finding receives exactly one disposition:
    ``unjudged`` when the reply gave none the reader accepted; RESOLVED,
    UNRESOLVED or INVALIDATED as read, each bound to ``binding``. New
    findings get the next free ids with the basis and impact the line
    carried, raised by the judge on this source; foreign ids were never
    applied; findings not in the reading are untouched.
    """
    _text(binding, "binding")
    _text(judged_binding, "judged_binding")
    drifted = binding != judged_binding
    out = []
    for finding in findings:
        verdict = reading.dispositions.get(finding.fid)
        if verdict is None:
            out.append(finding)
            continue
        refusal = designated_judge(finding, author, role)
        if drifted:
            state, why = "unjudged", "the bound identity changed between the judgment and its reading"
        elif refusal:
            state, why = "unjudged", refusal
        elif verdict.state == INVALID:
            state, why = "unjudged", verdict.why
        else:
            state, why = verdict.state, verdict.why
        out.append(replace(finding, history=finding.history + (
            Disposition(finding.fid, state, why, author, role, binding, source_digest, call),)))
    number = _next_number(findings, prefix)
    for new in reading.new:
        if drifted:
            break
        out.append(finding_from_line(f"{prefix}{number}", new.line, author=author, role=role,
                                     source_digest=source_digest or binding, task=task))
        number += 1
    return tuple(out)


# --- evidence obligations -------------------------------------------------

EXECUTION_STATES = ("missing", "pending", "ran", "failed", "skipped", "invalid", "stale")
SUFFICIENCY_STATES = ("unjudged", "sufficient", "insufficient")


@dataclass(frozen=True)
class EvidenceObligation:
    """A behavior that must be established before acceptance. ``runner`` is
    a capability id the readiness probes passed or the harness's own gate,
    or None for a prerequisite nobody can run yet (reported before dispatch,
    never papered over). ``policy`` names the review policy the evidence is
    judged under; a different policy is a different judgment."""

    oid: str
    behavior: str
    requirements: Tuple[Tuple[str, int], ...]
    gate: str
    runner: Optional[str] = None
    cases: Tuple[str, ...] = ()
    environment: str = ""
    policy: str = ""
    task: str = ""
    execution: str = "missing"
    sufficiency: str = "unjudged"
    source_digest: Optional[str] = None
    receipts: Tuple[str, ...] = ()
    judged_by: str = ""
    judged_binding: str = ""
    reason: str = ""

    def __post_init__(self):
        _check_id(self.oid, "obligation id")
        _text(self.behavior, f"{self.oid} behavior")
        if not self.requirements:
            raise ValueError(f"{self.oid} binds no requirement revision")
        for rid, revision in self.requirements:
            _check_id(rid, "requirement id")
            _count(revision, f"{self.oid}: {rid} revision", minimum=1)
        if self.runner is not None and not (isinstance(self.runner, str) and self.runner.strip()):
            raise ValueError(f"{self.oid} runner is a capability id or None, not {self.runner!r}")
        if self.execution not in EXECUTION_STATES:
            raise ValueError(f"execution must be one of {EXECUTION_STATES}")
        if self.sufficiency not in SUFFICIENCY_STATES:
            raise ValueError(f"sufficiency must be one of {SUFFICIENCY_STATES}")
        if self.sufficiency != "unjudged" and not (self.judged_by and self.judged_binding):
            raise ValueError("a sufficiency judgment names the seat that made it and what it judged")

    @property
    def executable(self) -> bool:
        return self.runner is not None

    @property
    def evidence_binding(self) -> str:
        """What a sufficiency judgment is about: exactly these receipts on
        this source, these requirement revisions, this gate, environment and
        policy. Any of them changing makes the judgment stale."""
        return _digest(dict(receipts=list(self.receipts), source=self.source_digest,
                            requirements=[list(r) for r in self.requirements], gate=self.gate,
                            environment=self.environment, policy=self.policy, execution=self.execution))

    @property
    def sufficiency_current(self) -> bool:
        return self.sufficiency != "unjudged" and self.judged_binding == self.evidence_binding

    @property
    def established(self) -> bool:
        """Both fields, never one standing in for the other, and the
        judgment about exactly this evidence."""
        return self.execution == "ran" and self.sufficiency == "sufficient" and self.sufficiency_current

    def to_dict(self) -> dict:
        return dict(asdict(self), executable=self.executable, established=self.established,
                    evidence_binding=self.evidence_binding, sufficiency_current=self.sufficiency_current)

    @classmethod
    def from_dict(cls, data: Mapping) -> "EvidenceObligation":
        kept = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        kept["requirements"] = tuple((r[0], int(r[1])) for r in kept.get("requirements") or ())
        kept["cases"] = tuple(kept.get("cases") or ())
        kept["receipts"] = tuple(kept.get("receipts") or ())
        return cls(**kept)


@dataclass(frozen=True)
class Discharge:
    obligation: EvidenceObligation
    accepted: bool
    reason: str


def discharge(obligation: EvidenceObligation, receipt: Mapping, *, current_source: str,
              current_requirements: Mapping[str, int]) -> Discharge:
    """Settle the execution field from one harness receipt.

    Accepted only when every identity matches: the receipt's source is the
    current source, its gate, runner and environment are the obligation's,
    its requirement revisions are the obligation's and those are the current
    revisions, it completed, and every named case appears among its passed
    cases and not among its skipped ones. A mismatch records ``invalid``
    with the first reason and changes nothing else; a matching failed run
    records ``failed``. A new execution makes any sufficiency judgment stale
    (its binding no longer matches); the fields themselves are untouched.
    """
    _text(current_source, "current_source")
    bound = tuple(tuple(r) for r in receipt.get("requirements") or ())
    checks = (
        (receipt.get("source") == current_source, "receipt is not on the current source"),
        ((receipt.get("gate") or "") == (obligation.gate or ""), "receipt ran under another gate"),
        (obligation.runner is not None and receipt.get("runner") == obligation.runner,
         "receipt came from another runner"),
        ((receipt.get("environment") or "") == (obligation.environment or ""),
         "receipt ran in another environment"),
        (bound == obligation.requirements, "receipt binds other requirement revisions"),
        (all(current_requirements.get(rid) == revision for rid, revision in obligation.requirements),
         "the obligation binds requirement revisions that are no longer current"),
        (bool(receipt.get("completed")), "execution did not complete"),
        (not (set(obligation.cases) & set(receipt.get("skipped_cases") or ())),
         "a named case was skipped"),
        (set(obligation.cases) <= set(receipt.get("passed_cases") or ())
         or (not receipt.get("passed") and bool(obligation.cases)),
         "a named case is not among the passed cases"),
    )
    for ok, reason in checks:
        if not ok:
            return Discharge(replace(obligation, execution="invalid", reason=reason), False, reason)
    state = "ran" if receipt.get("passed") else "failed"
    receipt_id = str(receipt.get("id") or _digest(dict(receipt))[:16])
    return Discharge(replace(obligation, execution=state, source_digest=current_source,
                             receipts=obligation.receipts + (receipt_id,), reason=""),
                     True, "")


def refresh(obligation: EvidenceObligation, *, current_source: Optional[str],
            current_requirements: Mapping[str, int]) -> EvidenceObligation:
    """What the obligation is on the source and requirements as they are
    now: an execution on another source is ``stale``, and one bound to a
    requirement revision that is no longer current is ``stale`` too. Nothing
    here makes anything current again; only a new receipt does."""
    if obligation.execution in ("ran", "failed") and obligation.source_digest != current_source:
        return replace(obligation, execution="stale", reason="executed on another source")
    if any(current_requirements.get(rid) != revision for rid, revision in obligation.requirements):
        if obligation.execution in ("ran", "failed", "pending"):
            return replace(obligation, execution="stale", reason="a bound requirement revision changed")
    return obligation


def judge_sufficiency(obligation: EvidenceObligation, verdict: str, *, judged_by: str) -> EvidenceObligation:
    """An independent seat's judgment of what the executed evidence
    establishes, bound to exactly that evidence. Only a completed execution
    can be judged sufficient; the harness never judges."""
    if verdict not in ("sufficient", "insufficient"):
        raise ValueError("a sufficiency verdict is sufficient or insufficient")
    if not judged_by.strip() or judged_by.strip().lower() == "harness":
        raise ValueError("sufficiency is an independent seat's judgment, never the harness's")
    if verdict == "sufficient" and obligation.execution != "ran":
        raise ValueError(f"{obligation.oid} execution is {obligation.execution}; nothing to find sufficient")
    return replace(obligation, sufficiency=verdict, judged_by=judged_by.strip(),
                   judged_binding=obligation.evidence_binding)


# --- operations -----------------------------------------------------------

OPERATION_STATES = ("prepared", "started", "settled", "uncertain")


@dataclass(frozen=True)
class OperationRecord:
    """One external effect: a model call, a check run, a capture or a probe.
    ``identity`` is what makes two requests the same work (purpose, owner
    role, policy and input digests); ``reservation`` is the budget ticket
    published with the intent, before anything starts."""

    opid: str
    purpose: str
    identity: Mapping[str, str]
    allowed_effects: Tuple[str, ...]
    owner: str
    reservation: Optional[str] = None
    status: str = "prepared"
    result_digest: Optional[str] = None
    note: str = ""

    def __post_init__(self):
        _check_id(self.opid, "operation id")
        _text(self.purpose, f"{self.opid} purpose")
        _text(self.owner, f"{self.opid} owner")
        if self.status not in OPERATION_STATES:
            raise ValueError(f"status must be one of {OPERATION_STATES}")
        for name in ("purpose", "role", "policy", "input"):
            if not str(self.identity.get(name, "")).strip():
                raise ValueError(f"{self.opid} identity needs {name}")

    @property
    def dedupe_key(self) -> str:
        return _digest(dict(self.identity))

    @property
    def unsettled(self) -> bool:
        return self.status in ("started", "uncertain")

    def to_dict(self) -> dict:
        return dict(asdict(self), identity=dict(self.identity), unsettled=self.unsettled)


def start_operation(op: OperationRecord) -> OperationRecord:
    """Prepared and reserved, then started. No reservation, no start."""
    if op.status != "prepared":
        raise ValueError(f"{op.opid} is {op.status}; only a prepared operation starts")
    if not op.reservation:
        raise ValueError(f"{op.opid} has no published reservation; no effect may start")
    return replace(op, status="started")


def settle_operation(op: OperationRecord, result_digest: str) -> OperationRecord:
    if op.status != "started":
        raise ValueError(f"{op.opid} is {op.status}; only a started operation settles")
    _text(result_digest, f"{op.opid} result")
    return replace(op, status="settled", result_digest=result_digest)


def uncertain_operation(op: OperationRecord, why: str) -> OperationRecord:
    """A started operation whose result was never recorded. It stays
    uncertain: not absent, not repeated, not resumed here."""
    if op.status != "started":
        raise ValueError(f"{op.opid} is {op.status}; only a started operation becomes uncertain")
    return replace(op, status="uncertain", note=why.strip() or "result not recorded")


# --- the remaining path ---------------------------------------------------

#: The budget's own exposure classes. ``editing`` is the post-return per-call
#: ceiling for a call holding the write grant; ``reserve`` is the per-call
#: reserve for any other call; ``shaped`` exists only where RunBudget already
#: grants it (a summary-only call on a CLI whose argv holds it to one turn).
EXPOSURE_CLASSES = ("editing", "reserve", "shaped")


@dataclass(frozen=True)
class PlanStage:
    name: str
    exposure: str
    editing: bool = False
    calls: int = 1
    finishing: bool = False

    def __post_init__(self):
        _text(self.name, "plan stage name")
        if self.exposure not in EXPOSURE_CLASSES:
            raise ValueError(f"exposure must be one of {EXPOSURE_CLASSES}")
        if self.editing and self.exposure != "editing":
            raise ValueError(f"{self.name} holds the write grant and is exposed at the editing ceiling")
        _count(self.calls, f"{self.name} calls", minimum=1)


@dataclass(frozen=True)
class PlanVerdict:
    feasible: bool
    calls_needed: int
    tokens_needed: int
    wall_needed: float
    short: Dict[str, float] = field(default_factory=dict)
    detail: str = ""
    stages: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def admit_plan(stages: Sequence[PlanStage], *, calls_left: int, tokens_left: int,
               wall_left: float, exposures: Mapping[str, int], wall_per_stage: float) -> PlanVerdict:
    """Whether the whole remaining path fits what is left, exactly counted in
    the budget's exposure figures. Not a forecast of what calls will report,
    and no stage is cheaper than its class says. A shaped stage with no
    shaped figure supplied is counted at the reserve. With no reserve
    configured (0) the budget admits no token axis, and none is counted
    here either; the calls and wall axes always count."""
    calls_left = _count(calls_left, "calls_left")
    tokens_left = _count(tokens_left, "tokens_left")
    wall_left = _amount(wall_left, "wall_left")
    wall_per_stage = _amount(wall_per_stage, "wall_per_stage")
    figures = {name: _count(exposures.get(name, 0), f"exposures[{name}]") for name in EXPOSURE_CLASSES}
    tokens_counted = figures["reserve"] > 0
    calls = sum(s.calls for s in stages)
    tokens = 0
    if tokens_counted:
        for stage in stages:
            figure = figures[stage.exposure] or figures["reserve"]
            if stage.exposure == "editing":
                figure = max(figures["editing"], figures["reserve"])
            tokens += figure * stage.calls
    wall = wall_per_stage * calls
    short = {}
    if calls > calls_left:
        short["calls"] = calls - calls_left
    if tokens_counted and tokens > tokens_left:
        short["tokens"] = tokens - tokens_left
    if wall > wall_left or (calls and wall_left <= 0):
        # With no per-stage figure, a stage still needs some time of its own.
        short["wall_seconds"] = max(wall - wall_left, 1.0 if wall_left <= 0 else 0.0)
    names = tuple(f"{s.name}({s.exposure}{'/write' if s.editing else ''})" for s in stages)
    detail = (f"{calls} call(s), {tokens:,} tokens, {wall:.0f} s for {', '.join(names) or 'nothing'}; "
              f"{calls_left} call(s), {tokens_left:,} tokens, {wall_left:.0f} s left")
    if not tokens_counted:
        detail += " (no token reserve configured; tokens not counted)"
    if short:
        detail += "; short by " + ", ".join(f"{k} {v:,.0f}" for k, v in short.items())
    return PlanVerdict(not short, calls, tokens, wall, short, detail, names)


_ = checkable_reference  # re-exported for callers that validate evidence text
