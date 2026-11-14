"""What each seat is told for one task, as a mixin of Session: the lead's
context, the collaborator's prompt, the revision, re-check and fix
prompts, the verifier's and the consultant's.

The methods moved out of session.py unchanged on 2026-11-11. They build
text and call no model; the standing sentences they join are in
session_prompts.py. ``_lead_prompt`` and ``_review_diff`` stay in
session.py, because tests replace ``is_review_only`` and
``_measured_diff_block`` on that module and they must see the
replacement. This module never imports quadratus.session.
The move was redone on 2026-11-12, after the development line was merged
into this file, and took that line's code of the same kind with it.
"""
from __future__ import annotations

from typing import List, Optional

from .registry import resolve
from .reply_parsers import _behavioural_error, _finding_id, boundary_note
from .session_prompts import (
    _AUDIT_LINE_RULE,
    _AUTHORITY_ORDER,
    _BOUNDARY_NOTE_RULE,
    _CONTRACT_BOUNDARY_RULE,
    _COUNTERPART_FIX,
    _COUNTERPART_LENS,
    _COUNTERPART_RECHECK,
    _DESIGN_REVIEW_LENS,
    _EMPTY_BLOCKING_RULE,
    _ERROR_BOUNDARY_LENS,
    _ERROR_PATH_RULE,
    _EXECUTED_ERROR_RULE,
    _FINDING_SHAPE,
    _NO_REQUESTS_IN_FIX,
    _PLAIN_VERDICT_RULE,
    _PROBE_RULE,
    _PROTOCOL_RULE,
    _TEST_COMMAND_RULE,
    _account,
    _handoff_note,
    _review_subject_note,
    _revision_changed_delivery,
)
from .session_types import Complexity, TaskSpec
from .task_kinds import TaskKind
from .verdicts import REVIEW_COMPLETE as _REVIEW_COMPLETE


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
        resolving = self._resolution_findings_block(spec)
        if resolving:
            blocks.append(resolving)
        taken = self._discharges_block(spec)
        if taken:
            blocks.append(taken)
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

    def _writer_turn_budget_note(self, key: str, role, allow_writes: bool) -> str:
        """The round budget in words for a capped writing call after the
        draft: the cap the Fleet already applies to this role on this CLI,
        and nothing else (r12 on 56ff155: t3's capped Grok revision spent
        its 8 rounds on 19 reads, greps and commands and wrote nothing; its
        prompt carried the goal and the headroom but no cap, which only the
        draft was told). No note for an uncapped role, a CLI with no round
        cap, or a call without the write grant."""
        from .runtime import LEAD_CAPPED_ROLES
        limit = self.config.lead_max_turns
        if not limit or not allow_writes or role == "lead" or role not in LEAD_CAPPED_ROLES:
            return ""
        from .cli_providers import CLI_SPECS
        spec = CLI_SPECS.get(key.partition(":")[0])
        if spec is None or not spec.max_turns_flag:
            return ""
        write_by = max(max(1, limit // 3) + 1, (2 * limit) // 3)
        return (f"## Your round budget\n"
                f"This call has at most {limit} tool rounds. At round {limit} this call stops; the "
                "edits already on disk are kept, and the task's checks, its reviewers and the final "
                "audit still decide what happens to them. The findings, the goal and "
                "your remaining room are in this prompt and are current: start with the files they name "
                "and read further (callers, imports, tests) only where the edit needs it, "
                f"have the edit written by about round {write_by}, and keep the last rounds for running "
                "the project's check.")

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

    def _recheck_diff(self, spec: TaskSpec) -> str:
        """What changed since the draft the reviewer read, where the harness
        kept that source (``_collaborator_prompt`` does); otherwise the task's
        whole change."""
        marked = getattr(self, "_reviewed_source", None)
        if marked and marked[0] == spec.task_id:
            return self._review_diff("what changed since the draft you reviewed", since=marked[1])
        return self._review_diff("this task's whole change, against the source it started from")

    @staticmethod
    def _work_account(title: str, text: str, measured: str) -> str:
        """The work a review seat is shown: the lead's reply as its account,
        ahead of the measured diff. When a lead stopped at its turn limit the
        text is the harness's own record of what it measured, and that is
        never labelled as the lead's words."""
        if str(text).startswith("HARNESS RECORD:"):
            return f"{title}:\n{text}{measured}"
        return _account(title, "lead", text, measured)

    def _collaborator_prompt(self, spec: TaskSpec, draft: str, peer: str, *, since: Optional[dict] = None) -> str:
        """``since``: the task's own starting capture, for the in-task
        review only; the measured change is shown against it. A review
        after a merge passes none (its parent's capture is not this
        task's)."""
        label = resolve(peer)
        prompt = self._collaborator_prompt_text(spec, draft, peer, label, since=since)
        self._declare_deferred_evidence(spec, peer)
        return prompt

    def _collaborator_prompt_text(self, spec: TaskSpec, draft: str, peer: str, label, *,
                                  since: Optional[dict] = None) -> str:
        # The review made inside its task: the caller hands over the task's
        # starting capture, or the task is the one running. A review after a
        # merge is neither, and the parent's capture is not that task's, so
        # it is not a blind first read and is shown no diff.
        in_task = since is not None or getattr(self._active_spec, "task_id", None) == spec.task_id
        blind = spec.kind == TaskKind.REVIEW and (in_task or not self.project)
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
                self._work_account(
                    "Current work", draft,
                    self._review_diff("this task's whole change, against the source it started from", since=since)
                    if in_task else "")
                + "\n\n")
        current_work = "" if blind else (
            (_DESIGN_REVIEW_LENS + (self._design_note or "") if self._collaboration_applicable(spec) else "")
            + self._changed_originals_note()
            + self._proof_rows(spec)
            + self._restated_review_block(spec)
            + (self._scope_size_note(spec, audience="reviewer") if self._writes() else "")
            + self._sibling_note()
            + self._inspection_note(spec, peer, "review"))
        return self._role_packet(spec, "reviewer") + "\n\n" + (
            f"Task: {spec.description}\n\n"
            + work
            + f"You are {label.label if label else peer}, contributing an independent "
            "read. Report everything you find; do "
            "not filter to only the important ones: "
            "a reviewer told to be selective suppresses its own findings. "
            "Start each finding that must be fixed before this work is acceptable "
            "on its own line with 'BLOCKING:' -- you will be asked to re-check exactly those "
            "against the revision, each by its own id. End a BLOCKING line with what it rests on and "
            "what it risks: ' | basis: <requirement id, the command that shows it, or the file and "
            "line> | impact: <data-loss|integrity|security|irreversible|correctness|contract|other>'. "
            "A finding with no basis is still recorded and carried to the final audit by id; a "
            "data-loss, integrity, security or irreversible impact holds the affected work whatever "
            "its basis. " + _FINDING_SHAPE + " " + _ERROR_BOUNDARY_LENS + " " + _COUNTERPART_LENS
            + " If you genuinely find nothing worth changing, "
            f"reply exactly 'NO FINDINGS' and nothing else: {_EMPTY_BLOCKING_RULE}. "
            f"Otherwise end your reply with exactly one line, {_REVIEW_COMPLETE}, once you have "
            "read the whole work and reported everything. A reply with no BLOCKING finding and "
            "no such line is recorded as an unfinished review."
            + _review_subject_note(spec, blind=blind)
            + self._goal_authority(peer, source_only=blind)
            + current_work
            + self._review_turn_budget_note(peer)
        )

    def _goal_block(self) -> str:
        """The original goal and the covered requirements' original text,
        verbatim: what every review and every editing call is held to."""
        covered = self._covered_texts()
        return ("\n\n## Original goal (verbatim; it outranks the task text where they differ)\n\n"
                + self.memory.goal.strip()
                + ("\n\n## Original requirements this task covers (verbatim)\n\n"
                   + "\n".join(f"{rid}: {text}" for rid, text in covered) if covered else "")
                + boundary_note(covered)
                + self._audit_history([rid for rid, _ in covered]))

    def _writer_goal(self) -> str:
        """The goal block for an editing call after the draft (r11 on
        136a3ec: t2's revision prompt held the generated task text, which
        prescribed route branches without the goal's path prefix and called
        the existing CLI exit a refusal, and not the goal itself; the draft
        alone had it, through the memory render). Context only: it grants
        nothing and changes no bound."""
        return (self._goal_block()
                + "\n\nMeet the goal and these requirements wherever they fall within this task's work, "
                  "including what they say must happen on an error or a refusal. Task text, a generated "
                  "plan or recipe, or an earlier answer that differs from them does not change them. "
                  + _AUTHORITY_ORDER + " " + _CONTRACT_BOUNDARY_RULE + " "
                  "It grants nothing: the task's permitted paths, edits, commands and size bound hold "
                  "as before, and a condition outside them goes to the record as a line 'BLOCKER: "
                  "<condition>', never an edit there.")

    def _goal_authority(self, peer: str = "", *, source_only: bool = False) -> str:
        """The original goal, verbatim, and the original text of the
        requirements the current task covers, for every task review.

        A reviewer saw the role packet, the task's paraphrase and the draft,
        never the goal (root review of d0f3263, then r04 on 5366412): r03's
        and r04's CLI tasks paraphrased the goal without its JSON refusal
        contract, both closed, and the source still let malformed IDs reach
        argparse's plain-text exit. The lead already gets the goal through
        the memory render; the reviewer now judges against the same text.
        Context only: scope, edits, runners and paths bind as before, and
        nothing here grants anything."""
        covered = self._covered_texts()
        return (self._goal_block()
                + "\n\nJudge the work against the goal and the task: a condition of the goal or a "
                  "covered requirement that falls within this task's work and is not met is a BLOCKING "
                  "finding even when the task text omits it. It grants nothing: the task's permitted "
                  "paths, edits and commands bind as before, and a finding that needs a change outside "
                  "them stays open as a finding, never a reason to edit there. " + _AUTHORITY_ORDER + " "
                + _PROTOCOL_RULE + " " + _CONTRACT_BOUNDARY_RULE
                + ("\n\nFor each covered requirement give one line 'R<n>: MET - <file>:<line>' naming "
                   "where the source meets every condition of it, including what it says must happen "
                   "on an error or a refusal, or 'R<n>: NOT MET - <the condition>', which is a BLOCKING "
                   "finding. A review that leaves a covered requirement without such a line is recorded "
                   "as unfinished. " + _AUDIT_LINE_RULE + " With nothing to change, reply NO FINDINGS on the first line followed "
                   "only by those lines. " + _ERROR_PATH_RULE + " " + _EXECUTED_ERROR_RULE if covered else "")
                + (" " + _BOUNDARY_NOTE_RULE if any(_behavioural_error(t) for _, t in covered) else "")
                + (" " + _TEST_COMMAND_RULE if any(self._test_command_requirement(r) for r, _ in covered) else "")
                + (" " + _PROBE_RULE if covered and not source_only and self._probe_applies(peer) else ""))

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
        resolving = self._resolution_findings_block(spec)
        return (
            f"Task: {spec.description}\n\n"
            f"Your draft:\n{draft}\n\n"
            f"Independent reviews of it:\n{joined}\n\n"
            + (resolving + "\n\n" if resolving else "")
            + "Revise your work. Address every finding explicitly: fix it, or "
            "rebut it with a reason -- silence is not a response. A finding "
            "you cannot decide goes to the record as an open question, not "
            "into the void." + _COUNTERPART_FIX + " "
            + self._revision_delivery()
            + self._binding_feedback(spec)
            + (self._scope_size_note(spec, audience="editor") if self._writes() else "")
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

    def _recheck_prompt(self, spec: TaskSpec, note: str, revision: str, peer: str, *,
                        mine: Optional[List[dict]] = None, instructions: Optional[str] = None) -> str:
        """One reviewer's re-check: its own findings by id, the lead's account
        of the revision, and the measured change since the draft it read.

        ``mine`` and ``instructions`` are the finding records and the rendered
        rules ``_recheck_blocking`` holds for the call. Given only the
        reviewer's note, they are worked out here the same way."""
        if mine is None:
            mine = [r for r in self._finding_records(task=spec.task_id, peer=peer) if r.get("kind") != "unreviewed"]
            mine = [r for r in (mine or self._register_review_findings(spec, peer, note)) if not r.get("held")]
        if instructions is None:
            instructions = self._recheck_instructions([r["fid"] for r in mine])
        history = self._audit_history([(r.get("requirement") or [None])[0] for r in mine])
        return (
            self._role_packet(spec, 'reviewer') + '\n\n' + self._finding_contract(mine) + '\n\n' +
            (history.strip() + '\n\n' if history else '') +
            f"Task: {spec.description}\n\n"
            f"You reviewed this work and raised these findings:\n{self._finding_lines(mine)}\n\n"
            + self._routing_facts(mine)
            + self._work_account("The revised work", revision, self._recheck_diff(spec))
            + self._inspection_note(spec, peer, "recheck")
            + "\n\n" + instructions + self._review_turn_budget_note(peer)
        )

    def _recheck_instructions(self, ids: List[str], *, spec=None) -> str:
        spec = spec if spec is not None else getattr(self, "_active_spec", None)
        return (self._proof_rows(spec) + self._restated_review_block(spec) + "\n\n" if spec is not None else "") + (
                "Check only your BLOCKING findings against the revision, one line per finding and nothing "
                "else about them: '<id>: RESOLVED - <how the revision addresses it>', '<id>: UNRESOLVED "
                "<what specifically remains>', or '<id>: INVALIDATED - <the test, file, receipt or "
                "command showing the finding never held>'. A missing, duplicated or contradictory "
                "line settles nothing. " + _PLAIN_VERDICT_RULE + " A line that opens 'UNRESOLVED:' with no id "
                "is read as a finding that stands. A finding that is a defect of a declared proof scenario's own text, "
                "or a bound case the harness has not shown executed, is restated in full on its line: "
                "'<id>: UNRESOLVED SCENARIO P<n> - <the contradiction> | proposal: <operation|state|expect>: "
                "<the corrected text>' or '<id>: UNRESOLVED EVIDENCE P<n> - <what is not shown>'; the harness "
                "routes it to the planner or runs the checks and asks you again, and no code is rewritten "
                "for it. A defect you find that is not one of these goes on its own "
                "line 'NEW: <finding> | basis: <requirement id, command or file> | impact: "
                "<data-loss|integrity|security|irreversible|correctness|contract|other>'; a remark that "
                "is not a defect goes on 'NOTE: <remark>'. Do not reopen the review of anything else."
                + _COUNTERPART_RECHECK + " Expected ids: " + ", ".join(ids) + ".")

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
        # Each open finding with its original text beside the latest verdict
        # (PR73 6693873055: a follow-up after a capped revision saw only
        # "RFT1-2: UNRESOLVED: no docstring yet", never what was asked).
        texts = {r["fid"]: r.get("text") or "" for r in self.review_findings}

        def shown(verdict: str) -> str:
            original = texts.get(_finding_id(verdict) or "", "")
            return verdict if not original or original in verdict else f"{verdict}\n  (finding: {original})"
        remaining = "\n\n".join(f"[{p}]\n{shown(v)}" for p, v in unresolved)
        return (
            f"Task: {spec.description}\n\n"
            f"Your current work:\n{revision}\n\n"
            f"These blocking findings remain unresolved:\n{remaining}\n\n"
            "Fix them, or state precisely why the reviewer is wrong. Produce "
            "the complete revised work." + _COUNTERPART_FIX
            + (self._scope_size_note(spec, audience="editor") if self._writes() else "")
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
