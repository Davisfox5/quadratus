"""The role packet of a task and its one extension, as a mixin of Session:
what a seat's packet holds, the ceiling it is sized against, the request a
lead may make for a larger one, and the orchestrator's decision on it.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from .codebase_map import CodebaseMap
from .delegation import invocation
from .providers import ProviderError, TurnLimitReached
from .reply_parsers import (
    _FALLBACK_PACKET_BYTES,
    _PACKET_CARRYING_CALLS,
    _PACKET_FREE_REQUESTS,
    _PACKET_GRANT_TAIL,
    _PACKET_REASON_MIN,
    _PACKET_REFUSE_TAIL,
    _PACKET_ROUND_SHARE,
    _packet_allowance_record,
    _packet_bytes,
    _packet_reason_problem,
    _parse_packet_request,
)
from .routing import WorkClass
from .run_budget import FinishReserved
from .session_prompts import _PACKET_NARROW_TEXT
from .session_types import CapabilityUnavailable, RunStalled, TaskSpec


class PacketsMixin:
    """The packet each role is handed, and the bounded request to extend it.

    Session inherits this; nothing here is used on its own.
    """

    def _role_packet(self, spec, role, *, limit=None):
        # Both packet builders cut reference notes at a byte limit, and the
        # map's block ends with the marker that closes its "data, not
        # instructions" label. So the block goes in whole, smaller if it has
        # to be, or not at all: never with its end cut off, which would leave
        # the rest of the prompt reading as part of the data.
        code_map = self.config.codebase_map
        for room in (None, 3000, 1200):
            notes = '' if code_map is None else (
                code_map.render(topics=['conventions']) if room is None
                else code_map.render(topics=['conventions'], max_chars=room))
            packet = self._role_packet_with(spec, role, notes, limit=limit)
            if CodebaseMap.block_survives(notes, packet):
                return packet
        return self._role_packet_with(spec, role, '', limit=limit)

    def _role_packet_with(self, spec, role, notes, *, limit=None):
        policy = self.config.repository_policy
        if policy is not None:
            # The task's approved allowance, or the policy's default: the
            # same lookup for every role, call and path that builds a packet.
            allowance = limit if limit is not None else self._packet_allowance(spec)
            return policy.role_packet(spec.scope, role, notes,
                                      **({"limit": allowance} if allowance is not None else {}))
        contract = '## Role packet\n' + (spec.scope.render() if spec.scope else 'Scope: not declared.')
        if len(contract.encode()) > _FALLBACK_PACKET_BYTES:
            raise RunStalled('Task scope exceeds the role packet limit; narrow the task')
        return contract + '\nConventions notes:\n' + notes.encode()[:3000].decode('utf-8', errors='ignore')

    def _packet_problem(self, spec: TaskSpec, decisions=()) -> str:
        """Why a task cannot start with its role packets, or "" (cycle21, run
        20261109T195618Z: t8's packet was built only at dispatch, after its
        slot was charged). Measured with the dispatch builder for every role
        the task carries; only size is a problem here, every other refusal is
        left for dispatch, where it stops as before.

        Over the policy's default the task needs an allowance (Davis,
        2026-11-09). Its lead is asked once, for this exact scope, whether
        the content is necessary as declared (``_packet_justify``); a
        justified request goes to the orchestrator, whose PACKET GRANT or
        PACKET REFUSE line in its next reply decides it (``decisions``,
        ``_packet_decide``). Nothing here approves anything, and nothing runs
        under an allowance until the orchestrator granted it. Over the run's
        ceiling (``_packet_ceiling``) no request is made. Every send-back goes
        through the bounded correction allowance its caller charges."""
        self._packet_progress = False
        policy = self.config.repository_policy
        if policy is None or getattr(spec, "scope", None) is None:
            self._packet_decisions_unused(spec, decisions, "no role packet of this task is measured against a "
                                                           "repository policy")
            return ""
        prepared = replace(spec, scope=policy.scope(spec.scope))
        role, needed = None, 0
        for name in self._packet_roles(prepared):
            try:
                size = policy.packet_needed(prepared.scope, name)
            except Exception:  # noqa: BLE001 -- any other refusal is dispatch's to raise
                return ""
            if role is None or size > needed:
                role, needed = name, size
        default = policy.packet_limit
        if needed <= default:
            self._packet_decisions_unused(spec, decisions, "the task's role packets fit the default")
            return ""
        fingerprint = self._packet_fingerprint(spec)
        problems = self._packet_decide(spec, fingerprint, needed, decisions) if decisions else []
        grant = self._packet_grant_for(spec)
        if grant is not None and int(grant.get("granted") or 0) >= needed:
            return ""
        parts = prepared.scope.component_bytes()
        components = dict(parts, fixed=needed - parts.get("scope", 0))
        self.packet_refusals.append(dict(task=spec.task_id, role=role, needed=needed, limit=default,
                                         components=components))
        text = (f"The {role} role packet for this task would be {needed:,} bytes; the limit is {default:,} (the "
                f"repository policy's default role packet limit). Its scope renders {parts.get('scope', 0):,} bytes: "
                f"proof scenarios {parts.get('proofs', 0):,}, acceptance {parts.get('acceptance', 0):,}, intended "
                f"result {parts.get('intended', 0):,}, the rest of the scope text {parts.get('other', 0):,}; the "
                f"policy's own checklists and check configuration take {components['fixed']:,}.")
        if problems:
            text += " Your packet decision was not applied: " + "; ".join(problems) + "."
        ceiling, basis = self._packet_ceiling()
        if needed > ceiling:
            self._note(f"task {spec.task_id}: role packet {needed:,} bytes is over this run's ceiling {ceiling:,}")
            return (text + f" That is over the largest allowance any extension can grant in this run, {ceiling:,} "
                    f"bytes ({basis}), so no extension can carry it. " + _PACKET_NARROW_TEXT)
        if self._explicit_tasks is not None:
            # No orchestrator runs a listed task, so nobody can grant one.
            return (text + " A listed task has no orchestrator to grant a larger allowance; " + _PACKET_NARROW_TEXT)
        request = self._packet_current_request(spec.task_id, fingerprint)
        if request is None:
            request = self._packet_justify(spec, prepared, fingerprint, role, needed, default, ceiling, basis,
                                           components)
            # A new justified request is the protocol's own step, paid by its
            # call, not a planner error: its send-back spends no correction,
            # for at most _PACKET_FREE_REQUESTS requests per task (the
            # correction allowance would otherwise stall a run whose fourth
            # task is the fourth to need an extension).
            asked = sum(1 for r in self.packet_requests if r.get("task") == spec.task_id and r.get("origin") == "intake")
            self._packet_progress = request.get("state") == "requested" and asked <= _PACKET_FREE_REQUESTS
        return text + " " + self._packet_request_text(request)

    def _packet_default(self) -> int:
        policy = self.config.repository_policy
        return policy.packet_limit if policy is not None else _FALLBACK_PACKET_BYTES

    def _packet_ceiling(self) -> Tuple[int, str]:
        """The largest role packet any grant can allow in this run, and how it
        was derived (``_PACKET_ROUND_SHARE``). Never below the default; the
        policy's 24,000-byte clamp on its own default does not bound it."""
        from .run_budget import TURN_CAP_MARGIN, TURN_CONTEXT_TOKENS, lead_turns_for
        from .usage import CHARS_PER_TOKEN
        budget = self._budget()
        per_call = getattr(getattr(budget, "limits", None), "max_tokens_per_call", None) if budget is not None else None
        turns = self.config.lead_max_turns or lead_turns_for(per_call)
        if per_call and turns:
            round_tokens = per_call * TURN_CAP_MARGIN / int(turns)
            basis = (f"half of one round of a capped call: the {int(per_call):,}-token per-call threshold times "
                     f"{TURN_CAP_MARGIN:g} over {int(turns)} rounds is {int(round_tokens):,} tokens a round")
        else:
            round_tokens = TURN_CONTEXT_TOKENS
            basis = f"half of the measured {TURN_CONTEXT_TOKENS:,}-token context of one round"
        ceiling = int(round_tokens * _PACKET_ROUND_SHARE * CHARS_PER_TOKEN)
        return max(ceiling, self._packet_default()), basis + f", at about {CHARS_PER_TOKEN:g} bytes a token"

    def _packet_fingerprint(self, spec) -> str:
        """What a grant approves: the roles and the scope exactly as every
        packet renders it. A changed scenario, path, acceptance line or
        intended result is a different request."""
        policy = self.config.repository_policy
        scope = policy.scope(spec.scope) if policy is not None else spec.scope
        text = ",".join(self._packet_roles(spec)) + "\n" + (scope.render() if scope is not None else "")
        return hashlib.sha256(text.encode()).hexdigest()

    def _packet_grant_for(self, spec):
        """The granted request in force for this task and scope, or None:
        bound to the task id it was requested for and to the scope it
        measured, or to a revision of it recorded by a reviewed correction
        (``_apply_scenario_correction``). The latest grant wins."""
        task_id = getattr(spec, "task_id", None)
        granted = [r for r in getattr(self, "packet_requests", ()) or ()
                   if r.get("state") == "granted" and r.get("task") == task_id]
        if not granted or getattr(spec, "scope", None) is None:
            return None
        fingerprint = self._packet_fingerprint(spec)
        for request in reversed(granted):
            if fingerprint == request.get("fingerprint") or any(
                    fingerprint == r.get("fingerprint") for r in request.get("revisions") or ()):
                return request
        return None

    def _packet_allowance(self, spec) -> Optional[int]:
        """This task's approved role packet allowance in bytes, or None for
        the policy's default."""
        grant = self._packet_grant_for(spec)
        return int(grant["granted"]) if grant is not None else None

    def _packet_current_request(self, task_id, fingerprint):
        return next((r for r in reversed(self.packet_requests)
                     if r.get("task") == task_id and r.get("fingerprint") == fingerprint
                     and r.get("origin") == "intake"), None)

    def _packet_seat_key(self) -> str:
        try:
            return self.seat().key
        except Exception:  # noqa: BLE001 -- the record names what it can
            return "orchestrator"

    def _packet_budget_problem(self, extra_bytes: int) -> str:
        """Why an extension cannot be afforded, or "": the request call needs
        a call and its reserve, and the larger packet is re-sent on every
        round of the calls that carry it (``_PACKET_CARRYING_CALLS``), all
        from what is left after the held finish. An estimate for refusing
        early; the calls are charged what they report."""
        budget = self._budget()
        if budget is None or not hasattr(budget, "snapshot"):
            return ""
        try:
            snap = budget.snapshot()
        except Exception:  # noqa: BLE001 -- a missing estimate refuses nothing
            return ""
        from .run_budget import lead_turns_for
        from .usage import CHARS_PER_TOKEN
        limits, fin = snap.get("limits") or {}, snap.get("finish_protection") or {}
        held = len(fin.get("pending") or [])
        calls = int(limits.get("max_calls") or 0) - int(snap.get("reserved_attempts") or 0) - held
        if calls < 2:
            return (f"the job has {max(0, calls)} ordinary call(s) left after the held finish, and a request and "
                    "the task need at least 2")
        reserve = int(limits.get("reserve_tokens_per_call") or 0)
        if not reserve:
            return ""
        turns = int(self.config.lead_max_turns or lead_turns_for(limits.get("max_tokens_per_call")) or 1)
        extra = -(-max(0, int(extra_bytes)) // int(CHARS_PER_TOKEN)) * turns * _PACKET_CARRYING_CALLS
        tokens = (int(limits.get("max_reported_tokens") or 0) - int(snap.get("reported_tokens") or 0)
                  - held * reserve)
        if tokens < reserve + extra:
            return (f"the job has {max(0, tokens):,} tokens left after the held finish; the request call's reserve "
                    f"of {reserve:,} and the larger packet's estimated re-send cost of {extra:,} ({extra_bytes:,} "
                    f"more bytes on each of {turns} rounds of {_PACKET_CARRYING_CALLS} calls) do not fit")
        return ""

    def _packet_lead(self, spec) -> str:
        """The lead asked to justify: the one dispatch will use (picked once
        and held for this task id, as the capture check does)."""
        chosen = getattr(self, "_dispatch_lead", None)
        if chosen and chosen[0] == spec.task_id:
            return chosen[1]
        lead = self._pick_lead(spec)
        self._dispatch_lead = (spec.task_id, lead)
        return lead

    def _packet_justify(self, spec, prepared, fingerprint, role, needed, default, ceiling, basis, components):
        """One bounded call to the task's lead, before anything of the task
        runs: is the content necessary as declared, and how large must the
        packet be? It carries the scope as text for judgment, not a role
        packet: the lead is not working, cannot edit (a source copy, the
        declaration round cap) and its reply grants nothing. Asked once per
        task and scope; any failure is a request not made, never a stop."""
        rid = f"PX-{spec.task_id}-{sum(1 for r in self.packet_requests if r.get('task') == spec.task_id) + 1}"
        request = dict(id=rid, task=spec.task_id, origin="intake", fingerprint=fingerprint, role=role,
                       roles=list(self._packet_roles(prepared)), needed=needed, default=default, ceiling=ceiling,
                       ceiling_basis=basis, components=dict(components), lead=None, state=None, requested=None,
                       decisions=[], revisions=[])
        self.packet_requests.append(request)
        short = self._packet_budget_problem(needed - default)
        if short:
            request.update(state="unaffordable", problem=short[:400])
            return request
        try:
            lead = self._packet_lead(spec)
        except (RunStalled, CapabilityUnavailable) as exc:
            request.update(state="no_lead", problem=str(exc)[:400])
            return request
        request["lead"] = lead
        prompt = (
            f"--- ROLE PACKET EXTENSION REQUEST ({spec.task_id}) ---\n"
            f"You are named to lead this task. Before it starts, the harness measured the role packet its calls "
            f"would carry: {needed:,} bytes against the repository policy's default of {default:,}. The packet is "
            f"the task's scope as rendered (below) plus the policy's own checklists and check configuration "
            f"({components.get('fixed', 0):,} bytes). Required text is never cut, so the task cannot start at the "
            f"default. Measured parts of the scope: proof scenarios {components.get('proofs', 0):,}, acceptance "
            f"{components.get('acceptance', 0):,}, intended result {components.get('intended', 0):,}, the rest "
            f"{components.get('other', 0):,}.\n\n"
            f"You may ask the orchestrator for a larger allowance for this task only, at most {ceiling:,} bytes "
            f"({basis}). Ask when the content is necessary as declared: when stating it more briefly or splitting "
            "the task would lose required context or be counterproductive. When a briefer statement or a split "
            "would serve the task as well, say so instead. You change nothing in this call and run nothing; read "
            "the source copy only if you need it to judge what the task needs.\n\n"
            "## Task (verbatim)\n" + (spec.description or "").strip() + "\n\n"
            "## Scope as rendered\n" + prepared.scope.render() + "\n\n"
            "Reply with exactly one of these two forms and nothing else:\n"
            f"PACKET EXTENSION: <bytes, at least {needed:,} and at most {ceiling:,}>\n"
            "NECESSARY: <the content the packet must carry and what each part is for>\n"
            "WHY NOT SHORTER: <why a briefer statement or a split would lose required context or be "
            "counterproductive>\n\nor\n\n"
            "PACKET NARROW: <how to state the task more briefly or split it, keeping every covered requirement's "
            "scenario>\n\n"
            f"Each line says something checkable (at least {_PACKET_REASON_MIN} characters). The orchestrator "
            "decides; your reply grants nothing. A reply in neither form is no request.")
        self._count("packet_justification")
        self._note(f"task {spec.task_id}: role packet {needed:,} bytes over the default {default:,}; asking "
                   f"its lead {lead} whether an extension is justified")
        try:
            with invocation("packet-request", "packet-justification"):
                reply = self._invoke_model(lead, prompt)
        except FinishReserved as exc:
            request.update(state="unaffordable", problem=f"the request call was refused to keep the finish: {exc}"[:400])
            return request
        except TurnLimitReached as exc:
            if exc.partial_text:
                request["partial_artifact"] = self.store.put(exc.partial_text, kind="packet-justification-capped",
                                                             author=lead).id
            request.update(state="capped", problem=(f"the request call stopped at {exc.turns or 'its'} rounds "
                                                    "before answering; nothing it wrote is read as a request"))
            return request
        except ProviderError as exc:
            request.update(state="call_failed", problem=f"the request call failed: {str(exc)[:300]}")
            return request
        reply = reply if isinstance(reply, str) else ""
        request["reply_artifact"] = self.store.put(reply, kind="packet-justification", author=lead).id
        parsed = _parse_packet_request(reply)
        if parsed["kind"] == "narrow":
            request.update(state="narrow", advice=parsed["advice"])
        elif parsed["kind"] == "malformed":
            request.update(state="malformed", problem=parsed["problem"])
        elif parsed["bytes"] < needed:
            request.update(state="malformed", requested=parsed["bytes"],
                           problem=f"it asks for {parsed['bytes']:,} bytes, below the measured {needed:,}")
        elif parsed["bytes"] > ceiling:
            request.update(state="malformed", requested=parsed["bytes"],
                           problem=f"it asks for {parsed['bytes']:,} bytes, over this run's ceiling of {ceiling:,}")
        else:
            request.update(state="requested", requested=parsed["bytes"], necessary=parsed["necessary"],
                           why_not_shorter=parsed["why"])
        self._note(f"task {spec.task_id}: packet request {rid} from {lead}: {request['state']}")
        return request

    def _packet_request_text(self, request: dict) -> str:
        """What the planner is told about a task's packet request."""
        state, rid, lead = request.get("state"), request.get("id"), request.get("lead") or "its lead"
        art = request.get("reply_artifact")
        where = f" (its whole reply is artifact {art}; FETCH: {art})" if art else ""
        if state == "requested":
            needed, requested = int(request["needed"]), int(request["requested"])
            return (f"Its lead ({lead}) asks for a larger allowance for this task only, request {rid}: "
                    f"{requested:,} bytes{where}. NECESSARY, in the lead's words: {request.get('necessary')} "
                    f"WHY NOT SHORTER, in the lead's words: {request.get('why_not_shorter')} "
                    "Decide on its merits; nothing of the task runs until you do. To approve, name this task again "
                    f"unchanged and add the line 'PACKET GRANT: {rid} <bytes> | because: <why the justification "
                    f"holds>', granting at least the measured {needed:,} and at most {requested:,} bytes. To refuse, "
                    f"add 'PACKET REFUSE: {rid} | because: <why>' and name the task narrower or split. A grant covers "
                    "this task's scope exactly as measured: any change to it is a new request.")
        if state == "refused":
            return (f"You refused request {rid} ({request.get('refusal_reason') or 'no reason given'}); this exact "
                    "scope is not asked again. " + _PACKET_NARROW_TEXT)
        if state == "narrow":
            return (f"Its lead ({lead}) recommends narrowing over an extension{where}: {request.get('advice')} "
                    + _PACKET_NARROW_TEXT)
        if state == "granted":
            return f"Request {rid} was granted {int(request.get('granted') or 0):,} bytes."
        reason = request.get("problem") or "no reason recorded"
        if state == "malformed":
            return (f"Its lead's reply made no valid request{where} ({reason}), so there is nothing to grant. "
                    + _PACKET_NARROW_TEXT)
        return f"No extension can be requested for it ({reason}). " + _PACKET_NARROW_TEXT

    def _packet_decide(self, spec, fingerprint, needed: int, decisions, *, floor=None, grants: str = "") -> List[str]:
        """Apply the orchestrator's decision lines. A line decides only a
        pending request the harness issued under its id. A refusal applies
        whatever task the line rides on (it widens nothing; the planner
        refuses and names a narrower task in one reply). A grant applies only
        to this task and the scope its request measured, with a size from
        the measured need to the lead's request, within the ceiling, and a
        reason; ``grants`` names why no grant can apply here (a task that
        fits). Every line is recorded; the problems are returned."""
        problems: List[str] = []
        ceiling, _ = self._packet_ceiling()
        seat = self._packet_seat_key()
        by_id: Dict[str, List[dict]] = {}
        for line in decisions:
            by_id.setdefault(line["id"], []).append(line)
        for rid, lines in by_id.items():
            record = dict(task=spec.task_id, seat=seat, lines=[line["line"][:400] for line in lines])
            request = next((r for r in self.packet_requests if r.get("id") == rid), None) if rid else None
            if request is None:
                self.packet_decisions_rejected.append(dict(record, id=rid, problem="no request the harness issued "
                                                                                   "has this id"))
                problems.append(f"'{rid or '(no id)'}' names no request the harness issued, so it decides nothing")
                continue
            if len(lines) > 1:
                request["decisions"].append(dict(record, outcome="not applied: more than one decision line"))
                problems.append(f"{rid} carries more than one decision line; one line decides a request")
                continue
            line = lines[0]
            if request.get("state") != "requested":
                request["decisions"].append(dict(record, outcome=f"not applied: the request is {request.get('state')}"))
                problems.append(f"{rid} is {request.get('state')}, so there is nothing to decide")
                continue
            if line["verb"] == "GRANT" and grants:
                request["decisions"].append(dict(record, outcome=f"not applied: {grants}"))
                continue
            if line["verb"] == "GRANT" and (request.get("task") != spec.task_id
                                            or request.get("fingerprint") != fingerprint):
                why = ("another task" if request.get("task") != spec.task_id
                       else "a different scope than this task now declares")
                request["decisions"].append(dict(record, outcome=f"not applied: made for {why}"))
                problems.append(f"{rid} was requested for {why}; a grant covers only the scope its request measured, "
                                "so a changed task needs its own request")
                continue
            artifact = self.store.put(line["line"], kind="packet-decision", author=seat).id
            if line["verb"] == "REFUSE":
                match = _PACKET_REFUSE_TAIL.match(line["rest"])
                reason = (match.group(1) if match else line["rest"]).strip()
                request.update(state="refused", refusal_reason=reason[:600] or None, decided_by=seat,
                               decision_artifact=artifact)
                request["decisions"].append(dict(record, outcome="refused", artifact=artifact))
                self._note(f"task {spec.task_id}: packet request {rid} refused by {seat}")
                continue
            match = _PACKET_GRANT_TAIL.match(line["rest"])
            size = _packet_bytes(match.group(1)) if match else None
            reason = match.group(2).strip() if match else ""
            bad = ("the line is not 'PACKET GRANT: <id> <bytes> | because: <reason>'" if size is None
                   else _packet_reason_problem(reason, "the grant's reason"))
            if not bad and size < needed:
                bad = f"{size:,} bytes is below the measured {needed:,} the packet needs"
            if not bad and floor is not None and size < floor:
                bad = f"{size:,} bytes does not raise the task's current allowance ({floor - 1:,})"
            if not bad and size > int(request.get("requested") or 0):
                bad = f"{size:,} bytes is more than the lead asked for ({int(request.get('requested') or 0):,})"
            if not bad and size > ceiling:
                bad = f"{size:,} bytes is over this run's ceiling of {ceiling:,}"
            if bad:
                request["decisions"].append(dict(record, outcome=f"not applied: {bad}", artifact=artifact))
                problems.append(f"the grant on {rid} was not applied: {bad}")
                continue
            request.update(state="granted", granted=size, grant_reason=reason[:600], decided_by=seat,
                           decision_artifact=artifact)
            request["decisions"].append(dict(record, outcome="granted", artifact=artifact))
            self._note(f"task {spec.task_id}: packet request {rid} granted {size:,} bytes by {seat}")
        return problems

    def _packet_decisions_unused(self, spec, decisions, why: str) -> None:
        """Decision lines on a task that needs no grant: a refusal still
        decides its pending request, a grant is recorded and never applied."""
        if decisions:
            self._packet_decide(spec, None, 0, decisions, grants=why)

    def _packet_record_dispatch(self, spec) -> None:
        """The allowance a task starts with, on its own record: the request,
        the lead's justification, the decision and its reason."""
        grant = self._packet_grant_for(spec)
        outcome = getattr(self, "_outcome", None)
        if grant is None or outcome is None or getattr(outcome, "task_id", None) != spec.task_id:
            return
        outcome.packet_allowance = _packet_allowance_record(grant)
        if spec.task_id not in grant.setdefault("dispatched", []):
            grant["dispatched"].append(spec.task_id)

    def _packet_roles(self, spec: TaskSpec):
        """The role packets a task's calls carry: lead and reviewer always,
        verifier on a task classified as security. Read from the spec, not the
        contract, which is not fixed yet; a verifier packet on a route the
        contract alone selects is still checked where it is built."""
        if spec.work_class == WorkClass.SECURITY:
            return ("lead", "reviewer", "verifier")
        return ("lead", "reviewer")
