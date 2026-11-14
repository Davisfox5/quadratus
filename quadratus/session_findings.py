"""The review finding registry of the session engine, as a mixin of Session:
stable finding ids and per-id dispositions, RESOLVES and inheritance by a
continuation, the re-check of blocking findings, review notes, review
debt, and what is still open when a task closes.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import delivery, judgment
from .check_facts import _check_ids
from .memory import TaskMemory
from .providers import TurnLimitReached
from .reply_parsers import (
    _BLOCKING_LINE,
    _COVERS,
    _RESOLVES,
    _behavioural_error,
    _cited_cases,
    _clean_goal_review,
    _finding_id,
    _met_lines,
    _proof_cases,
    _requirement_verdicts,
    _review_judgments,
    _typed_finding,
    _typed_near_miss,
    _worst_impact,
    boundary_classes,
)
from .run_budget import FinishReserved
from .session_prompts import (
    _AUTHORITY_ORDER,
    _CONTRACT_BOUNDARY_RULE,
    _MISSING_SCENARIO_RULE,
    _OWED_SETTLEMENT_ATTEMPTS,
    _REPAIR_OUTCOME_RULE,
    _STANDING_REASON_CHARS,
)
from .session_types import TaskSpec, _invocation_role
from .verdicts import REVIEW_COMPLETE as _REVIEW_COMPLETE
from .verdicts import has_blocking_finding as _has_blocking_finding
from .verdicts import is_empty_note as _is_empty_note
from .verdicts import requirement_line as _requirement_line
from .verdicts import resolved_verdict as _resolved_verdict
from .verdicts import review_complete as _review_complete
from .verdicts import unstyled as _unstyled

# One logger name for the engine, wherever its code lives.
log = logging.getLogger("quadratus.session")


class FindingsMixin:
    """Every review finding by id, from the review that raised it to its settlement.

    Session inherits this; nothing here is used on its own.
    """

    def _record_review(self, spec: TaskSpec, peer: str, role: str, note: str, *, complete: bool,
                       capped: bool = False, raised: Optional[int] = None) -> None:
        """One review a seat gave, for the reviewer record: read from the
        reply the task already has, never a call of its own."""
        try:
            from .verdicts import blocking_findings
            if raised is None:
                raised = len(blocking_findings(note))
                if role == "design-review" and not capped:
                    # A design review is APPROVED or BLOCKING lines. Any other
                    # reply gave no verdict: the blocker the harness writes
                    # for it is not a finding this reviewer raised.
                    complete = bool(raised) or str(note or "").strip() == "APPROVED"
                try:
                    # A verifier's JSON verdict lists its findings in a field.
                    listed = json.loads(note).get("blocking_findings") if note.lstrip().startswith("{") else None
                    raised = len(listed) if isinstance(listed, list) else raised
                except (ValueError, AttributeError):
                    pass
            count = 0 if capped else int(raised)
            self.reviewer_records.append(dict(
                task=spec.task_id, model=peer, role=role, complete=bool(complete) and not capped,
                capped=bool(capped), blocking_raised=count, blocking_resolved=0, blocking_unresolved=0,
                recheck=None, recheck_rounds=0))
        except Exception:  # noqa: BLE001 -- the record never fails a task
            log.debug("could not record a review", exc_info=True)

    def _record_recheck(self, spec: TaskSpec, peer: str, verdict: str) -> None:
        """What a reviewer's re-check said of its own blocking findings. It
        answers for them as a set, so RESOLVED counts all of them resolved
        and UNRESOLVED all of them standing; a capped re-check counts none."""
        for record in reversed(self.reviewer_records):
            if (record["task"], record["model"], record["role"]) == (spec.task_id, peer, "collaborator"):
                raised = record["blocking_raised"]
                record.update(recheck=verdict, recheck_rounds=record["recheck_rounds"] + 1,
                              blocking_resolved=raised if verdict == "resolved" else 0,
                              blocking_unresolved=raised if verdict == "unresolved" else 0)
                return

    def _hold_open_finding(self, spec, peer: str, verdict: str, *, record=None, original=None, outcome=None,
                           carry: bool = True) -> None:
        """One review finding left open at a close, into the debt accounting
        every later reader uses: the legacy open text (or, for a finding
        with no basis and no immediate risk, the note that carries it to the
        final audit by id) and the held entry (``unresolved_reviews``) the
        post-merge recheck, the evidence loop, the hold and a RESOLVES
        recheck read. The same block the fix loop always wrote, shared with
        the capped and failed closes (one finding identity, 2026-10-28)."""
        fid = record["fid"] if record is not None else _finding_id(verdict)
        # The task outcome the fact lands on: the running task's, or the one
        # a parent names for a child it is holding findings for (a raised or
        # unmerged child, 2026-10-29).
        holder = outcome if outcome is not None else self._outcome
        if (carry and record is not None and record.get("audit_carried")
                and self.config.requirements_ledger and self.memory.ledger.requirements):
            # Carried only where an audit will read it: with no requirements
            # ledger there is no final audit, so the finding stays an open
            # one and stops the run as before. No basis and no immediate
            # risk: it does not stop the run here. It stays open on the
            # registry by id, is carried to the final audit, and the
            # requirements it touches are not met until a judge closes it.
            if holder is not None:
                holder.note("unverified", f"Task {spec.task_id}: {fid} open, carried to the final "
                                          f"audit by id: {record['claim'][:200]}", terminal=False)
            self._note(f"task {spec.task_id}: {fid} has no basis and claims no immediate risk; carried "
                       "to the final audit by id")
        elif outcome is not None:
            self.open_findings.append(verdict)
            outcome.note("unverified", verdict)
        else:
            self._open_finding("unverified", verdict)
        text = original if original is not None else (record["text"] if record is not None else verdict)
        self.unresolved_reviews.append(dict(task=spec.task_id, peer=peer, finding=(text or "").strip(),
                                            text=verdict, description=spec.description[:2000],
                                            finding_id=fid))

    def _hold_task_findings(self, spec, why: str, *, outcome=None, reasons=None) -> None:
        """Every open review finding of this task that the debt accounting
        does not hold yet, held there at a capped or failed close (one
        finding identity, 2026-10-28). controller-continuation-01: t4's
        second revision hit the turn cap, ``_close_turn_limited`` returned
        before the block above ran, and eight of Opus's open ids were in the
        registry but in no held entry, so the post-merge recheck asked about
        one finding, the planner's RESOLVES was refused, and the audit was
        the first judge to see them. The finding keeps its reviewer: nothing
        here judges it, and ``hold`` says why it is held."""
        held = {e.get("finding_id") for e in self.unresolved_reviews}
        for record in self._finding_records(task=spec.task_id):
            if record["fid"] in held:
                # Already held (owned debt from a recheck with no verdict):
                # not held twice, but the close's own reason is recorded.
                if (reasons or {}).get(record["fid"]) and not record.get("hold"):
                    record["hold"] = reasons[record["fid"]]
                continue
            why_here = (reasons or {}).get(record["fid"]) or why
            record["hold"] = why_here
            verdict = f"{record['fid']}: UNRESOLVED ({why_here}): {record['claim']}"
            self._hold_open_finding(spec, record["author"], verdict, record=record, original=record.get("text"),
                                    outcome=outcome)
            self._note(f"task {spec.task_id}: {record['fid']} by {record['author']} held open: {why_here}")

    def _findings_block(self, carried: List[dict], standing: List[dict]) -> str:
        if not carried and not standing:
            return ""
        lines = ["\n\n## Review findings still open (harness records)"]
        if carried:
            lines.append("Carried to you by id (no basis was given and no immediate risk is claimed; judge "
                         "each by its id):")
            lines.extend(f"- {r['fid']} ({r['task']}, raised by {r['author']}, impact {r['impact']}): {r['claim']}"
                         for r in carried)
        if standing:
            lines.append("Standing (basis given or an immediate risk; only the reviewer that raised each one "
                         "closes it, so the requirements they touch are not met while they stand):")
            lines.extend(f"- {r['fid']} ({r['task']}, {r['author']}, basis {r['basis']['kind']}"
                         f"{': ' + r['basis']['ref'] if r['basis'].get('ref') else ''}, impact {r['impact']}, "
                         f"{r['state']}): {r['claim']}"
                         + (f" [{self._scope_route_text(r['task'], r['scope_paths'])}]" if r.get("scope_paths") else "")
                         for r in standing)
        return "\n".join(lines)

    def _close_review_finding_by_id(self, fid: str, why: str) -> None:
        """Close the open texts and facts recorded for a review finding."""
        for entry in self.unresolved_reviews:
            if entry.get("finding_id") == fid:
                self._close_review_finding(entry, why)
        for text in list(self.open_findings):
            if _finding_id(text) == fid or f" {fid} " in text or text.endswith(fid):
                self.open_findings.remove(text)
        self._refresh_recovery_results()

    def _close_review_finding(self, entry: dict, why: str) -> None:
        """Close one held review finding: its open text and its fact."""
        text = entry["text"]
        if text in self.open_findings:
            self.open_findings.remove(text)
        outcomes = [o for o in self.task_outcomes if o.task_id == entry["task"]]
        for outcome in outcomes + [self.run_outcome]:
            for fact in outcome.facts:
                if fact.kind == "unverified" and fact.active and (fact.full or fact.detail) in (text, text[:400]):
                    fact.recovered = True
        for outcome in outcomes:
            outcome.note("unverified", f"{why}: {entry.get('finding', text)[:200]}", terminal=False)

    def _open_findings_touching(self, rid: str) -> List[str]:
        """Open review finding ids naming ``rid``, or raised on a task that
        covers it when the finding names no requirement."""
        covers = {o.task_id: set(o.covers or ()) for o in self.task_outcomes}
        out = []
        for record in self.review_findings:
            if record.get("closed"):
                continue
            named = record.get("requirement")
            if named and named[0] == rid:
                out.append(record["fid"])
            elif not named and rid in covers.get(record.get("task"), ()):
                out.append(record["fid"])
        return out

    def _normalize_resolves(self, spec):
        """Read a RESOLVES line by its typed ids before admission (cycle10,
        PR73 6722947726: the planner wrote ``RESOLVES: R17 (audit), R24
        (audit)`` for a repair of what the final audit found not met, every
        id was read as an unknown finding, and the sendbacks spent the
        correction allowance until the run stalled). A parenthesised
        annotation is not an id and is dropped. A requirement id whose only
        hold is the audit's NOT MET (no open finding names it) is the
        COVERS route the DONE refusal names, so it moves to COVERS and the
        same independent audit judges the repair. Everything else stays on
        the line for ``_resolves_problem``: a finding id, a requirement held
        by an open finding (its typed ids are named back), one that is not
        NOT MET, one that is not listed, and any malformed token. The line
        as written and what moved are kept on ``resolves_normalized``."""
        if not self.config.requirements_ledger:
            return spec
        text = spec.description or ""
        matches = list(_RESOLVES.finditer(text))
        if len(matches) != 1:
            return spec
        match = matches[0]
        raw = match.group(1)
        dropped = re.findall(r"\([^()]*\)", raw)
        tokens = [t for t in re.split(r"[\s,]+", re.sub(r"\([^()]*\)", " ", raw).strip()) if t]
        ledger = self.memory.ledger
        moved = []
        for token in tokens:
            if (re.fullmatch(r"R\d+", token) and token in ledger.requirements
                    and str(ledger.requirement_status.get(token) or "").startswith("NOT MET")
                    and not self._open_findings_for(token) and token not in moved):
                moved.append(token)
        # A finding the registry holds closed is no longer resolvable: the
        # id is dropped and recorded, and the task stands or falls on its
        # COVERS and ordinary admission. Nothing reopens it (cycle10: the
        # planner was shown a frozen close-out listing RFT4-14 unresolved).
        closed_reviews = {r["fid"] for r in self.review_findings if r.get("closed")}
        closed_audit = {f["id"] for f in self.findings if f.get("status") not in (None, "open")}
        closed = [t for t in dict.fromkeys(tokens) if t in closed_reviews or t in closed_audit]
        if not moved and not dropped and not closed:
            return spec
        kept = [t for t in tokens if t not in moved and t not in closed]
        # A line left with no id is kept empty, so it is refused as naming
        # none; one whose ids all moved is gone.
        line = ("RESOLVES: " + ", ".join(kept)) if kept else ("" if moved or closed else "RESOLVES:")
        body = text[:match.start()] + line + text[match.end():]
        if moved:
            cover = _COVERS.search(body)
            if cover:
                have = re.findall(r"R\d+", cover.group(1))
                ids = have + [r for r in moved if r not in have]
                body = body[:cover.start()] + "COVERS: " + ", ".join(ids) + body[cover.end():]
            else:
                body = body.rstrip() + "\nCOVERS: " + ", ".join(moved)
        self.resolves_normalized.append(dict(task=spec.task_id, written=raw.strip()[:300], moved_to_covers=moved,
                                             annotations=[d[:80] for d in dropped], kept=kept,
                                             closed_dropped=closed))
        self._note(f"{spec.task_id}: RESOLVES read by typed ids"
                   + (f"; {', '.join(moved)} (held NOT MET by the audit) moved to COVERS" if moved else "")
                   + (f"; {', '.join(closed)} already closed on the registry, dropped (not reopened)"
                      if closed else "")
                   + (f"; annotation(s) {', '.join(dropped)} dropped" if dropped else ""))
        return replace(spec, description=body.strip())

    def _scope_resolves_problem(self, resolves, spec) -> str:
        """A RESOLVES naming a finding whose repair site its lead declared
        outside its task's scope, from a task whose SCOPE permits none of
        those paths, is sent back once per finding and path set (cycle04):
        the same grant would meet the same block. Named again as written,
        it proceeds: the declaration is a claim, and the planner decides."""
        scope = getattr(spec, "scope", None)
        if scope is None or not scope.permitted_paths:
            return ""
        by_id = {f["id"]: f for f in self.findings}
        reviews = {r["fid"]: r for r in self.review_findings}
        blocked = []
        for fid in resolves:
            paths = (list((by_id[fid].get("scope") or {}).get("paths") or []) if fid in by_id
                     else list(reviews[fid].get("scope_paths") or []) if fid in reviews else [])
            if not paths or any(scope.permits(p) for p in paths):
                continue
            key = f"{fid}:{','.join(sorted(paths))}"
            if key in self.scope_sendbacks:
                continue
            self.scope_sendbacks.append(key)
            blocked.append(f"{fid} (declared repair site: {', '.join(paths)})")
        if not blocked:
            return ""
        return ("RESOLVES names findings whose repair site was declared outside the finding task's scope, and "
                "this task's SCOPE permits none of those paths: " + "; ".join(blocked) + ". Include the "
                "paths the repair needs, or name the task again as written to proceed (sent back once).")

    def _closed_findings_note(self) -> str:
        """The findings the registry holds closed now, by id, with their
        judge (cycle10 run 20261108T163004Z: t4's close-out, frozen in the
        append-only ledger, listed RFT4-14 as unresolved; the post-merge
        recheck closed it; the planner prompt carried only the close-out,
        and two tasks naming it were refused as already resolved until the
        correction allowance stopped the run). The ledger stays as written:
        this block is the current disposition, and says so."""
        if not (self.config.requirements_ledger and self.memory.ledger.requirements):
            return ""
        closed = [f"{r['fid']}: {str(r.get('state') or 'closed').upper()} by "
                  f"{((r.get('history') or [{}])[-1] or {}).get('author') or 'unknown'}"
                  for r in self.review_findings if r.get("closed")]
        closed += [f"{f['id']}: resolved by {f.get('resolved_by') or 'unknown'}" for f in self.findings
                   if f.get("status") not in (None, "open")]
        if not closed:
            return ""
        shown = closed[-20:]
        return ("\n\n--- FINDINGS CLOSED NOW (the registry, current) ---\n"
                "These findings are closed on the registry as of this prompt. A close-out or ledger entry "
                "above that lists one of them as unresolved or open is history from when that task closed. "
                "RESOLVES names only open findings; a task still needed for a closed finding's area is "
                "ordinary work under COVERS, and a RESOLVES naming a closed id has that id dropped and is "
                "recorded, never reopened.\n"
                + ("" if len(closed) <= 20 else f"({len(closed) - 20} earlier closed ids not shown.)\n")
                + "\n".join(f"- {line}" for line in shown))

    def _open_findings_prompt(self) -> str:
        opened = [f for f in self.findings if f["status"] == "open"]
        # Review ids are named only where RESOLVES can take them (the
        # requirements ledger on) and their claims are shown (the
        # obligations view, which that ledger renders).
        reviews = ([r for r in self.review_findings if not r.get("closed")]
                   if self.config.requirements_ledger and self.memory.ledger.requirements else [])
        if not opened and not reviews:
            return ""
        if not opened:
            return "\n\n--- OPEN FINDINGS ---\n" + self._review_resolves_note(reviews)
        lines = [f"- {f['id']} (found by {f['task']}, requirements {', '.join(f['requirements'])}): "
                 + (f"the {f['view']} render of {f['target']} is {f['width']}px wide at a "
                    f"{f['viewport']}px viewport" if f.get("target") else f["message"])
                 + (f" (output artifact {(f.get('check') or {}).get('output_artifact') or 'unavailable'})"
                    if f.get("kind") == "check.failed" else "")
                 + (f" ({self._scope_route_text(f['task'], f['scope']['paths'])}; failed cases declared: "
                    f"{len(f['scope']['cases_declared'])}, not declared: {len(f['scope']['cases_undeclared'])}"
                    + ("" if f["scope"].get("record_complete") else ", failure record incomplete") + ")"
                    if f.get("scope") else "")
                 + (f" (reopened: {f['reopened']})" if f.get("reopened") else "")
                 + (f" (last attempt: {f['last_attempt'][:160]})" if f.get("last_attempt") else "")
                 for f in opened]
        return ("\n\n--- OPEN FINDINGS ---\nThese measured faults keep their requirements NOT MET. "
                "Name a task that fixes one or more of them, with a "
                "COVERS line including their requirements and one line 'RESOLVES: F<n>' naming them "
                "(one RESOLVES line, ids from this list only, each once, no duplicates). A render "
                "finding needs a UI task named alone, and resolves only if the task's own fresh "
                "renders of the same page, reached by the same interaction steps and fixture, verify "
                "with no overflow and are approved by the cross-vendor design review. A failed-check "
                "finding resolves when the resolving task's own integration check passes and it "
                "closes with no new finding; its failed cases and output are under 'Failed checks still "
                "standing' below, and its output artifact is named on its line. " + _REPAIR_OUTCOME_RULE
                + "\n" + "\n".join(lines)
                + ("\n\n" + self._review_resolves_note(reviews) if reviews else ""))

    def _standing_reason_note(self, record: dict) -> str:
        """The planner's view of an open review finding's standing question
        (cycle16, cycle15 RFT4-11: t6 and t7 were planned from a 140-character
        claim while the owner's recheck said which declared scenarios were
        missing). Data from the record, bounded, attributed and labelled;
        the owner still judges closure."""
        reason = self._standing_reason(record, limit=_STANDING_REASON_CHARS)
        if reason is None:
            return ""
        return (f"\n  standing reason, as {reason['author']} last wrote it ({reason['role']}, {reason['where']}; "
                f"recorded, not a verdict): {reason['why']}")

    def _review_resolves_note(self, reviews: List[dict]) -> str:
        """The open review findings RESOLVES may name, by id only: their
        claims, reviewers and states are in the obligations view, so they
        are not rendered twice (planning context, 2026-10-28)."""
        ids = ", ".join(f"{r['fid']} (by {r['author']} on {r['task']})" for r in reviews)
        return (f"Open review findings, by id: {ids}. Their claims are under 'Review findings open by id' "
                "below. RESOLVES may name them on the same one line as F<n> ids, each once. A review finding "
                "closes only when the reviewer who raised it rechecks it RESOLVED after the resolving task "
                "closes clean with its checks passing on the current source; the resolving task's lead cannot "
                "clear it, no other reviewer judges it, a finding whose reviewer is unavailable cannot be named, "
                "and the harness never marks its requirement met. A task that CONTINUES the task holding a "
                "finding inherits it, and the same recheck follows its clean close without naming it. On a "
                "survey run a RESOLVES naming a finding raised on a capped, failed or unmerged task is a "
                "recovery and is charged to the recovery allowance, once with any CONTINUES; naming only "
                "findings on closed work is ordinary coverage and is not charged. " + _MISSING_SCENARIO_RULE
                + self._owed_resolves_note(reviews))

    def _owed_resolves_note(self, reviews: List[dict]) -> str:
        """For owed review debt among ``reviews``: what it is and how it
        settles, said once (recovery lifecycle, 2026-10-31)."""
        owed = [r for r in reviews if r.get("kind") == "unreviewed"]
        if not owed:
            return ""
        ids = ", ".join(f"{r['fid']} ({r.get('owed_kind') or 'review'} of {r['task']} owed by {r['author']})"
                        for r in owed)
        return (f" Owed review debt: {ids}. Each is a required judgment that did not finish, not a defect and not "
                "an approval. Only its owner's completed judgment of that work on the current source settles it: "
                "for a review, the owner reviews the work once when a task naming its id in RESOLVES (or "
                "continuing its task) closes clean; for a design review or a verification, when a task that "
                "continues its task completes that judgment by the same owner. At most "
                f"{_OWED_SETTLEMENT_ATTEMPTS} owner attempts are made, never twice on the same source; DONE and the "
                "cap never complete over it.")

    # -- review findings: one identity for RESOLVES, inheritance and the recheck ----
    def _continuation_chain(self, task_id: Optional[str]) -> List[str]:
        """The tasks ``task_id`` continues, nearest first (t5 continues t4,
        which continued t3), read from the dispatch records."""
        chain, seen = [], set()
        links = {o.task_id: o.continues for o in self.task_outcomes}
        current = task_id or ""
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            current = links.get(current) or ""
        return chain

    def _inherited_review_findings(self, spec, continues=None) -> List[dict]:
        """The open review findings raised on the work this task continues:
        the predecessor's and, through it, every earlier link's. A
        continuation whose reviewer is another seat cannot close them
        (controller-continuation-01, t5 after t4), so they stay their own
        reviewer's and are rechecked by that reviewer when the continuation
        closes clean (``_settle_review_findings``)."""
        if continues is None:
            outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
            continues = (outcome.continues if outcome is not None else None) or getattr(self, "_continues", None)
        chain = self._continuation_chain(continues)
        if not chain:
            return []
        return [r for r in self.review_findings if not r.get("closed") and r.get("task") in chain]

    def _inherit_review_findings(self, spec, continues) -> None:
        """Mark, at dispatch, the open review findings this continuation
        inherits, so the record and the planner say who carries them."""
        for record in self._inherited_review_findings(spec, continues):
            inherited = list(record.get("inherited_by") or [])
            if spec.task_id not in inherited:
                record["inherited_by"] = inherited + [spec.task_id]

    def _inherited_findings_block(self, spec) -> str:
        """For the lead of a task that continues work or resolves review
        findings: exactly the open review findings this task is to resolve,
        by id and as written, with the reason each one's own author last
        kept it open (``_resolution_findings_block``, cycle16)."""
        return self._resolution_findings_block(spec)

    def _resolution_targets_of(self, spec) -> List[dict]:
        """The open review findings this task resolves: those its own RESOLVES
        named and those it inherits by the CONTINUES its own dispatch record
        holds (never another task's working state, and nothing for a task
        with no record); one per id, in that order. Only findings this session holds open; a parallel child
        holds none, so it is shown none (RESOLVES is not admitted in a batch)."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        if outcome is None:
            # No dispatch record for this task: nothing is its to resolve.
            return []
        reviews = {r["fid"]: r for r in self.review_findings if not r.get("closed")}
        named = [reviews[fid] for fid in (getattr(outcome, "resolves", None) or ()) if fid in reviews]
        out = list(named)
        # Its own recorded CONTINUES only, "" when it continues nothing: the
        # helper would otherwise fall back to the session's working
        # ``_continues``, which may be another task's (root review of
        # b592380). Settlement and admission keep their own reading.
        for record in self._inherited_review_findings(spec, getattr(outcome, "continues", "") or ""):
            if all(record["fid"] != r["fid"] for r in out):
                out.append(record)
        return out

    def _resolution_findings_block(self, spec) -> str:
        """The open review findings this task resolves, for its lead (cycle16;
        before, only a CONTINUES with a capped or failed predecessor showed
        them, and a RESOLVES lead saw only the planner's description). Each
        is shown as its reviewer wrote it, with that reviewer's latest
        standing reason where one stands. Nothing here is the lead's to
        close."""
        records = self._resolution_targets_of(spec)
        if not records:
            return ""
        lines = []
        for r in records:
            lines.append(f"- {r['fid']} (raised by {r['author']} on {r['task']}): {(r.get('text') or r['claim'])[:600]}")
            reason = self._standing_reason(r, limit=600)
            if reason is not None:
                lines.append(f"  {r['author']}'s latest judgment ({reason['role']}, {reason['where']}) kept it open: "
                             f"{reason['why']}")
        return ("## Open review findings this task resolves\n" + "\n".join(lines)
                + "\nThese are still open. Where one is a defect inside this task's permitted paths, address it as "
                "part of this task. Where it says declared scenarios are missing, bind each scenario this task "
                "declares for it with PROVES lines; you cannot add a scenario. A finding that opens with SCENARIO "
                "or EVIDENCE is about the plan's scenario text or about execution the harness has not shown, not "
                "about the code, and is not yours to answer. After this task closes clean, the reviewer who raised "
                "each finding rechecks it against the finished work: do not mark any of them resolved yourself, "
                "and do not report one as done.")

    def _review_resolution_targets(self, spec) -> Tuple[List[str], List[dict]]:
        """``(named, inherited)``: the open review finding ids this task's
        RESOLVES names, and the open records it inherits by CONTINUES that
        RESOLVES did not name."""
        reviews = {r["fid"]: r for r in self.review_findings if not r.get("closed")}
        named = [fid for fid in (self._current_resolves or []) if fid in reviews]
        inherited = [r for r in self._inherited_review_findings(spec) if r["fid"] not in named]
        return named, inherited

    def _resolution_reviewers(self, spec, *, continues=None, resolves=None) -> List[str]:
        """The reviewers a clean close of this task sends back to their own
        open findings (named by RESOLVES or inherited by CONTINUES): one
        reserve call each on the task's route, costed before the task's
        first call. Read without side effects."""
        reviews = {r["fid"]: r for r in self.review_findings if not r.get("closed")}
        ids = {fid for fid in (resolves if resolves is not None else self._current_resolves or []) if fid in reviews}
        chain = self._continuation_chain(continues if continues is not None else getattr(self, "_continues", None))
        ids |= {r["fid"] for r in reviews.values() if r.get("task") in chain}
        return sorted({reviews[fid]["author"] for fid in ids})

    def _settle_review_findings(self, spec, named: List[str], inherited: List[dict], reasons: List[str]) -> List[str]:
        """After a clean close: the review findings this task named in
        RESOLVES or inherited by CONTINUES go back to the reviewers who
        raised them, one recheck call per reviewer on the current source
        (``_recheck_open_findings``: RESOLVED closes exactly those findings;
        anything else keeps them open). Nothing else closes a review
        finding: not the lead's account, not another reviewer, not the
        harness; and a closed finding makes no requirement met (the audit
        judges that).

        Each target's outcome is recorded on the resolution row and on the
        finding (``resolution_attempts``): ``closed``; ``adverse``, its
        owner rechecked and judged it not resolved; ``owed``, the owner's
        recheck was capped, refused to keep the finish or gave no verdict
        (owed review debt under its own attempt bounds); ``unavailable``,
        the owner could not be asked; ``not_attempted``, a settlement reason
        (a failed gate, a finding of the task's own, a ledger change) meant
        no recheck was asked. Only ``adverse`` is the FindingsUnresolved
        stop. The rest is pending work held for the planner like any held
        review debt (cycle01, PR73 6702597850: bounded-recovery-01 stopped
        on a RESOLVES whose recheck was never asked, with five slots left).
        A repeat is caught by state, not by count: a named finding left
        without its owner's verdict again in the same decision state
        (``_decision_state``: source, evidence and bindings, blocking
        findings, reason, owner availability) is no progress, and that stops
        it; a changed state, or an unknown one, is not a repeat. Returns the
        named ids that stop."""
        reviews = {r["fid"]: r for r in self.review_findings if not r.get("closed")}
        targets = [reviews[fid] for fid in named if fid in reviews] + [r for r in inherited if r["fid"] in reviews]
        if not targets:
            return []
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        record = dict(task=spec.task_id, named=list(named), inherited=[r["fid"] for r in inherited],
                      reviewers=sorted({r["author"] for r in targets}), rechecks=None, refused=None, outcomes={})
        self.review_resolutions.append(record)
        if outcome is None:
            # No dispatch record: nothing can be settled or held against it.
            record["refused"] = "its dispatch record is missing"
            for target in targets:
                target["hold"] = f"{spec.task_id} closed without its recheck: its dispatch record is missing"
                record["outcomes"][target["fid"]] = "not_attempted"
            return list(named)
        source = self._source_fingerprint() if self.project else None
        history_before = {t["fid"]: len(t.get("history") or ()) for t in targets}
        if reasons:
            why = "; ".join(reasons)
            record["refused"] = why
            for target in targets:
                target["hold"] = f"{spec.task_id} closed without its recheck: {why}"[:300]
            self._note(f"task {spec.task_id}: {len(targets)} review finding(s) not rechecked, held as pending "
                       f"work: {why[:160]}")
            classes = {t["fid"]: "not_attempted" for t in targets}
        else:
            held = {e.get("finding_id"): e for e in self.unresolved_reviews}
            entries = []
            for target in targets:
                entry = held.get(target["fid"])
                if entry is None:
                    entry = dict(task=target["task"], peer=target["author"], finding=target.get("text")
                                 or target["claim"], text=None, finding_id=target["fid"])
                entries.append(entry)
            origins = sorted({e["task"] for e in entries})
            how = (f"Task {spec.task_id} named {'them' if len(entries) > 1 else 'it'} in RESOLVES"
                   if named and not inherited else
                   f"Task {spec.task_id} continues {', '.join(origins)}" if inherited and not named else
                   f"Task {spec.task_id} named some in RESOLVES and continues {', '.join(origins)} for the rest")
            situation = (f"You reviewed {', '.join(origins)} and these findings were left open. {how}; it has "
                         "now closed: its checks passed on the current source and it closed with no new open "
                         "finding. Your working directory holds the current source; 'the revision' below is the "
                         "source as it now stands. Nothing here says your findings are addressed: read the source.")
            record["rechecks"] = self._recheck_open_findings(spec, entries, outcome, situation=situation)
            current = {r["fid"]: r for r in self.review_findings}
            classes = {}
            for target in targets:
                now = current.get(target["fid"], target)
                written = (now.get("history") or [])[history_before[target["fid"]]:]
                last = written[-1] if written else {}
                if now.get("closed"):
                    classes[target["fid"]] = "closed"
                elif last.get("state") == "unresolved" and last.get("author") == target["author"]:
                    classes[target["fid"]] = "adverse"
                elif not self._available(target["author"]) and not written:
                    classes[target["fid"]] = "unavailable"
                else:
                    # Capped, refused to keep the finish, or no verdict: the
                    # owner's debt (``_owe_review``), under its own bounds.
                    classes[target["fid"]] = "owed"
        stop = []
        for target in targets:
            fid, cls = target["fid"], classes[target["fid"]]
            state = self._decision_state(target, cls, record["refused"] or "", source)
            # No progress is the same decision state again, never the same
            # bytes alone: a declared binding, new exact-source evidence, a
            # changed blocking finding, settlement reason or owner
            # availability is progress with the source unchanged (root
            # review of 9b0280b, R1). An unknown state proves nothing.
            repeated = cls in ("not_attempted", "unavailable") and state is not None and any(
                a.get("state") == state for a in target.get("resolution_attempts") or ())
            target.setdefault("resolution_attempts", []).append(dict(
                task=spec.task_id, outcome=cls, source=source, why=(record["refused"] or "")[:300],
                state=state, repeated=repeated))
            record["outcomes"][fid] = cls + (" (again in an unchanged decision state)" if repeated else "")
            if fid in named and (cls == "adverse" or repeated):
                stop.append(fid)
        if any(c == "closed" for c in classes.values()):
            self._discharge_pending_settlements(spec.task_id)
        return stop

    def _decision_state(self, target: dict, cls: str, why: str, source: Optional[str]) -> Optional[str]:
        """The identity of what a RESOLVES settlement decided on, for the
        no-progress check (root review of 9b0280b, R1): the source; every
        acceptance row's scenario, binding, bound cases, execution and
        judgment, read as a set without task, row or receipt ids (so a later
        task restating the same evidence adds nothing, and a new receipt of
        the same result is not progress); the open blocking findings by
        claim, without ids; the settlement's reason and outcome; and whether
        the owner can be asked. None when the source is unknown: an unknown
        state never proves no progress."""
        if not source:
            return None
        rows = sorted({json.dumps([sorted(r.get("requirements") or []) if isinstance(r.get("requirements"), list)
                                   else str(r.get("requirements")),
                                   {k: v for k, v in dict(r.get("scenario") or ()).items() if k != "proof_id"},
                                   r.get("modality"), r.get("binding"), sorted(r.get("cases") or []),
                                   r.get("execution"), r.get("sufficiency")], sort_keys=True, default=str)
                       for r in self.evidence_obligations if r.get("kind") == "acceptance"
                       and not r.get("superseded_by")})
        blocking = sorted({re.sub(r"\bRFT\d+-\d+\b|\bF\d+\b", "", str(r.get("claim") or r.get("text") or ""))
                           for r in self.review_findings if not r.get("closed")}
                          | {str(f.get("text") or f.get("claim") or "") for f in self.findings
                             if f.get("status") == "open"})
        reason = re.sub(r"\bt\d+\b", "", why)
        payload = json.dumps(dict(source=source, rows=rows, blocking=blocking, reason=reason, outcome=cls,
                                  owner=target.get("author"), available=bool(self._available(target.get("author")))),
                             sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _resolution_pending(self, task_id: str) -> List[str]:
        """The review findings ``task_id`` named in RESOLVES that its
        settlement left open without its owner's adverse verdict: held
        pending work (cycle01)."""
        row = next((r for r in reversed(self.review_resolutions) if r.get("task") == task_id), None)
        if row is None:
            return []
        closed = {r["fid"] for r in self.review_findings if r.get("closed")}
        return [fid for fid in row.get("named") or () if fid not in closed
                and not str((row.get("outcomes") or {}).get(fid, "")).startswith(("closed", "adverse"))
                and "unchanged decision state" not in str((row.get("outcomes") or {}).get(fid, ""))]

    def _discharge_pending_settlements(self, task_id: Optional[str]) -> None:
        """An earlier task's settlement edge left pending (its named review
        findings held without their owner's verdict) is satisfied by fact
        once every finding it named is closed by its owner's later recheck:
        the resolution the earlier task attempted is then established, and
        the row says which task's settlement established it."""
        closed = {r["fid"] for r in self.review_findings if r.get("closed")}
        for row in self.review_resolutions:
            if row.get("task") == task_id or row.get("settled_later_by") or not row.get("named"):
                continue
            outcomes = row.get("outcomes") or {}
            if not any(not str(outcomes.get(fid, "")).startswith("closed") for fid in row["named"]):
                continue
            if not all(fid in closed for fid in row["named"]):
                continue
            mine = next((o for o in reversed(self.task_outcomes) if o.task_id == row.get("task")), None)
            if mine is None or mine.edges.get("settlement") is not False:
                continue
            mine.edge("settlement", True)
            row["settled_later_by"] = task_id or "a later owner settlement"
            self._note(f"task {row.get('task')}: its RESOLVES {', '.join(row['named'])} settled by the owners' "
                       f"later judgment ({row['settled_later_by']})")

    def _review_debt_hold(self) -> str:
        """Why every open finding is held review debt the evidence loop can
        test, or "" (close/1). Read only. The rule the serial close applied
        and the batch close did not (close family, 2026-10-26;
        recovery-handoff-01 stopped FindingsOpen after a batch on a finding
        t3 had left held): the requirements ledger is on, the orchestrator
        plans (no explicit list), a configured check can run evidence, and
        every open finding is a held review finding, in both directions. The
        typed record must say the same as the legacy list: a legacy finding
        with no active typed finding behind it, or a typed record with
        problems, is a lost record and fails closed (root's probe, 2026-10-26:
        an empty typed set passed a vacuous all()). The closing task's COVERS
        are not required: inherited debt is still debt. Whether a slot is
        left is the close policy's question, not this rule's."""
        if not self.open_findings:
            return ""
        # Owed review debt (an unfinished review or recheck, held for its
        # owner) settles by the owner's completed judgment, not by the
        # evidence loop: it is held without a discovering check, and in an
        # operator list too, whose end makes the one owner attempt
        # (``_settle_owed_reviews``). Nothing completes over it (recovery
        # lifecycle, 2026-10-31).
        owed = self._owed_texts()
        owed_only = bool(owed) and all(text in owed for text in self.open_findings)
        if owed_only:
            if not (self.config.requirements_ledger or self._explicit_tasks is not None):
                return ""
        elif not (self.config.requirements_ledger and self._explicit_tasks is None and self._discovering_checks()):
            return ""
        from .finding_state import from_session
        held = {e["entry"]["text"] for e in self._disputable().values() if e["kind"] == "review"} | owed
        state = from_session(self)
        typed = list(state.findings())
        if state.problems or not typed:
            return ""
        if not all(text in held for text in self.open_findings):
            return ""
        if not all(any(item.whole == text or text.startswith(item.whole) for text in held) for item in typed):
            return ""
        if not all(any(item.whole == text or text.startswith(item.whole) for item in typed)
                   for text in self.open_findings):
            return ""
        if owed_only:
            return (f"{len(self.open_findings)} open finding(s) are owed review debt, settled only by their owners' "
                    "completed judgment")
        return f"{len(self.open_findings)} open finding(s) are held review findings the evidence loop can test"

    def _review_debt_holds(self, covers, *, slot_left: bool) -> bool:
        """Compatibility reading of ``_review_debt_hold``: the rule applies
        and a slot is left. ``covers`` is no longer required."""
        return bool(slot_left and self._review_debt_hold())

    def _close_policy(self, closed, *, slot_left: bool, debt_recorded: bool, unresolved=()) -> dict:
        """The one close decision, serial task or merged batch (close/1), as
        a record and without a side effect: ``stop`` names the stop or is
        None. A failing last check with no recorded debt stops CheckFailing.
        Open findings stop FindingsOpen unless every one is held review debt
        (``_review_debt_hold``): with a slot left the orchestrator may test
        or repair them; with none left they go to the cap's completion
        decision, which asks the goal question and the audit as the finish
        reservation held capacity for and never completes over them. A
        RESOLVES whose owner rechecked and did not accept it, or that was
        left again without its owner's verdict in an unchanged decision state
        (``_decision_state``), stops FindingsUnresolved; a recheck that was never asked is pending work
        held like the rest (cycle01, PR73 6702597850).

        Where it applies (cycle15, corrected to the code): a capped or failed
        serial task never reaches it, since the serial loop hands that task
        back to the planner under the turn-limit breaker. A parallel batch
        always reaches it after its merge, whatever its children did: capped
        children are merged and owed, their findings counted here like any
        other, and a child that was not merged (a raised call, a refusal, a
        permission block) leaves an open finding no review debt holds, so the
        batch stops FindingsOpen."""
        record = dict(closed=sorted(closed), slot_left=bool(slot_left), debt_recorded=bool(debt_recorded),
                      stop=None, holds=[], reasons=[])
        check_failed = bool(self.checks) and not self.checks[-1].get("passed")
        # Findings first, then the check, as the serial close always read
        # them (the close-out's prediction names the same reason).
        if self._findings_stop_due():
            hold = self._review_debt_hold()
            if hold:
                record["holds"].append(hold)
                record["reasons"].append("held review debt: the orchestrator may test or repair it" if slot_left
                                         else "held review debt with no slot left: it goes to the cap's completion "
                                              "decision (goal question, audit), which never completes over it")
            else:
                record.update(stop="FindingsOpen")
                record["reasons"].append("open findings nothing holds: " + "; ".join(self._open_work())[:400])
        if record["stop"] is None and check_failed and not debt_recorded:
            record.update(stop="CheckFailing")
            record["reasons"].append("the last check failed and no check or merge debt was recorded")
        if record["stop"] is None and unresolved:
            record.update(stop="FindingsUnresolved")
            record["reasons"].append(f"RESOLVES {', '.join(unresolved)} did not establish acceptance (owner's "
                                     "verdict, or no progress in an unchanged decision state)")
        return record

    def _close_decision(self, site: str, closed, *, slot_left: bool, debt_recorded: bool, unresolved=()) -> Optional[str]:
        """``_close_policy`` applied and recorded for ``site`` (``task``,
        ``batch``); returns the stop name or None."""
        record = dict(site=site, **self._close_policy(closed, slot_left=slot_left, debt_recorded=debt_recorded,
                                                      unresolved=unresolved))
        self.close_decisions.append(record)
        if record["holds"]:
            self._note("held review findings are debt: " + record["reasons"][-1])
        return record["stop"]

    def _stop_follows_close(self) -> str:
        """Why the serial loop is certain to stop right after this task
        closes, or "" (R24 on 54d729: t1 closed with open review findings, a
        model close-out was written, and the run stopped FindingsOpen before
        any planning call could read it). Read only, from the record the
        close policy reads (``_close_policy``): nothing between this
        close-out and that decision removes a legacy open finding (check
        debt only adds one, settlement only resolves design and check
        findings). Anything less certain keeps the model close-out. Only a
        task the serial loop dispatched is asked: a task run any other way
        has no such decision after it, and a parallel child's batch decides."""
        if self._batch_child or not self._serial_dispatch:
            return ""
        covers = list(getattr(self, "_current_covers", []) or [])
        slot_left = not getattr(self, "_last_wave", False)
        debt_possible = bool(self.config.requirements_ledger and covers)
        policy = self._close_policy({getattr(self._active_spec, "task_id", "")}, slot_left=slot_left,
                                    debt_recorded=debt_possible)
        if policy["stop"] == "FindingsOpen":
            return "the run stops on its open findings right after this task: no planning call reads it"
        if policy["stop"] == "CheckFailing":
            return "the run stops on its failed check right after this task: no planning call reads it"
        return ""

    def _record_merge_debt(self, merge, parsed) -> Optional[str]:
        """Merged parallel work that fails the project check after the merge
        fix round is an integration conflict between the tasks, recorded as
        the same check finding a serial task leaves (step 2): a scoped task
        naming RESOLVES repairs it, instead of the run ending. Needs the
        requirements ledger and the tasks' COVERS to hold the debt; otherwise
        the CheckFailing stop stands. Returns the finding id or None."""
        covers = sorted({rid for _spec, task_covers, *_ in parsed for rid in task_covers})
        if not (self.config.requirements_ledger and covers and self.checks) or self.checks[-1].get("passed"):
            return None
        last = self.checks[-1]
        fid = f"F{len(self.findings) + 1}"
        failed_checks = _check_ids(last.get("receipts") or [], passed=False)
        tasks = ", ".join(spec.task_id for spec, *_ in parsed)
        message = (f"the merged parallel work of {tasks} failed the project check after the merge fix "
                   f"round (failed: {', '.join(failed_checks)}); repair it within {merge.task_id}'s files")
        self.findings.append(dict(
            id=fid, task=merge.task_id, requirements=covers, kind="check.failed",
            target=None, steps=[], view=None, width=None, viewport=None, message=message[:300],
            check=dict(attempt=last.get("attempt"), gate=last.get("gate"), failed_checks=failed_checks,
                       output_artifact=last.get("output_artifact"), attribution=last.get("attribution")),
            evidence=None, status="open"))
        for rid in covers:
            self.memory.ledger.requirement_status[rid] = f"NOT MET: open finding {', '.join(self._open_findings_for(rid))}"
        # The finding now holds the merge gate's failure, as the scope
        # finding holds a serial breach: it blocks completion until a task
        # resolves it, and the run-level fact is history from here.
        for fact in self.run_outcome.facts:
            if fact.kind == "product" and fact.active and "merge gate" in fact.detail:
                fact.recovered = True
        self._note(f"{merge.task_id}: the merged work failed its check; recorded as finding {fid}")
        return fid

    def _open_work(self) -> List[str]:
        """What still blocks completion, as facts from the record; each is
        one reason. Only blocking debt: a failing check, a failing merge
        gate, owed capped or failed work, open audit or review findings.
        Read by the verification handoff (a blocker there dispatches the
        task instead of handing its requirements to the audit), by the
        close policy's stop reason and by the terminal stop. A requirement
        no task has covered is not debt of this kind: it is reported at a
        stop by ``_uncovered_requirements`` (root's full suite on 7650cb0:
        the report line, added here, turned a verification-only task over
        an uncovered requirement into a writer dispatch)."""
        reasons = []
        for outcome in self.task_outcomes:
            if (outcome.checks and not outcome.checks[-1]["passed"]
                    and not self._check_findings_settled(outcome.task_id)):
                last = outcome.checks[-1]
                reasons.append(f"task {outcome.task_id}'s last check still fails (attempt {last.get('attempt', '?')}, "
                               f"output artifact {last.get('output_artifact', 'unavailable')})")
        if any(f.active and f.kind == "product" for f in self.run_outcome.facts):
            reasons.append("the merge gate still fails")
        # Named from typed and legacy together, as the blocking predicate
        # decides (map P3.4, Codex 5864252244); the same text when they agree.
        partial = sorted(self._partial_from_outcomes() | set(self._partial_tasks))
        if partial:
            reasons.append(f"capped or failed task(s) {', '.join(partial)} not continued to completion")
        ledger = self._open_findings_for(None)
        if ledger:
            reasons.append(f"audit findings {', '.join(ledger)} are open")
        if self.open_findings:
            reasons.append(f"{len(self.open_findings)} open finding(s), first: {self.open_findings[0][:160]}")
        return reasons

    def _uncovered_requirements(self) -> List[str]:
        """The requirements no closed task covers and no audit found met,
        for terminal reporting. Coverage is not MET and this is not debt:
        the final audit judges them, and the verification handoff may hand
        one to it from the harness's own evidence."""
        if not (self.config.requirements_ledger and self.memory.ledger.requirements) or self._explicit_tasks is not None:
            return []
        status = self.memory.ledger.requirement_status
        return [rid for rid in self.memory.ledger.requirements
                if not str(status.get(rid, "")).startswith(("covered", "met"))]

    def _requirement_texts_now(self) -> dict:
        """The original requirement texts every judgment and correction is
        held to: the ledger's, or, in a parallel child (whose own ledger is
        off), the texts its parent handed it. One lookup for every reader
        (root's parallel scenario control on fff4e99: the child's correction
        prompt rendered an empty requirement)."""
        return self.memory.ledger.requirements or getattr(self, "_requirement_texts", {}) or {}

    def _covered_texts(self) -> List[tuple]:
        """(rid, original text) for each requirement the current task covers.
        A parallel child has no ledger of its own; its parent hands it the
        requirement text with the ids it covers."""
        texts = self._requirement_texts_now()
        return [(rid, texts[rid]) for rid in getattr(self, "_current_covers", []) or [] if rid in texts]

    def _record_review_notes(self, spec, peer: str, note: str, rids: List[str]) -> None:
        """Keep each paragraph of a task review that names a covered
        requirement without blocking on it, with the source it was written
        about (R19 on e2a46a4: t3's reviewer warned that malformed IDs broke
        R12's JSON refusal, did not block, and the final auditor's prompt
        never saw it). BLOCKING findings and verdict lines keep their own
        routes; nothing here is a verdict or changes one."""
        source = self._source_fingerprint() if self.project else None
        for block in re.split(r"\n\s*\n", note or ""):
            lines = []
            for line in block.splitlines():
                if _BLOCKING_LINE.match(line.replace("**", "")):
                    # The finding and every line after it in its paragraph
                    # belong to the blocking and recheck route (root review of
                    # 728c0fc: a continuation line was carried as an unchecked
                    # observation).
                    break
                # Only a line the strict reader took as a verdict is left
                # out: a refused one ("R9: MET, but only partly: ...") is
                # the reviewer's caveat and is kept for the audit.
                if line.strip() and (_requirement_line(line) or (None, None))[1] is None and (
                        line.strip() not in ("NO FINDINGS", _REVIEW_COMPLETE)):
                    lines.append(line.strip())
            text = " ".join(lines)
            if not text:
                continue
            ids = {r.upper() for r in re.findall(r"\bR\d+\b", text)}
            named = sorted(ids & set(rids))
            association = "named"
            if not named:
                if ids:
                    # Names only requirements this task does not cover.
                    continue
                # _FINDING_SHAPE lets a finding state its condition without
                # an id (root review of 225367f): kept against the task's
                # covered requirements, labelled as unassigned.
                named, association = sorted(rids), "unassigned"
            # Whole: a slice kept an innocent prefix and dropped the warning
            # after it (root review of 728c0fc). Review volume is already
            # bounded by the job's calls and tokens.
            self.review_notes.append(dict(task=spec.task_id, peer=peer, requirements=named,
                                          association=association, text=text, source=source))

    def _review_notes_grouped(self, rows: List[dict], current) -> str:
        """The same observations, each distinct text once (cycle13): texts
        are grouped only when they are identical character for character, so
        whitespace a note quotes stays material; every task, reviewer,
        requirement association and source label that raised it is listed
        with it, each as recorded, never merged into one reading; the shared
        explanation is said once in the header. Nothing is dropped or cut."""
        groups: Dict[str, List[dict]] = {}
        for n in rows:
            groups.setdefault(n["text"], []).append(n)

        def raised(n: dict) -> str:
            about = (f"about {', '.join(n['requirements'])}" if n.get("association", "named") == "named" else
                     f"naming no requirement (the task covered {', '.join(n['requirements'])}; which one, if any, "
                     "it concerns is uncertain)")
            where = ("the current source" if n.get("source") and n["source"] == current
                     else "an earlier source (it may no longer hold)")
            return f"{n['task']} review by {n.get('peer') or 'unknown'}, {about}, written about {where}"
        lines = [f"- raised by {'; '.join(raised(n) for n in group)}: {text}" for text, group in groups.items()]
        return ("\n\n## Earlier task-review observations on these requirements (harness record, not verdicts)\n"
                "Each line is one observation's exact text, after every review that raised it, with what that review "
                "said it was about and the source it was written about. None was marked blocking and none was "
                "rechecked.\n"
                + "\n".join(lines)
                + "\nA reviewer raised each of these without blocking on it. Reconcile each against the goal "
                "and the current source yourself: an observation is neither a finding nor an approval, and "
                "the reviewer's reason for not blocking binds nothing.")

    def _review_notes_block(self, audited: dict) -> str:
        """The carried review observations on the requirements judged, each
        labelled with whether it was written about the current source."""
        current = self._source_fingerprint() if self.project else None
        # Every one: a count cap dropped the oldest warning behind newer
        # benign notes (root review of 728c0fc).
        rows = [n for n in self.review_notes if set(n["requirements"]) & set(audited)]
        if not rows:
            return ""
        if getattr(self, "_compact_audit_packet", True):
            return self._review_notes_grouped(rows, current)
        lines = [f"- {n['task']} review, "
                 + (f"about {', '.join(n['requirements'])}, " if n.get("association", "named") == "named" else
                    f"naming no requirement (the task covered {', '.join(n['requirements'])}; which one, if "
                    "any, it concerns is uncertain), ")
                 + ("written about the current source" if n.get("source") and n["source"] == current
                    else "written about an earlier source (it may no longer hold)")
                 + f", not marked blocking and never rechecked: {n['text']}" for n in rows]
        return ("\n\n## Earlier task-review observations on these requirements (harness record, not verdicts)\n"
                + "\n".join(lines)
                + "\nA reviewer raised each of these without blocking on it. Reconcile each against the goal "
                "and the current source yourself: an observation is neither a finding nor an approval, and "
                "the reviewer's reason for not blocking binds nothing.")

    def _judge_goal_review(self, spec, peer: str, note: str, *, record: Optional[dict] = None) -> str:
        """Hold a goal-bearing review to its per-requirement verdicts (r05 on
        1655cf1: the CLI reviewer carried the goal and R10 verbatim and still
        replied NO FINDINGS while malformed IDs left through argparse's plain
        exit). A covered requirement with no MET or NOT MET line makes the
        review unfinished; a NOT MET line is a BLOCKING finding even when the
        reviewer did not write one. Returns the note the lead answers."""
        rids = [rid for rid, _ in self._covered_texts()]
        # Whether this call held the review as unfinished, for the caller's
        # review edge and reviewer record.
        self._goal_review_owed = False
        if not rids:
            return note
        self._record_review_notes(spec, peer, note, rids)
        verdicts = _requirement_verdicts(note)
        missing = [rid for rid in rids if rid not in verdicts]
        # Only an approval needs every verdict: a review that already raises a
        # BLOCKING finding sends the work back whatever it left unsaid.
        if missing and not _has_blocking_finding(note) and not any(v is False for v in verdicts.values()):
            # An unfinished review, held as owned review debt for its seat
            # (recovery lifecycle, 2026-10-31: the bare unverified string
            # this used to open had no settlement route).
            self._goal_review_owed = True
            judged = _review_judgments(note)
            duplicated = [rid for rid in missing if rid in judged and judged[rid].lines > 1]
            refused = [rid for rid in missing if rid in judged and judged[rid].lines == 1]
            self._owe_review(spec, peer, "review", f"gave no MET or NOT MET line for {', '.join(missing)}"
                             + (f"; duplicate MET lines for {', '.join(duplicated)} are no verdict"
                                if duplicated else "")
                             + (f"; the MET line for {', '.join(refused)} is not 'R<n>: MET - <citation>' "
                                "(a condition or other text after MET is no verdict)" if refused else ""),
                             role="collaborator", record=record)
            self._note(f"task {spec.task_id}: review by {peer} left {', '.join(missing)} without a verdict")
        unmet = [rid for rid in rids if verdicts.get(rid) is False]
        if unmet and not _has_blocking_finding(note):
            note += "\n" + "\n".join(f"BLOCKING: {rid} is NOT MET (the reviewer's own verdict line)."
                                      for rid in unmet)
        return self._hold_unexercised_errors(spec, peer, note, verdicts)

    def _hold_unexercised_errors(self, spec, peer: str, note: str, verdicts: dict) -> str:
        """A review's MET on a covered error or refusal requirement counts only
        on executed evidence: this review's own scratch demonstration, run by
        the harness and passed, or a cited test case a complete approved check
        executed and passed on this source (Davis, 2026-10-16). Otherwise the
        MET line is replaced by an UNVERIFIED line, so nothing downstream reads
        it as an approval (the lead answers it like any note), and the gap is
        kept as a non-terminal fact and an observation for the final audit,
        which applies the same rule to acceptance."""
        texts = dict(self._covered_texts())
        proof = getattr(self, "_last_scratch_proof", None) or {}
        # This review's own demonstration, case by case, only if it passed in
        # full: an unrelated passing scratch shows nothing about a requirement
        # whose MET line does not cite its cases (root review of 17a96d3).
        demonstrated = (_proof_cases(proof) if proof.get("status") == "passed" and not self._handed_drift(proof)
                        else set())
        held = []
        for rid, rest in _met_lines(note).items():
            if verdicts.get(rid) is not True or rid not in texts or not _behavioural_error(texts[rid]):
                continue
            cited = _cited_cases(rest)
            if set(cited) & demonstrated or self._executed_cases(cited):
                continue
            held.append(rid)
        if not held:
            return note
        # A review that finished keeps finishing: holding one line is not
        # turning the reply into an unfinished review.
        finished = _review_complete(note) or _clean_goal_review(note, list(texts))
        bounded = {rid for rid, _ in boundary_classes([(rid, texts[rid]) for rid in held])}
        lines = []
        for line in note.splitlines():
            read = _requirement_line(line)
            m = re.match(r"(R\d+)", read[0]) if read and read[1] is True and read[0] in held else None
            if m:
                line = (f"{m.group(1).upper()}: UNVERIFIED - MET claimed without executed negative-path "
                        "evidence (harness: its MET line cites no exact test case shown executed and passed, by "
                        "this review's scratch demonstration or a recorded check on this source); an untested "
                        "risk, no counterexample observed; add a test that exercises the rejecting path"
                        + (" at each listed input boundary that applies to it" if m.group(1).upper() in bounded
                           else ""))
            lines.append(line)
        source = self._source_fingerprint() if self.project else None
        for rid in held:
            text = (f"{peer} marked {rid} MET without executed negative-path evidence: its MET line cites no "
                    "exact test case shown executed and passed on this source; not counted. An untested risk, "
                    "no counterexample observed: a test case through an approved check, or this reviewer's "
                    "scratch demonstration, is the route")
            self.review_notes.append(dict(task=spec.task_id, peer=peer, requirements=[rid], association="named",
                                          text=text, source=source, risk="untested"))
            if self._outcome is not None:
                self._outcome.note("unverified", f"{rid}: {text}", terminal=False)
        self._note(f"task {spec.task_id}: {peer}'s MET on {', '.join(held)} not counted: no executed "
                   "negative-path evidence")
        held_note = "\n".join(lines)
        if finished and not _review_complete(held_note):
            held_note += "\n" + _REVIEW_COMPLETE
        return held_note

    def _sibling_note(self) -> str:
        """For a parallel child's reviewer: the sibling tasks whose changes are
        not in this copy (r09 on ded0095: t3's reviewer blocked twice on HTTP
        routes that the still-running sibling t2 owned, and the finding stayed
        open after the merge though the merged source held the routes)."""
        siblings = getattr(self, "_batch_siblings", None) or []
        if not siblings:
            return ""
        rows = "\n".join(f"- {tid}: may change only {', '.join(paths) or 'nothing'}; intended: {intended}"
                         for tid, paths, intended in siblings)
        return ("\n\nThis task runs in parallel with the tasks below. Their changes are not in your copy "
                "and are merged after all of them finish:\n" + rows + "\nA condition that depends on work "
                "inside a sibling's paths is not a finding against this task: write it on its own line as "
                "'DEFERRED: <task id> - <condition>'. It stays open and unverified until it is rechecked "
                "against the merged source; it is never an approval. Judge everything inside this task's own "
                "paths as usual.")

    # -- review findings: stable ids, per-id dispositions (blueprint section 4) --
    def _requirement_revisions(self) -> Dict[str, int]:
        """Each requirement's current revision: 1 when its text was first
        seen, one more each time the text changes. Recorded with the text
        digest it was taken at (carried by the checkpoint), so an evidence
        obligation or a judgment bound to a revision can tell when the
        requirement it was about is no longer the one on the ledger."""
        texts = self._requirement_texts_now()
        record = self._requirement_revision_record
        for rid, text in texts.items():
            digest = hashlib.sha256((text or "").strip().encode()).hexdigest()
            held = record.get(rid)
            if held is None:
                record[rid] = [1, digest]
            elif held[1] != digest:
                record[rid] = [int(held[0]) + 1, digest]
        return {rid: int(v[0]) for rid, v in record.items() if rid in texts}

    def _environment_id(self) -> str:
        """The environment an execution is bound to: the interpreter, the
        platform, the project root. Never shown to a model."""
        import platform
        import sys
        body = dict(python=sys.executable, platform=platform.platform(),
                    root=str(Path(self.project).resolve()) if self.project else "")
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:24]

    def _finding_prefix(self, spec) -> str:
        task_id = str(getattr(spec, "task_id", "") or "run")
        return "RF" + re.sub(r"[^A-Z0-9]", "", task_id.upper()) + "-"

    def _register_review_findings(self, spec, peer: str, note: str, *, role: str = "collaborator") -> List[dict]:
        """One finding per BLOCKING (or UNRESOLVED) line a reviewer wrote,
        with a stable id, the basis and impact the line carried, the source
        it was raised on and the requirement it covers when there is exactly
        one. Lines already registered for this task and seat (same text) are
        not registered twice."""
        prefix = self._finding_prefix(spec)
        source = self._source_fingerprint() or "unavailable"
        revisions = self._requirement_revisions()
        covers = list(getattr(self, "_current_covers", []) or [])
        requirement = (covers[0], revisions.get(covers[0], 1)) if len(covers) == 1 else None
        known = {(r["task"], r["author"], r.get("text")) for r in self.review_findings}
        number = len([r for r in self.review_findings if r["fid"].startswith(prefix)])
        added = []
        for raw in (note or "").splitlines():
            # A line the review's reader (verdicts.py) counts as a finding is
            # registered here too: one under a heading mark or backticks buys
            # a revision there, and with no id it was never re-checked.
            match = _BLOCKING_LINE.match(raw.replace("**", "")) or _BLOCKING_LINE.match(_unstyled(raw))
            if not match or _is_empty_note(match.group(1)):
                continue
            text = raw.strip()
            if (spec.task_id, peer, text) in known:
                continue
            number += 1
            try:
                finding = delivery.finding_from_line(f"{prefix}{number}", match.group(1), author=peer, role=role,
                                                     source_digest=source, task=spec.task_id,
                                                     requirement=requirement)
            except ValueError:
                number -= 1
                continue
            record = dict(finding.to_dict(), text=text)
            self.review_findings.append(record)
            added.append(record)
        return added

    def _finding_records(self, *, task=None, peer=None, open_only=True) -> List[dict]:
        out = []
        for record in self.review_findings:
            if task is not None and record["task"] != task:
                continue
            if peer is not None and record["author"] != peer:
                continue
            if open_only and record.get("closed"):
                continue
            out.append(record)
        return out

    def _standing_reason(self, record: dict, *, limit: int) -> Optional[dict]:
        """The reason a review finding's own author last gave for keeping it
        open, or None: the one reading the owner's recheck, the planner and
        the resolving lead all use (cycle16, PR73 6737404225). Only the
        latest judged entry is read; an older reason is never resurrected
        past a newer RESOLVED, another seat's entry or any other state. It is
        kept as written, bounded to ``limit`` characters, with its role and
        whether it was given on this source, an earlier one or an unrecorded
        one; it is that author's question as it stood, never a verdict."""
        latest = next((h for h in reversed(record.get("history") or ()) if h.get("state") != "unjudged"), None)
        if not (latest and latest.get("state") == delivery.UNRESOLVED and latest.get("author") == record.get("author")
                and str(latest.get("why") or "").strip()):
            return None
        current = self._source_fingerprint() if self.project else None
        digest = str(latest.get("source_digest") or "")
        where = ("on this same source" if current and digest == current else
                 "on an earlier source" if digest else "on an unrecorded source")
        return dict(author=record.get("author"), role=latest.get("role"), where=where,
                    why=str(latest["why"]).strip()[:limit])

    def _finding_lines(self, records: List[dict]) -> str:
        """Each finding by id, in the reviewer's own words (the whole line as
        written, basis and impact segments included), and, where its author's
        latest judgment kept it open with a reason, that reason as written:
        the question still standing (cycle10 run 20261108T163004Z: RFT4-14's
        penultimate recheck said the CLI test did not isolate transitive
        conflicts and asked for indirect-only cases; the post-merge recheck
        was shown only the original claim about missing scenarios and closed
        it on a generic citation). Which answer suffices stays the
        reviewer's; the harness only keeps the question in front of it."""
        lines, standing = [], False
        for r in records:
            lines.append(f"- {r['fid']}: {r.get('text') or r['claim']}")
            reason = self._standing_reason(r, limit=1200)
            if reason is not None:
                lines.append(f"  your latest judgment ({reason['role']}, {reason['where']}) kept it open: "
                             f"{reason['why']}")
                standing = True
        if standing:
            lines.append("Where your latest judgment kept a finding open with a reason, that reason is the question "
                         "still standing: a RESOLVED line answers it, naming the executed case or file that now shows "
                         "what you said was missing; otherwise the finding stays UNRESOLVED.")
        return "\n".join(lines)

    def _apply_review_reading(self, spec, peer: str, role: str, reading, binding_before: dict, *,
                              call: str = "") -> List[dict]:
        """Record a judge's per-id reading on the registry (delivery.apply_reading):
        every expected finding gets one disposition, bound to the identity
        the judge was handed and refused if it drifted; new findings are
        registered with their basis and impact. Returns the new records."""
        before = {r["fid"]: r for r in self.review_findings}
        findings = tuple(delivery.Finding.from_dict(r) for r in self.review_findings)
        now = self._judgment_binding(spec, peer, role)
        after = delivery.apply_reading(findings, reading, author=peer, role=role, binding=now["identity"],
                                       judged_binding=binding_before["identity"],
                                       source_digest=self._source_fingerprint() or "unavailable", call=call,
                                       prefix=self._finding_prefix(spec), task=getattr(spec, "task_id", "run"),
                                       resolve=self._evidence_resolves)
        # The typed fields come from the judged record; the session's own
        # marks on a finding (its text as written, its route, a hold for
        # the post-merge recheck) survive the rebuild (workflow repair,
        # 2026-10-24: a rebuild dropped them).
        rebuilt = []
        for f in after:
            prior = before.get(f.fid, {})
            row = dict(prior, **f.to_dict())
            row["text"] = prior.get("text", f.claim)
            rebuilt.append(row)
        self.review_findings = rebuilt
        # Closing is atomic across every representation (completion
        # workflow, 2026-10-30): a finding its designated judge closed here
        # closes its held entries, its legacy texts and its facts in the
        # same step, wherever the recheck ran.
        for fid, record in before.items():
            now = next((r for r in self.review_findings if r["fid"] == fid), None)
            if now is not None and now.get("closed") and not record.get("closed"):
                self._close_review_finding_by_id(fid, f"{now['state']} by {peer} on {role}")
        new = [r for r in self.review_findings if r["fid"] not in before]
        for record in new:
            record["text"] = f"BLOCKING: {record['claim']}"
            self._note(f"{record['task']}: {peer} raised a new finding {record['fid']} on {role}: "
                       f"{record['claim'][:120]}")
        return new

    def _finding_contract(self, records: List[dict]) -> str:
        """The original contract the named findings are held to, for a
        recheck (root's correction on 0c5c6f1, Q1; addenda 6537726867 and
        6538203622: both recheck routes carried a generated task and a bare
        requirement id, never the goal, the requirement's text or the order
        of authority). Compact and bound to the findings themselves, never
        to the current task's COVERS: the verbatim goal, the operator's
        rulings, the original text of each requirement a named finding was
        raised under (its own requirement, its basis, else the covers of the
        task that raised it), the order of authority and the contract
        boundary rule. It binds what the findings are judged against and
        adds no finding duty, no verdict line and no fresh review."""
        texts = self._requirement_texts_now()
        bound: Dict[str, List[str]] = {}
        by_task = {o.task_id: list(o.covers or ()) for o in self.task_outcomes}
        for record in records:
            found: List[str] = []
            requirement = record.get("requirement")
            if requirement:
                found.append(str(requirement[0]))
            basis = record.get("basis") or {}
            if basis.get("kind") == "requirement" and basis.get("ref"):
                found.append(str(basis["ref"]).split("@", 1)[0].upper().replace("R-", "R"))
            # A basis that names no requirement on the ledger binds nothing;
            # the raising task's own covers then stand (workflow refutation
            # of this batch: a multi-cover task's finding with an unknown
            # basis id had no requirement block at all).
            found = [rid for rid in found if rid in texts]
            if not found:
                found = [rid for rid in by_task.get(str(record.get("task") or ""), []) if rid in texts]
            label = str(record.get("finding_id") or record.get("fid") or record.get("id") or "").strip()
            origin = str(record.get("task") or "").strip()
            who = (label + (f", raised on {origin}" if origin else "")) if label else origin
            for rid in found:
                holders = bound.setdefault(rid, [])
                if who and who not in holders:
                    holders.append(who)
        rulings = list(getattr(self.memory.ledger, "rulings", None) or [])
        return ("## Original goal (verbatim; it outranks the task text where they differ)\n\n"
                + self.memory.goal.strip()
                + ("\n\n## Original requirements the findings below were raised under (verbatim)\n\n"
                   + "\n".join(f"{rid} ({'; '.join(bound[rid])}): {texts[rid]}" if bound[rid] else f"{rid}: {texts[rid]}"
                               for rid in bound) if bound else "")
                + ("\n\n## Operator rulings (asked and answered; do not re-ask)\n\n"
                   + "\n".join(f"- {r}" for r in rulings) if rulings else "")
                + "\n\n" + _AUTHORITY_ORDER + " " + _CONTRACT_BOUNDARY_RULE
                + " Hold each named finding to this contract where it bears on it; it adds no finding, asks "
                  "no verdict on a requirement and reopens no review.")

    @staticmethod
    def _recheck_reading(reply: str, ids: List[str]):
        """A recheck reply read one finding at a time (``judgment.py``). One
        finding may still be answered the old way, with a bare verdict and no
        id line. That whole-reply verdict is read by ``verdicts.py``, the one
        reader of it: a plain acceptance under formatting ("RESOLVED.")
        resolves, and the last word of a wrapped sentence ("... is not" /
        "resolved") or a RESOLVED beside a finding does not. A verdict the
        reply gives on an id line stands as ``judgment.py`` reads it."""
        reading = judgment.recheck_verdicts(reply, ids)
        if len(ids) != 1:
            return reading
        fid, text = ids[0], reply or ""
        if _resolved_verdict(text):
            if fid.upper() in text.upper():
                return reading      # the reply names the finding: its id lines decide
            said = judgment.Verdict(judgment.RESOLVED, "")
        elif reading.dispositions[fid].state == judgment.RESOLVED:
            # Asked about two findings the bare form is never read, so this
            # is what the id lines alone say of this one.
            if judgment.recheck_verdicts(text, [fid, "-"]).dispositions[fid].state != judgment.INVALID:
                return reading
            said = judgment.Verdict(judgment.INVALID, "the bare reply is not a plain RESOLVED")
        else:
            return reading
        return replace(reading, dispositions={fid: said})

    @_invocation_role("recheck")
    def _recheck_blocking(
        self,
        spec: TaskSpec,
        blocking: List[tuple],
        revision: str,
        task: TaskMemory,
        *,
        labels: Optional[dict] = None,
    ) -> List[tuple]:
        """Have each blocking reviewer re-check its own findings, by id.

        Returns (peer, text) pairs for findings still open: UNRESOLVED ones,
        ones the reply gave no valid verdict for are recorded as not
        re-checked (they buy no fix round), and NEW findings the reviewer
        raised, which open as findings of their own. Scope stays the
        narrowest that does the job: the reviewer sees its own findings and
        the revision, not the other reviewers and not a vote. Each finding
        settles on its own: resolved ones close while an unresolved or new
        one stays open (blueprint section 4).
        """
        unresolved: List[tuple] = []
        if blocking:
            self._observe_checks(spec, "recheck")
            self._refresh_obligations()
        for peer, note in blocking:
            shown = (labels or {}).get(peer, peer)
            # Owed review debt is never rechecked by id: only its owner's
            # completed review settles it (``_settle_owed``).
            mine = [r for r in self._finding_records(task=spec.task_id, peer=peer) if r.get("kind") != "unreviewed"]
            if not mine:
                mine = self._register_review_findings(spec, peer, note)
            # A finding held for the post-merge recheck (a parallel child
            # measures no execution) is not re-asked here: nothing new can
            # be shown for it until the merge gate runs.
            held = [r for r in mine if r.get("held")]
            mine = [r for r in mine if not r.get("held")]
            for record in held:
                unresolved.append((peer, f"{record['fid']}: UNRESOLVED: {record['held']}"))
                self._dispose(spec, peer, f"recheck: {record['fid']} held: {record['held'][:160]}")
            if not mine:
                continue
            ids = [r["fid"] for r in mine]
            # The rows are rendered (and refreshed) before the binding is
            # taken, so the identity the judge is handed is the one compared.
            instructions = self._recheck_instructions(ids)
            binding = self._judgment_binding(spec, peer, "recheck")
            prompt = self._recheck_prompt(spec, note, revision, peer, mine=mine, instructions=instructions)
            # The worst impact among this peer's findings is this call's
            # effort input and nothing else's: set for the call, cleared on
            # every exit (root's source observation on bcd2b52).
            self._recheck_impact = _worst_impact(mine)
            rv = None
            try:
                verdict = self._invoke_model(peer, prompt)
            except FinishReserved as exc:
                # Refused to keep the finish: no verdict, so the finding is
                # neither resolved nor confirmed and buys no fix round, as a
                # capped recheck (below). The findings stay open under their
                # own ids, held for their author (recovery lifecycle,
                # 2026-10-31: never a bare string nothing can settle).
                self._finish_refused(spec, f"recheck by {peer}", exc)
                self._dispose(spec, peer, "recheck not run: refused to keep the finish")
                standing = note.strip()
                rv = self._recovery_open(spec, role="recheck", seat=peer, kind="recheck",
                                         outcome_class="judgment_refused_finish", observe=False,
                                         question=f"{peer}'s recheck of {', '.join(ids)}")
                rv["eligible"], rv["decision"] = ["hold_owned_debt"], "hold_owned_debt: the finish is held"
                self._owe_review(spec, peer, "recheck", "ordinary calls stopped to keep the finish", role="recheck",
                                 record=rv, fids=ids, original=standing)
                task.record("user", "A blocking finding was not re-checked against the revision "
                                    "(ordinary calls were stopped to keep the finish) -- carry it into the "
                                    f"summary as an open question:\n[{shown}] {standing}")
                self._recheck_impact = None
                continue
            except TurnLimitReached as exc:
                # No verdict either way: the finding is neither resolved nor
                # confirmed. The same seat completes its recheck once with
                # what the call did carried; failing that, the reviewer's
                # original findings stay open under their own ids, held for
                # it (Codex review of 0983dac: the specific defect survives,
                # beside the cap fact).
                self._recheck_impact = None
                completed, rv = self._recover_judgment(
                    spec, peer, "recheck", role="recheck", prompt=prompt, task=task, exc=exc,
                    gaps=[f"one line per finding ({', '.join(ids)}): RESOLVED, UNRESOLVED or INVALIDATED"])
                if completed is None or not self._recheck_reading(completed, ids).judged_any:
                    if completed is not None:
                        self._settle_completion(spec, peer, "recheck", False)
                    capped = self._capped_review(spec, peer, 'recheck', exc)
                    self._dispose(spec, peer, "recheck capped at its turn limit; no verdict")
                    task.record("assistant", f"[{shown} recheck] {capped}")
                    standing = note.strip()
                    self._owe_review(spec, peer, "recheck", f"stopped at {exc.turns or 'its'} rounds with no verdict",
                                     role="recheck", record=rv, fids=ids, original=standing)
                    task.record("user", "A blocking finding was not re-checked against the revision "
                                        "(the reviewer stopped at its turn cap) -- carry it into the "
                                        f"summary as an open question:\n[{shown}] {standing}")
                    self._record_recheck(spec, peer, "capped")
                    continue
                self._settle_completion(spec, peer, "recheck", True)
                self._recovery_result(rv, "recovered: the same seat completed its recheck with a verdict")
                verdict = completed
            finally:
                self._recheck_impact = None
            task.record("assistant", f"[{shown} recheck] {verdict}")
            reading = self._recheck_reading(verdict, ids)
            if not reading.judged_any and rv is None:
                # Arrived, but no judgment on any finding: the same seat may
                # complete it once (the job's allowance), told what the
                # harness observed and never shown its own reply; a genuine
                # UNRESOLVED never comes here.
                completed, rv = self._recover_judgment(
                    spec, peer, "recheck", role="recheck", prompt=prompt, task=task, raw=verdict,
                    gaps=[f"one line per finding ({', '.join(ids)}): RESOLVED, UNRESOLVED or INVALIDATED"])
                if completed is not None:
                    verdict = completed
                    task.record("assistant", f"[{shown} recheck, completed] {verdict}")
                    reading = self._recheck_reading(verdict, ids)
                    self._settle_completion(spec, peer, "recheck", reading.judged_any)
                if reading.judged_any:
                    self._recovery_result(rv, "recovered: the same seat completed its recheck with a verdict")
            if reading.judged_any:
                self._attest_inspections(spec.task_id, peer, "recheck", verdict)
            new = self._apply_review_reading(spec, peer, "recheck", reading, binding)
            current = {r["fid"]: r for r in self.review_findings}
            for fid in ids:
                record = current[fid]
                state, why = record["state"], (record["history"][-1]["why"] if record["history"] else "")
                if state == judgment.UNRESOLVED:
                    unresolved.append((peer, f"{fid}: UNRESOLVED: {why}".strip()))
                    self._dispose(spec, peer, f"recheck: {fid} unresolved: {why[:160]}")
                    self._read_restatement(spec, record, why)
                elif record["closed"]:
                    self._dispose(spec, peer, f"recheck: {fid} {state}")
                else:
                    # No valid verdict on this id: the finding stays open under
                    # its own id, held for its author (never a bare string).
                    if rv is None:
                        rv = self._recovery_open(spec, role="recheck", seat=peer, kind="recheck",
                                                 outcome_class="recheck_unfinished",
                                                 question=f"{peer}'s verdict on {fid}",
                                                 event=self._call_event(spec.task_id, "recheck", peer))
                        rv["eligible"], rv["decision"] = ["hold_owned_debt"], "hold_owned_debt"
                    self._owe_review(spec, peer, "recheck", f"{peer} gave no valid verdict on {fid}: {why[:120]}",
                                     role="recheck", record=rv, fids=[fid])
                    self._dispose(spec, peer, f"recheck: {fid} no valid verdict")
                    task.record("user", f"Finding {fid} was not re-checked against the revision (the reviewer's "
                                        "reply carried no valid verdict for it) -- carry it into the summary "
                                        f"as an open question:\n[{shown}] {record['text']}")
            for record in new:
                unresolved.append((peer, f"{record['fid']}: NEW: {record['claim']}"))
                self._dispose(spec, peer, f"recheck: new finding {record['fid']}: {record['claim'][:160]}")
            # The reviewer record takes the seat's findings as a set: resolved
            # only when every finding it was asked about closed.
            self._record_recheck(spec, peer, "resolved" if all(current[fid]["closed"] for fid in ids)
                                 else "unresolved")
        return unresolved

    def _routing_facts(self, records: List[dict]) -> str:
        """What the harness did with a reviewer's typed claims, for its
        recheck: a claim held unroutable says why and how to restate it; a
        claim the planner answered says the disposition and the planner's
        reason, so the reviewer judges the declared text against it rather
        than re-sending the same line. Facts only; nothing here is a
        verdict."""
        lines = []
        for record in records:
            route = str(record.get("route") or "")
            if route == "unroutable":
                lines.append(f"- {record['fid']}: held, not routed and not given to the editor: {record['route_note']}. "
                             "Restate it in full as '<id>: UNRESOLVED SCENARIO P<n> - <contradiction> | proposal: "
                             "<operation|state|expect>: <text>' or '<id>: UNRESOLVED EVIDENCE P<n> - <what is not "
                             "shown>' against a scenario this task declares, or settle it RESOLVED or INVALIDATED. "
                             "It stays held until then. A code defect you want the editor to answer is its own "
                             "new finding on its own line, 'NEW: <defect> | basis: <requirement id, command or "
                             "file> | impact: <impact>', which takes the ordinary editor and recheck route; "
                             "UNRESOLVED prose on this id never does.")
            elif route.startswith("scenario:"):
                entry = next((c for c in reversed(self.scenario_corrections) if record["fid"] in c.get("findings", [])),
                             None)
                why = (entry or {}).get("why") or (entry or {}).get("answer") or ""
                typed = record.get("typed") or {}
                tid = typed.get("task")
                now = ""
                if tid and tid != getattr(self._active_spec, "task_id", None):
                    proof = {p["id"]: p for p in (getattr(self._prior_scope(tid), "proofs", None) or ())}.get(
                        typed.get("proof_id"))
                    if proof is not None:
                        now = (f" Scenario {tid}:{typed['proof_id']} of the earlier task {tid} now reads: operation: "
                               f"{proof['operation']}; state: {proof['state']}; expect: {proof['expect']}.")
                lines.append(f"- {record['fid']}: the planner's answer was: {route[len('scenario:'):].strip()}"
                             + (f" ({why[:300]})" if why else "") + "." + now + " The proof rows show the scenario as "
                             "it now stands; judge the declared text, not the code, for this finding.")
        lines += self._declaration_facts(records)
        return ("Harness routing facts for your findings:\n" + "\n".join(lines) + "\n\n") if lines else ""

    def _declaration_facts(self, records: List[dict]) -> List[str]:
        """For the recheck of a finding the harness routed to the lead's
        binding declaration: what the lead declared after the revised work
        the reviewer is shown, with its provenance, and what the harness's
        own run on this exact source then showed (cycle05 trial t6: the
        recheck saw only the revision, read P1's PROVES as absent and asked
        for its truthful NOT RUN to be deleted). Facts only: the finding is
        the reviewer's, nothing here resolves it, and a declaration on
        another source is not shown."""
        current = self._source_fingerprint() if self.project else None
        out = []
        for record in records:
            for route in self.evidence_routes:
                if (route.get("finding") != record["fid"] or not str(route.get("action", "")).startswith(
                        "binding requested") or not route.get("declared") or route.get("source") != current):
                    continue
                ran = [f"execution {e}" + (f" (receipt {', '.join(rs)})" if rs else "")
                       for e, rs in zip(route.get("execution_after") or [], route.get("receipts_after") or [], strict=False)]
                out.append(
                    f"- {record['fid']}: after the revised work below, the task's lead {route.get('author')} declared in "
                    f"a read-only declaration call on this exact source"
                    + (f" (artifact {route['declaration_artifact']})" if route.get("declaration_artifact") else "")
                    + ": " + "; ".join(f"'{line}'" for line in route["declared"])
                    + f". The harness then settled {route.get('proof_id')} against its own runs on this source: "
                    + ("; ".join(ran) or "no execution recorded")
                    + ". This declaration is the lead's current binding for that scenario; where the revised work "
                    "says otherwise about it, the declaration is the later account. A NOT RUN or failure the revised "
                    "work reports is the lead's own working copy, not the harness's approved execution shown here. "
                    "Judge whether the declared case proves the scenario; your finding stays yours to settle.")
        return out

    def _read_restatement(self, spec, record: dict, why: str) -> None:
        """A recheck's UNRESOLVED text that is a complete typed claim is the
        finding's restatement: kept on the record beside the original claim
        and read by the next routing before any editor round. A held
        unroutable claim restated in full is released for that routing; a
        claim the planner already answered keeps its disposition. Anything
        else leaves the record as it is: UNRESOLVED prose on a held id is
        never editing authority (root's consistency note, 2026-10-26); a code
        defect reaches the editor only as the reviewer's own ``NEW:`` line,
        which registers as a distinct finding with the ordinary route.
        Nothing is cleared here."""
        kind, _ = _typed_finding(why or "")
        if kind is None:
            return
        record["restated"] = (why or "").strip()
        history = list(record.get("route_history") or [])
        if record.get("route") == "scope":
            # The reviewer restated what it holds (cycle04 correction): the
            # lead's claim was about the earlier text, so the route lapses
            # and the restated claim is routed by its own type.
            history.append(dict(route="scope", note=record.get("route_note"), paths=record.get("scope_paths"),
                                lapsed="restated by its reviewer"))
            record.update(route=None, route_note="", route_history=history, scope_lapsed=True)
        if record.get("route") == "unroutable":
            history.append(dict(route=record["route"], note=record.get("route_note")))
            record["route"] = None
            record["route_note"] = ""
            record["route_history"] = history
        self._note(f"task {spec.task_id}: {record['fid']} restated at the recheck as a typed {kind} claim")

    def _capped_review(self, spec: TaskSpec, peer: str, what: str, exc: TurnLimitReached) -> str:
        """A review seat that hit its turn cap gave no verdict.

        Series rule-2ffa7f6 f3 and f5: uncapped collaborators spent 15 to 20
        rounds and up to 1.18M tokens. Capping them (runtime.REVIEW_CAPPED_ROLES)
        means a review can now stop short; its narration is never read as
        findings, and the task is recorded as not fully reviewed rather than
        as reviewed clean or as blocked on a finding nobody made.
        """
        rounds = f"{exc.turns} rounds" if exc.turns else "its turn cap"
        text = f"UNFINISHED: the {what} stopped at {rounds} before giving a verdict."
        # No bare open string (recovery lifecycle, 2026-10-31: one such string
        # stopped a live run FindingsOpen after two of eight slots, since
        # nothing could hold, recheck or settle it). The caller recovers the
        # judgment or holds it as owned debt (``_recover_judgment``,
        # ``_owe_review``).
        self._note(f"task {spec.task_id}: {what} by {peer} capped at {rounds}; no verdict")
        return text

    @staticmethod
    def _actionable(unresolved: List[tuple]) -> List[tuple]:
        """The open findings an editor round answers: every one except a
        finding held for the post-merge recheck."""
        return [u for u in unresolved if ": UNRESOLVED: held for the post-merge recheck" not in u[1]]

    def _plan_findings(self, spec, blocking: List[tuple], notes: List[tuple]) -> dict:
        """Split one review round's blocking findings by what answers them,
        without a call: a typed SCENARIO finding (the plan's scenario text,
        one field, with the reviewer's proposal) goes to the planner's one
        correction per scenario; a typed EVIDENCE finding (a bound case the
        harness has not shown executed on this source) goes to the harness,
        or is held for the post-merge recheck in a parallel child, which
        can run nothing; an ordinary finding is the editor's. A claim that
        opens as a typed one but is incomplete, names a scenario this task
        does not declare, or names a scenario already corrected once, is
        about the plan or the evidence and not the code: it is held
        ``unroutable`` for its reviewer to restate at the recheck and is
        never the editor's instruction (close family, 2026-10-26; before,
        an unbound id went to the editor as code work). The reviewers' notes
        reach the editor with the routed and held lines replaced by the fact
        of their routing, and a round whose every blocking line was routed
        or held makes no editing call. A finding's latest complete typed
        restatement (``restated``, from a recheck) is read before its
        original claim."""
        mine = {r.get("proof_id") for r in self.evidence_obligations
                if r.get("task") == spec.task_id and r.get("kind") == "acceptance" and r.get("proof_id")
                and not r.get("superseded_by")}
        plan = dict(scenario=[], evidence=[], binding=[], held=[], unroutable=[], notes=[], editor=True)
        routed_texts = set()
        for peer, _ in blocking:
            for record in self._finding_records(task=spec.task_id, peer=peer):
                if record.get("route") or record.get("kind") == "unreviewed":
                    continue
                claim = record.get("restated") or record.get("claim") or ""
                kind, detail = _typed_finding(claim)
                if kind is None:
                    miss = _typed_near_miss(claim)
                    if not miss:
                        continue
                    record.update(route="unroutable", route_note=miss)
                    plan["unroutable"].append(record)
                elif detail.get("task") and detail["task"] != spec.task_id:
                    # An earlier task's scenario, named with its task (cycle22):
                    # the same planner correction, only when the record holds it.
                    why = self._prior_scenario_problem(spec, detail["task"], detail["proof_id"])
                    if why:
                        record.update(route="unroutable", typed=detail, route_note=why)
                        plan["unroutable"].append(record)
                    else:
                        record.update(route="scenario", typed=detail)
                        plan["scenario"].append(record)
                elif detail["proof_id"] not in mine:
                    record.update(route="unroutable", typed=detail,
                                  route_note=f"names scenario {detail['proof_id']}, which this task does not declare")
                    plan["unroutable"].append(record)
                elif kind == "scenario" and not self._scenario_allowance(spec, detail["proof_id"]):
                    record.update(route="unroutable", typed=detail,
                                  route_note=f"scenario {detail['proof_id']} was already corrected once for this "
                                             "task; a second correction is not routed")
                    plan["unroutable"].append(record)
                elif kind == "scenario":
                    record.update(route="scenario", typed=detail)
                    plan["scenario"].append(record)
                elif self._scenario_unbound(spec, detail["proof_id"]) and not self._binding_requested(
                        spec, detail["proof_id"]):
                    # No case is bound to the scenario (none declared, or the
                    # declaration was invalid): no run can show it executed,
                    # so the harness route cannot answer it. Its author, the
                    # lead, is asked once on this source for the PROVES lines
                    # alone (cycle01, PR73 6702597850; bounded-recovery-01
                    # t3 routed two missing bindings to the harness, which
                    # ran nothing and changed nothing). Asked again on the
                    # same source it is the same request: the finding takes
                    # the harness route below and its reviewer rechecks it.
                    # A parallel child declares it too and, measuring no
                    # execution, still holds it for the post-merge recheck.
                    record.update(route="binding", typed=detail)
                    if self._batch_child and self.config.integration_gate is None:
                        record["held"] = "held for the post-merge recheck (a parallel child measures no execution)"
                    plan["binding"].append(record)
                elif self._batch_child and self.config.integration_gate is None:
                    record.update(route="held", typed=detail,
                                  held="held for the post-merge recheck (a parallel child measures no execution)")
                    plan["held"].append(record)
                else:
                    record.update(route="evidence", typed=detail)
                    plan["evidence"].append(record)
                routed_texts.add(record.get("text"))
        if not routed_texts:
            plan["notes"] = list(notes)
            return plan
        filtered = []
        for peer, note in notes:
            kept, routed, held = [], [], []
            for raw in note.splitlines():
                record = next((r for r in plan["scenario"] + plan["evidence"] + plan["binding"] + plan["held"]
                               + plan["unroutable"] if r.get("text") == raw.strip() and r["author"] == peer), None)
                if record is None:
                    kept.append(raw)
                elif record["route"] == "unroutable":
                    held.append(f"{record['fid']} ({record['route_note']})")
                else:
                    routed.append(record["fid"])
            if routed:
                kept.append(f"(findings {', '.join(routed)} were routed to the planner or the harness and are not "
                            "yours to answer; do not rewrite code or tests for them)")
            if held:
                kept.append(f"(findings {'; '.join(held)} are typed claims about the plan or the evidence that the "
                            "harness could not route; they are held for the reviewer's re-check and are not yours "
                            "to answer; do not rewrite code or tests for them)")
            filtered.append((peer, "\n".join(kept)))
        plan["notes"] = filtered
        plan["editor"] = any(_has_blocking_finding(n) for _, n in filtered)
        return plan
