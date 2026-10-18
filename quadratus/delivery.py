"""Typed delivery records and their pure transitions (blueprint stage A, records).

Consolidated execution blueprint (Davis, 2026-10-17): the controller owns a
small pure transition function over typed records, and ``session.py`` becomes
the adapter that invokes models and runners and records what came back. This
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
  claimed impact, and the history of judgments on it. A finding is resolved
  only by a valid per-id disposition on a source other than the one it was
  raised on; an immediate risk (data loss, integrity, security, an
  irreversible effect) holds the affected effect whatever its basis; a
  basis-less concern that is not an immediate risk is carried to the audit by
  id, never dropped as a note.
- :class:`EvidenceObligation`: a behavior that must be established, with
  ``execution`` (did the right thing run) and ``sufficiency`` (does what ran
  establish the behavior) as two fields. A receipt discharges execution only
  when source, gate, runner, requirement revisions, named cases and completed
  execution all match; sufficiency comes only from a named independent seat.
- :class:`OperationRecord`: one external effect with explicit prepared,
  started, settled and uncertain states; a started operation that did not
  settle is uncertain, never assumed absent and never resumed by this module.
- :class:`PlanStage` and :func:`admit_plan`: the whole remaining path costed
  in the budget's own exposure classes. The exposure figures are inputs from
  ``RunBudget`` semantics; a stage holding the write grant is always exposed
  at the editing figure. This module invents no cheaper class.

The wiring (session hooks, durable publication, prompts that ask reviewers
for per-id lines) is deliberately absent: root raises effort for it after this
commit. Nothing here is a product claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .judgment import INVALID, RESOLVED, UNRESOLVED, RecheckReading

__all__ = [
    "ORIGINS", "RequirementRevision",
    "BASIS_KINDS", "IMMEDIATE_RISKS", "FindingBasis", "Disposition", "Finding", "apply_reading",
    "EXECUTION_STATES", "SUFFICIENCY_STATES", "EvidenceObligation", "Discharge", "discharge",
    "judge_sufficiency",
    "OPERATION_STATES", "OperationRecord", "start_operation", "settle_operation",
    "uncertain_operation",
    "EXPOSURE_CLASSES", "PlanStage", "PlanVerdict", "admit_plan",
]

_ID = re.compile(r"[A-Z][A-Z0-9]*-?[0-9]+\Z")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _check_id(value: str, what: str) -> None:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"{what} must look like R1, F12 or O-3, not {value!r}")


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
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError(f"{self.rid} revision {self.revision} has no text")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}, not {self.origin!r}")
        if self.brief_version < 0:
            raise ValueError("brief_version cannot be negative")

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
DISPOSITION_STATES = (RESOLVED, UNRESOLVED, "invalidated", "unjudged")


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
    """One judgment on one finding, by one seat, on one source."""

    fid: str
    state: str
    why: str
    author: str
    source_digest: str
    call: str = ""

    def __post_init__(self):
        if self.state not in DISPOSITION_STATES:
            raise ValueError(f"disposition state must be one of {DISPOSITION_STATES}")
        if not self.author.strip() or not self.source_digest.strip():
            raise ValueError("a disposition names its author and the source it judged")


@dataclass(frozen=True)
class Finding:
    fid: str
    author: str
    role: str
    claim: str
    basis: FindingBasis
    impact: str
    source_digest: str
    requirement: Optional[Tuple[str, int]] = None
    history: Tuple[Disposition, ...] = ()

    def __post_init__(self):
        _check_id(self.fid, "finding id")
        if not self.claim.strip():
            raise ValueError(f"{self.fid} has no claim")
        if not self.author.strip() or not self.source_digest.strip():
            raise ValueError(f"{self.fid} names its author and the source it was raised on")
        if self.requirement is not None:
            _check_id(self.requirement[0], "requirement id")

    @property
    def state(self) -> str:
        """``open`` until a disposition other than ``unjudged`` lands."""
        for item in reversed(self.history):
            if item.state != "unjudged":
                return item.state
        return "open"

    @property
    def closed(self) -> bool:
        return self.state in (RESOLVED, "invalidated")

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
        data.update(state=self.state, holds_effects=self.holds_effects,
                    audit_carried=self.audit_carried)
        return data


def _next_fid(findings: Iterable[Finding]) -> int:
    numbers = [int(re.sub(r"\D", "", f.fid) or 0) for f in findings]
    return max(numbers, default=0) + 1


def apply_reading(findings: Sequence[Finding], reading: RecheckReading, *, author: str,
                  role: str, source_digest: str, call: str = "") -> Tuple[Finding, ...]:
    """Record one recheck's per-id reading against ``findings``.

    Every expected finding receives exactly one disposition, ``unjudged`` when
    the reply gave none the reader accepted. ``resolved`` needs a source other
    than the one the finding was raised on (nothing was fixed otherwise) and
    is downgraded to ``unjudged`` with that reason. ``invalidated`` without
    evidence is already ``unjudged`` in the reading. New findings get the next
    free ids with basis ``none`` until a reviewer supplies one; foreign ids
    were never applied. Findings not in the reading are untouched.
    """
    if not source_digest.strip():
        raise ValueError("a reading is applied on a named source")
    out = []
    for finding in findings:
        verdict = reading.dispositions.get(finding.fid)
        if verdict is None:
            out.append(finding)
            continue
        state, why = verdict.state, verdict.why
        if state == INVALID:
            state = "unjudged"
        if state == RESOLVED and source_digest == finding.source_digest:
            state, why = "unjudged", f"RESOLVED on the source {finding.fid} was raised on; nothing changed"
        out.append(replace(finding, history=finding.history + (
            Disposition(finding.fid, state, why, author, source_digest, call),)))
    number = _next_fid(findings)
    for claim in reading.new:
        out.append(Finding(f"F{number}", author, role, claim, FindingBasis("none"), "unstated",
                           source_digest))
        number += 1
    return tuple(out)


# --- evidence obligations -------------------------------------------------

EXECUTION_STATES = ("missing", "pending", "ran", "failed", "skipped", "invalid", "stale")
SUFFICIENCY_STATES = ("unjudged", "sufficient", "insufficient")


@dataclass(frozen=True)
class EvidenceObligation:
    """A behavior that must be established before acceptance. ``runner`` is
    a capability id the readiness probes passed, or None for a prerequisite
    nobody can run yet (reported before dispatch, never papered over)."""

    oid: str
    behavior: str
    requirements: Tuple[Tuple[str, int], ...]
    gate: str
    runner: Optional[str] = None
    cases: Tuple[str, ...] = ()
    environment: str = ""
    execution: str = "missing"
    sufficiency: str = "unjudged"
    source_digest: Optional[str] = None
    receipts: Tuple[str, ...] = ()
    judged_by: str = ""
    reason: str = ""

    def __post_init__(self):
        _check_id(self.oid, "obligation id")
        if not self.behavior.strip():
            raise ValueError(f"{self.oid} names no behavior")
        if not self.requirements:
            raise ValueError(f"{self.oid} binds no requirement revision")
        for rid, revision in self.requirements:
            _check_id(rid, "requirement id")
            if not isinstance(revision, int) or revision < 1:
                raise ValueError(f"{self.oid}: {rid} revision must be a positive integer")
        if self.execution not in EXECUTION_STATES:
            raise ValueError(f"execution must be one of {EXECUTION_STATES}")
        if self.sufficiency not in SUFFICIENCY_STATES:
            raise ValueError(f"sufficiency must be one of {SUFFICIENCY_STATES}")
        if self.sufficiency != "unjudged" and not self.judged_by:
            raise ValueError("a sufficiency judgment names the seat that made it")

    @property
    def executable(self) -> bool:
        return self.runner is not None

    @property
    def established(self) -> bool:
        """Both fields, never one standing in for the other."""
        return self.execution == "ran" and self.sufficiency == "sufficient"

    def to_dict(self) -> dict:
        return dict(asdict(self), executable=self.executable, established=self.established)


@dataclass(frozen=True)
class Discharge:
    obligation: EvidenceObligation
    accepted: bool
    reason: str


def discharge(obligation: EvidenceObligation, receipt: Mapping, *, current_source: str) -> Discharge:
    """Settle the execution field from one harness receipt.

    Accepted only when every identity matches: the receipt's source is the
    current source and the same for every receipt, its gate and runner are the
    obligation's, its requirement revisions are the obligation's, it completed,
    and every named case appears among its passed cases and not among its
    skipped ones. A mismatch records ``invalid`` with the first reason and
    changes nothing else; a matching failed run records ``failed``.
    Sufficiency is untouched either way.
    """
    checks = (
        (receipt.get("source") == current_source, "receipt is not on the current source"),
        (receipt.get("gate") == obligation.gate, "receipt ran under another gate"),
        (obligation.runner is not None and receipt.get("runner") == obligation.runner,
         "receipt came from another runner"),
        (tuple(tuple(r) for r in receipt.get("requirements") or ()) == obligation.requirements,
         "receipt binds other requirement revisions"),
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


def judge_sufficiency(obligation: EvidenceObligation, verdict: str, *, judged_by: str) -> EvidenceObligation:
    """An independent seat's judgment of what the executed evidence
    establishes. Only a completed execution can be judged sufficient; the
    harness never judges."""
    if verdict not in ("sufficient", "insufficient"):
        raise ValueError("a sufficiency verdict is sufficient or insufficient")
    if not judged_by.strip() or judged_by.strip().lower() == "harness":
        raise ValueError("sufficiency is an independent seat's judgment, never the harness's")
    if verdict == "sufficient" and obligation.execution != "ran":
        raise ValueError(f"{obligation.oid} execution is {obligation.execution}; nothing to find sufficient")
    return replace(obligation, sufficiency=verdict, judged_by=judged_by.strip())


# --- operations -----------------------------------------------------------

OPERATION_STATES = ("prepared", "started", "settled", "uncertain")


@dataclass(frozen=True)
class OperationRecord:
    """One external effect: a model call, a check run or an editing call.
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
        if not self.purpose.strip() or not self.owner.strip():
            raise ValueError(f"{self.opid} names its purpose and owner")
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
    if not result_digest.strip():
        raise ValueError(f"{op.opid} settles on a recorded result")
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
        if not self.name.strip():
            raise ValueError("a plan stage has a name")
        if self.exposure not in EXPOSURE_CLASSES:
            raise ValueError(f"exposure must be one of {EXPOSURE_CLASSES}")
        if self.editing and self.exposure != "editing":
            raise ValueError(f"{self.name} holds the write grant and is exposed at the editing ceiling")
        if self.calls < 1:
            raise ValueError(f"{self.name} needs at least one call")


@dataclass(frozen=True)
class PlanVerdict:
    feasible: bool
    calls_needed: int
    tokens_needed: int
    wall_needed: float
    short: Dict[str, float] = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def admit_plan(stages: Sequence[PlanStage], *, calls_left: int, tokens_left: int,
               wall_left: float, exposures: Mapping[str, int], wall_per_stage: float) -> PlanVerdict:
    """Whether the whole remaining path fits what is left, exactly counted in
    the budget's exposure figures. Not a forecast of what calls will report,
    and no stage is cheaper than its class says. A shaped stage with no
    shaped figure supplied is counted at the reserve."""
    if calls_left < 0 or tokens_left < 0 or wall_left < 0:
        raise ValueError("capacity cannot be negative")
    for name in ("editing", "reserve"):
        if int(exposures.get(name, 0)) <= 0:
            raise ValueError(f"exposures needs a positive {name} figure from the budget")
    calls = sum(s.calls for s in stages)
    tokens = 0
    for stage in stages:
        figure = exposures.get(stage.exposure) or exposures["reserve"]
        if stage.exposure == "editing":
            figure = max(int(exposures["editing"]), int(exposures["reserve"]))
        tokens += int(figure) * stage.calls
    wall = float(wall_per_stage) * calls
    short = {}
    if calls > calls_left:
        short["calls"] = calls - calls_left
    if tokens > tokens_left:
        short["tokens"] = tokens - tokens_left
    if wall > wall_left:
        short["wall_seconds"] = wall - wall_left
    names = ", ".join(f"{s.name}({s.exposure}{'/write' if s.editing else ''})" for s in stages)
    detail = (f"{calls} call(s), {tokens:,} tokens, {wall:.0f} s for {names or 'nothing'}; "
              f"{calls_left} call(s), {tokens_left:,} tokens, {wall_left:.0f} s left")
    if short:
        detail += "; short by " + ", ".join(f"{k} {v:,.0f}" for k, v in short.items())
    return PlanVerdict(not short, calls, tokens, wall, short, detail)
