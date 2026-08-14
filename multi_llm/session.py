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
from .memory import PersistentMemory, TaskMemory, TaskSummary
from .registry import peers_for, resolve
from .routing import (
    Seat,
    WorkClass,
    orchestrator_seat,
    route_security_work,
)
from .task_kinds import MAX_TASK_LINES, ROUTING, TaskKind, guidance_for, policy_for
from .task_kinds import route as route_kind
from .workers import WorkerBudget, WorkerPool

log = logging.getLogger(__name__)

__all__ = ["Complexity", "TaskSpec", "Session", "SessionConfig"]


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
    """How many brain-trust members a task is worth.

    Collaborators are not free: each one is another full invocation and another
    voice in the lead's working memory. Scaling by complexity is the same
    guidance Anthropic gives its own lead agents -- simple fact-finding gets one
    agent, complex work gets many -- rather than convening everyone by default.
    """

    SIMPLE = "simple"      # lead alone
    STANDARD = "standard"  # lead + one collaborator
    COMPLEX = "complex"    # lead + the whole brain trust

    _COLLABORATORS = {SIMPLE: 0, STANDARD: 1, COMPLEX: None}

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


@dataclass
class SessionConfig:
    mode: str = "adversarial"
    worker_budget: WorkerBudget = field(default_factory=WorkerBudget)
    #: Render only the last N ledger entries into the orchestrator's prompt.
    #: Earlier entries and every artifact stay reachable; this narrows the
    #: view rather than discarding anything.
    recent_entries: Optional[int] = None


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
        others = [p for p in self.brain_trust if p != lead]
        chosen = others[: Complexity.collaborator_count(spec.complexity, len(others))]
        if spec.kind == TaskKind.REVIEW:
            for peer in policy_for(TaskKind.REVIEW).prefer:
                if peer != lead and peer in others and peer not in chosen:
                    chosen.append(peer)
        return chosen

    # -- one task ------------------------------------------------------------
    def run_task(self, spec: TaskSpec) -> TaskSummary:
        """Work one task to completion and fold it into the ledger."""
        lead = self._pick_lead(spec)
        if spec.work_class == WorkClass.SECURITY:
            lead = route_security_work(
                lead, work_class=WorkClass.SECURITY, available=self._available
            )
        collaborators = self.collaborators_for(spec, lead)

        task = TaskMemory(spec.task_id, lead, self.store)
        task.record("user", spec.description)

        # The lead drafts with full working memory.
        draft = self.invoke(lead, self._lead_prompt(spec))
        task.record("assistant", draft)
        task.keep(draft, kind="draft")

        # Collaborators contribute into the lead's working memory. They see the
        # task and the draft, not the whole session: their value is an
        # independent read, which inheriting the lead's history would erode.
        for peer in collaborators:
            note = self.invoke(peer, self._collaborator_prompt(spec, draft, peer))
            task.record("assistant", f"[{peer}] {note}")
            task.keep(note, kind=f"review:{peer}")

        summary_text, reasoning, dead_ends = self._close_out(lead, spec, task)
        summary = task.close(
            summary=summary_text, reasoning=reasoning, dead_ends=dead_ends
        )
        self.memory.absorb(summary)
        self.history.append(summary)
        return summary

    # -- orchestration -------------------------------------------------------
    def next_task(self) -> Optional[TaskSpec]:
        """Ask the orchestrator what to do next, given the ledger.

        Returns None when it reports the goal met. The orchestrator sees
        summaries with pointers, so if the decision turns on a detail a summary
        skipped it can fetch the original rather than guess.
        """
        seat = self.seat()
        reply = self.invoke(
            seat.key,
            self.memory.render(
                current=(
                    "Name the single next task, or reply exactly DONE if the goal "
                    "is met. Use the artifact pointers above if a detail matters."
                    f"\n\n{_SIZE_CEILING}\n\n{_KIND_REQUEST}"
                ),
                recent=self.config.recent_entries,
            ),
        )
        if reply.strip().upper().startswith("DONE"):
            return None
        kind, description = _parse_kind(reply)
        return TaskSpec(
            task_id=f"t{len(self.history) + 1}",
            description=description,
            kind=kind,
        )

    def run(self, *, max_tasks: int = 20) -> List[TaskSummary]:
        """Drive tasks until the orchestrator says DONE or the cap is hit.

        The cap is a runaway backstop, not a quality gate: a loop that has not
        converged by then has a problem the cap will not fix, and the caller
        should look at why.
        """
        for _ in range(max_tasks):
            spec = self.next_task()
            if spec is None:
                break
            self.run_task(spec)
        return list(self.history)

    # -- prompts -------------------------------------------------------------
    def _lead_prompt(self, spec: TaskSpec) -> str:
        parts = [
            self.memory.render(
                current=spec.description, recent=self.config.recent_entries
            ),
            "You are leading this task. Produce the complete work.",
        ]
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
        return "\n\n".join(parts)

    def _collaborator_prompt(self, spec: TaskSpec, draft: str, peer: str) -> str:
        label = resolve(peer)
        return (
            f"Task: {spec.description}\n\n"
            f"Current work:\n{draft}\n\n"
            f"You are {label.label if label else peer}, contributing an independent "
            "read. Report everything you find with a severity and a confidence; do "
            "not filter to only the important ones. Filtering happens downstream, "
            "and a reviewer told to be selective suppresses its own findings."
        )

    def _close_out(self, lead: str, spec: TaskSpec, task: TaskMemory):
        """Have the lead write the one thing that survives the task."""
        transcript = "\n\n".join(f"[{t.role}] {t.content}" for t in task.turns())
        reply = self.invoke(
            lead,
            f"Task: {spec.description}\n\n{transcript}\n\n"
            "The task is finished. Write the record that survives it, as three "
            "sections:\nSUMMARY: what was built and decided.\n"
            "REASONING: why, including alternatives weighed.\n"
            "DEAD ENDS: one line each for anything tried that failed, and why. "
            "Write the lesson, not the transcript.",
        )
        return _parse_closeout(reply)


#: Asks the orchestrator to label the task so :mod:`multi_llm.task_kinds` can
#: act on it. Optional by design -- an unlabelled task rotates, which is what
#: most labels would have produced anyway.
_KIND_REQUEST = (
    "Begin your reply with a single line 'KIND: <kind>' choosing from: "
    + ", ".join(sorted(ROUTING)) + ". Then the task on the following line. "
    "Omit the line if none fits."
)


def _parse_kind(reply: str) -> tuple:
    """Split an optional leading ``KIND:`` line off the orchestrator's reply.

    Tolerant in the same way close-out parsing is: an unrecognised or absent
    kind degrades to GENERAL rather than failing the round. A mislabelled task
    costs a routing preference; a rejected round costs the task.
    """
    text = (reply or "").strip()
    lines = text.splitlines()
    if lines and lines[0].strip().upper().startswith("KIND:"):
        claimed = lines[0].split(":", 1)[1].strip().lower()
        rest = "\n".join(lines[1:]).strip()
        if claimed in ROUTING and rest:
            return claimed, rest
        if rest:
            log.debug("orchestrator proposed unknown task kind %r", claimed)
            return TaskKind.GENERAL, rest
    return TaskKind.GENERAL, text


def _parse_closeout(reply: str):
    """Split a close-out into summary, reasoning and dead ends.

    Tolerant by design: a missing section degrades to a usable record rather
    than failing the task, since the raw work is stored either way. Reasoning
    falls back to the summary because the ledger refuses an empty one, and a
    weak reason recorded honestly beats a lost task.
    """
    sections: Dict[str, List[str]] = {"SUMMARY": [], "REASONING": [], "DEAD ENDS": []}
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
    return summary, reasoning, dead_ends
