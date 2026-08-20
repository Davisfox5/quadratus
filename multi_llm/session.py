"""The run loop: how a task moves through the architecture.

One cycle:

1. The orchestrator reads its ledger and names the next task.
2. A brain-trust member leads it, with collaborators drawn in according to the
   task's complexity.
3. The lead works with full memory for the duration, commissioning worker bees
   for anything it needs fetched, read, or checked.
4. The task closes. The lead emits one summary, with reasoning, dead ends, and
   pointers to the full work. Everything else it accumulated is dropped.
5. The orchestrator absorbs that summary and picks the next task.

Model calls are injected as ``invoke`` rather than constructed here, so the
loop can be driven by real CLI providers in production and by a fake in tests
without either knowing about the other.

The orchestrator is asked for a decision, never for a fact it could look up:
it names the next task and rules on questions, and the harness does the
bookkeeping. That keeps the expensive, hard-dependency model out of work that
does not need judgement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .artifacts import ArtifactStore
from .codebase_map import CodebaseMap
from .memory import PersistentMemory, TaskMemory, TaskSummary
from .registry import peers_for, resolve
from .routing import (
    Seat,
    WorkClass,
    close_excursion,
    cross_family_verifier,
    open_security_excursion,
    orchestrator_seat,
)
from .task_kinds import MAX_TASK_LINES, ROUTING, TaskKind, guidance_for, policy_for
from .task_kinds import route as route_kind
from .usage import UsageMeter
from .workers import WorkerBudget, WorkerPool, worker_menu

log = logging.getLogger(__name__)

__all__ = [
    "Complexity",
    "TaskSpec",
    "Session",
    "SessionConfig",
    "RunStalled",
    "OperatorInputNeeded",
]


class OperatorInputNeeded(RuntimeError):
    """The orchestrator asked a question only the operator can answer.

    Raised when no ``ask_operator`` channel is configured. Deliberately not
    swallowed: a system that guesses the answer to a question it explicitly
    flagged as operator-only has defeated the point of asking. The question
    is in ``args[0]``; answer it, add it to the session's rulings, and resume.
    """


class RunStalled(RuntimeError):
    """The orchestrator named the same task twice in a row.

    Two identical consecutive decompositions mean the ledger is not moving the
    orchestrator's view forward -- the loop has stopped converging and every
    further round would spend the subscription window re-running a known
    outcome. Raised rather than silently continued or silently stopped: the
    operator should see that the run stalled and why, because the fix (a
    better close-out, a narrower goal, a fetched artifact) is theirs to pick.
    """


#: Emitted on every decomposition prompt.
#:
#: This is the highest-value instruction in the whole loop, and it is about
#: size rather than about quality. Review F1 measured 0.657 on diffs under ten
#: lines and 0.043 on diffs over a hundred and fifty -- an order of magnitude,
#: which is far wider than the gap between any two reviewers we could pick
#: between. A task scoped too large has therefore already lost most of the
#: review that was supposed to catch its defects, and no downstream choice
#: recovers it. Splitting is the only intervention that works.
_SIZE_CEILING = (
    f"Size the task so it produces at most ~{MAX_TASK_LINES} lines of diff. If "
    f"the obvious next step is bigger than that, name the first slice of it "
    f"instead and leave the rest for the next round. Review quality falls by "
    f"roughly an order of magnitude across this threshold, so a task scoped "
    f"too large is one whose defects will not be found."
)


class Complexity:
    """How hard a task is. Drives two decisions at once.

    First, who leads: difficulty maps onto the brain trust as a ladder (see
    :data:`multi_llm.task_kinds.DIFFICULTY_LADDER`), so the hardest work gets
    the strongest model and the bulk of the work lands on the subscriptions
    with capacity to spare. Second, how many collaborators the task draws:
    each one is another full invocation and another voice in the lead's
    working memory, so easy work is not made expensive by ceremony.
    """

    ROTE = "rote"          # mechanical; lead alone
    SIMPLE = "simple"      # the bulk of well-sized tasks; lead alone
    STANDARD = "standard"  # lead + one collaborator
    COMPLEX = "complex"    # many logical steps; lead + the whole brain trust

    _COLLABORATORS = {ROTE: 0, SIMPLE: 0, STANDARD: 1, COMPLEX: None}

    @classmethod
    def collaborator_count(cls, complexity: str, available: int) -> int:
        if complexity not in cls._COLLABORATORS:
            complexity = cls.STANDARD
        wanted = cls._COLLABORATORS[complexity]
        return available if wanted is None else min(wanted, available)


@dataclass
class TaskSpec:
    """One unit of work, as named by the orchestrator."""

    task_id: str
    description: str
    complexity: str = Complexity.STANDARD
    work_class: str = WorkClass.GENERAL
    #: What kind of work this is, which may pin the lead. See
    #: :mod:`multi_llm.task_kinds`; most kinds express no preference and rotate.
    kind: str = TaskKind.GENERAL
    #: Force a particular lead. Normally left to rotation.
    lead: Optional[str] = None

    def __post_init__(self) -> None:
        # ``kind`` and ``work_class`` grew up in different modules and both can
        # say "security". Keeping them synchronised here means no caller can
        # construct a task that one security mechanism sees and the other
        # misses -- which is exactly the bug this guards against: the excursion
        # machinery watching work_class while the orchestrator's label only
        # ever set kind.
        if self.kind == TaskKind.SECURITY:
            self.work_class = WorkClass.SECURITY
        elif self.work_class == WorkClass.SECURITY and self.kind == TaskKind.GENERAL:
            self.kind = TaskKind.SECURITY


@dataclass
class SessionConfig:
    mode: str = "adversarial"
    worker_budget: WorkerBudget = field(default_factory=WorkerBudget)
    #: Render only the last N ledger entries into the orchestrator's prompt.
    #: Earlier entries and every artifact stay reachable; this narrows the
    #: view rather than discarding anything.
    recent_entries: Optional[int] = None
    #: Cross-session memory about the repository itself. Rendered into every
    #: orchestrator and lead prompt, and amended from task close-outs.
    codebase_map: Optional[CodebaseMap] = None
    #: When set, ``run()`` first asks the orchestrator for the full expected
    #: task list and hands it to this callable. False aborts before any task
    #: executes -- the operator reviews the plan at the moment it is cheap to
    #: change, not after the window is spent. None runs ungated.
    plan_gate: Optional[Callable[[str], bool]] = None
    #: Records every invocation with its API-price counterfactual. Purely
    #: observational: metering failure never fails a run.
    usage_meter: Optional["UsageMeter"] = None
    #: How the system reaches the operator when only they can answer -- the
    #: orchestrator emits ``ASK: <question>`` instead of a task, and this
    #: callable returns the answer, which is recorded as a standing ruling.
    #: None means an ASK raises :class:`OperatorInputNeeded` so the caller can
    #: collect the answer and resume, rather than the question being guessed.
    ask_operator: Optional[Callable[[str], str]] = None
    #: How many artifact fetches one decision or draft may spend. A model that
    #: replies with only ``FETCH: <artifact-id>`` gets the full original and
    #: is re-asked -- the request loop behind "a summary is an index".
    max_fetches: int = 3
    #: How many peer consults a lead may spend per task. A consult is one
    #: bounded question to one named brain-trust member, answered blind --
    #: cross-expertise input where family escalation cannot help. Small on
    #: purpose: two questions is a consult, more is a conversation.
    max_consults: int = 2
    #: Runs the project's own check command after a task's work is final --
    #: the deterministic answer to "do the pieces actually fit together".
    #: Anything with a ``run() -> GateResult`` shape works; see
    #: :class:`multi_llm.integration.IntegrationGate`. None skips the gate.
    integration_gate: Optional[object] = None
    #: How many fix rounds a failing integration gate buys the lead before
    #: the failure is carried into the record as an open problem.
    max_gate_fixes: int = 1
    #: How many lead revisions a task may spend answering blocking findings.
    #: The count is deliberately small and the loop deliberately narrow --
    #: each extra cycle is a reviewer re-checking its own named findings
    #: against the revision, never a fresh round of open debate. The measured
    #: failure of unguided multi-round debate is conformity, not shortage of
    #: rounds; the measured success case for iteration is external feedback on
    #: a concrete defect, which is exactly and only what this loop carries.
    max_fix_cycles: int = 2


class Session:
    """A run: one orchestrator, one brain trust, many tasks."""

    def __init__(
        self,
        goal: str,
        store: ArtifactStore,
        invoke: Callable[..., str],
        *,
        config: Optional[SessionConfig] = None,
        invariants: Optional[Sequence[str]] = None,
        available: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.config = config or SessionConfig()
        self.store = store
        if self.config.usage_meter is not None:
            invoke = self.config.usage_meter.wrap(invoke)
        self.invoke = invoke
        self._available = available or (lambda _key: True)
        self.memory = PersistentMemory(goal, store, invariants=invariants)
        self.workers = WorkerPool(
            store=store,
            run=lambda model, prompt: self.invoke(model, prompt),
            budget=self.config.worker_budget,
        )
        self._rotation = 0
        self.history: List[TaskSummary] = []

    # -- seating -------------------------------------------------------------
    def seat(self, *, security: bool = False) -> Seat:
        """Who orchestrates this segment. Raises if the primary is gone."""
        return orchestrator_seat(security_segment=security, available=self._available)

    @property
    def brain_trust(self) -> List[str]:
        return peers_for(self.config.mode, orchestrator_seat().key)

    def _pick_lead(self, spec: TaskSpec) -> str:
        """Rotate the lead across the brain trust, then let the task kind speak.

        Rotation spreads load across separate subscription windows and, as a
        side effect, produces the comparison data that tells you which model
        actually leads best on your work -- exploration at no extra cost. That
        exploration is worth keeping, so a task kind only overrides it where
        there is measured reason to; most kinds express no preference and the
        rotation stands. See :mod:`multi_llm.task_kinds`.

        The rotation counter advances either way. If a pinned kind consumed a
        turn without advancing it, one model would be pinned for its own kind
        *and* keep its place in the general queue, which would skew the
        scoreboard the rotation exists to fill.
        """
        if spec.lead:
            return spec.lead
        trust = self.brain_trust
        rotated = trust[self._rotation % len(trust)]
        self._rotation += 1
        return route_kind(
            spec.kind,
            difficulty=spec.complexity,
            default=rotated,
            candidates=trust,
            available=self._available,
        )

    def collaborators_for(self, spec: TaskSpec, lead: str) -> List[str]:
        """Which other peers help with this task.

        Complexity sets the count, with one exception: review work always draws
        its counterpart reviewer, even at SIMPLE. The two pinned reviewers were
        chosen because they fail in opposite directions -- one catches ~70% of
        known issues but only ~32% of its comments are worth keeping, the other
        is the mirror image -- so a single reviewer is not a cheaper review, it
        is a review missing half its coverage. Buying that back is the one
        place an extra invocation is not optional.
        """
        # A collaborator that is down is not a collaborator; skipping it here
        # beats failing the task mid-flight when its invocation errors.
        others = [p for p in self.brain_trust if p != lead and self._available(p)]
        chosen = others[: Complexity.collaborator_count(spec.complexity, len(others))]
        if spec.kind == TaskKind.REVIEW:
            for peer in policy_for(TaskKind.REVIEW).prefer:
                if peer != lead and peer in others and peer not in chosen:
                    chosen.append(peer)
        return chosen

    # -- request channels ----------------------------------------------------
    def _invoke_with_fetches(self, model_key: str, build_prompt, *, task=None) -> str:
        """Invoke, serving artifact requests until a real answer arrives.

        ``build_prompt`` takes the fetched (id, content) pairs gathered so far
        and returns the full prompt, so each layer can splice the material in
        at its own right position. Bounded by ``max_fetches``; an unknown id
        comes back as a correction rather than an error, because the model can
        fix a typo and the harness cannot.
        """
        fetched: List[tuple] = []
        reply = self.invoke(model_key, build_prompt(fetched))
        for _ in range(self.config.max_fetches):
            artifact_id = _parse_fetch(reply)
            if artifact_id is None:
                break
            try:
                content = self.memory.fetch(artifact_id)
            except KeyError:
                content = f"(no artifact {artifact_id!r} exists -- check the id)"
            fetched.append((artifact_id, content))
            if task is not None:
                task.record("assistant", f"[fetched artifact {artifact_id}]")
            reply = self.invoke(model_key, build_prompt(fetched))
        return reply

    def _resolve_consultant(self, name: str, lead: str) -> Optional[str]:
        """Match a consult request to a brain-trust member, forgivingly.

        Leads say 'Sol' or 'Opus 5' or a full key; all resolve. The lead
        itself and anyone outside the brain trust do not -- a consult is
        cross-expertise input from a peer, not an arbitrary summons.
        """
        want = name.strip().lower()
        for key in self.brain_trust:
            if key == lead:
                continue
            spec = resolve(key)
            if want == key.lower() or (
                spec is not None
                and (want == spec.alias.lower() or want in spec.label.lower())
            ):
                return key
        return None

    def _draft_with_channels(self, lead: str, spec: TaskSpec, task: TaskMemory) -> str:
        """The lead drafts, with the fetch and consult channels live.

        A reply that is a request gets served and the lead re-asked; a reply
        that is work is the draft. Consults are answered blind -- the
        consultant sees the task and the question, never the draft or the
        session -- so the answer is expertise, not agreement. Both channels
        are budgeted, and a lead that spends its consult budget is told so and
        asked to proceed with what it has.
        """
        consult_answers: List[str] = []
        consults_used = 0

        def build(fetched: List[tuple]) -> str:
            extras: List[str] = []
            if consult_answers:
                extras.append("## Consult answers\n\n" + "\n\n".join(consult_answers))
            if fetched:
                extras.append(_render_fetches(fetched))
            return self._lead_prompt(spec, lead=lead, extras=extras)

        for _ in range(self.config.max_consults + 1):
            draft = self._invoke_with_fetches(lead, build, task=task)
            requests = _parse_consults(draft)
            if not requests:
                return draft
            if consults_used >= self.config.max_consults:
                consult_answers.append(
                    "(consult budget spent -- proceed with what you have and "
                    "record any open question in your close-out)"
                )
                return self._invoke_with_fetches(lead, build, task=task)
            for name, question in requests:
                if consults_used >= self.config.max_consults:
                    break
                consults_used += 1  # spent even on a bad name; no free retries
                peer = self._resolve_consultant(name, lead)
                if peer is None:
                    consult_answers.append(
                        f"[{name}] is not a member you can consult; see the "
                        f"list in your instructions."
                    )
                    continue
                answer = self.invoke(peer, self._consult_prompt(spec, question, peer))
                task.record("assistant", f"[consult {peer}] {answer}")
                task.keep(answer, kind=f"consult:{peer}")
                consult_answers.append(f"[{peer}]\n{answer}")
        return draft

    def _consult_prompt(self, spec: TaskSpec, question: str, peer: str) -> str:
        label = resolve(peer)
        return (
            f"Task context: {spec.description}\n\n"
            f"A colleague leading this task asks you one question in your "
            f"area of strength:\n{question}\n\n"
            f"You are {label.label if label else peer}. Answer just this "
            "question, concretely. You have no other context by design; if it "
            "cannot be answered without more, say exactly what is missing."
        )

    # -- one task ------------------------------------------------------------
    def run_task(self, spec: TaskSpec) -> TaskSummary:
        """Work one task to completion and fold it into the ledger."""
        if spec.work_class == WorkClass.SECURITY:
            return self._run_security_task(spec)

        lead = self._pick_lead(spec)
        collaborators = self.collaborators_for(spec, lead)

        task = TaskMemory(spec.task_id, lead, self.store)
        task.record("user", spec.description)

        # The lead drafts with full working memory, and with the fetch and
        # consult channels live: a reply that is a request gets served.
        draft = self._draft_with_channels(lead, spec, task)
        task.record("assistant", draft)
        task.keep(draft, kind="draft")

        # Collaborators contribute into the lead's working memory. They see the
        # task and the draft, not the whole session: their value is an
        # independent read, which inheriting the lead's history would erode.
        # Reviewers are anonymised toward the lead: a critique must be
        # weighed by its content, not its letterhead, and models carry priors
        # about other models that would let a lead discount or defer by
        # reputation. Full attribution survives for the operator -- in the
        # artifact kinds and the ledger -- which is where feedback quality
        # per reviewer belongs: the scoreboard's question, not the author's.
        labels = {peer: f"Reviewer {chr(65 + i)}"
                  for i, peer in enumerate(collaborators)}
        notes: List[tuple] = []
        for peer in collaborators:
            note = self.invoke(peer, self._collaborator_prompt(spec, draft, peer))
            task.record("assistant", f"[{labels[peer]}] {note}")
            task.keep(note, kind=f"review:{peer}")
            notes.append((peer, note))

        # The rebuttal round: the lead answers every finding and revises. This
        # is where the debate actually lands in the artifact -- without it,
        # independent reads inform only the close-out prose while the work
        # ships un-amended, which is critique as theatre. Costs one lead
        # invocation, bought only when there were critiques to answer.
        #
        # Then fix->verify, not more debate: reviewers who marked findings
        # BLOCKING re-check only those findings against the revision, and an
        # unresolved verdict buys at most one more revision. The distinction
        # is load-bearing. Measured across debate protocols, extra open
        # rounds produce conformity -- agents uncritically adopting the
        # majority at rates up to ~85%, peer rationales destabilising
        # previously-correct answers, at 2-3x the tokens for equal or worse
        # accuracy -- while iteration grounded in external feedback on a
        # concrete named defect is the one case that reliably improves the
        # artifact. So reviewers here never see each other, never vote, and
        # never widen scope: they check their own findings and nothing else.
        # Clean reviews cost nothing further: a reviewer with nothing to say
        # says NO FINDINGS, and a revision round against empty critiques would
        # be the most avoidable spend in the loop.
        notes = [
            (p, n) for p, n in notes
            if n.strip().upper().rstrip(".") != "NO FINDINGS"
        ]
        if notes:
            revision = self.invoke(
                lead,
                self._revision_prompt(
                    spec, draft, [f"[{labels[p]}]\n{n}" for p, n in notes]
                ),
            )
            task.record("assistant", revision)
            task.keep(revision, kind="revision")

            blocking = [(p, n) for p, n in notes if "BLOCKING" in n.upper()]
            cycles = 1
            unresolved = self._recheck_blocking(
                spec, blocking, revision, task, labels=labels
            )
            while unresolved and cycles < self.config.max_fix_cycles:
                revision = self.invoke(
                    lead,
                    self._fix_prompt(
                        spec, revision,
                        [(labels[p], v) for p, v in unresolved],
                    ),
                )
                task.record("assistant", revision)
                task.keep(revision, kind="revision")
                cycles += 1
                unresolved = self._recheck_blocking(
                    spec, [(p, n) for p, n in blocking
                           if any(p == up for up, _ in unresolved)],
                    revision, task, labels=labels,
                )
            if unresolved:
                # The cap ran out with findings still open. They go to the
                # record loudly rather than being lost in the transcript.
                task.record(
                    "user",
                    "Blocking findings still unresolved at close -- carry them "
                    "into the summary as open questions:\n"
                    + "\n".join(f"[{labels[p]}] {v}" for p, v in unresolved),
                )

        self._run_integration_gate(lead, spec, task)

        summary_text, reasoning, dead_ends = self._close_out(lead, spec, task)
        summary = task.close(
            summary=summary_text, reasoning=reasoning, dead_ends=dead_ends
        )
        self.memory.absorb(summary)
        self.history.append(summary)
        return summary

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
            task.record("user", spec.description)

            # Fetch channel only: the excursion stays a straight line, so
            # there is no consult here by design.
            draft = self._invoke_with_fetches(
                excursion.worker,
                lambda fetched: self._lead_prompt(
                    spec, extras=[_render_fetches(fetched)] if fetched else None
                ),
                task=task,
            )
            task.record("assistant", draft)
            task.keep(draft, kind="draft")

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
            verdict = self.invoke(
                verifier, self._verifier_prompt(spec, draft, verifier)
            )
            task.record("assistant", f"[{verifier}] {verdict}")
            task.keep(verdict, kind=f"verify:{verifier}")

            summary_text, reasoning, dead_ends = self._close_out(
                excursion.worker, spec, task
            )
            summary = task.close(
                summary=summary_text, reasoning=reasoning, dead_ends=dead_ends
            )
            self.memory.absorb(summary)
            self.history.append(summary)
            return summary
        finally:
            # Unconditional by design: a failed security lookup still ends the
            # excursion and hands the seat back rather than leaving the run
            # degraded.
            close_excursion(excursion)

    # -- orchestration -------------------------------------------------------
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
        seat = self.seat()

        def build(fetched: List[tuple]) -> str:
            # Rebuilt every round: an answered ASK lands in the rulings, and
            # the re-ask must carry it -- a stale prompt would re-ask the
            # operator the question they just answered.
            body = self.memory.render(
                current=(
                    "Name the single next task, or reply exactly DONE if the "
                    "goal is met. To read a full artifact behind a summary "
                    "first, reply with exactly 'FETCH: <artifact-id>' and "
                    "nothing else. If the decision turns on something only the "
                    "operator can answer, reply 'ASK: <one question>' instead."
                    f"\n\n{_SIZE_CEILING}\n\n{_KIND_REQUEST}"
                ),
                recent=self.config.recent_entries,
                extra=self._map_block(),
            )
            if fetched:
                body += ("\n\n" + _render_fetches(fetched)
                         + "\n\nWith that read, answer now.")
            return body

        for _ in range(_MAX_ASKS_PER_DECISION):
            reply = self._invoke_with_fetches(seat.key, build)
            stripped = reply.strip()
            if not stripped.upper().startswith("ASK:"):
                break
            question = stripped[4:].strip()
            if self.config.ask_operator is None:
                raise OperatorInputNeeded(question)
            answer = self.config.ask_operator(question)
            self.memory.ledger.rulings.append(f"Q: {question} -- A: {answer}")
        else:
            raise RunStalled(
                f"the orchestrator asked the operator {_MAX_ASKS_PER_DECISION} "
                f"questions without naming a task; it is interrogating, not "
                f"deciding."
            )
        # Strip any KIND label before testing for DONE: an orchestrator that
        # dutifully labels its final reply must still be able to end the run,
        # not spawn a task whose description is the word DONE.
        kind, difficulty, description = _parse_kind(reply)
        if description.strip().upper().startswith("DONE"):
            return None
        return TaskSpec(
            task_id=f"t{len(self.history) + 1}",
            description=description,
            kind=kind,
            complexity=difficulty,
        )

    def plan(self) -> str:
        """Ask the orchestrator for the full expected task list, without running.

        The Agent Action Plan idea, sized to fit: the operator reviews the
        decomposition at the moment it is cheap to change -- reviewing the
        plan is reviewing the work at a fraction of the cost -- instead of
        discovering a mis-scoped run after the subscription window is spent.
        """
        seat = self.seat()
        return self.invoke(
            seat.key,
            self.memory.render(
                current=(
                    "Do not start work. List every task you currently expect "
                    "this goal to need, in order, one per line, each as "
                    "'KIND: <kind> -- <description>'. Mark anything you are "
                    f"unsure about with '?'.\n\n{_SIZE_CEILING}"
                ),
                recent=self.config.recent_entries,
                extra=self._map_block(),
            ),
        )

    def run(self, *, max_tasks: int = 20) -> List[TaskSummary]:
        """Drive tasks until the orchestrator says DONE or the cap is hit.

        The cap is a runaway backstop, not a quality gate: a loop that has not
        converged by then has a problem the cap will not fix, and the caller
        should look at why.

        Raises:
            RunStalled: when the orchestrator names the same task twice in a
                row -- the loop has stopped converging, and burning further
                rounds on a known outcome helps nobody.
        """
        if self.config.plan_gate is not None:
            if not self.config.plan_gate(self.plan()):
                log.info("plan gate declined the run; nothing executed")
                return []
        previous_description: Optional[str] = None
        for _ in range(max_tasks):
            spec = self.next_task()
            if spec is None:
                break
            if spec.description == previous_description:
                raise RunStalled(
                    f"the orchestrator named the same task twice in a row: "
                    f"{spec.description!r}. The last close-out did not move "
                    f"its view forward. Improve the summary, narrow the goal, "
                    f"or intervene before re-running."
                )
            previous_description = spec.description
            self.run_task(spec)
        return list(self.history)

    # -- prompts -------------------------------------------------------------
    def _map_block(self) -> str:
        if self.config.codebase_map is None:
            return ""
        return self.config.codebase_map.render()

    def _lead_prompt(
        self,
        spec: TaskSpec,
        *,
        lead: Optional[str] = None,
        extras: Optional[List[str]] = None,
    ) -> str:
        parts = [
            self.memory.render(
                current=spec.description, recent=self.config.recent_entries
            ),
        ]
        map_block = self._map_block()
        if map_block:
            parts.append(map_block)
        parts.append("You are leading this task. Produce the complete work.")
        parts.append(worker_menu())
        parts.append(
            "To read a filed artifact in full before working, reply with "
            "exactly 'FETCH: <artifact-id>' and nothing else -- you will get "
            "the content and be asked again."
        )
        if lead is not None:
            consultables = [p for p in self.brain_trust if p != lead]
            names = ", ".join(
                (resolve(p).label if resolve(p) else p) for p in consultables
            )
            parts.append(
                "If one specific question outside your strengths blocks you, "
                "reply with only 'CONSULT <member>: <question>' -- the member "
                "answers blind and their answer comes back to you. Members "
                f"you may consult: {names}. At most "
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
        parts.append(
            "Before finishing, re-read your task, restated verbatim, and "
            f"confirm every part of it is addressed:\n\n{spec.description}"
        )
        return "\n\n".join(parts)

    def _collaborator_prompt(self, spec: TaskSpec, draft: str, peer: str) -> str:
        label = resolve(peer)
        return (
            f"Task: {spec.description}\n\n"
            f"Current work:\n{draft}\n\n"
            f"You are {label.label if label else peer}, contributing an independent "
            "read. Report everything you find with a severity and a confidence; do "
            "not filter to only the important ones. Filtering happens downstream, "
            "and a reviewer told to be selective suppresses its own findings. "
            "Prefix any finding that must be fixed before this work is acceptable "
            "with 'BLOCKING:' -- you will be asked to re-check exactly those "
            "against the revision. If you genuinely find nothing worth changing, "
            "reply exactly 'NO FINDINGS' and nothing else."
        )

    def _revision_prompt(self, spec: TaskSpec, draft: str, notes: List[str]) -> str:
        joined = "\n\n".join(notes)
        return (
            f"Task: {spec.description}\n\n"
            f"Your draft:\n{draft}\n\n"
            f"Independent reviews of it:\n{joined}\n\n"
            "Revise your work. Address every finding explicitly: fix it, or "
            "rebut it with a reason -- silence is not a response. A finding "
            "you cannot decide goes to the record as an open question, not "
            "into the void. Produce the complete revised work, not a diff."
        )

    def _recheck_blocking(
        self,
        spec: TaskSpec,
        blocking: List[tuple],
        revision: str,
        task: TaskMemory,
        *,
        labels: Optional[dict] = None,
    ) -> List[tuple]:
        """Have each blocking reviewer re-check its own findings. Nothing else.

        Returns (peer, verdict) pairs for findings still unresolved. Scope is
        the narrowest that does the job: the reviewer sees its own findings
        and the revision -- not the other reviewers, not a vote, not an
        invitation to find new problems. Widening any of those is where
        debate protocols measurably tip into conformity.
        """
        unresolved: List[tuple] = []
        for peer, note in blocking:
            verdict = self.invoke(
                peer,
                f"Task: {spec.description}\n\n"
                f"You reviewed this work and raised these findings:\n{note}\n\n"
                f"The revised work:\n{revision}\n\n"
                "Check only your BLOCKING findings against the revision. Do "
                "not raise new findings. Reply exactly 'RESOLVED' if every "
                "blocking finding is addressed, otherwise 'UNRESOLVED: <what "
                "specifically remains>'.",
            )
            shown = (labels or {}).get(peer, peer)
            task.record("assistant", f"[{shown} recheck] {verdict}")
            if not verdict.strip().upper().startswith("RESOLVED"):
                unresolved.append((peer, verdict.strip()))
        return unresolved

    def _fix_prompt(self, spec: TaskSpec, revision: str, unresolved: List[tuple]) -> str:
        remaining = "\n\n".join(f"[{p}]\n{v}" for p, v in unresolved)
        return (
            f"Task: {spec.description}\n\n"
            f"Your current work:\n{revision}\n\n"
            f"These blocking findings remain unresolved:\n{remaining}\n\n"
            "Fix them, or state precisely why the reviewer is wrong. Produce "
            "the complete revised work."
        )

    def _run_integration_gate(self, lead: str, spec: TaskSpec, task: TaskMemory) -> None:
        """Execute the project's own check and feed a failure back once.

        Reviewers judge the work by reading; this is the half that runs it.
        A failure buys the lead a bounded number of fix rounds with the real
        output in hand; a failure that survives the cap is written loudly
        into the task memory so the close-out and the ledger carry it as an
        open problem instead of a silent one.
        """
        gate = self.config.integration_gate
        if gate is None:
            return
        result = gate.run()
        task.record("user", result.render())
        fixes = 0
        while not result.passed and fixes < self.config.max_gate_fixes:
            fix = self.invoke(
                lead,
                f"Task: {spec.description}\n\n"
                f"The project's own integration check failed after your "
                f"work:\n{result.render()}\n\n"
                "Fix the failure. Produce the complete revised work.",
            )
            task.record("assistant", fix)
            task.keep(fix, kind="gate-fix")
            fixes += 1
            result = gate.run()
            task.record("user", result.render())
        if not result.passed:
            task.record(
                "user",
                "The integration gate is still failing at close -- carry it "
                "into the summary as an open failure.",
            )

    def _verifier_prompt(self, spec: TaskSpec, draft: str, verifier: str) -> str:
        label = resolve(verifier)
        return (
            f"Task: {spec.description}\n\n"
            f"Proposed answer:\n{draft}\n\n"
            f"You are {label.label if label else verifier}, verifying security "
            "work you did not author. Check it for correctness, for anything "
            "unsafe it recommends, and for anything it asserts without "
            "evidence. State plainly whether it should be accepted, and what "
            "must change if not. Do not redo the work; verify it."
        )

    def _close_out(self, lead: str, spec: TaskSpec, task: TaskMemory):
        """Have the lead write the one thing that survives the task."""
        transcript = "\n\n".join(f"[{t.role}] {t.content}" for t in task.turns())
        sections = (
            "The task is finished. Write the record that survives it, as three "
            "sections:\nSUMMARY: what was built and decided.\n"
            "REASONING: why, including alternatives weighed.\n"
            "DEAD ENDS: one line each for anything tried that failed, and why. "
            "Write the lesson, not the transcript."
        )
        if self.config.codebase_map is not None:
            # Every run strengthens the map -- that is what makes it an asset
            # that accrues rather than a snapshot that rots.
            sections += (
                "\nMAP NOTES: one line each, as 'topic: fact', for anything "
                "you learned about this codebase that the next session should "
                "not have to rediscover -- a convention, a dependency, a trap. "
                "Durable facts about the code only; omit the section if none."
            )
        reply = self.invoke(lead, f"Task: {spec.description}\n\n{transcript}\n\n{sections}")
        summary, reasoning, dead_ends, map_notes = _parse_closeout(reply)
        if self.config.codebase_map is not None:
            for topic, note in map_notes:
                self.config.codebase_map.amend(
                    topic=topic, note=note, author=lead, session=spec.task_id
                )
        return summary, reasoning, dead_ends


def _parse_fetch(reply: str) -> Optional[str]:
    """An artifact request: the whole reply is ``FETCH: <artifact-id>``.

    Deliberately strict -- only a reply that *is* a fetch request counts, so a
    draft that merely mentions the word FETCH in code or prose is never
    mistaken for one.
    """
    stripped = (reply or "").strip()
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) == 1 and lines[0].upper().startswith("FETCH:"):
        wanted = lines[0].split(":", 1)[1].strip()
        return wanted or None
    return None


#: Fetched artifacts are appended whole, but a pathological artifact must not
#: flood the requester's prompt.
_FETCH_CHAR_CAP = 12_000


def _render_fetches(fetched) -> str:
    blocks = ["## Fetched artifacts (you asked for these)"]
    for artifact_id, content in fetched:
        body = content if len(content) <= _FETCH_CHAR_CAP else (
            content[:_FETCH_CHAR_CAP] + "\n[...truncated]"
        )
        blocks.append(f"[artifact {artifact_id}]\n{body}")
    return "\n\n".join(blocks)


def _parse_consults(reply: str):
    """Consult requests: the reply *begins* with ``CONSULT <member>: <q>``.

    Returns [(member, question), ...] or [] when the reply is a real draft.
    Same strictness rationale as :func:`_parse_fetch`.
    """
    lines = [ln.strip() for ln in (reply or "").strip().splitlines() if ln.strip()]
    if not lines or not lines[0].upper().startswith("CONSULT "):
        return []
    out = []
    for line in lines:
        if not line.upper().startswith("CONSULT "):
            break
        rest = line[len("CONSULT "):]
        if ":" not in rest:
            continue
        member, question = rest.split(":", 1)
        if member.strip() and question.strip():
            out.append((member.strip(), question.strip()))
    return out


#: How many operator questions one decision may spend before it is judged to
#: be interrogating rather than deciding. Three is generous: a decision that
#: genuinely needs more operator input than that is a scoping conversation,
#: which belongs in the interview, not the loop.
_MAX_ASKS_PER_DECISION = 3


#: Asks the orchestrator to label the task so :mod:`multi_llm.task_kinds` can
#: act on it. Kind and difficulty together are the routing decision: the few
#: pinned kinds go where the evidence says, everything else rides the
#: difficulty ladder across the four subscriptions.
_KIND_REQUEST = (
    "Begin your reply with a single line 'KIND: <kind> <difficulty>'. Kind is "
    "one of: " + ", ".join(sorted(ROUTING)) + ". Difficulty is one of: rote, "
    "simple, standard, complex -- judge it by how many logical steps the task "
    "takes and what breaks if it is wrong. Most well-sized tasks are simple; "
    "reserve complex for genuinely hard reasoning. Then the task on the "
    "following line. Omit the line if none fits."
)


def _parse_kind(reply: str) -> tuple:
    """Split an optional leading ``KIND:`` line off the orchestrator's reply.

    Returns (kind, difficulty, description). Tolerant in the same way
    close-out parsing is: an unrecognised or absent label degrades to the
    default rather than failing the round. A mislabelled task costs a routing
    preference; a rejected round costs the task. The difficulty default is
    SIMPLE -- the bulk of well-sized tasks belong there, and the ladder puts
    them on the subscription with capacity to spare.
    """
    text = (reply or "").strip()
    lines = text.splitlines()
    if lines and lines[0].strip().upper().startswith("KIND:"):
        label = lines[0].split(":", 1)[1].strip().lower()
        rest = "\n".join(lines[1:]).strip()
        parts = label.split()
        claimed = parts[0] if parts else ""
        difficulty = next(
            (p for p in parts[1:] if p in Complexity._COLLABORATORS),
            Complexity.SIMPLE,
        )
        if claimed in ROUTING and rest:
            return claimed, difficulty, rest
        if rest:
            log.debug("orchestrator proposed unknown task kind %r", claimed)
            return TaskKind.GENERAL, difficulty, rest
    return TaskKind.GENERAL, Complexity.SIMPLE, text


def _parse_closeout(reply: str):
    """Split a close-out into summary, reasoning, dead ends and map notes.

    Tolerant by design: a missing section degrades to a usable record rather
    than failing the task, since the raw work is stored either way. Reasoning
    falls back to the summary because the ledger refuses an empty one, and a
    weak reason recorded honestly beats a lost task. Map notes that do not
    parse as 'topic: fact' are dropped rather than guessed at -- the map is
    long-lived, so a malformed note is worse there than nowhere.
    """
    sections: Dict[str, List[str]] = {
        "SUMMARY": [], "REASONING": [], "DEAD ENDS": [], "MAP NOTES": [],
    }
    current = "SUMMARY"
    for line in (reply or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        matched = next((k for k in sections if upper.startswith(k + ":")), None)
        if matched:
            current = matched
            rest = stripped[len(matched) + 1:].strip()
            if rest:
                sections[current].append(rest)
            continue
        if stripped:
            sections[current].append(stripped)

    summary = " ".join(sections["SUMMARY"]).strip() or (reply or "").strip() or "(no summary)"
    reasoning = " ".join(sections["REASONING"]).strip() or summary
    dead_ends = [d.lstrip("-• ").strip() for d in sections["DEAD ENDS"] if d.strip()]
    map_notes: List[tuple] = []
    for raw in sections["MAP NOTES"]:
        raw = raw.lstrip("-• ").strip()
        if ":" in raw:
            topic, note = raw.split(":", 1)
            if topic.strip() and note.strip():
                map_notes.append((topic.strip(), note.strip()))
    return summary, reasoning, dead_ends, map_notes
