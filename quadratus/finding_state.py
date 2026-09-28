"""Unresolved findings and the run's stop, projected from the typed record.

Candidate for map P3.4 (#25): the ``open_findings`` and ``stop_reason`` rows.
Nothing here decides, writes or calls anything. :func:`project` reads the
typed task and run outcomes (quadratus.outcome) and the authoritative audit
ledger (``Session.findings``) and returns one immutable :class:`FindingState`;
:func:`legacy_parity` says whether that projection describes what the legacy
inputs (``open_findings``, ``_design_unverified``, ``stop_reason`` and the
``result.json`` error) held. Integration seam and failure precedence:
docs/finding-state-contract.md.

Rules, from the map and outcome.py:

- The ledger is the authority for audit-finding identity and status. A task's
  ``open_at_close`` is a snapshot that may be stale; where they disagree the
  ledger wins and the disagreement is reported. A snapshot naming an id the
  ledger does not hold stays active: missing is never resolved.
- Facts are history. A recovered or non-terminal fact keeps its identity and
  moves to ``history``; it never disappears.
- The stop is the run's own named stop fact. Open findings never replace it,
  whatever they rank: a refusal, an operator handoff, a latched budget or an
  unknown failure stays the stop, and the debt is listed beside it.
- Completion is not derived here. ``done_accepted`` is not read.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Iterable, List, Optional, Sequence, Tuple

from .outcome import PRECEDENCE, legacy_error_class

#: Categories that the legacy ``open_findings`` list carries, one text each.
FINDING_CATEGORIES = frozenset({"scope", "cheap_gate", "review", "security", "design_evidence",
                                "design_review", "unmerged", "run_finding"})
#: Debt a later task can repay through the ledger (RESOLVES F<n>).
AUDIT_DEBT = frozenset({"audit_debt", "ledger_missing"})
#: Debt owned by the task that left it; nothing but that task's own verified
#: renders discharges it (map G8).
DESIGN_DEBT = frozenset({"design_evidence", "design_review"})

_CHECK_ATTEMPT = re.compile(r"check attempt \d+ failed\Z")
_UNMERGED_LEGACY = re.compile(r"Parallel task (?P<task>\S+) was not merged \((?P<reason>.*)\); "
                              r"its files are kept in artifact \S+\.\Z", re.S)
_DEPENDENCY = ("DependencyTreeChanged:", "DependencyIdentityUnavailable:")
_FACT_LIMIT = 400


def _get(obj, name, default=None):
    """An attribute or a key: live outcomes and their ``result.json`` form."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _rank(kind: str) -> int:
    return PRECEDENCE.index(kind) if kind in PRECEDENCE else len(PRECEDENCE)


@dataclass(frozen=True)
class Item:
    """One unresolved or historical fact, under an identity that never moves.

    ``id`` is ``ledger:<F-id>`` for the ledger, ``task:<task>[#n]:<stage>:<kind>:<k>``
    for a task fact (``#n`` for a repeated task id, ``k`` its ordinal among
    that task's facts of the same stage and kind) and ``run:<stage>:<kind>:<k>``
    for a run fact. Facts and ledger rows are append-only, so re-projecting a
    later record never renumbers an earlier item.
    """
    id: str
    source: str
    category: str
    kind: str
    state: str
    task: str = ""
    stage: str = ""
    detail: str = ""
    refs: Tuple[str, ...] = ()
    legacy_route: bool = False
    notes: Tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.state == "active"

    @property
    def debt(self) -> str:
        """``audit``, ``design`` or ``""``: the two debts never merge."""
        if self.category in AUDIT_DEBT:
            return "audit"
        if self.category in DESIGN_DEBT:
            return "design"
        return ""


@dataclass(frozen=True)
class Stop:
    """The run's named stop: its last active run fact that carries a legacy name."""
    name: str
    kind: str
    detail: str
    index: int
    id: str = ""


@dataclass(frozen=True)
class FindingState:
    active: Tuple[Item, ...]
    history: Tuple[Item, ...]
    stop: Optional[Stop]
    #: Every other active item's class, highest first; never the stop.
    secondary: Tuple[str, ...]
    #: Active items ranked strictly above the stop's class. Reported, never
    #: acted on: the named stop is what the run said.
    outranked_by: Tuple[str, ...]
    #: The record itself is malformed somewhere; each entry fails closed.
    problems: Tuple[str, ...] = field(default=())

    def findings(self) -> List[Item]:
        """Active items the legacy ``open_findings`` list would carry."""
        return [i for i in self.active if i.category in FINDING_CATEGORIES]

    def audit_debt(self) -> List[str]:
        """Open ledger ids, in ledger order: the ledger's own answer."""
        return [i.refs[0] for i in self.active if i.category == "audit_debt"]

    def design_debt(self, tasks: Optional[Iterable[str]] = None) -> List[Item]:
        """Active design debt, optionally only the named tasks' own (map G8)."""
        wanted = None if tasks is None else set(tasks)
        return [i for i in self.active if i.debt == "design" and (wanted is None or i.task in wanted)]

    def for_task(self, task_id: str) -> List[Item]:
        return [i for i in self.active if i.task == task_id]


def _task_category(outcome, fact) -> str:
    kind, stage, detail = _get(fact, "kind", ""), _get(fact, "stage", ""), _get(fact, "detail", "") or ""
    closed_as = str(_get(outcome, "closed_as", "") or "")
    if stage == "merge" and detail.startswith("not merged: "):
        return "unmerged"
    if closed_as.startswith("stopped:") and detail.startswith(closed_as.split(":", 1)[1] + ":"):
        return "stopped"
    if kind in ("product", "operator") and stage == "checks" and _CHECK_ATTEMPT.match(detail):
        return "check"
    if kind == "product" and detail == "Cheap gates failed before review":
        return "cheap_gate"
    if kind == "integrity" and " changed paths outside its declared scope: " in detail:
        return "scope"
    if kind == "security":
        return "security"
    if stage == "design" and kind == "invalid_proof":
        return "design_evidence"
    if stage == "design" and kind == "unverified":
        return "design_review"
    if kind == "unverified":
        return "review"
    if kind in ("cap", "transport", "refusal"):
        return kind
    return "other"


def _run_category(fact) -> str:
    detail = _get(fact, "detail", "") or ""
    if _get(fact, "legacy") is not None:
        return "stop"
    if detail == "a gate outside any task (the merge gate) failed":
        return "merge_gate"
    if detail.startswith(_DEPENDENCY):
        return "dependency"
    if detail.startswith("DONE sent back: "):
        return "sent_back"
    # _open_finding with no task running writes a run fact of its class.
    return "run_finding"


def _state(fact) -> str:
    if _get(fact, "recovered", False):
        return "recovered"
    if not _get(fact, "terminal", True):
        return "non_terminal"
    return "active"


def _ledger_items(ledger, problems) -> Tuple[List[Item], dict]:
    items, by_id = [], {}
    for index, finding in enumerate(ledger or []):
        fid = _get(finding, "id")
        if not isinstance(fid, str) or not fid:
            problems.append(f"ledger[{index}] has no id")
            fid = f"#{index}"
        if fid in by_id:
            problems.append(f"ledger id {fid} appears twice")
        status = _get(finding, "status")
        if status not in ("open", "resolved"):
            problems.append(f"ledger {fid} status {status!r} is not open or resolved")
        notes = tuple(f"{k}: {_get(finding, k)}" for k in ("reopened", "unresolved_reason", "last_attempt")
                      if _get(finding, k))
        item = Item(id=f"ledger:{fid}", source="ledger", category="audit_debt", kind="unverified",
                    state="resolved" if status == "resolved" else "active", task=str(_get(finding, "task") or ""),
                    stage="audit", detail=str(_get(finding, "message") or "")[:_FACT_LIMIT],
                    refs=(fid, *[str(r) for r in (_get(finding, "requirements") or [])]), notes=notes)
        by_id[fid] = item
        items.append(item)
    return items, by_id


def project(task_outcomes: Sequence, run_outcome, ledger: Sequence = ()) -> FindingState:
    """The unresolved findings and the named stop, read from the record.

    ``task_outcomes`` and ``run_outcome`` are the session's typed record, live
    or as ``result.json`` stores it; ``ledger`` is ``Session.findings``. Inputs
    are read, never written.
    """
    problems: List[str] = []
    ledger_items, by_id = _ledger_items(ledger, problems)
    items: List[Item] = list(ledger_items)
    seen_tasks: Counter = Counter()
    snapshot_ids: dict = {}
    for outcome in task_outcomes or []:
        tid = str(_get(outcome, "task_id", "") or "")
        seen_tasks[tid] += 1
        label = tid if seen_tasks[tid] == 1 else f"{tid}#{seen_tasks[tid]}"
        ordinals: Counter = Counter()
        for fact in _get(outcome, "facts", []) or []:
            kind, stage = str(_get(fact, "kind", "")), str(_get(fact, "stage", "") or "")
            if kind not in PRECEDENCE:
                problems.append(f"task {label} fact of unknown class {kind!r} kept active")
            ordinals[(stage, kind)] += 1
            items.append(Item(
                id=f"task:{label}:{stage or '-'}:{kind}:{ordinals[(stage, kind)]}", source="task",
                category=_task_category(outcome, fact), kind=kind, state=_state(fact), task=tid, stage=stage,
                detail=str(_get(fact, "detail", "") or ""), legacy_route=bool(_get(fact, "legacy_route", False))))
        for fid in ((_get(outcome, "open_at_close") or {}).get("findings") or []):
            snapshot_ids.setdefault(fid, []).append(tid)
    for fid, tasks in snapshot_ids.items():
        known = by_id.get(fid)
        if known is None:
            problems.append(f"{', '.join(tasks)} closed with {fid} open; the ledger has no such finding")
            items.append(Item(id=f"ledger:{fid}", source="ledger", category="ledger_missing", kind="unverified",
                              state="active", task=tasks[-1], stage="audit", refs=(fid,),
                              notes=("named by a task snapshot, absent from the ledger",)))
        elif known.state == "resolved":
            # The ledger resolved it after the snapshot: history, and said so.
            items[items.index(known)] = replace(known, notes=known.notes + (
                f"open when {', '.join(tasks)} closed; resolved in the ledger since",))
    run_facts = list(_get(run_outcome, "facts", []) or [])
    ordinals = Counter()
    stop = None
    for index, fact in enumerate(run_facts):
        kind, stage = str(_get(fact, "kind", "")), str(_get(fact, "stage", "") or "run")
        if kind not in PRECEDENCE:
            problems.append(f"run fact of unknown class {kind!r} kept active")
        ordinals[(stage, kind)] += 1
        state = _state(fact)
        if state == "active" and _get(fact, "legacy") is not None:
            stop = Stop(name=str(_get(fact, "legacy")), kind=kind, detail=str(_get(fact, "detail", "") or ""),
                        index=index)
        items.append(Item(id=f"run:{stage}:{kind}:{ordinals[(stage, kind)]}", source="run",
                          category=_run_category(fact), kind=kind, state=state, stage=stage,
                          detail=str(_get(fact, "detail", "") or "")))
    if stop is not None:
        stop = replace(stop, id=[i for i in items if i.source == "run"][stop.index].id)
    # The stop is reported as the stop, not again as an item. An earlier named
    # stop a later one replaced (map G9) stays active: it is still a fact.
    active = tuple(i for i in items if i.active and (stop is None or i.id != stop.id))
    history = tuple(i for i in items if not i.active)
    ranked = sorted(active, key=lambda i: _rank(i.kind))
    outranked = tuple(i.id for i in ranked if stop is not None and _rank(i.kind) < _rank(stop.kind))
    return FindingState(active=active, history=history, stop=stop, secondary=tuple(i.kind for i in ranked),
                        outranked_by=outranked, problems=tuple(problems))


def from_session(session) -> FindingState:
    """Read-only: the projection of a live session's record."""
    return project(list(getattr(session, "task_outcomes", []) or []), getattr(session, "run_outcome", None),
                   list(getattr(session, "findings", []) or []))


def from_result(result: dict) -> FindingState:
    """The projection of a ``result.json``: its ``workflow`` record and ledger."""
    workflow = result.get("workflow") or {}
    return project(workflow.get("tasks") or [], workflow.get("run") or {}, result.get("findings") or [])


def _legacy_key(text: str):
    """What a legacy open-finding text is recorded as in the typed record."""
    match = _UNMERGED_LEGACY.match(text)
    if match:
        return ("unmerged", match["task"], f"not merged: {match['reason']}"[:_FACT_LIMIT])
    return ("text", "", text[:_FACT_LIMIT])


def _item_key(item: Item):
    if item.category == "unmerged":
        return ("unmerged", item.task, item.detail)
    return ("text", "", item.detail)


def legacy_parity(state: FindingState, *, open_findings: Sequence[str] = (), error: str = "",
                  stop_reason: str = "", design_unverified: Optional[Sequence] = None) -> dict:
    """Whether ``state`` describes what the legacy inputs held.

    ``error`` is the ``result.json`` error (an exception's text, else
    ``stop_reason``); ``stop_reason`` is the session's own. Observational: a
    disagreement is reported and never raised. ``gaps`` are known limits of
    the typed record rather than disagreements: a stop detail that is a
    truncated prefix of the legacy text.
    """
    problems, gaps = list(state.problems), []
    legacy = Counter(_legacy_key(t) for t in open_findings)
    typed = Counter(_item_key(i) for i in state.findings())
    for key, n in (legacy - typed).items():
        problems.append(f"legacy open finding without a typed fact (x{n}): {key[2][:160]}")
    for key, n in (typed - legacy).items():
        problems.append(f"typed finding absent from the legacy list (x{n}): {key[2][:160]}")
    want = legacy_error_class(error or stop_reason)
    have = state.stop.name if state.stop is not None else ""
    if want != have:
        problems.append(f"stop: typed {have!r}, legacy {want!r}")
    elif state.stop is not None:
        text = error or stop_reason
        if text == state.stop.detail:
            pass
        elif text.startswith(state.stop.detail):
            gaps.append(f"stop {have}: typed detail is a {len(state.stop.detail)}-character prefix of the "
                        f"{len(text)}-character legacy text")
        else:
            problems.append(f"stop {have}: typed detail differs from the legacy text")
    if design_unverified is not None:
        legacy_tasks = sorted({str(d[0]) for d in design_unverified})
        typed_tasks = sorted({i.task for i in state.active if i.category == "design_evidence"})
        if legacy_tasks != typed_tasks:
            problems.append(f"design debt: typed {typed_tasks}, legacy {legacy_tasks}")
    return dict(agree=not problems, problems=problems, gaps=gaps,
                stop=have, findings=[i.id for i in state.findings()], audit_debt=state.audit_debt())
