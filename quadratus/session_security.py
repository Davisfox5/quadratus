"""The security excursion of the session engine, as a mixin of Session: one
security task drafted through the lead's channels, the verifier's call
across vendor lines, and the reading of its verdict.

The methods moved out of session.py unchanged on 2026-11-11, the rule that
redraws the verifier included: nothing about the excursion was tidied.
They reach the rest of Session only through ``self``. This module never
imports quadratus.session.
The move was redone on 2026-11-12, after the development line was merged
into this file, and took that line's code of the same kind with it.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from .delegation import invocation
from .memory import TaskMemory, TaskSummary
from .providers import TurnLimitReached
from .registry import resolve
from .routing import close_excursion, cross_family_verifier, open_security_excursion
from .run_budget import FinishReserved
from .session_prompts import _CAP_NO_VERDICT
from .session_types import TaskSpec
from .structured import StructuredError, parse_security_verdict
from .verdicts import has_prefixed_finding as _has_prefixed_finding
from .verdicts import security_finding_stands as _security_finding_stands
from .verdicts import security_verdict_line as _security_verdict_line


class SecurityMixin:
    """A security task's bounded excursion and its verification.

    Session inherits this; nothing here is used on its own.
    """

    def _run_security_task(self, spec: TaskSpec) -> TaskSummary:
        """Work a security-classified task inside a bounded excursion.

        The settled shape, now actually wired into the loop rather than
        existing alongside it: the deputy takes the seat for this thread only,
        the operator's preferred security model does the work, the deputy
        verifies -- never the worker checking itself -- and the excursion
        closes unconditionally. The primary orchestrator is not invoked at any
        point inside the task; it learns the outcome from the ledger entry,
        which is the continuity mechanism the excursion design already
        specified.

        The rotation counter is deliberately not consulted or advanced:
        security work was never the rotation's to give out, so it neither
        consumes anyone's turn nor skews the scoreboard.
        """
        excursion = open_security_excursion(available=self._available)
        try:
            task = TaskMemory(spec.task_id, excursion.worker, self.store)
            self._task_memory = task
            self._record_selection(spec, excursion.worker, "lead")
            if self._outcome is not None:
                self._outcome.lead = excursion.worker
            self._bind_contract(excursion.worker)
            task.record("user", spec.description)

            # Fetch and worker channels, no consult: the excursion stays a
            # straight line, but the lead's prompt offers WORKER and the
            # harness has to serve what it offers (Q9 baseline, 2026-09-22).
            self._stage("draft")
            try:
                draft = self._draft_with_channels(excursion.worker, spec, task, consults=False)
            except TurnLimitReached as exc:
                # A capped security draft is a capped task (map G2): the
                # verification never ran, so its edge stays unset and nothing
                # is reported as verified; the excursion still closes.
                return self._close_turn_limited(excursion.worker, spec, task, exc, self._task_before)
            task.record("assistant", draft)
            task.keep(draft, kind="draft")
            self._edge("draft", True)

            # Mandatory, not complexity-scaled: an unverified security answer
            # is the failure the excursion exists to prevent, so SIMPLE does
            # not buy the verification off. And verification crosses vendor
            # lines when it can: the deputy verifies by default, but if the
            # chain has degraded until deputy and worker share a vendor, a
            # cross-family peer is drafted instead -- same-vendor checking
            # shares the author's lineage and its blind spots.
            verifier = excursion.verifier
            worker_spec, verifier_spec = resolve(excursion.worker), resolve(verifier)
            if (worker_spec and verifier_spec
                    and worker_spec.provider == verifier_spec.provider):
                crossed = cross_family_verifier(
                    excursion.worker,
                    candidates=self.brain_trust,
                    available=self._available,
                )
                if crossed is not None:
                    verifier = crossed
            draft = self._run_integration_gate(excursion.worker, spec, task) or draft
            self._record_selection(spec, verifier, "verifier")
            # The verification edge is set only from a verdict the verifier
            # actually returned; a refusal or any other interruption leaves it
            # unset, so the record says it was not attempted to completion.
            self._stage("verification")
            # Asked and parsed with the protocol fixed at dispatch (O-NEXT-10
            # D); the live option is recorded beside it.
            if self._required("security_verdict", self._live_security_verdict()) == "json":
                self._verify_security_json(spec, task, draft, excursion.worker, verifier)
            else:
                # The identity an owed verification would be settled on,
                # taken before the call and compared after it.
                verifier_binding = self._judgment_binding(spec, verifier, "verifier")
                verdict = self._ask_verifier(spec, task, verifier, self._verifier_prompt(spec, draft, verifier))
                if verdict is not None:
                    # Owed verification settles only on a judgment the
                    # verifier actually wrote (its VERDICT line, or a prefixed
                    # finding) on an unchanged identity; narration, an empty
                    # or malformed reply leaves it owed (root's review of
                    # a7f9c6c, R2). The older reading below is unchanged.
                    recognised = _security_verdict_line(verdict) is not None or _has_prefixed_finding(verdict)
                    current = self._judgment_binding(spec, verifier, "verifier")["identity"]
                    if recognised and current == verifier_binding["identity"]:
                        self._settle_owed_by_judgment(spec, verifier, "verification")
                # A finding is a marker as written, not the word in prose (map
                # G7): the old substring test read "no BLOCKING findings" as
                # one. Semantics taken from the reviewed helper on 90cc5d9.
                # An explicit VERDICT line decides (series b1ff751 f6 and
                # rule-b1ff751 f6: an accepting report filed as the finding).
                if verdict is None:
                    pass
                elif _security_finding_stands(verdict):
                    self._open_finding("security", verdict)
                    self._edge("verification", False)
                else:
                    self._edge("verification", True)

            self._stage("closeout")
            summary_text, reasoning, dead_ends = self._task_close_out(excursion.worker, spec, task,
                                                                      mechanical=False)
            summary = task.close(
                summary=summary_text, reasoning=reasoning, dead_ends=dead_ends
            )
            self._edge("closeout", True)
            self.memory.absorb(summary)
            self.history.append(summary)
            return summary
        finally:
            # Unconditional by design: a failed security lookup still ends the
            # excursion and hands the seat back rather than leaving the run
            # degraded.
            close_excursion(excursion)

    def _security_snapshot(self, spec, draft):
        from .project import Project
        source = (Project(self.project, exclude=self.config.project_excludes).fingerprint()
                  if self.project else None)
        return hashlib.sha256(json.dumps({
            'source': source, 'task': spec.description, 'draft': draft,
            'acceptance': list(spec.scope.acceptance) if spec.scope else [spec.description],
        }, sort_keys=True).encode()).hexdigest()

    def _ask_verifier(self, spec, task, verifier, prompt) -> Optional[str]:
        """One verifier call, for the prose and the JSON protocol alike.

        Returns the reply, kept on the task, or None when the seat stopped at
        its turn cap. A capped verifier gave no verdict: the cap is recorded
        (``_capped_review``), the verification is held as this verifier's
        review debt (``_owe_review``) and the verification edge stays unset,
        as for any other interrupted verifier. One helper for both protocols,
        because they had drifted: the JSON path let the cap end the run
        (review 1110, session first half 4).
        """
        try:
            with invocation(spec.task_id, "verifier"):
                verdict = said = self._invoke_model(verifier, prompt)
        except TurnLimitReached as exc:
            verdict, said = None, self._capped_review(spec, verifier, "verification", exc)
            # The verification is owed by this verifier as review debt
            # (recovery lifecycle, 2026-10-31), settled only by its own
            # completed verification of this work.
            rv = self._recovery_open(spec, role="verifier", seat=verifier, kind="verification",
                                     outcome_class="verification_capped",
                                     question=f"{verifier}'s security verification of {spec.task_id}",
                                     event=self._call_event(spec.task_id, "verifier", verifier), exc=exc)
            rv["eligible"], rv["decision"] = ["hold_owned_debt"], "hold_owned_debt"
            self._owe_review(spec, verifier, "verification", f"stopped at {exc.turns or 'its'} rounds with "
                             "no verdict" + _CAP_NO_VERDICT, role="verifier", record=rv)
        # Complete here means the seat answered; whether the answer accepts
        # is decided by the verdict readers, as before.
        self._record_review(spec, verifier, "verifier", verdict or "", complete=verdict is not None,
                            capped=verdict is None)
        task.record("assistant", f"[{verifier}] {said}")
        task.keep(said, kind=f"verify:{verifier}", author=verifier)
        return verdict

    def _verify_security_json(self, spec, task, draft, worker, verifier):
        acceptance = list(spec.scope.acceptance) if spec.scope else [spec.description]
        acceptance = acceptance or [spec.description]
        # The rejecting verdict a security-fix answered, until it is verified again.
        standing = None
        for round_index in range(2):
            snapshot = self._security_snapshot(spec, draft)
            prompt = self._verifier_prompt(spec, draft, verifier) + (
                '\nReturn one JSON object only, with exactly these fields: '
                'schema_version (integer 1), verdict (accept, reject, insufficient_evidence), '
                'snapshot_hash, acceptance_results, blocking_findings, limitations. '
                'blocking_findings and limitations are arrays of nonempty strings. '
                'acceptance_results is an ordered array of {criterion, status, evidence}; '
                'status is passed, failed or insufficient_evidence; evidence must be nonempty. '
                'Accept requires all criteria passed and no findings or limitations. '
                'Missing evidence means insufficient_evidence. No fences or prose. '
                'This JSON replaces the BLOCKING: lines asked for above: findings go in '
                'blocking_findings. snapshot_hash echoes the hash below exactly. Each '
                'criterion is copied verbatim from the list below, in the same order. A reject '
                'must name a blocking finding or a failed criterion. Strict JSON: no duplicate '
                'keys, no NaN or Infinity. A verdict that fails any of these opens a security '
                'finding with no second try. '
                f'\nSnapshot hash: {snapshot}\nAcceptance criteria: {json.dumps(acceptance)}'
            )
            raw = self._ask_verifier(spec, task, verifier, prompt)
            if raw is None:
                if standing is not None:
                    # As after a capped recheck: the reject the fix answered
                    # is neither confirmed nor cleared, so it stays on the
                    # record as the specific unverified defect, beside the cap.
                    self._open_finding("unverified", f"Task {spec.task_id}: security finding not verified "
                                                     f"again after the fix (verifier capped): {standing}")
                    task.record("user", "A rejected security verdict was not verified again after the fix "
                                        "(the verifier stopped at its turn cap) -- carry it into the "
                                        f"summary as an open question:\n{standing}")
                return
            try:
                verdict = parse_security_verdict(raw, snapshot_hash=snapshot,
                                                 acceptance=acceptance)
                if self._security_snapshot(spec, draft) != snapshot:
                    raise StructuredError('Source changed during security verification')
            except StructuredError as exc:
                self._open_finding('security', f'Security verification incomplete: {exc}')
                self._edge("verification", False)
                return
            if verdict['verdict'] == 'accept':
                self._edge("verification", True)
                return
            if (verdict['verdict'] == 'reject' and round_index == 0
                    and self._gate_fixes_used < self.config.max_gate_fixes
                    # The route the reject selects, admitted whole before the
                    # editor writes; refused, the verdict stands as found.
                    and self._admit_repair(spec, "security fix", "security-repair",
                                           self._security_repair_stages(spec, verifier),
                                           after="security verification", wall_extra=self._gate_wall(spec),
                                           wall_known=self._gate_wall(spec) > 0)):
                # Share the existing gate repair allowance. A reject can buy
                # one round, never a separate allowance or parsing retry.
                # A fix that stops at the lead's turn cap is that one round
                # spent, as for a gate-fix (``_capped_fix``): its edits are
                # kept and held to scope, and the gate and the verifier
                # below still decide.
                try:
                    draft = self._edit(worker, self._fix_prompt(
                        spec, draft, [('Security verifier', raw)]), role='security-fix', capped=(spec, task))
                except FinishReserved as exc:
                    self._finish_refused(spec, "security fix", exc)
                    self._open_finding("security", f"Security verification {verdict['verdict']}: {raw}")
                    self._edge("verification", False)
                    return
                task.record("assistant", draft)
                task.keep(draft, kind='security-fix', author=worker)
                self._gate_fixes_used += 1
                self._run_integration_gate(worker, spec, task, max_fixes=0)
                standing = raw
                continue
            self._open_finding("security", f"Security verification {verdict['verdict']}: {raw}")
            self._edge("verification", False)
            return

    def _live_security_verdict(self) -> str:
        """The security verifier's protocol read from live config."""
        return "json" if self.config.security_verdict_json else "prose"
