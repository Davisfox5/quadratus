"""How a task closes when it did not close clean, and what is written when
it closes at all, as a mixin of Session: the capped revision and its
re-check, the handoff of unfinished work, turn-limited and failed closes,
and the harness-written close-outs.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional, Tuple

from .check_facts import _check_for_models
from .memory import TaskMemory, TaskSummary
from .reply_parsers import _finding_id
from .session_prompts import _ONE_CONTINUES_LINE
from .session_types import PartialWorkStopped, TaskFailed, TaskSpec, is_design_task

# One logger name for the engine, wherever its code lives.
log = logging.getLogger("quadratus.session")


class CloseOutMixin:
    """The close of a capped, failed or finished task, and its record.

    Session inherits this; nothing here is used on its own.
    """

    def _capped_revision_recheck(self, lead, spec, task, exc, before, pending, labels, *, stage: str = "revision",
                                 cycle_left: bool = False):
        """A capped revision or fix whose work is the measured, in-scope
        source with the task's checks passing on exactly it meets the
        reviewers whose blocking findings it answered, once, on the current
        source; or None and the capped close applies as before.

        Quality/completion batch, 2026-10-31 (P12: t3's and t4's capped
        revisions each left a gated, in-scope source whose findings were
        never rechecked, and each cost a continuation task): the handoff
        is no longer the last slot's alone. In any serial planner slot the
        same bounded one-shot applies (``_checked_source``): the original
        reviewers, no fresh writer, no retry, the cap event retained. In the
        last slot the task closes on the recheck as before (an unresolved
        finding goes to the close policy). Earlier, an unresolved recheck
        returns None and the capped-task path applies: the recheck's
        verdicts stand on the registry, the task is unfinished debt the
        planner continues by name, and nothing is accepted on the writer's
        behalf.

        Step 2 r10 on f6c028c: t3's capped Grok draft took the last-slot
        handoff to an Opus review, which found malformed CLI ids leaving as
        plain stderr. The revision fixed that within scope and the checks
        passed on its source, but it also stopped at its cap and the task
        closed turn-limited with no recheck, so the job ended
        GoalUnconfirmedAtCap.

        The same conditions as the capped draft (_capped_final_draft): a
        serial task in the last slot, measured in-scope edits under the write
        grant, the task's own checks passing on exactly this source. Also: at
        least one blocking finding to check, every one of its reviewers
        available, and no design evidence (a design fix would be a writer
        round). The writer stays capped: no fix cycle, no gate fix, no slot,
        recovery or grant is added. A finding the reviewer does not call
        RESOLVED, or cannot check, stays open; the source must be unchanged
        by the rechecks. Returns (record text, unresolved)."""
        if not pending or any(not self._available(peer) for peer, _ in pending):
            return None
        if is_design_task(spec) and self.project and self.config.allow_writes:
            return None
        final = self._final_work_slot(spec)
        bound = self._checked_source(spec, before, final_only=False)
        if bound is None:
            return None
        state, current, identity = bound
        peers = [peer for peer, _ in pending]
        said = (exc.partial_text or "").strip()
        where = "in the last task slot" if final else "with task slots left"
        handoff = dict(task=spec.task_id, lead=lead, stage=stage, turns=exc.turns, final_slot=final,
                       slot=int(getattr(self, "_slots_used", 0) or 0) + 1, reviewers=list(peers),
                       changed=list(state["changed"]), changed_lines=state["changed_lines"], outcome=None)
        self.capped_handoffs.append(handoff)
        # The recheck route is admitted whole before it is promised, as the
        # no-edit retry's is (``_no_edit_cap_note``); refused, nothing is
        # written to the transcript and the capped close applies.
        wall_extra, wall_known = self._route_wall(spec)
        if not self._admit_repair(spec, f"recheck of the capped {stage}", "fix-cycle",
                                  self._fix_cycle_stages(spec, pending, editor=False),
                                  after=f"capped {stage}", wall_extra=wall_extra, wall_known=wall_known):
            handoff["outcome"] = "refused"
            self._note(f"task {spec.task_id}: the recheck of the capped {stage} cannot be paid whole; "
                       "the capped close applies")
            return None
        task.keep(json.dumps(dict(task=spec.task_id, lead=lead, turns=exc.turns, changed=state["changed"],
                                  changed_lines=state["changed_lines"], reviewers=peers, final_slot=final,
                                  **self._attempt_identity(spec))),
                  kind="capped-final-revision" if final else "capped-checked-revision", author=lead)
        cap_fact = (f"lead stopped at its turn limit ({exc.turns or '?'} turns) while revising {where}; its "
                    f"in-scope source passed the checks and goes back to {', '.join(peers)} for their own findings")
        if final and self._outcome is not None:
            # With slots left the fact is written only once the recheck
            # resolves: an unresolved handoff takes the capped-task path,
            # whose own cap fact is the one the breaker and the survey count.
            self._outcome.note("cap", cap_fact, terminal=False)
        self._note(f"task {spec.task_id}: revision stopped at its turn limit {where}; its "
                   "checked, in-scope source goes back to the reviewers whose findings it answered")
        record = ("HARNESS RECORD: the lead stopped at its turn limit"
                  + (f" ({exc.turns} turns)" if exc.turns else "")
                  + " while revising, before giving a final answer, "
                  + ("in the job's last task slot" if final else "with task slots left in the job")
                  + ". The revised work is the project source as it is now. Changed since the task began: "
                    f"{', '.join(state['changed'])} ({state['changed_lines']} lines), inside the declared "
                    "scope. The project's gate passed on exactly this source in the harness's own observation "
                    "(the run-start original tests are not re-run there; the task's own gate still decides). "
                    "Nothing here says your findings are addressed: read the source."
                  + (f" The lead's last words, which are narration and not a result: {said[:300]}"
                     if said else " The lead returned no answer text.")
                  + f" Bound to source {current[:16]} and checks {identity[:16]}.")
        task.record("assistant", record)
        task.keep(record, kind="revision")
        # The outcome is read from the registry, never from the recheck's
        # return alone: a capped, refused or verdict-less recheck returns
        # nothing for a finding that stays open (the workflow refutation of
        # this batch, 2026-10-31).
        before_ids = {r["fid"] for r in self._finding_records(task=spec.task_id, open_only=False)}
        pending_ids = {r["fid"] for peer in peers for r in self._finding_records(task=spec.task_id, peer=peer)}
        # How long each finding's history was before the recheck: a verdict
        # is one this recheck wrote, never an earlier round's.
        history_before = {r["fid"]: len(r.get("history") or ())
                          for r in self._finding_records(task=spec.task_id, open_only=False)}
        try:
            unresolved = self._recheck_blocking(spec, pending, record, task, labels=labels)
        except BaseException as exc_:  # noqa: BLE001 -- recorded, then raised exactly as it was
            handoff["outcome"] = f"stopped: {type(exc_).__name__}"
            raise
        open_after = [r for r in self._finding_records(task=spec.task_id)
                      if r["fid"] in pending_ids or r["fid"] not in before_ids]
        # What each finding's recheck came to, by id, read from the
        # registry, for the record and the capped close's wording (workflow
        # refutations of this batch: a capped recheck was reported as the
        # reviewer's UNRESOLVED, a finding the recheck raised as a verdict,
        # an INVALIDATED one as RESOLVED). "unresolved": this recheck wrote
        # the reviewer's UNRESOLVED; "no verdict": it wrote nothing (capped,
        # refused or no valid verdict); "raised": the recheck raised it;
        # "source changed": a check overtaken by an edit. Closed pending
        # findings are "resolved" or "invalidated" as the registry says.
        handoff["findings"], handoff["resolved"], handoff["invalidated"] = {}, [], []
        records_now = {r["fid"]: r for r in self._finding_records(task=spec.task_id, open_only=False)}

        def _written_now(fid):
            history = list((records_now.get(fid) or {}).get("history") or ())
            return history[-1] if len(history) > history_before.get(fid, 0) else None

        if self._source_fingerprint() != current:
            # What was checked is no longer the source: nothing is resolved.
            unresolved = [(peer, f"UNRESOLVED: the source changed during the recheck; {note.strip()}")
                          for peer, note in pending]
            handoff["outcome"] = "source changed"
            handoff["findings"] = {fid: "source changed" for fid in sorted(pending_ids)}
        else:
            open_ids = {r["fid"] for r in open_after}
            said_ids = {_finding_id(v) for _p, v in unresolved}
            for r in open_after:
                fid = r["fid"]
                last = _written_now(fid)
                if fid not in before_ids:
                    handoff["findings"][fid] = "raised"
                elif last is not None and str(last.get("state")) == "unresolved":
                    handoff["findings"][fid] = "unresolved"
                else:
                    why = ((r.get("history") or [{}])[-1].get("why") or "no verdict: the recheck did not judge it")
                    if fid not in said_ids:
                        # Held as open under the reviewer's own name.
                        unresolved.append((r["author"], f"{fid}: UNRESOLVED: {str(why)[:200]}"))
                    handoff["findings"][fid] = f"no verdict: {str(why)[:160]}"
            for fid in sorted(pending_ids - open_ids):
                last = _written_now(fid)
                state = str((last or {}).get("state") or (records_now.get(fid) or {}).get("state") or "")
                (handoff["invalidated"] if state == "invalidated" else handoff["resolved"]).append(fid)
            handoff["outcome"] = "unresolved" if open_after else "resolved"
        handoff["unresolved"] = [_finding_id(v) or v[:120] for _p, v in unresolved]
        progress = len(handoff["resolved"]) + len(handoff["invalidated"])
        if (unresolved or open_after) and not final and cycle_left and progress and handoff["outcome"] == "unresolved":
            # Observed progress inside the task's own fix allowance (PR73
            # 6693873055): the capped call settled some findings on checked
            # source, so the remaining ones go to one more bounded fix call
            # of this task, carrying only those findings, rather than a new
            # task re-sending the whole bundle. Measured by the reviewers'
            # own verdicts, never a findings-per-turn guess; no cap, budget
            # or allowance grows, and admission still decides the call.
            handoff["outcome"] = f"continued: {progress} settled, {len(unresolved)} open"
            handoff["continued"] = True
            self._note(f"task {spec.task_id}: the capped {stage} settled {progress} finding(s); the "
                       f"{len(unresolved)} still open go to the task's next bounded fix call")
            if said:
                self._record_proves(lead, "lead", said)
            if self._outcome is not None:
                self._outcome.note("cap", cap_fact, terminal=False)
            return record, unresolved
        if (unresolved or open_after) and not final:
            # Slots are left: the reviewer's own verdict stands on the
            # registry and the task stays the unfinished debt it was, for
            # the planner to continue by name. Nothing is accepted and no
            # second writer or recheck is called; the capped close records
            # the PROVES lines itself.
            self._note(f"task {spec.task_id}: {len(open_after) or len(unresolved)} finding(s) stayed open after "
                       "the recheck; the capped close applies and the planner continues the work")
            return None
        if said:
            # The accepted capped revision's structured PROVES lines (root
            # on 9bedde0); a declaration, never execution or a verdict.
            self._record_proves(lead, "lead", said)
        if not final and self._outcome is not None:
            self._outcome.note("cap", cap_fact, terminal=False)
        return record, unresolved

    def _checked_final_source(self, spec, before):
        """``(state, source, gate)`` when a capped call's work is the last
        slot's measured, in-scope source and the task's checks passed on
        exactly it; None otherwise (``_checked_source`` with
        ``final_only``)."""
        return self._checked_source(spec, before, final_only=True)

    def _handoff_account(self, spec) -> Optional[dict]:
        """The one-shot handoff this task's capped revision went through
        before taking the capped close, if any (``capped_handoffs``): the
        recheck performed, its outcome and the gate observation it rested
        on. None when no handoff reached a recheck."""
        for entry in reversed(self.capped_handoffs):
            if entry.get("task") != spec.task_id:
                continue
            if entry.get("outcome") in (None, "refused"):
                return None
            return entry
        return None

    @staticmethod
    def _handoff_classes(handoff) -> dict:
        """The handoff's findings by what its recheck came to: ``unresolved``
        (the reviewer's own verdict), ``no_verdict``, ``raised``,
        ``source_changed``, ``resolved``, ``invalidated`` and ``outside``
        (other reviewers' findings open and outside the recheck, as
        ``fid by peer``). An entry written before per-finding outcomes were
        recorded has only ``unresolved`` ids, read as verdicts."""
        classes = {k: [] for k in ("unresolved", "no_verdict", "raised", "source_changed", "resolved",
                                   "invalidated", "outside")}
        if not handoff:
            return classes
        findings = handoff.get("findings")
        if not isinstance(findings, dict) or not findings:
            classes["unresolved"] = list(handoff.get("unresolved") or ()) if handoff.get("outcome") == "unresolved" else []
        else:
            for fid, how in findings.items():
                how = str(how)
                key = ("unresolved" if how == "unresolved" else "raised" if how == "raised"
                       else "source_changed" if how == "source changed" else "no_verdict")
                classes[key].append(fid)
        classes["resolved"] = list(handoff.get("resolved") or ())
        classes["invalidated"] = list(handoff.get("invalidated") or ())
        classes["outside"] = [f"{o.get('finding')} by {o.get('peer')}" for o in (handoff.get("others_open") or [])]
        return classes

    def _handoff_came_to(self, handoff) -> str:
        """The planner's one clause for what the handoff's recheck came to,
        from the per-finding record; each verdict word is the recheck
        grammar's own and appears only where the reviewer wrote it."""
        outcome = str(handoff.get("outcome") or "")
        c = self._handoff_classes(handoff)
        if outcome == "source changed":
            return "the source changed during the recheck, so nothing was resolved"
        parts = []
        if c["resolved"]:
            parts.append("RESOLVED (" + ", ".join(c["resolved"]) + ")")
        if c["invalidated"]:
            parts.append("INVALIDATED (" + ", ".join(c["invalidated"]) + ")")
        if c["unresolved"]:
            parts.append("UNRESOLVED (" + ", ".join(c["unresolved"]) + ")")
        if c["no_verdict"]:
            parts.append("no verdict on " + ", ".join(c["no_verdict"]) + " (the recheck stopped at its turn cap, was "
                         "refused or gave no valid verdict)")
        if c["raised"]:
            parts.append("raised during the recheck: " + ", ".join(c["raised"]))
        if c["outside"]:
            parts.append("findings of other reviewers were open and outside that recheck ("
                         + ", ".join(c["outside"]) + "), so the task is unfinished")
        if not parts:
            return f"the recheck ended {outcome or 'without a record'} with no verdict"
        return "; ".join(parts)

    def _handoff_holds(self, spec, handoff) -> Tuple[str, Dict[str, str]]:
        """``(default, by_fid)``: why each open finding of a capped task is
        held, with the finding's own outcome first so the planner's
        120-character view of a hold still carries it (second-round
        refutation of this batch: the outcome came after the cut)."""
        tid = spec.task_id
        stop = f"left open when {tid} stopped at its lead turn limit"
        if handoff is None:
            return f"{stop} before its recheck", {}
        c = self._handoff_classes(handoff)
        reviewers = ", ".join(handoff.get("reviewers") or ())
        outside = {o.get("finding"): o.get("peer") for o in (handoff.get("others_open") or [])}
        outcome = str(handoff.get("outcome") or "")
        reasons: Dict[str, str] = {}
        for fid in c["unresolved"]:
            reasons[fid] = (f"UNRESOLVED by {reviewers} at its one recheck of {tid}'s capped source (the harness's gate "
                            f"observation); {stop}")
        for fid in c["no_verdict"]:
            reasons[fid] = (f"no verdict at {reviewers}'s one recheck of {tid}'s capped source (capped, refused or no "
                            f"valid verdict); {stop}")
        for fid in c["raised"]:
            reasons[fid] = f"raised by {reviewers} at the recheck of {tid}'s capped source; {stop}"
        for fid in c["source_changed"]:
            reasons[fid] = f"not resolved: the source changed during the recheck of {tid}'s capped source; {stop}"
        for fid, peer in outside.items():
            reasons[fid] = (f"not rechecked: outside the one recheck of {tid}'s capped source by {reviewers} "
                            f"(its reviewer {peer} was not asked); {stop}")
        default = (f"no verdict: the recheck of {tid}'s capped source ended {outcome}; {stop}"
                   if outcome and outcome not in ("unresolved", "resolved", "source changed")
                   and not outcome.startswith("unresolved: other findings open")
                   else f"{stop}; not part of the one recheck of {tid}'s capped source")
        return default, reasons

    def _close_turn_limited(self, lead, spec, task, exc, before) -> TaskSummary:
        """A lead stopped at its turn limit: keep the work, record it, re-plan.

        The call ran and its usage is already on the ledger. Its edits stay
        where they are, inspected and held to the task's scope exactly as a
        finished draft's would be; out-of-scope or unmeasurable edits still
        stop the run with the work preserved. What does not happen is review,
        the gate or a close-out call: the task is unfinished, so the harness
        writes its record from the evidence and hands the rest back to the
        orchestrator, which names the remaining work as a new task. The lead's
        final text, if any, is narration of work in progress and is labelled
        as such, never folded in as a result.
        """
        state = self._inspect_partial_edits(before)
        if self._outcome is not None:
            self._outcome.note("cap", f"stopped at the lead turn limit ({exc.turns or '?'} turns)")
            self._outcome.partial = dict(changed=state["changed"], changed_lines=state["changed_lines"])
        if self._write_ceiling():
            if not state["inspected"]:
                raise PartialWorkStopped("Lead stopped at its turn limit and the source could not "
                                         "be inspected; work preserved.", partial=state) from exc
            if state["changed"]:
                report = self._assess_scope(spec, task, before)
                if spec.scope is not None and report is None:
                    raise PartialWorkStopped("Turn-limited edits could not be measured against the "
                                             "task scope; work preserved.", partial=state) from exc
                if report and report.blocking:
                    raise TaskFailed("scope", "Turn-limited edits exceed the declared scope; work "
                                     "preserved. " + report.render(), partial=state) from exc
        said = (exc.partial_text or "").strip()
        if state["changed"] and said:
            # The accepted capped work's own partial output, read only for
            # its structured PROVES lines (root on 9bedde0: _record_proves
            # ran only after a finished edit). A declaration is neither
            # execution nor a verdict; the cap stays recorded, malformed
            # lines fail closed, and no extra call is made.
            self._record_proves(lead, "lead", said)
        if state["changed"]:
            # What the capped lead left meets the gate once, as evidence only:
            # r08's t1 stopped at its cap and its source never met a check
            # before a verification-only task was named for it.
            self._observe_checks(spec, "turn-limited")
        # What was performed before this close is stated as it happened
        # (root's correction on 0c5c6f1, C3): a capped revision whose
        # checked source met its original reviewer once records that
        # recheck and its outcome, never "before its recheck", and the gate
        # it rested on was the harness's observation, not the task's
        # canonical gate.
        handoff = self._handoff_account(spec)
        record = dict(task=spec.task_id, lead=lead, turns=exc.turns,
                      changed=state["changed"], changed_lines=state["changed_lines"],
                      note=state["note"], partial_text=said[:4000] or None,
                      content=self._content_fingerprint(state["changed"]),
                      handoff=(dict(outcome=handoff.get("outcome"), reviewers=list(handoff.get("reviewers") or ()),
                                    unresolved=list(handoff.get("unresolved") or ()),
                                    findings=dict(handoff.get("findings") or {}),
                                    resolved=list(handoff.get("resolved") or ()),
                                    invalidated=list(handoff.get("invalidated") or ()),
                                    others_open=list(handoff.get("others_open") or ()))
                               if handoff is not None else None),
                      **self._attempt_identity(spec))
        task.keep(json.dumps(record), kind="turn-limited", author=lead)
        self.turn_limited_records[spec.task_id] = record
        reviewers = ", ".join(handoff.get("reviewers") or ()) if handoff is not None else ""
        held_why, reasons = self._handoff_holds(spec, handoff)
        self._hold_task_findings(spec, held_why, reasons=reasons)
        from .project_files import MAX_INSPECT_BYTES
        self._cap_baselines[spec.task_id] = {
            path: (before or {}).get(path, b"") for path in state["changed"]
            if len((before or {}).get(path, b"")) <= MAX_INSPECT_BYTES}
        changed = ", ".join(state["changed"]) or "no files"
        if handoff is None:
            performed = f". Changed, unreviewed and ungated: {changed}"
        else:
            came_to = self._handoff_came_to(handoff)
            performed = (f". Changed: {changed}. The harness observed the project gate passing on this source "
                         "(an observation for the reviewers; the task's own canonical gate, with the run-start "
                         f"original tests, did not run), and the original reviewer ({reviewers}) re-checked its "
                         f"finding once on it: {came_to}; no further writer or recheck was called")
        summary_text = (
            "STOPPED AT THE LEAD TURN LIMIT before finishing"
            + (f" ({exc.turns} turns)" if exc.turns else "")
            + performed
            + (f" ({state['changed_lines']} lines)" if state["changed"]
               else ". No change to project files was detected after it stopped; the "
                    "continuation is handed the task's files as they are now")
            + ". This task is not done: name the remaining work as a new, smaller task "
              f"whose description includes the line 'CONTINUES: {spec.task_id}' on its own line "
              "with nothing else on it, and do not assume any of it is finished. " + _ONE_CONTINUES_LINE
              + " Size max_lines for the remaining "
              "work only, and count every test not yet written in full, including its "
              "setup (fixtures, stubs, fakes); if that setup is large, give it its own task."
            + (f" The lead's last words, which are narration and not a result: {said[:300]}"
               if said else " The lead returned no answer text.")
        )
        summary = task.close(
            summary=summary_text,
            reasoning=("Recorded by the harness from the stopped call's evidence. No close-out "
                       "model call is made for an unfinished task."),
            dead_ends=[],
        )
        # TaskSummary is frozen; the outcome is set on a copy, never mutated.
        from dataclasses import replace
        summary = replace(summary, outcome="turn_limited")
        self.memory.absorb(summary)
        self.history.append(summary)
        self.turn_limited.append(spec.task_id)
        return summary

    def _close_failed(self, spec, exc: TaskFailed) -> TaskSummary:
        """A task failed on its own rules: keep the work, record it, re-plan.

        The capped-task path, applied to a failure (docs/DIRECTION.md;
        operator ruling 2026-09-28). The edits stay where they are. No
        review, gate or close-out call is made for it: the harness writes
        the record from the evidence, the cause is a terminal ``failed``
        fact on the task, and the orchestrator is told to name the remaining
        work with CONTINUES. A continuation that closes clean recovers the
        fact; two unfinished tasks in a row trip the same breaker a cap does.
        """
        task = self._task_memory
        lead = task.author
        partial = dict(exc.partial or {})
        state = partial if "inspected" in partial else self._inspect_partial_edits(self._task_before)
        if self.project and not state.get("inspected"):
            # The task-level path needs to know what the call left; without
            # that the run stops, as every other uninspectable case does
            # (Codex review of d80d9d3: a malformed WORKER reply on an
            # uninspectable tree reached a second orchestrator round).
            raise PartialWorkStopped(
                f"Task {spec.task_id} failed ({exc.cause}: {str(exc)[:200]}) and the source could "
                "not be inspected; work preserved. " + str(state.get("note") or ""),
                partial=dict(state, cause=exc.cause)) from exc
        changed = list(state.get("changed") or [])
        if exc.cause == "scope":
            # The scope check opened an out-of-path finding for the same
            # edits; the failed fact below carries the same list and is what
            # the continuation recovers, so the finding is history from here.
            marker = f"Task {spec.task_id} changed paths outside its declared scope: "
            self.open_findings = [text for text in self.open_findings if not text.startswith(marker)]
            if self._outcome is not None:
                for fact in self._outcome.facts:
                    if fact.kind == "integrity" and fact.detail.startswith(marker):
                        fact.recovered = True
        if self._outcome is not None:
            self._outcome.note("failed", f"{exc.cause}: {exc}")
            self._outcome.partial = dict(changed=changed, changed_lines=state.get("changed_lines", 0),
                                         inspected=state.get("inspected"), note=state.get("note"))
        if changed and state.get("inspected"):
            # What the failed attempt left meets the gate once, as evidence
            # for the re-plan only (step 2): the capped path already did this,
            # the failed path handed the planner the lead's error text alone.
            self._observe_checks(spec, "failed")
        record = dict(task=spec.task_id, lead=lead, cause=exc.cause, error=str(exc)[:2000],
                      changed=changed, changed_lines=state.get("changed_lines", 0), note=state.get("note"),
                      role=str(self._active_call.get("role") or ""),
                      content=self._content_fingerprint(changed), **self._attempt_identity(spec))
        task.keep(json.dumps(record), kind="task-failed", author=lead)
        self.failed_records[spec.task_id] = record
        try:
            self._record_task_failure(spec, exc, lead, state, changed)
        except Exception:  # noqa: BLE001 -- the improvement record never fails a run
            self._note(f"{spec.task_id}: the task-failure occurrence could not be written")
        self._hold_task_findings(spec, f"left open when {spec.task_id} failed ({exc.cause}) before its recheck")
        from .project_files import MAX_INSPECT_BYTES
        before = self._task_before or {}
        self._cap_baselines[spec.task_id] = {
            path: before.get(path, b"") for path in changed
            if len(before.get(path, b"")) <= MAX_INSPECT_BYTES}
        listed = ", ".join(changed) or "no files"
        what = {
            "scope": "its edits exceeded the declared scope",
            "reply": "its lead's reply was neither a request nor a delivery",
            "channel": "its lead's requests did not converge on a draft",
            "transport": "its lead's call stopped after changing source",
            "environment": "its calls repeated the same environmental tool failure on unchanged source",
        }[exc.cause]
        summary_text = (
            f"FAILED before finishing: {what}. {str(exc)[:600]}"
            f" Changed, unreviewed and ungated: {listed}"
            + (f" ({state.get('changed_lines', 0)} lines)" if changed else "")
            + ". This task is not done: name the remaining work as a new, smaller task "
              f"whose description includes the line 'CONTINUES: {spec.task_id}', and do not "
              "assume any of it is finished. " + _ONE_CONTINUES_LINE + " Size max_lines for the remaining work only"
            + (", and name every path it needs" if exc.cause == "scope" else "")
            + ". If the failure shows the plan was wrong, name a different task instead."
        )
        summary = task.close(
            summary=summary_text,
            reasoning=("Recorded by the harness from the failed call's evidence. No close-out "
                       "model call is made for an unfinished task."),
            dead_ends=[],
        )
        from dataclasses import replace
        summary = replace(summary, outcome="failed")
        self.memory.absorb(summary)
        self.history.append(summary)
        self.failed.append(spec.task_id)
        self._note(f"task {spec.task_id} failed ({exc.cause}): {str(exc)[:160]}; work kept, the orchestrator re-plans")
        return summary

    def _task_close_out(self, lead: str, spec: TaskSpec, task: TaskMemory, *, mechanical: bool = True):
        """The record that survives a task, written by the harness from what
        it holds and never asked of a model (Davis, 2026-10-21): the direct
        tier's mechanical record, else the factual record for every task,
        reviewed or not, whether or not a later planning call reads it. The
        summary call it replaces was one more model round per task, timed
        out on the ticket-workflow run's only task with nothing reportable
        in its stream, and could invent decisions, dead ends and map notes
        the evidence did not state. The ``why`` the record carries still
        says what the harness knew about the task's position."""
        if mechanical and self._tier(spec) == "direct":
            return self._mechanical_close_out(lead, spec, task)
        if self._terminal_for(spec):
            why = ("the job committed to end after this task when its ordinary route could not be paid "
                   "(terminal route)")
        elif getattr(self, "_last_wave", False):
            why = "this task is in the job's last dispatched wave"
        else:
            why = self._stop_follows_close() or "every task closes on the harness's record; no model summary is asked"
        return self._factual_close_out(lead, spec, task, why=why)

    def _factual_close_out(self, lead: str, spec: TaskSpec, task: TaskMemory, *, why: str):
        """A standard task's record, written by the harness from what it holds
        (Davis, 2026-10-16: no model summary where nothing later needs one).

        Not the direct tier's text, which says no collaborator reviewed: this
        task's reviews are stated as they ended, with each recheck. It names
        the measured changes, the last check and whether it ran on this
        source, the debt left open, NOT RUN reports and scratch proofs, and
        points to the full transcript and diff, kept as artifacts as a model
        close-out's are. It invents nothing: no decision, dead end or map
        note, and it says so."""
        transcript = "\n\n".join(f"[{t.role}] {t.content}" for t in task.turns()
                                   if not (t.role == "user" and t.content == spec.description))
        # The measurement, with its status: a change list is a fact only when
        # the pre-task snapshot exists and the inspection ran; otherwise the
        # record says unknown (root's review of 8bc9805: "no files" was
        # written for a task with edits and no snapshot).
        changed, lines, diff = None, 0, "No project source diff is available."
        measured = "no project selected"
        if self.project and self._task_before is None:
            measured = "unknown: no pre-task source snapshot was taken"
        elif self.project:
            state = self._inspect_partial_edits(self._task_before, stopped=False)
            if state.get("inspected"):
                changed, lines = list(state.get("changed") or []), state.get("changed_lines", 0)
                measured = "measured against the pre-task snapshot"
            else:
                measured = f"unknown: the source could not be inspected ({str(state.get('note') or '')[:120]})"
            try:
                from .project import Project
                diff = Project(self.project, exclude=self.config.project_excludes).diff(self._task_before) or \
                    "No source changes in this task."
            except Exception:  # noqa: BLE001 -- the record says what is missing
                log.debug("could not prepare factual close-out diff", exc_info=True)
        pointers = []
        for title, content in (("Recorded conversation", transcript), ("Source diff captured by the harness", diff)):
            ref = self.store.put(content, kind="closeout-evidence", author="harness")
            pointers.append(f"{title}: artifact {ref.id}")
        task.keep("\n".join(pointers), kind="closeout-evidence-index", author="harness")
        current = self._source_fingerprint() if self.project else None
        check = self.checks[-1] if self.checks else None
        if check is None:
            checked = "no check recorded"
        else:
            view = _check_for_models(check)
            gates = ", ".join(f"{g.get('id')}: {g.get('status')}" for g in view["gates"])
            checked = (("last check PASSED" if check.get("passed") else "last check FAILED")
                       + (f" [{gates}]" if gates else "")
                       + (" on the current source" if current and check.get("source") == current
                          else " (not shown to be on the current source)"))
        evidence = self._design_state_for_record(spec, current)
        scenarios = [f"{r.get('proof_id') or r.get('oid')}: binding {r.get('binding', 'unknown')}"
                     + (f" ({', '.join(r.get('cases') or ())})" if r.get("cases") else "")
                     + f", execution {r.get('execution', 'unknown')}"
                     for r in self.evidence_obligations
                     if r.get("task") == spec.task_id and r.get("kind") == "acceptance"]
        reviews = self.review_dispositions.get(spec.task_id) or []
        reviewed = ("; ".join(f"{peer}: {d}" for peer, d in reviews) if reviews
                    else "no reviewer was invoked for this task")
        open_ = [f.full or f.detail for f in (self._outcome.facts if self._outcome is not None else [])
                 if f.active]
        unresolved = [e["text"] for e in self.unresolved_reviews if e.get("task") == spec.task_id]
        not_run = [e["item"] for e in self.unexecuted_acceptance if e.get("task") == spec.task_id]
        proofs = [f"{p.get('peer')}: {p.get('status')}" for p in self.scratch_proofs if p.get("task") == spec.task_id]
        refused = [r["what"] for r in self.finish_refusals if r.get("task") == spec.task_id]

        # The full record is durable and unbounded (every path, every finding);
        # the model-facing summary is bounded per field with explicit omission
        # counts and points at it, so no debt is truncated away.
        full = task.keep(json.dumps(dict(task=spec.task_id, lead=lead, why=why, changed=changed, changed_lines=lines,
                                         measurement=measured, source=current, check=checked, reviews=reviews,
                                         design=evidence, proof_scenarios=scenarios, open=open_,
                                         unresolved=unresolved, not_run=not_run, scratch_proofs=proofs,
                                         finish_refused=refused, evidence=pointers)),
                         kind="factual-closeout", author="harness")
        record_pointer = f"full record: artifact {getattr(full, 'id', 'unavailable')}"

        def listed(items, limit=8, width=300):
            items = [str(i)[:width] for i in items]
            if not items:
                return "none"
            shown = "; ".join(items[:limit])
            return shown + (f" and {len(items) - limit} more ({record_pointer})" if len(items) > limit else "")
        if changed is None:
            changed_text = f"unknown ({measured})"
        elif not changed:
            changed_text = f"no files ({measured})"
        else:
            names = ", ".join(c[:120] for c in changed[:12])
            changed_text = (f"{len(changed)} file(s), {lines} lines: {names}"
                            + (f" and {len(changed) - 12} more ({record_pointer})" if len(changed) > 12 else ""))
        summary_text = (
            f"Recorded by the harness, not a model ({why[:300]}). "
            f"Changed: {changed_text}" + (f"; source {current[:12]}" if current else "") + ". "
            f"Checks: {checked[:600]}. Reviews: {listed(reviews and [f'{peer}: {d}' for peer, d in reviews] or [reviewed])}. "
            f"Design: {evidence[:400]}. "
            f"Proof scenarios: {listed(scenarios)}. "
            f"Open at close: {listed(open_)}. Unresolved review findings: {listed(unresolved)}. "
            f"NOT RUN reports: {listed(not_run)}. Scratch proofs: {listed(proofs)}. "
            f"Calls refused to keep the finish: {listed(refused)}. "
            "No decisions or dead ends were written by a model, and the codebase map was not amended by "
            "this task. Evidence: " + "; ".join(pointers) + f"; {record_pointer}")
        self._note(f"{spec.task_id}: close-out recorded by the harness ({why})")
        return summary_text, f"No model-written decision record: recorded by the harness ({why}).", []

    def _mechanical_close_out(self, lead: str, spec: TaskSpec, task: TaskMemory):
        """The direct tier's record, written by the harness from what it
        measured: no model call, no map notes. The orchestrator reads it at
        its next planning call exactly where a close-out summary goes."""
        changed, lines = [], 0
        state = self._inspect_partial_edits(self._task_before) if self.project else {}
        if state.get("inspected"):
            changed, lines = list(state.get("changed") or []), state.get("changed_lines", 0)
        check = self.checks[-1] if self.checks else None
        checks = ("no check ran" if check is None else
                  ("check passed" if check.get("passed") else "check FAILED")
                  + (": " + ", ".join(f"{r.get('id')} {r.get('status')}" for r in check.get("receipts") or [])
                     if check.get("receipts") else ""))
        record = (self.design_checks[-1] if self.design_checks
                  and self.design_checks[-1].get("task") == spec.task_id else None)
        # The review's stored decision, never its text compared to a literal:
        # "APPROVED" with a trailing newline was approved and then written
        # here as "not verified:" with no reason (review 1110, session second
        # half 19). Verified renders the review did not approve say so.
        review = (record or {}).get("final_review") or {}
        if record is None:
            evidence = "no design evidence required"
        elif not record.get("verified"):
            evidence = f"design evidence not verified: {str(record.get('problem') or '')[:160]}"
        elif review.get("approved") is True:
            evidence = "design evidence verified and reviewed"
        elif review:
            blocking = "; ".join(str(line)[:200] for line in (review.get("blocking") or [])[:3])
            evidence = "design evidence verified, design review not approved" + (f": {blocking}" if blocking else "")
        else:
            evidence = "design evidence verified, with no design review on record"
        summary_text = (
            f"Direct tier, recorded by the harness. Changed: {', '.join(changed) or 'no files'}"
            + (f" ({lines} lines)" if changed else "") + f". {checks}. {evidence}. "
            "No collaborator review and no model close-out were run for this task; the codebase "
            "map was not amended by it."
        )
        task.keep(json.dumps(dict(task=spec.task_id, lead=lead, tier="direct", changed=changed,
                                  changed_lines=lines, checks=checks, evidence=evidence)),
                  kind="direct-closeout", author="harness")
        return summary_text, "Recorded by the harness from measured facts (direct tier).", []


    def _refused_close_out(self, lead, spec, task, exc, changed, pointers):
        """The record a task keeps when the vendor declines to write it.

        GameTape run 10 (2026-09-26): after a Claude lead's edits and a passing
        check, the tool-less close-out on the same seat came back as a vendor
        safeguard refusal, and the run stopped with the task's work unrecorded.
        The refusal stands: nothing is retried, rephrased or sent to another
        model. The harness writes a plain record from what it measured itself
        and says, in the record, that no model wrote it.
        """
        refusal = f"{type(exc).__name__}: {exc}"[:500]
        ref = task.keep(refusal, kind="closeout-refused", author=lead)
        if self._outcome is not None:
            # Historical, not a block: the handled route keeps the task closed.
            self._outcome.note("refusal", f"close-out refused ({getattr(exc, 'category', None) or 'unspecified'}); "
                               f"harness record kept as artifact {getattr(ref, 'id', 'unavailable')}",
                               stage="closeout", terminal=False)
        if changed is None:
            files = "changed files unknown (no source snapshot)"
        elif changed:
            files = "changed files: " + ", ".join(changed[:40]) + (" ..." if len(changed) > 40 else "")
        else:
            files = "no source changes"
        # The model view of the check, never its command: this summary reaches
        # the orchestrator's memory, and a gate command can name an examiner
        # path seats must not see (tests/test_gate_privacy.py).
        check = _check_for_models(self.checks[-1]) if self.checks else None
        if check is None:
            checked = "no check recorded"
        else:
            gates = ", ".join(f"{g.get('id')}: {g.get('status')}" for g in check["gates"])
            checked = ("last recorded check (may predate this task) "
                       + ("PASSED" if check["passed"] else "FAILED") + (f" [{gates}]" if gates else ""))
        summary = (f"Close-out not written: {lead} declined the summary request "
                   f"(vendor refusal). Harness-recorded facts for {spec.task_id}: "
                   f"{files}; {checked}. Evidence: " + "; ".join(pointers))
        self._note(f"{spec.task_id}: close-out refused by {lead}; harness record kept")
        return summary, "No model-written decision record: the close-out was refused.", []
