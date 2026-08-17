"""Three memory scopes, one per role.

The system deliberately does not have one memory policy. Each role forgets on
a different schedule, and the schedule is chosen from what that role's job
actually needs.

**Orchestrator -- persistent.** Fable keeps everything for the whole session,
as an append-only :class:`~multi_llm.ledger.Ledger` of completed tasks, each
entry pointing at the full raw work. It is the only participant that persists,
which is why an unavailable orchestrator halts the run rather than being
substituted for. Nobody in the industry runs a coordinator that discards its
own working context; that would be the red flag.

**Brain trust -- task-scoped.** A peer keeps its full working memory for the
duration of one task: every turn, every worker result it commissioned, at full
fidelity. When the task closes it produces one summary and everything else is
dropped. This is the shape the evidence supports from both directions. Within
a task, full context is what makes the work good. Across tasks, carrying the
previous task's transcript is actively harmful: models self-condition on their
own past errors -- and this does not diminish with model size -- and in a
multi-model debate, one peer's output sitting in another's context pulls them
toward agreement, eroding the uncorrelated judgement that is the entire reason
for having several vendors.

**Worker bees -- none.** A worker gets one prompt, does one thing, returns a
summary, and is wiped. It is spun up by a brain-trust member to serve that
member's current task, and it reports back to whoever spawned it.

The asymmetry is the point: continuity where a thread must be held, isolation
where independence is worth more than briefing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .artifacts import ArtifactRef, ArtifactStore
from .ledger import Ledger
from .providers import Turn

__all__ = [
    "NoMemory",
    "TaskMemory",
    "TaskSummary",
    "PersistentMemory",
]


class NoMemory:
    """Worker-bee scope: nothing is retained between invocations.

    Present as a real object rather than an absence so that a worker and a peer
    can be driven through the same code path, and so "this role has no memory"
    is an explicit statement someone can find.
    """

    def turns(self) -> List[Turn]:
        return []

    def record(self, *_args, **_kwargs) -> None:
        """Accepted and discarded. Workers keep nothing by design."""
        return None

    def wipe(self) -> None:
        return None


@dataclass(frozen=True)
class TaskSummary:
    """What survives when a brain-trust member finishes a task.

    Everything else that member accumulated -- its own turns, the raw output of
    every worker it commissioned -- is dropped at this point. This object and
    the artifacts it points at are the whole inheritance.
    """

    task_id: str
    author: str
    summary: str
    reasoning: str
    dead_ends: List[str] = field(default_factory=list)
    refs: List[ArtifactRef] = field(default_factory=list)


class TaskMemory:
    """Brain-trust scope: full fidelity within a task, wiped at its close.

    Worker results commissioned during the task are recorded here in full and
    are visible to the peer for as long as the task is open. They do not
    survive it: only what the peer chooses to carry into its
    :class:`TaskSummary` continues onward.
    """

    def __init__(self, task_id: str, author: str, store: Optional[ArtifactStore] = None):
        self.task_id = task_id
        self.author = author
        self._store = store
        self._turns: List[Turn] = []
        self._refs: List[ArtifactRef] = []
        self._closed = False

    # -- accumulation --------------------------------------------------------
    def turns(self) -> List[Turn]:
        """Full working history for this task, in order."""
        return list(self._turns)

    def record(self, role: str, content: str) -> None:
        if self._closed:
            raise RuntimeError(
                f"task {self.task_id!r} is closed; its memory was wiped and cannot "
                f"be added to. Open a new task."
            )
        self._turns.append(Turn(role, content))

    def record_worker_result(self, worker_label: str, summary: str,
                             refs: Sequence[ArtifactRef] = ()) -> None:
        """Fold a commissioned worker's report into this task's memory.

        The worker reports to the peer that spawned it, not to the
        orchestrator: it was commissioned to serve this task, and its output is
        raw material for that task rather than a finding in its own right.
        """
        self.record("assistant", f"[worker {worker_label}] {summary}")
        self._refs.extend(refs)

    def keep(self, content: str, *, kind: str) -> Optional[ArtifactRef]:
        """Store raw output durably and hold a pointer to it."""
        if self._store is None:
            return None
        ref = self._store.put(content, kind=kind, author=self.author)
        self._refs.append(ref)
        return ref

    @property
    def refs(self) -> List[ArtifactRef]:
        return list(self._refs)

    @property
    def closed(self) -> bool:
        return self._closed

    # -- closure -------------------------------------------------------------
    def close(self, *, summary: str, reasoning: str,
              dead_ends: Optional[Sequence[str]] = None) -> TaskSummary:
        """End the task, emit its summary, and wipe everything else.

        The wipe is not an optimisation. Carrying a finished task's transcript
        into the next one imports the peer's own dead ends as live context and
        makes repeating them more likely, so dropping it is what protects the
        next task's quality.
        """
        if self._closed:
            raise RuntimeError(f"task {self.task_id!r} is already closed")
        if not reasoning.strip():
            raise ValueError(
                "a task summary needs its reasoning: the next reader gets this "
                "and nothing else unless it fetches an artifact"
            )
        result = TaskSummary(
            task_id=self.task_id,
            author=self.author,
            summary=summary,
            reasoning=reasoning,
            dead_ends=list(dead_ends or []),
            refs=list(self._refs),
        )
        self._closed = True
        self._turns = []
        return result

    def wipe(self) -> None:
        """Discard working memory without producing a summary."""
        self._turns = []
        self._refs = []
        self._closed = True


class PersistentMemory:
    """Orchestrator scope: the whole session, as summaries that link back.

    Fable's memory is the ledger. Each completed task contributes one entry;
    entries are never rewritten, and every entry carries pointers to the full
    raw work so a detail the summary skipped can still be recovered. That is
    the difference between a lossy digest and an index over durable originals.
    """

    def __init__(self, goal: str, store: ArtifactStore,
                 invariants: Optional[Sequence[str]] = None):
        if not goal.strip():
            raise ValueError("the orchestrator needs a goal to hold")
        #: Held verbatim. Never paraphrased on any render, because paraphrase
        #: drift across many rebuilds compounds where nobody can see it.
        self.goal = goal
        self.store = store
        self.ledger = Ledger(invariants=invariants)

    def absorb(self, summary: TaskSummary) -> None:
        """Fold a completed task into the orchestrator's memory."""
        self.ledger.append(
            task_id=summary.task_id,
            author=summary.author,
            summary=summary.summary,
            reasoning=summary.reasoning,
            dead_ends=summary.dead_ends,
            refs=summary.refs,
        )

    def fetch(self, ref_or_id) -> str:
        """Open the full original behind a reference.

        The whole reason the ledger carries pointers: when a decision turns on
        a detail the summary skipped, the orchestrator reads the real thing
        rather than trusting the digest.
        """
        return self.store.get(ref_or_id)

    def render(self, *, current: str = "", recent: Optional[int] = None,
               with_previews: bool = True, extra: str = "") -> str:
        """The orchestrator's prompt body for this turn."""
        return self.ledger.render(
            goal=self.goal,
            current=current,
            recent=recent,
            with_previews=with_previews,
            extra=extra,
        )
