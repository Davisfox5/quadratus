"""Design evidence and capture for the session engine, as a mixin of
Session: who renders, the capture profile and its declaration, the
capture-only samples the harness writes, the design check, its one
recapture and one fix, and the cross-vendor review of the final renders.

The methods and the file helpers above them moved out of session.py
unchanged on 2026-11-11. The methods reach the rest of Session only
through ``self``. This module never imports quadratus.session.
The move was redone on 2026-11-12, after the development line was merged
into this file, and took that line's code of the same kind with it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from .delegation import invocation
from .providers import TurnLimitReached
from .reply_parsers import _design_approved, _parse_capture_line, _parse_fixture_blocks
from .session_prompts import (
    _CAPTURE_SCOPE_REQUEST,
    _CAPTURE_SHAPE,
    _DESIGN_REVIEW_LENS,
    _FRONTEND_WITHOUT_RENDER,
    _ONE_CAPTURE_MARKER,
    _ONE_FIXTURE_FENCE,
    _design_fix_delivery,
)
from .session_types import (
    CapabilityUnavailable,
    PreviewUnavailable,
    _frontend_without_rendered_file,
    _rendered_file,
    is_design_task,
)
from .verdicts import design_blocking as _design_blocking


def _step_failure(failure) -> bool:
    """Whether a harness capture failed on a declared interaction step: the
    capture tool exits 1 only when a step did not happen (design_evidence
    main), 2 for usage and fixture errors, 3 for a crash in the capture
    itself, and a preview failure carries its own text. Exit 1 is the
    tool's positive statement, never an inference from a nonzero exit
    (Codex review of f8d8c03: an uncaught exception used to exit 1 too)."""
    return str(failure).startswith("the capture exited with 1:")


def _declaration_only(records) -> bool:
    """Every typed problem on the evidence is a capture-declaration problem
    (design_evidence.CAPTURE_DECLARATION): nothing about the page or the
    source is reported, so a redeclaration is the whole remedy."""
    from .design_evidence import CAPTURE_DECLARATION
    kinds = [r.get("kind") for r in (records or [])]
    return bool(kinds) and all(k == CAPTURE_DECLARATION for k in kinds)


def _write_fixture_bound(root, path: str, data: bytes) -> str:
    """Write ``data`` at ``path`` through directory handles, or say why not.

    Every component is opened with ``O_NOFOLLOW`` relative to the handle of
    the directory before it, so a link swapped in after the pathname check
    is refused at the operation itself. The bytes go to a unique temporary
    name in the task folder, created ``O_EXCL``; only after the whole write
    succeeds is that inode linked to the declared name, which fails if
    anything has appeared there, so the declared name never names a
    partial or foreign file (Codex reviews of 6844b97, cc045a1 and a7cde45:
    a pathname check cannot protect a pathname the write re-resolves, a
    pathname unlink can delete a replacement, and a partial file left under
    the declared name became evidence later). Nothing is deleted by the
    declared name; the temporary name is this call's alone.
    """
    import secrets
    parts = PurePosixPath(path).parts
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fds: List[int] = []
    temp = f".supply-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        fds.append(os.open(str(root), dir_flags))
        for part in parts[:-1]:
            try:
                fd = os.open(part, dir_flags, dir_fd=fds[-1])
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=fds[-1])
                fd = os.open(part, dir_flags, dir_fd=fds[-1])
            fds.append(fd)
        folder = fds[-1]
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o644, dir_fd=folder)
        fds.append(fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError as exc:
            # The declared name was never created, so nothing partial can be
            # read by it; the temporary is emptied through its own
            # descriptor as a courtesy and left under its private name.
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            return (f"the write failed before the file was published; the declared name does not exist "
                    f"({exc.__class__.__name__}: {exc})")
        try:
            os.link(temp, parts[-1], src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        except FileExistsError:
            return "the target appeared before publication; nothing is overwritten"
        try:
            os.unlink(temp, dir_fd=folder)
        except OSError:
            pass    # the private temporary name stays; the published file is complete either way
        return ""
    except FileExistsError:
        return "the temporary name already exists; nothing is written"
    except (OSError, ValueError) as exc:
        return f"the write was refused at the operation ({exc.__class__.__name__}: {exc})"
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _confined_fixture_target(root, path: str, task_id: str):
    """The exact task-owned file to write for ``path``, or ``(None, why)``.

    Checked before any write (Codex review of 351d3ba: the later reader
    refuses links, but a write through a linked ``.quadratus``,
    ``capture-fixtures``, task folder or target had already landed outside
    the project): the path is exactly ``.quadratus/capture-fixtures/<task
    id>/<name>`` with a plain name, no existing component on the way is a
    symlink, and the resolved target stays inside the resolved project root.
    """
    parts = PurePosixPath(path).parts
    if (len(parts) != 4 or parts[:2] != (".quadratus", "capture-fixtures") or parts[2] != task_id
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[3])):
        return None, f"{path}: not this task's own fixture path"
    # The harness writes this file on a model's word, between calls, in a
    # folder no listing reads. A name a test runner or interpreter loads by
    # position (conftest.py, any .py, package.json, a runner config) would be
    # planted code written by the harness itself.
    from .project_watch import loaded_by_position
    if loaded_by_position(parts):
        return None, (f"{path}: a file of that name is loaded by a test runner or an interpreter "
                      "because of where it sits, so it is not written as a capture sample")
    base = Path(root)
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None, f"{path}: {current.relative_to(base).as_posix()} is a symlink"
        if current.exists() and not current.is_dir() and current != base / Path(path):
            return None, f"{path}: {current.relative_to(base).as_posix()} is not a directory"
    target = base / Path(path)
    if target.exists() and not target.is_file():
        return None, f"{path}: the target exists and is not a regular file"
    try:
        if not target.parent.resolve().is_relative_to(base.resolve()):
            return None, f"{path}: resolves outside the project"
    except OSError as exc:
        return None, f"{path}: cannot be resolved ({exc})"
    return target, ""


def _missing_fixtures(project, capture: dict, task_id: str) -> List[str]:
    """The file-step paths of ``capture`` that are not usable fixtures in
    ``project`` now, by the capture's own rule (``design_evidence._fixture``)."""
    from .design_evidence import _fixture
    from .project import Project
    # Not getattr(project, "root"): a Path has a ``root`` too, and it is "/".
    root = project.root if isinstance(project, Project) else Path(project)
    missing = []
    for step in capture.get("steps") or []:
        if step.get("action") != "file":
            continue
        try:
            _fixture(root, step.get("path") or "", task_id)
        except ValueError:
            missing.append(str(step.get("path")))
    return missing


def _snapshot_files(snapshot) -> Dict[str, str]:
    """Project-relative path -> sha256 for the files a render identity names."""
    _task, _target, _steps, evidence = snapshot
    folder = evidence["summary"].rsplit("/", 1)[0]
    files = {evidence["summary"]: evidence["sha256"]}
    files.update({f"{folder}/{view}/page.png": digest for view, digest in evidence["screenshots"].items()})
    files.update({f"{folder}/{view}/evidence.json": digest for view, digest in (evidence.get("records") or {}).items()})
    return files


class DesignMixin:
    """What a rendered change is checked against, and by whom.

    Session inherits this; nothing here is used on its own.
    """

    def _brief_design_reviewers(self, spec) -> None:
        """Point the design reviewers at the draft's renders, if it left any.
        Nothing is judged here: the enforced check runs after the last edit."""
        self._design_note = ""
        # Reset before any early return: a non-design task must never inherit
        # the previous task's renders (Codex review of 3d5c3f3).
        self._review_evidence = []
        self._review_evidence_hashes, self._review_snapshot = {}, None
        if not (self._collaboration_applicable(spec) and self.project):
            return
        # Who renders is the lead's instruction fixed at dispatch, the same
        # bound mode the lead prompt and the harness capture use (map P3.4;
        # O-NEXT-12, Codex 5865903915); the live reading is recorded beside it.
        if self._required("design_instruction", self._live_design_instruction(spec)) == "harness":
            # Harness renders are taken after the final edit and a passing
            # gate, so collaborators are promised none of the draft.
            return
        from .design_evidence import check
        ok, _, shots = check(self.project, spec.task_id, self._last_edit_started or 0,
                              expected_source=self._trusted_source())
        if ok and not self._evidence_refusals(spec, self._evidence_files(spec, shots)):
            self._review_evidence = self._evidence_files(spec, shots)
            self._design_note = (
                "\n\nThe lead's rendered evidence for this draft, copied read-only into your "
                "working copy at these paths: " + ", ".join(self._shown(shots))
                + ". Judge the design from the screenshots as well as the code.")

    def _recheck_after_fix(self, spec, lead, task, record, harness):
        """The checks again after the one design-fix call, for both capture
        modes: the gate, the harness's own recapture where it captures, and
        the evidence read exactly as it was the first time. Returns ``(ok,
        problem, shots, records)``, or None when the failure was filed as
        audit debt (the record is already kept).

        One helper because the two branches had drifted: only the
        self-capture branch qualified the evidence set and considered audit
        debt after the fix (review 1110, session second half 20). A harness
        fix never runs for a review-only task, so audit debt cannot arise
        there; what changes for it is that an overflow-only failure on an
        incomplete evidence set now names that too, as on the first read.
        """
        from .design_evidence import check_records
        self._run_integration_gate(lead, spec, task)
        # The gate set the stage to checks; the recheck is design work again
        # (Codex, 5863678556), so its facts carry the design stage.
        self._stage("design")
        failure = self._harness_capture(spec) if harness else None
        if failure:
            self._hand_off_preview(spec, task, record, failure)
            return False, failure, [], [dict(kind="integrity", message=failure)]
        ok, problem, shots, records = check_records(self.project, spec.task_id, self._last_edit_started or 0,
                                                    expected_source=self._trusted_source(),
                                                    receipt=self._capture_receipt(spec.task_id, harness))
        self._refuse_mismatched(spec, record, task, records, harness)
        problem, records = self._qualify_debt(spec, ok, problem, records)
        if self._audit_debt_applies(spec, ok, records):
            record.update(verified=False, problem=problem,
                          findings=self._record_audit_findings(spec, records))
            self.design_checks.append(record)
            task.keep(json.dumps(record), kind="design-evidence")
            return None
        return ok, problem, shots, records

    def _undeclared_design_work(self, spec, task) -> None:
        """A task whose contract asked for no design evidence, measured after
        its last edit: did it change a rendered file all the same?

        Design work is decided at dispatch from the kind and the declared
        paths, and a parent folder ("app") names nothing rendered, so a
        backend task scoped that way could ship a template or a stylesheet
        with no capture and no design review (review 1110, session first
        half 5). Only the paths a task changed can settle it, and of those
        only a file that is itself a rendered type and is no test file
        (``_rendered_file``): a folder name alone settles nothing here. The
        contract is fixed at dispatch and carries no capture declaration to render
        from, so nothing is captured after the fact: the task is left with
        the design debt a declared design task with no renders is left with
        (the same open finding, the same record, the same named stop), whose
        problem names the rendered files and says no render was reviewed. It
        is never closed as design-verified. The orchestrator's prompt says
        what avoids this (``_KIND_REQUEST``).

        Recorded the one way design debt is recorded, in the typed record
        and the legacy list alike: written any other way the two disagree,
        and the run's record says its own readings do not match.

        A continuation answers for the unfinished task it continues as well,
        when that task was not itself held to design evidence: a capped or
        failed task runs no design check at all and a clean continuation
        recovers it, so a template the capped task wrote under a parent
        scope was settled by a task that never touched it. A predecessor
        whose own contract asked for renders keeps its own unmet edges, and
        those already hold the run.
        """
        if not (self.project and self._writes()) or self._task_before is None:
            return
        state = self._inspect_partial_edits(self._task_before, stopped=False)
        inherited = self._undeclared_predecessor_changes()
        rendered = sorted({path for path in [*(state.get("changed") or []), *inherited] if _rendered_file(path)})
        if not rendered:
            return
        named = ", ".join(rendered[:8]) + (f" and {len(rendered) - 8} more" if len(rendered) > 8 else "")
        if any(path in inherited for path in rendered):
            named += f"; counting what {self._continues}, the unfinished task it continues, changed"
        record = dict(task=spec.task_id, undeclared=True, rendered=rendered[:40])
        if not self.config.design_self_verify:
            # The operator turned design verification off for every task.
            record.update(verified=None, problem="design self-verification disabled by the operator")
            self.design_checks.append(record)
            return
        problem = (f"it changed rendered files ({named}) that its declared scope did not name as UI work, "
                   "so it was dispatched with no capture and no render of them was captured or reviewed; "
                   "name the rendered files in a task's scope, or label it frontend, so they are captured")
        record.update(verified=False, problem=problem)
        self.design_checks.append(record)
        task.keep(json.dumps(record), kind="design-evidence")
        self._stage("design")
        self._open_finding("invalid_proof",
                           f"Task {spec.task_id} is design work without clean rendered evidence: {problem}.")
        self._design_unverified.append((spec.task_id, problem))
        self._edge("evidence", False)
        self._note(f"task {spec.task_id}: changed rendered files with no render reviewed ({named[:120]})")

    def _undeclared_predecessor_changes(self) -> List[str]:
        """The files changed by the capped or failed task this one continues,
        when that task's contract asked for no design evidence; else none."""
        prior = (self.turn_limited_records.get(self._continues or "")
                 or self.failed_records.get(self._continues or ""))
        if not prior:
            return []
        outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == self._continues), None)
        contract = getattr(outcome, "contract", None)
        required = (contract.get("required") if isinstance(contract, dict) else None) or {}
        if isinstance(required, dict) and required.get("design_evidence") in ("harness", "self"):
            return []
        return [path for path in prior.get("changed") or [] if isinstance(path, str)]

    def _recapture_declared(self, spec, lead, task, record, verdict, *, source="the design reviewer from another vendor"):
        """Ask the lead for the page and steps that reach the changed state,
        recapture once, and return ``(ok, problem, shots, records)``; None
        when the lead gave no usable new declaration (the verdict stands).

        The capture the orchestrator declared at dispatch is what the harness
        renders. GameTape's empty-state feature (f5) appears only when a
        project has no clips, which the declared steps never set up: the
        renders showed the normal page, the reviewer rightly said the change
        was not in them, and the task closed with that as an open finding on
        all three leads that built it. The harness checked that a capture ran,
        not that it showed the change. The lead built the interface and knows
        the state it needs, so it redeclares; the same validator and fixture
        rules as a SCOPE capture apply, and the recapture is bounded to one.
        """
        if record.get("recapture_spent"):
            # One redeclaration per design check, whichever route asks first
            # (Codex review of 351d3ba: the capture-check route succeeded, a
            # blind review then bought a second declaration, a third capture
            # and a second review). The first receipt stays as written.
            self._note(f"task {spec.task_id}: the one redeclaration is spent; the verdict stands")
            record["recapture_blocked"] = f"{source} asked again after the one redeclaration was spent"
            return None
        record["recapture_spent"] = True
        current = spec.scope.capture or {}
        self._count("recapture")
        self._note(f"task {spec.task_id}: renders do not show the change; one recapture declaration")
        ledger = self.memory.ledger
        prompt = (
            # The operator's own words travel with the declaration (r05,
            # 20261011T183849Z-869a1896: the goal prescribed /#q=Alpha, the
            # dispatched capture loaded / and its first wait could not be
            # met, and the redeclaring call never saw the goal).
            f"## Goal (verbatim)\n\n{self.memory.goal.strip()}\n\n"
            + (("## Requirements\n\n" + "\n".join(f"{rid}: {text}" for rid, text in ledger.requirements.items())
                + "\n\n") if ledger.requirements else "")
            + "Any page, fragment, steps or state the goal or requirements prescribe for the capture "
            "still apply: declare them as written there.\n\n"
            f"Task: {spec.description}\n\nThe harness captured {current.get('path')}, exactly as "
            f"declared with nothing added to it, after "
            f"{len(current.get('steps') or [])} declared interaction step(s) and {source} "
            f"replied: {verdict.strip()[:300]}\n\nThe interface this task changed "
            "does not appear in those renders, so the declared page or steps do not reach the state "
            "where it shows. Reply with exactly one line\n"
            'CAPTURE: {"path": "/route", "steps": [...]}\n'
            "naming the page and the interaction steps that reach that state, as a user meets it. "
            # The shape rules only (one validator, one statement). The whole
            # orchestrator rule was sent here, 2.5 KB for a one-line answer,
            # with advice to declare no steps that this call's own rule refuses.
            + _ONE_CAPTURE_MARKER + " " + _CAPTURE_SHAPE + "End the steps with a wait that "
            "names something only the changed state shows, never an element already on the page "
            "at load: a declaration whose last step is not a wait is not used. If no page and steps on this "
            "preview can reach that state, reply exactly CAPTURE: none and one line saying why. "
            "Do not change files: this call declares, it does not edit, so a file step may name "
            "only a sample that already exists in the project (a committed sample, or a fixture "
            "an earlier call of this task wrote); a sample that does not exist yet cannot be "
            "declared here. "
            # The validator's path rule, which left this prompt with the
            # orchestrator's whole capture rule. One line, no second try.
            "A file step's path is project-relative: nothing absolute, outside the project or "
            f"hidden, except a fixture under .quadratus/capture-fixtures/{spec.task_id}/."
        )
        try:
            with invocation(spec.task_id, "capture-redeclare"):
                reply = self._invoke_model(lead, prompt)
        except TurnLimitReached as exc:
            # A declaration call that hits its round cap gave no declaration
            # (series rule-7590b13 f5: the cap raised out of the design check
            # and ended a run whose graders all passed). Its narration is
            # kept, never parsed; the verdict stands, as for a reply with no
            # marker.
            why = self._capped_declaration(spec, lead, task, "capture-redeclare", exc)
            record["recapture"] = dict(declared=None, problem=why)
            return None
        task.record("assistant", f"[{lead}] {reply}")
        task.keep(reply, kind="capture-redeclare", author=lead)
        capture, why = _parse_capture_line(reply, spec.task_id)
        if capture is None:
            self._note(f"task {spec.task_id}: no usable recapture declaration ({why})")
            record["recapture"] = dict(declared=None, problem=why)
            return None
        if capture == current:
            self._note(f"task {spec.task_id}: the recapture declaration repeats the dispatched capture")
            record["recapture"] = dict(declared=capture, problem="same page and steps as dispatched")
            return None
        if not capture["steps"] or capture["steps"][-1].get("action") != "wait":
            # The prompt's own rule: the steps end in a wait on something
            # only the changed state shows. A bare page, or steps with no
            # final wait, is the dispatched problem again (series
            # rule-58a4625 f2 t1: {"path": "/", "steps": []} was accepted,
            # captured and verified because there was no wait to check).
            why = "a recapture must end in a wait on the changed state; a page with no final wait proves nothing"
            self._note(f"task {spec.task_id}: no usable recapture declaration ({why})")
            record["recapture"] = dict(declared=capture, problem=why)
            return None
        missing = _missing_fixtures(self.project, capture, spec.task_id)
        if missing:
            # The declaring call cannot write, so a fixture that is not in the
            # project yet would only fail at capture and spend the one
            # recapture on nothing (Codex review of 2ffa7f6 on #52).
            why = "the declaration uploads a sample that does not exist in the project: " + ", ".join(missing)
            self._note(f"task {spec.task_id}: no usable recapture declaration ({why})")
            record["recapture"] = dict(declared=capture, problem=why)
            return None
        spec.scope = replace(spec.scope, capture=capture)
        # The rows' claimed state follows the accepted declaration before the
        # fresh review: what is captured, what the reviewer is shown and what
        # the row says are one state (root on c9a665b: the rows kept the
        # dispatched "/" while /#q=Alpha was captured and reviewed).
        record.setdefault("declared_capture", dict(current) if current else None)
        self._rebind_capture_rows(spec, record, f"redeclared after {source} reported: {verdict.strip()[:200]}")
        self._note(f"task {spec.task_id}: recapturing {capture['path']} after {len(capture['steps'])} step(s)")
        self._stage("design")
        failure = self._harness_capture(spec)
        if failure:
            self._hand_off_preview(spec, task, record, failure)
            return False, str(failure), [], [dict(kind="integrity", message=str(failure))]
        from .design_evidence import check_records
        ok, problem, shots, records = check_records(self.project, spec.task_id, self._last_edit_started or 0,
                                                    expected_source=self._trusted_source(),
                                                    receipt=self._capture_receipt(spec.task_id, True))
        self._refuse_mismatched(spec, record, task, records, True)
        return ok, problem, shots, records

    def _fix_reviewed_design(self, spec, lead, task, record, reviewer, verdict, blocking):
        """One bounded fix of the reviewer's blocking design problems, then
        the same checks again: the gate, a harness recapture, and the same
        reviewer judging the new renders. Returns ``(verdict, blocking)``
        for the record; the original blockers stand when the fix breaks the
        check or the recapture.

        Overnight r01 (20261011T052348Z-1a2969ca): the reviewer rightly
        blocked a hint that squeezed the search field to 46 px, and the run
        ended FindingsOpen after 7 of 20 calls with no repair. A failed check
        gets a gate-fix and a broken render a design-fix; a reviewer's
        BLOCKING design judgement got nothing, unlike the fix-then-verify
        cycle every other blocking critique gets. One call, no loop: a second
        BLOCKING verdict stays an open finding."""
        lines = [line.strip()[:300] for line in blocking]
        fix = record["review_fix"] = dict(reviewer=reviewer, blocking=lines)
        self._note(f"task {spec.task_id}: the design review blocked; one fix call ({lines[0][:100]})")
        self._count("design_fix")
        if not self._design_fix_edit(spec, task, fix, lead, (
                f"Task: {spec.description}\n\nAn independent reviewer judged the rendered page and found "
                "these blocking design problems:\n" + "\n".join(lines) + "\nFix them in source and keep "
                "every other behaviour and test as it is. Do not start servers or run capture commands: the "
                "harness runs the checks, captures the page again and the same reviewer judges it. "
                + self._revision_delivery() + _design_fix_delivery(self._interim_edits_note())),
                reviewer=reviewer):
            fix["after"] = "the fix was not bought: " + str(fix.get("fix_refused", ""))[:200]
            return verdict, blocking
        self._run_integration_gate(lead, spec, task)
        self._stage("design")
        if self.checks and not self.checks[-1]["passed"]:
            fix["after"] = "the check failed after the fix; the design was not judged again"
            return verdict, blocking
        from .design_evidence import check_records
        failure = self._harness_capture(spec)
        if failure:
            self._hand_off_preview(spec, task, record, failure)
            ok, problem, shots = False, failure, []
        else:
            ok, problem, shots, records = check_records(
                self.project, spec.task_id, self._last_edit_started or 0,
                expected_source=self._trusted_source(), receipt=self._capture_receipt(spec.task_id, True))
            self._refuse_mismatched(spec, record, task, records, True)
        if not ok:
            record.update(verified=False, problem=problem)
            self._design_unverified.append((spec.task_id, problem))
            self._open_finding("invalid_proof",
                               f"Task {spec.task_id} is design work without clean rendered evidence: {problem}.")
            fix["after"] = f"the recapture after the fix is not clean evidence: {problem[:200]}"
            return verdict, blocking
        record.update(verified=True, problem="", screenshots=shots)
        verdict = self._final_design_review(spec, reviewer, shots)
        # APPROVED followed only by the per-row lines the prompt asks for is
        # still an approval; every other reply is read by the one verdict
        # reader.
        blocking = [] if _design_approved(verdict) else _design_blocking(verdict)
        fix["after"] = "approved" if not blocking else "still blocking"
        return verdict, blocking

    def _refuse_mismatched(self, spec, record, task, records, harness) -> None:
        """Stop on evidence positively observed not to hold (map J9b): a
        symlinked screenshot, a fixture whose bytes changed after capture,
        or a harness capture recorded against a source other than the
        trusted one. An integrity stop, not invalid proof: no recapture, no
        design-fix and no product repair, and the evidence stays as found.
        A self-capture on another source is ordinary staleness and keeps
        the bounded recapture (J8, J9a)."""
        seen = [r["message"] for r in records if r.get("kind") == "integrity"
                and (r.get("mismatch") or (harness and r.get("identity") == "source"))]
        if not seen:
            return
        from .design_evidence import EvidenceIdentityMismatch
        problem = "; ".join(seen)
        record.update(verified=False, problem=problem, identity_mismatch=seen, harness_capture=bool(harness))
        self.design_checks.append(record)
        task.keep(json.dumps(record), kind="design-evidence")
        self._note(f"task {spec.task_id}: design evidence did not hold; stopping ({problem[:120]})")
        raise EvidenceIdentityMismatch(f"task {spec.task_id}: {problem[:300]}. The evidence is preserved as found.")

    def _evidence_files(self, spec, shots) -> List[str]:
        """Project-relative evidence files behind ``shots``, for the Fleet to
        copy into a review call's source copy (see runtime._furnish_evidence)."""
        return self._evidence_files_for(spec.task_id)

    def _evidence_files_for(self, task_id) -> List[str]:
        from .design_evidence import evidence_dir
        folder = evidence_dir(self.project, task_id)
        files = [folder / "summary.json"]
        for view in ("desktop", "mobile"):
            files += [folder / view / "page.png", folder / view / "evidence.json"]
        root = Path(self.project)
        return [f.relative_to(root).as_posix() for f in files if f.is_file()]

    def _shown(self, shots) -> List[str]:
        """``shots`` with absolute screenshot paths made project-relative."""
        out = []
        for shot in shots:
            path = Path(shot)
            if path.is_absolute() and self.project and path.is_relative_to(Path(self.project)):
                out.append(path.relative_to(Path(self.project)).as_posix())
            else:
                out.append(shot)
        return out

    def _evidence_refusals(self, spec, files) -> list:
        from .runtime import evidence_refusals
        return evidence_refusals(self.project, files, spec.task_id)

    def _final_design_review(self, spec, reviewer, shots) -> str:
        """The cross-vendor reviewer judges the final renders, not the draft's."""
        # Which reply, if any, the reviewer itself wrote: every return below
        # but the last is the harness's own text and judges nothing.
        self._design_judgment = None
        files = self._evidence_files(spec, shots)
        refused = self._evidence_refusals(spec, files)
        if refused:
            # A reviewer handed an incomplete set would be judging less than
            # the prompt claims; the design stays unverified instead.
            self._review_evidence = []
            self._edge("delivered", False)
            return ("BLOCKING: the renders could not be handed to the reviewer ("
                    + "; ".join(f"{p or 'set'}: {r}" for p, r in refused)[:300] + ")")
        # The identity is taken before the call and the copy is bound to it,
        # so what is approved is what was delivered and what is committed.
        hashes = {}
        for rel in files:
            try:
                hashes[rel] = hashlib.sha256((Path(self.project) / rel).read_bytes()).hexdigest()
            except OSError:
                hashes[rel] = ""
        snapshot = (spec.task_id, *self._capture_state(spec.task_id))
        if any(hashes.get(rel) != digest for rel, digest in _snapshot_files(snapshot).items()):
            self._review_evidence = []
            self._edge("delivered", False)
            return "BLOCKING: the renders changed while they were being prepared for review"
        self._review_evidence, self._review_evidence_hashes, self._review_snapshot = files, hashes, snapshot
        self._review_source = self._source_fingerprint()
        capture_rows = [o for o in self.evidence_obligations
                        if o.get("task") == spec.task_id and o.get("modality") == "capture"]
        # The reviewer judges the rows against the original goal and the
        # authoritative text of the requirements they bind (root on
        # 4fac862: ids alone are no authority); a requirement whose text is
        # not on the ledger leaves the rows unjudgeable here.
        texts = self._requirement_texts_now()
        revisions = self._requirement_revisions()
        needed = sorted({r[0] for o in capture_rows for r in o.get("requirements") or ()})
        self._review_rows = [o["oid"] for o in capture_rows]
        self._review_authority_ok = all(rid in texts and str(texts[rid]).strip() for rid in needed)
        # Exactly what this reviewer is shown, kept immutably on the record:
        # the goal, each covered requirement's text and revision, and each
        # row's scenario. A later judgment or measurement is applied only
        # while the authoritative context still equals it; a prior reply is
        # never relabelled with live row contents (root on a18f2ae).
        self._review_context = dict(
            goal=self.memory.goal.strip(),
            requirements={rid: [int(revisions.get(rid, 1)), str(texts.get(rid, ""))] for rid in needed},
            rows={o["oid"]: dict(scenario=[list(s) for s in o.get("scenario") or []],
                                 requirements=[list(r) for r in o.get("requirements") or ()],
                                 behavior=o.get("behavior")) for o in capture_rows})
        authority = ("\n\n## Goal (verbatim)\n\n" + self.memory.goal.strip()
                     + "\n\n## Covered requirements (authoritative text, revision)\n"
                     + "\n".join(f"{rid} (revision {revisions.get(rid, 1)}): {texts.get(rid, '<text not on the ledger>')}"
                                  for rid in needed)) if capture_rows else ""
        prompt = (
            f"Task: {spec.description}\n\nThese are the final renders of this design work, taken "
            "after its last source change, copied read-only into your working copy at these paths: "
            + ", ".join(self._shown(shots)) + _DESIGN_REVIEW_LENS
            + self._review_diff("this task's whole change, against the source it started from",
                                design=True)
            + ((authority + "\n\n## Declared visual scenarios (evidence capture)" + self._obligations_block(capture_rows)
                + "\n\nFor each capture row above, add exactly one line '<O-id>: SUFFICIENT <what these renders "
                "show for that requirement and scenario>' or '<O-id>: INSUFFICIENT <what they do not show>'. "
                "APPROVED alone judges no row; a row left unjudged keeps its requirement not met; a line "
                "naming any other row is not read.")
               if capture_rows else "")
            + "\n\nFirst check that the renders show the interface this task added or changed. "
            "If they show a page where that interface does not appear, reply exactly "
            "'BLOCKING: the renders do not show the changed interface' and judge nothing else: "
            "a clean render of an unrelated page is not evidence for this task."
            + "\n\nReply APPROVED on its own line if the delivered interface is acceptable, or one line "
            "per blocking problem starting 'BLOCKING:'; then only the per-row lines asked for above, "
            "if any. Nothing else."
        )
        from .runtime import EvidenceNotDelivered, ReviewCopyAltered
        try:
            with invocation(spec.task_id, "design-review"):
                verdict = self._invoke_model(reviewer, prompt + self._review_turn_budget_note(reviewer))
        except TurnLimitReached as exc:
            # The model ran, so the renders reached its copy: delivery is
            # recorded before the capped call is filed (Codex review comment
            # 4226680387 on #53). Neither APPROVED nor a BLOCKING line, and
            # not a design defect: the design review is owed by this reviewer
            # as review debt (recovery lifecycle, 2026-10-31), settled by its
            # own completed design review of this work, never a bare string.
            self._record_delivery(reviewer, hashes)
            self._record_review(spec, reviewer, "design-review", "", complete=False, capped=True)
            rv = self._recovery_open(spec, role="design-review", seat=reviewer, kind="design review",
                                     outcome_class="design_review_capped",
                                     question=f"{reviewer}'s design review of {spec.task_id}",
                                     event=self._call_event(spec.task_id, "design-review", reviewer), exc=exc)
            rv["eligible"], rv["decision"] = ["hold_owned_debt"], "hold_owned_debt"
            self._owe_review(spec, reviewer, "design review", f"stopped at {exc.turns or 'its'} rounds with no "
                             "verdict", role="design-review", record=rv)
            return self._capped_review(spec, reviewer, "design review", exc)
        except EvidenceNotDelivered as exc:
            self._edge("delivered", False)
            return f"BLOCKING: the renders could not be handed to the reviewer ({str(exc)[:300]})"
        except ReviewCopyAltered as exc:
            # Delivered, but judged after the reviewer changed what it was
            # handed: the verdict is not of the bytes on record.
            self._edge("delivered", False)
            return f"BLOCKING: the reviewer altered its copy of the source or renders before judging ({str(exc)[:300]})"
        self._record_delivery(reviewer, hashes)
        self._record_review(spec, reviewer, "design-review", verdict, complete=True)
        self._design_judgment = (reviewer, verdict)
        return verdict

    def _record_delivery(self, reviewer, hashes) -> None:
        """Delivered as bound: the files and hashes the copy was checked against."""
        self._edge("delivered", True)
        if self._outcome is not None:
            self._outcome.delivery = dict(reviewer=reviewer, files=dict(hashes))

    def _design_review_drift(self, record: dict) -> str:
        """Why a delivered design review no longer describes what is on
        disk: the state snapshot, the delivered files' hashes or the source
        differ from what the reviewer was handed. "" when they all hold."""
        task = record.get("task")
        if not self.project or not record.get("snapshot") or record.get("source") is None:
            return "the review carries no measured snapshot"
        if record.get("source") != self._source_fingerprint():
            return "the source moved after the review"
        now = list(self._capture_state(task))
        if now != list(record["snapshot"]):
            return "the renders changed after they were reviewed"
        for rel, digest in (record.get("delivered_hashes") or {}).items():
            try:
                current = hashlib.sha256((Path(self.project) / rel).read_bytes()).hexdigest()
            except OSError:
                return f"a delivered render is missing: {rel}"
            if current != digest:
                return f"a delivered render was replaced: {rel}"
        return ""

    def _design_context_drift(self, record: dict) -> str:
        """Why the context a design reviewer was shown is no longer the
        authoritative one: the goal, a covered requirement's text or
        revision, or a shown row's scenario, requirement revisions or
        behaviour differ now from what the record keeps from before the
        call; or the record keeps no such context. "" when it all holds."""
        shown = record.get("review_context")
        if not isinstance(shown, dict) or "requirements" not in shown or "rows" not in shown:
            return "the review carries no bound requirement and scenario context"
        if str(shown.get("goal", "")) != self.memory.goal.strip():
            return "the goal changed since the review"
        texts = self._requirement_texts_now()
        revisions = self._requirement_revisions()
        for rid, (revision, text) in sorted(shown["requirements"].items()):
            if rid not in texts or not str(text).strip():
                return f"the review lacked {rid}'s authoritative text"
            if int(revisions.get(rid, 1)) != int(revision) or str(texts[rid]) != str(text):
                return f"{rid}'s text or revision changed since the review"
        rows = {o["oid"]: o for o in self.evidence_obligations}
        for oid, kept in sorted(shown["rows"].items()):
            now = rows.get(oid)
            if now is None:
                return f"{oid} is no longer on the record"
            if ([list(s) for s in now.get("scenario") or []] != kept.get("scenario")
                    or [list(r) for r in now.get("requirements") or ()] != kept.get("requirements")
                    or now.get("behavior") != kept.get("behavior")):
                return f"{oid}'s scenario or requirement revisions changed since the review"
        return ""

    def _capture_measurement(self, record: dict) -> Tuple[Optional[str], str]:
        """``(receipt id, "")`` when the task's design evidence measures the
        declared state on the current source now: its latest design record
        is verified, was delivered to a reviewer from another vendor whose
        review returned a verdict, was taken on this exact source, and its
        renders (target, steps, fixture, viewports and digests) are still
        the ones on disk and still pass the identity check. Else
        ``(None, why)``: missing, wrong or stale state or viewport, a
        changed source, fixture or image, a disabled or unavailable
        capture, or an undelivered review all measure nothing. Nothing a
        lead claimed is read here."""
        task = record.get("task")
        checks = [c for c in self.design_checks if c.get("task") == task]
        if not checks:
            return None, "no design evidence recorded for the task"
        latest = checks[-1]
        if latest.get("verified") is not True:
            return None, f"the design evidence is not verified ({str(latest.get('problem') or '')[:120]})"
        snapshot = latest.get("snapshot")
        if not snapshot or latest.get("source") is None:
            return None, "the design record carries no measured snapshot"
        if latest.get("source") != (self._source_fingerprint() if self.project else None):
            return None, "the design evidence was taken on another source"
        review = latest.get("final_review") or {}
        if not review.get("reviewer") or not str(review.get("verdict") or "").strip():
            return None, "the design review was not delivered"
        if str(review.get("verdict", "")).startswith("UNFINISHED"):
            return None, "the design review gave no verdict"
        if not latest.get("delivered_hashes"):
            return None, "the review's delivered renders were not recorded"
        if not latest.get("authority_ok"):
            return None, "the review lacked the covered requirements' authoritative text"
        drift = latest.get("review_stale") or self._design_review_drift(latest) or self._design_context_drift(latest)
        if drift:
            return None, drift
        if record.get("oid") not in (latest.get("review_rows") or ()):
            return None, "this row was not among those delivered to the design reviewer"
        target, steps, evidence = snapshot
        problem, _observed = self._identity_check(task, target, steps, evidence)
        if problem:
            return None, problem
        digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return f"capture:{task}:{digest}", ""

    @staticmethod
    def _capture_context(scope) -> str:
        """The declared visual state a capture scenario is about, as one
        string: the SCOPE capture declaration normalized by the same
        validation dispatch applies (``preview.validate_capture``: the
        route and the ordered steps with every supported field, action,
        selector, the file step's path and the confirm step's message),
        else the lead's own undeclared capture. Nothing is dropped and no
        other schema is invented: a declaration the validator refuses is
        serialized verbatim and marked, so it never collapses onto a valid
        one (root and SAME Astra on a18f2ae)."""
        capture = getattr(scope, "capture", None)
        if not capture:
            return "self-capture (no SCOPE capture declared)"
        from .preview import validate_capture
        try:
            normalized = validate_capture(capture)
        except ValueError as exc:
            return json.dumps(dict(invalid=str(exc)[:200], declared=capture), sort_keys=True, default=str)
        return json.dumps(dict(path=normalized["path"],
                               steps=[dict(sorted((k, v) for k, v in step.items())) for step in normalized["steps"]]),
                          sort_keys=True, default=str)

    def _capture_state(self, task_id):
        """``(target, steps, evidence)`` for a task's renders, from its summary.

        ``steps`` is the ordered interaction as ``[action, selector, fixture
        sha256]``: one URL can show several states (Run 17: the project list,
        an empty dialog and the populated preview all share it), so a finding
        is bound to the state it was measured in, not to the address alone.
        ``evidence`` holds the summary's and each screenshot's sha256.
        """
        from .design_evidence import VIEWPORTS, evidence_dir
        folder = evidence_dir(self.project, task_id)
        root = Path(self.project)

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        try:
            summary = json.loads((folder / "summary.json").read_text())
        except (OSError, ValueError):
            summary = {}
        requested = summary.get("steps") if isinstance(summary, dict) else None
        steps = [[s.get("action"), s.get("selector"), s.get("sha256")]
                 for s in (requested if isinstance(requested, list) else []) if isinstance(s, dict)]
        evidence = dict(summary=(folder / "summary.json").relative_to(root).as_posix(),
                        sha256=digest(folder / "summary.json"),
                        screenshots={name: digest(folder / name / "page.png") for name in VIEWPORTS},
                        # The render record beside each screenshot is part of
                        # what was approved too (Codex review of 2e57e94).
                        records={name: digest(folder / name / "evidence.json") for name in VIEWPORTS})
        return (summary.get("target") if isinstance(summary, dict) else None), steps, evidence

    def _harness_captures(self, spec) -> bool:
        """Whether the harness, not the lead, captures this task's renders."""
        return bool(self.config.capture_profile is not None and is_design_task(spec)
                    and getattr(spec.scope, "capture", None))

    def _live_design_instruction(self, spec) -> str:
        """The lead prompt's design instruction read from live config: the
        value fixed at dispatch and, afterwards, the legacy side of the check."""
        if not (self.config.design_self_verify and is_design_task(spec)):
            return "none"
        return "harness" if self._harness_captures(spec) else "self"

    @staticmethod
    def _profile_digest(profile) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(dataclasses.asdict(profile), sort_keys=True)
                                          .encode()).hexdigest()

    def _bind_capture_profile(self, spec) -> str:
        """Hold an independent copy of the live capture profile for this task
        and return its digest for the contract (O-NEXT-13; Codex 5865903915)."""
        import copy
        profile = copy.deepcopy(self.config.capture_profile)
        self._capture_snapshot = (spec.task_id, profile)
        return self._profile_digest(profile)

    def _dispatch_capture_profile(self, spec):
        """The capture profile bound at dispatch, and "" or why the capture
        must not start. It fails closed: no contract or snapshot for this
        task, or a snapshot that is not the one the contract records, is a
        refusal, never a reconstruction from live config. A live profile that
        was removed or differs is recorded by field name only (no values) and
        the capture stops before the preview starts, rather than run a
        profile the operator changed or fall back to a self-capture."""
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        held = getattr(self, "_capture_snapshot", None)
        if (outcome is None or contract is None or contract.task_id != outcome.task_id
                or contract.task_id != spec.task_id or not contract.capture_profile
                or held is None or held[0] != spec.task_id
                or self._profile_digest(held[1]) != contract.capture_profile):
            return None, "the task's contract holds no capture profile"
        profile, live = held[1], self.config.capture_profile
        if live == profile:
            return profile, ""
        moved = (["profile (removed)"] if live is None else
                 [f.name for f in dataclasses.fields(profile) if getattr(profile, f.name) != getattr(live, f.name)])
        note = f"capture_profile: live differs from contract in {', '.join(moved)}"
        if note not in outcome.mismatches:
            outcome.mismatches.append(note)
        return None, f"the capture profile changed after dispatch ({', '.join(moved)})"

    def _live_capture_page(self, spec) -> Optional[str]:
        """The page a harness instruction names, from the live profile."""
        profile = self.config.capture_profile
        if profile is None or not getattr(spec.scope, "capture", None):
            return None
        return profile.origin + spec.scope.capture["path"]

    def _design_instruction(self, spec):
        """The lead's design instruction and, for "harness", the page, from
        the contract fixed at dispatch (map P3.4), live readings recorded
        beside it. Outside a task the live readings decide, as before."""
        instruction = self._required("design_instruction", self._live_design_instruction(spec))
        if instruction != "harness":
            return instruction, None
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            return instruction, self._live_capture_page(spec)
        live = self._live_capture_page(spec)
        note = f"capture_page: contract {contract.capture_page!r}, legacy {live!r}"
        if live != contract.capture_page and note not in outcome.mismatches:
            outcome.mismatches.append(note)
        return instruction, contract.capture_page

    def _capture_guidance(self) -> bool:
        """Whether the orchestrator is held to a capture block, and so is told its rules."""
        return bool(self.project and self.config.allow_writes and self.config.capture_profile is not None
                    and self.config.design_self_verify)

    def _capture_problem(self, spec, resolves) -> str:
        """Why a UI task cannot be dispatched as declared, before any lead
        call; "" if it can. Raises CapabilityUnavailable when the lead could
        never produce the evidence the harness will require."""
        if not (self.config.design_self_verify and self.project and self.config.allow_writes
                and is_design_task(spec)):
            return ""
        if _frontend_without_rendered_file(spec) and spec.task_id not in self._frontend_challenged:
            # Once per task: a deliberate frontend label survives being named
            # again and is then held to the capture requirement as before.
            self._frontend_challenged.add(spec.task_id)
            return _FRONTEND_WITHOUT_RENDER
        profile = self.config.capture_profile
        if profile is not None:
            capture = getattr(spec.scope, "capture", None)
            if not capture:
                return ('A UI task must declare what the harness captures. ' + _CAPTURE_SCOPE_REQUEST)
            # A committed project sample or this task's own capture-only
            # fixture; the capture itself (design_evidence._fixture) holds the
            # same rule, and the lead's instructions already offer both. The
            # phase-4 rerun on a6c9576 stalled on the sample task 2 had just
            # committed under tests/fixtures/.
            own = f".quadratus/capture-fixtures/{spec.task_id}/"
            for step in capture["steps"]:
                if step["action"] != "file":
                    continue
                path = step["path"]
                if path.startswith(own) and "/" not in path[len(own):]:
                    continue
                if any(part.startswith(".") for part in path.split("/")):
                    return (f"A harness capture uploads a committed project file or this task's own "
                            f"fixture: {path} is hidden; put a capture-only sample at {own}<name>.")
            target = profile.origin + capture["path"]
            steps = [[s["action"], s["selector"]] for s in capture["steps"]]
            for finding in self.findings:
                if finding["id"] in (resolves or ()) and finding.get("target") and (
                        target != finding["target"] or steps != [s[:2] for s in finding["steps"]]):
                    return (f"The declared capture ({target}, {len(steps)} steps) is not the state "
                            f"{finding['id']} was measured in ({finding['target']}, "
                            f"{len(finding['steps'])} steps); declare the same page and steps.")
            return ""
        can_run = self.config.lead_can_run
        if can_run is None:
            return ""
        lead = self._pick_lead(spec)
        self._dispatch_lead = (spec.task_id, lead)
        if can_run is not None and not can_run(lead, "python -m quadratus.design_evidence"):
            raise CapabilityUnavailable(
                f"task {spec.task_id} is UI work whose renders the harness requires, and its lead "
                f"{lead} cannot run the capture on its transport; declare a capture profile "
                "(--capture-profile) so the harness captures, or run this task on a seat that can. "
                "No call was made to its lead.")
        return ""

    def _supply_fixtures(self, spec, lead, task, record, missing) -> List[str]:
        """One bounded round: the lead replies with the content of each
        missing capture-only sample and the harness writes it under the
        task's own .quadratus/capture-fixtures/<task id>/ folder. No write
        grant changes; what the lead cannot write, it dictates."""
        from .design_evidence import MAX_FIXTURE_BYTES
        self._count("fixture_supply")
        self._note(f"task {spec.task_id}: declared capture fixture(s) missing; one supply round "
                   f"({', '.join(missing)[:120]})")
        prompt = (
            f"Task: {spec.description}\n\nThe harness cannot capture this task yet: the capture declares "
            "an upload of " + ", ".join(missing) + ", and no such file exists. The repository policy "
            "refuses writes under .quadratus/, so the harness writes it from what you reply. For each "
            "path, reply with the line\nFIXTURE <path>:\nfollowed by one fenced block holding the "
            "complete file content, a valid non-secret sample of what that input accepts (at most "
            f"{MAX_FIXTURE_BYTES:,} bytes). " + _ONE_FIXTURE_FENCE + " Reply with nothing else. Do not "
            "change files: this call supplies content, it does not edit."
        )
        try:
            with invocation(spec.task_id, "fixture-supply"):
                reply = self._invoke_model(lead, prompt)
        except TurnLimitReached as exc:
            why = self._capped_declaration(spec, lead, task, "fixture-supply", exc)
            record["fixtures_written"] = []
            record["fixture_problems"] = [f"{path}: {why}" for path in missing]
            return list(record["fixture_problems"])
        task.record("assistant", f"[{lead}] {reply}")
        task.keep(reply, kind="fixture-supply", author=lead)
        supplied, problems = _parse_fixture_blocks(reply)
        written = []
        flagged = {p.split(":", 1)[0] for p in problems}
        with self._operation("fixture supply", inputs=dict(task=spec.task_id, paths=list(missing)),
                             effects=("project-write",)) as op:
            for path in missing:
                if path in flagged:
                    continue
                content = supplied.get(path)
                if content is None:
                    problems.append(f"{path}: no FIXTURE block in the reply")
                    continue
                data = content.encode("utf-8")
                if len(data) > MAX_FIXTURE_BYTES:
                    problems.append(f"{path}: {len(data):,} bytes exceeds {MAX_FIXTURE_BYTES:,}")
                    continue
                target, why = _confined_fixture_target(self.project, path, spec.task_id)
                if target is None:
                    problems.append(why)
                    continue
                why = _write_fixture_bound(self.project, path, data)
                if why:
                    problems.append(f"{path}: {why}")
                    continue
                written.append(path)
            op["result"] = dict(written=written, problems=problems)
        record["fixtures_written"] = written
        if problems:
            record["fixture_problems"] = problems
            self._note(f"task {spec.task_id}: fixture supply incomplete ({'; '.join(problems)[:160]})")
        return problems

    def _capped_declaration(self, spec, lead, task, what: str, exc: TurnLimitReached) -> str:
        """A bounded declaration call that stopped at its round cap made no
        declaration. The partial text is narration of unfinished work and is
        kept as evidence, never read for a CAPTURE line or a FIXTURE block;
        the run goes on exactly as after a reply with no marker."""
        rounds = f"{exc.turns} rounds" if exc.turns else "its round cap"
        why = f"the {what} call stopped at {rounds} before answering; no declaration"
        task.record("user", f"The {what} call stopped at {rounds}; nothing it wrote is read as a declaration.")
        if exc.partial_text:
            task.keep(exc.partial_text, kind=f"{what}-capped", author=lead)
        self._note(f"task {spec.task_id}: {why}")
        return why

    def _missing_own_fixtures(self, spec) -> List[str]:
        """The capture-only samples this task's file steps declare under its
        own ``.quadratus/capture-fixtures/<task id>/`` that are not regular
        files in the project. A committed sample elsewhere is checked by the
        capture itself, as before."""
        steps = (getattr(getattr(spec, "scope", None), "capture", None) or {}).get("steps") or []
        own = f".quadratus/capture-fixtures/{spec.task_id}/"
        missing = []
        for step in steps:
            path = str(step.get("path", "")) if step.get("action") == "file" else ""
            if not (path.startswith(own) and self.project):
                continue
            target = Path(self.project) / path
            # An empty own fixture is a discarded partial write (see
            # _write_fixture_bound), never a sample: it counts as missing.
            if not target.is_file() or target.is_symlink() or target.stat().st_size == 0:
                missing.append(path)
        return missing

    def _harness_capture(self, spec) -> str:
        """Run the operator's preview and capture this task's declared state;
        "" on success or why not. Only after the last gate passed, and the
        source must be the same before and after (the preview is not a
        writer)."""
        # A new attempt retires the previous receipt before anything can
        # stop it: a failed, ineligible or unprofiled attempt leaves no
        # measurement that an older render could be accepted against.
        self._harness_tasks.add(spec.task_id)
        self._capture_receipts.pop(spec.task_id, None)
        ineligible = self._capture_ineligible()
        if ineligible:
            return f"the harness did not capture because {ineligible}"
        profile, problem = self._dispatch_capture_profile(spec)
        if problem:
            return f"the harness did not capture because {problem}"
        from .preview import capture_task
        self._verify_dependencies(f"before preview ({spec.task_id})")
        before = self._source_fingerprint()
        receipt: dict = {}
        with self._operation("capture", inputs=dict(task=spec.task_id, source=before,
                                                    capture=spec.scope.capture),
                             effects=("preview", "capture")) as op:
            failure = capture_task(profile, self.project, spec.task_id, spec.scope.capture, receipt=receipt)
            op["result"] = dict(failure=failure, receipt=receipt)
        self._verify_dependencies(f"during preview ({spec.task_id})")
        if before is None or self._source_fingerprint() != before:
            return "the project source changed while the harness previewed and captured it"
        if not failure:
            self._capture_receipts[spec.task_id] = receipt
        return failure

    def _capture_receipt(self, task_id, harness) -> Optional[dict]:
        """The harness's measurement of a task's capture, for the check to
        hold the summary to. None only for a self-capture, which has none;
        a harness task with no receipt gets an empty one, which refuses
        every view as unmeasured (an absent receipt was weaker than an
        empty one: Codex review of 2e57e94)."""
        return self._capture_receipts.get(task_id, {}) if harness else None

    def _hand_off_preview(self, spec, task, record, failure) -> None:
        """Stop as an operator handoff when the capture failure's origin is
        proven to be the environment; otherwise return and the existing
        unverified route applies. The failure text is kept as it was."""
        from .preview import ENVIRONMENT
        if getattr(failure, "origin", None) != ENVIRONMENT:
            return
        record.update(verified=False, problem=str(failure), harness_capture=True)
        self.design_checks.append(record)
        task.keep(json.dumps(record), kind="design-evidence")
        if self._outcome is not None:
            self._outcome.edge("evidence", False)
        task.record("user", "The preview failed before the project was served; handed to the operator "
                            "without a repair call.")
        raise PreviewUnavailable(f"task {spec.task_id}: {failure}. No repair call was made. Work preserved.")

    def _capture_ineligible(self) -> str:
        """Why this task's renders may not be captured yet; "" if they may.

        Read from the current task's own record under its fixed contract
        (phase 3, map G4), never from the run's last check: a task with no
        required checks does not inherit an earlier task's failure, and a
        task whose required checks never ran does not borrow an earlier
        task's pass. Only an attempt of the task's full required gate counts;
        a cheap subset or another gate never grants eligibility. The passing
        attempt must have run against the source being captured."""
        outcome = self._outcome
        if outcome is None or not outcome.contract:
            return "the task has no dispatch record"
        if not (outcome.contract.get("required") or {}).get("checks"):
            return ""
        full = [c for c in outcome.checks if c.get("gate") == "full"]
        if not full:
            return "the task's required checks have not run"
        last = full[-1]
        if not last.get("passed"):
            return "the task's last required check failed"
        current = self._source_identity()
        if current in ("n/a", "unavailable") or last.get("source") != current:
            return "the source changed after the task's last passing required check"
        return ""

    def _evidence_set_problem(self, task_id) -> str:
        """Why a task's renders are not a complete, deliverable evidence set
        (both screenshots and the summary, within every copy bound); "" if
        they are. Checked before any file is hashed or a finding is made."""
        files = self._evidence_files_for(task_id)
        from .design_evidence import VIEWPORTS, evidence_dir
        folder = evidence_dir(self.project, task_id).relative_to(Path(self.project)).as_posix()
        need = [f"{folder}/summary.json"] + [f"{folder}/{name}/page.png" for name in VIEWPORTS]
        missing = [n for n in need if n not in files]
        if missing:
            return f"the evidence set is incomplete (missing {', '.join(missing)})"
        from .runtime import evidence_refusals
        refused = evidence_refusals(self.project, files, task_id)
        if refused:
            return "the evidence set could not be delivered (" + "; ".join(
                f"{p or 'set'}: {r}" for p, r in refused)[:240] + ")"
        return ""

    def _qualify_debt(self, spec, ok, problem, records):
        """An overflow-only audit whose evidence set is not complete and
        deliverable becomes an integrity stop, not debt: debt is created only
        from evidence a reviewer could be handed (Codex review of e47c7ed)."""
        if ok or not records or not all(r.get("kind") == "product.overflow" for r in records):
            return problem, records
        refusal = self._evidence_set_problem(spec.task_id)
        if not refusal:
            return problem, records
        return f"{problem}; {refusal}", records + [dict(kind="integrity", message=refusal)]

    def _design_debt(self, stopping=None) -> list:
        """``(task_id, problem)`` for the stopping tasks' unverified design
        evidence, read from their typed ``evidence`` records (map P3.4): a
        design check that ended ``verified=False``. Audit debt (a record
        carrying ``findings``) is requirement debt, not this. The legacy
        ``_design_unverified`` list is compared; a disagreement is recorded
        on the task, and either one names the stop."""
        typed = [(o.task_id, o.evidence.get("problem")) for o in self.task_outcomes
                 if (stopping is None or o.task_id in stopping) and isinstance(o.evidence, dict)
                 and o.evidence.get("verified") is False and "findings" not in o.evidence]
        legacy = [d for d in self._design_unverified if stopping is None or d[0] in stopping]
        # Each task's latest problem on both sides, and the fact that names
        # the stop, are compared (Codex, 5862205507), not only the task ids.
        typed_last, legacy_last = dict(typed), dict(legacy)
        for outcome in self.task_outcomes:
            tid = outcome.task_id
            if (tid in typed_last) != (tid in legacy_last):
                note = f"design debt: typed {tid in typed_last}, legacy {tid in legacy_last}"
            elif tid in typed_last and typed_last[tid] != legacy_last[tid]:
                note = (f"design debt: typed problem {str(typed_last[tid])[:120]!r}, "
                        f"legacy problem {str(legacy_last[tid])[:120]!r}")
            elif typed and legacy and typed[-1] != legacy[-1] and tid == typed[-1][0]:
                note = f"design debt: typed names task {typed[-1][0]}, legacy names task {legacy[-1][0]}"
            else:
                continue
            if note not in outcome.mismatches:
                outcome.mismatches.append(note)
        return typed or legacy

    def _design_state_for_record(self, spec, current) -> str:
        """Design evidence as the harness holds it for this task, for the
        record (root's review of 8bc9805): required or not from the task's
        contract where one is fixed, else the live reading; a required
        record that is missing is missing, a disabled one is disabled, and
        an approval on another source is historical, never current."""
        contract = getattr(self, "_contract", None)
        required = getattr(contract, "required", None) if getattr(contract, "task_id", None) == spec.task_id else None
        if required is not None:
            applicable = bool(getattr(required, "design_collaboration_applicable", False)
                              or getattr(required, "design_evidence", None) in ("harness", "self")
                              or getattr(required, "design_review", False))
        else:
            applicable = bool(self._live_collaboration(spec))
        record = (self.design_checks[-1] if self.design_checks
                  and self.design_checks[-1].get("task") == spec.task_id else None)
        if record is None:
            if not applicable:
                return "no design evidence required for this task"
            if not self.config.design_cross_check:
                return "design evidence applies to this task but design cross-checking is disabled; no record"
            return "design evidence required for this task and MISSING: no design record was produced"
        approved = bool(record.get("verified")) and (record.get("final_review") or {}).get("approved") is True
        source = record.get("source")
        identity = (f"source {str(source)[:12]}" if source else "source not recorded")
        for key in ("context", "render", "renders"):
            if record.get(key):
                identity += f", {key} {str(record.get(key))[:40]}"
        if not approved:
            return (f"design evidence not verified ({identity}): "
                    f"{str(record.get('problem') or 'no approval recorded')[:160]}")
        if source and current and source == current:
            drift = (record.get("review_stale") or self._design_review_drift(record)
                     or self._design_context_drift(record))
            if drift:
                return (f"design approval recorded on this source ({identity}), but its current "
                        f"render/context validity is unverified: {str(drift)[:160]}")
            return f"design evidence verified and reviewed on this source ({identity})"
        if source and current:
            return (f"design evidence verified and reviewed on an EARLIER source ({identity}; current "
                    f"{current[:12]}): historical, not evidence for the current source")
        return f"design evidence verified and reviewed, but its source identity is not recorded ({identity})"
