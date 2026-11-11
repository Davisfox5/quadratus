"""Check accounting for the session engine, as a mixin of Session: the
integration gate and what its receipts establish, skipped and NOT RUN
acceptance, unnamed test files, and run-start tests checked in their
run-start form.

The methods moved out of session.py unchanged on 2026-11-11. They reach
the rest of Session only through ``self``; the plain readers they call are
in check_facts.py. This module never imports quadratus.session.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from .check_facts import (
    _audit_check_facts,
    _case_block,
    _case_files,
    _check_runs,
    _check_source,
    _complete_run,
    _configured_checks,
    _describe_tree_change,
    _gate_identity,
    _item_paths,
    _named_outside_comments,
    _new_skips,
    _receipt_names,
    _verified_by_check,
)
from .memory import TaskMemory
from .providers import TurnLimitReached
from .reply_parsers import _NOT_RUN_LINE
from .session_prompts import _NO_REQUESTS_IN_FIX, _NOT_RUN_REMEDY_LINES, _revision_changed_delivery
from .session_types import TaskSpec, is_design_task, is_review_only
from .task_kinds import TaskKind
from .verdicts import review_reading as _review_reading


class ChecksMixin:
    """What the project's own checks did and what that establishes.

    Session inherits this; nothing here is used on its own.
    """

    def _current_gate(self):
        """The gate the current task runs: its bound gate, else the configured one."""
        return self._bound_gate() or self.config.integration_gate

    def _gate_for(self, spec):
        """The gate ``spec`` would run if dispatched now, read without side
        effects (the same policy resolution _prepare_dispatch makes); None
        when the policy refuses it."""
        policy = self.config.repository_policy
        if policy is None:
            return self.config.integration_gate
        from .policy import PolicyError, task_gate
        try:
            paths = spec.scope.permitted_paths if spec.scope else ()
            plan = policy.resolve(paths, writing=self.config.allow_writes)
            # Every refusal _prepare_dispatch makes is no gate here (SAME
            # review of f5c04b5: a blocked plan resolves without raising).
            if plan["blocked"] or (self.config.allow_writes and self.config.default_scope
                                   and any(not self.config.default_scope.permits(x) for x in paths)):
                return None
            return task_gate(policy, plan, self._original_gate, exclude=self.config.project_excludes)
        except (PolicyError, ValueError):
            return None

    def _observe_checks(self, spec, stage: str) -> None:
        """Run the task's own gate on the current source, once per source, as
        evidence for the seats that judge it: no fix call, never a task check,
        so it decides nothing about the task (r08, 20261011T203457Z-eaf86c90:
        reviewers' source copies have no dependency tree, so their runs of the
        project's browser tests failed with MODULE_NOT_FOUND while the same
        tests passed in the project, and the revisions looped on it).

        Skipped when the task requires no checks, when there is no gate, or
        when a check or an earlier observation already ran on this source."""
        live = self.config.integration_gate
        if not self.project or live is None or not self._required("checks", live is not None):
            return
        gate = self._current_gate()
        current = self._source_fingerprint()
        identity = _gate_identity(gate)
        # Reused only for the same source AND the same gate: a cheap subset,
        # another task's gate or a check of an earlier source is not this run.
        if not current or any(c.get("source") == current and c.get("gate") == identity
                              for c in self.review_checks):
            return
        from .project import Project
        project = Project(self.project, exclude=self.config.project_excludes)
        self._verify_dependencies(f"before observed check ({spec.task_id})")
        before = project.contents()
        self._activity("check-started", task=spec.task_id)
        try:
            result = gate.run()
        finally:
            self._activity("check-ended", task=spec.task_id)
        self._verify_dependencies(f"during observed check ({spec.task_id})")
        moved = project.contents() != before
        from .integration import without_names
        self.review_checks.append({
            "task": spec.task_id, "stage": stage, "gate": identity, "passed": bool(result.passed) and not moved,
            # An observation is read for its counts, never for the names of
            # the cases that passed, so it does not keep them.
            "command": result.command, "report": without_names(result.report),
            # A run the tree moved under shows nothing about any one source.
            "source": None if moved else current,
            "receipts": [dict(dataclasses.asdict(replace(r, report=without_names(r.report))),
                              output=(r.output or "")[-4000:]) for r in result.receipts],
        })
        self._note(f"task {spec.task_id}: harness ran the checks on the source before {stage}: "
                   + ("passed" if result.passed and not moved else "did not pass"))

    def _harness_facts(self, *, current_only=False) -> str:
        """The latest project check and renders as harness records: per
        command its status, counts and source; the renders' review and
        source. Never a command line or an unredacted reason. With
        ``current_only``, "" unless that check ran on the current source."""
        observed = getattr(self, "review_checks", [])
        if not self.project or not (self.checks or observed):
            return ""
        current = self._source_fingerprint()
        # Bound to the gate in force now as well as the source: a run of
        # another gate on these bytes is history, never this task's receipt.
        identity = _gate_identity(self._current_gate())
        canonical = (bool(self.checks) and bool(current) and self.checks[-1].get("source") == current
                     and self.checks[-1].get("gate") == identity)
        seen = next((c for c in reversed(observed)
                     if current and c.get("source") == current and c.get("gate") == identity), None)
        if current_only and not canonical and seen is None:
            return ""
        lines = []
        if self.checks and (canonical or not current_only):
            lines.append(_audit_check_facts(self.checks[-1:], current))
        if seen is not None and not canonical:
            lines.append(f"- harness run of the task's checks on this exact source before {seen['stage']} "
                         f"({seen['task']}; evidence only, not the task's check):")
            lines.append(_audit_check_facts([seen], current))
        if self.design_checks:
            record = self.design_checks[-1]
            from .design_evidence import evidence_dir
            try:
                summary = json.loads((evidence_dir(self.project, record.get("task")) / "summary.json").read_text())
            except (OSError, ValueError, TypeError):
                summary = {}
            source = summary.get("source_fingerprint") if isinstance(summary, dict) else None
            review = record.get("final_review")
            lines.append(f"- renders from {record.get('task')}: "
                         + ("verified" if record.get("verified") else "NOT verified")
                         + (f"; design review by {review.get('reviewer')}: {_review_reading(review)}" if review
                            else "; no design review verdict")
                         + f"; {_check_source(source, current)}")
        return "\n".join(lines)

    def _configured_checks_note(self) -> str:
        """The configured checks as the one source of executable verification
        (step 1, 2026-10-13: a pilot plan prescribed ``python3 -m unittest
        discover -v`` while the configured runner was pytest, and audits kept
        requiring the command nobody here runs). Runner names only."""
        listed = _configured_checks(self.config.integration_gate) if self.project else ""
        if not listed:
            return ""
        return ("\n\n## Configured project checks (the only commands the harness executes)\n" + listed
                + "\nPlan verification these checks can show: tests their runner collects. Do not invent "
                "a test command the goal does not name; a seat cannot be held to a command the harness "
                "does not run. If the goal itself requires a command these checks do not run, do not "
                "drop it and do not prescribe it: ASK, naming the command and the configured checks as "
                "a configuration conflict.")

    def _verification_note(self) -> str:
        """The orchestrator's view of the harness's own verification."""
        configured = self._configured_checks_note()
        facts = self._harness_facts()
        if not facts:
            return configured
        head = (configured + "\n\n## The harness's latest check and renders (its own runs, not a model's account)\n"
                + facts)
        current = self._source_fingerprint()
        canonical = (bool(self.checks) and bool(current) and self.checks[-1].get("source") == current
                     and self.checks[-1].get("gate") == _gate_identity(self.config.integration_gate))
        if not self.config.requirements_ledger or self._explicit_tasks is not None or not canonical:
            return head
        return (head + "\nA task that only verifies (SCOPE edits none) is not dispatched while these "
                "show the current source passing on every command with nothing skipped, its renders "
                "approved and nothing open: cover such requirements in the task that does the work, or "
                "reply DONE and the independent requirement audit judges them.")

    def _verification_problem(self, spec=None) -> str:
        """Why the current source is not fully verified by the harness's own
        records; "" when it is. Fully verified: the latest project check
        passed on this exact source with every command executing cases and
        none failing or skipping; no check failure, open finding, capped
        task, NOT RUN report or unrun test file stands; and, where this run
        has design work, its latest renders are clean, approved by the
        cross-vendor reviewer, and captured from this exact source."""
        if not self.project or not self.history:
            return "no task has delivered work on this project yet"
        current = self._source_fingerprint()
        why = _verified_by_check(self.checks[-1] if self.checks else None, current)
        if why:
            return why
        if spec is not None:
            gate = self._gate_for(spec)
            if gate is None and self.config.repository_policy is not None:
                # Dispatched, so the policy refuses it exactly as it would any task.
                return "the repository policy refuses this task"
            if self.checks[-1].get("gate") != _gate_identity(gate):
                return "the latest project check ran a different gate than this task's"
        if self._checks_standing_failed():
            return "a check failure still stands"
        open_work = self._open_work()
        if open_work:
            return open_work[0]
        if self._standing_not_run():
            return "a task reported acceptance it did not run"
        if self._unnamed_standing():
            return "test files no required check names have not been shown to run"
        if self.design_checks or (spec is not None and is_design_task(spec)):
            if not self.design_checks:
                return "no renders of this source were captured and reviewed"
            record = self.design_checks[-1]
            review = record.get("final_review") or {}
            if not record.get("verified"):
                return f"the latest renders ({record.get('task')}) are not verified"
            if not review.get("approved"):
                return f"the latest renders ({record.get('task')}) are not approved by the design reviewer"
            # The files themselves, not the saved flags (root review of
            # 95b81c2): the set must still be complete and deliverable, and
            # every file the reviewer was handed must hold the approved bytes.
            task = record.get("task")
            outcome = next((o for o in reversed(self.task_outcomes) if o.task_id == task), None)
            delivered = ((outcome.delivery or {}).get("files") or {}) if outcome is not None else {}
            if outcome is None or outcome.edges.get("reviewer") is not True or not delivered:
                return f"the latest renders ({task}) have no approved delivery on record"
            problem = self._evidence_set_problem(task)
            if problem:
                return f"the latest renders ({task}): {problem}"
            for rel, digest in sorted(delivered.items()):
                try:
                    now = hashlib.sha256((Path(self.project) / rel).read_bytes()).hexdigest()
                except OSError:
                    now = ""
                if not digest or now != digest:
                    return f"the latest renders ({task}) changed after their review: {rel}"
            from .design_evidence import evidence_dir
            try:
                summary = json.loads((evidence_dir(self.project, record.get("task")) / "summary.json").read_text())
            except (OSError, ValueError, TypeError):
                return "the latest renders' summary cannot be read"
            if not isinstance(summary, dict) or summary.get("source_fingerprint") != current:
                return "the latest renders were captured from an earlier source"
        return ""

    def _verification_handoff(self, spec, covers, resolves, continues) -> Optional[str]:
        """The send-back for a verification-only task the harness has already
        done, or None to dispatch the task as named.

        Only an orchestrator's review-only task (SCOPE edits none) that resolves no finding,
        continues no capped task and covers no requirement an audit found
        not met, over a fully verified source (_verification_problem). Its
        requirements are marked covered by the harness evidence, never met:
        the final audit still judges each one, with the same receipts and
        renders in hand. Anything short of full verification dispatches the
        task exactly as before."""
        if (self._explicit_tasks is not None or not is_review_only(spec) or resolves or continues
                or getattr(spec, "kind", None) == TaskKind.SECURITY):
            # An operator's own list is run as written: there is nobody to
            # send a task back to.
            return None
        if not self.config.requirements_ledger:
            # No final requirements audit runs, so nothing would judge what a
            # handoff covers (root review of 95b81c2): dispatch as named.
            self.verification_dispatched.append(dict(task=spec.task_id, covers=list(covers),
                                                     why="the requirements audit is disabled"))
            return None
        status = self.memory.ledger.requirement_status
        if any(str(status.get(rid, "")).startswith("NOT MET") for rid in covers):
            return None
        problem = self._verification_problem(spec)
        if problem:
            self.verification_dispatched.append(dict(task=spec.task_id, covers=list(covers), why=problem[:300]))
            self._note(f"{spec.task_id} dispatched as named: {problem}")
            return None
        current = self._source_fingerprint() or ""
        facts = _audit_check_facts(self.checks[-1:], current)
        design = self.design_checks[-1] if self.design_checks else None
        for rid in covers:
            if rid in self.memory.ledger.requirements:
                status[rid] = f"covered by harness verification of {current[:12]}"
        record = dict(task=spec.task_id, description=spec.description[:300], covers=list(covers),
                      source=current, check=facts,
                      renders=(dict(task=design.get("task"), reviewer=(design.get("final_review") or {}).get("reviewer"))
                               if design else None))
        self.verification_handoffs.append(record)
        self._note(f"{spec.task_id} not dispatched: the harness already verified source {current[:12]}; "
                   f"{', '.join(covers) or 'nothing'} wait for the final audit")
        self._activity("verification_handoff", task=spec.task_id, covers=list(covers), source=current[:12])
        return ("\n\n--- TASK NOT DISPATCHED ---\nThat task only verifies, and the harness has already "
                "verified the delivered source with its own runs:\n" + facts
                + (f"\n- renders from {design.get('task')}, captured from this source, approved by "
                   f"{(design.get('final_review') or {}).get('reviewer')}" if design else "")
                + f"\n{', '.join(covers)} are covered by that evidence. They are not met until the final "
                "requirement audit judges them, independently. Reply DONE to have it judge them, or name "
                "work that changes the source.")

    def _record_not_run(self, key, role, reply) -> None:
        """Keep each 'NOT RUN:' line an editing call wrote, with the
        requirements its task covers (series rule-119c83f f2: t3 and t4 said
        their browser scenario and search commands did not run, and the audit
        still found all five requirements met)."""
        task = getattr(self._active_spec, "task_id", "run")
        for match in _NOT_RUN_LINE.finditer(reply or ""):
            item, _, why = match.group(1).partition(" - ")
            item = item.strip().strip("`")
            if not item or item.lower() in ("none", "nothing", "n/a"):
                continue
            entry = dict(task=task, role=role, author=key, item=item[:300], why=why.strip()[:300],
                         requirements=list(self._current_covers), after_check=len(self.checks))
            # A report after a check is a new report: an earlier run cannot
            # discharge what was said about the source since (F3: a re-report
            # in revision was deduplicated away and the old run lifted it).
            if not any(e["task"] == task and e["item"] == entry["item"] and e["after_check"] == entry["after_check"]
                       for e in self.unexecuted_acceptance):
                self.unexecuted_acceptance.append(entry)
                self._note(f"task {task}: {role} reported NOT RUN: {entry['item'][:120]}")

    def _note_skips(self, spec, result, gate=None) -> None:
        """A passing required command that skipped cases the run-start source
        did not skip is acceptance that did not run for the task's
        requirements (gui-sort-v5 t4: Node 37 passed, 2 browser cases skipped).

        What counts is the run's own effect, measured against the same
        commands run once on the run-start source (``_skip_baseline``), not
        whether test files changed: a product edit can turn a case into a skip
        (Codex review of 81adcc7, R3), and a case skipped since before the run
        is not this task's missing acceptance (R4). A skip is historical only
        when its name is one case in both runs, read from the whole output,
        and that case was skipped at run start (S1: two cases sharing a name,
        or names lost from the output tail, hid a new skip). Anything else
        counts, so an unrelated old case that now runs cannot hide a new skip
        (F4). Without names, or without a baseline, every skip counts. The first check counts like any
        other; optional commands and the run-start compatibility receipts
        (``original-tests:*``, immutable by design) never record one."""

        from .integration import GateReceipt, skipped_count
        receipts = result.receipts or (GateReceipt(id="check", status="passed" if result.passed else "failed",
                                                   reason="", required=True, output=result.output,
                                                   report=result.report, cases=result.cases),)
        starts = getattr(self, "_start_skips", None) or {}
        keys = self._command_keys(gate) if gate is not None else {}
        texts, stable = self._case_texts()
        for receipt in receipts:
            if receipt.status != "passed" or not receipt.required or receipt.id.startswith("original-tests:"):
                continue
            count = skipped_count(receipt)
            if not count:
                continue
            names, every, _ = _receipt_names(receipt)
            cases = sorted(names.elements()) if names is not None else None
            start = starts.get(keys.get(receipt.id))
            record = receipt.cases if isinstance(receipt.cases, dict) else {}
            qualified = set(record.get("qualified") or ())
            if names is not None:
                # Historical only when the name is one case in both runs and
                # that case was skipped at run start: a name two cases share,
                # or one that ran there, says nothing about which skipped now
                # (Codex review of 67c9fad, S1). Without complete identity on
                # both sides every skip counts (d157378, T2).
                new = _new_skips(names, every, start, lambda n, q=frozenset(qualified): stable(n, n in q))
                if not new:
                    continue
                cases = sorted(new.elements())
            if any(e.get("kind") == "skipped" and e["task"] == spec.task_id and e["receipt"] == receipt.id
                   and e["after_check"] == len(self.checks) for e in self.unexecuted_acceptance):
                continue
            shown = f"{len(cases)} skipped test case(s) not skipped at run start: {', '.join(cases)}" if (
                cases and start is not None) else f"{count} skipped test case(s)" + (
                f": {', '.join(cases)}" if cases else "")
            entry = dict(task=spec.task_id, role="check", author="harness",
                         item=f"{receipt.id}: {shown}"[:300],
                         why="the required check passed with these cases skipped",
                         requirements=list(self._current_covers), after_check=len(self.checks),
                         receipt=receipt.id, kind="skipped", cases=cases,
                         # Which test file held each named case when it was
                         # recorded, so a later discharge can tell the same
                         # case from a name reused elsewhere (U1).
                         case_files=None if not cases else {
                             n: sorted(_case_files(n, texts, n in qualified) or ()) for n in set(cases)},
                         qualified_cases=sorted(n for n in set(cases or ()) if n in qualified),
                         # The run-start baseline this fact was measured
                         # against, kept so a later discharge reads the same
                         # one (T4).
                         baseline=None if start is None else dict(
                             skipped=None if start[1] is None else dict(start[1]),
                             every=None if start[2] is None else dict(start[2])))
            self.unexecuted_acceptance.append(entry)
            self._note(f"task {spec.task_id}: {entry['item']}")

    def _case_texts(self):
        """``(texts, stable)``: the current test and support files' text, and
        a predicate ``stable(key, qualified)`` for a case whose one holding
        file is the same file as at run start, with the case's own definition
        (``_case_block``) unchanged there (_new_skips). Other tests added to
        or changed in that file do not matter (Codex review of 5292fc2, V2)."""
        from .integration import is_test_support
        from .project import Project
        originals = self._original_tests or {}
        try:
            contents = Project(self.project, exclude=self.config.project_excludes).contents() if self.project else {}
        except Exception:  # noqa: BLE001 -- unreadable: nothing is stable
            # The same two arguments as ``stable`` below: callers pass both,
            # and a one-argument stand-in raised TypeError inside the gate.
            return {}, lambda key, qualified=False: False
        texts = {n: d.decode("utf-8", "replace") for n, d in contents.items() if is_test_support(n)}
        start = {n: d.decode("utf-8", "replace") for n, d in originals.items()}

        def stable(key, qualified):
            now, then = _case_files(key, texts, qualified), _case_files(key, start, qualified)
            if now is None or now != then or len(now) != 1:
                return False
            (path,) = now
            block = _case_block(key, qualified, texts[path])
            return block is not None and block == _case_block(key, qualified, start[path])
        return texts, stable

    def _discharge_named(self) -> None:
        """Decide, for the check just recorded, which named skip facts it
        discharges: a passing, required, unfiltered run of the same command
        whose complete case record shows each named case executed and none
        of them skipped. A pytest node id, printed by pytest itself both when
        the skip was recorded and now, carries its file, so its execution is
        enough, whatever else that command skipped (Codex review of e530b89,
        U2); a ``::`` in any other name proves nothing (5292fc2, V1). A bare
        name must be held by the same single test file as when it was
        recorded, read from this check's source, so a name reused in another
        file never stands in (T4, U1)."""
        from collections import Counter
        index = len(self.checks) - 1
        pending = [e for e in self.unexecuted_acceptance if e.get("kind") == "skipped" and e.get("cases")
                   and e.get("discharged_at") is None and e["after_check"] <= index]
        if not pending:
            return
        texts, _ = self._case_texts()
        runs = _check_runs(self.checks[index], self.project)
        for entry in pending:
            need = Counter(entry["cases"])
            files = entry.get("case_files") or {}
            qualified = set(entry.get("qualified_cases") or ())
            for run in runs:
                if not (run["id"] == entry["receipt"] and run["passed"] and run["required"]
                        and not run["filtered"] and run["every_names"] is not None):
                    continue
                if not all(run["executed_names"][n] >= k and not run["skipped_names"][n] for n, k in need.items()):
                    continue
                if all((n in qualified and n in run["qualified_names"])
                       or (files.get(n) and _case_files(n, texts, False) == frozenset(files[n]))
                       for n in need):
                    entry["discharged_at"] = index
                    break

    def _skip_baseline(self, gate, result, current: dict):
        """Run the gate's commands once on the run-start source when a passing
        required command skipped cases, and keep what each skipped there
        (``_start_skips``: command identity to ``(count, names)``). The
        copy is built like the original-test copy, nothing is written to the
        project, and a project that changed while it ran fails the check as
        any check whose tree moved does. A command that could not run there
        has no baseline, so its skips all count."""
        from .integration import run_original_tests, skipped_count
        cache = self.__dict__.setdefault("_start_skips", {})
        tried = self.__dict__.setdefault("_start_skips_tried", set())
        start = getattr(self, "_run_start_contents", None)
        keys = self._command_keys(gate)
        if not keys or set(keys.values()) <= tried or start is None or self._original_tests is None:
            return result
        if not any(r.status == "passed" and r.required and not r.id.startswith("original-tests:")
                   and skipped_count(r) for r in result.receipts or ()) and not (
                not result.receipts and result.passed and skipped_count(result)):
            return result
        task = getattr(self._active_spec, "task_id", "run")
        baseline, problem = run_original_tests(gate, self.project, self._original_tests, start,
                                               exclude=self.config.project_excludes)
        self._verify_dependencies(f"during run-start skip baseline ({task})")
        drift = self._source_drift(current)
        if drift:
            return replace(result, passed=False, output=drift)
        found = {}
        for receipt in (baseline.receipts if baseline else ()):
            key = keys.get(receipt.id.removeprefix("original-tests:"))
            if receipt.status == "passed" and key is not None:
                count = skipped_count(receipt)
                if count is not None:
                    found[receipt.id.removeprefix("original-tests:")] = cache[key] = (count, *_receipt_names(receipt))
        tried.update(keys.values())
        self.skip_baselines.append(dict(task=task, problem=problem, receipts={
            k: dict(skipped=v[0], cases=sorted(v[1].elements()) if v[1] is not None else None)
            for k, v in found.items()}))
        return result

    def _tests_changed_in_run(self) -> bool:
        """Whether any test or support file differs from run start (true when
        there is no run-start record to compare with: unknown is not none)."""
        from .integration import is_test_support
        from .project import Project
        if not self._original_tests or not self.project:
            return True
        try:
            current = Project(self.project, exclude=self.config.project_excludes).contents()
        except Exception:  # noqa: BLE001 -- unreadable is not unchanged
            return True
        names = {n for n in current if is_test_support(n)} | set(self._original_tests)
        return any(current.get(n) != self._original_tests.get(n) for n in names)

    def _standing_not_run(self) -> List[dict]:
        """Reported items no later complete run discharges. A NOT RUN item is
        discharged only by a later passing required command that names every
        path in it as a whole operand, runs no filter, executed cases and
        skipped none; a skip fact only by such a run of the same command. A
        model's word, a substring, an optional or skipped receipt, or a
        name-filtered run never does (Codex review of b6ba3ba, F3)."""
        standing = []
        for entry in self.unexecuted_acceptance:
            ran = False
            paths = _item_paths(entry["item"]) if entry.get("kind") != "skipped" else []
            for check in self.checks[entry["after_check"]:]:
                runs = [r for r in _check_runs(check, self.project) if _complete_run(r)]
                if entry.get("kind") == "skipped" and entry.get("cases"):
                    # Decided when each check ran, against that check's own
                    # source (_discharge_named).
                    ran = entry.get("discharged_at") is not None
                elif entry.get("kind") == "skipped":
                    ran = any(r["id"] == entry["receipt"] for r in runs)
                else:
                    ran = bool(paths) and any(all(p in r["files"] for p in paths) for r in runs)
                if ran:
                    break
            if not ran:
                standing.append(entry)
        return standing

    def _unnamed_standing(self) -> List[str]:
        """Unnamed test files not reconciled by later execution. A later
        complete run that names the file, or names a test file that names it,
        makes the observation unknown again, as it was never claimed when the
        file was first named that way (F6: a historical unnamed path kept a
        later executed child assertion NOT MET)."""
        since = getattr(self, "_unnamed_since", {}) or {}
        out = []
        for path in list(getattr(self, "unnamed_test_files", []) or []):
            reconciled = False
            for check in self.checks[since.get(path, 0):]:
                for run in _check_runs(check, self.project):
                    if not _complete_run(run):
                        continue
                    if path in run["files"] or (self.project and self._named_by_a_run_test(
                            path, [["runner", *sorted(run["files"])]])):
                        reconciled = True
                        break
                if reconciled:
                    break
            if not reconciled:
                out.append(path)
        return out

    def _not_run_block(self) -> str:
        """The audit's view of what is not shown to have run."""
        unrun = self._unnamed_standing()
        standing = self._standing_not_run()
        if not unrun and not standing:
            return ""
        lines = []
        if unrun:
            lines.append("- test files no required check names (not shown to have run): " + ", ".join(unrun))
        for entry in standing:
            lines.append(f"- task {entry['task']} ({entry['role']}) reported NOT RUN: {entry['item']}"
                         + (f" - {entry['why']}" if entry["why"] else "")
                         + (f" (covers {', '.join(entry['requirements'])})" if entry["requirements"] else ""))
        return ("\n\n## Not shown to have run (harness records)\n" + "\n".join(lines)
                + "\nA requirement whose only evidence is a file listed here is NOT MET, and so is a "
                "requirement a task covering it reported NOT RUN: name what did not run.")

    def _not_run_remedy(self, rids) -> tuple:
        """``(text, stuck)`` for a DONE refusal: what lifts each standing NOT
        RUN or skip record on the requirements ``rids``, and the requirements
        whose record nothing in this run can lift.

        The lowering rule is unchanged. This is its signpost: the refusal used
        to say only "name a task that fixes them", and a task that covers the
        requirement again never lifts a report, so the reopen allowance went
        on tasks that could not clear it. An item that names no file path
        (``_item_paths``) cannot be matched to any check: it stands for the
        rest of the run, and the orchestrator is told not to plan for it."""
        lines, stuck = [], []
        for entry in self._standing_not_run():
            hit = [r for r in entry["requirements"] if r in rids]
            if not hit:
                continue
            head = f"- {', '.join(hit)}: task {entry['task']} reported NOT RUN: {entry['item'][:160]}. "
            paths = _item_paths(entry["item"]) if entry.get("kind") != "skipped" else []
            if entry.get("kind") == "skipped":
                lines.append(head + f"Lifted when a later run of required check {entry['receipt']} passes "
                             "with those cases executed and none skipped.")
            elif paths:
                lines.append(head + "Lifted only when a later required check passes while naming "
                             f"{', '.join(paths)[:160]} as a whole file, with no filter and nothing skipped.")
            else:
                lines.append(head + "A report is lifted only by a later required check that names its "
                             "file, and this one names none: it stands for the rest of this run.")
                stuck += [r for r in hit if r not in stuck]
        if not lines:
            return "", []
        more = len(lines) - _NOT_RUN_REMEDY_LINES
        text = ("\nA NOT RUN report is lifted by execution, never by covering its requirement again. "
                "The harness runs only the configured checks, and a task cannot add one:\n"
                + "\n".join(lines[:_NOT_RUN_REMEDY_LINES])
                + (f"\n- and {more} more standing report(s)" if more > 0 else ""))
        if stuck:
            text += f"\nNo task lifts the report on {', '.join(stuck)}: do not name one for it."
        return text, stuck

    def _lower_unexecuted(self, found: dict) -> List[dict]:
        """Turn a MET into NOT MET where the record shows its acceptance did
        not run, whatever the auditor wrote; never the other way.

        A MET whose resolved citations are all test files no required check
        names, and no later run reconciled, is not shown (files outside the
        test families are not classified, so citing them is not lowered
        here). A requirement a task covering it reported NOT RUN for, or
        whose task's required check skipped cases, is not met while that
        record stands, even when the MET also cites a file that did run: a
        unit file that ran does not stand in for a scenario that did not.
        """
        unrun = set(self._unnamed_standing())
        standing = self._standing_not_run()
        lowered = []
        for rid, (met, why) in list(found.items()):
            if not met:
                continue
            cited = sorted({c.split("::")[0] for c in self._resolve_citations(why)})
            reason = ""
            if unrun and cited and all(c in unrun for c in cited):
                reason = (f"MET cites only test files no required check names ({', '.join(cited)}); "
                          "not shown to have run")
            blockers = [e for e in standing if rid in e["requirements"]]
            if blockers:
                reason = ("acceptance reported NOT RUN by the covering task: "
                          + "; ".join(f"{e['task']}: {e['item']}" for e in blockers))[:300]
            if reason:
                found[rid] = (False, reason)
                lowered.append(dict(requirement=rid, auditor_said=why[:200], reason=reason))
        return lowered

    def _run_integration_gate(self, lead: str, spec: TaskSpec, task: TaskMemory, *,
                              gate=None, max_fixes=None) -> str:
        """Execute the project's own check and feed a failure back once.

        Reviewers judge the work by reading; this is the half that runs it.
        A failure buys the lead a bounded number of fix rounds with the real
        output in hand; a failure that survives the cap is written loudly
        into the task memory so the close-out and the ledger carry it as an
        open problem instead of a silent one.
        """
        full = gate is None
        if full:
            # The task's mandatory gate is its contract's, fixed at dispatch
            # with the gate it describes (map P3.4, checks). The live setting
            # is still compared. With no contract for the current task (the
            # parallel merge gate) the configured gate decides, as before.
            live = self.config.integration_gate
            if not self._required("checks", live is not None):
                return ""
            gate = self._bound_gate() or live
        if gate is None:
            return ""
        self._stage("checks")
        ceiling = self.config.max_gate_fixes
        if max_fixes is not None:
            ceiling = min(ceiling, getattr(self, "_gate_fixes_used", 0) + max_fixes)
        latest_fix = ""
        # Which gate an attempt ran: the run's full required gate, or a subset
        # of it (a cheap view) or another gate (the merge gate).
        kind = "full" if full or gate is self.config.integration_gate else "subset"
        result = self._check(gate)
        task.record("user", result.for_models())
        attempt_facts = [self._record_check_attempt(result, gate_kind=kind)]
        self._handoff_unattributable(gate, result, task)
        while not result.passed and getattr(self, "_gate_fixes_used", 0) < ceiling:
            try:
                fix = self._edit(
                    lead,
                    f"Task: {spec.description}\n\n"
                    f"The project's own integration check failed after your "
                    f"work:\n{result.for_models()}\n\n"
                    "Fix the failure. Produce the complete revised work."
                    + (_revision_changed_delivery(self._interim_edits_note()) if self._writes() else "")
                    + _NO_REQUESTS_IN_FIX,
                    role="gate-fix",
                )
            except TurnLimitReached as exc:
                fix = self._capped_fix(lead, spec, task, exc, "gate-fix")
            task.record("assistant", fix)
            task.keep(fix, kind="gate-fix")
            latest_fix = fix
            self._gate_fixes_used = getattr(self, "_gate_fixes_used", 0) + 1
            result = self._check(gate)
            task.record("user", result.for_models())
            attempt_facts.append(self._record_check_attempt(result, gate_kind=kind))
            self._handoff_unattributable(gate, result, task)
        self.checks.append({"passed": result.passed, "command": result.command, "gate": _gate_identity(gate),
                            "output": result.output, "cwd": str(self.project or getattr(gate, 'cwd', '')),
                            "selection": self._gate_selection(gate), "command_cwd": self._gate_cwd(gate),
                            "cases": result.cases,
                            # A single-command gate's parsed report and the
                            # source it ran on (a suite's receipts carry
                            # their own), for the requirements audit.
                            "report": result.report, "source": self._source_fingerprint(),
                            "receipts": [dataclasses.asdict(r) for r in result.receipts]})
        self._discharge_named()
        # A passing check makes every earlier check failure in the same task
        # history: this invocation's attempts and an earlier invocation's, such
        # as a gate a design-fix later repaired (map G12, J38). Only the
        # task's own failures; another task's stay as they were.
        if result.passed:
            for fact in attempt_facts:
                if fact is not None:
                    fact.recovered = True
            if self._outcome is not None:
                for fact in self._outcome.facts:
                    if fact.stage == "checks" and fact.kind == "product" and fact.active:
                        fact.recovered = True
        if self._outcome is not None:
            self._outcome.attempts["gate_fix"] = getattr(self, "_gate_fixes_used", 0)
            self._outcome.edge("checks", result.passed)
        if full:
            self._note_unrun_tests(spec, task, gate)
            self._note_skips(spec, result, gate)
        if not result.passed:
            task.record(
                "user",
                "The integration gate is still failing at close -- carry it "
                "into the summary as an open failure.",
            )

        return latest_fix

    def _harness_record(self, task_id: str) -> List[str]:
        """What the harness measured for ``task_id``: the latest design
        check (capture verified or not, views, the final reviewer's verdict)
        and the task's required checks. Facts from records, never prose."""
        facts: List[str] = []
        checks = [r for r in self.design_checks if isinstance(r, dict) and r.get("task") == task_id]
        if checks:
            record = checks[-1]
            views = sorted({Path(str(shot)).parent.name for shot in record.get("screenshots") or ()
                            if str(shot).endswith(".png")})
            if record.get("verified"):
                facts.append("design capture: verified by the harness"
                             + (f" ({', '.join(views)})" if views else ""))
            else:
                facts.append("design capture: not verified"
                             + (f" ({str(record.get('problem'))[:200]})" if record.get("problem") else ""))
            review = record.get("final_review") or {}
            if review.get("verdict") or review.get("blocking"):
                facts.append(f"final design review by {review.get('reviewer')}: {_review_reading(review)}")
        outcome = self._outcome if getattr(self._outcome, "task_id", None) == task_id else None
        if outcome is not None and "checks" in (outcome.edges or {}):
            facts.append("required checks: " + ("passed" if outcome.edges["checks"] else "failed"))
        held = next((r for r in reversed(self.contests) if r.get("task") == task_id and r.get("formed")), None)
        if held is not None:
            from .contest import ledger_line
            facts.append(ledger_line(held))
        return facts

    def _note_unrun_tests(self, spec, task, gate) -> None:
        """A test file this task added or changed that no required check runs
        is unverified, never passed by the gate's silence (batch 2 gui-ui-v3
        on 5d9f5ff: a new tests/ui file beside a Node check listing five
        others). Nothing is widened: the commands stay as configured, and the
        finding says which file and which checks."""
        from .integration import uncovered_tests
        from .scope import changed_paths
        if not self.project or self._task_before is None:
            return
        argvs = ([c.argv for c in gate.commands] if hasattr(gate, "commands")
                 else [getattr(gate, "command", ())])
        try:
            from .project import Project
            changed = changed_paths(Project(self.project, exclude=self.config.project_excludes)
                                    .diff(self._task_before))
        except Exception:  # noqa: BLE001 -- observation never fails a task
            return
        unrun = uncovered_tests(changed, argvs)
        # A test file a required check runs may itself run the new one (batch
        # 2 gui-ui-v3 t5: a pytest test invoked node on the new search tests,
        # inside the approved pytest check). A mention by name is evidence it
        # may run, so nothing is claimed for it: unknown is not uncovered.
        unrun = [path for path in unrun if not self._named_by_a_run_test(path, argvs)]
        if not unrun:
            return
        # An observation, not a finding: a check can run a file it does not
        # name through a wrapper, so "not named" never proves "not run"
        # (Codex review of 7515f28). It reaches the lead, the close-out and
        # result.json; it does not decide completion.
        text = (f"Task {spec.task_id}: {', '.join(unrun)} changed by this task "
                f"{'is' if len(unrun) == 1 else 'are'} not named by any required check (the checks "
                "list their test files); whether a check runs "
                f"{'it' if len(unrun) == 1 else 'them'} indirectly is unknown, so "
                f"{'its' if len(unrun) == 1 else 'their'} tests are not shown to have passed.")
        self.unnamed_test_files = list(dict.fromkeys([*getattr(self, "unnamed_test_files", []), *unrun]))
        since = getattr(self, "_unnamed_since", {}) or {}
        for path in unrun:
            since[path] = len(self.checks)
        self._unnamed_since = since
        task.record("user", text)
        self._note(f"task {spec.task_id}: test file(s) no required check names: {', '.join(unrun)}")

    def _named_by_a_run_test(self, path: str, argvs, *, limit: int = 2000) -> bool:
        """Whether a test file some required check runs names ``path``: a
        listed test file, or a Python test file when a check discovers them.
        A mention inside a comment does not count (``_named_outside_comments``).
        Bounded: at most ``limit`` files of at most 1 MB are read."""
        from .integration import _test_family
        root = Path(self.project)
        name = PurePosixPath(path).name
        listed = {str(a) for argv in argvs for a in argv[1:] if _test_family(str(a))}
        discovers = any("pytest" in " ".join(str(a) for a in argv[:3]) for argv in argvs)
        from .project import Project
        seen = 0
        try:
            files = Project(self.project, exclude=self.config.project_excludes).files()
        except Exception:  # noqa: BLE001 -- observation never fails a task
            return False
        for candidate in files:
            if seen >= limit:
                break
            candidate = Path(candidate)
            candidate = candidate if candidate.is_absolute() else root / candidate
            rel = candidate.relative_to(root).as_posix()
            if rel == path:
                continue
            family = _test_family(rel)
            if not (rel in listed or (discovers and family and family[1] == "py")):
                continue
            seen += 1
            try:
                if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 1 << 20:
                    if _named_outside_comments(candidate.read_text(encoding="utf-8", errors="replace"),
                                               name, rel.endswith(".py")):
                        return True
            except OSError:
                continue
        return False

    def _handoff_unattributable(self, gate, result, task) -> None:
        """Stop, with diagnostics and no repair call, on a failure that is not
        an attributable assertion failure (integration.attribute). Only a
        product failure may reach a gate-fix; a runner crash, a setup or
        collection error, a runtime exception, a timeout, a missing runner or
        an undeclared report is the operator's (phase 3, #25; map J4-J6, J33)."""
        from .integration import CheckUnattributable
        attempt = self._outcome.checks[-1] if self._outcome is not None and self._outcome.checks else {}
        verdict = attempt.get("attribution") if attempt else None
        if verdict is None:
            from .integration import attribute
            verdict = attribute(result)
        if result.passed or verdict["product"]:
            return
        self.checks.append({"passed": False, "command": result.command, "output": result.output,
                            "cwd": str(self.project or getattr(gate, 'cwd', '')),
                            "selection": self._gate_selection(gate), "command_cwd": self._gate_cwd(gate),
                            "cases": result.cases,
                            "receipts": [dataclasses.asdict(r) for r in result.receipts]})
        if self._outcome is not None:
            self._outcome.edge("checks", False)
        task.record("user", "The check failure is not attributable to the application; handed to the "
                            "operator without a repair call.")
        raise CheckUnattributable(
            f"the check failed without an attributable assertion failure "
            f"({'; '.join(verdict['reasons'])[:400]}); returncode {result.returncode}; "
            f"output artifact {attempt.get('output_artifact', 'unavailable')}. No repair call was made. "
            "Work preserved.")

    def _record_check_attempt(self, result, gate_kind="full"):
        """One executed check, as a typed record: receipts in full, the output
        kept as an artifact, the source it ran against. Returns the product
        fact a failed attempt adds (None when it passed)."""
        target = self._outcome
        output, lost = "unavailable", None
        try:
            output = self.store.put(result.output or "", kind="check-output", author="harness").id
        except Exception as exc:  # noqa: BLE001 -- observation never fails a check
            # Kept explicit: a lost artifact makes the record incomplete
            # (outcome.missing_facts), it is never passed off as a receipt.
            lost = f"{type(exc).__name__}: {str(exc)[:160]}"
        from .integration import attribute, without_names
        # The names of a quiet run's passed cases are kept in one place: the
        # run's newest check in self.checks, which the caller appends after
        # this and which is where they are read (the test-repair and
        # test-change rules at DONE, tools/truth_check.py). This record keeps
        # the counts, and so does every earlier check from here on. Kept in
        # each, a large quiet suite took result.json past what the monitor
        # reads (final review 2026-11-11, gate 4).
        for earlier in self.checks:
            if earlier.get("report") is not None:
                earlier["report"] = without_names(earlier["report"])
            for receipt in earlier.get("receipts") or ():
                if isinstance(receipt, dict) and receipt.get("report") is not None:
                    receipt["report"] = without_names(receipt["report"])
        entry = dict(passed=result.passed, returncode=result.returncode, gate=gate_kind, output_artifact=output,
                     output_artifact_error=lost,
                     source=self._source_identity(),
                     receipts=[dict(id=r.id, status=r.status, reason=r.reason, tests=r.tests,
                                    returncode=r.returncode, cached=r.cached, source_hash=r.source_hash,
                                    runner_hash=r.runner_hash, report=without_names(r.report))
                               for r in result.receipts],
                     report=without_names(result.report))
        verdict = attribute(result)
        if not result.passed:
            entry["attribution"] = verdict
        if target is not None:
            entry["attempt"] = len(target.checks) + 1
            target.checks.append(entry)
            if not result.passed:
                # Classed by attribution: only an attributable assertion
                # failure is a product failure; anything else is the operator's.
                return target.note("product" if verdict["product"] else "operator",
                                   f"check attempt {entry['attempt']} failed", stage="checks")
            return None
        if not result.passed:
            return self.run_outcome.note("product", "a gate outside any task (the merge gate) failed")
        return None

    def _check(self, gate):
        """Run the gate, and say *what* changed when the tree moved under it.

        The same-tree check is unchanged: a fingerprint taken before and after,
        with any difference invalidating the result. What changed is the
        diagnostic. "Project source changed while the check ran" left the lead
        with nowhere to start -- on 2026-09-13 that message sent a model
        hunting for concurrent edits when the real cause was the test suite
        writing into ``data/recordings/``, which one filename would have named
        immediately.

        Contents are captured before and after rather than only a digest, so
        the comparison can name added, removed and modified paths. Unexpected
        new source files are the point of the check, so they are reported, not
        excluded -- narrowing the fingerprint to tracked files would have made
        this failure invisible instead of merely unhelpful.
        """
        from .project import Project
        project = Project(self.project, exclude=self.config.project_excludes) if self.project else None
        task = getattr(self._active_spec, "task_id", "run")
        self._verify_dependencies(f"before check ({task})")
        before = project.contents() if project else None
        self._activity('check-started', task=task)
        try:
            result = gate.run()
        finally:
            self._activity('check-ended', task=task)
        # A receipt taken with a changed dependency tree is never accepted.
        self._verify_dependencies(f"during check ({task})")
        if project is not None:
            after = project.contents()
            if before != after:
                return replace(result, passed=False, output=_describe_tree_change(before, after))
            return self._with_original_tests(gate, result, after)
        return result

    def _snapshot_original_tests(self) -> None:
        """Keep the run-start bytes of every test and support file.

        Series rule-119c83f f2: t2 changed the original helper
        tests/ui/load_app.js so the new code loaded under it, the gate ran the
        edited helper and passed, and the original tests failed against the
        delivered code. The bytes kept here are what a later check restores
        (Codex decision D, #35 6076286834)."""
        from .integration import original_tests, selection_sections
        from .project import Project
        try:
            contents = Project(self.project, exclude=self.config.project_excludes).contents()
            self._original_tests = original_tests(contents)
            self._record_repair_originals(contents)
            # The whole run-start source, for the one skip baseline run
            # (_skip_baseline, Codex review of 81adcc7, R3 and R4).
            self._run_start_contents = contents
            # Test selection held in mixed files (pyproject, setup.cfg, tox.ini,
            # package.json) is kept by section; pure test configs such as
            # pytest.ini are already among the restored files (F1).
            self._original_selection = selection_sections(contents)
        except Exception as exc:  # noqa: BLE001 -- recorded, never a silent pass
            self._original_tests = None
            self._unverified_once("The run-start test files could not be read, so changes to "
                                             f"them cannot be checked: {type(exc).__name__}: {str(exc)[:160]}")

    def _record_repair_originals(self, contents: dict) -> None:
        """Keep each authorized repair's run-start bytes as evidence before any
        edit: the broken original is a raw failure record, never discarded."""
        from .integration import declared_cases
        for name in self.config.repair_tests:
            data = contents.get(name)
            if data is None:
                continue
            self.test_repairs.append(dict(
                path=name, original_sha256=hashlib.sha256(data).hexdigest(),
                original_artifact=self.store.put(data.decode("utf-8", errors="replace"),
                                                 kind="repair-original", author="harness").id,
                original_cases=declared_cases(name, data), checks=[],
                # Kept names and executed cases do not show the assertions
                # were not weakened: a same-name trivial pass satisfies both.
                # Never set by the engine; an independent source review is
                # what can establish it.
                assertions_verified=False))

    def _repaired(self, changed, current: dict) -> List[str]:
        """The changed run-start files that are authorized repairs still present."""
        return [n for n in changed if n in self.config.repair_tests and n in current]

    def _repair_check(self, names, current: dict, receipts, where: str) -> List[str]:
        """Each authorized repair's cases against this check's receipts."""
        from .integration import repair_problems
        problems = []
        for name in names:
            found = repair_problems(name, self._original_tests[name], current, receipts)
            for record in self.test_repairs:
                if record["path"] == name:
                    record["checks"].append(dict(
                        at=where, current_sha256=hashlib.sha256(current[name]).hexdigest(),
                        passed=not found, problems=found))
            problems += found
        return problems

    def _with_original_tests(self, gate, result, current: dict):
        """The gate's result joined by the original tests, then the run-start
        skip baseline when this check is the first to need it."""
        result = self._join_original_tests(gate, result, current)
        return self._skip_baseline(gate, result, current)

    def _test_migrations(self, names, current: dict, receipts, *, request=False) -> Dict[str, dict]:
        """Exact, human-approved test changes whose replacement cases ran.

        A passing edited suite does not authorize replacing its old tests.
        A concrete approval binds the goal, original bytes and delivered bytes;
        changing the file again requires a new decision. No model can approve.
        """
        from .design_evidence import EvidenceIdentityMismatch
        from .integration import declared_cases, test_change_authorizations, test_change_problems
        from .project import Project
        owner = getattr(self, "_authority_parent", None) or self
        decisions = owner.test_change_decisions
        approved = {}
        for name in names:
            original, delivered = (self._original_tests or {}).get(name), current.get(name)
            if original is None or delivered is None:
                continue
            digest = hashlib.sha256(delivered).hexdigest()
            original_digest = hashlib.sha256(original).hexdigest()
            existing = next((d for d in reversed(decisions)
                             if d.get("path") == name and d.get("current_sha256") == digest
                             and d.get("original_sha256") == original_digest
                             and d.get("goal") == self.memory.goal), None)
            if existing is not None:
                if existing.get("status") == "approved" and not test_change_problems(
                        existing, original, current, receipts):
                    existing["checked_source"] = self._source_fingerprint()
                    approved[name] = existing
                continue
            configured = next((dict(d) for d in getattr(self.config, "test_changes", ())
                               if d["path"] == name), None)
            proposal = configured
            if proposal is None and request and self.config.ask_operator is not None:
                old_cases = declared_cases(name, original)
                new_cases = declared_cases(name, delivered)
                if new_cases is None and name.endswith(".py"):
                    import ast
                    try:
                        tree = ast.parse(delivered)
                        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                   and n.name.startswith("test_") for n in ast.walk(tree)):
                            new_cases = []
                    except (SyntaxError, ValueError):
                        pass
                if not old_cases or new_cases is None:
                    continue
                proposal = dict(goal=self.memory.goal, path=name, original_sha256=original_digest,
                                current_sha256=digest,
                                reason="Operator review of a test change against the verbatim goal",
                                cases=[dict(case=c, action="replace" if new_cases else "retire",
                                            replacements=[dict(path=name, case=c)] if c in new_cases else
                                            [dict(path=name, case=n) for n in new_cases]) for c in old_cases])
            if proposal is None or proposal["original_sha256"] != original_digest:
                continue
            if proposal.get("current_sha256") not in (None, digest):
                continue
            proposal["current_sha256"] = digest
            envelope = dict(schema_version=1, goal=self.memory.goal,
                            changes=[{k: v for k, v in proposal.items() if k != "goal"}])
            try:
                proposal = dict(test_change_authorizations(envelope, goal=self.memory.goal, root=self.project,
                                                          originals=self._original_tests)[0])
            except ValueError:
                continue
            problems = test_change_problems(proposal, original, current, receipts)
            if problems:
                continue
            preapproved = bool(configured and configured.get("current_sha256") == digest)
            if not preapproved and (not request or self.config.ask_operator is None):
                continue
            request_id = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
            answer = "exact delivered bytes supplied by the operator"
            if not preapproved:
                import difflib
                diff = "".join(difflib.unified_diff(original.decode("utf-8", errors="replace").splitlines(True),
                                                    delivered.decode("utf-8", errors="replace").splitlines(True),
                                                    fromfile=f"original/{name}", tofile=f"delivered/{name}"))
                # Never ask a human to approve a silently truncated change.
                if len(diff.encode()) > 64_000:
                    self._note(f"Test change {name} needs an exact --test-changes authorization; "
                               "its full diff exceeds the interactive review limit.")
                    continue
                question = (f"The edited tests pass, but run-start tests conflict with the delivered code. "
                            f"Approve this exact test change only if it implements your requested behavior. "
                            f"Other files' original checks remain required.\n\nGoal (verbatim):\n{self.memory.goal}"
                            f"\n\nProposed case changes:\n{json.dumps(proposal, indent=2)}\n\nFull diff:\n{diff}"
                            f"\nReply exactly APPROVE TEST CHANGE {request_id} to authorize these bytes; "
                            "any other answer declines the change.")
                answer = str(self.config.ask_operator(question))
                owner.memory.ledger.rulings.append(f"Test change {request_id} for {name}: {answer}")
                # The approval never applies to bytes that moved while the question was open.
                if Project(self.project, exclude=self.config.project_excludes).contents() != current:
                    raise EvidenceIdentityMismatch("Project changed during test-change approval; approval not applied.")
            accepted = preapproved or answer.strip() == f"APPROVE TEST CHANGE {request_id}"
            original_artifact = self.store.put(original.decode("utf-8", errors="replace"),
                                               kind="test-change-original", author="harness").id
            delivered_artifact = self.store.put(delivered.decode("utf-8", errors="replace"),
                                                kind="test-change-delivered", author="harness").id
            decision = dict(proposal, id=request_id, status="approved" if accepted else "declined",
                            approval="operator exact bytes" if preapproved else "operator reply",
                            answer=answer, original_artifact=original_artifact, delivered_artifact=delivered_artifact,
                            checked_source=self._source_fingerprint())
            decisions.append(decision)
            if hasattr(owner, "_publish_authority"):
                owner._publish_authority()
            if accepted:
                approved[name] = decision
        return approved


    def _command_keys(self, gate) -> dict:
        """Receipt id to the command's identity (id, argv, cwd), so a baseline
        measured once serves every gate object that runs the same command (a
        cheap view is a new object on each call)."""
        from .integration import _gate_commands
        if not self.project:
            return {}
        commands = _gate_commands(gate, Path(self.project).resolve()) or []
        return {c.id: (c.id, tuple(c.argv), c.cwd) for c in commands}

    def _gate_cwd(self, gate) -> str:
        """A single-command gate's working directory relative to the project
        ('.' for a suite, whose receipts carry their own; S4)."""
        from .integration import IntegrationGate
        if not isinstance(gate, IntegrationGate) or not self.project or not gate.cwd:
            return "."
        root, cwd = Path(self.project).resolve(), Path(gate.cwd).resolve()
        return cwd.relative_to(root).as_posix() if cwd.is_relative_to(root) else "."

    def _gate_selection(self, gate) -> str:
        """Configuration or environment test selection for a single-command
        gate (a suite's receipts carry their own)."""
        from .integration import IntegrationGate, config_selection
        if not isinstance(gate, IntegrationGate) or not self.project:
            return ""
        try:
            return config_selection(gate.command, Path(gate.cwd or self.project), Path(self.project))
        except Exception as exc:  # noqa: BLE001 -- unknown selection is selection
            return f"selection unreadable: {type(exc).__name__}"

    def _join_original_tests(self, gate, result, current: dict):
        """The gate's result, joined by the original tests when a run-start
        test or support file now differs.

        Unchanged originals need no second run: the gate already ran them as
        they are. When any changed or was removed, the same commands run in a
        copy of the current source with the run-start bytes restored
        (integration.run_original_tests), and a failure there fails the check
        with its own receipts, so the ordinary repair and attribution rules
        apply to it. A copy that cannot be checked is an open unverified
        finding, never a pass."""
        from .integration import GateReceipt, added_selectors, changed_originals, run_original_tests
        originals = self._original_tests
        if not originals:
            return result
        task = getattr(self._active_spec, "task_id", "run")
        if self._selection_changed(current, task):
            return result
        # An added pytest.ini or conftest.py can deselect a run-start test the
        # same way an edit can (Codex review of 81adcc7, R1); the copy leaves
        # it out, so the original suite runs as it was selected at run start.
        added = added_selectors(originals, current)
        changed = changed_originals(originals, current) + added
        if not changed:
            return result
        repaired = self._repaired(changed, current)
        migration_receipts = result.receipts or (dict(status="passed" if result.passed else "failed",
                                                     required=True, cases=result.cases, report=result.report),)
        migrated = self._test_migrations(changed, current, migration_receipts) if result.passed else {}
        repaired = [n for n in repaired if n not in migrated]
        # A failing check stays an ordinary failure with its own attribution;
        # the repaired cases are judged on a check that passed.
        if repaired and result.passed:
            problems = self._repair_check(repaired, current, result.receipts or (dict(
                status="passed" if result.passed else "failed", required=True, cases=result.cases),), task)
            if problems:
                text = ("Authorized test repair not shown to keep the original cases:\n"
                        + "\n".join(f"- {p}" for p in problems))
                self._note(f"task {task}: {text}")
                return replace(result, passed=False, returncode=result.returncode or 1,
                               output=f"{result.output}\n\n{text}"[-8_000:], receipts=tuple(result.receipts) + (
                                   GateReceipt(id="repair-check", status="failed", reason="; ".join(problems)[:300],
                                               required=True),))
        if repaired or migrated:
            changed = [n for n in changed if n not in repaired and n not in migrated]
            if not changed:
                if result.passed:
                    self._note(f"task {task}: authorized test changes in {', '.join([*repaired, *migrated])}; "
                               "their required cases executed; no other run-start test file changed")
                return result
        baseline, problem = run_original_tests(gate, self.project, originals, current, keep=[*repaired, *migrated],
                                               exclude=self.config.project_excludes)
        self._verify_dependencies(f"during original tests ({task})")
        drift = self._source_drift(current)
        if drift:
            # The copy was built from a source that is no longer the project's:
            # its receipts describe neither, so the check fails as any check
            # whose tree moved under it does (Codex preliminary review of b6ba3ba).
            return replace(result, passed=False, output=drift)
        skipped = [r for r in (baseline.receipts if baseline else ()) if r.status == "skipped"]
        self.original_test_runs.append(dict(
            task=task, changed=changed, added=added, passed=None if baseline is None else baseline.passed,
            problem=problem, receipts=[dict(id=r.id, status=r.status, reason=r.reason, tests=r.tests)
                                       for r in (baseline.receipts if baseline else ())]))
        if baseline is not None and not baseline.passed and result.passed and not skipped:
            approved = self._test_migrations(changed, current, migration_receipts, request=True)
            if approved:
                self.original_test_runs[-1]["superseded_tests"] = {n: d["id"] for n, d in approved.items()}
                return self._join_original_tests(gate, result, current)
        for receipt in skipped:
            self._unverified_once(f"Task {task}: {receipt.id} did not run against the run-start "
                                             f"test files: {receipt.reason}")
        if baseline is None:
            self._unverified_once(f"Task {task}: run-start test files changed "
                                             f"({', '.join(changed)}) and the original tests were not run: {problem}")
            return result
        self._note(f"task {task}: original tests {'passed' if baseline.passed else 'FAILED'} against the "
                   f"current source (changed run-start files: {', '.join(changed)})")
        inner = result.receipts or (GateReceipt(
            id="check", status="passed" if result.passed else "failed",
            reason="exit 0" if result.passed else "nonzero exit", required=True,
            command=result.command, returncode=result.returncode, output=result.output,
            report=result.report, cwd=self._gate_cwd(gate), selection=self._gate_selection(gate),
            cases=result.cases),)
        restored = [n for n in changed if n not in added]
        heading = ("Original tests (" + "; ".join(part for part in (
            f"run-start versions of {', '.join(restored)}" if restored else "",
            f"added test configuration left out: {', '.join(added)}" if added else "") if part)
                   + ") against the current "
                   f"source: {'PASSED' if baseline.passed else 'FAILED'}")
        return replace(result, passed=result.passed and baseline.passed,
                       returncode=result.returncode if not result.passed else baseline.returncode,
                       output=f"{result.output}\n\n{heading}\n{baseline.output}"[-8_000:],
                       receipts=tuple(inner) + tuple(baseline.receipts))

    def _selection_changed(self, current: dict, where: str) -> bool:
        """Whether test selection in a mixed configuration file changed since
        run start. Restoring such a file whole would also restore unrelated
        settings the current code needs, so the original suite cannot be
        established: an open unverified finding, never a pass (Codex review of
        b6ba3ba, F1: a changed selection hid a restored original assertion)."""
        from .integration import changed_selection
        names = changed_selection(getattr(self, "_original_selection", None) or {}, current)
        if not names:
            return False
        text = (f"{'At DONE' if where == 'DONE' else f'Task {where}'}: test selection in "
                f"{', '.join(names)} changed since run start, so the original tests cannot be run as "
                "they were; their result is not established.")
        self._unverified_once(text)
        # One record per distinct set of files: this runs on every check and
        # every DONE while the selection stays changed, and the same record
        # was appended each time.
        if tuple(names) not in self._selection_recorded:
            self._selection_recorded.add(tuple(names))
            self.original_test_runs.append(dict(task=where, changed=names, passed=None, problem=text, receipts=[]))
        return True

    def _source_drift(self, current: dict) -> str:
        """What changed in the project since ``current`` was read, or ''."""
        from .project import Project
        now = Project(self.project, exclude=self.config.project_excludes).contents()
        return "" if now == current else _describe_tree_change(current, now)

    def _original_tests_at_done(self) -> str:
        """At DONE: the original tests again when run-start bytes differ.
        Returns the refusal text, or '' when they pass or nothing changed;
        a run that cannot check them is an open unverified finding."""
        from .integration import added_selectors, changed_originals
        from .project import Project
        if not self._original_tests or not self.project:
            return ""
        current = Project(self.project, exclude=self.config.project_excludes).contents()
        if self._selection_changed(current, "DONE"):
            return ""
        changed = (changed_originals(self._original_tests, current)
                   + added_selectors(self._original_tests, current))
        if not changed:
            return ""
        repaired = self._repaired(changed, current)
        last = self.checks[-1] if self.checks else {}
        migrated = (self._test_migrations(changed, current, last.get("receipts") or (dict(
            status="passed", required=True, cases=last.get("cases") or {}, report=last.get("report")),))
                    if last.get("passed") and last.get("source") == self._source_fingerprint() else {})
        repaired = [n for n in repaired if n not in migrated]
        changed = [n for n in changed if n not in migrated]
        if not changed:
            return ""
        if repaired:
            # The delivered bytes of each repaired file must be bytes a check
            # already showed keeping every original case, executed.
            problems = []
            for name in repaired:
                digest = hashlib.sha256(current[name]).hexdigest()
                if not any(c["passed"] and c["current_sha256"] == digest
                           for r in self.test_repairs if r["path"] == name for c in r["checks"]):
                    problems.append(f"{name}: no check of these delivered bytes showed every original case "
                                    "executing")
            if problems:
                return ("\n\n--- DONE SENT BACK ---\nAn authorized test repair is not shown to keep the "
                        "original cases:\n" + "\n".join(f"- {p}" for p in problems))
            changed = [n for n in changed if n not in repaired]
            if not changed:
                return ""
        key = hashlib.sha256(b"".join(n.encode() + b"\0" + hashlib.sha256(d).digest()
                                      for n, d in sorted(current.items()))).hexdigest()
        cached = getattr(self, "_done_original_tests", None)
        if cached and cached[0] == key:
            return cached[1]
        refusal = self._run_original_tests_at_done(current, changed, keep=tuple([*repaired, *migrated]))
        self._done_original_tests = (key, refusal)
        return refusal

    def _run_original_tests_at_done(self, current: dict, changed: List[str], keep=()) -> str:
        from .integration import run_original_tests
        gate = self.config.integration_gate
        if gate is None:
            self._unverified_once("Run-start test files changed and no check is configured to "
                                             "run the original tests against the delivered source.")
            return ""
        baseline, problem = run_original_tests(gate, self.project, self._original_tests, current, keep=keep,
                                               exclude=self.config.project_excludes)
        self._verify_dependencies("during original tests (DONE)")
        drift = self._source_drift(current)
        if drift:
            return ("\n\n--- DONE SENT BACK ---\nThe project changed while its original tests ran, so "
                    "they prove nothing about the delivered source:\n" + drift[:1_000])
        self.original_test_runs.append(dict(
            task="DONE", changed=changed, passed=None if baseline is None else baseline.passed,
            problem=problem, receipts=[dict(id=r.id, status=r.status, reason=r.reason, tests=r.tests)
                                       for r in (baseline.receipts if baseline else ())]))
        if baseline is None:
            self._unverified_once(f"At DONE: run-start test files changed ({', '.join(changed)}) "
                                             f"and the original tests were not run: {problem}")
            return ""
        failed = [r for r in baseline.receipts if r.required and r.status not in ("passed", "skipped")]
        for receipt in baseline.receipts:
            if receipt.status == "skipped":
                self._unverified_once(f"At DONE: {receipt.id} did not run against the run-start "
                                                 f"test files: {receipt.reason}")
        if not failed:
            return ""
        return ("\n\n--- DONE SENT BACK ---\nThe project's original tests (their run-start versions) fail "
                "against the delivered source:\n"
                + "\n".join(f"- {r.id}: {r.status}: {r.reason}" for r in failed)
                + "\nName a task that makes the code pass the original tests (with COVERS).")
