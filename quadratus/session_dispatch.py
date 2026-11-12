"""Dispatch intake for the session engine, as a mixin of Session: reading a
task's directives, issuing its id, binding its contract (tier, gate,
write grant), the operator's explicit task list, the survey and kind
requests, and the routing of the next task.

The methods moved out of session.py unchanged on 2026-11-12, when the split
of 2026-11-11 was redone after the development line was merged. They reach
the rest of Session only through ``self``; a method that reads a name a
test replaces on the session module stayed there. This module never
imports quadratus.session.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from .check_facts import _case_collision
from .delegation import invocation
from .outcome import TaskOutcome
from .reply_parsers import (
    _DECIDE,
    _FALLBACK_PACKET_BYTES,
    _RESOLVES,
    _parallel_blocks,
    _read_continues,
    _read_covers,
    _read_hypothesis,
    _read_metadata,
    _read_requirements,
    _read_resolves,
    _read_task_orientation,
    _read_tier,
)
from .scope import TaskScope
from .scope import _matches as _scope_matches
from .session_prompts import (
    _DECIDER_LABELS_REQUEST,
    _HYPOTHESIS_REQUEST,
    _KIND_REQUEST,
    _SCOPE_REQUEST,
    _kind_request_text,
)
from .session_types import (
    Complexity,
    PartialWorkStopped,
    RunStalled,
    TaskListInvalid,
    TaskSpec,
    _Intake,
    _repeat_key,
    is_design_task,
)
from .task_kinds import ROUTING
from .taskmeta import AmbiguousMetadata, parse_control


class DispatchMixin:
    """How a named task becomes a dispatched one, and what its contract records.

    Session inherits this; nothing here is used on its own.
    """

    def _run_readiness(self) -> None:
        """The operator's readiness probes, once, before any model call. A
        failure is an operator handoff: no model call, no application edit.
        Probes may not change project source or dependency trees."""
        from .readiness import CapabilityProbeFailed, run_probes
        before = self._source_identity()
        with self._operation("readiness probes", inputs=dict(probes=[p.id for p in self.config.readiness_probes],
                                                             source=before),
                             effects=("run-probe",)) as op:
            receipts = run_probes(self.config.readiness_probes, self.project or ".")
            op["result"] = [dict(id=r.get("id"), passed=r.get("passed")) for r in receipts]
        for receipt in receipts:
            try:
                receipt["output_artifact"] = self.store.put(receipt["output"], kind="readiness-output",
                                                            author="harness").id
            except Exception as exc:  # noqa: BLE001 -- recorded, never hidden
                receipt["output_artifact"] = "unavailable"
                receipt["output_artifact_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        self.run_outcome.readiness = receipts
        self._verify_dependencies("during readiness probes")
        if self._source_identity() != before:
            raise PartialWorkStopped("A readiness probe changed project source; it must only observe.")
        failed = [r for r in receipts if not r["passed"]]
        if failed:
            from .readiness import failure_message
            raise CapabilityProbeFailed(failure_message(failed[0]))

    def _policy_denies(self, path: str) -> bool:
        """Whether the repository policy refuses a write to ``path``: its
        deny_write and sensitive sets and the operator's forbidden paths, read
        by the scope check's own matcher (``TaskScope.permits``). A matcher of
        its own here disagreed with it ("secrets" did not cover
        "secrets/key.pem", "**/.env" did not cover a root ".env") and ignored
        ``policy.forbid``, so a task was admitted and then failed on scope
        after a paid lead call (review 1110, session first half 15)."""
        policy = self.config.repository_policy
        if policy is None:
            return False
        if callable(getattr(policy, "scope", None)):
            # The policy's own scope: the denials the post-call check enforces.
            return not policy.scope(None).permits(path)
        # A policy that carries only a document: the same sets, the same matcher.
        try:
            capability = policy.document["capability_policy"] or {}
        except (AttributeError, KeyError, TypeError):
            capability = {}
        denied = list(capability.get("deny_write") or []) + list(getattr(policy, "forbid", None) or ())
        denied += [p for entry in capability.get("sensitive") or [] for p in entry.get("paths") or []]
        return not TaskScope(forbidden_paths=tuple(denied)).permits(path)

    def _live_tier(self, spec) -> str:
        """The tier as the live facts read it: the requested label and the
        admission, recomputed; the contract's value is the one that binds."""
        return "direct" if getattr(spec, "tier", "normal") == "direct" and not self._direct_refusal(spec) else "normal"

    def _live_collaboration(self, spec) -> bool:
        """Design collaboration as the live facts read it: a design task on a
        run with cross-checking, never on the direct tier."""
        return bool(self.config.design_cross_check and is_design_task(spec) and self._live_tier(spec) != "direct")

    def _tier(self, spec) -> str:
        """The current task's tier from its contract, the live reading beside it."""
        return self._required("tier", self._live_tier(spec))

    def _bind_contract(self, owner: str) -> None:
        """Freeze the task's contract with the owner just selected. Called
        once, at the selection point, before any model call for the task."""
        outcome, draft = self._outcome, getattr(self, "_contract", None)
        if outcome is None or draft is None or draft.task_id != outcome.task_id or outcome.contract:
            return
        self._contract = dataclasses.replace(draft, owner=owner)
        outcome.contract = self._contract.to_dict()
        outcome.dispatch = dict(state="dispatched", owner=owner)

    def _open_refs(self, outcome) -> dict:
        """Ledger references this task still owes, read without settling or
        covering anything: RESOLVES findings not resolved, COVERS
        requirements not covered or met."""
        status = self.memory.ledger.requirement_status
        return dict(
            findings=[f["id"] for f in self.findings if f["id"] in outcome.resolves and f["status"] != "resolved"],
            requirements=[rid for rid in outcome.covers
                          if not str(status.get(rid, "")).startswith(("covered", "met"))])

    def _contract_agrees(self, requirement: str, legacy) -> None:
        """Record where a legacy applicability decision disagrees with the
        contract's derived requirement. Observational: the legacy decision
        still decides until phase 3."""
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            return
        want = getattr(contract.required, requirement)
        if want != legacy:
            # Once per distinct note (O-NEXT-11; Sol, 5865650035): the same
            # drift read again adds nothing, a different value does.
            note = f"{requirement}: contract {want!r}, legacy {legacy!r}"
            if note not in outcome.mismatches:
                outcome.mismatches.append(note)

    def _collaboration_applicable(self, spec) -> bool:
        """Whether design collaboration applies to this task, from its
        contract (map P3.4), the live reading recorded beside it."""
        return self._required("design_collaboration_applicable", self._live_collaboration(spec))

    def _required(self, requirement: str, legacy):
        """The current task's own requirement, from the contract fixed at
        dispatch, with ``legacy`` (today's live reading) recorded beside it
        through :meth:`_contract_agrees`. With no contract for this task the
        legacy reading decides, as before, and the missing contract is a
        recorded mismatch, so the run cannot count as complete."""
        self._contract_agrees(requirement, legacy)
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            note = f"{requirement}: contract missing, legacy {legacy!r}"
            if outcome is not None and note not in outcome.mismatches:
                outcome.mismatches.append(note)
            return legacy
        return getattr(contract.required, requirement)

    def _bound_gate(self):
        """The gate the current task's contract was built with, or None when
        there is no contract for the current task."""
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            return None
        return getattr(self, "_task_gate", None)

    def _writes(self) -> bool:
        """Whether this call may write (map P3.4; O-NEXT-10 A, Codex 5865444019):
        a project, the live operator grant and, for a task with a contract,
        its dispatch grant. Dispatch is a ceiling, not an irrevocable grant: a
        later live grant never widens a task, and a live removal is honoured.
        A difference is recorded once. The Fleet's run-level grant still
        applies beneath this. With no contract for the current task the live
        grant decides, as before."""
        live = bool(self.project and self.config.allow_writes)
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            return live
        bound = dict(contract.authority).get("write_grant") == "operator"
        if bound != live:
            note = (f"write_grant: contract {'operator' if bound else 'none'!r}, "
                    f"legacy {'operator' if live else 'none'!r}")
            if note not in outcome.mismatches:
                outcome.mismatches.append(note)
        return bound and live

    def _write_ceiling(self) -> bool:
        """Whether this task could have written: its dispatch grant, or the
        live grant with no contract. Edits already made are held to scope
        against this, not against the grant at close, so a revocation during
        a call does not switch off the scope stop for what that call wrote
        under the grant (O-NEXT-15 F1, 5865760320). No live reading records
        anything here; ``_writes`` does."""
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            return bool(self.project and self.config.allow_writes)
        return bool(self.project) and dict(contract.authority).get("write_grant") == "operator"

    def _cheap_gate(self):
        """The cheap view of the task's own gate, bound at dispatch (map P3.4;
        O-NEXT-10 B, Sol 5865330461), or None when it has no cheap commands.
        A live gate whose cheap view differs is recorded. With no contract for
        the current task the configured gate decides, as before."""
        from .integration import GateSuite

        def view(gate):
            return gate.cheap() if isinstance(gate, GateSuite) else None

        def ids(cheap):
            return tuple(c.id for c in cheap.commands) if cheap is not None else ()
        live = view(self.config.integration_gate)
        outcome, contract = self._outcome, getattr(self, "_contract", None)
        if outcome is None or contract is None or contract.task_id != outcome.task_id:
            cheap = live
        else:
            cheap = view(getattr(self, "_task_gate", None))
            # Whole commands, not ids: a same-id command whose argv or minimum
            # changed is drift too (Sol, 5865462168). The bound one still runs.
            commands = [tuple(g.commands) if g is not None else () for g in (cheap, live)]
            if commands[0] != commands[1]:
                same = " (same ids, configuration differs)" if ids(cheap) == ids(live) else ""
                outcome.mismatches.append(f"cheap_checks: contract {ids(cheap)!r}, legacy {ids(live)!r}{same}")
        return cheap if cheap is not None and cheap.commands else None

    def _source_identity(self) -> str:
        """The selected source's fingerprint, or an explicit reason there is
        none: "n/a" with no project, "unavailable" when it cannot be read."""
        if not self.project:
            return "n/a"
        return self._source_fingerprint() or "unavailable"

    def _record_work(self, outcome: TaskOutcome, measured: Optional[dict]) -> None:
        """What the task left: source identity after, changed paths and lines
        against its start (an explicit uninspected note when that cannot be
        measured, never an implied "no edits"), and the dependency status."""
        try:
            outcome.source_after = self._source_identity()
            if outcome.partial is None:
                if measured is None:
                    # Only a clean close is not a stop: closed_as is set
                    # before this runs, on both the normal and the raising path.
                    stopped = getattr(outcome, "closed_as", "") != "closed"
                    measured = (self._inspect_partial_edits(self._task_before, stopped=stopped) if self.project
                                else dict(changed=[], changed_lines=0, inspected=False,
                                          note="no project: nothing to measure"))
                outcome.partial = {k: measured.get(k) for k in ("changed", "changed_lines", "inspected", "note")
                                   if k in measured}
            watch = self.dependency_watch
            outcome.dependency = watch.record.get("status") if watch is not None else "n/a"
        except Exception as exc:  # noqa: BLE001 -- observation never fails a task
            outcome.partial = outcome.partial or dict(inspected=False, note=f"unmeasured: {type(exc).__name__}")

    def _stage(self, name: str) -> None:
        self._activity('stage', stage=name, task=getattr(self._active_spec, 'task_id', 'run'))
        if self._outcome is not None:
            self._outcome.stage(name)

    def _edge(self, name: str, satisfied) -> None:
        if self._outcome is not None:
            self._outcome.edge(name, satisfied)

    def _count(self, attempt: str) -> None:
        if self._outcome is not None:
            self._outcome.count(attempt)

    def _prepare_dispatch(self, spec: TaskSpec) -> None:
        """This task's own scope and gate, settled before its contract is
        built (map P3.4): the contract then describes the gate and scope the
        task runs under, not the previous task's. Declaration and policy
        refusals raise here exactly as before; nothing is run and no model is
        called."""
        if self.project and self.config.allow_writes and spec.scope is None:
            raise RunStalled("Editing tasks must declare a scope before dispatch.")
        policy = self.config.repository_policy
        if policy is not None:
            from .policy import PolicyError, task_gate
            paths = spec.scope.permitted_paths if spec.scope else ()
            plan = policy.resolve(paths, writing=self.config.allow_writes)
            self.policy_plans.append(dict(task_id=spec.task_id, **plan))
            self._note(f"Family: {plan['primary_family']}; plan {plan['hash']}")
            if plan['blocked']:
                raise PolicyError('; '.join(plan['blocked']))
            if (self.config.allow_writes and self.config.default_scope
                    and any(not self.config.default_scope.permits(p) for p in paths)):
                raise PolicyError('Task declaration exceeds the operator path limits')
            spec.scope = policy.scope(spec.scope)
            self.config.integration_gate = task_gate(policy, plan, self._original_gate,
                                                     exclude=self.config.project_excludes)
        # Every packet this task's calls will carry, built before the first of
        # them: run 20261010T135407Z-f3d33e9d spent its planning calls, then
        # the lead's packet was refused. Same builder, and the task's own
        # allowance: the policy's default or what the orchestrator granted.
        for role in self._packet_roles(spec):
            self._role_packet(spec, role)
        self._packet_record_dispatch(spec)
        self._dispatch_prepared = spec.task_id

    def _unmark_not_started(self, spec: TaskSpec, decision: dict, *, why: str) -> None:
        """Undo the dispatch marks the serial loop set for a task that never
        began (cycle21): its survey recovery charge, its listed-task ran mark
        and its DISCHARGES takeover, and record it not started. Only for a
        refusal before the task's admission; started work keeps every mark."""
        if self.config.survey is not None and decision.get("charged"):
            self.survey["recovery_used"] = max(0, int(self.survey.get("recovery_used", 0) or 0) - 1)
            decision["charged"] = False
            decision["not_started"] = True
        if self._explicit_tasks is not None:
            ran = self.explicit.get("ran") or []
            self.explicit["ran"] = [r for r in ran if r.get("task") != spec.task_id]
        for entry in self.unexecuted_acceptance:
            if entry.get("taken_over_by"):
                entry["taken_over_by"] = [t for t in entry["taken_over_by"] if t.get("task") != spec.task_id]
        self.not_started.append(dict(task=spec.task_id, lead=getattr(spec, "lead", None),
                                     description=(spec.description or "")[:300], detail=str(why)[:300]))

    def _scope_request(self) -> str:
        """The SCOPE rules with the role packet bound this run enforces
        (cycle21: the prompt said "well under 20,000 bytes" while the policy's
        packet limit was 12,000). The limit is the policy's own, as the packet
        builder applies it, with its mandatory overhead measured by the same
        builder; without a policy, the fallback packet's limit."""
        policy = self.config.repository_policy
        if policy is None:
            return (_SCOPE_REQUEST + f" The scope as rendered goes into each role packet, whose limit here is "
                    f"{_FALLBACK_PACKET_BYTES:,} bytes; a task whose packet would not fit is refused.")
        limit, overhead = policy.packet_limit, policy.packet_overhead("lead")
        return (_SCOPE_REQUEST + f" The scope as rendered (intended result, paths, acceptance and every proof "
                f"scenario, in UTF-8 bytes) goes into each role packet, whose limit is {limit:,} bytes; the "
                f"policy's own checklists and check configuration already take about {overhead:,} of them, so "
                f"the rendered scope must stay under about {max(0, limit - overhead):,} bytes. A task whose packet "
                "would not fit is sent back with its measured size, before anything runs: narrow it or state its "
                "scenarios more briefly, keeping every covered requirement's scenario. When the content is "
                "necessary as declared, its lead is asked once whether a larger allowance for that task is justified, "
                f"up to this run's ceiling of {self._packet_ceiling()[0]:,} bytes; a justified request comes back to "
                "you to decide by its request id, and only your own 'PACKET GRANT: <id> <bytes> | because: <reason>' "
                "line grants it ('PACKET REFUSE: <id> | because: <reason>' refuses it). A grant covers that task's "
                "scope exactly as measured.")

    def _path_case_problem(self, spec: TaskSpec) -> str:
        """A SCOPE path that differs only in letter case from an existing
        project path, or "" (cycle21, run 20261109T195618Z: t7 named
        ``readme.md`` while the file is ``README.md``, and the merge refused
        the write only after the task was spent). Names are compared against
        the directory entries as stored, so a case-insensitive filesystem that
        resolves ``readme.md`` still shows ``README.md``. A glob component
        ends the walk, a symbolic link is never followed, a path that does not
        exist yet is a new file, and nothing is corrected: the planner names
        the path again. An existing spelling in a path the task may not name
        (an exclusion or a forbidden path) is not disclosed."""
        scope = getattr(spec, "scope", None)
        if not self.project or scope is None or not scope.permitted_paths:
            return ""
        policy = self.config.repository_policy
        prepared = policy.scope(scope) if policy is not None else scope
        hidden = list(prepared.forbidden_paths) + list(self.config.project_excludes or ())
        root = Path(self.project)
        problems = []
        for declared in scope.permitted_paths:
            found = _case_collision(root, declared)
            if found is None:
                continue
            if any(_scope_matches(found, pattern) for pattern in hidden):
                problems.append(f"`{declared}` differs only in letter case from an existing path this task may "
                                "not name")
            else:
                problems.append(f"`{declared}` differs only in letter case from the existing `{found}`")
        if not problems:
            return ""
        return ("SCOPE paths are exact, and " + "; ".join(problems) + ". Name the existing spelling, or a "
                "different path for a new file; nothing is corrected for you.")

    # -- orchestration -------------------------------------------------------
    def _kind_request(self) -> str:
        """The labelling instruction: the orchestrator labels, unless a decider
        is configured to label everything (SessionConfig.decider_labels)."""
        if self.config.decider is not None and self.config.decider_labels == "all":
            return _DECIDER_LABELS_REQUEST
        if self.config.decider is not None:
            # The decider answers what was left unlabelled, so "none fits"
            # is its question, not a forced "general".
            return _kind_request_text(decider=True)
        return _KIND_REQUEST

    def _survey_request(self) -> str:
        """The HYPOTHESIS instruction, on a survey run after an unfinished
        task or a task that left a finding."""
        if self.config.survey is None or not self._survey_after_failure():
            return ""
        left = max(0, self.config.survey.recovery_tasks - self.survey["recovery_used"])
        return "\n\n" + _HYPOTHESIS_REQUEST.format(left=left)

    def _survey_after_failure(self) -> bool:
        """Whether the last closed task was unfinished or left a finding."""
        if not self.history:
            return False
        last = self.history[-1]
        if getattr(last, "outcome", "closed") in ("turn_limited", "failed"):
            return True
        return any(f["task"] == last.task_id and f["status"] == "open" for f in self.findings)

    def _content_fingerprint(self, paths) -> Dict[str, str]:
        """sha256 of each changed path's content as the task left it (missing
        for a deleted file), so a repeat is judged on content, never on
        filenames alone (Codex on the survey design, 2026-09-30)."""
        if not self.project:
            return {}
        import hashlib
        root = Path(self.project)
        out = {}
        for path in paths:
            try:
                out[path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
            except OSError:
                out[path] = "missing"
        return out

    def _survey_repeat(self, spec, continues, unfinished: str) -> Optional[str]:
        """Why this unfinished continuation is a same-cause repeat of the task
        it continues, or None. A repeat is the same cause (cap, or the
        failure's cause), the same error (its first line), and no new
        evidence: every file the continuation changed holds the content its
        predecessor left, judged by content hash and task lineage, never by
        filenames alone."""
        if not continues:
            return None
        def record(tid):
            return self.turn_limited_records.get(tid) or self.failed_records.get(tid)
        before, after = record(continues), record(spec.task_id)
        if not before or not after:
            return None
        cause_before = before.get("cause") or "cap"
        cause_after = after.get("cause") or "cap"
        if cause_before != cause_after:
            return None
        def head(rec):
            # The harness's own signature: a failure's first error line, or
            # for a cap the fact of the cap and its round count; never the
            # model's progress prose (Codex review of 1995d02).
            if rec.get("cause"):
                return (rec.get("error") or "").strip().splitlines()[:1]
            return [f"turn limit after {rec.get('turns') or '?'} rounds"]
        if head(before) != head(after):
            return None
        earlier, later = before.get("content") or {}, after.get("content") or {}
        if any(earlier.get(path) != digest for path, digest in later.items()):
            return None
        return (f"{spec.task_id} continued {continues} and failed the same way ({cause_after}: "
                f"{' '.join(head(after))[:120]}) with no new content in the files it changed")

    def _operator_limits_note(self) -> str:
        """The operator's path limits and the repository policy's denied paths,
        stated to the orchestrator: a declaration outside them was refused at
        dispatch by a rule it had never been shown."""
        lines = []
        limit = self.config.default_scope
        if limit is not None and self.config.allow_writes:
            lines.append("Every permitted path must fall under the operator's limits: "
                         + ", ".join(limit.permitted_paths) + ".")
        policy = self.config.repository_policy
        denied = []
        try:
            capability = policy.document["capability_policy"] if policy is not None else None
            if capability:
                denied = list(capability.get("deny_write") or [])
                denied += [p for entry in capability.get("sensitive") or [] for p in entry.get("paths") or []]
        except (AttributeError, KeyError, TypeError):
            denied = []
        if denied and self.config.allow_writes:
            lines.append("The repository policy refuses writes under: " + ", ".join(denied[:20]) + ".")
        return ("\n\n" + " ".join(lines)) if lines else ""

    def _next_explicit_task(self) -> Optional[TaskSpec]:
        """The next operator-listed task, read by the orchestrator reply's own
        parsers with no model call, or None when the list is spent. What the
        loop would send back for correction stops the run here instead."""
        if not self._explicit_tasks:
            self._note("the task list is spent")
            return None
        index = self.explicit["listed"] - len(self._explicit_tasks) + 1
        self._explicit_index = index
        text = self._explicit_tasks.pop(0)
        task_id = self._next_task_id()
        self._note(f"listed task {index} of {self.explicit['listed']} runs as {task_id}")
        spec, problem = self._read_listed(self._absorb_requirements(text), task_id)
        if spec is None:
            self.explicit["invalid"] = dict(index=index, problem=str(problem)[:400])
            raise TaskListInvalid(f"listed task {index}: {problem}")
        return spec

    @staticmethod
    def _split_requirements(reply: str):
        """``(found, rest)``: the REQUIREMENTS block of a reply and the reply
        without it or its DECIDE lines. The reading ``_absorb_requirements``
        applies, with nothing absorbed, so the pre-launch list check strips
        exactly what the loop strips."""
        reply = _DECIDE.sub("", reply or "").strip() or reply
        found, rest = _read_requirements(reply)
        return found, (rest if found else reply)

    def _read_listed(self, reply: str, task_id: str, *, dry_run: bool = False):
        """One operator-listed text, already without its REQUIREMENTS block,
        read into its TaskSpec by the orchestrator reply's own parsers.
        Returns ``(spec, "")``, or ``(None, problem)`` for a text the loop
        would send back.

        One parse for the task's own turn and for the pre-launch check
        (``dry_run``), which reads with nothing absorbed into the map, no
        decider asked and no note written. The check used to stop short of the
        TaskSpec, so a bad NEEDS line, an empty description or a PARALLEL
        block in a later text was found only after the earlier tasks had run
        (review 1110, session first half 10)."""
        control = parse_control(reply)
        if control is not None:
            return None, f"a {control.verb} reply is not a task"
        if self._parallel_enabled() and _parallel_blocks(reply):
            return None, "a parallel batch is not a listed task; list each task on its own"
        try:
            meta = _read_metadata(reply)
        except AmbiguousMetadata as exc:
            return None, f"{exc}; state one 'KIND: <kind> <difficulty>' line first"
        if dry_run:
            # MAP NOTES leave the description as on the real pass; the notes
            # themselves reach the map only when the task runs.
            notes, rest = _read_task_orientation(meta.description)
            meta = replace(meta, description=rest) if notes else meta
        else:
            meta = self._route_with_decider(self._absorb_orientation(meta, "operator"), task_id=task_id)
        scope = self.config.default_scope
        if self.project and self.config.allow_writes:
            from .scope import read_scope
            try:
                scope, description = read_scope(meta.description)
            except ValueError as exc:
                return None, f"invalid scope: {exc}"
        else:
            description = meta.description.strip()
        if not description:
            return None, "no task description"
        if meta.defaulted and not dry_run:
            self._note(f"task metadata was not stated; routing as {meta.render()}")
        try:
            spec = TaskSpec(task_id=task_id, description=description, kind=meta.kind,
                            complexity=meta.difficulty, metadata_confidence=meta.confidence,
                            metadata_notes=list(meta.notes), scope=scope)
        except ValueError as exc:
            return None, f"invalid task requirements: {exc}"
        return spec, ""

    def _task_list_problems(self, listed: List[str]) -> List[tuple]:
        """What the loop would refuse in an explicit list, found by the same
        parse before anything runs (``_read_listed``, as a dry run): control
        replies, parallel blocks, unlabelled or unscoped texts, an empty
        description, a bad NEEDS line, and, with the ledger on, a first text
        without a REQUIREMENTS block or any text without a COVERS line naming
        listed ids. Deterministic and call-free; the loop's own checks still
        run."""
        problems: List[tuple] = []
        requirements: Dict[str, str] = {}
        for index, text in enumerate(listed, 1):
            found, rest = self._split_requirements(text)
            if found and not requirements:
                requirements = dict(found)
            elif found:
                problems.append((index, "REQUIREMENTS may be listed once, on the first text"))
            spec, problem = self._read_listed(rest, self._next_task_id(index - 1), dry_run=True)
            if spec is None:
                problems.append((index, problem))
                continue
            if self.config.requirements_ledger:
                covers, _ = _read_covers(spec)
                if not requirements:
                    problems.append((index, "No requirements are listed yet. Start the first text with a "
                                            "'REQUIREMENTS:' block numbering the goal's requirements (R1: ...)"))
                elif not covers:
                    problems.append((index, "The task has no COVERS line"))
                else:
                    unknown = [r for r in covers if r not in requirements]
                    if unknown:
                        problems.append((index, f"COVERS names requirements that do not exist: {', '.join(unknown)}"))
        return problems

    def _explicit_list_spent(self) -> bool:
        """An explicit run whose list is spent and whose completion has not
        been decided: one more loop turn reads the list's end as the DONE
        site and always breaks."""
        return self._explicit_tasks is not None and not self._explicit_tasks and not self.stop_reason \
            and not self.completed

    def _refuse_listed(self, spec, problem: str) -> None:
        """A send-back on a listed task is a stop: there is no orchestrator
        to correct it."""
        if self._explicit_tasks is None:
            return
        index = getattr(self, "_explicit_index", None)
        self.explicit["invalid"] = dict(index=index, problem=str(problem)[:400])
        raise TaskListInvalid(f"listed task {index}: {problem}")
    def _route_with_decider(self, meta, task_id: Optional[str] = None):
        """Ask the configured decider for kind and difficulty when the
        orchestrator stated none (a defaulted or degraded route). A stated
        label is never overridden. A refusal or an out-of-set answer keeps
        the rule's default and records why; nothing here raises."""
        decider = self.config.decider
        if decider is None:
            return meta
        # Each field on its own provenance (Codex review of #44): a stated
        # kind beside a defaulted difficulty, or the reverse, delegates only
        # the field nobody stated.
        wanted = [(key, attr, answers) for key, attr, answers, stated in (
            ("task.kind", "kind", tuple(ROUTING), getattr(meta, "kind_stated", not meta.defaulted)),
            ("task.difficulty", "difficulty", tuple(Complexity._COLLABORATORS),
             getattr(meta, "difficulty_stated", not meta.defaulted))) if not stated]
        if not wanted:
            return meta
        from .decisions import DecisionsUnavailable, describe
        task_id = task_id or self._next_task_id()
        context = (meta.description or "")[:8000]
        updates, notes = {}, list(meta.notes)
        for key, attr, answers in wanted:
            # Definitions, guidance and their version ride with the question
            # (decision_labels): the first live run sent bare label names.
            decision = describe(key, answers, context=context)
            record = dict(task=task_id, decision=key, default=getattr(meta, attr), labels=decision.labels)
            # A routing decision is an effect like any other (a billed call
            # when the decider is Jev): the authority it runs under is
            # committed first and named by its reservation and its
            # operation, and the decision given is the operation's receipt
            # (adversarial review of e688af5). An out-of-set answer is an
            # outcome the harness knows, settled as such, never uncertain.
            authority = self._authority_before("routing decision")
            try:
                with invocation(task_id, "decide", authority=authority), self._operation(
                        "routing decision", inputs=dict(task=task_id, decision=key, labels=decision.labels,
                                                        context=context)) as op:
                    try:
                        verdict = decider.decide(decision)
                    except ValueError as exc:
                        op["result"] = dict(decision=key, refused=f"ValueError: {str(exc)[:300]}")
                        raise DecisionsUnavailable(f"out-of-set answer: {exc}") from exc
                    op["result"] = dict(decision=key, answer=verdict.answer, source=verdict.source,
                                        confidence=verdict.confidence)
            except (DecisionsUnavailable, ValueError) as exc:
                record.update(answer=None, source=None, error=str(exc)[:300])
                notes.append(f"{attr}: the decider refused ({str(exc)[:120]}); default kept")
            else:
                record.update(answer=verdict.answer, source=verdict.source,
                              confidence=verdict.confidence, probabilities=verdict.note,
                              usage=verdict.usage)
                updates[attr] = verdict.answer
                notes.append(f"{attr} {verdict.answer!r} decided by {verdict.source}"
                             + (f" (confidence {verdict.confidence:.2f})" if verdict.confidence is not None else ""))
            self.decisions.append(record)
            self._note(f"routing decision {key} for {task_id}: "
                       + (record.get("answer") or f"refused, default {record['default']}"))
        if not updates:
            return replace(meta, notes=notes)
        return replace(meta, confidence="decided", notes=notes,
                       kind_stated=meta.kind_stated or "kind" in updates,
                       difficulty_stated=meta.difficulty_stated or "difficulty" in updates, **updates)

    def next_task(self) -> Optional[TaskSpec]:
        """Ask the orchestrator what to do next, given the ledger.

        Returns None when it reports the goal met. The orchestrator sees
        summaries with pointers, so if the decision turns on a detail a summary
        skipped it can fetch the original rather than guess.

        The orchestrator may also reply ``ASK: <question>`` when the decision
        turns on something only the operator knows -- scope, taste, a business
        constraint no artifact can settle. The answer is recorded as a
        standing ruling and re-emitted on every render, so a question is never
        asked twice, and the orchestrator is re-prompted with the ruling in
        hand. Bounded, so a confused orchestrator cannot interrogate the
        operator in a loop.

        Raises:
            OperatorInputNeeded: on an ASK when no ``ask_operator`` channel is
                configured. Guessing an answer the orchestrator explicitly
                flagged as operator-only would defeat the point of asking.
        """
        self._planning_decision = True
        try:
            return self._next_task_decision()
        finally:
            self._planning_decision = False

    def _next_task_id(self, offset: int = 0) -> str:
        """The id the next dispatched task gets; ``offset`` counts on for the
        later blocks of a batch. Read from the run's own counter, never from
        the history (review 1110, session second half, item 2): an unmerged
        parallel child has no history entry and its id is still spent. Naming
        a task does not spend an id, so a task sent back and named again keeps
        the id the orchestrator was told; dispatch spends it."""
        return f"t{self._ids_issued + 1 + offset}"

    def _issue_task_id(self, task_id: str) -> None:
        """Spend ``task_id``: the counter moves past it and never comes back,
        whether the task closes, fails, raises or is not merged."""
        match = re.fullmatch(r"t(\d+)", task_id or "")
        self._ids_issued = max(self._ids_issued + 1, int(match.group(1)) if match else 0)

    @staticmethod
    def _task_number(task_id: str) -> str:
        """``t7`` as ``7`` for the progress notes, which the monitor reads
        back as a task id; any other id as written."""
        return task_id[1:] if re.fullmatch(r"t\d+", task_id or "") else str(task_id)

    def _read_directives(self, spec, *, batch: bool = False) -> _Intake:
        """Read the directive lines off one named task and say what, if
        anything, refuses it before a lead call is spent.

        The one reading for a serial task and for each block of a parallel
        batch (review 1110, session second half, item 15: the batch kept its
        own copy and drifted, so TIER and HYPOTHESIS lines stayed in the text
        its lead recited and three checks never ran). In order: the same task
        named again, the lines themselves (CONTINUES, TIER, HYPOTHESIS,
        COVERS, RESOLVES), then COVERS, RESOLVES, the verification hand-off
        and the capture declaration. The caller words and counts the
        send-back; nothing here does.

        ``batch``: a forked child has no ledger to settle a finding in, no
        check of its own to finish a capped task's or to admit the direct
        tier on, and no preview to capture from. A block that asks for one of
        those is refused here rather than run without it.
        """
        if _repeat_key(spec) in self._named_last:
            raise RunStalled(
                f"the orchestrator named the same task twice in a row: "
                f"{spec.description!r}. The last close-out did not move "
                f"its view forward. Improve the summary, narrow the goal, "
                f"or intervene before re-running."
            )
        if not batch:
            self._note(
                f"task {self._task_number(spec.task_id)}: {spec.description} "
                f"[{spec.kind}/{spec.complexity}]"
            )
        continues, spec = _read_continues(spec)
        requested_tier, spec = _read_tier(spec)
        spec = replace(spec, tier=requested_tier)
        hypothesis, spec = _read_hypothesis(spec)
        if self.config.survey is not None and (hypothesis is not None or self._survey_after_failure()):
            # Diagnostic text, never a rule the reply can fail on (Codex on
            # the survey design): an omitted line is recorded as omitted.
            self.survey["hypotheses"].append(dict(
                task=spec.task_id, after=self.history[-1].task_id if self.history else None,
                text=hypothesis[:600] if hypothesis else None))
            if hypothesis:
                self.store.put(hypothesis, kind="hypothesis",
                               author="operator" if self._explicit_tasks is not None else self.seat().key)
            self._note(f"hypothesis for {spec.task_id}: {(hypothesis or 'none stated')[:160]}")
        covers, spec = _read_covers(spec)
        lines = len(_RESOLVES.findall(spec.description or ""))
        resolves, spec = _read_resolves(spec)
        intake = _Intake(spec=spec, continues=continues, covers=covers, resolves=resolves, hypothesis=hypothesis)
        if batch:
            problem = ""
            if resolves is not None:
                # Findings are resolved by one serial task with its own
                # verified renders; a forked child has no ledger to close them.
                problem = "RESOLVES is not supported in a parallel batch; name that task alone."
            elif continues:
                # A continuation takes over the capped task's debt, its
                # required check included, and a child runs none: such a run
                # ended CompletionUnproven after the batch had been paid for.
                problem = "CONTINUES is not supported in a parallel batch; name that task alone."
            elif requested_tier == "direct" and self.config.direct_tier:
                # Admission needs a check for the task itself; a child has
                # none (the gate runs once, on the merged tree), so the label
                # would be refused for a reason that is the batch's, not the
                # task's. With the tier off the label is refused and recorded
                # in the child exactly as a serial task's is.
                problem = ("TIER: direct is not supported in a parallel batch; name that task alone, "
                           "or drop the line to run it in the batch with review.")
            elif self.config.capture_profile is not None and is_design_task(spec):
                problem = "A UI task with harness capture is not supported in a parallel batch; name it alone."
            if problem:
                return replace(intake, refused="batch", problem=problem)
        problem = self._covers_problem(covers)
        if problem:
            return replace(intake, refused="covers", problem=problem)
        problem = (self._resolves_problem(resolves, covers, spec) if lines <= 1 else
                   "RESOLVES appears on more than one line; name every finding on one line.")
        if problem:
            return replace(intake, refused="resolves", problem=problem)
        handoff = self._verification_handoff(spec, covers, resolves, continues)
        if handoff is not None:
            return replace(intake, refused="handoff", problem=handoff)
        problem = self._capture_problem(spec, resolves)
        if problem:
            return replace(intake, refused="capture", problem=problem)
        return intake
