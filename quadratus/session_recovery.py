"""The recovery lifecycle of the session engine, as a mixin of Session: the
recovery allowance and its attempts, continuations and closure routes,
owed reviews and their settlement, a failed task's record, and the
completion judgment.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from . import delivery, recovery
from .delegation import invocation
from .memory import TaskMemory
from .providers import TurnLimitReached
from .reply_parsers import (
    _NO_VERDICT_CLASSES,
    _approving_review,
    _clean_goal_review,
    _requirement_verdicts,
)
from .run_budget import FinishReserved
from .session_prompts import _OWED_REVIEW_GRAMMAR, _OWED_SETTLEMENT_ATTEMPTS
from .verdicts import REVIEW_COMPLETE as _REVIEW_COMPLETE
from .verdicts import has_blocking_finding as _has_blocking_finding
from .verdicts import review_complete as _review_complete


class RecoveryMixin:
    """What a failed or unfinished task buys next, and what is still owed for it.

    Session inherits this; nothing here is used on its own.
    """

    def _unchanged_continuation(self, spec, continues) -> str:
        """Why a CONTINUES task is its predecessor re-sent unchanged, or "".

        The same text and the same scope after an unfinished attempt is the
        same attempt (step 2): whatever stopped it stops it again, and the
        breaker would only learn that after another lead call. A stated
        HYPOTHESIS is the planner's declared change and is not refused here;
        the survey's same-cause repeat check and the breaker still judge
        what the attempt then did."""
        record = self.failed_records.get(continues or "") or self.turn_limited_records.get(continues or "")
        if not record or "description" not in record:
            return ""
        def bounds(scope):
            # What the planner declared. The operator's exclusions and the
            # exact-file binding are added by the harness at dispatch.
            return ({k: v for k, v in scope.items() if k not in ("exact", "forbidden_paths")}
                    if scope is not None else None)
        scope = spec.scope.to_dict() if spec.scope is not None else None
        if (" ".join(str(spec.description or "").split()) != record["description"]
                or bounds(scope) != bounds(record.get("scope"))):
            return ""
        return (f"{spec.task_id} continues {continues} with the same task text and scope that did not "
                f"finish. Name what changes this time (a narrower scope, a different split, a different "
                f"method), or name a different task.")

    def _recover_continued(self, continues) -> None:
        """A task that CONTINUES a capped one takes over its debt: the
        predecessor's cap stops blocking, and its record stays (mirrors the
        legacy ``_partial_tasks.discard``; removed in phase 3 with it)."""
        for outcome in self.task_outcomes:
            if continues and outcome.task_id == continues:
                outcome.recover("cap")
                outcome.recover("failed")

    # -- completion workflow: ownership, recovery accounting, closure routes (2026-10-30) ----
    def _owed_tasks(self) -> set:
        """The tasks whose work is still owed: capped, failed or unmerged
        and not taken over by a continuation that closed. Typed and legacy
        together, the same set that blocks completion."""
        return set(self._partial_tasks) | self._partial_from_outcomes()

    def _continues_problem(self, continues) -> str:
        """Why a CONTINUES line cannot stand, before any call; "" when it
        can. It names a task this run dispatched whose work is still owed.
        A task with no record, a task that closed, or one a continuation
        already took over is not owed, and new work does not continue it:
        ownership is declared against the record, never inferred."""
        if not continues:
            return ""
        known = {o.task_id for o in self.task_outcomes} | {s.task_id for s in self.history}
        if continues not in known:
            return f"CONTINUES names {continues}, a task this run has no record of."
        if continues in self._owed_tasks():
            return ""
        takers = [o.task_id for o in self.task_outcomes if o.continues == continues and o.task_id != continues]
        return (f"CONTINUES names {continues}, whose work is not owed"
                + (f" ({', '.join(takers)} took it over)" if takers else " (it closed)")
                + "; new work does not continue it. Name the task without CONTINUES, or continue a task "
                  "that is still owed.")

    def _recovery_decision(self, spec, continues, resolves) -> dict:
        """What this task's CONTINUES and RESOLVES lines take over, decided
        against the record before its first call (evidence-authority-02:
        t3's RESOLVES of two findings on a task that had closed clean was
        charged as a recovery, so the allowance was spent before any work
        had failed, and t4's real continuation was the last one allowed). A
        recovery is a CONTINUES of owed work, or a RESOLVES naming a finding
        raised on owed work; it is charged once whatever it names. A
        RESOLVES naming only findings on closed work is ordinary coverage;
        those findings still settle only through their reviewers' rechecks."""
        owed = self._owed_tasks()
        by_task: Dict[Optional[str], List[str]] = {}
        for fid in resolves or []:
            record = next((r for r in self.review_findings if r["fid"] == fid), None)
            task = record["task"] if record is not None else next(
                (f.get("task") for f in self.findings if f["id"] == fid), None)
            by_task.setdefault(task, []).append(fid)
        recovers = sorted(({continues} if continues in owed else set()) | {t for t in by_task if t in owed})
        ordinary = {t: ids for t, ids in by_task.items() if t not in owed}
        if recovers:
            why = f"recovers the capped, failed or unmerged work of {', '.join(recovers)}"
        elif ordinary:
            why = ("resolves findings on closed work ("
                   + "; ".join(f"{t or 'unknown task'}: {', '.join(ids)}" for t, ids in ordinary.items())
                   + "): ordinary coverage, not charged")
        else:
            why = "ordinary coverage"
        return dict(task=spec.task_id, continues=continues or None, resolves=list(resolves or []),
                    owed=sorted(owed), recovers=recovers, recovery=bool(recovers), charged=False, why=why)

    def _owed_report(self) -> str:
        """The owed work as the record holds it, for the planner and for a
        stop: each owed task with the requirements it covered, the files
        its record says it changed and the review findings still open on it."""
        parts = []
        for tid in sorted(self._owed_tasks()):
            outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == tid), None)
            record = self.turn_limited_records.get(tid) or self.failed_records.get(tid) or {}
            open_ids = [r["fid"] for r in self._finding_records(task=tid)]
            covers = ", ".join(outcome.covers) if outcome is not None and outcome.covers else "nothing listed"
            parts.append(f"{tid}: covers {covers}; files {', '.join(record.get('changed') or ()) or 'none recorded'}; "
                         f"open findings {', '.join(open_ids) or 'none'}")
        return "; ".join(parts) or "none"

    def _closure_route_problem(self, spec, continues, resolves, covers) -> Tuple[str, str]:
        """``(route, problem)`` for a task named while work is owed:
        ``("", "")`` when the task takes the owed work over or does not
        touch it; ``("refuse", why)`` when it covers a requirement or names
        a file of owed work without CONTINUES, which is sent back (the
        harness never infers a takeover from an overlap: the owed task's
        debt and its reviewers' open findings close only through an explicit
        continuation); ``("stop", why)`` when that continuation is one the
        spent recovery allowance would refuse, so the run stops naming the
        owed work before any call is spent on a duplicate it could not close
        (evidence-authority-02: t5 re-did t4's HTTP work with the allowance
        spent, and the batch close stopped on t4 still owed)."""
        owed = self._owed_tasks()
        if not owed:
            return "", ""
        taking = set(self._recovery_decision(spec, continues, resolves)["recovers"])
        overlaps = []
        for tid in sorted(owed - taking):
            # The declared requirement relationship decides, never a shared
            # file: first coverage of another requirement that touches the
            # same file is ordinary work and leaves the owed task owed (root
            # review of d48014d, 2026-10-30).
            outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == tid), None)
            theirs = set(outcome.covers or ()) if outcome is not None else set()
            shared = sorted(theirs & set(covers or ()))
            if shared:
                overlaps.append(f"{tid} (requirements {', '.join(shared)})")
        if not overlaps:
            return "", ""
        detail = ", ".join(overlaps)
        survey = self.config.survey
        if survey is not None and self.survey["recovery_used"] >= survey.recovery_tasks:
            return "stop", (f"SurveyAllowanceSpent: the recovery allowance of {survey.recovery_tasks} task(s) is "
                            f"spent and {spec.task_id} overlaps work still owed by {detail}; no task can continue "
                            "it, and a task over the same requirements without CONTINUES could not close that "
                            f"work, so nothing is spent on it. Owed work: {self._owed_report()}. Work preserved.")
        left = (f"; {max(0, survey.recovery_tasks - self.survey['recovery_used'])} of {survey.recovery_tasks} left"
                if survey is not None else "")
        return "refuse", (f"{spec.task_id} overlaps work still owed by {detail}. The harness never infers a "
                          "takeover: that work closes only through a task whose description carries "
                          "'CONTINUES: <task id>' on its own line (a recovery, charged to the recovery "
                          f"allowance{left}), which inherits its open findings for their reviewers. Name the task "
                          "again with CONTINUES, or name work that does not cover its requirements.")

    def _refresh_recovery_results(self) -> None:
        """An occurrence's result read from the registry, never from the
        action's return: one still saying ``held`` whose every owed id its
        judge has since closed says so, with who closed each and how."""
        closed = {r["fid"]: r for r in self.review_findings if r.get("closed")}
        for occurrence in getattr(self, "recoveries", ()):
            debt = list(occurrence.get("debt") or ())
            if not debt or not str(occurrence.get("result") or "").startswith("held"):
                continue
            if all(fid in closed for fid in debt):
                judges = {fid: ((closed[fid].get("history") or [{}])[-1] or {}) for fid in debt}
                how = "; ".join(f"{fid} {closed[fid].get('state')} by {judges[fid].get('author') or 'unknown'} "
                                f"({judges[fid].get('role') or 'unknown'})" for fid in debt)
                # Recovered only by the debt's own owner; any other closing
                # is recorded as what it was, never as a recovery.
                owned = all(judges[fid].get("author") == closed[fid].get("author") for fid in debt)
                occurrence["result"] = (("recovered: its owner closed the owned debt (" if owned else
                                         "closed, not by its owner (") + how + ")")[:300]

    def _review_gaps(self, note: str) -> List[str]:
        """What an arrived review reply lacks to be a judgment, read only
        (nothing recorded): a verdict line, and for a goal-bearing review a
        single MET or NOT MET line per covered requirement. Only an approval
        needs every verdict: a reply that raises a BLOCKING finding or a NOT
        MET line is a genuine judgment and never a format gap."""
        rids = [rid for rid, _ in self._covered_texts()]
        if _has_blocking_finding(note):
            return []
        verdicts = _requirement_verdicts(note)
        if any(met is False for met in verdicts.values()):
            return []
        gaps = []
        missing = [rid for rid in rids if rid not in verdicts]
        if missing:
            gaps.append(f"one MET or NOT MET line for each of {', '.join(missing)} (duplicates count as none)")
        if not _review_complete(note) and not _clean_goal_review(note, rids):
            gaps.append(f"a verdict: NO FINDINGS, a 'BLOCKING: <finding>' line, or {_REVIEW_COMPLETE}")
        return gaps

    def _claim_judgment(self, limit: int, record: dict) -> bool:
        """Take one of the job's judgment completions before any call. The
        run budget holds the count when there is one: written to budget.json
        before the call, shared by parallel children (one budget) and carried
        into a resumed episode, so a completion that may have started is
        never offered again. Without a budget the session's share counts."""
        budget = self._budget()
        if budget is not None and hasattr(budget, "claim_judgment_completion"):
            return budget.claim_judgment_completion(limit, record)
        share = self._judgment_share
        with share["lock"]:
            used = max(share["used"], sum(1 for r in self.judgment_completions if r.get("claimed")))
            if used >= limit:
                return False
            share["used"] = used + 1
            return True

    # -- recovery lifecycle (quadratus.recovery, 2026-10-31) --------------------

    def _call_event(self, task, role: str, peer: Optional[str]):
        """The newest invoked ledger event for ``task``/``role`` by ``peer``:
        the outer call the harness just made, with its invocation identity
        and nullable usage. None when the run keeps no ledger."""
        ledger = getattr(self.config, "delegation_ledger", None)
        for event in reversed(list(getattr(ledger, "events", None) or [])):
            if not getattr(event, "invoked", False) or str(getattr(event, "task", "")) != str(task):
                continue
            if (getattr(event, "role", "") or "").split(":")[0] != role:
                continue
            if peer is not None and peer not in (getattr(event, "requested_model", None),
                                                 getattr(event, "canonical_model", None)):
                continue
            return event
        return None

    def _call_observations(self, event) -> dict:
        """The allowlisted facts of what ``event``'s call did (its own tool
        record, read while the run is live), or an explicit reason they are
        unavailable. Never raw text, never a search pattern or a command."""
        if event is None:
            return recovery.safe_observations(None, unavailable="the call has no invocation record in this run")
        reader = getattr(self.config, "call_trace", None)
        if reader is None:
            return recovery.safe_observations(None, unavailable="no transcript reader is configured for this run")
        try:
            trace_record = reader(event)
        except Exception as exc:  # noqa: BLE001 -- an observation never fails a run
            return recovery.safe_observations(None, unavailable=f"the call's transcript could not be read "
                                                                f"({type(exc).__name__})")
        if not trace_record:
            return recovery.safe_observations(None, unavailable="the call's transcript was not found in this "
                                                                "environment")
        return recovery.safe_observations(trace_record, project_root=self.project)

    def _recovery_allowance(self) -> dict:
        """What is actually left when a recovery is decided: calls, reported
        tokens, the job's judgment completions, task slots, and the held
        finish. None where the run sets no such bound."""
        budget = self._budget()
        snap = budget.snapshot() if budget is not None else {}
        limits = snap.get("limits") or {}
        limit = int(getattr(self.config, "max_judgment_completions", 0) or 0)
        claimed = snap.get("judgment_completions")
        total = getattr(self, "_max_tasks", None)
        calls = limits.get("max_calls")
        tokens = limits.get("max_reported_tokens")
        held = list((snap.get("finish_protection") or {}).get("pending") or [])
        calls_left = max(0, int(calls) - int(snap.get("reserved_attempts") or 0)) if calls else None
        # Slots after the work admitted and still running (its charge lands
        # after it returns, so ``_slots_used`` alone counts it as free).
        used = int(getattr(self, "_slots_used", 0) or 0)
        running = [int(e.get("slot") or 0) + int(e.get("slots") or 1) - 1
                   for e in getattr(self, "task_admissions", ()) if e.get("outcome") is None]
        return dict(
            calls_left=calls_left,
            # One call per finishing stage still held is not the recovery's.
            calls_left_after_finish=(max(0, calls_left - len(held)) if calls_left is not None else None),
            tokens_left=(max(0, int(tokens) - int(snap.get("reported_tokens") or 0)) if tokens else None),
            judgment_completions_left=(max(0, limit - int(claimed)) if isinstance(claimed, int) else
                                       (limit if budget is None else None)),
            slots_left=(max(0, int(total) - max([used] + running)) if total else None),
            finish_held=held)

    def _recovery_open(self, spec, *, role: str, seat: Optional[str], kind: str, outcome_class: str,
                       question: str, event=None, exc=None, observe: bool = True) -> dict:
        """One recovery occurrence, written when a production path sees an
        operation not give its caller the outcome it needed: identity, the
        source it was bound to, the class, the failed call's measured cost,
        the allowlisted observations, the prior attempts on the same
        identity, the question still open and the allowance left. The
        decision and result are filled in by the caller as they happen."""
        task_id = str(getattr(spec, "task_id", None) or "run")
        identity = f"{task_id}|{role}|{seat or '-'}|{kind}"
        prefix = f"RV{task_id}-"
        number = sum(1 for r in self.recoveries if str(r.get("id", "")).startswith(prefix)) + 1
        record = dict(
            id=f"{prefix}{number}", identity=identity, task=task_id, role=role, seat=seat, kind=kind,
            outcome_class=outcome_class, meaning=recovery.OUTCOME_CLASSES.get(outcome_class, ""),
            protective=outcome_class in recovery.PROTECTIVE,
            source=self._source_fingerprint() if self.project else None,
            question=str(question)[:400],
            prior_attempts=[r["id"] for r in self.recoveries if r.get("identity") == identity],
            failed_call=recovery.call_cost(event),
            observations=(self._call_observations(event) if observe else
                          recovery.safe_observations(None, unavailable="not read for this outcome")),
            allowance=self._recovery_allowance(), eligible=[], decision="", attempts=[], result="pending",
            # The exception's type and, for a cap, its round count: its
            # message can carry vendor or model text and is not copied (the
            # run's own error field keeps it for the operator).
            debt=[], detail=(type(exc).__name__ + (f" after {exc.turns} rounds" if getattr(exc, "turns", None)
                                                    else "") if exc is not None else ""))
        self.recoveries.append(record)
        self._note(f"{task_id}: recovery {record['id']}: {outcome_class} ({role} by {seat or '-'})")
        return record

    def _recovery_tried(self, record: dict, action: str) -> bool:
        """Whether this identity already had ``action`` attempted on the same
        source: the job's memory, so nothing is tried twice unchanged."""
        for other in self.recoveries:
            if other is record or other.get("identity") != record.get("identity"):
                continue
            if other.get("source") != record.get("source"):
                continue
            if any(a.get("action") == action for a in other.get("attempts") or ()):
                return True
        return False

    def _recovery_attempt(self, record: Optional[dict], action: str, outcome: str, *, task=None, role=None,
                          peer=None, site: str = "", source: Optional[str] = None) -> None:
        """One attempted action on an occurrence, with the outer call it made
        (its own ledger event: identity and measured cost; none when no call
        was made)."""
        if record is None:
            return
        event = self._call_event(task, role, peer) if task is not None and role else None
        call = recovery.call_cost(event)
        failed = (record.get("failed_call") or {}).get("invocation_id")
        known = {failed} | {(a.get("call") or {}).get("invocation_id") for a in record.get("attempts") or ()}
        if call is not None and call.get("invocation_id") in known:
            call = None  # no new call was made for this attempt
        # The source the attempt judged: given by a caller that read it before
        # the call (a voided owner review judged the old source, not the new).
        stamped = source if source is not None else (self._source_fingerprint() if self.project else None)
        record["attempts"].append(dict(action=action, outcome=str(outcome)[:300], site=site, source=stamped,
                                       call=call))

    def _recovery_result(self, record: Optional[dict], result: str) -> None:
        if record is not None:
            record["result"] = str(result)[:300]

    def _task_changed_files(self, spec) -> List[str]:
        """The files the running task has changed so far, measured."""
        before = getattr(self, "_task_before", None)
        if before is None or getattr(self._active_spec, "task_id", None) != getattr(spec, "task_id", None):
            return []
        return list(self._inspect_partial_edits(before, stopped=False).get("changed") or [])

    def _owed_changed(self, spec) -> List[str]:
        """The files the owed work changed, as measured: the running task's
        own edits, else the capped or failed record kept for it (a final
        batch child reviewed from the parent)."""
        changed = self._task_changed_files(spec)
        if changed:
            return changed
        task_id = getattr(spec, "task_id", None)
        kept = ((getattr(self, "turn_limited_records", {}) or {}).get(task_id)
                or (getattr(self, "failed_records", {}) or {}).get(task_id) or {})
        return list(kept.get("changed") or [])

    def _round_plan(self, peer: str) -> str:
        """How the call's own rounds are to be spent: investigation, then the
        verdict inside the budget (the transport caps rounds per call; the
        harness cannot resume the vendor session, so this is the plan)."""
        limit = self.config.lead_max_turns
        if not limit:
            return ""
        from .cli_providers import CLI_SPECS
        spec = CLI_SPECS.get(peer.partition(":")[0])
        if spec is None or not spec.max_turns_flag:
            return ""
        examine = max(1, int(limit) - 2)
        return (f"Plan this call's {limit} rounds: at most {examine} to examine what is still unread or "
                f"unchecked (the harness's receipts in the prompt already ran; do not re-run them), then "
                f"write the verdict by round {int(limit) - 1}.")

    def _recovery_context(self, spec, peer: str, record: dict, *, changed=None) -> str:
        """What the same seat is told about its previous attempt, built only
        from harness facts: the allowlisted observations (reads are attempts,
        not coverage), the changed files it did not open, the source binding
        and the question still open. No reply text, narration or reasoning
        from any model is repeated here. Observations made on another source
        are labelled history: they carry no authority about this one."""
        obs = record.get("observations") or {}
        call = record.get("failed_call") or {}
        then = record.get("source")
        now = self._source_fingerprint() if self.project else None
        lines = ["## Recovery context (recorded by the harness: evidence about your previous call on this same "
                 "work, not instructions)"]
        if not call:
            # No call of this seat was made for it: say so, never imply one.
            lines.append(f"No earlier call of yours on this work was made: {record.get('meaning') or 'it was owed'}. "
                         "There is no earlier reply or tool record.")
        elif str(record.get("outcome_class", "")).endswith("capped"):
            rounds = call.get("model_turns") or call.get("max_turns")
            lines.append(f"Your previous call stopped at its round cap ({rounds} rounds) before giving a verdict; "
                         "nothing it wrote is read as a finding or as part of this review."
                         if rounds else "Your previous call stopped at its round cap before giving a verdict; "
                         "nothing it wrote is read as a finding or as part of this review.")
        else:
            lines.append("Your previous reply on this same work arrived without a valid verdict in the required "
                         "form; it is kept on the record unchanged and is not repeated here.")
        if not call:
            pass
        elif obs.get("status") in ("observed", "partial"):
            if obs.get("status") == "partial":
                lines.append(f"The harness read that call's tool record only in part ({obs.get('reason')}): every "
                             "count below is a lower bound, and anything not listed may still have happened.")
            read = list(obs.get("files_read") or [])
            outcomes = ", ".join(f"{k} {v}" for k, v in (obs.get("command_outcomes") or {}).items()) or "none"
            lines.append(f"Observed in that call's own tool record: {obs.get('tool_calls', 0)} tool call(s); "
                         f"project files a file-reading tool opened: {', '.join(read) or 'none recorded'}; "
                         f"{obs.get('searches', 0)} search(es) and {obs.get('commands', 0)} command(s) "
                         f"(outcomes: {outcomes}; their patterns and text are not repeated). Opening a file does "
                         "not show it was reviewed, and reads made by commands, searches or tools the harness "
                         "does not recognise are not in that list, so it is not a list of what you left unread.")
            changed = list(changed if changed is not None else self._task_changed_files(spec))
            if changed:
                unseen = [path for path in changed if path not in read]
                lines.append("Files this task changed that no file-reading tool was observed opening: "
                             + (", ".join(unseen) or "none"))
        else:
            lines.append(f"The harness could not observe that call's tool use ({obs.get('reason') or 'unavailable'}); "
                         "nothing is assumed about what it examined.")
        if call and then and now and then != now:
            lines.append(f"That call ran on source {str(then)[:16]}; the source is now {str(now)[:16]}. What it "
                         "observed is history about an earlier source: it shows nothing about the source you judge "
                         "now and approves nothing.")
        lines.append(f"This judgment is bound to source {str(now or then or 'unavailable')[:16]}: a changed source "
                     "voids it before it is read.")
        lines.append("Still open: " + str(record.get("question") or "your verdict"))
        plan = self._round_plan(peer)
        if plan:
            lines.append(plan)
        return "\n".join(lines) + "\n\n"

    def _recover_judgment(self, spec, peer: str, kind: str, *, role: str, prompt: str, task=None, outcome=None,
                          exc=None, raw: str = "", gaps=(), review_source=None) -> Tuple[Optional[str], dict]:
        """A required review or recheck by ``peer`` that did not finish (its
        call reached the round cap: ``exc``; or a reply arrived without a
        valid verdict: ``raw`` and ``gaps``). The occurrence is recorded with
        what the call did; then, once per identity and source, the same seat
        completes its judgment with that context carried
        (``_complete_judgment``: the job's completion allowance, claimed
        before the call and never refunded; the finish hold, the source
        binding and the seat's own turn cap all apply). Returns the new
        reply (the caller's strict reader decides whether it is a verdict)
        or None, with the occurrence; the caller records the result."""
        capped = exc is not None
        outcome_class = _NO_VERDICT_CLASSES.get((kind, capped), "review_capped" if capped else "review_unfinished")
        event = self._call_event(spec.task_id, role, peer)
        question = (f"{peer}'s verdict on {spec.task_id}'s {kind}"
                    + (f" ({'; '.join(str(g) for g in gaps)})" if gaps else ""))
        record = self._recovery_open(spec, role=role, seat=peer, kind=kind, outcome_class=outcome_class,
                                     question=question, event=event, exc=exc)
        if capped and getattr(exc, "partial_text", None):
            # Narration of unfinished work: kept on the record, never read as
            # a result and never placed in a prompt.
            record["narration_artifact"] = self.store.put(exc.partial_text, kind=f"capped-narration:{kind}:{peer}",
                                                          author=peer).id
        record["eligible"] = ["complete_judgment", "hold_owned_debt"]
        if self._recovery_tried(record, "complete_judgment"):
            record["decision"] = "hold_owned_debt: a completion was already attempted for this judgment on this source"
            return None, record
        if not self._available(peer):
            record["decision"] = "hold_owned_debt: the seat that owes the judgment is unavailable"
            return None, record
        record["decision"] = "complete_judgment"
        missing = [str(g) for g in gaps] or ["a verdict: the previous call stopped at its round cap"]
        reply = self._complete_judgment(spec, peer, kind, raw or "", missing, role=role, prompt=prompt, task=task,
                                        outcome=outcome, context=self._recovery_context(spec, peer, record),
                                        recovery_record=record, review_source=review_source)
        last = (record.get("attempts") or [{}])[-1]
        if str(last.get("outcome") or "").startswith("not attempted"):
            # Decided on what was actually available: no completion was made.
            record["decision"] = f"hold_owned_debt: completion {last['outcome']}"
        return reply, record

    def _owed_record(self, fid) -> Optional[dict]:
        """The open owed-review record ``fid`` names, or None."""
        record = next((r for r in self.review_findings if r.get("fid") == fid), None)
        if record is None or record.get("closed") or record.get("kind") != "unreviewed":
            return None
        return record

    def _owe_review(self, spec, peer: str, kind: str, why: str, *, role: str, outcome=None,
                    record: Optional[dict] = None, fids=(), original: str = "", final_cap: bool = False) -> List[str]:
        """A required judgment by ``peer`` that did not finish, held as owned
        review debt (never a bare open string): for a recheck, the findings it
        was asked about stay open under their own ids, held for their author;
        for a review, design review or verification, one ``unreviewed``
        record (delivery.FINDING_KINDS) is registered for the owner, bound to
        this source and the task's covers. Either way the debt is in the
        held entries every later reader uses (the close policy, the planner,
        RESOLVES and CONTINUES, the post-merge recheck, DONE and the cap),
        and only the owner's completed judgment settles it."""
        held: List[str] = []
        if fids:
            # The reviewer's whole original finding stays on the record beside
            # the ids (Codex review of 0983dac): a multi-line finding whose
            # BLOCKING line carried no text of its own registers no record,
            # and it must not leave the record with the cap.
            extra = (original or "").strip()
            for fid in fids:
                existing = next((r for r in self.review_findings if r.get("fid") == fid), None)
                if existing is None or existing.get("closed"):
                    continue
                if not any(e.get("finding_id") == fid for e in self.unresolved_reviews
                           if e.get("text") in self.open_findings):
                    shown = str(existing.get("text") or existing["claim"])
                    verdict = f"{fid}: UNRESOLVED (no verdict at its recheck by {peer}: {why}): {shown}"
                    if extra and extra != shown.strip():
                        verdict += f" [the reviewer's original findings, not re-checked: {extra[:2000]}]"
                        extra = ""
                    # Never carried to the audit in place of its author: a
                    # recheck that gave no verdict is the author's debt, and
                    # the task that holds it closed with an open finding.
                    self._hold_open_finding(spec, peer, verdict, record=existing, outcome=outcome, carry=False)
                # On the record too, so the audit is not handed it in the
                # author's place (``Finding.owed_recheck``) until the author
                # judges it again.
                existing.update(replace(delivery.Finding.from_dict(existing), owed_recheck=True).to_dict())
                held.append(fid)
        elif any(r.get("kind") == "unreviewed" and not r.get("closed") and r.get("task") == str(spec.task_id)
                 and r.get("author") == peer and r.get("owed_kind") == kind for r in self.review_findings):
            # The same judgment is already owed by the same seat for this
            # task: one record, never a second for the same debt.
            existing = next(r for r in self.review_findings if r.get("kind") == "unreviewed" and not r.get("closed")
                            and r.get("task") == str(spec.task_id) and r.get("author") == peer
                            and r.get("owed_kind") == kind)
            held.append(existing["fid"])
        else:
            prefix = self._finding_prefix(spec)
            numbers = [int(r["fid"][len(prefix):]) for r in self.review_findings
                       if str(r.get("fid", "")).startswith(prefix) and str(r["fid"][len(prefix):]).isdigit()]
            fid = f"{prefix}{max(numbers, default=0) + 1}"
            covers = list(getattr(self, "_current_covers", []) or [])
            revisions = self._requirement_revisions()
            requirement = (covers[0], revisions.get(covers[0], 1)) if len(covers) == 1 else None
            claim = (f"UNREVIEWED: the {kind} of {spec.task_id} by {peer} did not finish ({why}); its judgment of "
                     "this work is owed")
            finding = delivery.Finding(fid, peer, role, claim, delivery.FindingBasis("none"), "unstated",
                                       self._source_fingerprint() or "unavailable", str(spec.task_id), requirement,
                                       (), "unreviewed")
            row = dict(finding.to_dict(), text=claim, owed_kind=kind, owed_role=role, covers=covers,
                       description=str(getattr(spec, "description", "") or "")[:2000],
                       changed=self._owed_changed(spec))
            self.review_findings.append(row)
            verdict = f"{fid}: UNRESOLVED (owed {kind} by {peer}: {why}): {claim}"
            self._hold_open_finding(spec, peer, verdict, record=row, outcome=outcome)
            held.append(fid)
        if final_cap and not fids:
            # Owed for a capped child of the job's final batch: the owner's
            # clean settlement lifts the cap exactly as a clean review of it
            # would have (``_lift_final_cap``).
            for row_ in self.review_findings:
                if row_.get("fid") in held:
                    row_["final_cap"] = True
        # The held entry carries the typed fact (``_hold_open_finding``), so
        # the owner's settlement recovers it atomically; no second fact is
        # added that nothing could close.
        self._judgment_gap(spec, peer, kind, why)
        if record is not None:
            record["debt"] = sorted(set(record.get("debt") or []) | set(held))
            held = list(record["debt"])
            if "hold_owned_debt" not in str(record.get("decision") or ""):
                record["decision"] = (record.get("decision") + "; then " if record.get("decision") else "") + \
                    "hold_owned_debt"
            self._recovery_result(record, "held: owned review debt " + (", ".join(held) or "(already closed)"))
        return held

    def _owed_texts(self) -> set:
        """The held open texts that are owed review debt (an ``unreviewed``
        record, or a finding whose recheck gave no verdict): settled by the
        owner's judgment, not by the evidence loop."""
        out = set()
        for entry in self.unresolved_reviews:
            text = entry.get("text")
            if text not in self.open_findings:
                continue
            fid = entry.get("finding_id")
            record = next((r for r in self.review_findings if r.get("fid") == fid), None)
            if record is None or record.get("closed"):
                continue
            if record.get("kind") == "unreviewed" or "(no verdict at its recheck by " in str(text):
                out.add(text)
        return out

    def _owed_review_prompt(self, spec, record: dict, situation: str) -> str:
        """The owner's review of work it owes a judgment on, as the source
        now stands: the task as it was described, the files measured as
        changed, the contract its findings are held to, the review grammar,
        and the harness's recovery context. The settling task's own text is
        not the subject."""
        task_id = record.get("task")
        changed = ", ".join(record.get("changed") or []) or "not recorded"
        last = next((r for r in reversed(self.recoveries) if record["fid"] in (r.get("debt") or ())), None)
        context = (self._recovery_context(spec, record["author"], last, changed=record.get("changed") or [])
                   if last is not None else "")
        return (self._role_packet(spec, "reviewer") + "\n\n"
                f"## The work you owe a judgment on ({record['fid']})\n"
                f"Task {task_id}, as it was described: {str(record.get('description') or '')[:1500]}\n"
                f"Files it changed (measured by the harness): {changed}.\n"
                f"Your {record.get('owed_kind') or 'review'} of it is owed: it did not finish or was never made, so "
                "this work has no completed review by you. "
                f"{situation} Review that work as it stands now in your working directory, against the original "
                "goal and the requirements below.\n\n" + context
                + _OWED_REVIEW_GRAMMAR + self._goal_authority(record["author"])
                + self._review_turn_budget_note(record["author"]))

    def _owed_settlements(self, fid: str) -> List[dict]:
        """Every owner settlement made for ``fid``, across every occurrence
        that names it (a later occurrence that re-owes the same record never
        resets the count or the per-source rule)."""
        return [a for r in self.recoveries if fid in (r.get("debt") or ())
                for a in r.get("attempts") or () if a.get("action") == "owner_settlement"
                and not str(a.get("outcome") or "").startswith("not attempted")]

    def _settle_owed(self, spec, record: dict, *, situation: str, outcome=None, site: str = "") -> dict:
        """One owner judgment of an owed-review record on the current source.
        Only the record's author, only while that seat is available, at most
        ``_OWED_SETTLEMENT_ATTEMPTS`` times per record (counted over every
        occurrence that names it) and never twice on the same source. A
        completed review closes the record (RESOLVED by its author, the
        harness recording the completion) and registers every BLOCKING line,
        and every covered requirement it calls NOT MET, as a new held finding
        of the original task; a capped, refused, voided or verdict-less reply
        leaves it open, with the attempt on the record, stamped with the
        source the owner was shown. Returns the row the settlement reports."""
        fid, owner = record["fid"], record["author"]
        row = dict(peer=owner, findings=1, verdict=None, resolved=[], kept=[], new=[], owed=fid)
        occurrence = next((r for r in reversed(self.recoveries) if fid in (r.get("debt") or ())), None)
        if occurrence is None:
            occurrence = self._recovery_open(SimpleNamespace(task_id=record.get("task")), role="owner-review",
                                             seat=owner, kind=record.get("owed_kind") or "review",
                                             outcome_class="review_capped", question=f"{owner}'s judgment owed on "
                                             f"{record.get('task')} ({fid})", observe=False)
            occurrence["debt"] = [fid]
        source = self._source_fingerprint() if self.project else None
        tried = self._owed_settlements(fid)
        if record.get("owed_kind") == "test comparison":
            # Bounded continuation (root review of 0ffc9e9, item 2): a call
            # that judged some files and left others waiting for the call's
            # bound does not use up the source; a call that judged nothing
            # does. The attempt bound still caps the total.
            reason = ("kept: the owner is unavailable" if not self._available(owner)
                      else "kept: the owner already judged this exact source without a verdict"
                      if any(a.get("source") == source and not str(a.get("outcome") or "").startswith("partial")
                             for a in tried)
                      else f"kept: the owner's {_OWED_SETTLEMENT_ATTEMPTS} settlement attempts are spent"
                      if len(tried) >= _OWED_SETTLEMENT_ATTEMPTS else "")
            if reason:
                row["verdict"] = reason
                self._recovery_attempt(occurrence, "owner_settlement", "not attempted: " + reason[6:], site=site,
                                       source=source)
                return row
            return self._settle_test_comparison(spec, record, owner, site=site, occurrence=occurrence)
        if record.get("owed_kind") not in ("review", None):
            reason = (f"kept: a {record.get('owed_kind')} is settled by its owner's completed "
                      f"{record.get('owed_kind')} of a continuation of {record.get('task')}, not by a code review")
        elif not self._available(owner):
            reason = "kept: the owner is unavailable"
        elif any(a.get("source") == source for a in tried):
            reason = "kept: the owner already judged this exact source without a verdict"
        elif len(tried) >= _OWED_SETTLEMENT_ATTEMPTS:
            reason = f"kept: the owner's {_OWED_SETTLEMENT_ATTEMPTS} settlement attempts are spent"
        else:
            reason = ""
        if reason:
            row["verdict"] = reason
            self._recovery_attempt(occurrence, "owner_settlement", "not attempted: " + reason[6:], site=site,
                                   source=source)
            return row
        task_id = record.get("task") or spec.task_id
        owner_spec = SimpleNamespace(task_id=task_id, description=record.get("description") or "")
        saved_covers = self._current_covers if hasattr(self, "_current_covers") else None
        self._current_covers = list(record.get("covers") or [])

        # The full identity the owner judges (source, checks, requirement
        # texts, contract, packet), taken before the call and compared after
        # it, as on the completion and recheck routes (root's review of
        # a7f9c6c, R3). Every attempt carries the pre-call source.
        binding = self._judgment_binding(owner_spec, owner, "collaborator")

        def attempt(result: str, *, called: bool = True) -> None:
            self._recovery_attempt(occurrence, "owner_settlement", result, site=site, source=source,
                                   **(dict(task=task_id, role="collaborator", peer=owner) if called else {}))

        try:
            prompt = self._owed_review_prompt(spec, record, situation)
            try:
                with invocation(task_id, "collaborator"):
                    reply = self._invoke_model(owner, prompt)
            except TurnLimitReached:
                attempt("capped: no verdict")
                row["verdict"] = "kept: the owner's review stopped at its round cap again"
                return row
            except FinishReserved as exc:
                self._finish_refused(spec, f"owed review of {task_id} by {owner}", exc, outcome=outcome)
                # No call was made, so nothing about this source was judged.
                attempt("not attempted: refused to keep the finish", called=False)
                row["verdict"] = "kept: refused to keep the finish"
                return row
            self.store.put(reply, kind=f"review:{owner}", author=owner)
            after = self._judgment_binding(owner_spec, owner, "collaborator")
            if after["identity"] != binding["identity"]:
                changed = sorted(k for k in binding if k != "identity" and binding[k] != after.get(k))
                attempt(f"void: the bound identity changed during the review ({', '.join(changed)})")
                row["verdict"] = f"kept: the bound identity changed during the owner's review ({', '.join(changed)})"
                return row
            rids = [rid for rid, _ in self._covered_texts()]
            verdicts = _requirement_verdicts(reply)
            unmet = [rid for rid in rids if verdicts.get(rid) is False]
            judged = (_has_blocking_finding(reply) or bool(unmet) or _review_complete(reply)
                      or _clean_goal_review(reply, rids))
            if self._review_gaps(reply) or not judged:
                attempt("no valid verdict")
                row["verdict"] = "kept: the owner's reply gave no valid verdict"
                return row
            # The ordinary review qualification, exactly as on the review
            # route (root's review of a7f9c6c, R4): a NOT MET line is a
            # BLOCKING finding, and a MET on an error or refusal requirement
            # counts only on executed negative-path evidence (a cited source
            # file is not that). The owner's own call set the scratch proof
            # it demonstrated, as a review call does; notes land on the owed
            # task's own outcome.
            holder = next((o for o in reversed(self.task_outcomes) if o.task_id == task_id), None)
            saved_outcome = self._outcome
            self._outcome = holder or outcome or self._outcome
            try:
                note = self._judge_goal_review(owner_spec, owner, reply, record=occurrence)
            finally:
                self._outcome = saved_outcome
            new = self._register_review_findings(owner_spec, owner, note, role="collaborator")
            for made in new:
                verdict = f"{made['fid']}: UNRESOLVED (raised by {owner} in its owed review): {made['text']}"
                self._hold_open_finding(owner_spec, owner, verdict, record=made, outcome=outcome)
                row["new"].append(made["fid"])
            settling = self._call_event(task_id, "collaborator", owner)
            self._close_owed(record, owner, f"completed review on source {str(source)[:16]}"
                             + (f"; new findings {', '.join(row['new'])}" if row["new"] else "; no findings"),
                             call=getattr(settling, "invocation_id", "") or "")
            row["resolved"].append(fid)
            row["verdict"] = "reviewed" + (f": new {', '.join(row['new'])}" if row["new"] else ": no findings")
            if record.get("final_cap") and not row["new"] and _approving_review(note, rids):
                row["cap_lifted"] = self._lift_final_cap(task_id, owner, list(record.get("covers") or []), note,
                                                         rids)
            attempt(row["verdict"])
            self._recovery_result(occurrence, "recovered: the owner completed the owed review"
                                  + (f"; new findings {', '.join(row['new'])} are held" if row["new"] else ""))
            return row
        finally:
            self._current_covers = saved_covers

    def _lift_final_cap(self, task_id: str, peer: str, covers: List[str], note: str, rids) -> bool:
        """A capped child of the final batch whose owed review its owner has
        now completed clean: the cap is lifted as the review itself would
        have lifted it (``_review_capped_final_child``), on that task's own
        outcome. Nothing else: no slot, recovery, turn or grant."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == task_id), None)
        if outcome is None:
            return False
        notes = not (_clean_goal_review(note, rids) if rids else note.strip().upper().rstrip(".") == "NO FINDINGS")
        outcome.recover("cap")
        outcome.note("cap", f"capped in the final parallel batch; merged source passed the checks and {peer}'s "
                            "owed review, completed at its settlement, approved it"
                            + (" with non-blocking notes kept on record" if notes else ""), terminal=False)
        outcome.edge("review", True)
        self._partial_tasks.discard(task_id)
        for rid in covers:
            self._mark_covered(rid, task_id)
        self._note(f"{task_id}: capped final work reviewed clean by {peer} at its owed settlement; its cap is lifted")
        return True

    def _close_owed(self, record: dict, owner: str, why: str, *, call: str = "") -> None:
        """Close an owed-review record on its author's completed review: one
        RESOLVED disposition by that author (delivery.designated_judge holds),
        then every held text and fact for it, atomically."""
        finding = delivery.Finding.from_dict(record)
        refusal = delivery.designated_judge(finding, owner, "recheck")
        if refusal:
            raise ValueError(refusal)
        source = self._source_fingerprint() or "unavailable"
        settled = replace(finding, history=finding.history + (
            delivery.Disposition(finding.fid, delivery.RESOLVED, why[:300], owner, "recheck", source, source,
                                 str(call or "")),))
        record.update(settled.to_dict())
        self._close_review_finding_by_id(finding.fid, f"reviewed by {owner}: {why[:160]}")

    def _settle_owed_by_judgment(self, spec, peer: str, kind: str) -> List[str]:
        """An owed design review or verification settles when its owner
        completes the same kind of judgment on the current source for this
        task, a task that continues it, or a task that names it in RESOLVES:
        the owner's own completed judgment, never another seat's and never a
        code review standing in for it. Returns the ids closed."""
        chain = set(self._continuation_chain(getattr(self, "_continues", None))) | {str(spec.task_id)}
        named = set(getattr(self, "_current_resolves", None) or [])
        closed = []
        for record in [r for r in self.review_findings if r.get("kind") == "unreviewed" and not r.get("closed")
                       and r.get("owed_kind") == kind and r.get("author") == peer]:
            if record.get("task") not in chain and record["fid"] not in named:
                continue
            self._close_owed(record, peer, f"completed {kind} in {spec.task_id}")
            occurrence = next((r for r in reversed(self.recoveries) if record["fid"] in (r.get("debt") or ())), None)
            self._recovery_attempt(occurrence, "owner_settlement", f"completed {kind} in {spec.task_id}",
                                   site=f"{kind} in {spec.task_id}")
            self._recovery_result(occurrence, f"recovered: {peer} completed the owed {kind}")
            closed.append(record["fid"])
        return closed

    def _owed_attempts(self, fid: str) -> int:
        """Owner settlements already attempted for the owed record ``fid``."""
        return len(self._owed_settlements(fid))

    def _owed_open(self) -> List[dict]:
        """The open owed judgments: ``unreviewed`` records, and defects whose
        author's recheck gave no verdict."""
        return [r for r in self.review_findings if not r.get("closed")
                and (r.get("kind") == "unreviewed" or r.get("owed_recheck"))]

    def _settle_owed_reviews(self, site: str) -> List[dict]:
        """Before a terminal decision with no planner after it (an explicit
        list's end, the cap): one owner judgment of each open owed review on
        the current source, under the same bounds as any settlement. Nothing
        is accepted here; a review that settles only removes the debt it
        owed, and anything it finds is held."""
        rows = []
        for record in [r for r in self.review_findings if r.get("kind") == "unreviewed" and not r.get("closed")]:
            spec = SimpleNamespace(task_id=record.get("task"), description=record.get("description") or "",
                                   scope=None, kind="review", complexity="simple", lead=None, needs=frozenset())
            holder = next((o for o in reversed(self.task_outcomes) if o.task_id == record.get("task")), None)
            rows.append(self._settle_owed(spec, record, outcome=holder or self.run_outcome, site=site,
                                          situation=f"No task is left to name a settlement ({site}): this is the one "
                                                    "owner review the harness makes before the run's finish."))
        # A defect whose author's recheck gave no verdict: one recheck by
        # that author on the current source, the ordinary settlement recheck,
        # never twice on the same source and within the same attempt bound.
        source = self._source_fingerprint() if self.project else None
        by_task: Dict[str, List[dict]] = {}
        for record in [r for r in self.review_findings if r.get("owed_recheck") and not r.get("closed")]:
            tried = self._owed_settlements(record["fid"])
            if any(a.get("source") == source for a in tried) or len(tried) >= _OWED_SETTLEMENT_ATTEMPTS:
                continue
            entry = next((e for e in self.unresolved_reviews if e.get("finding_id") == record["fid"]
                          and e.get("text") in self.open_findings), None)
            if entry is not None:
                by_task.setdefault(str(record.get("task")), []).append(entry)
        for task_id, entries in by_task.items():
            description = next((e.get("description") for e in entries if e.get("description")), "") or ""
            spec = SimpleNamespace(task_id=task_id, description=description, scope=None, kind="review",
                                   complexity="simple", lead=None, needs=frozenset())
            holder = next((o for o in reversed(self.task_outcomes) if o.task_id == task_id), None)
            # The recheck records itself as the owner settlement of each owed
            # finding (``_recheck_open_findings``), with its pre-call source.
            rows.append(self._recheck_open_findings(
                spec, entries, holder or self.run_outcome,
                situation=(f"No task is left to name a settlement ({site}). Your recheck of these findings gave no "
                           "verdict; this is the one recheck the harness makes before the run's finish, on the "
                           "source as it now stands.")))
        return rows

    def _record_task_failure(self, spec, exc, lead: str, state: dict, changed: List[str]) -> None:
        """The occurrence for a task that failed and was handed back
        (``_close_failed``): the call that failed, bound to its own ledger row
        (identity, measured cost, unknown usage kept unknown), its observed
        trace through the allowlist (progress included where the transcript
        has it), and what it left on disk, as counts. The task fails, the run
        goes on; nothing is retried here."""
        names = set()
        seen = exc
        for _ in range(6):
            if seen is None:
                break
            names |= {klass.__name__ for klass in type(seen).__mro__}
            seen = seen.__cause__ or seen.__context__
        if exc.cause == "transport":
            cls = "provider_timeout" if names & {"TimeoutError", "TimeoutExpired"} else "provider_error"
        else:
            cls = "task_failed"
        call = dict(getattr(self, "_active_call", {}) or {})
        role = str(call.get("role") or "lead")
        seat = call.get("model") or lead
        event = self._call_event(spec.task_id, role.split(":")[0], seat)
        record = self._recovery_open(spec, role=role, seat=seat, kind="task", outcome_class=cls,
                                     question=f"{spec.task_id} failed ({exc.cause}) and was handed back",
                                     event=event, exc=exc, observe=event is not None)
        record["effect"] = dict(inspected=bool(state.get("inspected")), changed_files=len(changed),
                                changed_lines=int(state.get("changed_lines") or 0))
        record["eligible"], record["decision"] = ["hand_back"], ("hand_back: the task failed; its work is kept and the "
                                                               "planner names the rest (CONTINUES)")
        self._recovery_result(record, f"failed: {spec.task_id} handed back ({exc.cause})")

    @staticmethod
    def _bind_unknown_causes(ops: List[dict], invoked: list) -> list:
        """The ledger row of each unknown-usage operation, or None (root's
        review of 56ab825, item 1). A row whose ``operation`` equals the op
        id is that call. Only a row with no operation id at all (written
        before the id existed) may be associated otherwise, and only when it
        is the single unclaimed row of the op's task, role and seat with
        unknown usage, and no other unbound op of that task, role and seat
        could claim it; a row is never bound to two operations. Anything
        else stays unbound, so the operation is counted once as
        unidentified instead of borrowing another call's cost."""
        def same(op, event):
            return (str(getattr(event, "task", "")) == str(op.get("task"))
                    and (getattr(event, "role", "") or "").split(":")[0] == str(op.get("role") or "").split(":")[0]
                    and op.get("seat") in (getattr(event, "requested_model", None),
                                           getattr(event, "canonical_model", None)))
        rows: list = [None] * len(ops)
        claimed = set()
        for i, op in enumerate(ops):
            exact = [e for e in invoked if op.get("opid") and getattr(e, "operation", None) == op.get("opid")]
            if len(exact) == 1 and id(exact[0]) not in claimed:
                rows[i] = exact[0]
                claimed.add(id(exact[0]))
        legacy = [e for e in invoked if not getattr(e, "operation", None) and not getattr(e, "tokens_known", False)]
        for i, op in enumerate(ops):
            if rows[i] is not None:
                continue
            candidates = [e for e in legacy if id(e) not in claimed and same(op, e)]
            rivals = [o for j, o in enumerate(ops) if j != i and rows[j] is None
                      and o.get("task") == op.get("task") and o.get("seat") == op.get("seat")
                      and str(o.get("role") or "").split(":")[0] == str(op.get("role") or "").split(":")[0]]
            if len(candidates) == 1 and not rivals:
                rows[i] = candidates[0]
                claimed.add(id(candidates[0]))
        return rows

    def _record_stop_rule(self, reason: str) -> None:
        """The occurrence for a stop the session chose rather than one an
        exception forced (``stop_reason``: open findings, unmet requirements,
        a failed check, a breaker): its own rule, recorded for the
        improvement loop, never retried and never reconciled."""
        name = str(reason or "").split(":", 1)[0].strip() or "stop"
        if any(r.get("kind") == "run" and r.get("result") == f"stopped: {name}" for r in self.recoveries):
            return
        record = self._recovery_open(SimpleNamespace(task_id="run"), role="run", seat=None, kind="run",
                                     outcome_class="stop_rule", question=f"the run ended on {name}", observe=False)
        record["detail"] = str(reason)[:200]
        record["eligible"], record["decision"] = ["stop"], "stop: the run's own stop rule"
        self._recovery_result(record, f"stopped: {name}")

    def _record_terminal_recovery(self, exc: BaseException) -> None:
        """The occurrence for an exception that ends the run: classified by
        the exception's type, its cause chain and the budget's structured
        stop reason, never by its message (a provider error carries model
        text); bound to the failed call only when that call is the newest one
        and did not succeed; marked protective or uncertain. A protective
        stop stays a stop; an uncertain outcome names reconciliation; neither
        is retried."""
        name = type(exc).__name__
        chain, seen = [], exc
        while seen is not None and len(chain) < 6:
            chain.append(seen)
            seen = seen.__cause__ or seen.__context__
        names = {klass.__name__ for item in chain for klass in type(item).__mro__}
        budget = self._budget()
        reason = str(getattr(budget, "_reason", "") or "") if budget is not None else ""
        if "PartialWorkStopped" in names or "PartialWorkSuspected" in names:
            cls = "uncertain_write"
        elif names & {"DependencyTreeChanged", "DependencyIdentityUnavailable", "EvidenceIdentityMismatch",
                      "JobStoreUnavailable", "CheckpointRefused"}:
            cls = "integrity"
        elif names & {"CapabilityUnavailable", "ProviderRefusal", "PermissionBlocked"}:
            cls = "permission"
        elif names & {"KeyboardInterrupt", "CancelledError"}:
            cls = "cancelled"
        elif "WindowExhausted" in names:
            cls = "provider_quota"
        elif reason == "unknown_usage":
            cls = "unknown_usage"
        elif names & {"TimeoutError", "TimeoutExpired", "_GateTimeout"}:
            cls = "provider_timeout"
        elif "FinishReserved" in names or "RunBudgetExceeded" in names:
            cls = "capacity_exhausted"
        elif "OperatorInputNeeded" in names:
            cls = "operator_input"
        elif "RunStalled" in names:
            cls = "stalled"
        elif "ProviderError" in names:
            cls = "provider_error"
        elif str(getattr(type(exc), "__module__", "")).startswith("quadratus"):
            # One of the run's own deterministic stop rules (a failed or
            # unattributable check, unverified evidence, open findings,
            # unmet requirements, a breaker): known, not uncertain.
            cls = "stop_rule"
        else:
            cls = "unknown"
        call = dict(getattr(self, "_active_call", {}) or {})
        task = call.get("task") or getattr(self._active_spec, "task_id", None) or "run"
        role = str(call.get("role") or "run")
        seat = call.get("model")
        ledger = getattr(self.config, "delegation_ledger", None)
        invoked = [e for e in list(getattr(ledger, "events", None) or []) if getattr(e, "invoked", False)]
        last = invoked[-1] if invoked else None
        # The failed call, only when it is the newest call and it did not
        # succeed: a stop the harness decided after successful calls names
        # none, and an earlier call of another seat or role is never named.
        event = None
        if (cls != "stop_rule" and call and last is not None and str(getattr(last, "task", "")) == str(task)
                and (getattr(last, "role", "") or "").split(":")[0] == role.split(":")[0]
                and seat in (getattr(last, "requested_model", None), getattr(last, "canonical_model", None))
                and getattr(last, "outcome", None) not in (None, "ok")):
            event = last
        # An unknown-usage stop is caused by the call(s) whose settlement
        # carried no usage, as the budget named them, never by the admission
        # it later refused (root on 113f4e1, 6549956979 item 3): each cause is
        # bound to its own ledger row by the operation id both carry; the
        # refused admission is kept apart and never counted as a call.
        causes, stopped = [], None
        if cls == "unknown_usage" and budget is not None:
            ops = [op for op in (budget.snapshot().get("unknown_usage_operations") or []) if isinstance(op, dict)]
            rows = self._bind_unknown_causes(ops, invoked)
            for op, row in zip(ops, rows, strict=True):
                causes.append(dict(operation=op.get("opid"), task=op.get("task"), role=op.get("role"),
                                   seat=op.get("seat"), editing=bool(op.get("editing")),
                                   call=recovery.call_cost(row), event=row))
        if causes:
            stopped = (dict(task=str(task), role=role, seat=seat, invoked=False, why=name) if call else None)
            first = next((c for c in causes if c["event"] is not None), causes[0])
            event = first["event"]
            task = str(first.get("task") or task)
            role = str((getattr(event, "role", None) if event is not None else None) or first.get("role") or role)
            seat = ((getattr(event, "requested_model", None) if event is not None else None)
                    or first.get("seat") or seat)
        record = self._recovery_open(SimpleNamespace(task_id=task), role=role, seat=seat, kind="run",
                                     outcome_class=cls, question=f"the run ended on {name}", event=event, exc=exc,
                                     observe=event is not None and bool(causes))
        if causes:
            record["causes"] = [{k: v for k, v in c.items() if k != "event"} for c in causes]
            if stopped is not None:
                record["stopped_call"] = stopped
        record["eligible"] = ["reconcile", "stop"] if cls in ("unknown_usage", "provider_timeout", "uncertain_write",
                                                              "unknown") else ["stop"]
        record["decision"] = ("stop: protective" if cls in recovery.PROTECTIVE else
                              "reconcile: the call's usage or effect is unknown; an operator reconciles it from the "
                              "retained evidence before any resume" if "reconcile" in record["eligible"] else
                              "stop: the run's own stop rule")
        self._recovery_result(record, f"stopped: {name}")

    def _complete_judgment(self, spec, peer: str, kind: str, raw: str, gaps: List[str], *, role: str,
                           prompt: str, task: Optional[TaskMemory] = None, outcome=None, context: str = "",
                           recovery_record: Optional[dict] = None, review_source=None) -> Optional[str]:
        """One same-seat judgment completion for an ARRIVED reply that is not
        a judgment (strategy reset, 2026-10-16). Returns the seat's new reply,
        or None when nothing new may be relied on.

        The same seat, the same existing role (``collaborator`` or
        ``recheck``: its turn cap, read-only source copy, scratch and privacy
        rules unchanged); the original prompt again, with the raw reply and
        what it lacked. The binding (``_judgment_binding``) is taken before
        the claim, persisted with it, and compared before the call and again
        before the reply is relied on: any change voids the answer and the
        claim stays spent. The claim is never refunded. A cap or a refusal by
        the finish hold gives nothing, as the same stop would on the review
        call; a refusal by the seat, and every other failure, is recorded and
        then raised exactly as the review call's own would be: nothing works
        around it. Whether the reply is a judgment is decided by the caller's
        strict reader (``_settle_completion``), never here."""
        binding = self._judgment_binding(spec, peer, role)
        # The source the completion is bound to: every attempt it records,
        # a void or a stop included, names this one (root's review, L2).
        pre = binding.get("source")
        record = dict(task=spec.task_id, seat=peer, kind=kind, role=role, gaps=list(gaps), binding=binding,
                      claimed=False, outcome="", judgment="")
        raw_ref = self.store.put(raw or "(no reply: the call stopped before answering)",
                                 kind=f"judgment-raw:{kind}:{peer}", author=peer)
        record["raw_artifact"] = raw_ref.id
        if recovery_record is not None:
            record["recovery"] = recovery_record["id"]
        self.judgment_completions.append(record)
        limit = int(getattr(self.config, "max_judgment_completions", 0) or 0)
        if limit <= 0 or not self._claim_judgment(limit, dict(task=spec.task_id, seat=peer, kind=kind, role=role,
                                                                identity=binding["identity"],
                                                                source=binding["source"])):
            record["outcome"] = "not attempted: " + ("turned off" if limit <= 0 else "the job's allowance is spent")
            self._note(f"{spec.task_id}: {peer}'s {kind} gave no valid verdict; completion {record['outcome']}")
            self._recovery_attempt(recovery_record, "complete_judgment", record["outcome"], source=pre)
            return None
        record["claimed"] = True
        self.store.put(json.dumps(record), kind="judgment-completion-intent", author="harness")
        if task is not None:
            task.keep(raw, kind=f"{kind}-incomplete:{peer}", author=peer)
        if self._judgment_binding(spec, peer, role) != binding:
            record["outcome"] = "void before the call: the bound identity changed"
            self._note(f"{spec.task_id}: {peer}'s {kind} completion {record['outcome']}")
            self._recovery_attempt(recovery_record, "complete_judgment", record["outcome"], source=pre)
            return None
        missing = "; ".join(gaps)
        # The previous reply is never repeated: it is model output, kept on
        # the record (``raw_artifact``), and repeating it would promote
        # unfiltered text into the prompt (root's context audit on 1dbbf9f,
        # L2). What the seat is told comes from harness facts only.
        ask = (context or ("Your previous reply on this same work arrived without a valid verdict in the "
                           "required form; it is kept on the record unchanged and is not repeated here.\n\n"))
        ask += (f"It lacked: {missing}. The work and the source are unchanged. Give your judgment now in the "
                "required form below; judge the work itself.\n\n")
        try:
            with invocation(spec.task_id, role, review_source=review_source):
                reply = self._invoke_model(peer, ask + prompt)
        except TurnLimitReached:
            record["outcome"] = "capped: no verdict"
        except FinishReserved as exc:
            self._finish_refused(spec, f"{kind} judgment completion by {peer}", exc, outcome=outcome)
            record["outcome"] = "refused to keep the finish"
        except BaseException as exc:
            # A refusal by the seat, transport, integrity or anything else:
            # recorded, then raised as the review call's own failure is. The
            # message can carry vendor or model text: it is kept as a private
            # artifact and only its type is exported (root's review of
            # a7f9c6c, L1).
            record["outcome"] = f"stopped: {type(exc).__name__}"
            record["error_artifact"] = self.store.put(str(exc), kind=f"judgment-completion-error:{kind}:{peer}",
                                                      author="harness").id
            self.store.put(json.dumps(record), kind="judgment-completion-stopped", author="harness")
            self._recovery_attempt(recovery_record, "complete_judgment", record["outcome"], task=spec.task_id,
                                   role=role, peer=peer, source=pre)
            self._recovery_result(recovery_record, f"stopped: {type(exc).__name__}")
            raise
        else:
            after = self._judgment_binding(spec, peer, role)
            if after != binding:
                changed = sorted(k for k in binding if k != "identity" and binding[k] != after.get(k))
                record["outcome"] = f"void: the bound identity changed during the completion ({', '.join(changed)})"
            else:
                ref = self.store.put(reply, kind=f"judgment-completion:{kind}:{peer}", author=peer)
                record.update(outcome="answered", reply_artifact=ref.id)
                self._recovery_attempt(recovery_record, "complete_judgment", "answered", task=spec.task_id,
                                       role=role, peer=peer, source=pre)
                return reply
        self._recovery_attempt(recovery_record, "complete_judgment", record["outcome"], task=spec.task_id,
                               role=role, peer=peer, source=pre)
        self._note(f"{spec.task_id}: {peer}'s {kind} judgment completion gave nothing: {record['outcome']}")
        return None

    def _settle_completion(self, spec, peer: str, kind: str, valid: bool, *, outcome=None) -> None:
        """The strict reader's decision on the answered completion: only a
        valid judgment is recorded as completed; anything else stays
        unjudged, its open finding recorded by the ordinary path."""
        record = next((r for r in reversed(self.judgment_completions)
                       if r["task"] == spec.task_id and r["seat"] == peer and r["kind"] == kind), None)
        if record is None or record.get("outcome") != "answered":
            return
        record["judgment"] = "valid" if valid else "still no valid verdict"
        target = outcome or self._outcome
        if valid and target is not None:
            fact = target.note("unverified", f"{peer}'s {kind} arrived without a valid verdict; the same seat "
                                             f"completed its judgment once (raw reply {record['raw_artifact']})",
                               terminal=False)
            fact.recovered = True
        self._note(f"{spec.task_id}: {peer}'s {kind} completion "
                   + ("is a valid judgment" if valid else "still gave no valid verdict"))

    def _judgment_gap(self, spec, peer: str, kind: str, why: str) -> None:
        """A mandatory judgment that ended without a valid verdict and was not
        completed: kept for the obligation view (it blocks completion through
        its open finding, not through this list)."""
        task_id = getattr(spec, "task_id", "run")
        for gap in self.judgment_gaps:
            if (gap["task"], gap["seat"], gap["kind"]) == (task_id, peer, kind):
                gap["why"] = f"{gap['why']}; {why}" if why not in gap["why"] else gap["why"]
                return
        self.judgment_gaps.append(dict(task=task_id, seat=peer, kind=kind, why=why))
