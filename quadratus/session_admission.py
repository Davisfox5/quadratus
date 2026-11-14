"""Admission and finish reservation for the session engine, as a mixin of
Session: the stages a task or repair is expected to spend, the walls they
are measured against, admitting a plan, a batch or a repair, the slots a
dispatch holds, and the terminal slot kept for the finish.

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
from typing import List, Optional

from . import delivery, obligations
from .run_budget import FinishReserved
from .session_types import JobStoreUnavailable


class AdmissionMixin:
    """Whether the run can pay for what it is about to start, and what it keeps back.

    Session inherits this; nothing here is used on its own.
    """

    def _slot_event(self, event: str, tasks, used: int, *, why: str = "", how: str = "") -> None:
        """One ``slot_ledger`` entry. ``charged``: admitted work dispatched
        (a serial task or a parallel wave), charged once at dispatch whatever
        followed it (a clean close, a cap, a failure, a stop raised inside
        it). ``sent_back``: a planner reply the intake refused before any
        lead call. ``done_sent_back``: a DONE the completion decision
        reopened. ``not_started``: a task or batch whose route was refused
        at dispatch with nothing written. Only ``charged`` moves
        ``_slots_used``; every entry records the count as it stands after
        the event, so the ledger and the count agree at every point. The
        uncharged events are bounded by the correction allowance
        (``max_requirement_reopens`` → ``RunStalled``), the completion
        decision's reopen allowance, the call, token and wall budgets and
        the repeated-task stall; none of them frees planning."""
        total = getattr(self, "_max_tasks", None)
        entry = dict(event=event, tasks=[str(t) for t in tasks], slots_used=int(used),
                     max_tasks=int(total) if total else None)
        if how:
            entry["how"] = how
        if why:
            entry["why"] = str(why)[:200]
        self.slot_ledger.append(entry)

    def _renew_audit_hold(self, specs, *, how: str) -> None:
        """Before a writer is admitted after the final audit was consumed:
        hold the audit's capacity again, durably, so the repair plus the
        required finishing work must fit what is left (root's correction on
        0c5c6f1, C1). The DONE decision consumed the hold when it asked the
        final audit (``_audit_requirements`` under ``finishing("audit")``);
        a repair that follows makes a second audit necessary, and the
        budget's own admission (``_admit`` counting the held stages) then
        refuses a writer the remainder cannot pay through to it. Bounded by
        ``max_requirement_reopens``, the allowance that bounds the DONE
        reopens that make a renewal necessary; a renewal that bound refuses
        refuses the writer (``FinishReserved``, infeasible: the task is not
        started). Nothing is reset and no allowance grows. Tied to the
        source as it stands when the work is admitted, on the record."""
        budget = self._budget()
        if (budget is None or not hasattr(budget, "renew_finish") or not self.config.requirements_ledger
                or self._explicit_tasks is not None or not (self.project and self.config.allow_writes)):
            return
        tasks = [s.task_id for s in specs]
        # A review-only task is not excluded: one that re-covers a NOT MET
        # requirement is a dispatched route whose close makes the next
        # DONE's audit necessary (workflow refutation of this batch: it was
        # admitted with no hold, spent its calls, and the second audit was
        # then unpayable).
        snap = budget.snapshot()
        fin = snap.get("finish_protection") or {}
        if "audit" not in (fin.get("started") or ()) or "audit" in (fin.get("pending") or ()):
            return
        why = f"{how} {', '.join(tasks)} admitted after the final audit was consumed"
        renewed = budget.renew_finish("audit", why=why, limit=self.config.max_requirement_reopens)
        self.finish_renewals.append(dict(tasks=tasks, how=how, renewed=bool(renewed),
                                         at_attempt=snap.get("reserved_attempts"),
                                         reopens=int(getattr(self, "_requirement_reopens", 0) or 0),
                                         source=self._source_identity()))
        if not renewed:
            exc = FinishReserved(f"{', '.join(tasks)}: a second final audit cannot be held again (the renewal "
                                 f"allowance of {self.config.max_requirement_reopens} is spent); the repair is "
                                 "not started")
            exc.infeasible = True
            raise exc
        self._note(f"holding audit capacity again before {how} {', '.join(tasks)}: the final audit was consumed "
                   "and this work makes a second one necessary")

    def _release_renewal(self, tasks, why: str) -> None:
        """Release the audit hold a renewal made for ``tasks`` when that work
        is not started (second-round refutation of root's correction on
        0c5c6f1, C1: the renewed hold outlived the refused repair and the
        cap's goal question was refused against it). The budget is put back
        where it stood before the renewal; the renewal stays counted
        against its bound, so no allowance is added, and a later admitted
        repair renews again within the same bound."""
        entry = next((e for e in reversed(self.finish_renewals) if e.get("tasks") == list(tasks)), None)
        if entry is None or not entry.get("renewed") or entry.get("released"):
            return
        budget = self._budget()
        if budget is None or not hasattr(budget, "release_finish") or "audit" not in budget.finish_pending():
            return
        budget.release_finish("audit", why)
        entry["released"] = str(why)[:200]

    def _refuse_admission_record(self, specs, exc, *, used: int) -> None:
        """A dispatch the store refused before its first effect (its
        admission, or the authority commit before it): nothing ran, so no
        slot is charged, each task is recorded not started with its typed
        outcome saying so, an explicit list's 'ran' entry for it is taken
        back, and the ledger names the refusal. The caller re-raises."""
        tasks = [s.task_id for s in specs]
        for spec in specs:
            outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
            lead = str((outcome.lead if outcome is not None else "") or spec.lead or "")
            self.not_started.append(dict(task=spec.task_id, lead=lead, description=(spec.description or "")[:300],
                                         detail=str(exc)[:300]))
            if outcome is not None:
                outcome.closed_as = "not_started"
                outcome.note("unverified", f"NOT RUN: {spec.task_id} was not started; the job store refused its "
                             f"admission and nothing ran ({str(exc)[:200]})", terminal=False)
        if self._explicit_tasks is not None and self.explicit.get("ran"):
            last = self.explicit["ran"][-1]
            if last.get("task") in tasks:
                self.explicit["ran"].pop()
        self._slot_event("not_started", tasks, used, why=str(exc))

    def _admit_dispatch(self, specs, *, how: str, used: int) -> dict:
        """Publish the admission of ``specs`` to a slot before their first
        effect (root's correction on 0c5c6f1, C2; addendum 6537675374): the
        task ids, the slot and count, the committed authority digest the
        work runs under and the attempt count at admission, committed to
        the job store as ``task-admissions`` in the same lineage the
        operations use. A store that cannot commit it stops the run here
        with nothing started. The charge itself stays where it was (after
        dispatch, ``_slots_used`` keeps its pre-task convention); a resumed
        episode that finds an admission committed after its checkpoint
        counts it as spent (``_count_interrupted_admissions``)."""
        tasks = [s.task_id for s in specs]
        try:
            authority = self._authority_before(f"admission of {', '.join(tasks)}")
        except JobStoreUnavailable as exc:
            exc.admission_refused = True
            raise
        budget = self._budget()
        total = getattr(self, "_max_tasks", None)
        entry = dict(admission=f"A{len(self.task_admissions) + 1}", tasks=tasks, how=how, slot=int(used) + 1,
                     slots=len(tasks), max_tasks=int(total) if total else None, authority=authority,
                     at_attempt=(budget.snapshot().get("reserved_attempts") if budget is not None else None),
                     descriptions=[str(getattr(s, "description", "") or "")[:200] for s in specs],
                     covers=list(getattr(self, "_current_covers", []) or []) if how == "task" else [],
                     outcome=None)
        self.task_admissions.append(entry)
        store = self.config.job_store
        if store is not None:
            try:
                store.publish({"task-admissions": [dict(e) for e in self.task_admissions]})
            except Exception as exc:  # noqa: BLE001 -- the store is latched; nothing starts without the record
                self.task_admissions.pop()
                refused = JobStoreUnavailable(f"{how} {', '.join(tasks)} not started: the job store could not "
                                              f"publish its admission ({type(exc).__name__})")
                # Read by the dispatch loop: nothing of the task ran, so the
                # slot is not charged and the record says not started.
                refused.admission_refused = True
                raise refused from exc
        return entry

    def _count_interrupted_admissions(self, used: int) -> int:
        """Admissions the resume found committed in the parent's store and
        absent from its checkpoint (admitted after the quiescent point and
        interrupted before any effect settled): each is counted as spent
        now, on the ledger and the admissions record, never dropped and
        never re-planned as fresh capacity. Conservative: fewer slots
        remain. Returns the count after them."""
        for entry in list(self._interrupted_admissions):
            slots = int(entry.get("slots") or len(entry.get("tasks") or ()) or 1)
            used += slots
            self._slots_used = used
            kept = dict(entry, outcome="interrupted: admitted in an earlier episode and interrupted before its "
                                       "first effect settled; counted as spent")
            self.task_admissions.append(kept)
            self._slot_event("charged", entry.get("tasks") or [], used,
                             how=f"{entry.get('how') or 'task'} admitted in an earlier episode and interrupted",
                             why=str(entry.get("admission") or ""))
            self._note(f"{', '.join(entry.get('tasks') or ())}: admitted in an earlier episode and interrupted "
                       f"before closing; its {slots} slot(s) count as spent, and any change it left is in the "
                       "project, unreviewed")
        self._interrupted_admissions = []
        return used

    def _slots_note(self) -> str:
        """The job's task allowance as the planner must plan against it: the
        slots left, counting the task about to be named, and the requirements
        no task has covered yet (Step 2 medium job on 3e59970: the planner was
        never told the cap of 3, spent it on storage and HTTP, and the CLI and
        docs requirements were never planned). Facts only: the cap and the
        completion gates are unchanged, and size stays advisory."""
        total = getattr(self, "_max_tasks", None)
        if not total or self._explicit_tasks is not None:
            return ""
        left = max(0, total - int(getattr(self, "_slots_used", 0) or 0))
        text = (f"\n\nTask slots: {left} of {total} remain for this job, counting the task you name now. "
                "A slot is spent when a task is dispatched, one per task in a parallel batch, however the "
                "task ends. A task or batch sent back before any lead call spends no slot and counts against "
                f"the correction allowance of {self.config.max_requirement_reopens}, past which the run "
                f"stalls; a DONE sent back spends no slot and is sent back at most "
                f"{self.config.max_requirement_reopens} times, after which the run ends incomplete.")
        interrupted = [e for e in self.task_admissions if str(e.get("outcome") or "").startswith("interrupted")]
        if interrupted:
            text += (" Admitted in an earlier episode and interrupted before closing: "
                     + ", ".join(t for e in interrupted for t in (e.get("tasks") or ()))
                     + " (slot spent; any change it left is in the project, unreviewed; name its remaining work "
                       "as new work if it is still owed).")
        text += self._allowance_note()
        ledger = self.memory.ledger
        if self.config.requirements_ledger and ledger.requirements:
            status = ledger.requirement_status
            open_ids = [rid for rid in ledger.requirements
                        if not str(status.get(rid, "")).startswith(("covered", "met"))]
            if open_ids:
                text += (f" Requirements no task has covered yet: {', '.join(open_ids)}. Plan so the "
                         "remaining slots can reach every one of them, declaring a task at its real "
                         "size where that is what covers them (the size figure is a guide and stops "
                         "nothing); if they cannot, say which will be left open rather than spend the "
                         "slots unaware.")
        return text

    def _allowance_note(self) -> str:
        """The job's allowance as the admission will charge the next task,
        for the planner: ordinary calls, tokens and wall left after the held
        finish, the exposure classes a route is costed in, and what a
        reviewed task's known route costs before and in the last slot.
        Facts from the budget's own snapshot, nothing promised: reported
        usage can be below an exposure, admission still charges the
        exposure, and a route the allowance cannot pay is not started."""
        budget = self._budget()
        if budget is None or not hasattr(budget, "snapshot"):
            return ""
        try:
            snap = budget.snapshot()
        except Exception:  # noqa: BLE001 -- a note never fails planning
            return ""
        limits, fin = snap.get("limits") or {}, snap.get("finish_protection") or {}
        held = len(fin.get("pending") or [])
        reserve = int(limits.get("reserve_tokens_per_call") or 0)
        editing = int(fin.get("editing_exposure") or 0) or reserve
        calls = max(0, int(limits.get("max_calls") or 0) - int(snap.get("reserved_attempts") or 0) - held)
        tokens = max(0, int(limits.get("max_reported_tokens") or 0) - int(snap.get("reported_tokens") or 0)
                     - held * reserve)
        wall = max(0.0, float(limits.get("wall_seconds") or 0) - float(snap.get("elapsed_seconds") or 0)
                   - float(fin.get("held_wall_seconds") or 0))
        if not reserve:
            return (f" Allowance after the held finish ({held} stage(s) held): {calls} call(s), {wall:.0f} s; "
                    "no token reserve is configured, so tokens are not counted at admission.")
        return (f" Allowance after the held finish ({held} stage(s) held): {calls} ordinary call(s), "
                f"{tokens:,} tokens, {wall:.0f} s. Admission charges exposures, not reported usage: an editing "
                f"call {editing:,} tokens, any other call {reserve:,}. A reviewed task's known route is one "
                f"editing call plus one reserve call per required review; its record is written by the "
                f"harness and costs no call. A correction a review selects adds an editing call and one "
                f"reserve call per recheck. A route the allowance cannot pay is not started, so name a task "
                f"whose route fits, or say which requirements will be left open.")

    def _budget(self):
        """The run's shared budget, when the transport carries one (the
        session holds the fleet's bound ``invoke``)."""
        owner = getattr(self.invoke, "__self__", self.invoke)
        return getattr(owner, "run_budget", None)

    def _protect_finish(self) -> None:
        """Hold the finishing judgments' capacity from ordinary work for this
        episode (Davis, 2026-10-16; run_budget.FINISH_STAGES): the cap's goal
        question, unless an explicit list ends without one or a resumed job
        already holds its answer, then the requirements audit while the
        ledger is on and no final audit is on record. Recomputed per episode
        from the session's own record, so a resume holds nothing twice and
        never holds a judgment already made; nothing is added to the job's
        allowance and no limit changes."""
        budget = self._budget()
        if budget is None or not hasattr(budget, "protect_finish"):
            return
        stages = []
        if self._explicit_tasks is None and self._cap_goal is None:
            stages.append("goal")
        if self.config.requirements_ledger and not self._final_audit_current():
            stages.append("audit")
        budget.protect_finish(stages)
        if stages:
            self._note("holding " + " and ".join(stages) + " capacity from ordinary calls")

    def _finish_refused(self, spec, what: str, exc, *, outcome=None) -> None:
        """Record an ordinary call refused to keep the finish. The work it
        would have done stays undone: the caller keeps every finding open."""
        task_id = getattr(spec, "task_id", None) or "run"
        self.finish_refusals.append(dict(task=task_id, what=what, detail=str(exc)[:300]))
        target = outcome or (self._outcome if spec is not None else None)
        text = f"{what} not run: ordinary calls stopped to keep the finish ({str(exc)[:200]})"
        if target is not None:
            target.note("unverified", text, terminal=False)
        else:
            self.run_outcome.note("unverified", text, terminal=False)
        self._note(f"{task_id}: {text}")

    def _mandatory_stages(self, spec, collaborators=(), *, security: bool = False) -> List[str]:
        """The model stages this task cannot close without, known before any
        call: the lead's draft and each required review; a design review its
        contract requires; for a security task, the worker's draft and the
        cross-vendor verification. Revisions, fixes and recaptures are not
        known in advance and are not counted."""
        if security:
            return ["security worker draft", "security verification"]
        stages = ["lead draft"] + [f"review by {peer}" for peer in collaborators]
        if self._design_review_ahead(spec):
            stages.append("design review")
        return stages

    def _design_review_ahead(self, spec) -> bool:
        """Whether the task's contract requires the cross-vendor design
        review (``_check_design``, after the last edit and the gate): a
        known judgment every repair before it still owes."""
        contract = getattr(self, "_contract", None)
        required = getattr(contract, "required", None) if getattr(contract, "task_id", None) == spec.task_id else None
        return bool(required is not None and getattr(required, "design_review", False))

    def _known_remainder(self, spec, *, after: str) -> list:
        """What the task still owes after a point on its route, known before
        the editor being admitted is called, in the budget's exposure
        classes: after the draft (``after="draft"``), each required review;
        after the reviews (``"review"``), the required design review; at
        every point, the model close-out where one is made. A correction's
        own rechecks follow its findings and are the caller's to name;
        nothing is listed twice, and the held finish is counted by the
        admission itself. Root's control on fef1e27: a fix cycle admitted
        without the task's known 200,000 close-out ran, resolved the
        finding, and the job stopped FinishReserved at the close-out with
        the goal question and audit never asked."""
        rows = []
        if after == "draft":
            rows.extend(delivery.PlanStage(f"review by {peer}", "reserve")
                        for peer in list(getattr(self, "_route_peers", None) or []))
        if after in ("draft", "review") and self._design_review_ahead(spec):
            rows.append(delivery.PlanStage("design review", "reserve"))
        if not self._closes_factually(spec):
            rows.append(delivery.PlanStage("close-out", "reserve"))
        rows.extend(delivery.PlanStage(f"resolution recheck by {peer}", "reserve")
                    for peer in self._resolution_reviewers(spec))
        # The planning call that follows is counted at dispatch
        # (_plan_stages) and for a batch, never against a mid-task repair:
        # a fix the allowance can pay is worth more than the planning call
        # it may leave unpaid, and the finish keeps its own hold.
        return rows

    def _measure_context(self, kind: str, body: str, *, task: Optional[str] = None, extra: str = "") -> None:
        """Record what one prompt carried, in characters by block: the
        completed-work block as sent (index) and as it would have been sent
        with previews, the goal and rules, the map, and the rest (the
        question or task text with its obligation and debt blocks). A
        comparison of renderings, never a token claim; a failure to measure
        is noted and never fails the call."""
        try:
            ledger = self.memory.ledger
            index = ledger.render(goal=self.memory.goal, recent=self.config.recent_entries, with_previews=False)
            full = ledger.render(goal=self.memory.goal, recent=self.config.recent_entries, with_previews=True)
            # With no question and no extra block the completed work is the
            # last block of both renders; everything before it is the goal,
            # the rules, the rulings and the requirements.
            marker = "## Completed work"
            head_chars = index.find(marker) if marker in index else len(index)
            record = dict(kind=kind, task=task or getattr(self._active_spec, "task_id", None),
                          prompt_chars=len(body), goal_and_rules_chars=max(0, head_chars),
                          completed_index_chars=max(0, len(index) - head_chars),
                          completed_preview_chars=max(0, len(full) - (full.find(marker) if marker in full else len(full))),
                          map_chars=len(extra or ""), entries=len(ledger.entries),
                          refs=sum(len(e.refs) for e in ledger.entries),
                          unit="characters of the rendered prompt; not tokens")
            record["rest_chars"] = max(0, record["prompt_chars"] - record["goal_and_rules_chars"]
                                       - record["completed_index_chars"] - record["map_chars"])
            self.prompt_measurements.append(record)
        except Exception as exc:  # noqa: BLE001 -- a measurement never fails a call
            self.prompt_measurements.append(dict(kind=kind, task=task, prompt_chars=len(body or ""),
                                                 error=f"{type(exc).__name__}: {str(exc)[:160]}"))

    def _last_planning_tokens(self) -> Optional[int]:
        """The reported tokens of the last planning call this episode (the
        delegation ledger's orchestrator event with known usage), or None."""
        ledger = getattr(self.config, "delegation_ledger", None)
        for event in reversed(list(getattr(ledger, "events", None) or [])):
            if not getattr(event, "invoked", False) or (event.role or "").split(":")[0] != "orchestrator":
                continue
            if event.input_tokens is None or event.output_tokens is None:
                continue
            return int(event.input_tokens) + int(event.output_tokens)
        return None

    def _planning_stages(self, *, slots: int = 1) -> list:
        """The planning call that follows a task (or a batch) when a slot is
        left after it: one reserve call, counted at the larger of the reserve
        and the last planning call's reported size (budget-aware admission,
        2026-10-28; controller-continuation-01's two post-audit planning
        calls reported 451,817 and 358,436 tokens against a 200,000 reserve,
        and the route they left room for could not be paid). An explicit
        list, a parallel child and a job committed to end make no planning
        call; neither does the final slot, whose finish is held already."""
        if self._explicit_tasks is not None or getattr(self, "_batch_child", False) or self._terminal_committed():
            return []
        total = getattr(self, "_max_tasks", None)
        if total and int(getattr(self, "_slots_used", 0) or 0) + slots >= int(total):
            return []
        budget = self._budget()
        if budget is None:
            return []
        snap = budget.snapshot()
        reserve = int((snap.get("limits") or {}).get("reserve_tokens_per_call") or 0)
        if not reserve:
            # No token reserve: the planning call is still a call, and while
            # a renewed audit hold is pending the repair's route must leave
            # room for the planner's DONE between it and that audit
            # (second-round refutation of root's correction on 0c5c6f1, C1:
            # at 11 calls the renewal admitted the repair and the DONE was
            # then refused). Only then, so zero-reserve admission elsewhere
            # is unchanged; counted as a call, tokens not counted.
            fin = snap.get("finish_protection") or {}
            renewed = ("audit" in (fin.get("pending") or ())
                       and any(r.get("stage") == "audit" for r in (fin.get("renewals") or ())))
            if not renewed:
                return []
            return [delivery.PlanStage("next planning call", "reserve")]
        measured = self._last_planning_tokens()
        return [delivery.PlanStage("next planning call", "reserve", tokens=max(reserve, measured or 0))]

    def _route_wall(self, spec):
        """The wall time of one run of the task's gate after an edit, as
        ``(wall_extra, wall_known)``: none and nothing to know without a
        gate; else the measured run, or 0.0 reported as unmeasured."""
        if self._route_gate(spec) is None:
            return 0.0, None
        extra = self._gate_wall(spec)
        return extra, extra > 0

    def _plan_stages(self, spec, stages, *, collaborators=(), security: bool = False,
                     post_merge: bool = False, slots: int = 1) -> list:
        """The selected finite route of one task as plan stages in the
        budget's exposure classes (delivery.PlanStage), admission policy
        admission/2: the stages known before any call (the lead's draft,
        each required review, a required design review or security
        verification), its model close-out where one is made, and the
        finishing judgments still held. A correction (the revision with the
        write grant and one recheck per reviewer whose finding it answers)
        is not on the route: it exists only once a review has made a
        finding, and is admitted whole at that moment, before its editor
        writes, together with everything the task still owes after it (the
        required design review, the model close-out: ``_known_remainder``;
        ``_fix_cycle_stages``, every cycle, the first included);
        unpayable, no editor is called and the findings stay open as the
        reviewer wrote them, so the task closes with them and the run ends
        incomplete and says so. The same holds for a gate fix after a
        failed check and for a merged child's post-merge recheck. Costing
        these hypothetical calls in advance at their exposure ceilings
        refused every reviewed task under the default bound (the fresh
        end-to-end job on 21b342d: a 1,600,000 route against the
        1,473,864 left after a 126,136 intake and the 400,000 finish hold,
        nothing written); the known stages still must be paid whole, no
        review is dropped to fit, and the finish keeps its hold. Nothing is
        cheaper than its class: a call holding the write grant is at the
        editing ceiling."""
        writes = bool(self._writes())
        rows = []
        for name in stages:
            editing = writes and name in ("lead draft", "security worker draft")
            rows.append(delivery.PlanStage(name, "editing" if editing else "reserve", editing=editing))
        if not self._closes_factually(spec, slots=slots):
            rows.append(delivery.PlanStage("close-out", "reserve"))
        rows.extend(delivery.PlanStage(f"resolution recheck by {peer}", "reserve")
                    for peer in self._resolution_reviewers(spec))
        if not post_merge:
            rows.extend(self._planning_stages(slots=slots))
        budget = self._budget()
        pending = list(budget.finish_pending()) if budget is not None and hasattr(budget, "finish_pending") else []
        rows.extend(delivery.PlanStage(stage, "reserve", finishing=True) for stage in pending)
        return rows

    def _route_gate(self, spec):
        """The gate the task's route runs, read without side effects."""
        if not self.project or self.config.integration_gate is None:
            return None
        if getattr(self, "_batch_child", False):
            return None
        return self._gate_for(spec)

    def _closes_factually(self, spec, *, slots: int = 1) -> bool:
        """Whether this task's close-out is written by the harness rather
        than asked of a model (``_task_close_out``): always, since
        2026-10-21. Kept as the one question the route planners ask, so no
        plan stage, remainder or exposure is ever reserved for a model
        close-out again."""
        return True

    def _gate_repair_stages(self, spec, gate, kind: str) -> list:
        """The route a failed check has selected: one gate fix holding the
        write grant, then what the task still owes (``_known_remainder``).
        A cheap pre-review gate (``kind`` subset) leaves the reviews ahead;
        either gate leaves the required design review and the close-out
        where a model makes one. The check that judges the repair is wall
        time, counted by ``_gate_wall_of``."""
        rows = [delivery.PlanStage("gate fix", "editing" if self._writes() else "reserve", editing=self._writes())]
        reviewer = self._comparison_reviewer(getattr(self._outcome, "lead", None)) if self._writes() else None
        if reviewer and self._generated_tests():
            # The reviewer's comparison a repair of a generated test draws
            # (root review of b49a0cb, R4): on the route, reserved.
            rows.append(delivery.PlanStage(f"generated test comparison by {reviewer}", "reserve"))
        after_merge = getattr(self, "_route_after_merge", None)
        if after_merge is not None:
            # The merge gate: no review and no model close-out follow it; the
            # merged children's post-merge rechecks and capped final reviews
            # do, once it passes.
            rows.extend(delivery.PlanStage(f"post-merge recheck by {peer}", "reserve")
                        for peer in after_merge.get("rechecks") or ())
            rows.extend(delivery.PlanStage(f"capped final review of {task_id}", "reserve")
                        for task_id in after_merge.get("capped") or ())
            return rows
        peers = list(getattr(self, "_route_peers", None) or [])
        # After a pre-review subset gate the reviews ahead are known (the
        # correction they may select is admitted when they do, admission/2);
        # after either gate the required design review and the model
        # close-out are, and every one is on the repair's route.
        rows.extend(self._known_remainder(spec, after="draft" if kind == "subset" and peers else "review"))
        return rows

    def _gate_repair_wall(self, spec, gate, kind: str):
        """The check wall time the gate's repair route runs: the check that
        judges the fix (one run of ``gate``) and, after a pre-review subset
        gate whose reviews and revision lie ahead, the full gate that runs
        after the revision. Counted where measured this episode; an
        unmeasured run is reported as such, never guessed."""
        extra = self._gate_wall_of(gate)
        known = extra > 0
        if kind == "subset" and (getattr(self, "_route_peers", None) or []):
            full = self._gate_wall(spec)
            extra += full
            known = known and full > 0
        return extra, known

    def _admit_repair(self, spec, what: str, scope: str, rows: list, *, after: str,
                      wall_extra: float = 0.0, wall_known: Optional[bool] = None) -> bool:
        """Admit a repair editor's route whole before it writes: ``rows``
        is the route a failed check, a rejecting verifier or a broken render
        selected (the fix holding the write grant, the judgment that follows
        it, what the task still owes). A refused route is recorded
        (``_finish_refused``), no editor is called and the failure stands as
        found; returns False. With no budget there is nothing to admit."""
        if self._budget() is None:
            return True
        verdict = self._admit_plan(spec.task_id, rows, scope=scope, wall_extra=wall_extra, wall_known=wall_known)
        if verdict.feasible:
            return True
        self._finish_refused(spec, what, FinishReserved(
            f"{spec.task_id}: the repair route after the failed {after} cannot be paid whole: {verdict.detail}"))
        return False

    def _security_repair_stages(self, spec, verifier: str) -> list:
        """The route a rejecting security verifier selects: one fix holding
        the write grant, the verifier's second look, the close-out where a
        model makes one. The check between them is wall time."""
        writes = bool(self._writes())
        rows = [delivery.PlanStage("security fix", "editing" if writes else "reserve", editing=writes),
                delivery.PlanStage(f"security verification by {verifier}", "reserve")]
        rows.extend(self._known_remainder(spec, after="design"))
        return rows

    def _design_repair_stages(self, spec, reviewer: Optional[str] = None) -> list:
        """The route a broken render or a blocking design review selects:
        one fix holding the write grant, the same reviewer's judgment of the
        new renders where a reviewer blocked (else the required design
        review of the repaired capture, still ahead), the close-out where a
        model makes one. The check and the recapture are wall time; the
        recapture is unmeasured, so the route's wall is reported unknown."""
        writes = bool(self._writes())
        rows = [delivery.PlanStage("design fix", "editing" if writes else "reserve", editing=writes)]
        if reviewer:
            rows.append(delivery.PlanStage(f"design review by {reviewer}", "reserve"))
        rows.extend(self._known_remainder(spec, after="design" if reviewer else "review"))
        return rows

    def _design_fix_edit(self, spec, task, record: dict, lead: str, prompt: str, *,
                         reviewer: Optional[str] = None) -> bool:
        """One design fix, bought whole before the editor writes
        (``_admit_repair``); a refusal, at admission or at the call, is
        recorded on the design record and the render's problem stands as
        found. Returns whether the fix was made."""
        if not self._admit_repair(spec, "design fix", "design-repair", self._design_repair_stages(spec, reviewer),
                                  after="render", wall_extra=self._gate_wall(spec), wall_known=False):
            record["fix_refused"] = self.finish_refusals[-1]["detail"] if self.finish_refusals else "refused"
            return False
        try:
            self._edit(lead, prompt, role="design-fix", capped=(spec, task))
        except FinishReserved as exc:
            self._finish_refused(spec, "design fix", exc)
            record["fix_refused"] = str(exc)[:300]
            return False
        return True

    def _fix_cycle_stages(self, spec, unresolved, *, editor: bool = True, correction: bool = False,
                          binding: bool = False) -> list:
        """One fix cycle: a revision with the write grant, one recheck per
        reviewer whose findings it answers, and what the task still owes
        after its reviews (the required design review, the model close-out:
        ``_known_remainder``). ``correction`` adds the planner's one
        scenario-correction call; ``binding`` the lead's one declaration-only
        call for unbound scenarios (cycle01); ``editor`` False is a round
        whose every finding was routed to the planner or the harness, so no
        editing call is costed or made (workflow repair, 2026-10-24)."""
        writes = bool(self._writes())
        rows = []
        if correction:
            rows.append(delivery.PlanStage("scenario correction", "reserve"))
        if binding:
            rows.append(delivery.PlanStage("proof binding declaration", "reserve"))
        if editor:
            rows.append(delivery.PlanStage("fix revision", "editing" if writes else "reserve", editing=writes))
        rows.extend(delivery.PlanStage(f"recheck by {peer}", "reserve")
                    for peer in sorted({peer for peer, _ in unresolved}))
        rows.extend(self._known_remainder(spec, after="review"))
        return rows

    def _admit_plan(self, task_id: str, rows: list, *, scope: str = "full", wall_extra: float = 0.0,
                    wall_known: Optional[bool] = None, record: bool = True) -> "delivery.PlanVerdict":
        """Cost ``rows`` against what the budget has left after the held
        finish (delivery.admit_plan), and record the decision. ``scope`` is
        ``mandatory`` (the stages the task cannot close without), ``full``
        (the selected route: the correction, the gate fix and the close-out
        too), ``fix-cycle`` (a further cycle before it starts) or a batch's
        name; a shortfall on any of them refuses the work before its first
        call (``_refuse_unpayable_dispatch``, ``_admit_batch``). ``wall_extra``
        is the wall time the route's own checks may take."""
        budget = self._budget()
        snap = budget.snapshot()
        limits, fin = snap["limits"], snap.get("finish_protection") or {}
        held = len(fin.get("pending") or [])
        reserve = int(limits.get("reserve_tokens_per_call") or 0)
        editing = int(fin.get("editing_exposure") or 0) or reserve
        finishing = [r for r in rows if r.finishing]
        ordinary = [r for r in rows if not r.finishing]
        # The held finish is counted once: its calls and reserve are already
        # withheld from what is left, so only the ordinary stages are asked
        # against that remainder, and the finishing stages are listed for
        # the record.
        verdict = delivery.admit_plan(
            ordinary, calls_left=max(0, limits["max_calls"] - snap["reserved_attempts"] - held),
            tokens_left=max(0, limits["max_reported_tokens"] - snap["reported_tokens"] - held * reserve),
            wall_left=max(0.0, limits["wall_seconds"] - snap["elapsed_seconds"]
                          - float(fin.get("held_wall_seconds") or 0)),
            # The finish's wall hold is already withheld from what is left; an
            # ordinary call gets the time left, so no per-stage wall figure is
            # counted beyond needing some time of its own.
            exposures=dict(editing=editing, reserve=reserve), wall_per_stage=0.0, wall_extra=wall_extra)
        detail = verdict.detail
        if wall_known is False:
            # An honest unknown: the route's check has no measured wall time
            # this episode, so none is counted and none is claimed.
            detail += " (the route's check wall time is unmeasured; not counted)"
            verdict = replace(verdict, detail=detail)
        if not record:
            # A quote, not an admission (cycle03's planning quotes): the
            # caller keeps what it decided on.
            return verdict
        self.plan_admissions.append(dict(task=task_id, scope=scope, feasible=verdict.feasible, detail=detail,
                                         stages=list(verdict.stages), finishing=[r.name for r in finishing],
                                         short=dict(verdict.short), at_attempt=snap["reserved_attempts"],
                                         wall_extra=wall_extra, wall_known=wall_known,
                                         # The numbers behind the detail (cycle04), for a
                                         # stop that names a refused round.
                                         needed=dict(calls=verdict.calls_needed, tokens=verdict.tokens_needed),
                                         left=dict(calls=max(0, limits["max_calls"] - snap["reserved_attempts"] - held),
                                                   tokens=max(0, limits["max_reported_tokens"]
                                                              - snap["reported_tokens"] - held * reserve))))
        return verdict

    def _refuse_unpayable_dispatch(self, spec, stages, *, collaborators=(), security: bool = False) -> None:
        """Before any call of a serial task: what is left after the held
        finish must pay the task's whole remaining path, exactly counted in
        the budget's exposure classes (delivery.admit_plan), or the task
        cannot reach its judgments and is not started
        (``_settle_not_started``). Two deterministic candidates are costed
        (admission/3): the ordinary selected route, and when that cannot
        be paid, the same task, reviewers, gates, design and security
        stages closed by the harness's factual record (``_commit_terminal``:
        the job ends after it, later ordinary slots relinquished, a choice
        recorded before any call and never a proof that no later task
        could have fitted). The mandatory stages must fit either way.
        Decided before the first editing call, never after work has begun.
        A parallel child is never refused here (its batch decides,
        ``_admit_batch``), and an explicit list never commits."""
        budget = self._budget()
        if budget is None or not self._serial_dispatch or self._batch_child:
            return
        full = self._plan_stages(spec, stages, collaborators=collaborators, security=security)
        mandatory = [r for r in full if r.name in stages]
        verdict = self._admit_plan(spec.task_id, mandatory, scope="mandatory")
        if not verdict.feasible:
            snap = budget.snapshot()
            held = len((snap.get("finish_protection") or {}).get("pending") or [])
            left = snap["limits"]["max_calls"] - snap["reserved_attempts"] - held
            exc = FinishReserved(f"{spec.task_id}: {obligations.dispatch_floor(list(stages), left) or verdict.detail}")
            exc.infeasible = True
            raise exc
        # The route's own checks: one after the draft, one after a revision.
        checks = 1 + (1 if any(r.name == "revision" for r in full) else 0)
        whole = self._admit_plan(spec.task_id, full, scope="full", wall_extra=self._gate_wall(spec) * checks)
        if whole.feasible:
            return
        # The terminal candidate ends the job after this task: no close-out
        # and no planning call follow it.
        terminal = [r for r in full if r.name not in ("close-out", "next planning call")]
        if len(terminal) < len(full) and self._explicit_tasks is None:
            # The second candidate (admission/3): the same task, reviewers,
            # gates, design and security stages, closed by the harness's
            # factual record instead of a model close-out, and the job
            # committed to end after it. A policy choice to relinquish the
            # later ordinary slots, recorded before any call; not a proof
            # that no later task could have fitted.
            last = self._admit_plan(spec.task_id, terminal, scope="terminal",
                                    wall_extra=self._gate_wall(spec) * checks)
            if last.feasible:
                self._commit_terminal(spec, whole.detail, last.detail)
                return
            detail = f"{whole.detail}; nor its terminal route: {last.detail}"
        else:
            detail = whole.detail
        # Neither route the task could take is payable: it is not started,
        # before its first call, rather than started on a path that ends
        # short of its judgments (root's admission finding on f4fe5b7: a
        # recorded shortfall is not an admission).
        exc = FinishReserved(f"{spec.task_id}: cannot pay its selected route: {detail}")
        exc.infeasible = True
        raise exc

    def _commit_terminal(self, spec, ordinary: str, selected: str) -> None:
        """Commit the job to end after ``spec``: recorded on the session
        (carried by the checkpoint), kept as an artifact, and noted before
        the task's first call. From here no ordinary task is planned or
        dispatched; the task's close-out is factual, every repair's known
        remainder is costed without one, and the cap's decision (the goal
        question and the audit where the gates allow) follows the task.
        ``_slots_used`` keeps the actual count; the relinquished slots are
        named, not spent."""
        total = int(getattr(self, "_max_tasks", 0) or 0)
        used = int(getattr(self, "_slots_used", 0) or 0)
        record = dict(task=spec.task_id, reason=f"its ordinary route could not be paid: {ordinary[:400]}",
                      selected=selected[:400], slots_used=used + 1, relinquished=max(0, total - used - 1),
                      max_tasks=total)
        self._terminal_route = record
        self._last_wave = True
        self.store.put(json.dumps(record), kind="terminal-route", author="harness")
        self._note(f"{spec.task_id}: terminal route committed; {record['relinquished']} later slot(s) relinquished")

    def _terminal_for(self, spec) -> bool:
        """Whether ``spec`` is the task the job committed to end after."""
        record = getattr(self, "_terminal_route", None)
        return bool(record) and record.get("task") == getattr(spec, "task_id", None)

    def _final_work_slot(self, spec) -> bool:
        """Whether no later planning call can continue ``spec``'s work: a
        serial planner task in the job's numeric last slot, or the exact
        task the job committed to end after (``_commit_terminal``). Never an
        explicit list (its own completion path) and never a parallel child
        (its batch decides). The one boundary the capped-source handoffs
        (``_capped_final_draft``, ``_checked_final_source``) and the factual
        close-out read, so a terminal task's capped work meets the same
        bounded review the numeric last slot's does; every other guard
        (measured in-scope edits, passing checks on that exact source, the
        write ceiling, an available cross-vendor reviewer, one cap) stays
        with its caller."""
        if self._explicit_tasks is not None or getattr(self, "_batch_child", False):
            return False
        if self._terminal_for(spec):
            return True
        total = getattr(self, "_max_tasks", None)
        return bool(total) and int(getattr(self, "_slots_used", 0) or 0) + 1 >= int(total)

    def _terminal_committed(self) -> bool:
        return bool(getattr(self, "_terminal_route", None))

    def _admit_batch(self, parsed) -> Optional[str]:
        """Before a parallel wave starts: the sum of every child's remaining
        path, the merge gate's recheck round and the held finish must fit
        what is left, or the wave is not started and the reason is recorded.
        Decided before any child's first call."""
        budget = self._budget()
        if budget is None:
            return None
        rows = []
        saved = self._current_covers
        try:
            for spec, covers, *_ in parsed:
                # The child's reviewers as its lead draws them: the lead is
                # already chosen on the spec, and the covers decide the
                # cross-vendor read.
                self._current_covers = list(covers or [])
                peers = self.collaborators_for(spec, spec.lead or "")
                stages = self._mandatory_stages(spec, peers)
                rows.extend(r for r in self._plan_stages(spec, stages, collaborators=peers, post_merge=True,
                                                         slots=len(parsed)) if not r.finishing)
            rows.extend(self._planning_stages(slots=len(parsed)))
        finally:
            self._current_covers = saved
        name = "batch:" + ",".join(spec.task_id for spec, *_ in parsed)
        # The merge gate's one run on the merged source; a fix of it is
        # bought at that moment, as a task's gate fix is.
        wall_extra = self._gate_wall(SimpleNamespace(task_id=name, scope=None))
        mandatory = [r for r in rows if r.name in ("lead draft", "security worker draft", "design review",
                                                   "security verification") or r.name.startswith("review by ")]
        verdict = self._admit_plan(name, mandatory, scope="mandatory")
        if not verdict.feasible:
            return verdict.detail
        whole = self._admit_plan(name, rows, scope="full", wall_extra=wall_extra)
        if not whole.feasible:
            return f"cannot pay its selected route: {whole.detail}"
        return None

    def _admission_notes(self) -> list:
        """What the last route decision was, as the record holds it: a task
        refused at dispatch and NOT RUN (``not_started`` says so; a refused
        admission alone does not), a task admitted on its terminal route
        with the job committed to end after it (admitted, which is not
        proof any call ran: the task rows and evidence say what ran), or a
        later correction refused after the task had started, its findings
        left open as written."""
        last = next((a for a in reversed(self.plan_admissions)
                     if a.get("scope") in ("full", "fix-cycle", "terminal")), None)
        if last is None:
            return []
        task, detail = last["task"], last["detail"][:200]
        if last["scope"] == "terminal":
            if not last["feasible"]:
                return []  # the refusal that followed is the full route's, below
            terminal = getattr(self, "_terminal_route", None) or {}
            if terminal.get("task") != task:
                return []
            return [f"Plan admission for {task}: its ordinary route could not be paid from what is left after the "
                    f"held finish ({terminal.get('reason', '')[:200]}); its terminal route (the same stages, closed "
                    f"factually) was admitted and the job committed to end after it: {terminal.get('slots_used')} "
                    f"of {terminal.get('max_tasks')} slot(s) spent, {terminal.get('relinquished')} relinquished. "
                    "Admission is not execution: what ran is in the task rows and evidence above."]
        if last["feasible"]:
            return []
        if last["scope"] == "fix-cycle":
            return [f"Plan admission for {task}: a correction its review selected could not be paid from what is "
                    f"left after the held finish ({detail}); the task had started, no editor was called for that "
                    "correction, and the findings it would have answered stay open as written."]
        if any(n.get("task") == task for n in self.not_started):
            return [f"Plan admission for {task}: its selected route could not be paid from what is left after the "
                    f"held finish, so it was not started ({detail})."]
        return [f"Plan admission for {task}: its selected route could not be paid from what is left after the "
                f"held finish ({detail})."]

    def _settle_not_started(self, spec, exc) -> bool:
        """A serial task whose first editing call was refused to keep the
        finish, before any editing call ran and with the measured source
        unchanged, is NOT RUN: recorded, never closed, its requirements left
        uncovered, and the run goes to the finish the existing gates allow.
        Anything else returns False and the refusal stops the run as before:
        an explicit list (its listed work cannot be judged done), a parallel
        child (its batch decides), or a task that already edited."""
        if self._explicit_tasks is not None or self._batch_child or self._task_edits_admitted:
            return False
        state = dict(getattr(self, "in_flight", {}) or {})
        call = state.get("invocation") or {}
        infeasible = bool(getattr(exc, "infeasible", False))
        if not state.get("inspected") or state.get("changed"):
            return False
        if not infeasible and (call.get("role") != "lead" or not call.get("allow_writes")):
            return False
        lead = str(call.get("model") or spec.lead or getattr(self._outcome, "lead", "") or "")
        self.not_started.append(dict(task=spec.task_id, lead=lead, description=(spec.description or "")[:300],
                                     detail=str(exc)[:300]))
        self.finish_refusals.append(dict(task=spec.task_id, what=("dispatch: the calls left cannot pay its "
                                                                  "lead and required reviews" if infeasible
                                                                  else f"first editing call by {lead}"),
                                         detail=str(exc)[:300]))
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        if outcome is not None:
            outcome.closed_as = "not_started"
            for fact in outcome.facts:
                if fact.active and fact.detail.startswith("FinishReserved:"):
                    fact.recovered = True
            outcome.note("unverified", f"NOT RUN: {spec.task_id} was not started; its first editing call was "
                         f"refused to keep the finish and nothing was written ({str(exc)[:200]})", terminal=False)
        self.run_outcome.note("unverified", f"{spec.task_id} NOT RUN: its first editing call was refused to keep "
                              "the finish; no later task can start an edit", terminal=False)
        self.in_flight = {}
        self._active_spec = self._task_memory = None
        self._active_call = {}
        self._note(f"{spec.task_id} not started: its first editing call was refused to keep the finish and "
                   "nothing was written; no further task is planned")
        return True

    def _dispose(self, spec, peer: str, disposition: str) -> None:
        """One reviewer's disposition of this task, in order, for the record
        a factual close-out writes (it never decides anything)."""
        task_id = getattr(spec, "task_id", None)
        if task_id:
            self.review_dispositions.setdefault(task_id, []).append((peer, disposition))
