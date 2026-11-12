"""The evidence channel of the session engine, as a mixin of Session: proof
scenarios and their bindings, the EVIDENCE request a seat may make and
how it is served and bound to a source, scratch receipts, deferred rows,
the reassessment a new receipt buys, and scenario corrections.

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
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from . import delivery, judgment
from .check_facts import _item_paths
from .delegation import invocation
from .providers import ProviderError, TurnLimitReached
from .reply_parsers import (
    _BOUNDARY_NOTE,
    _DEFERRED_ROW_LINE,
    _DOC_SUFFIXES,
    _PROVES_ANY,
    _PROVES_PID,
    _TYPED_SEP,
    _behaviour_text,
    _behavioural_error,
    _finding_id,
)
from .run_budget import FinishReserved
from .scope import PROVES_RULE, TEST_SUBSTANCE_RULE, TaskScope
from .session_prompts import (
    _AUDIT_LINE_RULE,
    _BOUNDARY_NOTE_CHARS,
    _BOUNDARY_NOTES_PER_REPLY,
    _ERROR_PATH_RULE,
    _EVIDENCE_AUTHORITY,
    _EVIDENCE_CHANNEL,
    _EXECUTED_ERROR_RULE,
    _PLAIN_VERDICT_RULE,
    _SCENARIO_RENDER_MAX_CHARS,
    _TYPED_FINDING_FORMS,
    _display_item,
)
from .session_types import JobStoreUnavailable, TaskSpec, _seat_takes_scratch, is_design_task
from .verdicts import audit_lines as _audit_lines


class EvidenceMixin:
    """What a seat may ask the harness to run, and what the receipt is bound to.

    Session inherits this; nothing here is used on its own.
    """

    def _deferred_rows_block(self, rows: List[dict]) -> str:
        """For the reviewer's recheck: the rows the harness deferred to the
        merge gate when it reviewed, as the record holds them now, and the
        one-line form it answers each with."""
        if not rows:
            return ""
        lines = []
        for r in rows:
            receipts = [str(x) for x in (r.get("receipts") or ())]
            lines.append(f"- {r.get('proof_id') or r['oid']} [{', '.join(x[0] for x in r['requirements'])}]: "
                         f"{r['behavior'][:220]}; binding {r.get('binding', 'unknown')}: "
                         f"{', '.join(r.get('cases') or ()) or 'no case bound'}; execution {r['execution']}"
                         + (f" (receipt {receipts[-1]})" if receipts and r["execution"] == "ran" else "")
                         + (f" ({r['reason'][:80]})" if r.get("reason") else ""))
        return ("Rows the harness deferred to the merge gate when you reviewed, as they stand now:\n"
                + "\n".join(lines)
                + "\nFor each row, one line and nothing else about it: 'P<n>: SHOWN <what the executed case "
                "shows for the scenario>' or 'P<n>: NOT SHOWN <what is missing, or what the execution does not "
                "establish>'. A NOT SHOWN line is recorded as your EVIDENCE finding on this task and held open "
                "until a later complete run shows it; SHOWN is your reading, recorded as such, and makes nothing "
                "met: the final audit judges sufficiency. A row whose execution is not 'ran' cannot be SHOWN, "
                "and is held by its execution state whatever you write. A missing, duplicated or contradictory "
                "line is recorded as no reading.\n\n")

    def _read_deferred_rows(self, spec, peer: str, rows: List[dict], records, verdict: str, outcome) -> List[str]:
        """Read the reviewer's per-row lines for its deferred rows: NOT
        SHOWN registers its EVIDENCE finding (typed, held open, the
        reviewer's own to close); SHOWN is recorded as the reviewer's
        reading and establishes nothing; anything else is no reading.
        Returns the ids of the findings registered."""
        lines: Dict[str, List[tuple]] = {}
        for m in _DEFERRED_ROW_LINE.finditer(verdict or ""):
            lines.setdefault(m.group(1).upper(), []).append((m.group(2).upper().replace("  ", " "), m.group(3).strip()))
        readings: Dict[str, str] = {}
        new_ids: List[str] = []
        for r in rows:
            pid = r.get("proof_id") or r["oid"]
            distinct = list(dict.fromkeys(lines.get(pid.upper(), [])))
            if not distinct:
                readings[pid] = "no reading: no line for it"
                continue
            if len({state for state, _ in distinct}) > 1 or len(distinct) > 1:
                readings[pid] = f"no reading: {len(distinct)} conflicting lines"
                continue
            state, text = distinct[0]
            if state.startswith("NOT"):
                saved = self._current_covers
                self._current_covers = list(getattr(outcome, "covers", None) or [])
                try:
                    # The typed EVIDENCE form the review prompt names, basis
                    # and impact included, so the finding is held as a legacy
                    # open text and not carried to the audit as baseless.
                    made = self._register_review_findings(
                        spec, peer, f"BLOCKING: EVIDENCE {pid} - {text or 'not shown'} | basis: {pid} | impact: contract")
                finally:
                    self._current_covers = saved
                if made:
                    record = made[0]
                    record["hold"] = "raised at the post-merge recheck of a deferred row"
                    self._hold_open_finding(spec, peer, f"{record['fid']}: UNRESOLVED EVIDENCE {pid} - {text}",
                                            record=record, original=record.get("text"))
                    new_ids.append(record["fid"])
                    readings[pid] = f"NOT SHOWN, recorded as {record['fid']}: {text[:200]}"
                else:
                    readings[pid] = f"NOT SHOWN (already on the record): {text[:200]}"
            elif r.get("execution") != "ran":
                readings[pid] = f"SHOWN refused: the row's execution is {r.get('execution')}, so nothing is shown"
            else:
                readings[pid] = f"SHOWN (the reviewer's reading; sufficiency is the audit's): {text[:200]}"
        for entry in records:
            entry["rechecked"] = "rechecked"
            entry["reading"] = dict(readings)
        self._note(f"task {spec.task_id}: {peer} read {len(rows)} deferred row(s): "
                   + "; ".join(f"{pid}: {text[:60]}" for pid, text in readings.items()))
        return new_ids

    def _proof_problem(self, spec, covers) -> str:
        """A newly planned write task under the requirements ledger must map
        every COVERS requirement to at least one declared proof scenario and
        name no uncovered requirement in one. Declared mapping only: nothing
        here judges whether the scenarios are enough. Review-only tasks,
        explicit lists and runs without the ledger declare nothing."""
        scope = getattr(spec, "scope", None)
        if (not self.config.requirements_ledger or not self.memory.ledger.requirements or self._explicit_tasks is not None
                or not self.project or not self.config.allow_writes or scope is None or scope.review_only or not covers):
            return ""
        if scope.proofs is None and all(self._test_command_requirement(r) for r in covers):
            # Every covered requirement is a test command's pass: the
            # harness's receipt is its evidence (_TEST_COMMAND_RULE).
            return ""
        if scope.proofs is None:
            return ('The task declares no proof scenarios: add "proofs": [{"id": "P1", "requirements": ["R<n>"], '
                    '"operation": ..., "state": ..., "expect": ..., "location": ...}, ...] to SCOPE, mapping every '
                    f"requirement in COVERS ({', '.join(covers)}).")
        named = {r for p in scope.proofs for r in p["requirements"]}
        missing = [r for r in covers if r not in named and not self._test_command_requirement(r)]
        extra = sorted(r for r in named if r not in covers)
        if missing:
            # Say why a requirement that names a test command still needs a
            # scenario, so the one correction addresses it (cycle08: the
            # planner dropped R17 and R21 instead).
            named_commands = [f"{r} names a test command but also claims '{' '.join(words[:12])}'"
                              for r in missing if (words := self._test_command_extra_words(r))]
            return (f"COVERS names {', '.join(missing)} but no proof scenario shows {'it' if len(missing) == 1 else 'them'}."
                    + (" " + "; ".join(named_commands) + ": declare a scenario for that claim and keep the "
                       "requirement in COVERS (the command's own run is cited from the harness's receipt)."
                       if named_commands else ""))
        if extra:
            return f"Proof scenarios name requirements the task does not COVER: {', '.join(extra)}."
        texts = self.memory.ledger.requirements
        for proof in scope.proofs:
            if proof.get("evidence", "test") == "capture":
                # A visual claim is admitted only where the harness will
                # measure it: design work with design evidence in force
                # (never a test or reading exemption for a UI file).
                legacy = ("none" if not is_design_task(spec) else "disabled" if not self.config.design_self_verify
                          else "on")
                if legacy != "on":
                    return (f"Proof scenario {proof['id']} declares capture evidence, but this task is not design "
                            "work with design evidence in force; declare a test case instead.")
                behavioural = [r for r in proof["requirements"] if _behavioural_error(str(texts.get(r, "")))]
                if behavioural:
                    return (f"Proof scenario {proof['id']} declares capture evidence for {', '.join(behavioural)}, "
                            "which asserts error or refusal behaviour; that needs an executed case (evidence test).")
                continue
            if proof.get("evidence", "test") != "reading":
                continue
            # Reading evidence is a supported modality only for a
            # documentation file and never for a requirement about errors
            # or refusals: a behavioural scenario is proved by a case that
            # runs, wherever the planner put it.
            if not str(proof["location"]).lower().endswith(_DOC_SUFFIXES):
                return (f"Proof scenario {proof['id']} declares reading evidence at {proof['location']}, which is "
                        "not a documentation file; a behavioural scenario needs a test case (evidence test).")
            behavioural = [r for r in proof["requirements"] if _behavioural_error(str(texts.get(r, "")))]
            if behavioural:
                # Name the requirement and the behaviour it asserts, and the
                # modality that fits, so the one correction can be made
                # (cycle07: the bare refusal spent the correction budget).
                others = [r for r in proof["requirements"] if r not in behavioural]
                shown = "; ".join(f"{r}: '{_behaviour_text(str(texts.get(r, ''))).strip()[:160]}'"
                                  for r in behavioural)
                return (f"Proof scenario {proof['id']} declares reading evidence for {', '.join(behavioural)}, which "
                        f"asserts error or refusal behaviour of the program ({shown}); prove that behaviour with an "
                        "executed case in its own scenario (evidence test)"
                        + (f", and keep reading evidence for the documentation claim of {', '.join(others)}"
                           if others else "")
                        + ". A requirement that only asks documentation to describe such behaviour takes reading "
                        "evidence.")
        return ""

    def _consume_evidence(self, record: dict) -> None:
        """One evidence record is done, and the acceptance on record has
        consumed it: the final audit's binding moves to the packet as this
        record and its own judgment left it, with the record named, so the
        acceptance stays current across a served challenge; a reassessment
        MET replaces that requirement's verdict. Only when the acceptance
        was current as the record came in (``audit_current``) and nothing
        but this record and its judgment moved the packet since: a record
        the source moved under carries nothing forward. The auditor's own
        packet stays named as ``packet_audited``. Without this, every served
        challenge left the final audit 'not current', a run completed on a
        reassessed MET committed a terminal saying so, and a resume at the
        cap asked the final audit again (adversarial review of e688af5)."""
        kept = self._final_audit
        if (kept is None or not kept.get("binding") or record.get("stage") != "done"
                or not record.get("audit_current")):
            return
        ids = kept.get("ids")
        disposition = str(record.get("disposition", ""))
        reassessed = record.get("kind") == "audit" and disposition.startswith("MET on reassessment")
        if reassessed:
            # The reassessment's own binding, taken as its judgment settled;
            # anything moved since leaves the acceptance where it was.
            if record.get("acceptance_binding") != self._audit_binding(ids):
                return
        else:
            # Nothing but this record may have moved the packet.
            index = next((i for i, r in enumerate(self.evidence_requests) if r is record), None)
            if index is None:
                return
            del self.evidence_requests[index]
            try:
                without = self._audit_binding(ids)
            finally:
                self.evidence_requests.insert(index, record)
            if without != kept["binding"]:
                return
        kept.setdefault("packet_audited", kept.get("packet"))
        kept["consumed"] = [e for e in (kept.get("consumed") or []) if e != record.get("id")] + [record.get("id")]
        if reassessed:
            target = record["target"]
            kept.setdefault("verdicts", {})[target] = [True, disposition]
            if target in self._last_acceptance:
                kept.setdefault("acceptance", {})[target] = dict(self._last_acceptance[target])
        kept["binding"] = self._audit_binding(ids)
        kept["packet"] = self._packet().digest

    _EVIDENCE_ID = re.compile(r"\b(?:RF[A-Z0-9]+-\d+|O\d+|R\d+|check-\d+|receipt-o-\d+)\b")

    def _evidence_resolves(self, text: str) -> bool:
        """Whether an INVALIDATED line names evidence that is on the record:
        a project file or test that exists (the resolution a MET citation
        gets), a recorded check or receipt id, a registered finding or
        obligation id, a requirement on the ledger, or a command one of the
        recorded checks ran. A token that merely looks like a reference
        (root's finding on f4fe5b7: ``UTF8``, a backticked word) closes
        nothing."""
        if self._resolve_citations(text):
            return True
        known = ({r["fid"] for r in self.review_findings} | {o["oid"] for o in self.evidence_obligations}
                 | set(self.memory.ledger.requirements) | {f"check-{i + 1}" for i in range(len(self.checks))}
                 | {f"observed-{i + 1}" for i in range(len(self.review_checks))})
        if set(self._EVIDENCE_ID.findall(text or "")) & known:
            return True
        for check in list(self.checks) + list(self.review_checks):
            for receipt in check.get("receipts") or ():
                if receipt.get("id") and str(receipt["id"]) in text:
                    return True
            command = str(check.get("command") or "")
            if command and command in text:
                return True
        return False

    def _gate_can_run(self, paths) -> bool:
        """Whether the harness's own gate runs what a NOT RUN item names:
        every path is a whole operand of one of the effective gate's
        commands that is not skipped (integration.operand_paths, the same
        reading ``_discharging_check`` applies to a receipt). A path no
        command names is a prerequisite nobody here can run: its obligation
        gets no runner and lowers the requirement until someone can."""
        gate = self._current_gate()
        if gate is None or not self.project or not paths:
            return False
        from .integration import _gate_commands, operand_paths
        root = Path(self.project).resolve()
        try:
            commands = [c for c in (_gate_commands(gate, root) or []) if not c.skip_reason]
        except Exception:  # noqa: BLE001 -- an unreadable gate runs nothing
            return False
        named = set()
        for command in commands:
            named |= set(operand_paths(list(command.argv), root, command.cwd))
        return all(p in named for p in paths)

    def _authority_record(self) -> dict:
        """The approval authority a judgment is made under: every
        requirement as a typed revision (delivery.RequirementRevision; an
        inferred reading, approved by the cross-vendor requirements review
        when one succeeded), the operator's rulings, the orchestrator's
        decisions and ambiguity notes, the goal and the plan gate's
        receipt. Its digest is in every packet and is what a resumed
        episode must find committed (checkpoint._same_authority)."""
        parent = getattr(self, "_authority_parent", None)
        if parent is not None:
            # A parallel child judges under the parent's requirements,
            # rulings, decisions and plan gate: its authority is the
            # parent's record, never one built from its own empty ledger.
            return parent._authority_record()
        ledger = self.memory.ledger
        revisions = self._requirement_revisions()
        # The cross-vendor requirements review that covered a text exactly as
        # it stands is recorded beside the row as a review, never as the
        # decision receipt: a model's review is not the human's consent, and
        # a revision it never saw is not reviewed (Astra's authority control
        # on 04a967d). A text the reviewer itself proposed (an ADD line) is
        # its proposal, not a review: nobody has compared it with the goal.
        reviewed: Dict[tuple, str] = {}
        proposed: Dict[tuple, str] = {}
        for review in self.requirement_reviews:
            if review.get("reviewer") and str(review.get("result", "")) == "reviewed":
                for rid, digest in (review.get("texts") or {}).items():
                    reviewed[(rid, digest)] = str(review["reviewer"])
                for rid, digest in (review.get("proposed") or {}).items():
                    proposed[(rid, digest)] = str(review["reviewer"])
        rows = []
        for rid, text in ledger.requirements.items():
            if not str(text or "").strip():
                continue
            row = delivery.RequirementRevision(rid, int(revisions.get(rid, 1)), str(text), origin="inferred",
                                               decision_receipt=None)
            reviewer = reviewed.get((rid, row.text_digest))
            proposer = proposed.get((rid, row.text_digest))
            rows.append(dict(row.to_dict(),
                             review=(dict(reviewer=reviewer, text_digest=row.text_digest) if reviewer else None),
                             proposal=(dict(reviewer=proposer, text_digest=row.text_digest) if proposer else None)))
        body = dict(goal=hashlib.sha256((self.memory.goal or "").encode()).hexdigest()[:16], requirements=rows,
                    rulings=list(ledger.rulings), decisions=dict(ledger.decisions), ambiguous=dict(ledger.ambiguous),
                    plan_gate=self._plan_gate_receipt, policy="authority/3")
        if self.test_change_decisions:
            body["test_changes"] = [dict(d) for d in self.test_change_decisions]
        body["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]
        return body

    def _publish_authority(self) -> Optional[str]:
        """Commit the current authority record to the job store (once per
        distinct record) and return its digest, for the checkpoint written
        at the same quiescent point. No store, no digest: a checkpoint
        without one is read as before."""
        parent = getattr(self, "_authority_parent", None)
        if parent is not None:
            return parent._publish_authority()
        store = self.config.job_store
        if store is None:
            return None
        with self._authority_lock:
            # Under a lock: a parallel wave's children commit through the
            # parent at once.
            record = self._authority_record()
            if record["digest"] != self._published_authority:
                store.publish({"authority": record})
                self._published_authority = record["digest"]
            return record["digest"]

    def _authority_before(self, what: str) -> str:
        """Commit the authority an effect is about to run under, before the
        effect (every operation and every model call, a task's and a wave's
        first call among them), and return its digest: the one the effect's
        identity names. Idempotent on an unchanged record. A store that
        cannot commit it stops the run here, with nothing started; with no
        store the digest is still the effect's name for it."""
        try:
            published = self._publish_authority()
        except Exception as exc:  # noqa: BLE001 -- the store is latched; nothing starts without the record
            raise JobStoreUnavailable(f"{what} not started: the approval authority could not be committed "
                                      f"({type(exc).__name__})") from exc
        return published or self._authority_record()["digest"]

    @staticmethod
    def _scenario_text(o: dict) -> Tuple[str, bool]:
        """The obligation's canonical scenario and location as one line
        (proof id, operation, prior state, expectation, location, declared
        evidence, bound cases), and whether it fits the render bound. A row
        with no scenario (a legacy string) is wholly its behaviour, which
        is always shown."""
        scenario = dict(o.get("scenario") or {})
        if not scenario and not o.get("location") and not o.get("cases"):
            return "", True
        fields = [(k, scenario[k]) for k in ("proof", "operation", "state", "expect", "location", "evidence", "capture")
                  if k in scenario]
        if "location" not in scenario and o.get("location"):
            fields.append(("location", str(o["location"])))
        if o.get("cases"):
            fields.append(("bound cases", ", ".join(o["cases"])))
        text = "; ".join(f"{k}: {v}" for k, v in fields)
        return text, len(text) <= _SCENARIO_RENDER_MAX_CHARS

    @classmethod
    def _scenario_shown(cls, o: dict) -> bool:
        """Whether a judging seat can be shown this row's whole scenario:
        the condition under which a SUFFICIENT about it is accepted at all
        (Astra's control on b18ca98: wording alone is not enforcement)."""
        return cls._scenario_text(o)[1]

    @classmethod
    def _scenario_line(cls, o: dict) -> str:
        """The scenario line for the seats that judge it (the audit and a
        reassessment); over ``_SCENARIO_RENDER_MAX_CHARS`` the line says the
        scenario is not shown and the row is to be left unjudged, never a
        silent prefix (Astra's finding on 00cfc02), and the harness then
        accepts no SUFFICIENT for it (``_scenario_shown``)."""
        text, shown = cls._scenario_text(o)
        if not text:
            return ""
        if not shown:
            return (f"\n  scenario: {len(text):,} characters, over the {_SCENARIO_RENDER_MAX_CHARS:,}-character bound "
                    "for this prompt and not shown; leave this row unjudged (an explicit INSUFFICIENT is allowed).")
        return f"\n  scenario: {text}"

    @staticmethod
    def _scenario_field(o: dict, key: str) -> str:
        return str(dict(o.get("scenario") or {}).get(key) or "")

    @classmethod
    def _behavior_is_generated(cls, o: dict) -> bool:
        """Whether a row's behaviour is exactly the summary
        ``_register_acceptance_obligations`` generates from its own declared
        scenario (proof, operation, prior state, expectation, location),
        bounded as it is stored: then it says nothing the scenario line does
        not. A legacy row, a mixed record or a behaviour that differs in any
        character is its own claim and is never treated as redundant."""
        scenario = dict(o.get("scenario") or {})
        keys = ("proof", "operation", "state", "expect", "location")
        if not all(k in scenario for k in keys):
            return False
        generated = (f"{scenario['proof']}: {scenario['operation']}; given {scenario['state']}; "
                     f"expect {scenario['expect']}; in {scenario['location']}")
        return str(o.get("behavior") or "") == generated[:300]

    # -- evidence loop -------------------------------------------------------
    def _finish_pending_evidence(self) -> None:
        """Finish, once, each reassessment a resumed job left unstarted: a
        passed proof whose record reached "awaiting reassessment" and no
        further. Only with the identity it was bound to (source and checks)
        unchanged and the finding still disputable; the proof is not run
        again and the original audit is not asked again. A record whose
        judgement may have started is never retried, and an outcome already
        recorded MET on this identity is carried, never re-judged."""
        identity = self._evidence_identity() if self.project else None
        binding = self._audit_binding(self._final_audit.get("ids") if self._final_audit else None)
        for record in self.evidence_requests:
            if record.get("kind") != "audit" or record.get("identity") != identity:
                continue
            if (str(record.get("disposition", "")).startswith("MET on reassessment")
                    and record.get("acceptance_binding") == binding):
                # Carried only on exactly the packet the reassessment was
                # accepted on; a packet that moved since leaves the final
                # audit's own verdict standing (root's cache finding).
                self.memory.ledger.requirement_status[record["target"]] = (
                    f"met (reassessed on evidence {record['id']})")
        for record in self.evidence_requests:
            if record.get("stage") == "intent":
                # A durable intent a resumed job never runs (the evidence
                # never started): closed as it stands and consumed, so the
                # acceptance on record stays current rather than the final
                # audit being drawn again over a record nobody will finish
                # (adversarial review of e688af5).
                record["stage"] = "done"
                self._consume_evidence(record)
        for record in self.evidence_requests:
            if record.get("stage") != "awaiting reassessment":
                continue
            if record.get("identity") != identity or not record.get("test"):
                record["stage"] = "done"
                record["disposition"] = "held: the source or checks changed before the pending reassessment"
                self._consume_evidence(record)
                continue
            entry = self._disputable().get(record["target"])
            if entry is None or entry["kind"] != record["kind"] or entry["finding"] != record.get("finding"):
                record["stage"] = "done"
                record["disposition"] = "held: the finding is no longer the one the proof was made for"
                self._consume_evidence(record)
                continue
            self._note(f"evidence {record['id']}: finishing the reassessment a resumed job left unstarted")
            record["disposition"] = self._reassess(record["target"], entry, record, record["test"])
            record["stage"] = "done"
            self._consume_evidence(record)
            self._quiescent()

    def _record_boundary_notes(self, key, role, reply: str, *, task=None) -> None:
        """Keep a reviewer's 'BOUNDARY R<n>:' lines (cycle12) as observations:
        bounded, attributed, bound to the source they were written on. They
        are never proof, a verdict or a gate."""
        current = None
        for match in list(_BOUNDARY_NOTE.finditer(reply or ""))[:_BOUNDARY_NOTES_PER_REPLY]:
            if current is None:
                current = (self._source_fingerprint() if self.project else None) or ""
            self.boundary_notes.append(dict(task=task or getattr(self._active_spec, "task_id", "run"), author=key,
                                            role=role, requirement=match.group(1),
                                            note=match.group(2)[:_BOUNDARY_NOTE_CHARS], source=current or None))

    def _boundary_notes_block(self, rids) -> str:
        """For the audit: what reviewers said they checked for each covered
        error requirement, or 'none stated' (cycle12). Observations only."""
        texts = dict(self._requirement_texts_now())
        errors = [r for r in rids if _behavioural_error(texts.get(r, ""))]
        if not errors:
            return ""
        current = self._source_fingerprint() if self.project else None
        lines = []
        for rid in errors:
            notes = [n for n in self.boundary_notes if n.get("requirement") == rid][-3:]
            if not notes:
                lines.append(f"- {rid}: none stated")
                continue
            for n in notes:
                where = "this source" if current and n.get("source") == current else "an earlier source"
                lines.append(f"- {rid}: {n['author']} ({n['role']}, {n['task']}, {where}): {n['note']}")
        return ("\n\n## Input layers reviewers said they checked (observations, not proof)\n\n"
                "Each line is a reviewer's own note of what it checked for a requirement about errors or invalid "
                "input. It is not evidence that a layer is handled and decides nothing; judge every layer the "
                "requirement's input passes through yourself.\n" + "\n".join(lines))

    def _discovering_checks(self) -> list:
        """The approved gate commands that would run a new test file added
        to a copy of the project (integration.discovers_added_tests). A
        parallel child reads the commands its parent approved."""
        if not self.project:
            return []
        from . import integration
        root = Path(self.project).resolve()
        if self.config.integration_gate is not None:
            commands = integration._gate_commands(self.config.integration_gate, root) or []
        else:
            commands = getattr(self, "_inherited_checks", None) or []
        return [c for c in commands if not c.skip_reason
                and integration.discovers_added_tests(integration.model_facing(c.argv), root / c.cwd)]

    def _evidence_route(self, rid: str, why: str = "") -> dict:
        """Where an unmet requirement goes at the finish (evidence lifecycle
        batch, 2026-10-29): ``execution debt`` when a standing NOT RUN item
        of a covering task holds it (discharged only by the harness's gate
        executing what the item binds, or a repair task; a scratch test
        shows nothing about it); ``prerequisite`` when an obligation of its
        has no authorized runner; else a ``finding`` on the behaviour, which
        executed evidence may dispute (a false positive is legitimate) and
        which, confirmed, is a repair task's while a slot is left or an
        honest incomplete. finding-lifecycle-02 spent three challenges, about
        594k tokens, on requirements held by runner-less NOT RUN rows."""
        inspections = [g for g in self._open_inspections() if rid in (g.get("requirements") or ())]
        if inspections:
            return dict(kind="inspection", challengeable=False, route=(
                "inspection: " + "; ".join(f"{g['task']} could not read {', '.join(g['files'])}" for g in inspections)
                + "; attested only by the READ line of a completed review or recheck it was handed to, on those "
                  "bytes (a review-only verification task is handed them); never a challenge "
                  "and never a test run")[:500])
        standing = [e for e in self._standing_not_run() if rid in (e.get("requirements") or ())]
        if standing:
            # Each item's own route, truthfully: a route that cannot clear it
            # is never offered (cycle02; completion-cycle01 was told a repair
            # task would run O15, which named production modules).
            parts = []
            for e in standing:
                oid = e.get("oid") or "the item"
                cases = ", ".join(e.get("cases") or [])
                executable = self._not_run_runnable(e)
                gone = self._bound_cases_gone(e)
                if gone:
                    parts.append(f"{oid}: its bound case {', '.join(gone)} is no longer defined on the current "
                                 "source; a run never clears it. Route: its own task's lead (or a task naming "
                                 f"'DISCHARGES: {oid}') restates it with 'RUNS: {oid} - file::[Class::]test_case' "
                                 "lines for exact current cases; the harness settles them from a complete passing "
                                 "run on the current source and an independent seat judges whether they exercise "
                                 "everything the item originally reported")
                elif e.get("restatements") and self._discharge_record(e) is not None:
                    # Who judges it and what stands, truthfully (cycle18): the
                    # audit never judges its own restatement, and a current
                    # adverse judgment carries its reason and repair route.
                    parts.append(f"restated as {cases} and executed on this source; " + self._restatement_status(e))
                elif e.get("cases"):
                    parts.append(f"{oid}: discharged only by the harness's gate executing {cases} whole on the "
                                 "current source, gate and environment"
                                 + ((" (restated; " + (self._restatement_status(e) if self._restated_current(e)
                                                       else "then judged by " + self._restatement_judge(e)[1]) + ")")
                                    if e.get("restatements") else ""))
                elif executable:
                    parts.append(f"{oid}: discharged only by a complete run naming "
                                 f"{', '.join(_item_paths(str(e.get('item') or '')))} whole")
                else:
                    parts.append(f"{oid}: names nothing a check can run "
                                 f"({(e.get('binding') or {}).get('problem') or _display_item(e['item'])[:80]}); a repair "
                                 f"task that runs checks never clears it. Route: its own task's lead restates it with "
                                 f"'RUNS: {oid} - file::[Class::]test_case' lines, or a later task naming "
                                 f"'DISCHARGES: {oid}' has its lead write them once the cases exist; the harness settles "
                                 "them from a complete passing run on the current source and the audit judges whether "
                                 "they exercise the whole reported behaviour")
            route = "execution debt: " + "; ".join(parts) + "; never a challenge"
            audit = self.requirement_audits[-1] if self.requirement_audits else {}
            if rid in (audit.get("stated") or ()) and audit.get("source") == (
                    self._source_fingerprint() if self.project else None):
                # A defect the auditor found in the behaviour itself stands
                # beside the debt (cycle18: a genuine product defect and held
                # evidence on one requirement); settling the debt does not fix
                # it, and fixing it does not settle the debt.
                route = ("the auditor's own NOT MET on the behaviour (its reason above) is a finding: a repair task "
                         f"covering {rid} fixes it. Separately, " + route)
            return dict(kind="execution debt", challengeable=False, route=route[:1200])
        typed_debt = [o for o in self.evidence_obligations
                      if any(r[0] == rid for r in o.get("requirements") or ()) and not o.get("superseded_by")
                      and delivery.purpose_of(o) != "acceptance"
                      and (delivery.purpose_of(o) == "unknown" or o.get("execution") != "ran")]
        if typed_debt:
            # The typed record holds what the entry-level reading may not
            # (a skip fact executed on another source, a mixed record):
            # execution debt, settled only by the harness, never a challenge
            # (quality/completion batch, 2026-10-31).
            route = ("execution debt (typed record): " + "; ".join(
                f"{o['oid']} " + ("purpose unknown; held, never credited" if delivery.purpose_of(o) == "unknown"
                                  else f"execution {o.get('execution')}" + (f" ({o['reason']})" if o.get("reason") else ""))
                for o in typed_debt)
                     + "; discharged only by the harness's gate executing what it binds on the current source, or a "
                       "repair task that runs it; never a challenge")
            return dict(kind="execution debt", challengeable=False, route=route[:500])
        unrun = [o for o in self.evidence_obligations
                 if any(r[0] == rid for r in o.get("requirements") or ()) and o.get("modality") == "test"
                 and not o.get("superseded_by") and o.get("execution") != "ran"]
        stale = [o for o in unrun if str(o.get("binding") or "").startswith("stale")
                 or (o.get("execution") == "invalid" and o.get("cases"))]
        if stale:
            # The existing exact-redeclaration route, named (cycle02;
            # completion-cycle01 O24 was bound to a case a later task removed):
            # a task declaring the same scenario exactly supersedes the row,
            # binds the current case and is judged afresh.
            def told(o):
                scenario = dict(o.get("scenario") or ())
                return (f"{o['oid']} ({o.get('task')} {scenario.get('proof_id') or ''}: operation "
                        f"{scenario.get('operation') or o.get('behavior')!r}; state {scenario.get('state')!r}; "
                        f"expect {scenario.get('expect')!r}; {o.get('binding') or o.get('reason') or 'not executed'})")
            return dict(kind="stale binding", challengeable=False, route=(
                "stale binding: " + "; ".join(told(o) for o in stale)
                + ". Route: a task that declares the same scenario exactly (the same operation, state, expect, "
                  "requirements and evidence kind; no edits are needed when the cases exist) supersedes the row; its "
                  "lead binds the current case with PROVES and its reviewer and the audit judge it afresh. The earlier "
                  "verdict never transfers; never a challenge")[:900])
        prerequisites = [o for o in unrun if o.get("runner") is None]
        if prerequisites:
            return dict(kind="prerequisite", challengeable=False,
                        route=("prerequisite: " + ", ".join(o["oid"] for o in prerequisites)
                               + " has no authorized runner; a task that binds it to a case the gate runs, never a challenge")[:500])
        failed = [o for o in unrun if o.get("execution") == "failed"]
        if failed:
            # A read-only proof cannot repair a required run that failed;
            # the reassessment's own acceptance would refuse it (root's
            # finish seam control: the planner call was bought for nothing).
            # A missing or stale execution is not withheld: the challenge's
            # own proof may be the bound case executed on this source.
            return dict(kind="failed execution", challengeable=False,
                        route=("; ".join(f"{o['oid']} failed" + (f" ({o.get('reason')})" if o.get("reason") else "")
                                         for o in failed)
                               + ": a repair task whose checks pass it, never a challenge")[:500])
        open_reviews = self._open_findings_touching(rid)
        if open_reviews:
            return dict(kind="open review finding", challengeable=False,
                        route=("held by open review finding(s) " + ", ".join(open_reviews)
                               + ": their own reviewer's recheck decides them; a challenge of the audit cannot")[:500])
        return dict(kind="finding", challengeable=True,
                    route="finding on the behaviour: executed evidence may dispute it (a false positive is "
                          "legitimate); confirmed, it is a repair task's while a slot is left, else the run ends "
                          "incomplete and says so")

    def _disputable(self) -> Dict[str, dict]:
        """The findings an evidence request may test now, by id: each NOT MET
        line the latest audit's auditor itself wrote on the current source
        (never a lowered MET, an uncited MET or a missing verdict, which are
        the harness's own facts), and each held review finding still open
        (never a deferred one, a design verdict or a security verdict)."""
        out: Dict[str, dict] = {}
        audit = self.requirement_audits[-1] if self.requirement_audits else None
        status = self.memory.ledger.requirement_status
        if (audit and "verdicts" in audit and audit.get("auditor") and audit.get("source")
                and audit["source"] == self._source_fingerprint()):
            lowered = {e.get("requirement") for e in audit.get("lowered") or ()}
            for rid in audit.get("stated") or ():
                if rid in lowered or not str(status.get(rid, "")).startswith("NOT MET"):
                    continue
                why = (audit["verdicts"].get(rid) or {}).get("why", "")
                out[rid] = dict(kind="audit", reviewer=audit["auditor"], finding=f"{rid}: NOT MET - {why}",
                                disposition="NOT MET", identity=rid)
        owed = {r.get("fid") for r in self.review_findings if r.get("kind") == "unreviewed"}
        for number, entry in enumerate(self.unresolved_reviews, 1):
            if (entry.get("deferred") and not entry.get("deferred_rechecked")) or \
                    entry.get("text") not in self.open_findings:
                continue
            if entry.get("finding_id") in owed:
                # Owed review debt is settled by its owner's judgment, never
                # disputed by a test (recovery lifecycle, 2026-10-31).
                continue
            finding = (entry.get("finding") or entry["text"]).strip()
            out[f"U{number}"] = dict(kind="review", reviewer=entry["peer"], finding=finding,
                                     disposition="UNRESOLVED", task=entry["task"], entry=entry,
                                     identity=hashlib.sha256(f"{entry['task']}\0{entry['peer']}\0{finding}"
                                                             .encode()).hexdigest()[:16])
        return out

    def _current_checks_digest(self) -> str:
        from .integration import checks_digest
        return checks_digest(self._discovering_checks())

    def _scratch_identity(self, source, checks) -> str:
        """What a scratch proof is bound to: the source its copy was handed and
        the approved commands it ran with (both measured at hand-out by the
        runtime), the project root and its excludes. Never shown to a model."""
        body = dict(source=source, checks=checks, root=str(Path(self.project).resolve()) if self.project else "",
                    excludes=sorted(map(str, self.config.project_excludes or ())))
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]

    def _handed_drift(self, proof) -> str:
        """Why a scratch proof is not about the current source and checks: the
        copy's handed bytes or commands differ from now, or were never
        measured. "" when they hold. The raw result is kept either way."""
        proof = proof or {}
        if not proof:
            return ""
        if not proof.get("handed_source") or not proof.get("handed_checks"):
            return "the handed source or checks were not measured"
        if proof["handed_source"] != self._source_fingerprint():
            return "the source moved after the copy was handed"
        if proof["handed_checks"] != self._current_checks_digest():
            return "the approved checks changed after the copy was handed"
        return ""

    def _evidence_note(self) -> str:
        """The evidence channel, offered only where the harness can serve it
        (a configured check that discovers a new test file) and there is
        something to test: a disputable finding or a failure on record."""
        if not (self.project and self._discovering_checks()):
            return ""
        identity = self._evidence_identity()
        mine = [r for r in self.evidence_requests if r.get("identity") == identity]
        tested = {tuple(r["key"][:2]) for r in mine}
        open_ = {fid: e for fid, e in self._disputable().items() if (e["kind"], e["identity"]) not in tested}
        failing = bool(self._partial_tasks) or bool(self.checks and not self.checks[-1].get("passed"))
        if not open_ and not failing:
            return ""
        listed = "; ".join(f"{fid} ({'audit' if e['kind'] == 'audit' else 'task ' + e['task'] + ' review'}): "
                           f"{e['finding'][:200]}" for fid, e in open_.items())
        return (
            "\n\nEXECUTED EVIDENCE: to test a finding you dispute, or to investigate a failure, "
            + _EVIDENCE_CHANNEL + " A failing test confirms the defect: repair it with a task. A held review "
            "finding blocks completion until its reviewer resolves it."
            + (f"\nFindings you may test: {listed}." if listed else "")
            + ("\nAlready tested on this source: " + "; ".join(
                f"{r['id']} on {r['target']}: {r['disposition'][:160]}" for r in mine) + "." if mine else ""))

    def _run_evidence(self, path: str, text: str) -> dict:
        """The harness's own run of one new test file, in a disposable copy of
        the current source, with the approved discovering checks, inside the
        scratch sandbox (runtime._scratch_proof). Returns status, reason and
        case outcomes for models, and under ``execution`` the receipts and
        check ids for the record only; never a command line or a path
        outside the copy reaches a model. A file that rewrote handed source,
        or a project that changed while it ran, is not run."""
        import shlex

        from .integration import _gate_commands, model_facing
        from .project import Project
        from .runtime import _copy_digests, _private_paths, _scratch_proof
        base = dict(files=[path], cases=[], execution=dict(checks=[], receipts=[]))
        checks = self._discovering_checks()
        if not checks:
            return dict(base, status="not run", reason="no configured check discovers a new test file here")
        base["execution"]["checks"] = [c.id for c in checks]
        project = Project(self.project, exclude=self.config.project_excludes)
        before = self._capture_source()
        if path in project.contents():
            return dict(base, status="not run", reason="the file already exists in the project; evidence is a new file")
        root = Path(self.project).resolve()
        gate = self.config.integration_gate
        every = ((_gate_commands(gate, root) or []) if gate is not None
                 else list(getattr(self, "_inherited_checks", None) or []))
        commands = sorted({shlex.join(argv) for c in every for argv in (tuple(c.argv), tuple(model_facing(c.argv)))})
        try:
            with self._operation("evidence run", inputs=dict(
                    path=path, text=hashlib.sha256(text.encode()).hexdigest()[:32], checks=[c.id for c in checks],
                    source=self._source_fingerprint()), effects=("run-scratch",)) as op, \
                    project.snapshot() as directory:
                handed = _copy_digests(directory)
                target = Path(directory) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                proof = _scratch_proof(directory, [path], handed, checks,
                                       private=_private_paths(self.project, commands, directory))
                op["result"] = dict(status=proof.get("status"), reason=proof.get("reason"))
        except OSError as exc:
            return dict(base, status="not run", reason=f"the copy could not be prepared: {type(exc).__name__}")
        if before is None or self._capture_source() != before:
            proof = dict(proof, status="not run", reason="the project changed while the evidence ran"
                         if before is not None else "the project could not be read around the evidence run")
        base["execution"]["receipts"] = list(proof.get("receipts") or [])
        return dict(base, status=str(proof.get("status")), reason=str(proof.get("reason") or ""),
                    cases=list(proof.get("cases") or [])[:40])

    def _scratch_receipt(self, record: dict) -> Optional[dict]:
        """A harness receipt for ``record`` from a reviewer's scratch
        demonstration (``scratch_proofs``, root on 9bedde0): the proof
        passed in full, its handed source and checks are the current ones,
        it was bound to this obligation before the reviewer ran (a snapshot
        taken at the collaborator call), every captured field still equals
        the obligation and the run, its scratch files' hashes were measured
        in the copy, and every bound case is among its passed cases. Its
        gate is the producer gate that actually ran, recorded at the call,
        never the obligation's. Missing bindings, hashes or producer
        identity is no receipt."""
        if record.get("kind") != "acceptance" or record.get("modality", "test") != "test":
            return None
        cases = [str(c) for c in record.get("cases") or ()]
        if not cases or not self.project:
            return None
        identity = self._scratch_identity(self._source_fingerprint(), self._current_checks_digest())
        for proof in reversed(self.scratch_proofs):
            if (proof.get("task") != record.get("task") or proof.get("status") != "passed"
                    or proof.get("eligible") is not True or proof.get("identity") != identity):
                continue
            if (proof.get("producer_gate") is None or not isinstance(proof.get("file_hashes"), dict)
                    or not proof["file_hashes"]):
                continue
            binding = next((b for b in proof.get("bindings") or () if b.get("oid") == record["oid"]), None)
            if binding is None or not self._binding_matches(binding, record, evidence=proof.get("id")):
                continue
            if binding.get("source") != proof.get("handed_source"):
                continue
            if not set(cases) <= self._proof_passed_cases(proof):
                continue
            if not all(c.partition("::")[0] in proof["file_hashes"] for c in cases):
                continue
            return dict(id=f"{proof['id']}:{record['oid']}", source=self._source_fingerprint(),
                        gate=proof["producer_gate"], runner="gate", provenance="reviewer scratch proof",
                        evidence=proof["id"], peer=proof.get("peer"), files=list(proof.get("files") or ()),
                        file_hashes=dict(proof["file_hashes"]), environment=self._environment_id(),
                        requirements=[list(r) for r in binding.get("requirements") or ()], completed=True,
                        passed=True, passed_cases=list(cases), skipped_cases=[])
        return None

    @staticmethod
    def _proof_passed_cases(receipt: dict) -> set:
        """The case identities a passed scratch proof shows executed and
        passed, by file-qualified identity: every exact (parametrized)
        instance whose own outcomes are all passed, and a case's base name
        only when every recorded instance of it passed (root's F2 on
        9bedde0: exact names were dropped by the collapse)."""
        if not receipt or receipt.get("status") != "passed":
            return set()
        exact: Dict[str, set] = {}
        for row in receipt.get("cases") or ():
            key, sep, outcome = str(row).rpartition(": ")
            if not sep or "::" not in key:
                continue
            exact.setdefault(key, set()).add(outcome.strip())
        passed = {case for case, seen in exact.items() if seen == {"passed"}}
        bases: Dict[str, set] = {}
        for case, seen in exact.items():
            bases.setdefault(case.split("[", 1)[0], set()).update(seen)
        passed |= {base for base, seen in bases.items() if seen == {"passed"}}
        return passed

    def _evidence_receipt(self, record: dict) -> Optional[dict]:
        """A harness receipt for ``record`` from a served evidence proof:
        the proof passed, its bound source and checks never drifted, it was
        bound to this obligation at intent, every captured field equals the
        obligation and the run as they are now, and every bound case is
        among the proof's passed cases. Any missing or unequal field is no
        receipt (fail closed). The receipt keeps its scratch provenance: it
        names the evidence record and file, and is never an integration
        pass of the whole project."""
        if record.get("kind") != "acceptance" or record.get("modality", "test") != "test":
            return None
        cases = [str(c) for c in record.get("cases") or ()]
        if not cases:
            return None
        current = self._source_fingerprint() if self.project else None
        identity = self._evidence_identity() if self.project else None
        for request in reversed(self.evidence_requests):
            receipt = request.get("receipt") or {}
            if receipt.get("status") != "passed" or request.get("identity") != identity:
                continue
            if str(request.get("disposition", "")).startswith("held: the source or checks changed"):
                continue
            # The gate that actually produced the run, persisted at intent
            # and confirmed after execution (root's F1 on 9bedde0): a
            # receipt never carries the obligation's gate as its own.
            producer = request.get("producer_gate")
            if producer is None or request.get("producer_gate_executed") != producer:
                continue
            binding = next((b for b in request.get("bindings") or () if b.get("oid") == record["oid"]), None)
            if binding is None or not self._binding_matches(
                    binding, record, evidence=request.get("id"), file=request.get("file"),
                    file_sha256=request.get("file_sha256")):
                continue
            if binding.get("source") != request.get("source"):
                continue
            if not set(cases) <= self._proof_passed_cases(receipt):
                continue
            return dict(id=f"{request['id']}:{record['oid']}", source=current, gate=producer, runner="gate",
                        provenance="scratch evidence proof", evidence=request["id"], file=request.get("file"),
                        file_sha256=request.get("file_sha256"), environment=self._environment_id(),
                        requirements=[list(r) for r in binding.get("requirements") or ()], completed=True,
                        passed=True, passed_cases=list(cases), skipped_cases=[])
        return None

    def _reassess(self, target: str, entry: dict, record: dict, text: str) -> str:
        """The one reassessment of a finding on new executed evidence, by the
        model that made it. Returns the disposition. The proof's identity
        (source and effective checks) must hold before the call is spent and
        after it returns, or the finding is held. An audit MET still needs a
        resolved citation and is lowered by any standing NOT RUN or unrun
        record (_lower_unexecuted); a review RESOLVED closes exactly that
        finding. Anything else keeps the finding held. The whole test file is
        shown: the size bound keeps it complete."""
        reviewer = entry["reviewer"]
        if self._evidence_identity() != record["identity"]:
            return "held: the source or checks changed after the evidence was bound"
        if not self._available(reviewer):
            return "held: the reviewer that made the finding is unavailable, and no other model reassesses it"
        receipt = record["receipt"]
        evidence = (f"The harness ran this new test file, {record['file']}, unchanged and shown here whole, in a "
                    f"disposable copy of exactly the current source with the project's own configured checks:"
                    f"\n```\n{text}\n```\nResult: {receipt['status']} - {receipt['reason']}\nCases: "
                    + ("; ".join(receipt.get("cases") or ()) or "none shown")
                    + (f"\nThe orchestrator says it shows: {record['claim']}" if record.get("claim") else "")
                    + "\nA passing test shows only what it executes. Judge whether it exercises the condition "
                    "your finding names, and whether the finding holds on paths the test does not reach.")
        # Durable before any reservation: a job interrupted from here on never
        # retries this judgement, since it may have started.
        record["stage"] = "reassessment started"
        self._quiescent()
        saved = self._active_spec
        try:
            if entry["kind"] == "audit":
                rid = target
                requirement = self.memory.ledger.requirements.get(rid, "")
                self._refresh_obligations()
                bound = [o for o in self.evidence_obligations if any(r[0] == rid for r in o["requirements"])]
                prompt = (self._authority_context({rid: requirement})
                          + f"\n\nYou audited the delivered work and found:\n{entry['finding']}\n\n"
                          + evidence + self._obligations_block(bound) + "\n\n" + _EVIDENCE_AUTHORITY
                          + "\n\nThe project in your working directory is the delivered work. " + _ERROR_PATH_RULE
                          + " " + _EXECUTED_ERROR_RULE
                          + f" Reassess {rid} alone, once. Reply with exactly one line: '{rid}: MET - <project "
                          f"file path or test name that shows it; for an error or refusal requirement, also the "
                          f"exact executed case identities the rule above names, beside a project file or test "
                          f"that resolves>' or '{rid}: NOT MET - <what is still missing>'. A MET without a cited "
                          "project file or test is counted as not met. " + _AUDIT_LINE_RULE
                          + " A citation resolves only as a "
                          "project-relative file path with an extension, or a test function named test_* defined "
                          "in a file whose name starts with test and ends in .py; other names do not resolve, and "
                          "the scratch test the harness ran is not a project file."
                          + ("" if not any(o.get("execution") == "ran" and delivery.purpose_of(o) == "acceptance"
                                           for o in bound) else
                             " Each executed acceptance obligation listed above needs its own line, "
                             "'<O-id>: SUFFICIENT - <why>' or '<O-id>: INSUFFICIENT <what it does not establish>'; "
                             "your verdict on the requirement never judges a row it binds, and an obligation "
                             "left unjudged or judged insufficient keeps the requirement not met. An "
                             "execution-debt row is not judged; a line about one is applied to nothing.")
                          + ("" if not any(o.get("modality") in ("legacy", "reading") for o in bound) else
                             " Each legacy acceptance or reading row listed above needs its own '<O-id>: "
                             "SUFFICIENT - <evidence read, citing the file>' or '<O-id>: INSUFFICIENT <gap>' line; "
                             "it is never judged by your verdict on its requirement.")
                          + ("" if not bound else " " + _PLAIN_VERDICT_RULE))
                self._active_spec = None
                with invocation("audit", "auditor"):
                    reply = self._invoke_model(reviewer, prompt)
            else:
                texts = self.memory.ledger.requirements
                covered = next((o.covers for o in self.task_outcomes if o.task_id == entry["task"]), None)
                relevant = {r: texts[r] for r in (covered or texts) if r in texts}
                prompt = (self._authority_context(relevant)
                          + f"\n\n## Task {entry['task']} (this run's reading of part of the goal)\n\n"
                          f"{entry['entry'].get('description') or entry['task']}\n\nYou reviewed this "
                          f"task and this finding was left open:\n{entry['finding']}\n\n" + evidence
                          + "\n\n" + _EVIDENCE_AUTHORITY
                          + "\n\nYour working directory holds the current source. Check only this finding "
                          "against the source and that executed evidence; 'the revision' below is the "
                          "source as it now stands. "
                          + self._recheck_instructions([entry["entry"].get("finding_id") or "the finding"])
                          + self._review_turn_budget_note(reviewer))
                binding = self._judgment_binding(SimpleNamespace(task_id=entry["task"]), reviewer, "reassessment")
                with invocation(entry["task"], "recheck"):
                    reply = self._invoke_model(reviewer, prompt)
        except TurnLimitReached:
            return "held: the reassessment stopped at its turn cap"
        except ProviderError as exc:
            return f"held: the reassessment failed ({type(exc).__name__})"
        finally:
            self._active_spec = saved
        record["reassessment"] = (reply or "").strip()[:1000]
        if self._evidence_identity() != record["identity"]:
            return "held: the source or checks changed during the reassessment"
        if entry["kind"] == "review":
            fid = entry["entry"].get("finding_id")
            if fid is None:
                return "held: the finding has no id on the registry"
            spec = SimpleNamespace(task_id=entry["task"])
            reading = judgment.recheck_verdicts(reply or "", [fid])
            self._apply_review_reading(spec, reviewer, "reassessment", reading, binding, call=record["id"])
            current = next((r for r in self.review_findings if r["fid"] == fid), None)
            if current is None:
                return "held: the finding left the registry"
            if current["closed"]:
                self._close_review_finding(entry["entry"], f"{current['state']} by {reviewer} on evidence {record['id']}")
                return f"{current['state']} on reassessment by the same reviewer"
            if current["state"] == judgment.UNRESOLVED:
                return "confirmed on reassessment: the reviewer kept the finding"
            return "held: the reassessment gave no valid verdict"
        # The first matching line no longer decides: the reply is read by the
        # one audit-line reader (quadratus.verdicts.audit_lines), the same
        # rule the audit's own lines get. A MET stands only as a plain
        # 'MET - <citation>' line; two different lines for the target are no
        # verdict, and no verdict comes back not met with the reason.
        read = next(((met, why) for rid, met, why in _audit_lines(reply or "") if rid == target), None)
        if read is None:
            return "held: the reassessment gave no verdict"
        met, why = read
        if not met:
            return f"confirmed on reassessment: {why[:200]}"
        if not self._resolve_citations(why):
            # The same typed citation the audit accepts: the harness's own
            # exact receipt of the command the requirement names, on the
            # current source (never a model's word, never another command).
            receipt, problem = self._command_receipt_citation(target)
            if receipt is None:
                return "held: MET claimed without a project file or test that exists" + (f"; {problem}" if problem else "")
            why = f"{why} (harness receipt {receipt['id']}: `{receipt['command']}` passed on the current source)"[:400]
            record["citation_receipt"] = receipt
        verdict = {target: (True, why)}
        # The same acceptance the audit's own MET gets: refreshed
        # obligations, the NOT RUN and error-clause rules, the auditor's
        # explicit sufficiency lines, then the one predicate.
        self._refresh_obligations()
        original = dict(verdict)
        lowered = self._lower_unexecuted(verdict)
        lowered += self._lower_unexercised_errors(verdict, [target], original=original, lowered=lowered)
        self._judge_obligations(reviewer, reply or "", {}, ids=[target])
        lowered += self._apply_acceptance(verdict, [target])
        if lowered:
            return f"held: {lowered[0]['reason'][:200]}"
        record["acceptance_binding"] = self._audit_binding(self._final_audit.get("ids") if self._final_audit else None)
        self.memory.ledger.requirement_status[target] = f"met (reassessed on evidence {record['id']})"
        if self._done_refusal:
            self._done_refusal = "\n".join(line for line in self._done_refusal.split("\n")
                                           if not line.startswith(f"- {target}:"))
        return f"MET on reassessment by the same auditor: {why[:200]}"

    def _evidence_block(self) -> str:
        """For the audit: executed evidence on record for exactly this
        source, with each finding and its one reassessment. Evidence, never a
        verdict."""
        identity = self._evidence_identity() if self.project else None
        rows = [r for r in self.evidence_requests if r.get("kind") == "audit" and identity
                and r.get("identity") == identity and (r.get("receipt") or {}).get("status") in ("passed", "failed")]
        if not rows:
            return ""
        lines = [f"- {r['id']} on {r['target']}: earlier finding: {(r.get('finding') or '')[:300]}. The harness "
                 f"ran the new test file {r['file']} in a disposable copy of this source: {r['receipt']['status']} "
                 f"({'; '.join(r['receipt'].get('cases') or ())[:300] or 'no cases shown'}). Reassessment: "
                 f"{(r.get('reassessment') or 'none')[:300]}" for r in rows]
        return ("\n\n## Executed evidence on this exact source (harness records)\n" + "\n".join(lines)
                + "\nA passing test shows only what it executes; judge each requirement yourself.")

    def _proof_rows(self, spec) -> str:
        """The task's declared proof scenarios with their binding and
        execution state, for the seats that judge the work: the mapping
        the planner declared (or that none was, for a legacy task), what
        the lead bound, and what the harness has shown to run. Sufficiency
        is the reader's judgment; a bound, executed case proves only that
        it ran."""
        task = getattr(spec, "task_id", None)
        rows = [r for r in self.evidence_obligations if r.get("task") == task and r.get("kind") == "acceptance"]
        if task is None or not rows:
            return ""
        self._refresh_obligations()
        self._mark_deferred_rows(task)
        rows = [r for r in self.evidence_obligations if r.get("task") == task and r.get("kind") == "acceptance"]
        lines = []
        for r in rows:
            cases = ", ".join(r.get("cases") or ()) or "no case bound"
            receipts = [str(x) for x in (r.get("receipts") or ())]
            execution = (f"{r['execution']}, deferred to the merge gate (declared by the harness: a parallel child "
                         "measures no execution; not an EVIDENCE finding)"
                         if r.get("deferred") and r["execution"] != "ran" else r["execution"])
            lines.append(f"- {r.get('proof_id') or r['oid']} [{', '.join(x[0] for x in r['requirements'])}]: "
                         f"{r['behavior'][:220]}; binding {r.get('binding', 'unknown')}: {cases}; execution "
                         f"{execution}" + (f" (receipt {receipts[-1]})" if receipts and r["execution"] == "ran" else "")
                         + (f" ({r['reason'][:80]})" if r.get("reason") else ""))
            for entry in (r.get("scenario_lineage") or [])[-1:]:
                changed = entry.get("fields") or {entry.get("field"): dict(before=entry.get("before"))}
                lines.append(f"  corrected by the planner on finding {entry.get('finding')}: "
                             + "; ".join(f"{f} was '{str((v or {}).get('before'))[:160]}'" for f, v in changed.items())
                             + "; the scenario above is the one in force. Judge it whole: a RESOLVED on its finding "
                               "means operation, state and expect are consistent with each other and with the "
                               "requirement, not only the corrected field. A field that still contradicts another "
                               "is '<id>: UNRESOLVED SCENARIO P<n> - <contradiction> | proposal: <field>: <text>', "
                               "naming each field that must change.")
        legacy = all(r.get("binding", "").startswith("unknown") for r in rows)
        facts = self._observation_facts(task)
        return ("\n\n## Proof scenarios this task declared, as the harness holds them\n" + "\n".join(lines)
                + (f"\n{facts}" if facts else "")
                + ("\nThis task declared no scenario mapping (legacy acceptance): treat the mapping as unknown, "
                   "not complete; no check discharges these rows and only the final audit's explicit per-id "
                   "line judges them." if legacy else "")
                + "\nJudge three things and report each gap as a BLOCKING finding with its basis: whether the "
                "scenarios cover every condition of the covered requirements (operations, negative cases, "
                "preconditions) and name any missing scenario; whether each expected state is right for its "
                "stated prior state (a state that already holds stays as it is: 'unchanged' is not 'empty'); and "
                "whether the bound cases, as written, exercise their scenarios. A case that ran proves only that "
                "it ran. An unbound scenario, a case not shown executed on this source, or a scenario whose "
                "expectation contradicts its prior state is not evidence for its requirement. "
                + TEST_SUBSTANCE_RULE + " A case that asserts nothing, cannot fail, or forbids what the "
                "requirement permits is a BLOCKING finding on that test. "
                "The binding rule the lead is held to: " + PROVES_RULE + " A row whose binding reads 'invalid "
                "declaration' is a measured fault of the PROVES line, with its reason shown: report it as a "
                "BLOCKING finding that asks for the corrected PROVES line(s), one exact case per line, and "
                "nothing more; it calls for no code or test rewrite by itself. Ask for code or test changes "
                "only where the code or the cases themselves are wrong. " + _TYPED_FINDING_FORMS)

    def _mark_deferred_rows(self, task) -> None:
        """In a parallel child with no gate, every executable acceptance row
        the child cannot run is marked deferred to the merge gate: a fact
        the harness holds and declares, so the reviewer need not discover it
        row by row (controller-continuation-01 t4: seven EVIDENCE findings
        and seven hold records for one known fact). The mark is the child's;
        the parent clears it when it re-bases the row to its own gate."""
        if not (self._batch_child and self.config.integration_gate is None):
            return
        for r in self.evidence_obligations:
            if (r.get("task") == task and r.get("kind") == "acceptance" and r.get("executable_claim")
                    and r.get("execution") != "ran" and not r.get("deferred")):
                r["deferred"] = "merge gate"

    def _declare_deferred_evidence(self, spec, peer: str) -> None:
        """The deferral, on the record, for this reviewer: which rows of
        this task the harness will run after the merge and ask this
        reviewer about. One record per (task, reviewer)."""
        rows = [r for r in self.evidence_obligations
                if r.get("task") == spec.task_id and r.get("kind") == "acceptance" and r.get("deferred")]
        if not rows or any(d["task"] == spec.task_id and d["peer"] == peer for d in self.deferred_evidence):
            return
        self.deferred_evidence.append(dict(
            task=spec.task_id, peer=peer, proof_ids=[r.get("proof_id") or r["oid"] for r in rows],
            declared="deferred to the merge gate: a parallel child measures no execution; the parent's merge "
                     "gate runs the rows and the same reviewer is asked about them then",
            rechecked=None, reading=None))
        self._note(f"task {spec.task_id}: {len(rows)} acceptance row(s) deferred to the merge gate for {peer}")

    def _deferred_rows_for(self, task: str, peer: str) -> List[dict]:
        """The acceptance rows a deferral record names, as the record holds
        them now (the parent's re-based rows after a merge)."""
        pids = [pid for d in self.deferred_evidence if d["task"] == task and d["peer"] == peer
                for pid in d.get("proof_ids") or ()]
        return [r for r in self.evidence_obligations
                if r.get("task") == task and r.get("kind") == "acceptance"
                and (r.get("proof_id") or r["oid"]) in pids and not r.get("superseded_by")]

    def _probe_applies(self, key: str) -> bool:
        return bool(self._probe_checks(key))

    def _probe_checks(self, key: str) -> list:
        """The approved gate commands that would run a new scratch test file
        in ``key``'s copy; empty where no demonstration can be asked for: a
        seat whose copy takes no scratch, or no approved command that would
        run a new test file (root review of b24b6db: an empty suite or an
        explicit file list never runs one). A parallel child has no gate of
        its own, so it reads the commands its parent approved (root on
        40b2133: r09's parallel reviewer never received the rule because
        the child's gate is None)."""
        if not (self.project and _seat_takes_scratch(key)):
            return []
        return self._discovering_checks()

    def _record_deferred(self, spec, peer: str, note: str) -> None:
        """DEFERRED lines in a parallel child's review: each one an open,
        unverified finding the parent rechecks after the merge."""
        siblings = {tid for tid, _, _ in (getattr(self, "_batch_siblings", None) or [])}
        if not siblings:
            return
        # The same bounded separators as a typed claim (``_TYPED_SEP``; root's
        # probe, 2026-10-26: an em dash after the sibling id dropped the
        # deferred condition in silence). The sibling must be one of this
        # batch and the condition must say something; otherwise the line is
        # not a deferral and is read as the ordinary review text it is in.
        for match in re.finditer(r"^\s*(?:[-*+]\s+)?DEFERRED\s*:\s*(\S+)\s*" + _TYPED_SEP + r"\s*(.*)$",
                                 note or "", re.IGNORECASE | re.MULTILINE):
            sibling, condition = match.group(1).strip(" .,:"), match.group(2).strip()
            if sibling not in siblings or not condition:
                continue
            line = f"BLOCKING: {condition} (deferred to {sibling})"
            registered = self._register_review_findings(spec, peer, line)
            record = registered[0] if registered else next(
                (r for r in self.review_findings if r["task"] == spec.task_id and r["author"] == peer
                 and r.get("text") == line), None)
            fid = record["fid"] if record is not None else None
            if record is not None and not record.get("closed"):
                # One identity, never closed in the child (completion
                # workflow, 2026-10-30; evidence-authority-02 t6: the child's
                # own recheck resolved RFT6-1 on its pre-merge copy, the text
                # built here carried no id, so it outlived the typed record
                # and defeated the batch close's hold). A deferral's own words
                # condition it on the merged source: it is held out of the
                # child's rechecks and judged once, there, by its reviewer.
                record["held"] = (f"deferred to parallel task {sibling}: rechecked by {peer} on the merged "
                                  "source, never in the child")
            text = ((f"{fid}: " if fid else "")
                    + f"Task {spec.task_id}: deferred by {peer} to parallel task {sibling}, unverified until "
                      f"rechecked on the merged source: {condition}")
            self._open_finding("unverified", text)
            self.unresolved_reviews.append(dict(task=spec.task_id, peer=peer, finding=match.group(0).strip(),
                                                text=text, deferred=sibling, finding_id=fid))

    def _record_scratch_evidence(self, spec, peer: str) -> None:
        """What a review asked for a scratch demonstration actually left in
        its copy, measured by the runtime, never read from the reply (r09 on
        ded0095: the codex reviewer read source and reran the existing suites;
        its copy held no new test file). Recorded as a fact on the task; it
        decides nothing by itself."""
        found = getattr(self, "_last_scratch_tests", None)
        proof = getattr(self, "_last_scratch_proof", None)
        hashes = getattr(self, "_last_scratch_hashes", None)
        pending = getattr(self, "_pending_scratch", None) or {}
        self._last_scratch_tests = self._last_scratch_proof = self._last_scratch_hashes = None
        self._pending_scratch = None
        if found is None or self._outcome is None:
            return
        if not found:
            detail = "not run: no new test file in its copy"
        elif not proof:
            detail = "not run: " + ", ".join(found) + " added; the harness did not run it"
        else:
            detail = (f"{proof.get('status')}: {proof.get('reason')}; "
                      + "; ".join(proof.get("cases") or proof.get("files") or ()))
        # Provenance for the final audit (root review of 17a96d3): the source
        # and checks the copy was handed (the evidence identity), and the
        # requirements this review covered. Never shown to a model.
        handed = (proof or {}).get("handed_source"), (proof or {}).get("handed_checks")
        drift = self._handed_drift(proof)
        self.scratch_proofs.append(dict(id=pending.get("id") or f"S{len(self.scratch_proofs) + 1}",
                                        task=spec.task_id, peer=peer, files=list(found),
                                        identity=self._scratch_identity(*handed) if self.project and all(handed)
                                        else None, eligible=bool(proof) and all(handed) and not drift,
                                        **({"stale": drift} if drift else {}),
                                        requirements=[rid for rid, _ in self._covered_texts()],
                                        # Bound before the call, measured in the copy: the
                                        # obligations this proof may serve, the producer gate,
                                        # the handed source and the scratch files' hashes.
                                        bindings=list(pending.get("bindings") or []),
                                        producer_gate=pending.get("producer_gate"),
                                        handed_source=handed[0], handed_checks=handed[1],
                                        file_hashes=dict(hashes or {}),
                                        **({k: proof[k] for k in ("status", "reason", "cases", "receipts")}
                                           if proof else dict(status="not run"))))
        fact = self._outcome.note("unverified", f"scratch demonstration by {peer}, as the harness measured "
                                  "and ran it: " + detail, terminal=False)
        # Executed and passed is evidence the operator can read, never a MET.
        fact.recovered = bool(proof and proof.get("status") == "passed")

    def _binding_feedback(self, spec) -> str:
        """The harness's own measurement of this task's PROVES declaration,
        for the call that corrects it: each scenario whose binding is an
        invalid declaration with the measured reason, the binding rule and
        the test rule, and what a declaration fault alone calls for (the
        corrected lines, not rewritten code or tests). Empty when every
        scenario is bound or unbound: nothing is said twice."""
        task = getattr(spec, "task_id", None)
        if task is None:
            return ""
        unrunnable = self._owned_unexecutable(task)
        runs = ("\n\n## NOT RUN items no check can run as written\n" + "\n".join(
            f"- {e['oid']}: NOT RUN: {_display_item(e['item'])[:300]}" for e in unrunnable)
            + "\nWhere cases that exercise the whole reported behaviour exist in the project's tests, restate "
            "each item with one line 'RUNS: O<n> - file::[Class::]test_case' per case. The harness settles them "
            "only from a complete passing run on the current source, and the independent audit judges whether "
            "they exercise everything the item reported. Without such lines the item stays held as written."
            if unrunnable else "")
        rows = [r for r in self.evidence_obligations if r.get("task") == task and r.get("kind") == "acceptance"]
        if not rows:
            return runs
        faults = [f"- {r.get('proof_id') or r['oid']}: {r['binding'][len('invalid declaration: '):][:200]}"
                  for r in rows if str(r.get("binding", "")).startswith("invalid declaration")]
        text = "\n\n## Proof binding\n" + PROVES_RULE + " " + TEST_SUBSTANCE_RULE
        if faults:
            text += ("\nThe harness measured these PROVES lines of your delivery invalid; each scenario is unbound "
                     "until redeclared:\n" + "\n".join(faults)
                     + "\nA declaration fault alone is corrected by the corrected PROVES line(s) in this reply, one "
                     "exact case per line; it calls for no code or test rewrite. Findings about the code or the "
                     "cases themselves still need the fix they ask for.")
        return text + runs

    def _scenario_rows(self, spec, pid: str) -> List[dict]:
        return [r for r in self.evidence_obligations if r.get("task") == spec.task_id and r.get("proof_id") == pid
                and r.get("kind") == "acceptance" and not r.get("superseded_by")]

    def _scenario_unbound(self, spec, pid: str) -> bool:
        """Whether scenario ``pid`` of this task binds no case: no PROVES
        line declared one, or the declaration was measured invalid. A bound
        case whose execution is missing is not this: the harness route
        answers that."""
        rows = [r for r in self._scenario_rows(spec, pid) if (r.get("modality") or "test") == "test"]
        return bool(rows) and any(r.get("binding") != "bound" or not r.get("cases") for r in rows)

    def _binding_requested(self, spec, pid: str) -> str:
        """The earlier binding request for ``pid`` that saw exactly the
        scenario's present state, or "": the same source (known) and the
        same binding and cases after that request as now. Asking again with
        nothing changed is the same request; a binding that changed since
        (a later declaration, a correction) is a new state (root review of
        9b0280b). An unknown source proves no sameness."""
        source = self._source_fingerprint() if self.project else None
        if not source:
            return ""
        rows = self._scenario_rows(spec, pid)
        now = ([r.get("binding") for r in rows], sorted(c for r in rows for c in (r.get("cases") or [])))
        for route in self.evidence_routes:
            if (route.get("task") == spec.task_id and route.get("proof_id") == pid
                    and str(route.get("action") or "").startswith("binding requested")
                    and route.get("source") == source
                    and (list(route.get("binding_after") or []), sorted(route.get("cases") or [])) == now):
                return f"finding {route.get('finding')}"
        return ""

    def _request_bindings(self, spec, task, records: List[dict], lead: Optional[str]) -> None:
        """One declaration-only call to the task's lead, the author of its
        bindings, for the PROVES lines of the scenarios whose EVIDENCE
        findings say they bind no case (cycle01, PR73 6702597850). Run in a
        source copy with no write grant and the declaration round cap; only
        PROVES lines naming a requested scenario are read. Any other PROVES
        line (malformed, or naming another scenario) is dropped and recorded
        in ``proof_binding_problems``, so it can neither rebind nor reset a
        scenario nobody asked about; prose is ignored and not recorded. The new
        binding is then settled against checks already recorded on this
        exact source (``_refresh_obligations``: the exact case, executed and
        passed, unskipped, current gate and environment); no suite is rerun
        to attach a declaration. The finding is the reviewer's to recheck;
        nothing here closes it or judges sufficiency."""
        pids = sorted({r["typed"]["proof_id"] for r in records})
        source = self._source_fingerprint() if self.project else None
        before = {r["oid"]: (r.get("binding"), r.get("execution")) for r in self.evidence_obligations}
        outcome, reply = "", ""
        declaration_artifact, kept = None, []
        if not lead:
            outcome = "not requested: no lead seat is known for this task"
        else:
            lines = []
            for pid in pids:
                for row in self._scenario_rows(spec, pid):
                    told = dict(row.get("scenario") or ())
                    lines.append(f"- {pid}: {told.get('operation') or row.get('behavior') or ''} | state: "
                                 f"{told.get('state') or ''} | expect: {told.get('expect') or ''} (binding now: "
                                 f"{row.get('binding') or 'none declared'})")
            claims = "".join(f"- {r['fid']} ({r['author']}): {str(r.get('claim') or '')[:400]}\n" for r in records)
            unrunnable = self._owned_unexecutable(spec.task_id)
            runs_ask = ("\nThese NOT RUN items of your task name nothing a check can run:\n" + "".join(
                f"- {e['oid']}: {_display_item(e['item'])[:300]}\n" for e in unrunnable)
                + "Where cases that exercise the whole reported behaviour exist, add one line "
                "'RUNS: O<n> - file::[Class::]test_case' per case for each.\n" if unrunnable else "")
            prompt = (
                f"Task: {spec.description}\n\nThese proof scenarios of your task bind no test case, so no run "
                "can show them executed:\n" + "\n".join(lines) + "\n\nThe review findings that named them:\n"
                + claims + runs_ask + "\n" + PROVES_RULE + " " + TEST_SUBSTANCE_RULE + "\nReply with one line "
                "'PROVES: P<n> - file::[Class::]test_case' per case that proves each scenario above, naming cases "
                "that exist in the project's test files as they stand now, and nothing else. Only lines for the "
                f"scenarios above ({', '.join(pids)}) are read. Do not change files: this call declares where "
                "the proof lives; it does not edit. The harness settles each case only against a check it has "
                "already run on this exact source in which that case executed and passed; the reviewer who "
                "raised the finding judges whether it proves the scenario.")
            try:
                with invocation(spec.task_id, "proof-binding"):
                    reply = self._invoke_model(lead, prompt)
            except TurnLimitReached as exc:
                outcome = self._capped_declaration(spec, lead, task, "proof-binding", exc)
            except FinishReserved as exc:
                self._finish_refused(spec, "proof binding declaration", exc)
                outcome = "not requested: refused to keep the finish"
            else:
                task.record("assistant", f"[{lead}] {reply}")
                ref = task.keep(reply, kind="proof-binding", author=lead)
                declaration_artifact = getattr(ref, "id", None)
                kept, dropped = [], []
                for match in _PROVES_ANY.finditer(reply or ""):
                    named = _PROVES_PID.match(match.group(1))
                    if named and named.group(1) in pids:
                        kept.append(match.group(0).strip())
                    else:
                        dropped.append(match.group(0).strip()[:120])
                if dropped:
                    self.proof_binding_problems.extend(
                        dict(task=spec.task_id, role="proof-binding", author=lead,
                             problem=f"not a requested scenario's line, not read: {line}") for line in dropped)
                if kept:
                    self._record_proves(lead, "proof-binding", "\n".join(kept))
                self._record_runs(lead, "proof-binding", reply or "")
                outcome = ("declared" if kept else "no PROVES line for a requested scenario")
        self._refresh_obligations()
        after = {r["oid"]: r for r in self.evidence_obligations}
        for record in records:
            pid = record["typed"]["proof_id"]
            rows = self._scenario_rows(spec, pid)
            self.evidence_routes.append(dict(
                task=spec.task_id, finding=record["fid"], proof_id=pid, reviewer=record["author"],
                action=f"binding requested from {lead or 'no lead'}: {outcome}", author=lead,
                binding_before=[before.get(r["oid"], (None, None))[0] for r in rows],
                binding_after=[after[r["oid"]].get("binding") for r in rows if r["oid"] in after],
                cases=[c for r in rows for c in (after.get(r["oid"], {}).get("cases") or [])],
                execution_before=[before.get(r["oid"], (None, None))[1] for r in rows],
                execution_after=[after[r["oid"]].get("execution") for r in rows if r["oid"] in after],
                receipts_after=[list(after[r["oid"]].get("receipts") or []) for r in rows if r["oid"] in after],
                declared=[line for line in kept if (m := _PROVES_PID.match(line.split(":", 1)[1])) and m.group(1) == pid],
                declaration_artifact=declaration_artifact, source=source))
            self._note(f"task {spec.task_id}: {record['fid']} ({pid} binds no case) went to {lead}'s binding "
                       f"declaration: {outcome}; {record['author']} rechecks it")

    def _fold_note(self, spec, records: List[dict]) -> str:
        """What the editor revision is asked to declare when the bundle's
        binding stage is folded into it (cycle22): each scenario a finding
        says binds no case or binds invalidly, with its text and binding as
        measured, and the binding rule. Read from this reply by the editing
        readers (``_record_proves``, ``_record_runs``) with the lead's own
        authority; the reviewer who raised each finding still rechecks it."""
        pids = sorted({r["typed"]["proof_id"] for r in records})
        lines = []
        for pid in pids:
            for row in self._scenario_rows(spec, pid):
                told = dict(row.get("scenario") or ())
                lines.append(f"- {pid}: {told.get('operation') or row.get('behavior') or ''} | state: "
                             f"{told.get('state') or ''} | expect: {told.get('expect') or ''} (binding now: "
                             f"{row.get('binding') or 'none declared'})")
        claims = "".join(f"- {r['fid']} ({r['author']}): {str(r.get('claim') or '')[:400]}\n" for r in records)
        return ("\n\n## Scenarios of your task that bind no case, or bind invalidly\n" + "\n".join(lines)
                + "\nThe review findings that named them:\n" + claims + PROVES_RULE
                + " In this same reply, after your edits, add one line 'PROVES: P<n> - file::[Class::]test_case' per "
                "case that proves each scenario above, naming cases that exist in the project's tests as they stand "
                "after your edits. The harness settles each case only against a check it runs on the source this "
                "reply leaves; the reviewer who raised the finding judges whether it proves the scenario. No "
                "separate declaration call is made for these scenarios.")

    def _settle_folded(self, spec, lead, reply: str) -> None:
        """Record the folded binding declaration once the editor revision
        returned: the same route record a separate declaration call leaves
        (``_request_bindings``), so a later round on the same state is the
        same request, with the bindings as the revision left them. Their
        execution is settled on the post-edit source by the checks that run
        after it, never by a run before the edit."""
        pending = getattr(self, "_fold_pending", None)
        if not pending or pending.get("task") != spec.task_id:
            return
        self._fold_pending = None
        self._refresh_obligations()
        before = pending["before"]
        after = {r["oid"]: r for r in self.evidence_obligations}
        source = self._source_fingerprint() if self.project else None
        declared = [m.group(0).strip() for m in _PROVES_ANY.finditer(reply or "")]
        for record in pending["records"]:
            pid = record["typed"]["proof_id"]
            rows = self._scenario_rows(spec, pid)
            mine = [line for line in declared if (m := _PROVES_PID.match(line.split(":", 1)[1])) and m.group(1) == pid]
            self.evidence_routes.append(dict(
                task=spec.task_id, finding=record["fid"], proof_id=pid, reviewer=record["author"],
                action=(f"binding requested from {lead or 'no lead'} in the fix revision: "
                        + ("declared" if mine else "no PROVES line for a requested scenario")),
                author=lead, folded=True,
                binding_before=[before.get(r["oid"], (None, None))[0] for r in rows],
                binding_after=[after[r["oid"]].get("binding") for r in rows if r["oid"] in after],
                cases=[c for r in rows for c in (after.get(r["oid"], {}).get("cases") or [])],
                execution_before=[before.get(r["oid"], (None, None))[1] for r in rows],
                execution_after=[after[r["oid"]].get("execution") for r in rows if r["oid"] in after],
                receipts_after=[list(after[r["oid"]].get("receipts") or []) for r in rows if r["oid"] in after],
                declared=mine, declaration_artifact=None, source=source))
            self._note(f"task {spec.task_id}: {record['fid']} ({pid} binds no case) was declared in {lead}'s fix "
                       f"revision; {record['author']} rechecks it")

    def _scenario_allowance(self, spec, pid: str) -> bool:
        """Whether the planner may still be asked to correct scenario ``pid``
        of this task: once per scenario (close family, 2026-10-26; before,
        once per task, so a second reviewer's finding on another scenario
        went to the editor). A bare task id in the record is an older
        checkpoint's whole-task spend and still counts for every scenario."""
        return spec.task_id not in self._scenario_corrected and f"{spec.task_id}:{pid}" not in self._scenario_corrected

    def _routed_ids(self, spec) -> set:
        """Ids of this task's open findings the editor never answers: routed
        to the planner or the harness, held for a recheck, or held as
        typed claims the harness could not route."""
        return {r["fid"] for r in self._finding_records(task=spec.task_id)
                if r.get("route") and r.get("route") != "editor"}

    def _editor_lines(self, spec, unresolved: List[tuple]) -> List[tuple]:
        """The open findings an editor round answers: every one except a
        finding held for the post-merge recheck or routed or held by id."""
        routed = self._routed_ids(spec)
        return [u for u in self._actionable(unresolved) if _finding_id(u[1]) not in routed]

    def _restated(self, spec, unresolved: List[tuple]) -> List[dict]:
        """Open findings whose reviewer restated them at a recheck as a
        complete typed claim and that are not yet routed: read by the next
        ``_plan_findings`` before any editor round."""
        out = []
        for _, verdict in unresolved:
            record = next((r for r in self._finding_records(task=spec.task_id) if r["fid"] == _finding_id(verdict)), None)
            if record is not None and record.get("restated") and not record.get("route"):
                out.append(record)
        return out

    def _serve_plan_findings(self, spec, task, plan: dict, labels: dict, *, lead: Optional[str] = None,
                             fold: bool = False):
        """Answer the routed findings before any editor call: the harness
        runs the task's checks on the current source if it has not (the
        same observation a review gets; a run already recorded on these
        bytes is not repeated) and re-settles the rows, and the planner is
        asked once for the scenario corrections. Returns the spec, with its
        scenarios as corrected. Nothing here closes a finding: the reviewer
        who raised it re-checks it against what is now shown."""
        if plan.get("binding") and fold:
            # The editor revision this bundle already buys declares the
            # bindings itself (cycle22): no separate declaration call, and
            # the bindings settle on the source the revision leaves.
            self._fold_pending = dict(task=spec.task_id, records=list(plan["binding"]), lead=lead,
                                      before={r["oid"]: (r.get("binding"), r.get("execution"))
                                              for r in self.evidence_obligations})
        elif plan.get("binding"):
            self._request_bindings(spec, task, plan["binding"], lead)
        if plan["evidence"]:
            before = {r["oid"]: r.get("execution") for r in self.evidence_obligations}
            runs = len(self.review_checks)
            self._observe_checks(spec, "evidence")
            self._refresh_obligations()
            after = {r["oid"]: r for r in self.evidence_obligations}
            for record in plan["evidence"]:
                pid = record["typed"]["proof_id"]
                rows = [r for r in self.evidence_obligations if r.get("task") == spec.task_id
                        and r.get("proof_id") == pid and r.get("kind") == "acceptance"]
                self.evidence_routes.append(dict(
                    task=spec.task_id, finding=record["fid"], proof_id=pid, reviewer=record["author"],
                    action=("harness ran the task's checks on the current source" if len(self.review_checks) > runs
                            else "no new run: a harness run on this exact source is already recorded"),
                    execution_before=[before.get(r["oid"]) for r in rows],
                    execution_after=[after[r["oid"]].get("execution") for r in rows],
                    source=self._source_fingerprint()))
                self._note(f"task {spec.task_id}: {record['fid']} (EVIDENCE {pid}) routed to the harness and "
                           f"{record['author']}'s recheck; no editor call for it")
        for record in plan["held"]:
            self.evidence_routes.append(dict(task=spec.task_id, finding=record["fid"], proof_id=record["typed"]["proof_id"],
                                             reviewer=record["author"], action=record["held"], execution_before=None,
                                             execution_after=None, source=self._source_fingerprint()))
            self._note(f"task {spec.task_id}: {record['fid']} (EVIDENCE) {record['held']}")
        for record in plan.get("unroutable") or []:
            # Recorded where the routes are: a typed claim the harness could
            # not route is held for its reviewer, not answered by anyone.
            self.evidence_routes.append(dict(task=spec.task_id, finding=record["fid"],
                                             proof_id=(record.get("typed") or {}).get("proof_id"),
                                             reviewer=record["author"],
                                             action=f"held unroutable: {record['route_note']}; the reviewer is asked to "
                                                    "restate it at the recheck",
                                             execution_before=None, execution_after=None,
                                             source=self._source_fingerprint()))
            self._note(f"task {spec.task_id}: {record['fid']} held unroutable: {record['route_note']}")
        if plan["scenario"]:
            spec = self._correct_scenarios(spec, task, plan["scenario"], labels)
        return spec

    def _prior_scenario_problem(self, spec, tid: str, pid: str) -> str:
        """Why an earlier task's scenario ``tid:pid`` cannot take the
        planner's correction, or "" (cycle22). It must be a scenario of an
        earlier closed task of this run, still standing on the record, not
        corrected once already, whose owning lead is on record and
        reachable, and whose dispatched scope the record keeps. Anything
        else stays held for its reviewer; nothing is cleared."""
        closed = {getattr(h, "task_id", None) for h in self.history}
        if tid == spec.task_id or tid not in closed:
            return f"names task {tid}, which is not an earlier closed task of this run"
        rows = [r for r in self.evidence_obligations if r.get("task") == tid and r.get("proof_id") == pid
                and r.get("kind") == "acceptance" and not r.get("superseded_by")]
        if not rows:
            return f"{tid} has no standing scenario {pid} on the record"
        if tid in self._scenario_corrected or f"{tid}:{pid}" in self._scenario_corrected:
            return f"scenario {tid}:{pid} was already corrected once; a second correction is not routed"
        lead = self._owner_lead(tid)
        if not lead or not self._available(lead):
            return (f"{tid}'s owning lead {lead or '(none on record)'} is not reachable, so its scenario {pid} "
                    "stays held as declared")
        if self._prior_scope(tid) is None:
            return f"{tid}'s dispatched scope is not on record, so its scenario {pid} cannot be revised"
        return ""

    def _prior_scope(self, tid: str):
        """An earlier task's scope as dispatched (its frozen contract), with
        each scenario's text as it stands now on its rows (earlier
        corrections included), or None when the record does not hold it."""
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == tid and o.contract), None)
        data = (outcome.contract or {}).get("scope") if outcome is not None else None
        if not isinstance(data, dict) or not data.get("proofs"):
            return None
        proofs = []
        for proof in data["proofs"]:
            row = next((r for r in reversed(self.evidence_obligations) if r.get("task") == tid
                        and r.get("proof_id") == proof.get("id") and r.get("kind") == "acceptance"
                        and not r.get("superseded_by")), None)
            now = dict((str(k), str(v)) for k, v in (row.get("scenario") or [])) if row is not None else {}
            proofs.append(dict(proof, **{k: now[k] for k in ("operation", "state", "expect") if k in now}))
        try:
            return TaskScope(permitted_paths=tuple(data.get("permitted_paths") or ()),
                             forbidden_paths=tuple(data.get("forbidden_paths") or ()),
                             intended_result=str(data.get("intended_result") or ""),
                             acceptance=tuple(data.get("acceptance") or ()), max_lines=data.get("max_lines"),
                             review_only=data.get("edits") == "none", capture=data.get("capture"),
                             exact=bool(data.get("exact")), estimate=data.get("estimate"), proofs=tuple(proofs))
        except (TypeError, ValueError):
            return None

    def _prior_packet_problem(self, tid: str, pid: str, changes: dict) -> str:
        """The size bound an earlier task's corrected scenario meets: that
        task's role packets, built from its scope with the correction, within
        its own allowance (its granted extension if it ran under one, else
        the policy default). Nothing is applied here."""
        policy = self.config.repository_policy
        scope = self._prior_scope(tid)
        if policy is None or scope is None:
            return ""
        trial = replace(scope, proofs=tuple(dict(p, **changes) if p["id"] == pid else p for p in scope.proofs))
        granted = [int(r.get("granted") or 0) for r in self.packet_requests
                   if r.get("task") == tid and r.get("state") == "granted"]
        allowance = max(granted) if granted else policy.packet_limit
        needed = max(policy.packet_needed(policy.scope(trial), role) for role in ("lead", "reviewer"))
        if needed > allowance:
            return (f"the corrected text puts {tid}'s role packet over its allowance ({needed:,} bytes against "
                    f"{allowance:,})")
        return ""

    def _apply_prior_scenario_correction(self, tid: str, pid: str, changes: dict, *, findings: List[str], by: str,
                                         raised_in: str) -> None:
        """Put an earlier task's corrected scenario text in force on its own
        rows (cycle22), exactly as a task's own correction does: the prior
        text, execution and judgment kept as lineage, execution and
        sufficiency start again and are settled from the receipts on record
        and judged afresh; the requirements, location, evidence kind,
        binding and scenario id stay the earlier task's. Its frozen contract
        and outcome are not touched, and no other scenario or task moves. A
        granted packet extension of that task records the revision."""
        new = None
        for row in self.evidence_obligations:
            if (row.get("task") != tid or row.get("proof_id") != pid or row.get("kind") != "acceptance"
                    or row.get("superseded_by")):
                continue
            scenario = dict((str(k), str(v)) for k, v in (row.get("scenario") or []))
            lineage = list(row.get("scenario_lineage") or [])
            first = next(iter(changes))
            lineage.append(dict(field=", ".join(changes), before=scenario.get(first), after=changes[first],
                                fields={f: dict(before=scenario.get(f), after=t) for f, t in changes.items()},
                                findings=list(findings), finding=findings[0] if findings else None, by=by,
                                raised_in=raised_in, owner_lead=self._owner_lead(tid),
                                behavior=row.get("behavior"), scenario=[list(x) for x in row.get("scenario") or []],
                                execution=row.get("execution"), sufficiency=row.get("sufficiency"),
                                judged_by=row.get("judged_by", ""), receipts=list(row.get("receipts") or [])))
            scenario.update(changes)
            new = scenario
            item = (f"{pid}: {scenario.get('operation')}; given {scenario.get('state')}; expect "
                    f"{scenario.get('expect')}; in {scenario.get('location')}")
            row.update(scenario=sorted(scenario.items()), behavior=item[:300],
                       entry=dict(row.get("entry") or {}, item=item), scenario_lineage=lineage,
                       execution="missing", sufficiency="unjudged", judged_by="", judged_binding="", receipts=[],
                       source_digest=None, discharged_cases=None,
                       reason=f"scenario corrected by the planner on {raised_in}'s review; executed and judged afresh")
            self._sync_obligation(row)
        scope = self._prior_scope(tid)
        for request in self.packet_requests:
            if request.get("task") == tid and request.get("state") == "granted" and scope is not None:
                request.setdefault("revisions", []).append(dict(
                    fingerprint=self._packet_fingerprint(TaskSpec(task_id=tid, description="", kind="backend",
                                                                  complexity="simple", scope=scope)),
                    by="prior-task scenario correction", proof=pid, findings=list(findings), corrected_by=by,
                    raised_in=raised_in))
        self._refresh_obligations()
        self._note(f"task {raised_in}: earlier task {tid}'s scenario {pid} corrected by {by}"
                   + (f" ({', '.join(f'{k}: {v}' for k, v in changes.items())[:160]})" if new else ""))

    def _record_scenario_correction(self, spec, pid: str, records: List[dict], *, status: str, reply: str, **detail) -> None:
        entry = dict(task=spec.task_id, proof_id=pid, findings=[r["fid"] for r in records],
                     reviewers=sorted({r["author"] for r in records}), status=status,
                     reply_artifact=(self.store.put(reply, kind="scenario-correction-reply", author=detail.get("by") or "harness").id
                                     if reply else None), source=self._source_fingerprint(), **detail)
        self.scenario_corrections.append(entry)
        for record in records:
            record["route"] = f"scenario: {status}"
        self._note(f"task {spec.task_id}: scenario {pid}: {status}")

    def _correction_packet_problem(self, spec, pid: str, changes: dict) -> str:
        """The size bound a corrected scenario field meets is the one its
        declaration met: the task's role packets (lead and reviewer) built
        from the corrected scope must still be within their limit. Nothing
        is applied; the trial scope is discarded. Returns why not, or ""."""
        scope = spec.scope
        if scope is None or not getattr(scope, "proofs", None):
            return ""
        trial = replace(spec, scope=replace(scope, proofs=tuple(
            dict(p, **changes) if p["id"] == pid else p for p in scope.proofs)))
        try:
            # Measured against the task's own allowance: the default, or the
            # orchestrator's grant this task runs under.
            allowance = self._packet_allowance(spec)
            for role in self._packet_roles(spec):
                self._role_packet(trial, role, limit=allowance)
        except Exception as exc:  # noqa: BLE001 -- the packet builder's own refusal, named
            return (f"the corrected text puts this task's role packet over its limit, which the declaration met "
                    f"({str(exc)[:160]})")
        return ""

    def _apply_scenario_correction(self, spec, pid: str, changes: dict, *, findings: List[str], by: str):
        """Put the corrected text in force: on the task's scope (so every
        later prompt of this task carries it) and on its acceptance rows,
        whose prior text, execution and judgment are kept as lineage and
        start again (as a capture redeclaration does): nothing judged under
        the old text is relabelled as a judgment of the new one. The
        existing receipts on this source re-discharge the row at once when
        its cases still match; a sufficiency judgment never carries over.
        ``changes`` maps each corrected field to its new text: all of them
        are one revision, one lineage entry carrying every field's original
        and replacement."""
        scope = spec.scope
        grant = self._packet_grant_for(spec)
        proofs = [dict(p, **changes) if p["id"] == pid else p for p in scope.proofs]
        new = next(p for p in proofs if p["id"] == pid)
        spec = replace(spec, scope=replace(scope, proofs=tuple(proofs)))
        if grant is not None:
            # The grant follows a reviewed correction it was measured against
            # (_correction_packet_problem), on the record: never silently.
            grant.setdefault("revisions", []).append(dict(
                fingerprint=self._packet_fingerprint(spec), by="scenario correction", proof=pid,
                findings=list(findings), corrected_by=by))
        if getattr(self._active_spec, "task_id", None) == spec.task_id:
            self._active_spec = spec
        item = f"{pid}: {new['operation']}; given {new['state']}; expect {new['expect']}; in {new['location']}"
        for row in self.evidence_obligations:
            if (row.get("task") != spec.task_id or row.get("proof_id") != pid or row.get("kind") != "acceptance"
                    or row.get("superseded_by")):
                continue
            scenario = dict((str(k), str(v)) for k, v in (row.get("scenario") or []))
            lineage = list(row.get("scenario_lineage") or [])
            first = next(iter(changes))
            lineage.append(dict(field=", ".join(changes), before=scenario.get(first), after=changes[first],
                                fields={f: dict(before=scenario.get(f), after=t) for f, t in changes.items()},
                                findings=list(findings),
                                finding=findings[0] if findings else None, by=by, behavior=row.get("behavior"),
                                scenario=[list(s) for s in row.get("scenario") or []], execution=row.get("execution"),
                                sufficiency=row.get("sufficiency"), judged_by=row.get("judged_by", ""),
                                receipts=list(row.get("receipts") or [])))
            scenario.update(changes)
            row.update(scenario=sorted(scenario.items()), behavior=item[:300],
                       entry=dict(row.get("entry") or {}, item=item), scenario_lineage=lineage,
                       execution="missing", sufficiency="unjudged", judged_by="", judged_binding="", receipts=[],
                       source_digest=None, discharged_cases=None,
                       reason="scenario corrected by the planner; executed and judged afresh")
            self._sync_obligation(row)
        self._refresh_obligations()
        return spec
