"""The run loop: how a task moves through the architecture.

One cycle:

1. The orchestrator reads its ledger and names the next *wave*: every task
   that is ready to start now and does not depend on another task in the same
   wave. Dependency ordering lives in the wave boundaries -- what must build
   on earlier work waits for a later wave; what is independent runs at the
   same time, throttled per subscription (``max_parallel_per_vendor``) --
   four vendors at four calls each is sixteen invocations in flight, and
   calls beyond a vendor's cap queue and start as its slots free. Instances
   are stateless CLI calls, so ten parallel tasks on one model are just ten
   subprocesses.
2. A brain-trust member leads each task, with collaborators drawn in
   according to the task's complexity.
3. The lead works with full memory for the duration, commissioning worker
   bees for anything it needs fetched, read, or checked.
4. The task closes. The lead emits one summary, with reasoning, dead ends,
   open questions, and pointers to the full work. Everything else it
   accumulated is dropped.
5. The orchestrator absorbs those summaries and names the next wave. It is
   the final arbiter: a result it judges not good enough -- even one that
   passed review -- is sent back with ``REDO`` and a concrete objection, and
   every open question bubbled up from any level must be addressed, never
   skated past.

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
import threading
from concurrent.futures import ThreadPoolExecutor
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
    """The loop has stopped converging.

    Raised when the orchestrator names the same task in two consecutive
    waves, rejects the same result more than ``max_redos`` times, or spends
    its ASK budget interrogating.

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
    #: How many invocations may be in flight *per vendor* at the same time.
    #: The limit being guarded is each subscription's rate limit, so the cap
    #: is per subscription, not global: four Claude calls, four OpenAI calls,
    #: four Gemini calls and four Grok calls can all run at once. Instances
    #: are stateless CLI calls, so many parallel tasks on one model are just
    #: subprocesses; calls beyond a vendor's cap queue and start automatically
    #: as its slots free up.
    max_parallel_per_vendor: int = 4
    #: How many times the orchestrator may reject (REDO) the same task's
    #: result before the loop stalls loudly for the operator. The orchestrator
    #: is the final arbiter of what ships, but an arbiter rejecting the same
    #: work three times is a stuck arbiter.
    max_redos: int = 2
    #: A no-stake completion judge, as a model key (usually the control
    #: plane's convergence model). When set, a DONE from the orchestrator is
    #: checked against the goal by a model with no authorship stake before
    #: the run is allowed to end; an explicit UNMET verdict buys exactly one
    #: veto -- the objection goes back to the orchestrator and the loop
    #: continues. None skips the check (the default, and what tests use).
    done_judge: Optional[str] = None
    #: On-disk session log (see :class:`multi_llm.persistence.SessionLog`).
    #: When set, every ledger entry and ruling is appended to disk the moment
    #: it happens, so a crashed or interrupted run can resume from what was
    #: actually recorded. None keeps the ledger in-process only.
    session_log: Optional[object] = None
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
        # Every invocation passes through its vendor's gate: at most
        # ``max_parallel_per_vendor`` calls in flight per subscription, calls
        # beyond that queueing until a slot frees. The gate wraps the invoke
        # itself rather than the task scheduler because tasks mix vendors --
        # a lead on one subscription drawing reviewers and workers from three
        # others -- and the thing being rate-limited is the subscription.
        self._vendor_gates: Dict[str, threading.BoundedSemaphore] = {}
        self._gates_lock = threading.Lock()
        inner = invoke

        def gated(model_key: str, prompt: str, *args, **kwargs) -> str:
            with self._vendor_gate(model_key):
                return inner(model_key, prompt, *args, **kwargs)

        self.invoke = gated
        self._available = available or (lambda _key: True)
        self.memory = PersistentMemory(goal, store, invariants=invariants)
        # kwargs pass through so a NEED TOOL regrant (allow_writes) reaches a
        # provider that honours it; providers that don't are handled by the
        # pool's TypeError fallback.
        self.workers = WorkerPool(
            store=store,
            run=lambda model, prompt, **kw: self.invoke(model, prompt, **kw),
            budget=self.config.worker_budget,
        )
        self._done_vetoed = False
        self._rotation = 0
        self.history: List[TaskSummary] = []
        # Wave bookkeeping. Tasks in one wave run on threads, so everything
        # the threads share -- the rotation counter, the ledger, the history
        # -- is touched only under this lock.
        self._lock = threading.Lock()
        #: Every spec ever issued, by task id, so a REDO can name its target.
        self._specs: Dict[str, TaskSpec] = {}
        #: REDO count per original task id; past ``max_redos`` the run stalls.
        self._redo_counts: Dict[str, int] = {}
        self._task_counter = 0
        #: Operator questions raised alongside a wave and not yet answered.
        #: They pause only the work that depends on them: independent tasks
        #: keep running, and the questions are re-rendered loudly to the
        #: orchestrator every round until answered.
        self.unanswered_asks: List[str] = []

    def _absorb(self, summary: TaskSummary) -> None:
        """Fold a finished task into the ledger, history, and on-disk log."""
        with self._lock:
            self.memory.absorb(summary)
            self.history.append(summary)
            if self.config.session_log is not None:
                try:
                    self.config.session_log.append_entry(
                        self.memory.ledger.entries[-1]
                    )
                except Exception:  # noqa: BLE001 -- persistence must not kill a run
                    log.warning("session log write failed", exc_info=True)

    def _add_ruling(self, ruling: str) -> None:
        with self._lock:
            self.memory.ledger.rulings.append(ruling)
            if self.config.session_log is not None:
                try:
                    self.config.session_log.append_ruling(ruling)
                except Exception:  # noqa: BLE001 -- persistence must not kill a run
                    log.warning("session log write failed", exc_info=True)

    def restore(self, session_log) -> int:
        """Resume from a prior run's on-disk log. Returns entries restored.

        Replays the log into this session's (empty) ledger, moves the task
        counter past every task id already used, and registers placeholder
        specs for completed tasks so an arbiter REDO of pre-resume work still
        resolves -- the reissue description then leans on the ledger summary,
        since the original spec text did not survive the process.
        """
        restored = session_log.restore_into(self.memory)
        self._task_counter = max(
            self._task_counter, session_log.highest_task_number()
        )
        for entry in self.memory.ledger.entries:
            self._specs.setdefault(
                entry.task_id,
                TaskSpec(
                    task_id=entry.task_id,
                    description=(
                        f"(completed in a prior session; its record follows) "
                        f"{entry.summary}"
                    ),
                ),
            )
        return restored

    def _vendor_gate(self, model_key: str) -> threading.BoundedSemaphore:
        spec = resolve(model_key)
        vendor = spec.provider if spec is not None else model_key.split(":", 1)[0]
        with self._gates_lock:
            gate = self._vendor_gates.get(vendor)
            if gate is None:
                gate = threading.BoundedSemaphore(
                    max(1, self.config.max_parallel_per_vendor)
                )
                self._vendor_gates[vendor] = gate
        return gate

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
        with self._lock:
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
        # Questions the harness itself knows are open at close -- an
        # unresolved blocking finding, a failing gate -- are folded into the
        # summary's open questions directly rather than trusting the lead's
        # close-out prose to carry them.
        harness_questions: List[str] = []
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
                harness_questions.extend(
                    f"Unresolved blocking finding ({labels[p]}): {v}"
                    for p, v in unresolved
                )

        gate_question = self._run_integration_gate(lead, spec, task)
        if gate_question:
            harness_questions.append(gate_question)

        summary_text, reasoning, dead_ends, open_questions = self._close_out(
            lead, spec, task
        )
        for q in harness_questions:
            if q not in open_questions:
                open_questions.append(q)
        summary = task.close(
            summary=summary_text, reasoning=reasoning, dead_ends=dead_ends,
            open_questions=open_questions,
        )
        self._absorb(summary)
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

            summary_text, reasoning, dead_ends, open_questions = self._close_out(
                excursion.worker, spec, task
            )
            summary = task.close(
                summary=summary_text, reasoning=reasoning, dead_ends=dead_ends,
                open_questions=open_questions,
            )
            self._absorb(summary)
            return summary
        finally:
            # Unconditional by design: a failed security lookup still ends the
            # excursion and hands the seat back rather than leaving the run
            # degraded.
            close_excursion(excursion)

    # -- orchestration -------------------------------------------------------
    def next_wave(self) -> Optional[List[TaskSpec]]:
        """Ask the orchestrator for the next wave of tasks, given the ledger.

        A wave is every task that can start right now: dependency ordering
        lives in the wave boundaries, and tasks within one wave run at the
        same time without seeing each other's results. Returns None when the
        orchestrator reports the goal met.

        The reply may also carry ``REDO <task-id>: <objection>`` lines -- the
        arbiter verdict. The orchestrator is the final judge of what ships,
        so a completed task it finds not good enough, even one that passed
        review, is reissued with the objection and a pointer to the prior
        work. Bounded by ``max_redos`` per task, because an arbiter rejecting
        the same work over and over is a stuck arbiter.

        The orchestrator may also raise ``ASK: <question>`` lines when a
        decision turns on something only the operator knows -- scope, taste,
        a business constraint no artifact can settle. An ASK alongside TASK
        lines pauses only the work that depends on the answer: the question
        is held (and answered in parallel with the wave when a channel is
        configured) while independent tasks run. A reply that is *nothing
        but* questions means the decision itself is blocked, so those are
        answered before re-asking -- bounded, so a confused orchestrator
        cannot interrogate the operator in a loop. Every answer is recorded
        as a standing ruling and re-emitted on every render, so a question
        is never asked twice.

        Raises:
            OperatorInputNeeded: on a decision-blocking ASK when no
                ``ask_operator`` channel is configured. Guessing an answer
                the orchestrator explicitly flagged as operator-only would
                defeat the point of asking.
            RunStalled: when a task is rejected more than ``max_redos`` times,
                or the ASK budget is spent interrogating.
        """
        seat = self.seat()

        def build(fetched: List[tuple]) -> str:
            # Rebuilt every round: an answered ASK lands in the rulings, and
            # the re-ask must carry it -- a stale prompt would re-ask the
            # operator the question they just answered.
            extra_blocks = [b for b in (self._map_block(), self._awaiting_block())
                            if b]
            body = self.memory.render(
                current=(
                    "Name the next wave of tasks: every task that is ready to "
                    "start right now, one per line, each as 'TASK <kind> "
                    "<difficulty>: <description>'. Tasks in one wave run at "
                    "the same time and cannot see each other's results, so a "
                    "task that builds on another task's output goes in a "
                    "later wave, never the same one. A wave of one is fine.\n\n"
                    "You are the final arbiter of finished work, and REDO is "
                    "your exceptional verdict, not your habit. Reject only a "
                    "result that is genuinely wrong or unusable against the "
                    "goal -- a missed requirement, work that does not do what "
                    "the task asked. It has already been reviewed: do not "
                    "reject for style, taste, or improvements you would "
                    "merely prefer -- if an improvement matters, name it as a "
                    "follow-up task instead. When a result truly fails that "
                    "bar, reject it with a line 'REDO <task-id>: <what must "
                    "change and why>' and it will be redone with your "
                    "objection in hand.\n\n"
                    "Address every OPEN QUESTION in the record before or "
                    "alongside new work, in this order: answer it yourself "
                    "from what you know and the record; failing that, issue "
                    "a task or REDO that resolves it; only when nobody in "
                    "the system can answer it, raise it to the operator as a "
                    "line 'ASK: <one question>'. ASK lines may accompany "
                    "TASK lines: the question goes to the operator while "
                    "independent work continues. Never issue a task that "
                    "depends on an unanswered question -- hold that branch "
                    "and keep issuing work that does not. Never skate past "
                    "an open question.\n\n"
                    "Reply exactly DONE if the goal is met and nothing "
                    "remains. To read a full artifact behind a summary "
                    "first, reply with exactly 'FETCH: <artifact-id>' and "
                    "nothing else."
                    f"\n\n{_SIZE_CEILING}\n\n{_KIND_REQUEST}"
                ),
                recent=self.config.recent_entries,
                extra="\n\n".join(extra_blocks),
            )
            if fetched:
                body += ("\n\n" + _render_fetches(fetched)
                         + "\n\nWith that read, answer now.")
            return body

        for _ in range(_MAX_ASKS_PER_DECISION):
            reply = self._invoke_with_fetches(seat.key, build)
            tasks, redos, asks, residual = _parse_wave(reply)
            if tasks or redos or residual or not asks:
                break
            # The reply is nothing but questions: the decision itself is
            # blocked on the operator, so answer before re-asking.
            if self.config.ask_operator is None:
                raise OperatorInputNeeded(
                    asks[0] if len(asks) == 1 else "\n".join(asks)
                )
            for question in asks:
                answer = self.config.ask_operator(question)
                self._add_ruling(f"Q: {question} -- A: {answer}")
        else:
            raise RunStalled(
                f"the orchestrator asked the operator {_MAX_ASKS_PER_DECISION} "
                f"questions without naming a task; it is interrogating, not "
                f"deciding."
            )

        # Questions riding alongside a wave pause only what depends on them:
        # they are held for the operator while the wave's tasks run.
        if asks and (tasks or redos or residual):
            with self._lock:
                for question in asks:
                    already_ruled = any(
                        r.startswith(f"Q: {question} --")
                        for r in self.memory.ledger.rulings
                    )
                    if question not in self.unanswered_asks and not already_ruled:
                        self.unanswered_asks.append(question)

        specs: List[TaskSpec] = []
        for task_id, objection in redos:
            reissued = self._reissue(task_id, objection)
            if reissued is not None:
                specs.append(reissued)
        for kind, difficulty, description in tasks:
            specs.append(self._new_spec(kind, difficulty, description))
        if tasks or redos:
            return specs or None

        # No TASK/REDO lines: a bare reply is a single-task wave (or DONE).
        # Strip any KIND label before testing for DONE: an orchestrator that
        # dutifully labels its final reply must still be able to end the run,
        # not spawn a task whose description is the word DONE.
        kind, difficulty, description = _parse_kind(residual or reply)
        if description.strip().upper().startswith("DONE"):
            return None
        return [self._new_spec(kind, difficulty, description)]

    def next_task(self) -> Optional[TaskSpec]:
        """The next single task: the first of the next wave.

        Kept for callers that drive tasks one at a time; ``run()`` goes
        through :meth:`next_wave` and executes whole waves.
        """
        wave = self.next_wave()
        return wave[0] if wave else None

    def _new_spec(self, kind: str, difficulty: str, description: str) -> TaskSpec:
        with self._lock:
            self._task_counter += 1
            task_id = f"t{self._task_counter}"
        spec = TaskSpec(
            task_id=task_id, description=description,
            kind=kind, complexity=difficulty,
        )
        self._specs[spec.task_id] = spec
        return spec

    def _reissue(self, task_id: str, objection: str) -> Optional[TaskSpec]:
        """Turn an arbiter REDO into a fresh spec carrying the objection.

        The reissued task inherits the original's kind and difficulty, gets
        the objection verbatim in its description, and points at the prior
        work so the new lead reads what was rejected instead of guessing at
        it. An unknown task id is logged and skipped -- the orchestrator
        mistyped, and the next render still shows the work it wanted redone.
        """
        spec = self._specs.get(task_id.strip())
        if spec is None:
            log.warning("orchestrator rejected unknown task %r; skipping", task_id)
            return None
        root = spec.task_id.split("-r", 1)[0]
        count = self._redo_counts.get(root, 0) + 1
        if count > self.config.max_redos:
            raise RunStalled(
                f"the orchestrator has rejected task {root!r} {count} times. "
                f"An arbiter rejecting the same work past max_redos "
                f"({self.config.max_redos}) is stuck: the objection is not "
                f"one the current decomposition can answer. Rescope the task "
                f"or intervene before re-running."
            )
        self._redo_counts[root] = count
        prior = next(
            (e for e in self.memory.ledger.entries if e.task_id == spec.task_id),
            None,
        )
        pointer = ""
        if prior is not None and prior.refs:
            pointer = (
                "\nThe rejected work is filed as: "
                + ", ".join(f"artifact {r.id} ({r.kind})" for r in prior.refs)
                + ". Read it before redoing rather than starting blind."
            )
        redo = TaskSpec(
            task_id=f"{root}-r{count}",
            description=(
                f"{spec.description}\n\n"
                f"The orchestrator reviewed the previous result and rejected "
                f"it: {objection}\n"
                f"Redo the work so this objection is answered.{pointer}"
            ),
            complexity=spec.complexity,
            work_class=spec.work_class,
            kind=spec.kind,
        )
        self._specs[redo.task_id] = redo
        return redo

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
        """Drive waves until the orchestrator says DONE or the cap is hit.

        Each wave's tasks run concurrently, throttled per subscription by the
        vendor gates; the next wave is not asked for until every task in this
        one has closed, because the wave boundary is where dependency
        ordering lives.
        The cap is a runaway backstop, not a quality gate: a loop that has not
        converged by then has a problem the cap will not fix, and the caller
        should look at why.

        Raises:
            RunStalled: when a task in this wave repeats a description from
                the previous wave verbatim -- the loop has stopped converging,
                and burning further rounds on a known outcome helps nobody.
            OperatorInputNeeded: when the orchestrator reports DONE while
                operator questions are still unanswered and no ``ask_operator``
                channel exists -- a run does not end with questions for the
                operator silently outstanding.
        """
        if self.config.plan_gate is not None:
            if not self.config.plan_gate(self.plan()):
                log.info("plan gate declined the run; nothing executed")
                return []
        previous_wave: List[str] = []
        executed = 0
        while executed < max_tasks:
            wave = self.next_wave()
            if not wave:
                pending = list(self.unanswered_asks)
                if pending:
                    # DONE with questions outstanding is not done. With a
                    # channel, collect the answers and re-ask -- they may
                    # change the orchestrator's mind. Without one, surface
                    # them to the caller.
                    if self.config.ask_operator is None:
                        raise OperatorInputNeeded(
                            pending[0] if len(pending) == 1
                            else "\n".join(pending)
                        )
                    for question in pending:
                        self._answer_ask(question)
                    continue
                # The no-stake completion check: the author of the DONE call
                # never gets the last word on whether the goal is met. One
                # explicit UNMET verdict sends the objection back as a
                # standing ruling; a second DONE stands (an unsatisfiable
                # judge must not deadlock the run) but the objection is
                # already in the record for the operator.
                objection = self._judge_done()
                if objection is not None and not self._done_vetoed:
                    self._done_vetoed = True
                    self._add_ruling(
                        "A no-stake completion judge reviewed DONE and found "
                        f"the goal unmet: {objection} -- address this before "
                        "declaring DONE again."
                    )
                    continue
                if objection is not None:
                    log.warning(
                        "orchestrator declared DONE over the completion "
                        "judge's standing objection: %s", objection,
                    )
                break
            wave = wave[: max_tasks - executed]
            repeated = next(
                (s.description for s in wave if s.description in previous_wave),
                None,
            )
            if repeated is not None:
                raise RunStalled(
                    f"the orchestrator named the same task in two consecutive "
                    f"waves: {repeated!r}. The last close-out did not move "
                    f"its view forward. Improve the summary, narrow the goal, "
                    f"or intervene before re-running."
                )
            previous_wave = [s.description for s in wave]
            executed += len(wave)
            self._run_wave(wave)
        return list(self.history)

    def _judge_done(self) -> Optional[str]:
        """Ask the no-stake judge whether the goal is actually met.

        Returns None for accepted (or no judge configured, or the judge
        unavailable, or a malformed verdict -- only an explicit UNMET vetoes,
        because a confused judge must never block a legitimately finished
        run). Otherwise returns what specifically remains.
        """
        judge = self.config.done_judge
        if not judge or not self._available(judge):
            return None
        reply = self.invoke(
            judge,
            self.memory.render(
                current=(
                    "The orchestrator has declared this goal met. You are a "
                    "completion judge with no stake in the work: check the "
                    "goal against the completed record above, requirement by "
                    "requirement. Reply exactly 'MET' if every requirement "
                    "is demonstrably covered, otherwise 'UNMET: "
                    "<specifically what remains>'. Judge only whether the "
                    "goal is met -- quality was reviewed elsewhere."
                ),
                recent=self.config.recent_entries,
                with_previews=False,
            ),
        )
        stripped = (reply or "").strip()
        if stripped.upper().startswith("UNMET"):
            remainder = stripped.split(":", 1)
            return remainder[1].strip() if len(remainder) > 1 else stripped
        return None

    def _run_wave(self, wave: List[TaskSpec]) -> List[TaskSummary]:
        """Execute one wave, tasks concurrently, and wait for all of them.

        Throttling is not done here: every invocation passes through its
        vendor's gate, so a wave wider than one subscription's cap simply
        queues the excess per vendor while other vendors' work proceeds.
        Held operator questions are put to the channel on the same pool, so
        the operator answers *while* independent tasks run rather than the
        run stopping to wait.

        A wave of one with nothing held skips the thread pool entirely --
        exceptions and tracebacks stay plain in the common sequential case.
        For a real wave, every task is allowed to finish even if a sibling
        fails: a completed summary is real progress and is already in the
        ledger, so tearing down siblings would only discard finished work.
        The first failure is re-raised afterwards.
        """
        asks: List[str] = []
        if self.config.ask_operator is not None:
            with self._lock:
                asks = list(self.unanswered_asks)
        if len(wave) == 1 and not asks:
            return [self.run_task(wave[0])]
        results: List[TaskSummary] = []
        first_error: Optional[BaseException] = None
        width = min(len(wave) + len(asks), _MAX_WAVE_THREADS)
        with ThreadPoolExecutor(max_workers=width) as pool:
            ask_futures = [pool.submit(self._answer_ask, q) for q in asks]
            futures = [pool.submit(self.run_task, spec) for spec in wave]
            for future in futures:
                try:
                    results.append(future.result())
                except BaseException as exc:  # noqa: BLE001 -- re-raised below
                    if first_error is None:
                        first_error = exc
            for future in ask_futures:
                try:
                    future.result()
                except BaseException as exc:  # noqa: BLE001 -- re-raised below
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error
        return results

    # -- prompts -------------------------------------------------------------
    def _map_block(self) -> str:
        if self.config.codebase_map is None:
            return ""
        return self.config.codebase_map.render()

    def _awaiting_block(self) -> str:
        """Unanswered operator questions, re-rendered loudly every round.

        These hold only the branch that needs them: the orchestrator is told
        to keep issuing independent work and to hold anything that depends on
        an answer, so one open question never stops the whole run.
        """
        with self._lock:
            pending = list(self.unanswered_asks)
        if not pending:
            return ""
        return (
            "## Awaiting the operator (not yet answered)\n\n"
            "These questions have been sent to the operator and not yet "
            "answered. Do not re-ask them and do not issue tasks that depend "
            "on an answer; keep issuing independent work.\n\n"
            + "\n".join(f"- {q}" for q in pending)
        )

    def _answer_ask(self, question: str) -> None:
        """Put one held question to the operator and record the ruling."""
        answer = self.config.ask_operator(question)
        self._add_ruling(f"Q: {question} -- A: {answer}")
        with self._lock:
            if question in self.unanswered_asks:
                self.unanswered_asks.remove(question)

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
            # Kept under the reviewer's real name: recheck outcomes are what
            # the operator's scoreboard scores feedback quality from.
            task.keep(verdict, kind=f"recheck:{peer}")
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

    def _run_integration_gate(
        self, lead: str, spec: TaskSpec, task: TaskMemory
    ) -> Optional[str]:
        """Execute the project's own check and feed a failure back once.

        Reviewers judge the work by reading; this is the half that runs it.
        A failure buys the lead a bounded number of fix rounds with the real
        output in hand; a failure that survives the cap is written loudly
        into the task memory and returned as an open question so the ledger
        carries it as an open problem instead of a silent one.
        """
        gate = self.config.integration_gate
        if gate is None:
            return None
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
            return (
                f"The integration check was still failing when task "
                f"{spec.task_id} closed; the full output is in the task's "
                f"record."
            )
        return None

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
            "The task is finished. Write the record that survives it, as four "
            "sections:\nSUMMARY: what was built and decided.\n"
            "REASONING: why, including alternatives weighed.\n"
            "DEAD ENDS: one line each for anything tried that failed, and why. "
            "Write the lesson, not the transcript.\n"
            "OPEN QUESTIONS: one line each for anything you could not settle "
            "that must not be skated past -- a decision you need made, access "
            "you lacked, a finding left unresolved, a question a worker "
            "raised that you could not answer. Answer questions at your own "
            "level first; raise here only what you genuinely could not "
            "settle, and it will be answered above you or put to the "
            "operator. The orchestrator is required to address every line "
            "here. Omit the section if none."
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
        summary, reasoning, dead_ends, map_notes, open_questions = _parse_closeout(reply)
        if self.config.codebase_map is not None:
            for topic, note in map_notes:
                self.config.codebase_map.amend(
                    topic=topic, note=note, author=lead, session=spec.task_id
                )
        return summary, reasoning, dead_ends, open_questions


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


#: Sanity ceiling on threads for one wave. Not a rate limit -- vendor gates
#: do the throttling per subscription -- just a guard against an orchestrator
#: naming a pathologically wide wave and spawning a thread per line.
_MAX_WAVE_THREADS = 32


#: How many operator questions one decision may spend before it is judged to
#: be interrogating rather than deciding. Three is generous: a decision that
#: genuinely needs more operator input than that is a scoping conversation,
#: which belongs in the interview, not the loop.
_MAX_ASKS_PER_DECISION = 3


#: Defines the label vocabulary for TASK lines so :mod:`multi_llm.task_kinds`
#: can act on them. Kind and difficulty together are the routing decision:
#: the few pinned kinds go where the evidence says, everything else rides the
#: difficulty ladder across the four subscriptions.
_KIND_REQUEST = (
    "On each TASK line, kind is one of: " + ", ".join(sorted(ROUTING)) + ". "
    "Difficulty is one of: rote, simple, standard, complex -- judge it by how "
    "many logical steps the task takes and what breaks if it is wrong. Most "
    "well-sized tasks are simple; reserve complex for genuinely hard "
    "reasoning. Omit either label if none fits."
)


def _parse_wave(reply: str) -> tuple:
    """Split the orchestrator's reply into tasks, rejections, questions, rest.

    Returns ``(tasks, redos, asks, residual)``: ``tasks`` as (kind,
    difficulty, description) tuples from ``TASK <kind> <difficulty>:
    <description>`` lines, ``redos`` as (task_id, objection) tuples from
    ``REDO <task-id>: <objection>`` lines, ``asks`` as operator questions
    from ``ASK: <question>`` lines, and ``residual`` as the remaining lines
    joined -- which is where a legacy single-task reply or a DONE lands.

    Tolerant the same way ``_parse_kind`` is: an unrecognised kind or
    difficulty degrades to the default rather than dropping the task, because
    a mislabelled task costs a routing preference and a dropped one costs the
    work.
    """
    tasks: List[tuple] = []
    redos: List[tuple] = []
    asks: List[str] = []
    residual: List[str] = []
    for line in (reply or "").splitlines():
        stripped = line.strip().lstrip("-• ").strip()
        upper = stripped.upper()
        if (upper.startswith("TASK ") or upper.startswith("TASK:")) and ":" in stripped:
            header, description = stripped.split(":", 1)
            description = description.strip()
            if not description:
                continue
            labels = [p.lower() for p in header.split()[1:]]
            kind = next((p for p in labels if p in ROUTING), TaskKind.GENERAL)
            difficulty = next(
                (p for p in labels if p in Complexity._COLLABORATORS),
                Complexity.SIMPLE,
            )
            tasks.append((kind, difficulty, description))
        elif upper.startswith("REDO ") and ":" in stripped:
            header, objection = stripped.split(":", 1)
            parts = header.split(None, 1)
            task_id = parts[1].strip() if len(parts) > 1 else ""
            if task_id and objection.strip():
                redos.append((task_id, objection.strip()))
        elif upper.startswith("ASK:"):
            question = stripped.split(":", 1)[1].strip()
            if question:
                asks.append(question)
        elif stripped:
            residual.append(stripped)
    return tasks, redos, asks, "\n".join(residual)


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
    """Split a close-out into summary, reasoning, dead ends, map notes and
    open questions.

    Tolerant by design: a missing section degrades to a usable record rather
    than failing the task, since the raw work is stored either way. Reasoning
    falls back to the summary because the ledger refuses an empty one, and a
    weak reason recorded honestly beats a lost task. Map notes that do not
    parse as 'topic: fact' are dropped rather than guessed at -- the map is
    long-lived, so a malformed note is worse there than nowhere.
    """
    sections: Dict[str, List[str]] = {
        "SUMMARY": [], "REASONING": [], "DEAD ENDS": [], "MAP NOTES": [],
        "OPEN QUESTIONS": [],
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
    open_questions = [
        q.lstrip("-• ").strip()
        for q in sections["OPEN QUESTIONS"]
        if q.lstrip("-• ").strip()
        and q.lstrip("-• ").strip().lower() not in ("none", "none.")
    ]
    return summary, reasoning, dead_ends, map_notes, open_questions
