"""Worker bees: single-shot helpers commissioned by a brain-trust member.

A peer working a task can commission a worker to fetch, read, check, or draft
something it needs. The worker gets one prompt, holds no memory, returns a
summary plus a pointer to its full output, and is wiped.

Three limits are structural rather than advisory, because each corresponds to
a way this fails in practice:

**Depth is capped at one.** Workers cannot commission workers. Recursive
delegation is how a bounded helper becomes an unbounded fleet, and there is no
depth at which a second layer is obviously worth it.

**Fan-out is capped per task.** Opus 5 is specifically documented to
over-delegate to subagents -- a reversal from its predecessor -- and Anthropic's
guidance is to cap it explicitly rather than trust the model's judgement.

**Cross-provider commissioning is discouraged, not forbidden.** A worker on the
parent's own provider reuses that provider's cached scaffolding prefix; one on
a different provider pays a cold start. Same-provider is the default and
crossing is allowed when a capability genuinely demands it.

Workers report to the peer that spawned them, never to the orchestrator. They
were commissioned to serve one task, and their output is raw material for that
task rather than a finding in its own right. It reaches the orchestrator, if at
all, folded into the peer's task summary.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .artifacts import ArtifactRef, ArtifactStore
from .memory import NoMemory, TaskMemory
from .registry import resolve

log = logging.getLogger(__name__)

__all__ = [
    "WorkerBudget",
    "WorkerResult",
    "WorkerPool",
    "FanOutExceeded",
    "RepeatedFailure",
]


class FanOutExceeded(RuntimeError):
    """A task tried to commission more workers than its budget allows."""


class RepeatedFailure(RuntimeError):
    """The same worker prompt failed once and is being retried verbatim.

    Repeating a failed action unchanged is the canonical autonomous-agent
    death spiral -- Manus's most-reported field failure is exactly this loop,
    burning budget on an action whose outcome is already known. One failure is
    information; an identical retry is a strategy problem, and the fix is a
    different prompt, a different model, or escalating the blocker into the
    task summary -- not a second pull of the same lever.
    """


@dataclass
class WorkerBudget:
    """Limits on commissioning, per task.

    Attributes:
        max_per_task: How many workers one task may commission in total.
        max_depth: Delegation depth. Fixed at 1 in practice -- workers do not
            commission workers -- but expressed as a field so a caller that
            tries to raise it has to do so visibly.
    """

    max_per_task: int = 4
    max_depth: int = 1

    def __post_init__(self) -> None:
        if self.max_per_task < 0:
            raise ValueError("max_per_task cannot be negative")
        if self.max_depth != 1:
            raise ValueError(
                "worker depth is fixed at 1: workers do not commission workers, "
                "because recursive delegation is how a bounded helper becomes an "
                "unbounded fleet"
            )


@dataclass(frozen=True)
class WorkerResult:
    """What a worker hands back to the peer that commissioned it."""

    label: str
    model: str
    #: Commissioned by this peer, and returned to it. Not to the orchestrator.
    parent: str
    summary: str
    ref: Optional[ArtifactRef] = None


@dataclass
class WorkerPool:
    """Commissions workers on behalf of brain-trust members.

    ``run`` is injected rather than hardcoded so the pool can be driven by a
    real provider in production and a fake in tests without either knowing
    about the other.
    """

    store: ArtifactStore
    #: (model_key, prompt) -> raw output.
    run: Callable[[str, str], str]
    budget: WorkerBudget = field(default_factory=WorkerBudget)
    _counts: dict = field(default_factory=dict)
    #: (task_id, prompt-hash) for every commission that raised. A verbatim
    #: retry of a failed prompt is refused; see :class:`RepeatedFailure`.
    _failed: set = field(default_factory=set)

    # -- accounting ----------------------------------------------------------
    def spawned(self, task_id: str) -> int:
        return self._counts.get(task_id, 0)

    def remaining(self, task_id: str) -> int:
        return max(0, self.budget.max_per_task - self.spawned(task_id))

    def _charge(self, task_id: str) -> None:
        if self.remaining(task_id) <= 0:
            raise FanOutExceeded(
                f"task {task_id!r} has already commissioned "
                f"{self.spawned(task_id)} workers, its budget is "
                f"{self.budget.max_per_task}. Raise WorkerBudget.max_per_task "
                f"deliberately, or have the peer do this itself."
            )
        self._counts[task_id] = self.spawned(task_id) + 1

    # -- commissioning -------------------------------------------------------
    def preferred_model(self, parent_key: str, candidate: Optional[str] = None) -> str:
        """Pick the worker's model, defaulting to the parent's own provider.

        Same-provider workers reuse the parent's cached scaffolding prefix,
        which is roughly a tenfold difference on the input side. Crossing
        providers is allowed but should be for a capability reason.
        """
        if candidate is None:
            parent = resolve(parent_key)
            provider = parent.provider if parent else parent_key.split(":", 1)[0]
            return f"{provider}:haiku" if provider == "claude" else parent_key
        parent = resolve(parent_key)
        cand = resolve(candidate)
        if parent and cand and parent.provider != cand.provider:
            log.debug(
                "worker %s crosses provider from parent %s; cold cache expected",
                candidate, parent_key,
            )
        return candidate

    def commission(
        self,
        *,
        task: TaskMemory,
        parent_key: str,
        prompt: str,
        label: str,
        model: Optional[str] = None,
        depth: int = 0,
    ) -> WorkerResult:
        """Run one worker for ``task`` and fold its report into that task.

        The worker itself holds :class:`~multi_llm.memory.NoMemory`: it sees
        only ``prompt``, and nothing of it survives the call except the summary
        and the stored artifact.
        """
        if depth >= self.budget.max_depth:
            raise FanOutExceeded(
                "workers do not commission workers; delegation depth is capped at 1"
            )
        if task.closed:
            raise RuntimeError(
                f"task {task.task_id!r} is closed and cannot commission workers"
            )
        fingerprint = (task.task_id, hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16])
        if fingerprint in self._failed:
            raise RepeatedFailure(
                f"this exact prompt already failed for task {task.task_id!r}. "
                f"Retrying it verbatim is how an agent loops on a known-bad "
                f"action: rephrase it, try a different model, or record the "
                f"blocker in the task summary and move on."
            )
        self._charge(task.task_id)

        model_key = self.preferred_model(parent_key, model)
        scratch = NoMemory()  # explicit: a worker carries nothing in or out
        try:
            raw = self.run(model_key, prompt)
        except Exception:
            self._failed.add(fingerprint)
            raise
        scratch.wipe()

        ref = self.store.put(raw, kind=f"worker:{label}", author=model_key)
        summary = self._summarise(raw)
        result = WorkerResult(
            label=label, model=model_key, parent=parent_key, summary=summary, ref=ref
        )
        # Reports to the commissioning peer, which decides what, if anything,
        # of this reaches the orchestrator when the task closes.
        task.record_worker_result(label, summary, refs=[ref])
        return result

    @staticmethod
    def _summarise(raw: str, *, max_chars: int = 1200) -> str:
        """Trim a worker's output for the peer's working memory.

        Truncation rather than model-written summarisation on purpose: this
        text lives inside one open task alongside a pointer to the full
        original, so nothing is lost, and inserting another model here would
        add a lossy hop where none is needed.
        """
        raw = (raw or "").strip()
        if len(raw) <= max_chars:
            return raw
        return raw[:max_chars].rstrip() + " […truncated; see artifact]"

    def results_for(self, task_id: str) -> List[str]:
        return [task_id] * self.spawned(task_id)
