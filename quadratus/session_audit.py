"""The requirements ledger, the audit and the audit findings of the session
engine, as a mixin of Session: what a task covers, the requirements
review, the cross-vendor audit and its citations, and findings kept as
requirement debt until a later task settles them.

The methods moved out of session.py unchanged on 2026-11-11. They reach
the rest of Session only through ``self``. This module never imports
quadratus.session.
The move was redone on 2026-11-12, after the development line was merged
into this file, and took that line's code of the same kind with it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from . import delivery, judgment
from .check_facts import (
    _PASS_ONLY_WORDS,
    _RUNNER_SPAN,
    _WHOLE_GATE,
    _audit_check_facts,
    _check_ids,
    _command_rest,
    _command_words,
    _configured_checks,
    _population_clause,
    _python_case_defs,
    _test_runner_shape,
)
from .delegation import invocation
from .providers import ProviderError
from .reply_parsers import _ANSWER_ID, _DECIDE, _NOT_MET_SEGMENT, _QUOTED_SPAN, _cited_path
from .run_budget import FinishReserved, finishing
from .session_design import _snapshot_files
from .session_prompts import (
    _AUDIT_EXAMPLES,
    _AUDIT_LINE_RULE,
    _AUTHORITY_ORDER,
    _CONTRACT_BOUNDARY_RULE,
    _ERROR_PATH_RULE,
    _EXECUTED_ERROR_RULE,
    _FINISH_REASON_CHARS,
    _HISTORY_BINDING_CHARS,
    _HISTORY_EXAMPLE_CHARS,
    _HISTORY_FOOTER_CHARS,
    _HISTORY_PER_REQUIREMENT,
    _HISTORY_REASON_CHARS,
    _HISTORY_TOTAL_CHARS,
    _OWED_SETTLEMENT_ATTEMPTS,
    _PLAIN_VERDICT_RULE,
    _PROTOCOL_RULE,
    _REPAIR_OUTCOME_RULE,
)
from .session_types import is_design_task
from .verdicts import audit_lines as _audit_lines
from .verdicts import review_reading as _review_reading

# One logger name for the engine, wherever its code lives.
log = logging.getLogger("quadratus.session")


class AuditMixin:
    """Whether the goal's requirements are covered, met and free of open findings.

    Session inherits this; nothing here is used on its own.
    """

    def _finalise_findings(self, exc: BaseException) -> None:
        """A resolution a later task's changes undid must not persist as
        resolved because the run ended on an exception; if the re-check
        itself fails, every resolution is distrusted instead."""
        try:
            self._recheck_resolved_findings()
            self._annotate_open_findings(f"{type(exc).__name__}: {str(exc)[:200]}")
        except Exception as failure:  # noqa: BLE001 -- the original stop is the one reported
            log.warning("could not finalise audit findings after %s", type(exc).__name__, exc_info=True)
            self._distrust_resolutions(f"the findings could not be re-checked after "
                                       f"{type(exc).__name__} ({type(failure).__name__})")

    def _findings_stop_due(self) -> bool:
        """Whether open findings stop the run after a task or batch (map P3.4):
        the projected active findings (quadratus.finding_state) or the
        legacy list, either one. A malformed record fails closed. Where the
        two disagree the closing task records it, so the run cannot count
        as complete."""
        from .finding_state import from_session
        state = from_session(self)
        typed, legacy = bool(state.findings() or state.problems), bool(self.open_findings)
        if typed != legacy and self.task_outcomes:
            note = f"open findings: typed {typed}, legacy {legacy}"
            if note not in self.task_outcomes[-1].mismatches:
                self.task_outcomes[-1].mismatches.append(note)
        return typed or legacy

    def _covers_problem(self, covers) -> str:
        ledger = self.memory.ledger
        if not self.config.requirements_ledger:
            return ""
        if not ledger.requirements:
            return ("No requirements are listed yet. Start the reply with a 'REQUIREMENTS:' block "
                    "numbering the goal's requirements (R1: ...), then the task.")
        if not covers:
            return "The task has no COVERS line."
        unknown = [r for r in covers if r not in ledger.requirements]
        if unknown:
            return f"COVERS names requirements that do not exist: {', '.join(unknown)}."
        return ""

    def _absorb_requirements(self, reply: str) -> str:
        """Take a REQUIREMENTS block into the ledger, once, and any DECIDE
        lines on ambiguous requirements, off the reply."""
        ledger = self.memory.ledger
        for rid, text in _DECIDE.findall(reply or ""):
            if rid in ledger.ambiguous:
                ledger.decisions[rid] = text[:400]
                self._note(f"the orchestrator decided ambiguous {rid}: {text[:120]}")
        found, rest = self._split_requirements(reply)
        if not found:
            return rest
        ledger = self.memory.ledger
        if not ledger.requirements and self.config.requirements_ledger:
            ledger.requirements.update(found)
            self._note(f"the orchestrator numbered {len(found)} requirements")
            self._review_requirements()
        return rest

    def _review_requirements(self) -> None:
        """A second vendor compares the numbered list with the verbatim goal.

        Codex's review: an auditor that checks an incomplete list faithfully
        still passes an incomplete product. Missing requirements are added;
        disputed ones stay open. An unavailable reviewer is recorded, not
        treated as agreement.
        """
        ledger = self.memory.ledger
        reviewer = self._auditor()
        if reviewer is None:
            self.requirement_reviews.append(dict(reviewer=None, result="no reviewer available"))
            return
        # Exactly the texts the prompt carries, frozen before the call: the
        # review is of these bytes, whatever the ledger holds when the reply
        # arrives (Astra's in-flight control on 6bf1afc).
        sent = {rid: str(text) for rid, text in ledger.requirements.items()}
        prompt = (
            f"## Goal (verbatim)\n\n{self.memory.goal.strip()}\n\n## Numbered requirements\n\n"
            + "\n".join(f"{rid}: {text}" for rid, text in sent.items())
            + "\n\nCompare the list with the goal. Reply exactly COMPLETE if it covers everything "
            "the goal asks for and forbids. Otherwise reply only with lines 'ADD: <one testable "
            "requirement the list misses>' (deliverables, interfaces, user interface, documentation, "
            "compatibility with existing behaviour, and must-not constraints) and "
            "'AMBIGUOUS: R<n> - <the two readings>'. Mark a requirement ambiguous only when the goal "
            "supports readings that would build different things: an ambiguous requirement needs an "
            "operator ruling before the run can finish."
        )
        saved = self._active_spec
        self._active_spec = None
        try:
            with invocation("plan", "requirements-review"):
                reply = self._invoke_model(reviewer, prompt, allow_writes=False)
        except FinishReserved as exc:
            # Not reviewed: recorded as a failed review, so DONE cannot stand.
            self._finish_refused(None, "requirements review", exc)
            self.requirement_reviews.append(dict(reviewer=reviewer, result="failed: refused to keep the finish"))
            return
        except ProviderError as exc:
            self.requirement_reviews.append(dict(reviewer=reviewer, result=f"failed: {type(exc).__name__}"))
            return
        finally:
            self._active_spec = saved
        # Only COMPLETE, or nothing but well-formed ADD / AMBIGUOUS lines, is a
        # review. Anything else is recorded as a failed review, which keeps
        # DONE blocked (Codex review of #25: prose used to count as a pass).
        lines = [line.strip() for line in (reply or "").splitlines() if line.strip()]
        added = [m.group(1) for m in (re.fullmatch(r"ADD:\s*(.+)", line) for line in lines) if m]
        disputed = [(m.group(1), m.group(2)) for m in
                    (re.fullmatch(r"AMBIGUOUS:\s*(R\d+)\s*[-:—]\s*(.+)", line) for line in lines) if m]
        well_formed = lines == ["COMPLETE"] or (lines and len(added) + len(disputed) == len(lines)
                                                and all(r in ledger.requirements for r, _ in disputed))
        if not well_formed:
            self.requirement_reviews.append(dict(reviewer=reviewer, result="failed: the reply was not "
                                                 "COMPLETE or only ADD/AMBIGUOUS lines",
                                                 reply=(reply or "")[:400]))
            return
        # From the highest number in use, not the count: a list numbered with
        # gaps (R1, R3, R4) has three entries and already has an R4, which a
        # count-based id overwrote.
        next_id = max((int(r[1:]) for r in ledger.requirements if re.fullmatch(r"R\d{1,9}", r)), default=0) + 1
        texts = {rid: hashlib.sha256(text.strip().encode()).hexdigest() for rid, text in sent.items()}
        proposed: Dict[str, str] = {}
        for text in added:
            while f"R{next_id}" in ledger.requirements:
                # Never over a row that arrived while the review was out.
                next_id += 1
            rid = f"R{next_id}"
            next_id += 1
            ledger.requirements[rid] = text
            ledger.requirement_status[rid] = "open (added by the requirements review)"
            # The reviewer's own proposal, recorded as a proposal: nobody has
            # compared this text with the goal, so it carries no review.
            proposed[rid] = hashlib.sha256(text.strip().encode()).hexdigest()
        for rid, why in disputed:
            ledger.ambiguous[rid] = why[:300]
            ledger.ambiguous_since[rid] = len(ledger.rulings)
        self.requirement_reviews.append(dict(reviewer=reviewer, result="reviewed", added=added,
                                             ambiguous=disputed,
                                             # The exact texts the prompt sent, by digest: a text
                                             # that changed during the call, or later, is unreviewed.
                                             # The review's own additions are its proposals.
                                             texts=texts, proposed=proposed))

    def _requirements_satisfied(self, final: Optional[bool] = None) -> bool:
        """Whether DONE may stand: every requirement covered, then audited met.

        Not satisfied means DONE goes back to the orchestrator with the gap
        named. No requirements listed means the ledger is inactive for this
        run; that is recorded, not invented. An audit that did not run is not
        satisfied either, but it judged nothing: ``_audit_unavailable`` says
        why, no status is written, and the completion decision sends DONE
        back once and then stops for the operator (``AuditUnavailable``).
        """
        if final is None:
            final = bool(getattr(self, "_final_check", False))
        ledger = self.memory.ledger
        # Only the audit this check asks for counts as its answer.
        self._audit_unavailable = ""
        kept = self._final_audit if final else None
        if kept is not None:
            # Evidence a previous episode left pending at the cap (a proof run
            # and never judged, an intent never run) is reconciled first:
            # finished once where the record allows, never replayed, and
            # consumed, so the acceptance on record is current again before
            # coverage is read. Without this the pending record moved the
            # packet, the restore below was skipped, and the resumed cap
            # stopped on 'never covered' before the challenge could finish
            # it (root on 637a7e2). A judgment that may have started stays
            # held and unconsumed, as _finish_pending_evidence leaves it.
            self._finish_pending_evidence()
        if (kept and kept.get("status_before") is not None
                and kept.get("binding") == self._audit_binding(kept.get("ids"))):
            # Resumed at the cap on exactly the evidence packet the final
            # audit judged: the ledger as it stood when that audit was asked,
            # before its own verdicts were written over coverage. A packet
            # that moved since leaves every later status as written.
            ledger.requirement_status.update(kept["status_before"])
        refusal = self._original_tests_at_done()
        if refusal:
            self._done_refusal = refusal
            self._note("DONE sent back: the original tests fail against the delivered source")
            return False
        if not self.config.requirements_ledger:
            return True
        if not ledger.requirements:
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nNo requirements were listed. Before DONE can stand, "
                "reply with a 'REQUIREMENTS:' block numbering the goal's requirements (R1: ...), "
                "then name the next task or reply DONE.")
            self._note("DONE sent back: no requirements listed")
            return False
        if not any(r.get("reviewer") and not str(r.get("result", "")).startswith(("failed", "no reviewer"))
                   for r in self.requirement_reviews):
            self._review_requirements()
            if not any(r.get("reviewer") and not str(r.get("result", "")).startswith(("failed", "no reviewer"))
                       for r in self.requirement_reviews):
                # No second vendor read the list, so nothing was judged. That
                # is the answer an audit that did not run gives, and it takes
                # the same road: one more try at the next DONE, then the
                # operator. A live run with one vendor signed in ended
                # RequirementsUnmet here, three calls after its work was done
                # (2026-11-11).
                missed = str((self.requirement_reviews[-1] if self.requirement_reviews else {}).get("result")
                             or "no review was made")
                self._audit_unavailable = ("the requirement list was not reviewed against the goal by "
                                           f"another vendor: {missed[:200]}")
                self._audit_misses += 1
                self._done_refusal = (
                    "\n\n--- DONE SENT BACK ---\nThe requirement list has not been reviewed against "
                    "the goal by another vendor (no reviewer available, or the review failed). No "
                    "requirement was judged, so none is recorded as not met and nothing needs covering "
                    "again. Reply DONE to have the review tried once more; if it still does not run, the "
                    "run stops for the operator. Name a task only if work really remains.")
                self._note("DONE sent back: requirements not independently reviewed")
                return False
            # The review ran, so the audit that follows starts with its own
            # one retry: a review miss before it is not an audit miss.
            self._audit_misses = 0
            if any(not s.startswith(("covered", "met")) for s in
                   (ledger.requirement_status.get(r, "open") for r in ledger.requirements)):
                return self._requirements_satisfied()
        claimed = (getattr(self, "_explicit_claims", set()) if self._explicit_tasks is not None
                   else set(ledger.requirements))
        if self._explicit_tasks is not None:
            self.explicit["requirements_claimed"] = [r for r in ledger.requirements if r in claimed]
            self.explicit["requirements_unclaimed"] = [r for r in ledger.requirements if r not in claimed]
        unsettled = [r for r in ledger.ambiguous if r in claimed and not ledger.settled(r)]
        if unsettled:
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nThese requirements are ambiguous and not yet settled: "
                f"{', '.join(unsettled)}. Settle each with 'DECIDE: R<n> - <reading and why>', or "
                "ASK the operator only if it is a large-scale production decision.")
            self._note(f"DONE sent back: unsettled ambiguous requirements: {', '.join(unsettled)}")
            return False
        status = ledger.requirement_status
        self._cover_by_receipt(claimed)
        early = None
        if not final and self._explicit_tasks is None:
            # Declaration-only debt is offered to its owning leads before the
            # coverage gate (cycle10, PR73 6722947726: with a requirement
            # uncovered or held NOT MET by the audit, DONE was refused above
            # the declaration route and done_declarations stayed empty while
            # the same binding-only items were re-filed task after task). The
            # plan's own rules decide; uncovered work is not editing debt.
            first = self._deterministic_blockers()
            if first:
                held = dict(first)
                declared, why = self._done_declarations(first)
                early = (declared, why, held)
                if declared is not None:
                    self._note(f"DONE declarations asked {', '.join(declared)} before the coverage gate")
        if final and self._explicit_tasks is None:
            # The cap's final check reaches the same declaration route DONE
            # does (cycle05 trial t7: an admission refusal at the cap went to
            # the goal question and the final audit, and declaration-only
            # debt was never offered to its owners), before the coverage gate
            # as DONE's is (cycle10).
            self._declare_at_cap()
        uncovered = [r for r in ledger.requirements
                     if r in claimed and not status.get(r, "").startswith(("covered", "met"))]
        if uncovered:
            # The same split as the audit refusal below: a requirement whose
            # NOT RUN report nothing can lift is not asked for a task here and
            # told "do not name one" two lines later.
            remedy, stuck = self._not_run_remedy(uncovered)
            nameable = [r for r in uncovered if r not in stuck]
            # A pure test-command claim needs no scenario, only the harness's
            # receipt: say exactly what that receipt lacks, so the repair is
            # the missing run or tests, not a task re-covering the claim
            # (cycle10: R1's population clause).
            receipts = []
            for rid in uncovered:
                if self._test_command_requirement(rid) and not status.get(rid, "").startswith("NOT MET"):
                    _receipt, why = self._command_receipt_citation(rid)
                    if why:
                        receipts.append(f"- {rid}: {why[:300]}")
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nThese requirements are not covered by any finished "
                f"task: {', '.join(uncovered)} (never covered, or found not met by the audit and not "
                "covered again since)."
                + (" Name a task for them (with COVERS), or explain in the task why one cannot be done."
                   if not stuck else
                   f" Name a task for {', '.join(nameable)} (with COVERS), or explain in the task why one "
                   "cannot be done." if nameable else "")
                + ("\nTest-command requirements need no scenario; the harness's receipt of the named command "
                   "covers them once it shows what is missing below:\n" + "\n".join(receipts) if receipts else "")
                + remedy)
            self._note(f"DONE sent back: uncovered {', '.join(uncovered)}")
            return False
        if not final and self._explicit_tasks is None:
            # Before an audit is bought: requirements the record already
            # holds not met on this source, by the same facts that would
            # lower the audit's MET afterwards, go back to the planner with
            # their real routes while an authorized continuation is
            # affordable (cycle02, PR73 6705254133: completion-cycle01 spent
            # a 307k audit and 266k of planning on blockers the harness
            # already held, then could not pay the repair).
            blocked = self._deterministic_blockers()
            needs_editing = bool(getattr(self, "_blockers_need_editing", True))
            declared, declaration = None, ""
            if blocked or early is not None:
                # Declaration-only debt is settled first, by its owning leads,
                # with no planner call and no task slot (cycle05); a mixed set
                # keeps the edit route below and its pricing. Asked once per
                # DONE, before the coverage gate (cycle10).
                if early is not None:
                    declared, declaration, held_before = early
                else:
                    held_before = dict(blocked)
                    declared, declaration = self._done_declarations(blocked)
                if declared is not None:
                    blocked = self._deterministic_blockers()
                    needs_editing = bool(getattr(self, "_blockers_need_editing", True))
                    declaration = (f"asked {', '.join(declared)}; still held: {', '.join(blocked) or 'none'}"
                                   f" (of {', '.join(held_before)})")
            source = self._source_fingerprint() if self.project else None
            again = any(not g.get("audit_asked") and g.get("source") == source
                        and sorted(g.get("blocked") or {}) == sorted(blocked) for g in self.done_gates)
            if declared is not None and not blocked:
                moved = any(str(d.get("outcome", "")).startswith("void") for d in self.done_declarations
                            if d.get("owner") in declared)
                self.done_gates.append(dict(site="done", blocked={}, audit_asked=True, source=source,
                                            declaration=declaration,
                                            why=("the source moved during a declaration, so the record's blockers "
                                                 "are not known on it; the audit judges" if moved else
                                                 "the owning leads' declarations left nothing held; the audit judges")))
            if blocked:
                continuation = self._continuation_admission(needs_editing)
                if continuation["feasible"] and not again:
                    routes = {r: self._evidence_route(r, why) for r, why in blocked.items()}
                    shown = {r: self._shown_reason(r, why) for r, why in blocked.items()}
                    for rid, route in routes.items():
                        self.finish_routing.append(dict(requirement=rid, site="done refused before audit",
                                                        why=blocked[rid][:_FINISH_REASON_CHARS], **shown[rid][1],
                                                        **route))
                    self._done_refusal = (
                        "\n\n--- DONE SENT BACK ---\nNo audit was asked: the harness's record on the current "
                        "source already holds these requirements not met, whatever an audit would say:\n"
                        + "\n".join(f"- {r}: {shown[r][0]}\n  route: {routes[r]['route']}" for r in blocked)
                        + f"\nA continuation is admissible now ({continuation['detail'][:200]}). Name a task "
                        "that takes one of these routes, or reply DONE again to have the independent audit "
                        "judge the rest.")
                    self.done_gates.append(dict(site="done", blocked=dict(blocked), continuation=continuation,
                                                audit_asked=False, source=source, declaration=declaration))
                    self._note(f"DONE sent back before any audit: {', '.join(blocked)} held by the record")
                    return False
                self.done_gates.append(dict(site="done", blocked=dict(blocked), continuation=continuation,
                                            audit_asked=True, source=source, declaration=declaration,
                                            why=("DONE repeated over the same held requirements on this source"
                                                 if again else "no continuation is admissible")))
        if self._explicit_tasks is None:
            # Restated NOT RUN items the auditor cannot judge (it wrote the
            # restatement) are offered to another eligible independent seat
            # before the audit (cycle14).
            self._judge_restatements("final" if final else "done")
        if self._explicit_tasks is not None:
            self._note("requirements no listed task claimed, left unjudged: "
                       + ", ".join(self.explicit["requirements_unclaimed"]))
            asked = dict(status)
            verdicts = self._audit_requirements(ids=self.explicit["requirements_claimed"], final=final) if claimed else {}
        else:
            asked = dict(status)
            verdicts = self._audit_requirements(final=final)
        if self._audit_unavailable:
            # The audit did not happen, so nothing was judged and nothing is
            # written: the statuses stay as the tasks left them. Marking them
            # NOT MET made the orchestrator cover finished work again before
            # the audit was even retried (review 2026-11-10, second half 4).
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nThe independent requirements audit did not run "
                f"({self._audit_unavailable[:300]}). No requirement was judged, so none is recorded as "
                "not met and nothing needs covering again. Reply DONE to have the audit tried once "
                "more; if it still does not run, the run stops for the operator. Name a task only if "
                "work really remains.")
            self._note(f"DONE sent back: the requirements audit did not run ({self._audit_unavailable[:160]})")
            return False
        if final and self._final_audit is not None and "status_before" not in self._final_audit:
            self._final_audit["status_before"] = asked
        unmet = {r: why for r, (ok, why) in verdicts.items() if not ok}
        for rid, (ok, why) in verdicts.items():
            status[rid] = "met (audited)" if ok else f"NOT MET: {why[:160]}"
        if unmet and final:
            unmet = self._final_challenge(unmet)
        if unmet:
            # A requirement held down by a NOT RUN report is not fixed by a
            # task that covers it again: say what lifts each report, and which
            # nothing in this run can (review 2026-11-10, second half 12).
            remedy, stuck = self._not_run_remedy(unmet)
            fixable = [r for r in unmet if r not in stuck]
            routes = {r: self._evidence_route(r, why) for r, why in unmet.items()}
            shown = {r: self._shown_reason(r, why) for r, why in unmet.items()}
            for rid, route in routes.items():
                self.finish_routing.append(dict(requirement=rid, site="done refused",
                                                why=unmet[rid][:_FINISH_REASON_CHARS], **shown[rid][1], **route))
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nAn independent audit found these requirements not met:\n"
                + "\n".join(f"- {r}: {shown[r][0]}" + self._basis_label(r) + f"\n  route: {routes[r]['route']}"
                             for r in unmet)
                # The review batch's sentence first, word for word, then the
                # development line's routes: which id goes on which line and
                # what kind of task each route is.
                + ("" if stuck and not fixable else
                   "\nName a task that fixes " + ("them" if not stuck else ", ".join(fixable))
                   + " (with COVERS). COVERS takes these requirement ids; RESOLVES names only "
                   "finding ids, F<n> or RFT<task>-<n>, never a requirement. Name a repair for a finding on the "
                   "behaviour; for execution debt, a task whose checks run what the NOT RUN item binds. The same "
                   "independent audit judges the repaired source. " + _REPAIR_OUTCOME_RULE)
                + remedy)
            self._note(f"DONE sent back: audit found {', '.join(unmet)} not met")
            return False
        return True

    def _audit_text_artifact(self, audit: dict, text: str) -> Tuple[Optional[str], str]:
        """The artifact of one audit that holds ``text`` verbatim (whitespace
        normalised): its reply, or its completion's reply (an omitted id
        answered on the completion is not in the first reply). ``(None,
        "reply artifact")`` when neither is stored or holds it, so a pointer
        never names a reply that does not contain the words (cycle20)."""
        def norm(value) -> str:
            return " ".join(str(value or "").split())
        want = norm(text)
        for ref, label in ((audit.get("reply_artifact"), "reply artifact"),
                           ((audit.get("completion") or {}).get("reply_artifact"), "completion reply artifact")):
            if not ref or not want:
                continue
            try:
                held = self.store.get(ref)
            except (KeyError, OSError, ValueError):
                continue
            if want in norm(held):
                return ref, label
        return None, "reply artifact"

    def _reason_source(self, rid: str, why: str) -> Tuple[Optional[str], str]:
        """Where the whole of a finish reason can be read (cycle20): the
        audit reply, or that audit's completion reply, that holds this exact
        text (whitespace normalised), found from the audit whose verdict for
        ``rid`` is this reason; never an audit reply that does not contain
        it. A reason the harness wrote or revised (a lowering, a synthesized
        verdict, a record held before any audit) is kept as its own artifact
        and named as the reason as recorded. Returns ``(artifact id or None,
        what it is)``."""
        def norm(text) -> str:
            return " ".join(str(text or "").split())
        want = norm(why)
        for audit in reversed(self.requirement_audits):
            if norm(((audit.get("verdicts") or {}).get(rid) or {}).get("why")) != want:
                continue
            ref, label = self._audit_text_artifact(audit, why)
            if ref:
                return ref, "audit " + label.replace(" artifact", "")
            break
        try:
            return self.store.put(str(why), kind=f"finish-reason:{rid}", author="harness").id, "the reason as recorded"
        except OSError:
            return None, "no stored copy"

    def _shown_reason(self, rid: str, why: str) -> Tuple[str, dict]:
        """The finish reason as the send-back shows it: whole when it fits
        ``_FINISH_REASON_CHARS``, else that many characters and a marker that
        counts what was left out and names the artifact holding the whole
        text, readable with FETCH (cycle20: the cut was silent and named no
        artifact). Returns ``(text, record fields for finish_routing)``."""
        why = str(why or "")
        if len(why) <= _FINISH_REASON_CHARS:
            return why, {}
        ref, label = self._reason_source(rid, why)
        omitted = len(why) - _FINISH_REASON_CHARS
        marker = (f" [... {omitted} more characters in {label} {ref}; read it whole with FETCH: {ref}]" if ref
                  else f" [... {omitted} more characters; {label}]")
        return why[:_FINISH_REASON_CHARS] + marker, dict(why_omitted=omitted, why_artifact=ref, why_source=label)

    def _basis_label(self, rid: str) -> str:
        """How the latest audit's NOT MET for ``rid`` stated its basis, for
        the planner: provenance only, the verdict stands either way."""
        audit = self.requirement_audits[-1] if self.requirement_audits else {}
        basis = (audit.get("bases") or {}).get(rid)
        if basis is None:
            return ""
        if not basis.get("stated"):
            return " (no basis stated by the auditor; the verdict stands)"
        if basis.get("quoted") and not basis.get("quoted_in"):
            return " (its quoted basis was not found in the goal, the requirement or a ruling; recorded only)"
        return ""

    def _cover_by_receipt(self, claimed) -> None:
        """A requirement whose whole claim is that a named test command passes
        (``_test_command_requirement``), never covered by a task (no status,
        or open; not one an audit found NOT MET), is covered for the audit
        when the harness's own exact receipt of that command shows it run
        whole and passed on the current source, gate and environment
        (``_command_receipt_citation``). The fact is the receipt; the MET is
        the independent audit's. Cycle08: the planner dropped R17 and R21
        from COVERS after a sendback and DONE stopped on "not covered" while
        the gate's unittest run was passing. A missing, stale, failed,
        filtered or skipping receipt covers nothing. Kept on
        ``receipt_coverage`` (result.json, checkpoint)."""
        ledger = self.memory.ledger
        status = ledger.requirement_status
        for rid in ledger.requirements:
            current = str(status.get(rid, "") or "open")
            if rid not in claimed or current.startswith(("covered", "met", "NOT MET")):
                continue
            if not self._test_command_requirement(rid):
                continue
            receipt, problem = self._command_receipt_citation(rid)
            if receipt is None:
                continue
            status[rid] = f"covered (harness receipt {receipt['id']}: `{receipt['command']}` passed on the current source)"
            self.receipt_coverage.append(dict(requirement=rid, receipt=receipt["id"], command=receipt["command"],
                                              source=receipt.get("source"), gate=receipt.get("gate")))
            self._note(f"{rid} covered by the harness's receipt {receipt['id']} of `{receipt['command']}`; "
                       "the audit judges it")

    def _auditor(self) -> Optional[str]:
        """A brain-trust member from a vendor other than the orchestrator's,
        or None. Never the seat's own vendor: an audit that shares the
        planner's lineage is not independent (Codex review of #25)."""
        seat_vendor = self.seat().key.partition(":")[0]
        return next((p for p in self.brain_trust
                     if self._available(p) and p.partition(":")[0] != seat_vendor), None)

    def _command_requirement(self, rid: str) -> Optional[List[str]]:
        """The command a requirement's own text names, as argv, or None: a
        backticked command, else a phrase starting with a known runner
        (``python3 -m …``, ``python -m …``, ``pytest …``, ``npm …``,
        ``node …``, ``make …``) up to the end of its sentence."""
        import shlex
        text = str(self.memory.ledger.requirements.get(rid) or "")
        ticked = re.findall(r"`([^`\n]+)`", text)
        runner = _RUNNER_SPAN
        phrase = next((t for t in ticked if re.match(r"\s*" + runner + r"\b", t)), None)
        if phrase is None:
            match = re.search(r"(?<![\w./-])(" + runner + r"\b[^;\n`]*)", text)
            if match is not None:
                # The command is the runner and its own operands, never the
                # words around it: read token by token, structurally
                # (``_command_words``), not by a list of English stop-words
                # (cycle09: "... -s tests successfully before declaring
                # completion" kept "successfully" as an argument).
                phrase = " ".join(_command_words(match.group(1).split()))
        if phrase is None:
            return None
        try:
            argv = shlex.split(phrase.strip())
        except ValueError:
            return None
        return argv or None

    def _test_command_requirement(self, rid: str) -> bool:
        """Whether a requirement's whole claim is that a named test command
        passes (``_TEST_COMMAND_RULE``). Conservative: anything unclear keeps
        its scenario. Both must hold (root review of 63fbef7, PR73
        6718399838: a runner name anywhere in argv exempted three
        behavioural requirements):

        - the requirement names exactly one command, and it has a test
          runner's own shape (``_test_runner_shape``): ``pytest`` or
          ``python -m pytest`` / ``python -m unittest`` followed only by
          options, ``discover``, test paths or ``tests``, or exactly
          ``npm test`` / ``make test``. ``python app.py pytest`` runs an
          application, and a ``-k`` selection is not the whole suite;
        - with that command removed, every word of the requirement is
          run-and-pass vocabulary (``_PASS_ONLY_WORDS``): no behaviour,
          value, endpoint or status is asserted beside it. "refunds are
          atomic" or "malformed JSON returning 400" next to the command
          make it a behaviour, which needs its scenario."""
        text = str(self.memory.ledger.requirements.get(rid) or "")
        argv = self._command_requirement(rid) or []
        if not argv or not _test_runner_shape(argv):
            return False
        rest = _command_rest(text, argv)
        if rest is None:
            return False
        rest, population = _population_clause(rest)
        if population is not None and not population:
            return False
        words = re.findall(r"[a-z0-9']+", rest.lower())
        return bool(words) and all(w.removesuffix("'s") in _PASS_ONLY_WORDS for w in words)

    def _command_population(self, rid: str) -> Optional[List[dict]]:
        """The test population a pure test-command requirement says its run
        includes (``_population_clause``), or None when it names none."""
        text = str(self.memory.ledger.requirements.get(rid) or "")
        rest = _command_rest(text, self._command_requirement(rid) or [])
        return (_population_clause(rest)[1] or None) if rest is not None else None

    def _new_case_problems(self, tests, current: dict, passed: set, text: str) -> List[str]:
        """What a receipt lacks for "the new tests" (cycle11 review, PR73
        6725376982: a new case appended to an existing test file was refused
        as "the run added no test file"). New tests are new test cases, read
        from the run-start bytes (``_original_tests``) and the current source:
        a case name a test file did not define at run start, in a new or a
        changed file. A case whose body is that of a run-start case now gone
        from its file is the old test renamed, never new. Each new case must
        appear among the receipt's executed ids; at least one must exist. A
        changed test file whose cases cannot be read (not Python, or not
        parseable) leaves which cases are new unknown, and that is said."""
        if self._original_tests is None:
            return [f"which tests are new is unknown: the run-start tests were not recorded ({text[:60]})"]
        new, renamed, unknown = [], [], []
        for path in tests:
            before, now = self._original_tests.get(path), current.get(path)
            if before == now:
                continue
            defs_now = _python_case_defs(now) if path.endswith(".py") else None
            defs_before = ({} if before is None else _python_case_defs(before)) if path.endswith(".py") else None
            if defs_now is None or defs_before is None:
                unknown.append(path)
                continue
            gone = {body for name, body in defs_before.items() if name not in defs_now}
            for name, body in defs_now.items():
                if name in defs_before:
                    continue
                (renamed if body in gone else new).append(f"{path}::{name}")
        out = []
        if unknown:
            out.append(f"which cases are new in {', '.join(unknown[:3])} cannot be read from the source")
        if not new and not unknown:
            out.append(f"the run added no test case ({text[:60]})"
                       + (f"; {', '.join(renamed[:3])} renames a run-start test" if renamed else ""))
        out += [f"new case {case} was not executed" for case in new if case not in passed]
        return out

    def _test_command_extra_words(self, rid: str) -> List[str]:
        """For a requirement that names exactly one runner-shaped command but
        is not a pure run-and-pass claim, the words beside the command that
        are outside ``_PASS_ONLY_WORDS``: what the requirement claims besides
        the command passing, for an actionable correction. Empty otherwise."""
        text = str(self.memory.ledger.requirements.get(rid) or "")
        argv = self._command_requirement(rid) or []
        rest = _command_rest(text, argv) if argv else None
        if rest is None or not _test_runner_shape(argv) or self._test_command_requirement(rid):
            return []
        before, population = _population_clause(rest)
        if population:
            rest = before
        return list(dict.fromkeys(w for w in re.findall(r"[a-z0-9']+", rest.lower())
                                  if w.removesuffix("'s") not in _PASS_ONLY_WORDS))

    @staticmethod
    def _command_key(argv) -> Tuple[str, ...]:
        """A command's identity for the citation: the interpreter by its
        public name (``/usr/bin/python3.11`` is ``python``; ``pytest`` is
        ``python -m pytest``, the same rule as project_run._check_identity),
        the harness's own report token removed and, for a pytest command
        only, ``-p no:cacheprovider`` (non-selecting plumbing there); every
        other argument kept exactly: a flag, a path or a filter makes another
        command. For ``unittest discover``, ``-p`` is the discovery pattern
        and is never removed (cycle17: ``-p no:cacheprovider`` there is a
        pattern, and dropping it equated two different discovery commands)."""
        from .integration import REPORT_TOKEN, _is_pytest
        argv = [str(a) for a in argv or ()]
        if not argv:
            return ()
        pytest = _is_pytest(argv)
        head, rest = os.path.basename(argv[0]), argv[1:]
        if re.fullmatch(r"python(\d+(\.\d+)*)?", head):
            head = "python"
        elif head == "pytest":
            head, rest = "python", ["-m", "pytest", *rest]
        out, skip = [], False
        for i, arg in enumerate(rest):
            if skip:
                skip = False
                continue
            if pytest and arg == "-p" and i + 1 < len(rest) and rest[i + 1] == "no:cacheprovider":
                skip = True
                continue
            if (pytest and arg == "-pno:cacheprovider") or REPORT_TOKEN in arg or arg.startswith("--quadratus-report="):
                continue
            out.append(arg)
        return (head, *out)

    def _resolve_citations(self, text: str) -> List[str]:
        """The cited paths and test names that actually exist in the project."""
        if not self.project:
            return re.findall(r"[\w./-]+\.\w+|test_\w+", text)
        root = Path(self.project)
        found = []
        for token in re.findall(r"[\w./-]+\.[A-Za-z]\w*", text):
            path = _cited_path(token.split("::")[0])
            if path and (root / path).is_file():
                found.append(path)
        names = re.findall(r"\btest_\w+", text)
        if names:
            # One index for the whole audit, read from the project's source
            # files: a walk per cited name went through node_modules and
            # .venv every time (review 2026-11-10, second half 22).
            index = self._audit_tests
            if index is None:
                index = self._test_functions()
                if self._auditing:
                    self._audit_tests = index
            found += [index[name] for name in names if name in index]
        return found

    def _test_functions(self) -> Dict[str, str]:
        """Test function name -> ``file::name`` for the project's source: the
        ``def test_*`` lines of files named ``test*.py``, from
        ``Project.files()`` (dependency trees and ``.quadratus`` are not
        source), first file in path order. Bounded: at most 1 MiB per file.
        An unreadable project has no resolvable test names."""
        from .project import Project
        index = {}
        try:
            project = Project(self.project, exclude=self.config.project_excludes)
            # The root the files are listed under, so relative_to cannot raise.
            root, files = project.root, sorted(project.files())
        except Exception:  # noqa: BLE001 -- unreadable: nothing resolves, so nothing is MET by it
            return index
        for path in files:
            if not (path.name.startswith("test") and path.suffix == ".py"):
                continue
            try:
                if path.stat().st_size > 1 << 20:
                    continue
                source = path.read_text(errors="ignore")
            except OSError:
                continue
            for name in re.findall(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test_\w+)", source, re.MULTILINE):
                index.setdefault(name, f"{path.relative_to(root).as_posix()}::{name}")
        return index

    def _authority_context(self, audited: dict, ids=None) -> str:
        """What binds any judgement of the delivered work against the goal:
        the verbatim goal, the requirements judged, the harness's redacted
        check facts and configured checks, the orchestrator's decisions and
        operator rulings, the design evidence and its independent verdicts,
        and what is not shown to have run. The final audit and every
        evidence reassessment read this same context (root review of
        8f68ea0: a reassessment without the rulings or the goal could be
        led by a test to another reading)."""
        ledger = self.memory.ledger
        checks = _audit_check_facts(self.checks[-3:], self._source_fingerprint())
        configured = _configured_checks(self.config.integration_gate) if self.project else ""
        return (
            f"## Goal (verbatim)\n\n{self.memory.goal.strip()}\n\n## Requirements\n\n"
            + "\n".join(f"{rid}: {text}" for rid, text in audited.items())
            + ("\n\nOnly the requirements listed above are audited; the others the goal names were "
               "not claimed by any task in this run and are not judged." if ids is not None else "")
            + (f"\n\n## Latest project checks (run by the harness, oldest first)\n{checks}\n"
               "These are the harness's own executions. Your working copy may lack browsers or other "
               "dependencies the harness has, so a test you cannot run there is not evidence that it "
               "fails. A check that failed on the delivered source is evidence against it. Skipped tests and "
               "checks that did not run are incomplete evidence, and a check on an earlier source cannot "
               "establish the delivered source's result; none of these establishes a pass. Judge each "
               "requirement yourself." if checks else "")
            + (f"\n\n## Configured project checks (the only commands the harness executes)\n{configured}\n"
               "A test command that only a plan, task or model prescribed is not a requirement: judge "
               "the requirement by whether these checks, as executed on the delivered source, exercise "
               "it. A command the goal itself requires is a requirement: if these checks do not run it, "
               "that requirement is NOT MET as a configuration conflict naming the command. A configured "
               "check that is missing, failed, skipped, stale or not run is still not evidence of a "
               "pass." if configured else "")
            + (("\n\n## How ambiguous requirements were settled (judge against these)\n"
                + "\n".join([f"- {r}: decided by the orchestrator: {t}" for r, t in ledger.decisions.items()]
                             + [f"- operator ruling: {r}" for r in ledger.rulings]))
               if ledger.decisions or ledger.rulings else "")
            + (("\n\n## Rendered design evidence\n" + "\n".join(
                f"- {d['task']}: " + (", ".join(d.get('screenshots') or []) or d.get('problem', ''))
                + (f"; independent design review by {d['final_review'].get('reviewer')}: "
                   f"{_review_reading(d['final_review'])}" if d.get('final_review') else
                   "; no independent design review verdict on record")
                for d in self.design_checks)
                + "\nA design review verdict listed above is the independent approval of that task's "
                "renders; where one reads APPROVED, do not report the approval as absent.")
               if self.design_checks else "")
            + self._not_run_block()
            + self._unmeasured_block()
            + self._review_notes_block(audited)
            + self._obligation_view(ids=list(audited))
            + "\n\n" + _CONTRACT_BOUNDARY_RULE)

    def _audit_requirements(self, ids: Optional[List[str]] = None, final: bool = False) -> dict:
        """One read-only call: each requirement met or not, with evidence.
        ``ids`` restricts the audit to those requirements (an explicit run
        audits only what its listed tasks claimed); every listed requirement
        otherwise."""
        ledger = self.memory.ledger
        audited = {r: t for r, t in ledger.requirements.items() if ids is None or r in ids}
        self._refresh_obligations()
        kept = self._final_audit if final else None
        if kept and kept.get("binding") and kept.get("binding") == self._audit_binding(ids):
            # Resumed at the cap: the final audit of exactly this source,
            # requirement texts, gate, evidence, findings and policy is on
            # record, and asking again would be a second draw.
            self._note("the final audit of this exact state is on record; it is not asked again")
            budget = self._budget()
            if budget is not None and hasattr(budget, "release_finish"):
                # A hold renewed for a repair that changed nothing the audit
                # judged is not spent on a second draw of the same verdict.
                budget.release_finish("audit", "the final audit of this exact state is on record")
            return {r: (bool(v[0]), str(v[1])) for r, v in kept["verdicts"].items()}
        self._audit_unavailable, self._auditing, self._audit_tests = "", True, None
        carried = [r for r in self.review_findings if r.get("audit_carried")]
        standing = [r for r in self.review_findings if not r.get("closed") and not r.get("audit_carried")]
        obligations_rows = list(self.evidence_obligations)
        auditor = self._auditor()
        if auditor is None:
            self._audit_did_not_run(None, "no auditor from another vendor available")
            return {r: (False, "no auditor from another vendor available") for r in audited}
        packet_before = self._packet()
        required = self._audit_required_ids(audited, carried, obligations_rows, auditor)
        evidence = (
            self._authority_context(audited, ids)
            + self._evidence_block()
            + self._findings_block(carried, standing)
            + self._obligations_block(obligations_rows)
            + self._audit_history(list(audited), judge=True)
            + self._boundary_notes_block(list(audited)))
        instructions = (
            "\n\nYou are an independent auditor. The project in your working directory is the "
            "delivered work. The goal above is the authority; the requirements are this run's reading "
            "of it, and the decisions and rulings listed above bind. Where a requirement's wording "
            "states a condition the goal, a decision and a ruling do not, judge the goal's own "
            "condition and say so on the line ('wording stricter than the goal: <condition>'); a "
            "condition the goal does state is never relaxed this way. " + _AUTHORITY_ORDER + " "
            + _PROTOCOL_RULE + " " + _ERROR_PATH_RULE + " " + _EXECUTED_ERROR_RULE + " "
            + "For each requirement, check the delivered files themselves: code, "
            "interface, tests and documentation. Documentation must agree with the goal, not only "
            "with the code. A requirement about existing product behaviour is met only if it works "
            "with what the product actually does today. A user-interface requirement is met only "
            "with rendered evidence above. Reply with exactly the lines listed under 'Lines this reply "
            "must contain' below, one per id, and no other line that starts with an id. A requirement's "
            "line is 'R1: MET - <file path or test name that shows it>' or "
            "'R1: NOT MET - <what is missing or contradicts the goal>; basis: <the words of the goal, the "
            "requirement or a ruling it contradicts, quoted, and how the delivered behaviour contradicts "
            "them>; evidence: <executed reproduction | source reading | untested risk>; at <file and "
            "function>; example: <each distinct input or call you found and the wrong result it gives, at "
            "most " + str(_AUDIT_EXAMPLES) + ", separated by ' | ', or none for a qualitative requirement>'. Name "
            "only examples you found; one is enough, and none is asked for beyond them. The original goal outranks a requirement's generated wording. A "
            "MET without a cited "
            "file or test is counted as not met. " + _AUDIT_LINE_RULE + " A citation resolves only as a project-relative "
            "file path with an extension, or a test function named test_* defined in a file "
            "whose name starts with test and ends in .py; other names do not resolve."
            + ("" if not carried else
               " Then one line per review finding carried to you, by its id: '<id>: INVALIDATED - <the "
               "test, file, receipt or command showing it never held>' or '<id>: UNRESOLVED <why it "
               "stands>'; a finding you do not judge stays open.")
            + ("" if not any(o.get("execution") == "ran" and delivery.purpose_of(o) == "acceptance"
                             for o in obligations_rows) else
               " Each executed acceptance obligation listed above needs its own line, '<O-id>: SUFFICIENT - "
               "<what the executed case shows for that requirement and scenario>' or '<O-id>: INSUFFICIENT "
               "<what it does not establish>'. Your verdict on a requirement never judges a row it binds: a "
               "row left unjudged keeps its requirement not met. An execution-debt row listed under its own "
               "heading is not judged; a line about one is recorded and applied to nothing.")
            + ("" if not any(o.get("modality") in ("legacy", "reading") for o in obligations_rows) else
               " Each legacy acceptance or reading row listed above needs its own '<O-id>: SUFFICIENT - "
               "<evidence read, citing the file>' or '<O-id>: INSUFFICIENT <gap>' line; it is never judged by "
               "your verdict on its requirement.")
            + ("" if not any(k == "restatement" for _, k in required) else
               " Each executed restatement listed under its own heading above needs its own '<O-id>: SUFFICIENT - "
               "<how the cases exercise the whole reported behaviour>' or '<O-id>: INSUFFICIENT <what they leave "
               "unexercised>' line; your verdict on a requirement it holds never judges it.")
            + ("" if not (carried or obligations_rows or any(k == "restatement" for _, k in required)) else
               " " + _PLAIN_VERDICT_RULE)
        )
        prompt = evidence + instructions + self._audit_lines_block(required)
        saved = self._active_spec
        self._active_spec = None
        audit_binding = self._judgment_binding(SimpleNamespace(task_id="audit"), auditor, "audit")
        failed = False
        try:
            with invocation("audit", "auditor"), finishing("audit"):
                reply = self._invoke_model(auditor, prompt)
        except ProviderError as exc:
            # An audit that could not run is not a pass, and it is not a
            # verdict or a final audit on record either: the next episode
            # asks again. Budget stops and every other exception propagate
            # as from any other call.
            self._audit_did_not_run(auditor, f"the auditor call failed: {type(exc).__name__}: {str(exc)[:160]}",
                                    failed=True)
            return {r: (False, "the auditor call failed") for r in audited}
        finally:
            self._active_spec = saved
        reply_ref = self.store.put(reply or "(no reply)", kind=f"audit-reply:{auditor}", author=auditor).id
        omitted = [] if failed else self._audit_omitted(reply or "", required)
        completion = None
        # A reply with no verdict for any audited requirement judged nothing:
        # it is an audit that did not run, retried whole at the next DONE,
        # so no completion is spent on it (a completion of every id would be
        # a second audit under another name).
        judged_any = not audited or any(rid in audited for rid, _, _ in _audit_lines(reply or ""))
        if omitted and judged_any:
            # One same-auditor completion of exactly the omitted ids, on the
            # job's shared judgment allowance (cycle05): never a second audit
            # of the answered ids, never an inference from a requirement's
            # MET, and nothing when the allowance or the seat is unavailable.
            reply, completion = self._complete_audit(auditor, reply or "", omitted, required,
                                                     evidence + instructions)
        packet_after = self._packet()
        if packet_after.digest != packet_before.digest or (self.project and packet_before.source is None):
            # Anything the verdicts are about moved while they were made:
            # the source, a requirement's text, the gate, the environment, a
            # check, a finding or an obligation. Void, never partially kept:
            # an audit that did not run, so no status is written from it.
            moved = ("the tree changed during the audit" if packet_after.source != packet_before.source
                     or packet_before.source is None else "the evidence packet changed during the audit")
            self._audit_did_not_run(auditor, f"void: {moved}", reply_artifact=reply_ref,
                                    required=[i for i, _ in required], omitted=omitted, completion=completion)
            return {r: (False, moved) for r in audited}
        found, stated = {}, []
        citation_receipts: Dict[str, dict] = {}
        # One strict reading, by the one verdict reader
        # (quadratus.verdicts.audit_lines): the last line no longer wins, so
        # NOT MET then MET is no verdict for that requirement, and no verdict
        # is counted as not met.
        for rid, met, why in _audit_lines(reply):
            if not met:
                # The auditor's own NOT MET lines: the findings the evidence
                # loop may test (never a lowered or uncited MET). The reader
                # returns a line it refused as no verdict the same way, so
                # that id is listed here too, with the reader's reason.
                stated.append(rid)
            if met:
                resolved = self._resolve_citations(why)
                if not resolved:
                    # A requirement that names a command is shown by the
                    # harness's own exact receipt of that command on the
                    # current source (evidence lifecycle batch, 2026-10-29:
                    # "run the suite before declaring completion" was lowered
                    # as uncited while the gate had run it five times).
                    receipt, problem = self._command_receipt_citation(rid)
                    if receipt is not None:
                        citation_receipts[rid] = receipt
                        why = (f"{why} (harness receipt {receipt['id']}: `{receipt['command']}` passed on the "
                               f"current source)")[:400]
                    else:
                        met, why = False, (f"MET claimed without a file or test that exists in the project ({why[:100]})"
                                           + (f"; {problem}" if problem else ""))
            found[rid] = (met, why)
        if audited and not any(r in found for r in audited):
            # A reply with no verdict for any audited requirement judged
            # nothing; one missing among others is still that one's NOT MET.
            # Decided before the reply's other lines are applied: an audit
            # that did not run settles no finding, row or restatement.
            self._audit_did_not_run(auditor, "the auditor's reply held no verdict for any audited requirement",
                                    reply=(reply or "")[:400], reply_artifact=reply_ref,
                                    required=[i for i, _ in required], omitted=omitted, completion=completion)
            return {r: (False, "the auditor gave no verdict") for r in audited}
        if carried:
            # The auditor's per-id dispositions on the findings carried to it
            # land first: a finding it invalidates on evidence no longer
            # lowers the requirement it touched.
            reading = judgment.recheck_verdicts(reply or "", [r["fid"] for r in carried])
            self._apply_review_reading(SimpleNamespace(task_id="audit"), auditor, "audit", reading, audit_binding)
            for record in self.review_findings:
                if record["fid"] in reading.dispositions and record.get("closed"):
                    self._close_review_finding_by_id(record["fid"], f"{record['state']} by the final audit")
        original = dict(found)
        # Exactly the restatements the audit was asked to judge (cycle18): a
        # line about any other item, its own restatement included, is not read.
        self._judge_restated(auditor, reply or "", oids=[i for i, k in required if k == "restatement"])
        lowered = self._lower_unexecuted(found)
        lowered += self._lower_unexercised_errors(found, audited, original=original, lowered=lowered)
        # Sufficiency is implied from the auditor's own lines (``original``),
        # never from a verdict lowered above for another record.
        self._judge_obligations(auditor, reply or "", found, ids=list(audited))
        # The one acceptance predicate, after sufficiency is judged: what the
        # auditor wrote stands only where the record agrees.
        lowered += self._apply_acceptance(found, audited)
        self._auditing, self._audit_tests = False, None
        self._audit_misses = 0
        verdicts = {r: found.get(r, (False, "the auditor gave no verdict")) for r in audited}
        stated_reasons = {r: original[r][1] for r in stated if r in audited and r in original}
        revisions_now = self._requirement_revisions()
        self.requirement_audits.append(dict(auditor=auditor, verdicts={r: dict(met=ok, why=why)
                                                                       for r, (ok, why) in verdicts.items()},
                                            lowered=lowered, stated=[r for r in stated if r in audited],
                                            citation_receipts=citation_receipts,
                                            source=self._source_fingerprint() if self.project else None,
                                            findings_carried=[r["fid"] for r in carried],
                                            findings_standing=[r["fid"] for r in standing],
                                            acceptance=dict(self._last_acceptance), packet=packet_after.digest,
                                            failed=failed, reply_artifact=reply_ref,
                                            required=[i for i, _ in required], omitted=omitted,
                                            completion=completion,
                                            # The auditor's own NOT MET words, whole, with the
                                            # requirement revision judged and the basis as stated:
                                            # what later seats are shown as history (cycle12).
                                            stated_reasons=stated_reasons,
                                            revisions={r: int(revisions_now.get(r, 1)) for r in audited},
                                            bases={r: self._not_met_basis(r, why)
                                                   for r, why in stated_reasons.items()}))
        if not failed:
            # Every audit records the packet it was about (the cap's final
            # audit records the same binding below, settled), so the record
            # shows whether two audits judged the same packet. No DONE-route
            # reuse is built on it: a DONE repeated over nothing is sent back
            # by the coverage check before any audit, and a task that
            # re-covers a requirement closes with receipts the auditor
            # consumes, so its packet is new (root's correction on 0c5c6f1,
            # C1; the source-only cache tried in that batch was refused by
            # tests/test_boundary_records.py).
            self.requirement_audits[-1]["binding"] = self._audit_binding(ids)
        if final and not failed:
            # Two packets, both named: the one the auditor was handed
            # (``packet``) and the one its own dispositions settled
            # (``packet_settled``), which the binding and the acceptance
            # are about.
            binding = self._audit_binding(ids)
            settled = self._packet().digest
            self.requirement_audits[-1].update(binding=binding, packet_settled=settled)
            self._final_audit = dict(source=self.requirement_audits[-1]["source"],
                                     ids=sorted(ids) if ids is not None else None,
                                     binding=binding, packet=settled,
                                     verdicts={r: [ok, why] for r, (ok, why) in verdicts.items()},
                                     acceptance=dict(self._last_acceptance))
        return verdicts

    def _audit_did_not_run(self, auditor, reason: str, **kept) -> None:
        """Record an audit that judged nothing: no auditor, a failed call, a
        tree that moved under it, or a reply with no verdict at all.

        It is its own result. ``_requirements_satisfied`` reads
        ``_audit_unavailable`` and leaves every status as it was; the caller
        still gets all-False verdicts, so nothing can read this as met. The
        record carries ``ran=False`` for the run's readers (truth_check)."""
        self._auditing, self._audit_tests = False, None
        self._audit_unavailable = reason
        self._audit_misses += 1
        self.requirement_audits.append(dict(auditor=auditor, result=reason, ran=False, **kept))
        self._note(f"the requirements audit did not run: {reason[:200]}")

    _AUDIT_FORMS = {
        "requirement": "MET or NOT MET",
        "finding": "INVALIDATED or UNRESOLVED",
        "row": "SUFFICIENT or INSUFFICIENT",
        "restatement": "SUFFICIENT or INSUFFICIENT",
    }
    _AUDIT_KINDS = {
        "requirement": "requirements",
        "finding": "review findings carried to you",
        "row": "acceptance rows",
        "restatement": "executed restatements",
    }

    def _audit_lines_block(self, required, *, completion: bool = False) -> str:
        """The audit's one list of required lines (cycle05), grouped by kind
        with the form each takes."""
        lines = []
        for kind in ("requirement", "finding", "row", "restatement"):
            ids = [i for i, k in required if k == kind]
            if ids:
                lines.append(f"- {self._AUDIT_KINDS[kind]} ({self._AUDIT_FORMS[kind]}): {', '.join(ids)}")
        head = ("One line for each of these ids only, in the form above for its kind. A line for any other id is "
                "not read, and your other answers stand as you gave them." if completion else
                "One line for each id below, in the form above for its kind. An id with no line is an unanswered "
                "question, never an approval.")
        return "\n\n## Lines this reply must contain\n" + head + "\n" + "\n".join(lines)

    @staticmethod
    def _answer_id(line: str) -> Optional[str]:
        """The id a reply line answers (its leading '<id>:'), upper-cased, or None."""
        hit = _ANSWER_ID.match((line or "").replace("**", ""))
        return hit.group(1).upper() if hit else None

    def _audit_omitted(self, reply: str, required) -> List[str]:
        """The required ids no line of the reply answers at all (cycle05). A
        line that names the id in a form its reader refuses is an answer, read
        as the reader reads it (no verdict), never an omission to re-ask."""
        answered = {self._answer_id(line) for line in (reply or "").splitlines()}
        # A requirement line the one audit-line reader read is answered too:
        # that reader forgives formatting this id match does not (a heading
        # mark, italics), and asking again could add a second, different
        # line, which is no verdict.
        answered |= {rid for rid, _, _ in _audit_lines(reply or "")}
        return [i for i, _ in required if i.upper() not in answered]

    def _complete_audit(self, auditor: str, reply: str, omitted: List[str], required, body: str):
        """The one same-auditor completion of an audit's omitted ids
        (``_complete_judgment``: same seat, the auditor role, read-only, the
        bound identity compared before and after, the job's shared
        ``max_judgment_completions`` claimed first and never refunded), run
        as the audit's own finishing stage. Only lines answering an omitted
        id are appended to the reply; any other line is dropped and counted.
        Returns ``(reply, completion record)``."""
        spec = SimpleNamespace(task_id="audit")
        kinds = dict(required)
        asked = [(i, kinds[i]) for i in omitted]
        short = self._audit_completion_short()
        if short:
            # Priced before the claim: a call the budget cannot hold would
            # reach its latched limit and stop the run, so none is made and
            # the allowance is not spent (cycle05).
            completion = dict(asked=list(omitted), outcome=f"not attempted: {short}", answered=[],
                              still_omitted=list(omitted), dropped_lines=0, raw_artifact=None, reply_artifact=None)
            self._note(f"audit completion by {auditor} not attempted: {short}")
            return reply, completion
        context = ("Your audit reply on this exact packet is on record and stands; it is not repeated here, and "
                   f"none of its answers is asked again. It gave no line for: {', '.join(omitted)}.\n\n")
        answer = None
        try:
            with finishing("audit"):
                answer = self._complete_judgment(
                    spec, auditor, "audit", reply, [f"a line for each of {', '.join(omitted)}"], role="auditor",
                    prompt=body + self._audit_lines_block(asked, completion=True), context=context,
                    outcome=self.run_outcome)
        except ProviderError as exc:
            self._note(f"audit completion by {auditor} failed: {type(exc).__name__}")
        record = next((r for r in reversed(self.judgment_completions)
                       if r.get("task") == "audit" and r.get("seat") == auditor and r.get("kind") == "audit"), {})
        wanted = {i.upper() for i in omitted}
        kept, dropped = [], 0
        for line in (answer or "").splitlines():
            if self._answer_id(line) in wanted:
                kept.append(line)
            elif self._answer_id(line) is not None:
                dropped += 1
        answered = [i for i in omitted if i.upper() in {self._answer_id(line) for line in kept}]
        if record and record.get("outcome") == "answered":
            record["judgment"] = ("answered every omitted id" if len(answered) == len(omitted)
                                  else f"still no line for {', '.join(i for i in omitted if i not in answered)}")
        completion = dict(asked=list(omitted), outcome=str(record.get("outcome") or "not attempted"),
                          answered=answered, still_omitted=[i for i in omitted if i not in answered],
                          dropped_lines=dropped, raw_artifact=record.get("raw_artifact"),
                          reply_artifact=record.get("reply_artifact"))
        self._note(f"audit completion by {auditor}: asked {', '.join(omitted)}; {completion['outcome']}; "
                   f"answered {', '.join(answered) or 'none'}")
        return (reply + ("\n" + "\n".join(kept) if kept else "")), completion

    def _audit_completion_short(self) -> str:
        """Why the budget cannot hold one more call of the audit's finishing
        stage now, or "": a call left, the reserve in tokens and some wall
        time (the audit is the last finishing stage, so nothing later is
        held for). No budget, nothing to price."""
        budget = self._budget()
        if budget is None:
            return ""
        snap = budget.snapshot()
        limits = snap.get("limits") or {}
        reserve = int(limits.get("reserve_tokens_per_call") or 0)
        if int(limits.get("max_calls") or 0) - int(snap.get("reserved_attempts") or 0) < 1:
            return "no call is left in the run's budget"
        if reserve and int(limits.get("max_reported_tokens") or 0) - int(snap.get("reported_tokens") or 0) < reserve:
            return "the tokens left do not hold one reserve-sized call"
        if float(limits.get("wall_seconds") or 0) - float(snap.get("elapsed_seconds") or 0) <= 0:
            return "no wall time is left"
        return ""

    def _audit_binding(self, ids: Optional[List[str]]) -> str:
        """What a final audit's verdicts are about: the audited ids and the
        whole evidence packet (``_packet``: source, requirement revisions
        and texts, gate, environment, evidence identity, every recorded
        check, the findings with the binding each was last judged on, the
        obligations with their receipts, rulings, decisions and the approval
        authority). A cached audit, a status snapshot or a reassessment is
        reused only on exactly this (Astra audit-cache finding; root's
        cache, binding and TOCTOU findings on f4fe5b7)."""
        # Beside the packet: what the audit prompt carries that is not
        # evidence (the reviewers' notes, the judgment gaps on record) still
        # changes what the auditor was asked, so a cached audit is not
        # reused across it; these are not judgment bindings, so a recheck's
        # own completion gap never drifts its reading.
        body = dict(ids=sorted(ids) if ids is not None else None, packet=self._packet().digest,
                    notes=hashlib.sha256(json.dumps(list(getattr(self, "review_notes", []) or []), sort_keys=True,
                                                    default=str).encode()).hexdigest()[:16],
                    gaps=hashlib.sha256(json.dumps(list(getattr(self, "judgment_gaps", []) or []), sort_keys=True,
                                                   default=str).encode()).hexdigest()[:16],
                    policy="audit/5")
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]

    def _apply_acceptance(self, found: dict, audited) -> List[dict]:
        """The one acceptance predicate (delivery.acceptance) over the
        verdicts as lowered: a MET stands only when every obligation bound
        to the requirement has an authorized runner, ran on the current
        identity and was judged sufficient about exactly that evidence, and
        no review finding touching it is open. What it lowers is recorded
        with the judge's own words; the whole result is kept for the
        record (``_acceptance_record``)."""
        covers = {o.task_id: list(o.covers or ()) for o in self.task_outcomes}
        accepted = delivery.acceptance({r: found[r] for r in found if r in audited},
                                       self.evidence_obligations, self.review_findings, covers,
                                       current_requirements=self._requirement_revisions())
        lowered = []
        for rid, verdict in accepted.items():
            met, why = found[rid]
            if met and not verdict.met:
                reason = ("acceptance predicate: " + "; ".join(verdict.reasons))[:300]
                found[rid] = (False, reason)
                lowered.append(dict(requirement=rid, auditor_said=why[:200], reason=reason))
        self._last_acceptance = {rid: dict(met=v.met, reasons=list(v.reasons)) for rid, v in accepted.items()}
        return lowered

    def _acceptance_record(self) -> Optional[dict]:
        """The final audit's acceptance as committed with the terminal
        state: the packet digest it was decided on, whether that packet is
        still the current one, and each audited requirement's state; None
        when no final audit is on record."""
        if self._final_audit is None:
            return None
        return dict(binding=self._final_audit.get("binding"), packet=self._final_audit.get("packet"),
                    packet_audited=self._final_audit.get("packet_audited", self._final_audit.get("packet")),
                    consumed=list(self._final_audit.get("consumed") or []),
                    current=self._final_audit_current(),
                    requirements=dict(self._final_audit.get("acceptance") or {}))

    def _final_audit_current(self) -> bool:
        """Whether the acceptance on record (the final audit, as the
        evidence it has consumed since left it) is about exactly the
        current packet."""
        kept = self._final_audit
        return bool(kept and kept.get("binding") and kept.get("binding") == self._audit_binding(kept.get("ids")))

    # -- audit history and observational records (cycle12) -----------------------
    @staticmethod
    def _not_met_segments(why: str) -> Dict[str, str]:
        """A NOT MET line's labelled segments (``basis``, ``evidence``,
        ``at``, ``example``) and the explanation before them, as written."""
        text = str(why or "")
        marks = list(_NOT_MET_SEGMENT.finditer(text))
        out = {"explanation": text[:marks[0].start()] if marks else text}
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            out.setdefault(mark.group(1).lower(), text[mark.end():end].strip())
        return {k: v.strip() for k, v in out.items()}

    def _not_met_basis(self, rid: str, why: str) -> dict:
        """The basis an auditor stated for a NOT MET, as provenance only
        (cycle12, root's adjustment): what it quoted, its evidence kind as
        written, and which texts the quoted words occur in. An occurrence
        never validates the claim and its absence never discards it; the
        verdict stays the auditor's."""
        parts = self._not_met_segments(why)
        basis, evidence = parts.get("basis", ""), parts.get("evidence", "")
        quoted = [q.strip() for match in _QUOTED_SPAN.finditer(basis) for q in match.groups() if q and q.strip()]
        ledger = self.memory.ledger
        texts = [("goal", self.memory.goal), (rid, ledger.requirements.get(rid, ""))]
        texts += [(f"ruling {i + 1}", r) for i, r in enumerate(ledger.rulings)]
        texts += [(f"decision {k}", v) for k, v in ledger.decisions.items()]

        def norm(text: str) -> str:
            return " ".join(str(text or "").lower().split())
        found = sorted({name for q in quoted for name, text in texts if norm(q) and norm(q) in norm(text)})
        return dict(basis=basis[:600], evidence=evidence[:80], quoted=[q[:200] for q in quoted][:6],
                    quoted_in=found, stated=bool(basis))

    def _audit_source(self):
        """The audits, ledger and revisions this seat reads history from: a
        parallel child reads its parent's (its own ledger is off)."""
        parent = getattr(self, "_authority_parent", None)
        owner = parent if parent is not None and not self.requirement_audits else self
        return owner.requirement_audits, owner.memory.ledger, owner

    def _audit_history(self, rids, *, judge: bool = False) -> str:
        """Earlier audits' own NOT MET reasons for ``rids``, as historical
        questions (cycle12; cycle11 run 20261108T191017Z: each audit found
        one more defect of R16 and R18 and nothing showed the repair's lead,
        its reviewer or the next audit what earlier audits had asked).
        Read from ``requirement_audits`` only, never a second history: the
        auditor's own words (a harness lowering is not a counterexample),
        deduplicated, with the auditor, the source and requirement revision
        judged, the reply artifact, whether a later audit found it MET, and
        a decision or ruling naming it. Bounded per requirement, per reason
        and in all, the example kept apart from the explanation so a long
        explanation never cuts it, and every omission counted. Nothing here
        is a current finding, an owner, proof or a new requirement."""
        audits, ledger, owner = self._audit_source()
        rids = [r for r in dict.fromkeys(rids or ()) if r]
        if not audits or not rids:
            return ""
        current = self._source_fingerprint() if self.project else None
        revisions = owner._requirement_revisions()
        entries: Dict[str, List[dict]] = {rid: [] for rid in rids}
        index: Dict[tuple, dict] = {}
        for number, audit in enumerate(audits, 1):
            verdicts = audit.get("verdicts") or {}
            if audit.get("failed") or not verdicts:
                continue
            stated = audit.get("stated_reasons")
            if stated is None:
                # An audit recorded before cycle12: its own NOT MET lines are
                # the ids it stated, with the verdict text kept for them.
                stated = {r: (verdicts.get(r) or {}).get("why", "") for r in audit.get("stated") or ()}
            for rid in rids:
                verdict = verdicts.get(rid)
                if verdict is None:
                    continue
                if verdict.get("met"):
                    for entry in entries[rid]:
                        entry.setdefault("met_later", (number, audit.get("source")))
                    continue
                why = str(stated.get(rid) or "").strip()
                if not why:
                    continue
                key = (rid, " ".join(why.lower().split()))
                if key in index:
                    index[key]["again"].append(number)
                    index[key].pop("met_later", None)
                    continue
                artifact, kind = owner._audit_text_artifact(audit, why)
                entry = dict(number=number, auditor=audit.get("auditor") or "unknown", source=audit.get("source"),
                             revision=(audit.get("revisions") or {}).get(rid), artifact=artifact, kind=kind,
                             why=why, again=[], basis=(audit.get("bases") or {}).get(rid))
                index[key] = entry
                entries[rid].append(entry)
        if not any(entries.values()):
            return ""

        def clip(text: str, limit: int, artifact) -> str:
            text = " ".join(str(text or "").split())
            if len(text) <= limit:
                return text
            return text[:limit] + f" [... {len(text) - limit} more characters in artifact {artifact or 'unavailable'}]"

        def render(rid: str, entry: dict) -> str:
            parts = self._not_met_segments(entry["why"])
            where = ("this source" if current and entry["source"] == current else
                     f"an earlier source {str(entry['source'])[:12]}" if entry["source"] else "an unrecorded source")
            now = int(revisions.get(rid, 1))
            rev = entry["revision"]
            revised = ("requirement revision not recorded" if rev is None else
                       f"requirement revision {rev}" + (f", revised since (now {now}): judge the current wording"
                                                        if int(rev) != now else ""))
            line = (f"- {rid}: audit {entry['number']} by {entry['auditor']} on {where} ({revised}; {entry['kind']} "
                    f"{entry['artifact'] or 'unavailable'}): "
                    + clip(parts.get("explanation", ""), _HISTORY_REASON_CHARS, entry["artifact"]))
            basis = entry.get("basis") or {}
            line += (f" Basis as stated: {clip(parts['basis'], 300, entry['artifact'])}." if parts.get("basis")
                     else " No basis was stated.")
            if parts.get("evidence"):
                line += f" Evidence as stated: {clip(parts['evidence'], 80, entry['artifact'])}."
            if basis.get("quoted") and not basis.get("quoted_in"):
                line += " Its quoted words were not found in the goal, the requirement or a ruling (recorded only)."
            if parts.get("at"):
                line += f" At: {clip(parts['at'], 200, entry['artifact'])}."
            if parts.get("example"):
                line += f" Example: {clip(parts['example'], _HISTORY_EXAMPLE_CHARS, entry['artifact'])}."
            if entry["again"]:
                line += f" Stated again by audit(s) {', '.join(map(str, entry['again']))}."
            if entry.get("met_later"):
                number, source = entry["met_later"]
                line += (f" Audit {number} later found {rid} MET on "
                         + ("this source." if current and source == current else "another source."))
            return line
        head = ("\n\n## Earlier audits of these requirements (historical questions, not current findings)\n\n"
                "Each line is what an earlier independent audit wrote about a requirement here, on the source and "
                "requirement revision it names. It is a question to judge afresh on the current source: not a "
                "current finding, not proof either way, not a new requirement, and it transfers no finding or "
                "owner. A decision, a ruling or a revised requirement shown here outranks a condition an older "
                "line assumed, and the original goal outranks them all.")
        if judge:
            head += (" Judge each requirement on the current source yourself; where an earlier question still "
                     "holds, say so on its line, and where it no longer does, say what now shows that.")
        else:
            head += (" Work and review that touch these requirements should hold for these questions and for the "
                     "rest of the requirement's input, not only for the last example. For each example, identify the "
                     "rule the goal entitles at that input layer (the protocol's or the format's own grammar or "
                     "contract) that the example breaks, and hold that rule across the cases it applies to, while "
                     "every form the rule allows stays accepted. This says where to look; it adds no requirement.")

        def binding_lines(rid: str) -> List[str]:
            # A decision or ruling is shown whole or not at all: a cut one
            # could change what it says. Too long to show, it is named with
            # where to read it, and it still binds.
            out = []
            named = [("decision", ledger.decisions.get(rid), "the orchestrator's decisions")] if ledger.decisions.get(rid) else []
            named += [(f"ruling {i + 1}", r, "the operator's rulings") for i, r in enumerate(ledger.rulings)
                      if re.search(rf"\b{re.escape(rid)}\b", r)]
            for label, text, where in named:
                text = " ".join(str(text).split())
                if len(text) <= _HISTORY_BINDING_CHARS:
                    out.append(f"  Current {label} naming {rid} (binds over any older condition): {text}")
                else:
                    out.append(f"  Current {label} naming {rid} binds over any older condition; it is {len(text)} "
                               f"characters, too long to show whole here: read it in {where}.")
            return out
        # One shared bound for the whole block, framing and metadata
        # included (root review of b54a28c: the first entry of every
        # requirement was admitted after the bound was spent, and decisions,
        # rulings and omission lists were never counted). Each requirement
        # enters as a unit, its newest entry with every decision or ruling
        # naming it, so no older condition is ever shown without what
        # supersedes it; older entries fill what is left; whatever does not
        # fit is counted in one bounded line.
        budget = _HISTORY_TOTAL_CHARS - len(head) - 1 - _HISTORY_FOOTER_CHARS
        shown: Dict[str, List[dict]] = {}
        bindings: Dict[str, List[str]] = {}
        for rid in rids:
            items = entries[rid]
            if not items:
                continue
            unit = [render(rid, items[-1])] + binding_lines(rid)
            cost = sum(len(line) + 1 for line in unit)
            if cost <= budget:
                shown[rid], bindings[rid] = [items[-1]], unit[1:]
                budget -= cost
        for depth in range(2, _HISTORY_PER_REQUIREMENT + 1):
            for rid in list(shown):
                items = entries[rid]
                if len(items) < depth:
                    continue
                text = render(rid, items[-depth])
                if len(text) + 1 <= budget:
                    shown[rid].insert(0, items[-depth])
                    budget -= len(text) + 1
        blocks = []
        for rid in rids:
            if rid in shown:
                blocks.append("\n".join([render(rid, e) for e in shown[rid]] + bindings[rid]))
        hidden = {rid: [e for e in entries[rid] if all(e is not x for x in shown.get(rid, []))] for rid in rids}
        hidden = {rid: items for rid, items in hidden.items() if items}
        if hidden:
            count = sum(len(items) for items in hidden.values())
            whole = [rid for rid in hidden if rid not in shown]
            numbers = sorted({e["number"] for items in hidden.values() for e in items})

            def ids(names: List[str]) -> str:
                return ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
            footer = (f"({count} earlier audit entr{'y' if count == 1 else 'ies'} not shown here, for "
                      f"{len(hidden)} requirement(s): {ids(list(hidden))}"
                      + (f"; none shown for {ids(whole)}, whose binding decisions or rulings, if any, are in the "
                         "decisions and rulings" if whole else "")
                      + f". Each audit's whole reply is kept as a run artifact (audits {numbers[0]}-{numbers[-1]}, "
                      "kind audit-reply), retrievable by its id.)")
            blocks.append(footer)
        if not blocks:
            return ""
        return head + "\n" + "\n".join(blocks)

    def _audit_required_ids(self, audited, carried, rows, auditor: Optional[str]) -> List[Tuple[str, str]]:
        """The one authoritative list of lines an audit reply must contain,
        as ``(id, kind)`` (cycle05): every audited requirement; every review
        finding carried to the auditor; every acceptance row binding an
        audited requirement that the audit reads and no current judgment
        holds (an executed test row, a legacy or reading row; a capture row
        is the design reviewer's); every executed restatement awaiting
        judgment. The prompt lists exactly these, and an id with no line in
        the reply is an omitted answer (``_audit_omitted``)."""
        required: List[Tuple[str, str]] = [(r, "requirement") for r in audited]
        required += [(r["fid"], "finding") for r in carried]
        for o in rows:
            if (delivery.purpose_of(o) != "acceptance" or o.get("superseded_by")
                    or not any(r[0] in audited for r in o.get("requirements") or ())):
                continue
            modality = o.get("modality") or "test"
            if not ((modality == "test" and o.get("execution") == "ran") or modality in ("legacy", "reading")):
                continue
            if delivery.EvidenceObligation.from_dict(o).sufficiency_current:
                continue
            required.append((o["oid"], "row"))
        seen = {i for i, _ in required}
        required += [(oid, "restatement") for oid in self._restated_executed_ids(audited, auditor)
                     if oid not in seen]
        return required

    # -- audit findings: measured product faults kept as requirement debt ------
    def _open_findings_for(self, rid) -> List[str]:
        """Open finding ids naming requirement ``rid`` (any requirement if None)."""
        return [f["id"] for f in self.findings
                if f["status"] == "open" and (rid is None or rid in f["requirements"])]

    def _resolves_problem(self, resolves, covers, spec) -> str:
        """Why a RESOLVES line cannot stand, before any lead call; "" if it can."""
        if resolves is None:
            return ""
        if not self.config.requirements_ledger:
            return "RESOLVES needs the requirements ledger, which is off for this run."
        if not resolves:
            return "RESOLVES names no finding."
        if len(set(resolves)) != len(resolves):
            return f"RESOLVES names a finding twice: {', '.join(resolves)}."
        by_id = {f["id"]: f for f in self.findings}
        reviews = {r["fid"]: r for r in self.review_findings}
        requirements = [r for r in resolves if re.fullmatch(r"R\d+", r) and r not in by_id and r not in reviews]
        if requirements:
            # A requirement id is never read as a finding (cycle10); what is
            # left here is one the record does not hold NOT MET by the audit
            # alone (``_normalize_resolves`` moved those to COVERS).
            parts = []
            for rid in requirements:
                owing = self._open_findings_for(rid)
                if rid not in self.memory.ledger.requirements:
                    parts.append(f"{rid} is not a listed requirement")
                elif owing:
                    parts.append(f"{rid} is held by open finding(s) {', '.join(owing)}: name those in RESOLVES "
                                 f"and {rid} in COVERS")
                else:
                    state = str(self.memory.ledger.requirement_status.get(rid) or "open")
                    parts.append(f"{rid} is a requirement ({state[:60]}), not a finding: name it in COVERS")
            return ("RESOLVES names only finding ids (F<n>, RFT<task>-<n>); " + "; ".join(parts) + ".")
        unknown = [r for r in resolves if r not in by_id and r not in reviews]
        if unknown:
            return f"RESOLVES names findings that do not exist: {', '.join(unknown)}."
        # One registry for RESOLVES (2026-10-28): an audit finding (F<n>)
        # settles on this task's own check or renders; a review finding
        # (RFT<task>-<n>) settles only when the reviewer who raised it
        # rechecks it RESOLVED after this task closes clean
        # (_settle_review_findings). controller-continuation-01: the planner
        # named the eight ids the same prompt listed as open and was sent back
        # as naming findings that do not exist.
        audit = [r for r in resolves if r in by_id]
        review = [r for r in resolves if r not in by_id]
        if any(by_id[r].get("target") for r in audit) and not is_design_task(spec):
            # Only a UI task's own renders can show a measured fault gone.
            return ("RESOLVES needs a UI task whose own renders can show the fault gone; this task "
                    "is not one.")
        closed = ([r for r in audit if by_id[r]["status"] != "open"]
                  + [r for r in review if reviews[r].get("closed")])
        if closed:
            return f"RESOLVES names findings that are already resolved: {', '.join(closed)}."
        away = [r for r in review if not self._available(reviews[r]["author"])]
        if away:
            return ("RESOLVES names review findings only their own reviewer can close, and that seat is "
                    "unavailable now: " + ", ".join(f"{r} ({reviews[r]['author']})" for r in away) + ".")
        spent = [r for r in review if reviews[r].get("kind") == "unreviewed" and self._owed_attempts(r)
                 >= _OWED_SETTLEMENT_ATTEMPTS]
        if spent:
            return (f"RESOLVES names owed review debt whose owner has already been asked "
                    f"{_OWED_SETTLEMENT_ATTEMPTS} times without a verdict: {', '.join(spent)}. No further owner "
                    "attempt is made; it stays open and the run cannot complete over it.")
        wanted = {rid for r in audit for rid in by_id[r]["requirements"]}
        wanted |= {reviews[r]["requirement"][0] for r in review if reviews[r].get("requirement")}
        missing = sorted(wanted - set(covers))
        if missing:
            return (f"COVERS must include every requirement of the findings it resolves; missing: "
                    f"{', '.join(missing)}.")
        return self._scope_resolves_problem(resolves, spec)

    def _findings_prompt(self) -> str:
        return self._open_findings_prompt() + self._closed_findings_note()

    def _recheck_resolved_findings(self) -> None:
        """Reopen each resolved finding whose resolving renders no longer
        verify against the trusted source (a later task changed it)."""
        for finding in self.findings:
            if finding["status"] != "resolved" or not self.project or not finding.get("target"):
                continue
            problem = self._identity_problem(finding["resolved_by"], finding["target"], finding["steps"],
                                             finding["resolution"])
            if problem:
                finding.update(status="open", reopened=f"its resolving evidence no longer holds: {problem[:160]}")
                for rid in finding["requirements"]:
                    self.memory.ledger.requirement_status[rid] = f"NOT MET: open finding {finding['id']}"

    def _identity_problem(self, task_id, target, steps, evidence) -> str:
        """Why ``task_id``'s renders are no longer exactly the ones approved
        (``target``, ``steps``, ``evidence`` hashes) or no longer verify
        against the trusted source; "" if they still are and do. The renders
        that were reviewed, not merely renders that pass now (Codex reviews
        of e47c7ed and dd17a1d: a later write replaced them)."""
        return self._identity_check(task_id, target, steps, evidence)[0]

    def _identity_check(self, task_id, target, steps, evidence):
        """``(problem, observed)``: :meth:`_identity_problem`'s reason, and
        the evidence digests now on disk when, and only when, the renders
        still show the approved state but their digests differ from the
        approved snapshot's (map J9b). Otherwise ``observed`` is None: a
        different state or an incomplete set is not an observed digest
        mismatch. The approved snapshot is compared before the check re-runs:
        the check now verifies each file against the digest its capture
        recorded, so a replaced screenshot would otherwise surface as the
        check's own integrity failure and the observed comparison the
        settlement records would be lost."""
        from .design_evidence import check_records
        problem = self._evidence_set_problem(task_id)
        if problem:
            return problem, None
        now_target, now_steps, now_evidence = self._capture_state(task_id)
        if (now_target, now_steps) != (target, steps):
            return "the resolving renders now show a different state", None
        if now_evidence != evidence:
            problem = "the resolving renders were replaced after they were reviewed"
            # Only a digest actually read from every named file is an
            # observed comparison; an unreadable file is not (map J9b).
            if all(isinstance(d, str) and re.fullmatch(r"[0-9a-f]{64}", d)
                   for d in _snapshot_files((task_id, None, None, now_evidence)).values()):
                return problem, now_evidence
            return problem, None
        ok, problem, _, _records = check_records(self.project, task_id, 0, expected_source=self._trusted_source(),
                                                 receipt=self._capture_receipt(task_id, task_id in self._harness_tasks))
        return ("" if ok else problem), None

    def _findings_block_done(self) -> bool:
        """Whether open findings refuse DONE, after re-checking resolved ones
        against the current source; sets the refusal text when they do."""
        if not self.findings:
            return False
        self._recheck_resolved_findings()
        opened = self._open_findings_for(None)
        if not opened:
            return False
        self._done_refusal = (f"\n\n--- DONE SENT BACK ---\nAudit findings are still open: {', '.join(opened)}.")
        self._note(f"DONE sent back: open audit findings {', '.join(opened)}")
        return True

    def _mark_covered(self, rid, task_id) -> None:
        """A requirement with an open finding stays unmet whatever a task
        covered; only resolving every finding naming it lets a covering task
        mark it covered. Serial tasks and parallel merges both come here."""
        if rid not in self.memory.ledger.requirements:
            return
        owing = self._open_findings_for(rid)
        self.memory.ledger.requirement_status[rid] = (f"NOT MET: open finding {', '.join(owing)}" if owing
                                                      else f"covered by {task_id}")

    def _closed_with_findings(self, spec, open_before: int) -> bool:
        """Whether the resolving task closed with findings of its own (map
        P3.4): active findings the projection files under this task, or new
        entries on the legacy list, either one. A malformed record fails
        closed; a disagreement is recorded on the task."""
        from .finding_state import from_session
        state = from_session(self)
        typed = bool(state.problems or [i for i in state.findings() if i.task == spec.task_id])
        legacy = len(self.open_findings) > open_before
        mine = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        if typed != legacy and mine is not None:
            note = f"new findings: typed {typed}, legacy {legacy}"
            if note not in mine.mismatches:
                mine.mismatches.append(note)
        return typed or legacy

    def _settle_resolution(self, spec, checks_before, open_before) -> List[str]:
        """Commit this task's RESOLVES only after the whole task passed: its
        last gate, no new open finding, and verified, approved renders of the
        measured state (Codex review of e47c7ed: a failed gate closed debt).
        Returns the named findings still open."""
        named, inherited = self._review_resolution_targets(spec)
        if not self._current_resolves and not inherited:
            return []
        self._verify_dependencies(f"at settlement of {spec.task_id}")
        reasons = []
        self._ledger_at_settlement(spec, reasons)
        gates = self.checks[checks_before:]
        if gates and not gates[-1]["passed"]:
            reasons.append("its integration gate failed")
        if self._closed_with_findings(spec, open_before):
            reasons.append("it closed with open findings")
        if not reasons:
            self._resolve_check_findings(spec, gates)
        review_open = self._settle_review_findings(spec, named, inherited, list(reasons))
        design = [f for f in self.findings if f["id"] in self._current_resolves and f.get("target")]
        if design:
            approved = self._resolution_candidate
            if not approved or approved[0] != spec.task_id:
                reasons.append("its renders were not verified and approved")
            else:
                changed, observed = self._identity_check(*approved)
                if changed:
                    reasons.append(f"its approved renders did not hold until it closed: {changed[:160]}")
                if observed is not None:
                    self._refuse_settlement_mismatch(spec, approved, observed, reasons)
            if not reasons:
                self._resolve_findings(spec, approved)
        still = [f for f in self.findings if f["id"] in self._current_resolves and f["status"] == "open"]
        for finding in still:
            if reasons:
                finding["last_attempt"] = f"{spec.task_id}: " + "; ".join(reasons)
        return [f["id"] for f in still] + review_open

    def _ledger_at_settlement(self, spec, reasons) -> None:
        """Compare the ledger this task was dispatched under with the live one
        at settlement, on the task's own outcome by task id (map P3.4;
        O-NEXT-16 residual, Sol 5866094506). The task has already left
        ``run_task``, so ``_required`` would have no current task to record
        on. A disagreement is recorded once on that outcome, so the run
        cannot count as complete; a verified resolution may still be recorded.
        A missing or malformed dispatch record is recorded the same way, and
        never read as the live value. With no outcome for the task at all,
        nothing is settled."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        if outcome is None:
            reasons.append("its dispatch record is missing")
            return
        live = bool(self.config.requirements_ledger)
        contract = outcome.contract if isinstance(outcome.contract, dict) else None
        required = contract.get("required") if contract and contract.get("task_id") == spec.task_id else None
        want = required.get("requirements_ledger") if isinstance(required, dict) else None
        if type(want) is not bool:
            note = f"requirements_ledger: contract {'malformed' if required else 'missing'}, legacy {live!r}"
        elif want != live:
            note = f"requirements_ledger: contract {want!r}, legacy {live!r}"
        else:
            return
        if note not in outcome.mismatches:
            outcome.mismatches.append(note)

    def _refuse_settlement_mismatch(self, spec, approved, observed, reasons) -> None:
        """Stop when the renders a reviewer approved no longer have the
        approved digests at settlement (map J9b): an integrity stop, with no
        repair, recapture or second review. The findings this task named stay
        open, the approved and observed digests are kept in the run record,
        and the changed evidence is left on disk as found. A different state
        or source is not this, and stays unverified."""
        from .design_evidence import EvidenceIdentityMismatch
        for finding in self.findings:
            if finding["id"] in self._current_resolves and finding["status"] == "open":
                finding["last_attempt"] = f"{spec.task_id}: " + "; ".join(reasons)
        approved_files = _snapshot_files(approved)
        observed_files = _snapshot_files((spec.task_id, None, None, observed))
        record = dict(task=spec.task_id, comparison="settlement: the snapshot the design review approved "
                      "against the evidence on disk when the task closed",
                      approved=approved_files, observed=observed_files,
                      differing=sorted(p for p, d in approved_files.items() if observed_files.get(p) != d),
                      approved_bytes_retained=False,
                      note="Only the approved digests were kept; the reviewer's copy was disposable, so "
                           "the approved bytes cannot be reconstructed from this record. The observed "
                           "evidence is left on disk as found. No intent is inferred.")
        self.store.put(json.dumps(record), kind="evidence-identity", author="harness")
        self._note(f"task {spec.task_id}: approved renders changed before settlement; stopping")
        raise EvidenceIdentityMismatch(
            f"task {spec.task_id}: the renders the design review approved were replaced before settlement "
            f"({', '.join(record['differing'])[:240]} "
            "differ from the approved digests). The findings it named stay open; the evidence is "
            "preserved as found.")

    def _distrust_resolutions(self, reason: str) -> None:
        """Mark every resolved finding open and its requirements unmet,
        without reading anything: when the re-check itself failed, an
        unverified resolution is not kept as resolved (Codex review of
        ee7e62b). In-memory only, so it cannot fail the same way."""
        for finding in self.findings:
            if finding.get("status") == "resolved":
                finding.update(status="open", reopened=f"unverified: {reason}")
                for rid in finding.get("requirements", ()):
                    self.memory.ledger.requirement_status[rid] = f"NOT MET: open finding {finding['id']}"
            if finding.get("status") == "open":
                finding.setdefault("unresolved_reason", f"open when the run stopped: {reason}")

    def _annotate_open_findings(self, reason: str) -> None:
        """Say in each open finding why it is still open when the run ends."""
        for finding in self.findings:
            if finding["status"] != "open":
                continue
            finding.setdefault("unresolved_reason", f"open when the run stopped: {reason}")
            if finding["id"] in self._current_resolves and "last_attempt" not in finding:
                finding["last_attempt"] = f"the resolving task stopped before it closed: {reason}"

    def _record_check_debt(self, spec, covers) -> Optional[str]:
        """A task whose last check still fails after its fix round leaves a
        finding the orchestrator repairs by RESOLVES, instead of ending the
        run (survey plan, 2026-09-30; the second remaining task-level stop
        after session.TaskFailed). Needs the requirements ledger and a COVERS
        line to hold the debt; otherwise today's CheckFailing stop stands.
        Returns the finding id, or None when nothing was recorded."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == spec.task_id), None)
        if outcome is None or not outcome.checks or outcome.checks[-1].get("passed"):
            return None
        if not (self.config.requirements_ledger and covers):
            return None
        last = outcome.checks[-1]
        fid = f"F{len(self.findings) + 1}"
        failed_checks = _check_ids(last.get("receipts") or [], passed=False)
        whole = {}
        if not failed_checks:
            # Every command's own receipt passed, yet the check failed (the
            # tree moved under it, or only the scope receipt failed). An empty
            # list was read back as ["check"], an id a multi-command gate
            # never has, so the finding could not be resolved by any repair.
            # The gate's own commands; the harness's derived receipts exist
            # only while test files differ and must pass with the gate anyway.
            whole = dict(gate_checks=[c for c in _check_ids(last.get("receipts") or [], passed=True)
                                      if not c.startswith("original-tests:") and c != "repair-check"])
            failed_checks = [_WHOLE_GATE]
        message = (f"the project check still failed after {spec.task_id}'s work (attempt "
                   f"{last.get('attempt', '?')}, gate {last.get('gate', 'full')}; failed: "
                   + ("no single command, the gate as a whole" if whole else ", ".join(failed_checks)) + ")")
        scope = self._check_scope(spec.task_id, last)
        self.findings.append(dict(
            id=fid, task=spec.task_id, requirements=list(covers), kind="check.failed",
            target=None, steps=[], view=None, width=None, viewport=None, message=message[:300],
            check=dict(attempt=last.get("attempt"), gate=last.get("gate"), failed_checks=failed_checks, **whole,
                       output_artifact=last.get("output_artifact"), attribution=last.get("attribution")),
            evidence=None, status="open",
            # The lead's accepted outside-scope declarations for this check's
            # failed cases (cycle04): the paths a repair task's SCOPE needs.
            # A claim, carried for routing only.
            **({"scope": scope} if scope else {})))
        for rid in covers:
            self.memory.ledger.requirement_status[rid] = f"NOT MET: open finding {', '.join(self._open_findings_for(rid))}"
        self._note(f"task {spec.task_id}: failed check recorded as finding {fid}; the orchestrator re-plans")
        return fid

    def _check_findings_settled(self, task_id: str) -> bool:
        """Whether every finding ``task_id`` recorded is resolved (and it
        recorded at least one): its failed check was repaired by a later
        task, so the failure is history for the completion questions."""
        mine = [f for f in self.findings if f["task"] == task_id and f.get("kind") == "check.failed"]
        return bool(mine) and all(f["status"] == "resolved" for f in mine)

    def _record_audit_findings(self, spec, records) -> List[str]:
        """Record each measured overflow of a review-only task as a finding
        owned by that task and its COVERS ids (from the harness, never model
        prose). Every COVERS id is marked unmet: the harness cannot attribute
        an overflow to one requirement, so all of them block until resolved."""
        _target, steps, evidence = self._capture_state(spec.task_id)
        ids = []
        for record in records:
            fid = f"F{len(self.findings) + 1}"
            self.findings.append(dict(
                id=fid, task=spec.task_id, requirements=list(self._current_covers), kind=record["kind"],
                target=record.get("target"), steps=steps, view=record.get("view"), width=record.get("width"),
                viewport=record.get("viewport"), message=record["message"][:300], evidence=evidence,
                status="open"))
            ids.append(fid)
        for rid in self._current_covers:
            self.memory.ledger.requirement_status[rid] = f"NOT MET: open finding {', '.join(self._open_findings_for(rid))}"
        self._note(f"task {spec.task_id}: audit findings {', '.join(ids)} recorded as requirement debt")
        return ids

    def _resolve_check_findings(self, spec, gates) -> None:
        """Close each check finding this task names when the checks that
        failed were rerun on this task's source and passed (Codex review of
        1995d02: a repair whose policy selected a different gate must not
        settle it). ``gates`` are the checks this task ran. The task that
        left the failure keeps its record; its check facts become history
        (a recovered failure, quadratus.outcome)."""
        passed, whole = set(), []
        for gate in gates:
            # Only a gate that passed as a whole counts: one whose tree moved
            # is recorded failed although its receipts read passed.
            if gate.get("passed"):
                ids = set(_check_ids(gate.get("receipts") or [], passed=True))
                passed |= ids
                whole.append(ids)
        for finding in self.findings:
            if (finding["id"] not in self._current_resolves or finding["status"] != "open"
                    or finding.get("target")):
                continue
            check = finding.get("check") or {}
            # Only a finding recorded with the sentinel is settled by a whole
            # gate. One with no failed ids on record keeps the old reading,
            # the single check "check": a finding that is not a check finding
            # must not be closed by any passing gate.
            failed = list(check.get("failed_checks") or ["check"])
            if failed == [_WHOLE_GATE]:
                # No single command failed, so one whole passing gate must
                # have rerun every command the failed gate ran (a smaller
                # gate chosen by policy does not settle it).
                need = set(check.get("gate_checks") or ())
                problem = "" if any(need <= ids for ids in whole) else (
                    "none of its checks passed as a whole"
                    + (f" with {', '.join(sorted(need))} rerun" if need else ""))
            else:
                missing = [c for c in failed if c not in passed]
                problem = (f"its checks did not rerun {', '.join(missing)} with a passing receipt"
                           if missing else "")
            if problem:
                finding["last_attempt"] = f"{spec.task_id}: {problem}"
                continue
            finding.update(status="resolved", resolved_by=spec.task_id, reopened=None,
                           resolution=dict(checks=failed, gates=[g.get("command") for g in gates]))
            for outcome in self.task_outcomes:
                if outcome.task_id == finding["task"] and self._check_findings_settled(outcome.task_id):
                    for fact in outcome.facts:
                        if fact.stage == "checks" and fact.kind == "product" and fact.active:
                            fact.recovered = True
            self._note(f"{spec.task_id} resolved {finding['id']}: its check passed")

    def _resolve_findings(self, spec, approved) -> None:
        """Close each finding this task names in RESOLVES whose acceptance its
        own verified, approved renders establish: the same page reached by the
        same interaction (fixture content included), with no overflow. The
        identity committed is the one approved, re-verified at settlement."""
        _task, target, steps, evidence = approved
        for finding in self.findings:
            if finding["id"] not in self._current_resolves or finding["status"] != "open":
                continue
            if target != finding["target"] or steps != finding["steps"]:
                finding["last_attempt"] = (f"{spec.task_id} rendered a different state: {target} after "
                                           f"{len(steps)} steps, not {finding['target']} after "
                                           f"{len(finding['steps'])} steps as measured")
                continue
            finding.update(status="resolved", resolved_by=spec.task_id, reopened=None, resolution=evidence)
            self._note(f"task {spec.task_id} resolved audit finding {finding['id']}")

    def _name_findings_stop(self, stopping=None) -> None:
        """Name a stop caused by unverified design evidence.

        Codex, Run 15: the run stopped on unverified design evidence with
        ``completed`` false and ``result.error`` blank. Other open-finding and
        failed-check stops are left as they were.

        Only the design debt of the task or tasks that just closed names the
        stop (map G8): ``stopping`` is their ids. Another task's recorded
        debt never names this stop.
        """
        own = self._design_debt(stopping)
        if own and not self.stop_reason:
            task_id, problem = own[-1]
            self._stop_with("unverified", (f"DesignUnverified: task {task_id} is design work without clean "
                                           f"rendered evidence: {str(problem)[:400]}. Work preserved."))
        elif not self.stop_reason:
            # Named (map G9): open findings or a failed check used to end the
            # run with a blank error (J3).
            failing = [o.task_id for o in self.task_outcomes if o.checks and not o.checks[-1]["passed"]]
            self._stop_open_work("CheckFailing" if failing else "FindingsOpen", "a task closed with open work")
