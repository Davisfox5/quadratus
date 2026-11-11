"""The requirements ledger, the audit and the audit findings of the session
engine, as a mixin of Session: what a task covers, the requirements
review, the cross-vendor audit and its citations, and findings kept as
requirement debt until a later task settles them.

The methods moved out of session.py unchanged on 2026-11-11. They reach
the rest of Session only through ``self``. This module never imports
quadratus.session.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from .check_facts import _WHOLE_GATE, _audit_check_facts, _check_ids, _configured_checks
from .delegation import invocation
from .providers import ProviderError
from .reply_parsers import _DECIDE, _cited_path
from .session_design import _snapshot_files
from .session_prompts import _AUDIT_LINE_RULE
from .session_types import is_design_task, is_review_only
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
        prompt = (
            f"## Goal (verbatim)\n\n{self.memory.goal.strip()}\n\n## Numbered requirements\n\n"
            + "\n".join(f"{rid}: {text}" for rid, text in ledger.requirements.items())
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
        for text in added:
            rid = f"R{next_id}"
            next_id += 1
            ledger.requirements[rid] = text
            ledger.requirement_status[rid] = "open (added by the requirements review)"
        for rid, why in disputed:
            ledger.ambiguous[rid] = why[:300]
            ledger.ambiguous_since[rid] = len(ledger.rulings)
        self.requirement_reviews.append(dict(reviewer=reviewer, result="reviewed", added=added,
                                             ambiguous=disputed))

    def _requirements_satisfied(self) -> bool:
        """Whether DONE may stand: every requirement covered, then audited met.

        Not satisfied means DONE goes back to the orchestrator with the gap
        named. No requirements listed means the ledger is inactive for this
        run; that is recorded, not invented. An audit that did not run is not
        satisfied either, but it judged nothing: ``_audit_unavailable`` says
        why, no status is written, and the completion decision sends DONE
        back once and then stops for the operator (``AuditUnavailable``).
        """
        ledger = self.memory.ledger
        # Only the audit this check asks for counts as its answer.
        self._audit_unavailable = ""
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
        uncovered = [r for r in ledger.requirements
                     if r in claimed and not status.get(r, "").startswith(("covered", "met"))]
        if uncovered:
            # The same split as the audit refusal below: a requirement whose
            # NOT RUN report nothing can lift is not asked for a task here and
            # told "do not name one" two lines later.
            remedy, stuck = self._not_run_remedy(uncovered)
            nameable = [r for r in uncovered if r not in stuck]
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nThese requirements are not covered by any finished "
                f"task: {', '.join(uncovered)} (never covered, or found not met by the audit and not "
                "covered again since)."
                + (" Name a task for them (with COVERS), or explain in the task why one cannot be done."
                   if not stuck else
                   f" Name a task for {', '.join(nameable)} (with COVERS), or explain in the task why one "
                   "cannot be done." if nameable else "")
                + remedy)
            self._note(f"DONE sent back: uncovered {', '.join(uncovered)}")
            return False
        if self._explicit_tasks is not None:
            self._note("requirements no listed task claimed, left unjudged: "
                       + ", ".join(self.explicit["requirements_unclaimed"]))
            verdicts = self._audit_requirements(ids=self.explicit["requirements_claimed"]) if claimed else {}
        else:
            verdicts = self._audit_requirements()
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
        unmet = {r: why for r, (ok, why) in verdicts.items() if not ok}
        for rid, (ok, why) in verdicts.items():
            status[rid] = "met (audited)" if ok else f"NOT MET: {why[:160]}"
        if unmet:
            # A requirement held down by a NOT RUN report is not fixed by a
            # task that covers it again: say what lifts each report, and which
            # nothing in this run can (review 2026-11-10, second half 12).
            remedy, stuck = self._not_run_remedy(unmet)
            fixable = [r for r in unmet if r not in stuck]
            self._done_refusal = (
                "\n\n--- DONE SENT BACK ---\nAn independent audit found these requirements not met:\n"
                + "\n".join(f"- {r}: {why[:300]}" for r, why in unmet.items())
                + ("\nName a task that fixes them (with COVERS)." if not stuck else
                   f"\nName a task that fixes {', '.join(fixable)} (with COVERS)." if fixable else "")
                + remedy)
            self._note(f"DONE sent back: audit found {', '.join(unmet)} not met")
            return False
        return True

    def _auditor(self) -> Optional[str]:
        """A brain-trust member from a vendor other than the orchestrator's,
        or None. Never the seat's own vendor: an audit that shares the
        planner's lineage is not independent (Codex review of #25)."""
        seat_vendor = self.seat().key.partition(":")[0]
        return next((p for p in self.brain_trust
                     if self._available(p) and p.partition(":")[0] != seat_vendor), None)

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

    def _audit_requirements(self, ids: Optional[List[str]] = None) -> dict:
        """One read-only call: each requirement met or not, with evidence.
        ``ids`` restricts the audit to those requirements (an explicit run
        audits only what its listed tasks claimed); every listed requirement
        otherwise."""
        ledger = self.memory.ledger
        audited = {r: t for r, t in ledger.requirements.items() if ids is None or r in ids}
        self._audit_unavailable, self._auditing, self._audit_tests = "", True, None
        auditor = self._auditor()
        if auditor is None:
            self._audit_did_not_run(None, "no auditor from another vendor available")
            return {r: (False, "no auditor from another vendor available") for r in audited}
        before = self._capture_source() if self.project else None
        checks = _audit_check_facts(self.checks[-3:], self._source_fingerprint())
        configured = _configured_checks(self.config.integration_gate) if self.project else ""
        prompt = (
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
            + "\n\nYou are an independent auditor. The project in your working directory is the "
            "delivered work. For each requirement, check the delivered files themselves: code, "
            "interface, tests and documentation. Documentation must agree with the goal, not only "
            "with the code. A requirement about existing product behaviour is met only if it works "
            "with what the product actually does today. A user-interface requirement is met only "
            "with rendered evidence above. Reply with exactly one line per requirement and nothing "
            "else: 'R1: MET - <file path or test name that shows it>' or "
            "'R1: NOT MET - <what is missing or contradicts the goal>'. A MET without a cited "
            "file or test is counted as not met. " + _AUDIT_LINE_RULE + " A citation resolves only as a project-relative "
            "file path with an extension, or a test function named test_* defined in a file "
            "whose name starts with test and ends in .py; other names do not resolve."
        )
        saved = self._active_spec
        self._active_spec = None
        try:
            with invocation("audit", "auditor"):
                reply = self._invoke_model(auditor, prompt)
        except ProviderError as exc:
            # An audit that could not run is not a pass, and it is not a
            # verdict either. Budget stops and every other exception
            # propagate as from any other call.
            self._audit_did_not_run(auditor, f"the auditor call failed: {type(exc).__name__}: {str(exc)[:160]}")
            return {r: (False, "the auditor call failed") for r in audited}
        finally:
            self._active_spec = saved
        if self.project and before is not None and self._capture_source() != before:
            self._audit_did_not_run(auditor, "void: the tree changed during the audit")
            return {r: (False, "the tree changed during the audit") for r in audited}
        found = {}
        for rid, met, why in _audit_lines(reply):
            if met:
                resolved = self._resolve_citations(why)
                if not resolved:
                    met, why = False, f"MET claimed without a file or test that exists in the project ({why[:100]})"
            found[rid] = (met, why)
        lowered = self._lower_unexecuted(found)
        self._auditing, self._audit_tests = False, None
        if audited and not any(r in found for r in audited):
            # A reply with no verdict for any audited requirement judged
            # nothing; one missing among others is still that one's NOT MET.
            self._audit_did_not_run(auditor, "the auditor's reply held no verdict for any audited requirement",
                                    reply=(reply or "")[:400])
            return {r: (False, "the auditor gave no verdict") for r in audited}
        self._audit_misses = 0
        verdicts = {r: found.get(r, (False, "the auditor gave no verdict")) for r in audited}
        self.requirement_audits.append(dict(auditor=auditor, verdicts={r: dict(met=ok, why=why)
                                                                       for r, (ok, why) in verdicts.items()},
                                            lowered=lowered))
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
        unknown = [r for r in resolves if r not in by_id]
        if unknown:
            return f"RESOLVES names findings that do not exist: {', '.join(unknown)}."
        if any(by_id[r].get("target") for r in resolves) and not is_design_task(spec):
            # Only a UI task's own renders can show a measured fault gone.
            return ("RESOLVES needs a UI task whose own renders can show the fault gone; this task "
                    "is not one.")
        closed = [r for r in resolves if by_id[r]["status"] != "open"]
        if closed:
            return f"RESOLVES names findings that are already resolved: {', '.join(closed)}."
        missing = sorted({rid for r in resolves for rid in by_id[r]["requirements"]} - set(covers))
        if missing:
            return (f"COVERS must include every requirement of the findings it resolves; missing: "
                    f"{', '.join(missing)}.")
        return ""

    def _findings_prompt(self) -> str:
        opened = [f for f in self.findings if f["status"] == "open"]
        if not opened:
            return ""
        lines = [f"- {f['id']} (found by {f['task']}, requirements {', '.join(f['requirements'])}): "
                 + (f"the {f['view']} render of {f['target']} is {f['width']}px wide at a "
                    f"{f['viewport']}px viewport" if f.get("target") else f["message"])
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
                "closes with no new finding; read the check's output artifact before planning the "
                "repair.\n" + "\n".join(lines))

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
        if not self._current_resolves:
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
        return [f["id"] for f in still]

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
        self.findings.append(dict(
            id=fid, task=spec.task_id, requirements=list(covers), kind="check.failed",
            target=None, steps=[], view=None, width=None, viewport=None, message=message[:300],
            check=dict(attempt=last.get("attempt"), gate=last.get("gate"), failed_checks=failed_checks, **whole,
                       output_artifact=last.get("output_artifact"), attribution=last.get("attribution")),
            evidence=None, status="open"))
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

    def _audit_debt_applies(self, spec, ok, records) -> bool:
        """Whether this design check's failure becomes audit debt instead of a
        stop: a declared review-only task, the ledger on, COVERS present, and
        every problem a measured overflow on otherwise valid evidence. Any
        integrity or page problem alongside keeps today's stop."""
        if ok or not is_review_only(spec):
            return False
        # The ledger as dispatched AND as it is now (map P3.4; O-NEXT-10 C,
        # Codex 5865627034): a live enable adds no debt route, a live disable
        # is not ignored, and a disagreement is recorded and takes today's
        # stop. Nothing recorded in the ledger is erased either way.
        live = bool(self.config.requirements_ledger)
        ledger = self._required("requirements_ledger", live) and live
        return (ledger and bool(self._current_covers) and bool(records)
                and all(r.get("kind") == "product.overflow" for r in records))

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
