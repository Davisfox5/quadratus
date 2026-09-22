"""Who actually ran, and what the harness cannot see.

Two separate things were being conflated, and the 2026-09-13 review probe
proved it. During its review Sol used the Codex CLI's own ``spawn_agent`` to
create a second Sol. That child never passed through :class:`WorkerPool`, its
errand tree, or its per-task budget. The parent's CLI return reported 203,199
input and 6,350 output tokens; the child separately recorded 127,405 and
7,700. Quadratus metered the parent and nothing else, so 135,105 tokens were
spent inside an authorised run and were absent from every total it reported.

Two fixes, in order. Where the vendor offers a switch, the harness now throws
it: every Codex call carries ``--disable multi_agent`` (and its successor
switch) from ``cli_providers.CODEX_SPEC.control_args``, and an operator
override that would undo it is refused rather than out-ordered. OpenAI helpers
are intended to pass through :class:`WorkerPool`; runtime enforcement still
needs the live probe, and native observations must remain visible. An earlier version
of this text said the harness could not forbid native delegation at all; that
was too broad, and is the second design error this file has had to retract.
It remains true for the other two vendors' senior seats -- Claude's ``Task``
and Grok's ``Agent`` are denied only on restricted seats -- and for a control
that fails on codex, where a child would still run inside a vendor process.
For those the record is the check, and pretending otherwise would make the
report *more* wrong. This module gives that record three things it lacked:

**A provenance for every invocation.** :class:`Origin` distinguishes a
Quadratus-assigned seat, a Quadratus-commissioned worker, a vendor-native
child the harness merely observed, and vendor-internal auxiliary activity
(Claude's envelopes list Haiku usage that nobody dispatched). Counting those
four together produced a number that was neither a budget nor a bill.

**Unknown as a value.** A cancelled or timed-out call consumed a model window
and reported nothing. The old meter had no way to record that: absent from the
ledger reads as zero, and zero is a claim. :class:`InvocationEvent` carries
``tokens=None`` and says ``unknown``.

**Reconciliation that does not double count.** Vendor session files report
usage *cumulatively* -- each update restates the session total, so summing the
updates multiplies it. :func:`reconcile` takes the maximum per session id
rather than the sum, and refuses to add a child whose id it has already seen.

Nothing here is load-bearing on the hot path. Like :mod:`quadratus.usage`, a
failure to account must never fail a run.
"""

from __future__ import annotations

import json
import logging
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

__all__ = [
    "Origin",
    "InvocationEvent",
    "NativeChild",
    "DelegationLedger",
    "reconcile",
]


invocation_context = ContextVar("quadratus_invocation", default=None)
_pending_invocations = ContextVar("pending_invocations", default=None)


@contextmanager
def capture_invocations():
    """Finalize calls after the enclosing caller's acceptance checks.

    Fleet also uses this boundary on its own. Nested Fleet calls join the
    session boundary, so a successful provider response followed by a scope
    stop is persisted once, with usage and both outcomes intact.

    Persistence waits for this short acceptance boundary. A hard process kill
    during scope assessment can lose the pending row; ordinary exceptions and
    interrupts flush it. This is not a write-ahead journal.
    """
    if _pending_invocations.get() is not None:
        yield
        return
    pending = []
    token = _pending_invocations.set(pending)
    try:
        yield
    except BaseException as exc:
        if pending and pending[-1][1].outcome == "ok":
            event = pending[-1][1]
            event.post_return_failure = True
            event.outcome = type(exc).__name__
            event.detail = str(exc)[:200]
        raise
    finally:
        _pending_invocations.reset(token)
        for ledger, event in pending:
            try:
                ledger.record(event)
            except Exception:
                log.debug("invocation persistence failed", exc_info=True)


def record_invocation(ledger, event):
    """Queue an event for its acceptance boundary, or record a direct call."""
    pending = _pending_invocations.get()
    if pending is None:
        ledger.record(event)
    else:
        pending.append((ledger, event))


@contextmanager
def invocation(task, role, origin="seat"):
    token = invocation_context.set(dict(task=task, role=role, origin=origin))
    try:
        yield
    finally:
        invocation_context.reset(token)


class Origin:
    """Where an invocation came from. Four kinds, deliberately not merged."""

    #: A brain-trust seat or the orchestrator, dispatched by Quadratus.
    SEAT = "seat"
    #: A worker bee commissioned through :class:`quadratus.workers.WorkerPool`,
    #: inside its errand tree and its per-task budget.
    WORKER = "worker"
    #: A child the vendor CLI spawned on its own (Codex ``spawn_agent`` and
    #: friends). Observed, never dispatched; outside every Quadratus budget.
    NATIVE = "native"
    #: Vendor-internal activity appearing in a raw envelope -- Claude's Haiku
    #: rows, for instance. Not proof that Quadratus dispatched anything.
    AUXILIARY = "auxiliary"

    ALL = (SEAT, WORKER, NATIVE, AUXILIARY)

    #: The two the harness actually chose and can bound. Reported separately
    #: from the two it can only witness.
    CONTROLLED = (SEAT, WORKER)


@dataclass
class InvocationEvent:
    """One model call or retry, as the harness saw it.

    ``input_tokens``/``output_tokens`` are ``None`` when the call consumed a
    window but reported nothing -- a cancellation, a timeout, a killed child.
    That is recorded as unknown and stays unknown; substituting an estimate
    would launder a gap into a figure.

    ``requested_model`` and ``resolved_model`` differ whenever an alias,
    escalation, or seat fallback moved the call. ``invoked`` is False for a
    model that was *selected* but never reached -- the trial selected Grok as
    a collaborator and never called it, and counting that as coverage is
    exactly the claim this field exists to refuse.
    """

    task: str
    role: str
    origin: str = Origin.SEAT
    requested_model: Optional[str] = None
    resolved_model: Optional[str] = None
    selected: bool = True
    invoked: bool = False
    outcome: str = "unknown"
    seconds: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    canonical_model: Optional[str] = None
    invocation_id: Optional[str] = None
    wire_model: Optional[str] = None
    attempt: int = 1
    #: Set when the failure happened after the vendor returned text (a bad
    #: patch, an unparseable answer) rather than in transport. The two cost
    #: different things and the old ledger could not tell them apart.
    post_return_failure: bool = False
    #: Transport result before patch/scope acceptance. Older records lack it.
    provider_outcome: Optional[str] = None
    #: Bounded provider failure metadata; no tool arguments or transcript text.
    diagnostics: dict = field(default_factory=dict)
    #: Vendor session id, where one is known. Used to de-duplicate a child
    #: that several sources report.
    session_id: Optional[str] = None
    detail: str = ""
    #: The tail of the CLI's stderr for this attempt, and the failed tool
    #: calls the CLI reported (command, exit code, output tail), both bounded.
    #: Added after the Q9 canary (2026-09-22): both runs closed on a lead
    #: saying its sandbox could not start, with the bwrap error living only in
    #: the model's prose. The verifier could not check it and neither could
    #: anyone reading the record. Older records lack these and read as empty.
    stderr_tail: str = ""
    tool_failures: list = field(default_factory=list)

    @property
    def tokens_known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    @property
    def total_tokens(self) -> Optional[int]:
        if not self.tokens_known:
            return None
        return int(self.input_tokens or 0) + int(self.output_tokens or 0)

    def render(self) -> str:
        model = self.resolved_model or self.requested_model or "(unresolved)"
        if self.requested_model and self.resolved_model and self.requested_model != self.resolved_model:
            model = f"{self.requested_model} -> {self.resolved_model}"
        state = "invoked" if self.invoked else "selected, never invoked"
        tokens = (
            f"{self.total_tokens:,} tokens" if self.tokens_known else "usage unknown"
        )
        timing = f"{self.seconds:.1f}s" if self.seconds is not None else "duration unknown"
        bits = [
            f"{self.task}/{self.role}", f"[{self.origin}]", model, state,
            self.outcome, timing, tokens,
        ]
        if self.attempt > 1:
            bits.append(f"attempt {self.attempt}")
        if self.post_return_failure:
            bits.append("failed after return")
            if self.provider_outcome:
                bits.append(f"provider: {self.provider_outcome}")
        if self.detail:
            bits.append(self.detail[:120])
        return " | ".join(bits)


def safe_diagnostics(value) -> dict:
    """Whitelist provider metadata again at the durable event boundary."""
    if not isinstance(value, dict):
        return {}
    result = {}
    atom = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}\Z")
    reason = value.get('stop_reason')
    if isinstance(reason, str) and atom.fullmatch(reason):
        result['stop_reason'] = reason
    count = value.get('model_calls')
    if type(count) is int and 0 <= count <= 1_000_000:
        result['model_calls'] = count
    # Accept the older extractor spelling while keeping one stable ledger key.
    names = value.get('attempted_tools', value.get('tools_attempted'))
    if isinstance(names, list):
        result['attempted_tools'] = list(dict.fromkeys(
            name for name in names[:128] if isinstance(name, str) and atom.fullmatch(name)
        ))[:32]
    # Fan-out tools the CLI itself refused under the run-wide off mode: the
    # control holding, recorded by name only, same filter as attempted_tools.
    denied = value.get('denied_tools')
    if isinstance(denied, list):
        held = list(dict.fromkeys(
            name for name in denied[:128] if isinstance(name, str) and atom.fullmatch(name)
        ))[:32]
        if held:
            result['denied_tools'] = held
    # Usage provenance from the claude envelope: model names and one integer,
    # so a run's reported total can be traced to the rows it was built from.
    models = value.get('auxiliary_models')
    if isinstance(models, list):
        named = list(dict.fromkeys(
            name for name in models[:32] if isinstance(name, str) and atom.fullmatch(name)
        ))[:16]
        if named:
            result['auxiliary_models'] = named
    aux = value.get('auxiliary_tokens')
    if type(aux) is int and 0 <= aux <= 1_000_000_000:
        result['auxiliary_tokens'] = aux
    state = value.get('auxiliary_usage')
    if state in ('unknown', 'unattributed'):
        result['auxiliary_usage'] = state
    seat = value.get('seat_tokens')
    if type(seat) is int and 0 <= seat <= 1_000_000_000:
        result['seat_tokens'] = seat
    # How much of this call's input the model was being handed again. An agent
    # re-sends its whole conversation on every step, so a long loop's reported
    # total is mostly text it has already seen -- 402,816 of attempt 9's
    # 532,795. Nothing in the run record showed that, and it took opening the
    # private vendor envelopes to find it, which is exactly the kind of fact a
    # ledger exists to save someone from having to dig for. A subset of
    # ``input_tokens`` after provider normalisation, never an addition to it.
    reread = value.get('cached_input_tokens')
    if type(reread) is int and 0 <= reread <= 1_000_000_000:
        result['cached_input_tokens'] = reread
    # What the vendor itself says this call cost, where it says. Kept beside
    # our own API-price counterfactual rather than replacing it: the two
    # answer different questions, and on attempt 8's lead they differed 7.4x
    # because cache reads are billed at a fraction of fresh input. Which of
    # them a subscription window actually meters by is not documented, so
    # recording both is how that becomes answerable instead of assumed.
    cost = value.get('vendor_cost_usd')
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        if math.isfinite(cost) and 0 <= cost <= 1_000_000:
            result['vendor_cost_usd'] = round(float(cost), 6)
    return result


#: Bounds re-applied at the durable event boundary, whatever a provider sent.
STDERR_TAIL_LIMIT = 2_000
TOOL_FAILURE_LIMIT = 8
TOOL_FAILURE_TEXT = 500


def bounded_stderr(value) -> str:
    """The tail of a CLI's stderr, as text, never more than the limit."""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if not isinstance(value, str):
        return ""
    return value[-STDERR_TAIL_LIMIT:]


def bounded_tool_failures(value) -> list:
    """Failed tool calls as plain bounded dicts: command, exit code, output
    tail, or an error message. Anything else in the entry is dropped."""
    if not isinstance(value, list):
        return []
    kept = []
    for entry in value:
        if len(kept) >= TOOL_FAILURE_LIMIT:
            break
        if not isinstance(entry, dict):
            continue
        clean = {}
        for key in ("kind", "command", "status", "message", "output_tail"):
            text = entry.get(key)
            if isinstance(text, str) and text:
                clean[key] = text[-TOOL_FAILURE_TEXT:] if key == "output_tail" else text[:TOOL_FAILURE_TEXT]
        code = entry.get("exit_code")
        if type(code) is int:
            clean["exit_code"] = code
        if clean:
            kept.append(clean)
    return kept


@dataclass
class NativeChild:
    """A vendor-native child session the harness observed but did not dispatch."""

    session_id: str
    model: Optional[str] = None
    parent_session_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    tool_name: str = ""
    detail: str = ""

    @property
    def total_tokens(self) -> Optional[int]:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return int(self.input_tokens) + int(self.output_tokens)


@dataclass
class DelegationLedger:
    """Every invocation and retry, plus what the harness could not control.

    Append-only, like everything else that has to survive a run. Optionally
    mirrored to JSONL so the record outlives the process.
    """

    path: Optional[Path] = None
    events: List[InvocationEvent] = field(default_factory=list)
    native_children: Dict[str, NativeChild] = field(default_factory=dict)
    #: Free-text statements about what this run could not observe or bound.
    #: Rendered verbatim; an honest gap beats a confident total.
    blind_spots: List[str] = field(default_factory=list)

    def record(self, event: InvocationEvent) -> InvocationEvent:
        self.events.append(event)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(event)) + "\n")
            except OSError:  # noqa: BLE001 -- accounting never fails a run
                log.debug("could not persist invocation event", exc_info=True)
        return event

    def observe_native(self, child: NativeChild) -> None:
        """Record a vendor-native child, keeping the largest usage seen.

        Vendor session files restate the session total on every update, so the
        last or largest reading is the true one and summing readings inflates
        it. Keyed by session id, so the same child arriving from two sources is
        counted once.
        """
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.with_name("native-children.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(child)) + "\n")
            except OSError:
                log.debug("could not persist native observation", exc_info=True)
        existing = self.native_children.get(child.session_id)
        if existing is None:
            self.native_children[child.session_id] = child
            self.note_blind_spot(
                f"vendor-native child {child.session_id[:8]} "
                f"({child.model or 'unknown model'}) ran outside Quadratus "
                f"worker selection and budgets; observed, not controlled"
            )
            return
        # Cumulative snapshots: take the maximum, never the sum.
        self.native_children[child.session_id] = NativeChild(
            session_id=child.session_id,
            model=child.model or existing.model,
            parent_session_id=child.parent_session_id or existing.parent_session_id,
            input_tokens=_max_optional(existing.input_tokens, child.input_tokens),
            output_tokens=_max_optional(existing.output_tokens, child.output_tokens),
            tool_name=child.tool_name or existing.tool_name,
            detail=child.detail or existing.detail,
        )

    def note_blind_spot(self, text: str) -> None:
        if text not in self.blind_spots:
            self.blind_spots.append(text)

    # -- reporting -----------------------------------------------------------
    def controlled_tokens(self) -> int:
        """Tokens from calls Quadratus itself dispatched and can bound."""
        return sum(
            e.total_tokens or 0
            for e in self.events
            if e.origin in Origin.CONTROLLED and e.tokens_known
        )

    def native_tokens(self) -> int:
        """Tokens from observed native children, counted once each."""
        return sum(c.total_tokens or 0 for c in self.native_children.values())

    def unknown_events(self) -> List[InvocationEvent]:
        """Calls that consumed a window and reported nothing. Never zero."""
        return [e for e in self.events if e.invoked and not e.tokens_known]

    def invoked_models(self) -> List[str]:
        return sorted({
            e.resolved_model or e.requested_model or "(unresolved)"
            for e in self.events if e.invoked
        })

    def selected_never_invoked(self) -> List[str]:
        """Selected but never reached. Not coverage, and never reported as it."""
        def identity(e):
            return (e.task, e.canonical_model or e.requested_model or e.resolved_model)
        invoked = {identity(e) for e in self.events if e.invoked}
        return sorted({identity(e)[1] or "(unresolved)" for e in self.events
                       if e.selected and not e.invoked and identity(e) not in invoked})

    def render_report(self) -> str:
        lines = ["# Delegation and invocation record", ""]
        if not self.events and not self.native_children:
            lines.append("No invocations recorded.")
            return "\n".join(lines)

        by_origin: Dict[str, List[InvocationEvent]] = {}
        for event in self.events:
            by_origin.setdefault(event.origin, []).append(event)
        for origin in Origin.ALL:
            group = by_origin.get(origin)
            if not group:
                continue
            lines.append(f"## {origin}")
            for event in group:
                lines.append(f"- {event.render()}")
            lines.append("")

        if self.native_children:
            lines.append("## Vendor-native children (observed, not dispatched)")
            for child in self.native_children.values():
                total = (
                    f"{child.total_tokens:,} tokens"
                    if child.total_tokens is not None else "usage unknown"
                )
                lines.append(
                    f"- {child.session_id} ({child.model or 'unknown model'}) "
                    f"via {child.tool_name or 'native delegation'}: {total}"
                )
            lines.append("")

        totals = reconcile(self.events, self.native_children.values())
        controlled = totals['controlled_tokens']
        native = totals['native_child_tokens']
        lines.append("## Totals")
        lines.append(f"- Quadratus-dispatched: {controlled:,} tokens")
        if native:
            lines.append(
                f"- Vendor-native children (observed): {native:,} tokens, "
                f"outside Quadratus budgets"
            )
            lines.append(
                f"- Combined reported sum (conditional): {controlled + native:,} tokens. "
                "Parent/child counter overlap is unverified; this is not an "
                "established non-overlapping minimum."
            )
        unknown = self.unknown_events()
        if unknown:
            lines.append(
                f"- {len(unknown)} invocation(s) consumed a window and reported "
                f"no usage. These remain **unknown**, not zero:"
            )
            for event in unknown:
                lines.append(f"    - {event.render()}")
        lines.append(
            "- Subscription usage. Not an API charge; see usage.py for the "
            "separate API-price counterfactual."
        )
        lines.append("")

        never = self.selected_never_invoked()
        if never:
            lines.append("## Selected but never invoked")
            lines.append(
                "These models were chosen and never reached. This is not "
                "coverage:"
            )
            for model in never:
                lines.append(f"- {model}")
            lines.append("")

        if self.blind_spots:
            lines.append("## Not observable or not controllable by the harness")
            for spot in self.blind_spots:
                lines.append(f"- {spot}")
        return "\n".join(lines)


def _max_optional(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def reconcile(
    events: Iterable[InvocationEvent],
    children: Iterable[NativeChild],
) -> Dict[str, object]:
    """Fold invocations and observed children into one honest set of totals.

    De-duplicates children by session id and takes the maximum reading for
    each, because vendor session logs restate cumulative totals. A child whose
    session id matches an event the harness itself dispatched is *not* added
    again. Different session IDs alone do not prove that a vendor's parent
    counter excludes children. The combined sum remains conditional until
    that accounting contract is established.
    """
    events = list(events)
    dispatched_sessions = {e.session_id for e in events if e.session_id}

    folded: Dict[str, NativeChild] = {}
    for child in children:
        if child.session_id in dispatched_sessions:
            # Already counted as a Quadratus invocation; adding it here would
            # double it.
            continue
        existing = folded.get(child.session_id)
        if existing is None:
            folded[child.session_id] = child
            continue
        folded[child.session_id] = NativeChild(
            session_id=child.session_id,
            model=child.model or existing.model,
            parent_session_id=child.parent_session_id or existing.parent_session_id,
            input_tokens=_max_optional(existing.input_tokens, child.input_tokens),
            output_tokens=_max_optional(existing.output_tokens, child.output_tokens),
            tool_name=child.tool_name or existing.tool_name,
            detail=child.detail or existing.detail,
        )

    controlled = sum(
        e.total_tokens or 0 for e in events
        if e.origin in Origin.CONTROLLED and e.tokens_known
    )
    auxiliary = sum(
        e.total_tokens or 0 for e in events
        if e.origin == Origin.AUXILIARY and e.tokens_known
    )
    native = sum(c.total_tokens or 0 for c in folded.values())
    unknown = [e for e in events if e.invoked and not e.tokens_known]

    return {
        "controlled_tokens": controlled,
        "native_child_tokens": native,
        "auxiliary_tokens": auxiliary,
        "auxiliary_tokens_scope": "Explicit auxiliary InvocationEvent rows only; vendor aggregates are not included.",
        "combined_reported_tokens": controlled + native,
        "parent_child_overlap": "unverified" if native else "not_applicable",
        # Deprecated compatibility key: not a verified lower bound when child
        # counters may overlap their parents. New consumers use fields above.
        "known_minimum_tokens": controlled + native,
        "unknown_invocations": len(unknown),
        "unknown_detail": [e.render() for e in unknown],
        "native_children": sum(not c.session_id.startswith("unidentified:") for c in folded.values()),
        "unidentified_native_activity": sum(c.session_id.startswith("unidentified:") for c in folded.values()),
        "note": (
            "Subscription usage including cached and repeated input. Native "
            "children counted once at their highest cumulative reading, never "
            "summed across updates. The combined sum assumes child counters "
            "are additional to parent counters; that overlap is unverified. "
            "known_minimum_tokens is a deprecated compatibility key for that "
            "conditional sum, not an established lower bound. Auxiliary usage "
            "covers explicit auxiliary event rows only, not vendor aggregates. "
            "Unknown usage is unknown, not zero."
        ),
    }
