"""No-edit DONE declarations of the session engine, as a mixin of Session:
who owns a declaration, the restatement judgment it may need, what DONE
and the task cap declare, and the admission of a continuation after one.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

from . import delivery, judgment
from .delegation import invocation
from .providers import ProviderError, TurnLimitReached
from .reply_parsers import _PROVES_ANY, _PROVES_PID, _RUNS_ANY
from .run_budget import FinishReserved
from .scope import PROVES_RULE, TEST_SUBSTANCE_RULE
from .session_prompts import _display_item, _replaced_note


class DoneMixin:
    """What is declared at DONE or at the cap, and who judges a restatement.

    Session inherits this; nothing here is used on its own.
    """

    # -- no-edit DONE declarations (cycle05) ------------------------------------
    def _declaration_owner(self, entry: dict) -> str:
        """The task whose lead may restate a NOT RUN item: the last task that
        took it over by DISCHARGES, else the one that reported it."""
        taken = [t.get("task") for t in entry.get("taken_over_by") or () if t.get("task")]
        return str(taken[-1] if taken else entry.get("task") or "")

    def _owner_lead(self, task_id: str) -> str:
        """The lead seat the task's record names as invoked, or ""."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == task_id), None)
        return str(getattr(outcome, "lead", "") or "") if outcome is not None else ""

    def _done_declarations(self, blocked: Dict[str, str]) -> Tuple[Optional[List[str]], str]:
        """Settle declaration-only blockers at DONE without a planner call or
        a task slot (cycle05): one bounded read-only ``proof-binding`` call
        per owning lead, all of that owner's ids together, priced at the
        reserve and admitted against what is left after the held finish.
        Returns the attempted owners, or None and why nothing was asked. The
        record (``done_declarations``) is appended before each call, so an
        attempt that may have started is never offered again, across
        repeated DONEs, resumes and merges."""
        plan, why = self._done_declaration_plan(blocked)
        if not plan:
            return None, why
        if self._budget() is not None:
            stages = [delivery.PlanStage(f"declaration by {spec['lead']} for {owner}", "reserve")
                      for owner, spec in plan.items()]
            verdict = self._admit_plan("done-declaration", stages, scope="done-declaration")
            if not verdict.feasible:
                return None, f"the declarations cannot be paid: {str(verdict.detail)[:200]}"
        attempted = []
        for owner, spec in plan.items():
            self._declare_at_done(owner, spec)
            attempted.append(owner)
        self._refresh_obligations()
        return attempted, ""

    def _independent_judge(self, author: str, preferred=()) -> Optional[str]:
        """An eligible independent seat to judge a restatement ``author``
        wrote, or None (cycle14): a brain-trust seat (the recorded judges in
        ``preferred`` first), available, not the author and not of the
        author's vendor. A worker seat is never a judge, and no seat is
        added."""
        vendor = str(author or "").partition(":")[0]
        trust = list(self.brain_trust)
        for seat in dict.fromkeys([*preferred, *trust]):
            if seat not in trust or seat == author or str(seat).partition(":")[0] == vendor:
                continue
            if self._available(seat):
                return seat
        return None

    def _restatement_asked(self, entry: dict, judge: str) -> Optional[dict]:
        """The record of this exact decision's ask, if it was made."""
        key = self._restatement_decision_key(entry, judge)
        return next((r for r in reversed(self.restatement_judgments)
                     if key in (r.get("keys") or {}).values()), None)

    def _restatement_judgment_prompt(self, items: List[dict]) -> str:
        """The independent restatement judgment's one prompt: the goal, the
        requirements the items hold, and each item as reported, restated and
        executed (harness facts), with the one line form per asked id."""
        lines = []
        for e in items:
            record_ = self._discharge_record(e) or {}
            lines.append(f"- {e['oid']} (reported by {e['task']}, holds {', '.join(e.get('requirements') or [])}): "
                         f"NOT RUN: {_display_item(e['item'])[:400]}" + (f" - {e['why'][:200]}" if e.get("why") else "")
                         + f"; restated by {(e['restatements'][-1] or {}).get('task')}'s lead as "
                         f"{', '.join(e.get('cases') or [])}; executed and passed on this source in check "
                         f"{int(record_.get('index', -1)) + 1}" + _replaced_note(e))
        texts = self._requirement_texts_now()
        held = sorted({r for e in items for r in e.get("requirements") or ()})
        return ("You are asked for an independent judgment of restated NOT RUN items; the seat that wrote them "
                "cannot judge them.\n\n"
                + "## Original goal (verbatim)\n\n" + self.memory.goal.strip()
                + "\n\n## Requirements these items hold (verbatim)\n\n"
                + "\n".join(f"{r}: {texts.get(r, '')}" for r in held)
                + "\n\n## NOT RUN items restated as cases that need an independent judgment\n" + "\n".join(lines)
                + "\n\nThe project in your working directory is the delivered work. You did not write these "
                  "restatements. For each item give one line '<O-id>: SUFFICIENT <how these cases exercise "
                  "the whole reported behaviour>' or '<O-id>: INSUFFICIENT <what they leave unexercised>'. Judge "
                  "the cases against everything the item reported, not whether they pass: the harness settled "
                  "execution itself. Only lines for the ids listed above are read, one line per id; nothing else "
                  "is asked, and requirements are the audit's.")

    @staticmethod
    def _restatement_unread(reply: str, oids: Sequence[str]) -> List[str]:
        """The asked ids a judgment reply gives no single verdict line for:
        no line, or two or more (a conflict)."""
        verdicts, conflicts = judgment.sufficiency_reading(reply or "", list(oids))
        return [oid for oid in oids if oid not in verdicts or oid in conflicts]

    def _ask_restatement_judgment(self, record: dict, judge: str, items: List[dict], prompt: str) -> None:
        """One independent judge's first ask for its admitted items, and its
        strict reading (cycle18). The decision is already on the record, so
        it is never asked again whatever happens here. A cap, a finish hold,
        a transport or refusal stop, an empty reply or a bound identity that
        moved during the call leaves every item held and says which; nothing
        is completed after no reply. Lines are applied to exactly the asked
        ids. An arrived reply that leaves an asked id without a single
        verdict buys the job's shared completion once, for exactly those ids,
        with the reply that arrived kept as its raw."""
        oids = list(record["oids"])
        spec = SimpleNamespace(task_id="restatement-judgment")
        binding = self._judgment_binding(spec, judge, "recheck")
        record.update(asked=True, identity=binding["identity"])
        self.store.put(json.dumps(record, default=str), kind="restatement-judgment-intent", author="harness")
        try:
            with invocation(spec.task_id, "recheck"):
                reply = self._invoke_model(judge, prompt)
        except TurnLimitReached:
            record["outcome"] = "capped: no verdict; held"
            return
        except FinishReserved as exc:
            self._finish_refused(None, f"restatement judgment by {judge}", exc)
            record["outcome"] = "refused to keep the finish; held"
            return
        except ProviderError as exc:
            # A refusal, a transport failure or a permission block: recorded,
            # and the item held. Its message can carry vendor text, so it is
            # kept as a private artifact and only its type is exported.
            record["outcome"] = f"stopped: {type(exc).__name__}; held"
            record["error_artifact"] = self.store.put(str(exc), kind=f"restatement-judgment-error:{judge}",
                                                      author="harness").id
            return
        reply = reply if isinstance(reply, str) else ""
        record["reply_artifact"] = self.store.put(reply, kind=f"restatement-judgment:{judge}", author=judge).id
        after = self._judgment_binding(spec, judge, "recheck")
        if after != binding:
            changed = sorted(k for k in binding if k != "identity" and binding[k] != after.get(k))
            record["outcome"] = f"void: the bound identity changed during the call ({', '.join(changed)}); held"
            return
        if not reply.strip():
            record["outcome"] = "the reply was empty: no verdict; held"
            return
        self._judge_restated(judge, reply, oids=oids)
        lacking = self._restatement_unread(reply, oids)
        if lacking:
            answer = None
            try:
                answer = self._complete_judgment(
                    spec, judge, "restatement", reply, [f"one line for each of {', '.join(lacking)}"],
                    role="recheck", prompt=prompt, outcome=self.run_outcome)
            except ProviderError:
                pass  # recorded on the completion's own record, as the review route records it
            completion = next((r for r in reversed(self.judgment_completions)
                               if r.get("task") == spec.task_id and r.get("seat") == judge), {})
            if answer is not None:
                self._judge_restated(judge, answer, oids=lacking)
                self._settle_completion(spec, judge, "restatement", not self._restatement_unread(answer, lacking),
                                        outcome=self.run_outcome)
            record["completion"] = dict(asked=list(lacking), outcome=completion.get("outcome"),
                                        raw_artifact=completion.get("raw_artifact"),
                                        reply_artifact=completion.get("reply_artifact"),
                                        judgment=completion.get("judgment"))
        judged = [e["oid"] for e in items if self._restated_judged(e)]
        adverse = [e["oid"] for e in items if (e.get("restated_judgment") or {}).get("verdict") == "insufficient"
                   and (e.get("restated_judgment") or {}).get("by") == judge and self._restated_current(e)]
        record.update(outcome="answered", sufficient=judged, insufficient=adverse,
                      unanswered=[o for o in oids if o not in judged and o not in adverse])

    def _declaration_decision(self, owner: str, oids, pids) -> str:
        """What one DONE declaration decides: the owner, the source, gate and
        environment, the NOT RUN items asked (id, item, requirements,
        binding) and the scenario rows asked (id, oid, binding, cases), and
        the current revisions of the requirements they hold. Equal digests
        are the same decision and are asked once; anything that moves the
        question makes a new one (cycle05 correction)."""
        revisions = self._requirement_revisions()
        items = [dict(oid=e.get("oid"), item=e.get("item"), requirements=sorted(e.get("requirements") or []),
                      binding=e.get("binding"), cases=list(e.get("cases") or []))
                 for e in self.unexecuted_acceptance if e.get("oid") in oids]
        rows = [dict(pid=r.get("proof_id"), oid=r.get("oid"), binding=r.get("binding"), cases=list(r.get("cases") or []),
                     requirements=[list(x) for x in r.get("requirements") or []])
                for r in self.evidence_obligations if r.get("task") == owner and r.get("proof_id") in pids
                and not r.get("superseded_by")]
        held = sorted({rid for e in items for rid in e["requirements"]}
                      | {x[0] for r in rows for x in r["requirements"]})
        body = dict(owner=owner, **self._declaration_identity(), oids=sorted(oids), pids=sorted(pids),
                    items=sorted(items, key=lambda e: str(e["oid"])), rows=sorted(rows, key=lambda r: str(r["pid"])),
                    revisions={rid: int(revisions.get(rid, 1)) for rid in held})
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]

    def _declare_at_done(self, owner: str, spec: dict) -> dict:
        """One owner's DONE declaration call and its strict reading. Only
        RUNS lines for the listed NOT RUN ids and PROVES lines for the listed
        scenarios are read, through the same readers an editing reply uses
        (``_record_runs``, ``_record_proves``) with the owner as the active
        task; a line naming an unlisted id is dropped and recorded; a RUNS or
        PROVES line naming no id makes the whole reply unread. A cap, a
        refusal by the finish hold or the seat, a transport failure, or a
        source, gate or environment that moved during the call leaves every
        id held as it was. Nothing is closed, judged or retried here: the
        harness settles execution against receipts already on record and
        the independent audit judges sufficiency."""
        lead, oids, pids = spec["lead"], list(spec["oids"]), list(spec["pids"])
        before = self._declaration_identity()
        record = dict(owner=owner, lead=lead, oids=oids, pids=pids, source=before["source"], outcome="",
                      decision=spec.get("decision") or self._declaration_decision(owner, oids, pids),
                      intent=str(spec.get("intent") or "")[:300], dropped=[], settled=[], held=[])
        self.done_declarations.append(record)
        self.store.put(json.dumps(record), kind="done-declaration-intent", author="harness")
        items = [e for e in self.unexecuted_acceptance if e.get("oid") in oids]
        rows = [r for r in self.evidence_obligations if r.get("task") == owner and r.get("proof_id") in pids
                and str(r.get("binding") or "").startswith("stale") and not r.get("superseded_by")]
        lines = []
        if items:
            lines.append("NOT RUN items of your task that name nothing a check can run:")
            lines += [f"- {e['oid']}: {_display_item(e['item'])[:300]}" + (f" - {str(e.get('why'))[:160]}"
                                                                            if e.get("why") else "")
                      # A bound case gone from the source (cycle22): said
                      # so, so the declaration names current exact cases for
                      # everything the original item reported; the harness
                      # never pairs the old name with a new one.
                      + (f" (its bound case {', '.join(self._bound_cases_gone(e))} is no longer defined on the "
                         "current source; name the current cases that exercise everything this item reported, "
                         "if they exist)" if self._bound_cases_gone(e) else "") for e in items]
        if rows:
            lines.append("Proof scenarios of your task whose bound case no longer exists:")
            for row in rows:
                told = dict(row.get("scenario") or ())
                lines.append(f"- {row['proof_id']}: {told.get('operation') or row.get('behavior') or ''} | state: "
                             f"{told.get('state') or ''} | expect: {told.get('expect') or ''} (binding now: "
                             f"{row.get('binding')})")
        ids = oids + pids
        prompt = (
            f"Task {owner}" + (f" (its close-out: {spec['intent']})" if spec.get("intent") else "")
            + "\n\nThe run is finishing. The harness's record on the current "
            "source holds these items of your task, and each is settled by a declaration of where its proof already "
            "lives, not by an edit:\n" + "\n".join(lines) + "\n\n"
            + ("For each NOT RUN item, where cases that exercise the whole reported behaviour exist in the project's "
               "tests as they stand now, write one line 'RUNS: O<n> - file::[Class::]test_case' per case. " if items else "")
            + ("For each scenario, write one line 'PROVES: P<n> - file::[Class::]test_case' per case that proves it. "
               + PROVES_RULE + " " if rows else "")
            + TEST_SUBSTANCE_RULE + f"\nOnly lines naming the ids above ({', '.join(ids)}) are read; a RUNS or PROVES "
            "line that names no id leaves the whole reply unread. Where no such case exists, write no line for that "
            "id: it stays held for an editing task. Do not change files: this call declares; it does not edit. The "
            "harness settles each case only against a check it already ran on this exact source in which that case "
            "executed and passed, and an independent auditor judges whether the cases exercise everything the item "
            "or scenario says.")
        saved = self._active_spec
        self._active_spec = None
        reply = None
        try:
            with invocation(owner, "proof-binding"):
                reply = self._invoke_model(lead, prompt)
        except TurnLimitReached as exc:
            record["outcome"] = f"capped at {exc.turns or 'its'} rounds: no declaration"
            if exc.partial_text:
                record["partial_artifact"] = self.store.put(exc.partial_text, kind=f"done-declaration-capped:{owner}",
                                                            author=lead).id
        except FinishReserved as exc:
            self.finish_refusals.append(dict(task=owner, what="DONE declaration", detail=str(exc)[:300]))
            record["outcome"] = "refused to keep the finish"
        except ProviderError as exc:
            record["outcome"] = f"stopped: {type(exc).__name__}"
            record["error_artifact"] = self.store.put(str(exc), kind=f"done-declaration-error:{owner}",
                                                      author="harness").id
        finally:
            self._active_spec = saved
        if reply is not None:
            record["reply_artifact"] = self.store.put(reply, kind=f"done-declaration:{owner}", author=lead).id
            after = self._declaration_identity()
            if after != before:
                moved = sorted(k for k in before if before[k] != after[k])
                record["outcome"] = f"void: {', '.join(moved)} changed during the call"
            else:
                self._read_done_declaration(owner, lead, reply, oids, pids, record)
        for oid in oids:
            entry = next((e for e in self.unexecuted_acceptance if e.get("oid") == oid), None)
            done = entry is not None and bool(entry.get("restatements")) and self._discharge_record(entry) is not None
            (record["settled"] if done else record["held"]).append(oid)
        for pid in pids:
            row = next((r for r in self.evidence_obligations if r.get("task") == owner and r.get("proof_id") == pid
                        and not r.get("superseded_by")), None)
            done = row is not None and row.get("binding") == "bound" and row.get("execution") == "ran"
            (record["settled"] if done else record["held"]).append(pid)
        self._note(f"DONE declaration by {lead} for {owner}: {record['outcome']}; executed "
                   f"{', '.join(record['settled']) or 'none'}; held {', '.join(record['held']) or 'none'}")
        return record

    def _read_done_declaration(self, owner, lead, reply, oids, pids, record) -> None:
        """The strict reading of one DONE declaration reply (``_declare_at_done``)."""
        runs, proves, unnamed = [], [], []
        for match in _RUNS_ANY.finditer(reply or ""):
            named = re.match(r"\s*(O\d+)\b", match.group(1))
            if named is None:
                unnamed.append(match.group(0).strip()[:120])
            elif named.group(1) in oids:
                runs.append(match.group(0).strip())
            else:
                record["dropped"].append(match.group(0).strip()[:120])
        for match in _PROVES_ANY.finditer(reply or ""):
            named = _PROVES_PID.match(match.group(1))
            if named is None:
                unnamed.append(match.group(0).strip()[:120])
            elif named.group(1) in pids:
                proves.append(match.group(0).strip())
            else:
                record["dropped"].append(match.group(0).strip()[:120])
        problems = [dict(task=owner, role="proof-binding", author=lead,
                         problem=f"DONE declaration: not a listed id's line, not read: {line}")
                    for line in record["dropped"]]
        if unnamed:
            record["outcome"] = "unparseable: a RUNS or PROVES line names no id; nothing was read"
            problems += [dict(task=owner, role="proof-binding", author=lead,
                              problem=f"DONE declaration: names no id, whole reply unread: {line}") for line in unnamed]
        elif not runs and not proves:
            record["outcome"] = "no line for a listed id"
        else:
            saved = self._active_spec
            self._active_spec = SimpleNamespace(task_id=owner, description=record.get("intent") or "")
            try:
                if proves:
                    self._record_proves(lead, "proof-binding", "\n".join(proves))
                if runs:
                    self._record_runs(lead, "proof-binding", "\n".join(runs))
            finally:
                self._active_spec = saved
            record["outcome"] = "declared"
        self.proof_binding_problems.extend(problems)

    def _continuation_admission(self, needs_editing: bool = True, *, naming: bool = True, before=(),
                                scope: str = "done-continuation") -> dict:
        """Whether one more task is authorized and payable now, quoted as
        the route it actually takes (root review of 7a141e3, item 4: the
        quote left out the planner call that names the task): a slot is
        left, no route was forfeited and no terminal route committed, and
        ``_admit_plan`` admits, against what is left after the held finish,
        the planning call that names the task (``_planning_stages`` sizing:
        the larger of the reserve and the last planning call), the task's
        lead draft priced as dispatch prices it (``_plan_stages``: at the
        editing ceiling whenever the run grants writes, since the named task
        is an ordinary task and its lead holds the grant whatever the
        blockers need; root review of bae3830, item 2: a cheaper
        "declaration" quote bought a planner call the dispatch then could not
        follow), one review, and the planning call after it where a slot
        would still be left. Where that ordinary route cannot be paid, the
        terminal alternative dispatch itself would take (admission/3: no
        planning call after it) is quoted too, and either admits (cycle03).
        ``needs_editing`` (whether a blocker needs a code or test change) is
        recorded, never priced: no declaration-only route with a narrower
        grant exists. Known rechecks and the design review follow findings or
        a design scope the quote cannot know; they are admitted on the route
        itself when they arise. With no budget, the slot decides.

        Inside a planning decision (cycle03) the naming call is already
        spent or is the re-ask being priced: ``naming`` False leaves it out,
        and ``before`` names the future calls priced ahead of the route (one
        planner re-ask). Only future calls are priced, against what is left
        now; a spent call is in the budget's reported total already."""
        used = int(getattr(self, "_slots_used", 0) or 0)
        total = getattr(self, "_max_tasks", None)
        if total is not None and used >= total:
            return dict(feasible=False, detail=f"no task slot is left ({used} of {total} used)", kind="slots")
        if self._route_forfeit is not None or self._terminal_committed():
            return dict(feasible=False, detail="the remaining slots were forfeited or relinquished", kind="forfeit")
        if self._budget() is None:
            return dict(feasible=True, detail=f"{(total or 0) - used} slot(s) left", kind="slots")
        snap = self._budget().snapshot()
        reserve = int((snap.get("limits") or {}).get("reserve_tokens_per_call") or 0)
        planning = max(reserve, self._last_planning_tokens() or 0) if reserve else None
        head = list(before)
        if naming:
            head.append(delivery.PlanStage("planning call that names the task", "reserve", tokens=planning))
        # The lead's exposure class is the dispatch's (``_plan_stages``): the
        # write grant, not what the blocker needs, decides it.
        editing = bool(self._writes())
        rows = head + [delivery.PlanStage("continuation lead draft" + ("" if editing else " (read-only)"),
                                          "editing" if editing else "reserve", editing=editing),
                       delivery.PlanStage("continuation review", "reserve")]
        terminal = list(rows)
        # The planning call after the continuation, where a slot would still
        # be left then (the existing sizing rule, one slot ahead).
        if total and used + 1 < int(total):
            rows.append(delivery.PlanStage("next planning call", "reserve", tokens=planning))
        record = scope == "done-continuation"
        verdict = self._admit_plan(scope, rows, scope=scope, record=record)
        route_kind, alternative = "ordinary", {}
        if not verdict.feasible and len(terminal) < len(rows) and self._explicit_tasks is None:
            last = self._admit_plan(scope, terminal, scope=f"{scope} (terminal)", record=record)
            if last.feasible:
                verdict, rows, route_kind = last, terminal, "terminal"
            else:
                alternative = dict(terminal_detail=last.detail, terminal_short=dict(last.short))
        return dict(feasible=bool(verdict.feasible), detail=verdict.detail, kind="budget",
                    route="write-granted" if editing else "read-only", needs_editing=bool(needs_editing),
                    route_kind=route_kind, stages=[r.name for r in rows], short=dict(verdict.short),
                    **alternative)

    def _editing_work(self) -> List[str]:
        """Known remaining work that no DONE, audit or decision can settle and
        only an editing task can (cycle03): a task's last check still failing
        with its finding open, the merge gate failing, capped or failed work
        not continued, an open render finding, or a deterministic blocker
        that needs a code or test change (``_deterministic_blockers``). Review
        findings, owed rechecks and evidence a judgment or the audit can
        settle are not here: a decision path may still reach them."""
        reasons = [f"task {t}'s last check still fails (attempt {a.get('attempt', '?')})"
                   for t, a in self._failed_attempts()]
        if any(f.active and f.kind == "product" for f in self.run_outcome.facts):
            reasons.append("the merge gate still fails")
        partial = sorted(self._partial_from_outcomes() | set(self._partial_tasks))
        if partial:
            reasons.append(f"capped or failed task(s) {', '.join(partial)} not continued")
        reasons.extend(f"render finding {f['id']} is open" for f in self.findings
                       if f.get("status") == "open" and f.get("target"))
        try:
            blocked = self._deterministic_blockers()
        except Exception:  # noqa: BLE001 -- unknown is not known work
            blocked = {}
        if blocked and getattr(self, "_blockers_need_editing", False):
            reasons.extend(f"{rid}: {why[:160]}" for rid, why in blocked.items())
        return reasons

    def _planning_forfeit(self, max_tasks: int, used: int) -> Optional[dict]:
        """Before a planning call (cycle03): when the record holds work only an
        editing task can settle (``_editing_work``) and no continuation of it
        can be paid, neither ordinary nor terminal, counting the planning call
        that would name it, the remaining slots are forfeited through the
        existing route-forfeit record and no planner call is bought to
        discover the refusal. Anything else plans as before: no known editing
        work (a DONE, a verification task or the audit may still settle the
        run), the first plan, no budget, an explicit list, a parallel child.
        The shared budget's own admission stays the final authority on every
        call that is made."""
        if (self._explicit_tasks is not None or not self.history or self._budget() is None
                or getattr(self, "_batch_child", False)):
            return None
        work = self._editing_work()
        if not work:
            return None
        quote = self._continuation_admission(True, scope="planning")
        if quote["feasible"] or quote.get("kind") != "budget":
            return None
        self._planning_refused("before planning", quote, work=work[:3])
        return dict(task=None, site="before planning", detail=str(quote["detail"])[:400], work=work[:3],
                    slots_unused=max(0, max_tasks - used), slots_used=used)

    def _reask_refusal(self, kind: str) -> str:
        """Why a planner re-ask of ``kind`` (FETCH, evidence, an operator
        answer, a correction) cannot be bought inside this planning decision,
        or "" when it can (cycle03). The re-ask is priced at the larger of the
        reserve and the last planning call's size, ahead of the continuation
        the dispatch could pay (``_continuation_admission``, ordinary or
        terminal; the naming call is the re-ask itself). Refused only when
        the record holds work only an editing task can settle
        (``_editing_work``) and, after the re-ask, no continuation of it
        could be paid: the re-ask would cost the repair, or lead nowhere.
        Without such work it is never refused here, whatever an edit would
        cost (root review of d8225fc, item 1: a FETCH on the way to DONE was
        refused at 450 left because a hypothetical edit fit before it): the
        decision may end in DONE, evidence or the audit, and the shared
        budget's own admission decides it. Applies only inside
        a planner decision after the first plan with a budget; the planner
        call already made is in the reported total and is not priced again.
        A refused re-ask makes no call."""
        if (not getattr(self, "_planning_decision", False) or not self.history or self._budget() is None
                or self._explicit_tasks is not None or getattr(self, "_batch_child", False)):
            return ""
        quote = self._reask_quote(f"planner re-ask ({kind})", naming=False)
        if not quote:
            return ""
        self._planning_refused(f"planner re-ask ({kind})", quote)
        return (f"a planner re-ask ({kind}) would leave no payable continuation: {str(quote['detail'])[:300]}")

    def _reask_quote(self, name: str, *, naming: bool) -> Optional[dict]:
        """The refused quote for one more planner call, or None when it may
        be bought (``_reask_refusal``'s rule)."""
        snap = self._budget().snapshot()
        reserve = int((snap.get("limits") or {}).get("reserve_tokens_per_call") or 0)
        reask = delivery.PlanStage(name, "reserve",
                                   tokens=max(reserve, self._last_planning_tokens() or 0) if reserve else None)
        # Only known editing work is protected (root review of d8225fc,
        # item 1): without it the decision may end in DONE, evidence or the
        # audit, and an edit that merely happens to fit is hypothetical, so
        # the shared budget's per-call admission and the finish hold decide.
        if not self._editing_work():
            return None
        after = self._continuation_admission(True, naming=naming, before=(reask,), scope="planning re-ask")
        if after["feasible"] or after.get("kind") != "budget":
            return None
        return after

    def _reask_offer_closed(self) -> str:
        """Why FETCH and evidence requests are not offered in this planning
        decision, or "": the same rule as ``_reask_refusal``, quoted before
        the naming call (cycle03). Stated in the prompt so a closed channel is
        not asked for and then refused."""
        if (not self.history or self._budget() is None or self._explicit_tasks is not None
                or getattr(self, "_batch_child", False)):
            return ""
        quote = self._reask_quote("planner re-ask", naming=True)
        return str(quote["detail"])[:300] if quote else ""

    def _planning_refused(self, site: str, quote: dict, **extra) -> None:
        """Keep one refused planning quote on the record (``planning_refusals``,
        result.json): where, the quote's detail, stages and shortfall, the
        budget's reported tokens and attempts then (cycle03)."""
        budget = self._budget()
        snap = budget.snapshot() if budget is not None else {}
        self.planning_refusals.append(dict(
            site=site, detail=str(quote.get("detail"))[:400], stages=list(quote.get("stages") or []),
            short=dict(quote.get("short") or {}), route_kind=quote.get("route_kind"),
            terminal_short=dict(quote.get("terminal_short") or {}),
            reported_tokens=snap.get("reported_tokens"), at_attempt=snap.get("reserved_attempts"),
            last_planning_tokens=self._last_planning_tokens(), **extra))
        self._note(f"planning quote refused at {site}: {str(quote.get('detail'))[:200]}")

    def _known_incomplete(self) -> str:
        """Why completion is already impossible on the current source with
        no continuation left, or "": an audit already judged exactly this
        source, and deterministic blockers hold that nothing at the cap can
        settle (``_deterministic_blockers``). Unknown state, an audit of
        another source and evidence that can still be settled never count
        (cycle02)."""
        if not self.requirement_audits or not self.project:
            return ""
        current = self._source_fingerprint()
        if not current or self.requirement_audits[-1].get("source") != current:
            # The final audit has not judged this source: it still adds
            # evidence, so the cap asks as before.
            return ""
        blocked = self._deterministic_blockers()
        if blocked and self._explicit_tasks is None:
            # Declaration-only debt is offered to its owners before the cap
            # decides nothing could settle it (cycle05 trial t7).
            self._declare_at_cap()
            blocked = self._deterministic_blockers()
        if not blocked:
            return ""
        return "; ".join(f"{r}: {why[:200]}" for r, why in blocked.items())[:600]

    def _declare_at_cap(self) -> None:
        """The DONE declaration route at the task cap: the same plan, the
        same one call per owner and decision, the same reserve pricing
        against what is left after the held finish, no task slot. Kept on
        ``done_gates`` with site ``cap`` when a call was made, or once per
        source and reason when none could be."""
        blocked = self._deterministic_blockers()
        if not blocked:
            return
        declared, why = self._done_declarations(blocked)
        # A restatement the auditor's own seat wrote is offered to another
        # independent seat at the cap as at DONE (cycle14); once per decision.
        self._judge_restatements("cap")
        source = self._source_fingerprint() if self.project else None
        if declared is None:
            if any(g.get("site") == "cap" and g.get("source") == source and g.get("declaration") == why
                   for g in self.done_gates):
                return
            self.done_gates.append(dict(site="cap", blocked=dict(blocked), audit_asked=True, source=source,
                                        declaration=why))
            return
        held = self._deterministic_blockers()
        self.done_gates.append(dict(site="cap", blocked=dict(blocked), still_held=dict(held), audit_asked=True,
                                    source=self._source_fingerprint() if self.project else None,
                                    declaration=f"asked {', '.join(declared)}; still held: {', '.join(held) or 'none'}"))
