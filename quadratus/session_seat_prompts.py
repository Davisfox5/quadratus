"""What each seat is told for one task, as a mixin of Session: the lead's
prompt and its context, the collaborator's, the revision, re-check and fix
prompts, the verifier's and the consultant's.

The methods moved out of session.py unchanged on 2026-11-11. They build
text and call no model; the standing sentences they join are in
session_prompts.py. ``_review_diff`` stays in session.py, because a test
replaces ``_measured_diff_block`` on that module and it must see the
replacement. This module never imports quadratus.session.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from .registry import resolve
from .session_prompts import (
    _BLOCKED_REPORT_RULE,
    _CONSULT_NAMES_RULE,
    _DESIGN_RENDER_SHOWS,
    _DESIGN_REVIEW_LENS,
    _DESIGN_REVIEW_ONLY,
    _DESIGN_SELF_VERIFY,
    _EMPTY_BLOCKING_RULE,
    _HARNESS_CAPTURE,
    _NO_REQUESTS_IN_FIX,
    _READ_BEFORE_YOU_EXPLORE,
    _READ_WHAT_YOU_EDIT,
    _account,
    _capture_fixture_note,
    _handoff_note,
    _review_subject_note,
    _revision_changed_delivery,
)
from .session_types import Complexity, TaskSpec, is_review_only
from .task_kinds import TaskKind, guidance_for
from .taskmeta import MAX_REQUEST_PREFACE_LINES
from .verdicts import REVIEW_COMPLETE as _REVIEW_COMPLETE
from .workers import worker_menu


class SeatPromptsMixin:
    """The prompt a lead, reviewer, verifier or consultant gets for a task.

    Session inherits this; nothing here is used on its own.
    """

    def _consult_prompt(self, spec: TaskSpec, question: str, peer: str) -> str:
        label = resolve(peer)
        return self._role_packet(spec, "reviewer") + "\n\n" + (
            f"Task context: {spec.description}\n\n"
            f"A colleague leading this task asks you one question in your "
            f"area of strength:\n{question}\n\n"
            f"You are {label.label if label else peer}. Answer just this "
            "question, concretely. You have no other context by design; if it "
            "cannot be answered without more, say exactly what is missing."
        )

    def _lead_context(self, spec: TaskSpec) -> List[str]:
        """The capped predecessor's handoff and the task's files, for a lead.

        Built fresh for every lead prompt from the selected project's working
        tree, so a continuation sees the files as they are now, not as its
        predecessor left them in a transcript. The harness picks the files:
        a capped predecessor's changed files first, then the scope's exact
        permitted paths. See :mod:`quadratus.project_files` for what is
        refused.
        """
        if not self.project:
            return []
        from .project_files import context_pack, render_pack
        blocks: List[str] = []
        paths: List[str] = []
        record = (self.turn_limited_records.get(self._continues or "")
                  or self.failed_records.get(self._continues or ""))
        if record:
            blocks.append(_handoff_note(record))
            paths.extend(record.get("changed") or [])
        if spec.scope is not None:
            paths.extend(p for p in spec.scope.permitted_paths if spec.scope.permits(p))
        included, refused = context_pack(self.project, paths, exclude=self.config.project_excludes,
                                         baselines=self._cap_baselines.get(self._continues or ""))
        pack = render_pack(included, refused)
        if pack:
            blocks.append(pack)
        return blocks

    def _turn_budget_note(self, lead: str) -> str:
        """The lead's round budget in words, when a cap applies to its CLI.

        Run 14: both leads spent every round reading and were capped with no
        write (Grok) or with the edit last and no check (Claude). Neither
        prompt said a cap existed. Text only: the Fleet applies the cap, and
        nothing here changes it.
        """
        limit = self.config.lead_max_turns
        if not limit or not self._writes():
            return ""
        from .cli_providers import CLI_SPECS
        spec = CLI_SPECS.get(lead.partition(":")[0])
        if spec is None or not spec.max_turns_flag:
            return ""       # codex has no round cap, so there is nothing to plan against
        read_by = max(1, limit // 3)
        write_by = max(read_by + 1, (2 * limit) // 3)
        return (f"## Your round budget\n"
                f"This call has at most {limit} tool rounds. At round {limit} it stops with "
                "whatever is on disk, and the task goes back unfinished, unreviewed and "
                f"unchecked. Plan against it: finish reading by about round {read_by}, starting "
                "from the files and notes in this prompt, which are current; have the edit "
                f"written by about round {write_by}; keep the last rounds for running the "
                "project's check and fixing what it shows.")

    def _lead_prompt(
        self,
        spec: TaskSpec,
        *,
        lead: Optional[str] = None,
        extras: Optional[List[str]] = None,
        workers_closed: bool = False,
    ) -> str:
        parts = [
            self.memory.render(
                current=spec.description, recent=self.config.recent_entries
            ),
        ]
        map_block = self._map_block()
        if map_block:
            parts.append(map_block)
        # One statement of what a reply is. "Produce the complete work" stood
        # beside "commission a worker for it first" with nothing to say which
        # governs, and the recitation asked for a third thing, a confirmation.
        parts.append("You are leading this task. The finished work is yours to deliver.")
        no_worker = self._no_worker_reason(spec, closed=workers_closed)
        instruction, page = self._design_instruction(spec)
        if instruction == "harness":
            parts.append(_HARNESS_CAPTURE.format(page=page, steps=len(spec.scope.capture["steps"]))
                         + _capture_fixture_note(spec))
        elif instruction == "self":
            import sys as _sys
            package_root = Path(__file__).resolve().parent.parent
            command = (f"PYTHONPATH={package_root} {_sys.executable} -m quadratus.design_evidence "
                       f"<url of the page> {spec.task_id} .")
            template = _DESIGN_REVIEW_ONLY if is_review_only(spec) else _DESIGN_SELF_VERIFY
            parts.append(template.format(command=command, shows=_DESIGN_RENDER_SHOWS))
        # Stated before the work, checked after it. Telling a model its bound
        # helps some; measuring the diff is what makes the bound real, and
        # both happen -- see _assess_scope.
        parts.append(self._role_packet(spec, 'lead'))
        if no_worker:
            # "its edits" only where this call may edit: a run with no grant is
            # told below to return analysis, and must not be told both.
            parts.append(no_worker + (" Do its reading and its edits yourself." if self._writes()
                                      else " Do its reading yourself."))
        else:
            parts.append(worker_menu())
            parts.append('To commission one worker, reply only WORKER followed by JSON: '
                         '{"errand":"code","instruction":"one bounded request",'
                         '"demanding":false,"write":false,"needs":[]}. '
                         'errand is one of the menu names above (demanding is a boolean flag, not an '
                         'errand); instruction is a non-empty string; write and demanding are booleans; '
                         'needs is a list of strings; steps and token_limit, when given, are whole '
                         'numbers and retry_of is a string; no string holds a NUL or an unpaired \\u '
                         'escape. A WORKER reply that does not parse this way, a '
                         'helper that nests another helper, or write:true without an edit grant fails '
                         'this task. '
                         # The two needs no worker can meet stay declarable, so a
                         # mis-scoped errand is refused before it is paid for
                         # (tests/test_worker_tool_fit.py); the lead is told they
                         # are always refused, not offered them as grants.
                         'needs is [] for an answer in text or ["patch"] to change files '
                         '(with write:true, and only then). "execute" and "direct-write" may '
                         'be declared and are always refused before any call: that part is '
                         'yours. '
                         'Workers cannot delegate. After a failed errand, a retry may add '
                         '"retry_of":"failed-label" and one "helper" object with its own '
                         'errand, instruction, needs and demanding fields. The helper is always '
                         'read-only; both are siblings reporting to you. Optional steps (1 to '
                         f'{self.workers.budget.max_worker_steps}) and token_limit (1 to '
                         f'{self.workers.budget.max_worker_tokens} reported tokens) bound a worker '
                         'continuation. A value outside those bounds, or a needs label other than the '
                         'three above, is refused before any call and counts as a failed errand. '
                         'Defaults remain one shot.')
            if self.config.in_session_workers:
                parts.append('If a commission_worker tool is available in this session, use it for '
                             'read-only errands instead of a WORKER reply: you keep your session and '
                             'the answer comes back as the tool result, where a WORKER reply ends your '
                             'call and you start over. Keep the WORKER reply for write errands, or if '
                             'the tool is not listed. A request written in your reasoning or mid-answer '
                             'is not served; only the tool call or a whole WORKER reply is.')
        if self.project:
            # Deliberately no longer "inspect the project source": that told the
            # lead to go exploring in the same breath as the guidance below
            # asked it not to, and attempt 10 shows which of the two won.
            parts.append("The project source is in your working directory. "
                         + ("Implement this task using the edit method in your role instructions; prose alone is not implementation."
                            if self._writes() else
                            "This run has no edit grant. Return analysis and proposed changes only."))
            parts.append(_BLOCKED_REPORT_RULE)
        parts.append(
            "To read a filed artifact in full before working, reply with "
            "exactly 'FETCH: <artifact-id>' and nothing else -- you will get "
            "the content and be asked again. A request ("
            + ("FETCH or CONSULT" if no_worker else "FETCH, CONSULT or WORKER") + ") closes "
            "the reply and you start again with its answer: at most "
            + str(MAX_REQUEST_PREFACE_LINES) + " lines of preface may "
            "precede it, and a reply that carries a CHANGED line is a delivery, never a "
            "request. There is no ASK channel for you: a reply beginning ASK:, or a request "
            "line that does not parse, fails this task. Report a blocker in words instead."
        )
        if lead is None:
            parts.append("Consults are not available in this task; decide with your own judgment"
                         + ("." if no_worker else " or commission a worker."))
        if lead is not None:
            consultables = [p for p in self.brain_trust if p != lead]
            names = ", ".join(
                (resolve(p).label if resolve(p) else p) for p in consultables
            )
            parts.append(
                "If one specific question outside your strengths blocks you, "
                "reply with only 'CONSULT <member>: <question>' -- the member "
                "answers blind and their answer comes back to you. Members "
                f"you may consult: {names}. " + _CONSULT_NAMES_RULE + " At most "
                f"{self.config.max_consults} per task; a consult is a "
                "question, not a conversation."
            )
        guidance = guidance_for(spec.kind)
        if guidance:
            # Stated as requirements rather than advice. These exist because
            # models measurably do badly at the thing each one guards, so a
            # hint the lead is free to skip would not survive contact.
            parts.append(
                "This kind of work carries known failure modes. Treat each of "
                "these as a requirement:\n"
                + "\n".join(f"- {g}" for g in guidance)
            )
        for extra in extras or ():
            parts.append(extra)
        # Recitation: the task statement again, at the very end. The end of a
        # long context is the position attention favours, and re-emitting the
        # objective there is Manus's published fix for goal drift on long
        # tool loops. The middle of the prompt -- where the task would
        # otherwise sit once history piles up -- is the least reliable real
        # estate there is.
        if self.project:
            # Immediately before the recitation, which is the other thing this
            # prompt most needs read. Attempt 10 put this in the middle, after
            # an instruction to inspect the source, and got no delegation at
            # all out of it -- so it is both moved and no longer contradicted.
            parts.append(_READ_WHAT_YOU_EDIT if no_worker else _READ_BEFORE_YOU_EXPLORE)
        # The recitation asks for nothing but the delivery: "confirm every
        # part is addressed" read as a separate reply, and a reply is only
        # ever a delivery or a request.
        parts.append(
            "Before you deliver, re-read your task, restated verbatim, and "
            f"check the work against every part of it:\n\n{spec.description}"
        )
        return "\n\n".join(parts)

    def _no_worker_reason(self, spec: TaskSpec, *, closed: bool = False) -> str:
        """Why the lead prompt offers no worker for this task, or "" when it does.

        The worker menu and the WORKER protocol are about 3 KB on every lead
        call. They are left out where a worker cannot be used (the channel
        closed, the task's errand budget zero or spent) and where the task is
        one small pass by its own definition (the direct tier, a rote task),
        and the prompt then says no worker is offered, so nothing in it
        implies a channel it does not describe. Prompt text only: the harness
        serves a WORKER request exactly as before.
        """
        if closed:
            return "The worker channel is closed for this task."
        if self.workers.remaining(spec.task_id) <= 0:
            return "This task has no worker errands left."
        if self._tier(spec) == "direct":
            return "This is a direct-tier task (one small change, made in one pass), so no worker is offered."
        if spec.complexity == Complexity.ROTE:
            return "This is a rote task (mechanical work, done in one pass), so no worker is offered."
        return ""

    def _collaborator_prompt(self, spec: TaskSpec, draft: str, peer: str) -> str:
        label = resolve(peer)
        blind = spec.kind == TaskKind.REVIEW
        if not blind:
            # The source this review reads, kept so its re-check can be shown
            # exactly what the revision changed.
            reviewed = self._capture_source()
            if reviewed is not None:
                # Only the files the task changed are new bytes: the rest are
                # the starting snapshot's own, not a second copy of the project.
                start = self._task_before or {}
                reviewed = {name: start[name] if start.get(name) == data else data
                            for name, data in reviewed.items()}
            self._reviewed_source = (spec.task_id, reviewed) if reviewed is not None else None
        work = ("This is your first, blind review of the task's source and goal. Another seat's findings "
                "are withheld until your independent read is complete. Inspect the source yourself; "
                "report your own findings rather than reviewing or validating another review. The lead "
                "will receive both reads afterward to reconcile them.\n\n"
                if blind else
                _account("Current work", "lead", draft, self._review_diff(
                    "this task's whole change, against the source it started from"))
                + "\n\n")
        return self._role_packet(spec, "reviewer") + "\n\n" + (
            f"Task: {spec.description}\n\n"
            + work
            + f"You are {label.label if label else peer}, contributing an independent "
            "read. Report everything you find; do "
            "not filter to only the important ones: "
            "a reviewer told to be selective suppresses its own findings. "
            "Start each finding that must be fixed before this work is acceptable "
            "on its own line with 'BLOCKING:' -- you will be asked to re-check exactly those "
            "against the revision. If you genuinely find nothing worth changing, "
            f"reply exactly 'NO FINDINGS' and nothing else: {_EMPTY_BLOCKING_RULE}. "
            f"Otherwise end your reply with exactly one line, {_REVIEW_COMPLETE}, once you have "
            "read the whole work and reported everything. A reply with no BLOCKING finding and "
            "no such line is recorded as an unfinished review."
            + _review_subject_note(spec)
            + (("\n\n## Original goal (verbatim)\n\n" + self.memory.goal.strip()) if blind else "")
            + ((_DESIGN_REVIEW_LENS + (self._design_note or "")
                if self._collaboration_applicable(spec) else "") + self._changed_originals_note()
               if not blind else "")
            + self._review_turn_budget_note(peer)
        )

    def _changed_originals_note(self) -> str:
        """For a reviewer: run-start test or support files that now differ.
        f2's t2 changed the original helper tests/ui/load_app.js to fit the
        new code and its review approved it without the edit being named."""
        from .integration import changed_originals
        from .project import Project
        if not self._original_tests or not self.project:
            return ""
        try:
            current = Project(self.project, exclude=self.config.project_excludes).contents()
        except Exception:  # noqa: BLE001 -- a note, never a failure
            return ""
        changed = changed_originals(self._original_tests, current)
        if not changed:
            return ""
        shown = ", ".join(changed[:20]) + (f" and {len(changed) - 20} more" if len(changed) > 20 else "")
        return ("\n\nHarness record: these test or test-support files existed when the run started and "
                f"now differ or are gone: {shown}. Their run-start versions are run against this code "
                "separately. Judge each edit: a test or helper changed to fit the code is not evidence "
                "that the code meets the original tests.")

    def _revision_prompt(self, spec: TaskSpec, draft: str, notes: List[str]) -> str:
        joined = "\n\n".join(notes)
        return (
            f"Task: {spec.description}\n\n"
            f"Your draft:\n{draft}\n\n"
            f"Independent reviews of it:\n{joined}\n\n"
            "Revise your work. Address every finding explicitly: fix it, or "
            "rebut it with a reason -- silence is not a response. A finding "
            "you cannot decide goes to the record as an open question, not "
            "into the void. "
            + self._revision_delivery()
            + (self._scope_headroom(spec) if self._writes() else "")
            + (_revision_changed_delivery(self._interim_edits_note()) if self._writes() else "")
            + _NO_REQUESTS_IN_FIX
        )

    def _revision_delivery(self) -> str:
        """How the lead should return a revision, given its actual grant.

        A project being *selected* is not a grant to write to it. The old text
        told the lead to update the project whenever a project was set, so a
        read-only review run instructed its lead to edit files it had no
        permission to touch -- a contradictory instruction that made a
        correctly-completed review look like a thwarted implementation. The
        grant, not the selection, decides.
        """
        if self._writes():
            return "Update the project using the edit method in your role instructions."
        if self.project:
            return (
                "This run is read-only: you have no write grant for the "
                "project, so do not attempt to edit it. Produce the complete "
                "revised work as text. Reporting that the subject is not ready "
                "is a complete, successful outcome -- it is not an unfinished "
                "implementation, and you should not convert findings into "
                "edits you are not authorised to make."
            )
        return "Produce the complete revised work, not a diff."

    def _recheck_prompt(self, spec: TaskSpec, note: str, revision: str, peer: str) -> str:
        """One reviewer's re-check: its own findings, the lead's account of
        the revision, and the measured change since the draft it read."""
        return (
            self._role_packet(spec, 'reviewer') + '\n\n' +
            f"Task: {spec.description}\n\n"
            f"You reviewed this work and raised these findings:\n{note}\n\n"
            + _account("The revised work", "lead", revision, self._recheck_diff(spec)) + "\n\n"
            "Check only your BLOCKING findings against the revision. Do "
            "not raise new findings. Reply exactly 'RESOLVED' if every "
            "blocking finding is addressed, otherwise 'UNRESOLVED: <what "
            "specifically remains>'."
            + self._review_turn_budget_note(peer)
        )

    def _review_turn_budget_note(self, peer: str) -> str:
        """The reviewer's round budget in words, when a cap applies to its CLI."""
        limit = self.config.lead_max_turns
        if not limit:
            return ""
        from .cli_providers import CLI_SPECS
        spec = CLI_SPECS.get(peer.partition(":")[0])
        if spec is None or not spec.max_turns_flag:
            return ""
        return (f"\n\nThis call has at most {limit} tool rounds. At round {limit} it stops and the "
                "review is recorded as unfinished, with nothing you wrote read as a finding. The work "
                "is in this prompt; read the files it names, run any approved check once, and write "
                "the verdict well inside the budget.")

    def _fix_prompt(self, spec: TaskSpec, revision: str, unresolved: List[tuple]) -> str:
        remaining = "\n\n".join(f"[{p}]\n{v}" for p, v in unresolved)
        return (
            f"Task: {spec.description}\n\n"
            f"Your current work:\n{revision}\n\n"
            f"These blocking findings remain unresolved:\n{remaining}\n\n"
            "Fix them, or state precisely why the reviewer is wrong. Produce "
            "the complete revised work."
            + (_revision_changed_delivery(self._interim_edits_note()) if self._writes() else "")
            + _NO_REQUESTS_IN_FIX
        )

    def _verifier_prompt(self, spec: TaskSpec, draft: str, verifier: str) -> str:
        label = resolve(verifier)
        return self._role_packet(spec, "verifier") + "\n\n" + (
            f"Task: {spec.description}\n\n"
            + _account("Proposed answer", "author", draft, self._review_diff(
                "this task's whole change, against the source it started from"))
            + "\n\n"
            f"You are {label.label if label else verifier}, verifying security "
            "work you did not author. Check it for correctness, for anything "
            "unsafe it recommends, and for anything it asserts without "
            "evidence. A claim that the environment blocked the work counts "
            "only when it quotes the failing command, its exit status and the "
            "verbatim error; without those, treat it as unverified. State "
            "plainly whether it should be accepted, and what must change if "
            "not. Start each finding that must be fixed on its own line with "
            "'BLOCKING:' or 'UNRESOLVED:'. The words BLOCKING and UNRESOLVED in capitals "
            "are read as findings wherever they appear, so in prose write them in lower "
            f"case. {_EMPTY_BLOCKING_RULE}: when nothing must change, end with VERDICT: "
            "ACCEPT. Do not redo the work; "
            "verify it. End your reply with exactly one line, VERDICT: ACCEPT or "
            "VERDICT: REJECT; that line is read as your decision, and a REJECT must "
            "name at least one BLOCKING: or UNRESOLVED: finding above it. Write nothing "
            "after the word on that line: an ACCEPT followed by anything else is not "
            "read as an acceptance. Any VERDICT line that is not exactly VERDICT: ACCEPT "
            "is read as a rejection, and the finding stands. If you write more than one "
            "VERDICT line, the last is the one read."
            + self._review_turn_budget_note(verifier)
        )
