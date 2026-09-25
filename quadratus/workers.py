"""Worker bees: single-shot helpers commissioned by a brain-trust member.

A peer working a task can commission a worker to fetch, read, check, or draft
something it needs. The worker gets one prompt, holds no memory, returns a
summary plus a pointer to its full output, and is wiped.

Three limits are structural rather than advisory, because each corresponds to
a way this fails in practice:

**Depth is capped at one.** Workers cannot commission workers. Recursive
delegation is how a bounded helper becomes an unbounded fleet, and there is no
depth at which a second layer is obviously worth it.

**Fan-out is capped two ways.** ``max_concurrent`` bounds how many workers run
at once; ``max_per_task`` is a lifetime ceiling per task, set high enough that
a lead can rewrite failed errands or reroute them to a different worker
without going back through the orchestrator -- a full re-route through the
orchestrator costs far more than a few extra worker calls -- but low enough
that a confused lead cannot burn a window. Opus 5 is specifically documented
to over-delegate to subagents, so the caps are structural, not advisory.

**Workers are picked by errand, not by vendor loyalty.** The worker tree
(:data:`WORKER_TREE`) maps what the errand *is* to the cheap model measured
or reputed best at it: live lookup to Grok Fast, long reading to Luna, fact
and code checking and anything visual to Haiku, rote formatting and drafting
to Luna, with an in-family bump for errands that defeat the cheap tier. An
earlier design defaulted workers to the lead's own vendor to reuse cached
prefixes; that was retired -- each vendor's cache stays warm as long as the
vendor gets regular use, which the tree itself guarantees, and the
vendor-loyal default would have sent web lookups to models with stale
knowledge.

The tree lost a vendor on 2026-09-12 when Google left the lineup, and the two
errands it held moved on different reasoning. Long reading went to Luna,
which has the widest window among the cheap models that is corroborated by
something other than its own vendor's marketing -- Grok 4.1 Fast advertised
2M and the registry pointedly declined to believe it. Visual reading went to
Haiku, because the Claude CLI reads image files off disk directly and that
capability, not the window, is what the errand needs.

Workers report to the peer that spawned them, never to the orchestrator. They
were commissioned to serve one task, and their output is raw material for that
task rather than a finding in its own right. It reaches the orchestrator, if at
all, folded into the peer's task summary.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from .artifacts import ArtifactRef, ArtifactStore
from .delegation import invocation, invocation_context
from .memory import NoMemory, TaskMemory

log = logging.getLogger(__name__)
worker_loop_control = ContextVar('worker_loop_control', default=None)


@dataclass
class WorkerLoopControl:
    """Per-errand transport limits, in addition to the enclosing run budget.

    Tokens are a post-return threshold. Unknown usage stops continuation and
    in-flight overshoot is retained in the budget artifact.
    """
    max_steps: int
    max_tokens: int
    charge: Callable
    calls: int = 0
    tokens: int = 0
    unknown: bool = False

    def reserve(self):
        from .run_budget import RunBudgetExceeded
        if self.calls >= self.max_steps or self.tokens >= self.max_tokens or self.unknown:
            raise RunBudgetExceeded('Worker loop budget exhausted')
        if self.calls:
            self.charge()
        self.calls += 1

    def finish(self, usage):
        from .run_budget import RunBudgetExceeded
        values = [(usage or {}).get(k) for k in ('input_tokens', 'output_tokens')]
        if any(type(v) is not int or v < 0 for v in values):
            self.unknown = True
            raise RunBudgetExceeded('Worker loop usage unknown; continuation refused')
        self.tokens += sum(values)
        if self.tokens >= self.max_tokens:
            raise RunBudgetExceeded('Worker loop reported-token threshold reached')

__all__ = [
    "WorkerBudget",
    "WorkerResult",
    "WorkerPool",
    "FanOutExceeded",
    "RepeatedFailure",
    "ErrandToolMismatch",
    "WORKER_TREE",
    "pick_worker",
    "worker_menu",
    "worker_capabilities",
    "check_errand_fit",
    "capability_preamble",
]


class FanOutExceeded(RuntimeError):
    """A task tried to commission more workers than its budget allows."""


class ErrandToolMismatch(ValueError):
    """The errand needs an operation the worker seat cannot perform.

    Raised before the call is made, so a mis-scoped errand costs nothing. It
    exists because of what attempt 3 of the blind acceptance measured: a lead
    sent a Haiku worker the whole implementation with ``write:false``, and the
    worker, never told what it had or that it could ask, worked around the
    missing tools by writing 101KB of code and tests as prose across eleven
    turns. That is 312,518 tokens, sixty percent of the run, for output that
    could not land. The seat's restrictions held perfectly the whole time:
    they bound what it may do, not whether the errand was possible.
    """


class RepeatedFailure(RuntimeError):
    """The same prompt is being retried verbatim on the same model.

    Repeating a failed action unchanged is the canonical autonomous-agent
    death spiral -- Manus's most-reported field failure is exactly this loop,
    burning budget on an action whose outcome is already known. One failure is
    information; an identical retry is a strategy problem. Deliberately
    narrow: the *same prompt on a different model* is allowed, because that is
    a changed strategy -- a lookup that failed on a model with stale knowledge
    is rationally re-sent, unchanged, to the scout.
    """


#: Errand -> the cheap model best suited to it, one per vendor, so picking by
#: skill also spreads load across every subscription window. Seeds, not
#: truth: only Haiku is verified in this harness, and the tier churns monthly.
WORKER_TREE: dict = {
    #: Live web lookup, anything current. Grok's strength is the search itself.
    "lookup": "grok:worker",
    #: Read something long and digest it. The widest cheap window that is not
    #: an unverified vendor claim (1.1M against Haiku's 200K).
    "read": "openai:gpt-5.6-luna",
    #: Read a screenshot, PDF, or image. Picked for the capability rather than
    #: the window: the Claude CLI reads image files off disk directly.
    "visual": "claude:haiku",
    #: Check a fact, verify a claim, sanity-check code. The tier's best brain,
    #: but its knowledge is old -- anything current goes to lookup instead.
    "check": "claude:haiku",
    #: Small code errand: a snippet, explain a function.
    "code": "claude:haiku",
    #: Format, tag, sort, extract fields, draft boilerplate. Literal-minded
    #: is a feature here.
    "format": "openai:gpt-5.6-luna",
    "draft": "openai:gpt-5.6-luna",
}

#: Escalation stays in the family: an errand the base worker could not do,
#: but which still belongs to that worker's skill (a lookup stays a lookup),
#: bumps one tier up the same vendor's line rather than jumping sideways to a
#: different skill set. Operator directive. xAI is the exception that proves
#: the rule: a consumer subscription reaches one model line, so there is no
#: second model to bump to, and the bump is a reasoning-effort step on the
#: same line instead -- ``grok:worker`` (low) to ``grok:expert`` (high). That
#: is the honest version of what the old Grok Fast -> Grok 4.20 bump claimed,
#: with two IDs this transport rejects. Both ends are probe-verified. Note
#: what the degradation below does and does not cover -- resolve_model falls
#: back to the base when a bump target is missing from the *roster*, which is
#: not the same as the target being unreachable; a row that resolves but
#: cannot be invoked passes that check and fails at transport instead.
WORKER_ESCALATION: dict = {
    "claude:haiku": "claude:sonnet",
    "openai:gpt-5.6-luna": "openai:gpt-5.6-terra",
    "grok:worker": "grok:expert",
}

#: The generalist default when the lead names no errand: the tier's most
#: accurate brain, least likely to be confidently wrong.
DEFAULT_WORKER = "claude:haiku"


def pick_worker(errand: Optional[str] = None, *, demanding: bool = False) -> str:
    """The worker tree: skill picks the family, difficulty picks the tier.

    ``demanding`` bumps the errand's base worker one tier up its own vendor's
    line -- the skill stays matched, the horsepower increases. A bump target
    missing from the roster degrades to the base rather than failing the
    errand.
    """
    if errand is None:
        base = DEFAULT_WORKER
    else:
        base = WORKER_TREE.get(errand.strip().lower(), DEFAULT_WORKER)
    if demanding:
        from .registry import resolve
        bumped = WORKER_ESCALATION.get(base, base)
        return bumped if resolve(bumped) is not None else base
    return base


def worker_menu() -> str:
    """The tree as a prompt block, so leads pick workers deliberately."""
    return (
        "Worker bees (one instruction each, no memory; several may run at "
        "once). Pick by errand:\n"
        "- lookup: live web / anything current -> Grok\n"
        "- read: long document or file -> Luna\n"
        "- visual: screenshot, PDF, image -> Haiku\n"
        "- check: verify a fact or claim, sanity-check code -> Haiku "
        "(accurate, but its knowledge is old -- current things go to lookup)\n"
        "- code: small snippet or explanation -> Haiku\n"
        "- format / draft: extract, tag, boilerplate -> Luna\n"
        "- demanding: the errand defeated the base worker but the skill still "
        "fits -> same family, one tier up (Haiku->Sonnet, Luna->Terra, "
        "Grok worker->Grok expert). Sparingly.\n"
        "\nWhat a worker can do, before you write the errand. Every worker "
        "reads a fresh source copy. With write:false it answers in text. With "
        "write:true it is a bounded editor: it returns a patch the harness "
        "applies. No worker on any vendor can run a command or write a file "
        "itself, so an errand that needs a shell is yours to do. Declare the "
        "errand's needs and the harness checks the fit before the call is "
        "spent: a mismatch costs you this sentence, not a window.\n"
        "If an errand fails: rewrite it, re-send it unchanged to a different "
        "worker, or mark it demanding -- never the same instruction to the "
        "same worker twice. A worker that lacked a tool it needed will say "
        "NEED TOOL; reissue that one errand with the grant. That reissue is "
        "the whole escalation -- two calls, not four."
    )


#: What a worker seat can actually do, by grant. Every seat in the worker tree
#: is a restricted roster row, and a restricted row's whole capability set is
#: :data:`~quadratus.task_kinds.Need.PATCH` -- it returns a diff the harness
#: applies. So no worker, on any vendor, at any grant, can run a command or
#: write a file itself. That is not a gap to be widened: a worker is a
#: one-shot helper, and the two boundaries the lead keeps crossing are worth
#: stating in the same words to both sides.
def worker_capabilities(allow_writes: bool):
    """The operations a worker may perform under this grant."""
    from .task_kinds import Need
    return frozenset({Need.PATCH}) if allow_writes else frozenset()


def check_errand_fit(instruction: str, *, needs=None, write: bool = False, answer_only: bool = False):
    """Refuse a mis-scoped errand before any call is made.

    ``needs`` is what the lead declared, in the vocabulary of
    :data:`~quadratus.task_kinds.KNOWN_NEEDS`. It is checked together with
    what the instruction itself asks for, because the expensive mistake is the
    errand whose text says "run pytest" while its declaration says nothing.

    ``None`` means the lead declared nothing, which is different from
    declaring an empty list. Silence is read from the instruction and never
    treated as a contradiction: only a lead that *stated* what the errand
    needs can be told its grant disagrees with that statement.

    ``answer_only`` is the in-session tool's contract: no grant, no
    declared needs, and the answer is text by construction. Reading needs
    from the wording there protects nothing and refuses good errands -- on
    GameTape (2026-09-25) "Report only ... quote the first 40 lines of app.py"
    was refused as a patch because it also named the helper the lead meant to
    insert, and the lead went back to exploring on its own.

    Returns ``None`` when the errand fits, or one sentence naming the
    mismatch and the lead's move. Unknown need labels raise, as they do at
    decomposition: a label that was meant and then dropped is a silent
    misroute.
    """
    from .task_kinds import Need, needs_from_text, normalise_needs

    declared = None if needs is None else normalise_needs(needs)
    inferred = frozenset() if answer_only else needs_from_text(instruction or "")
    wanted = (declared or frozenset()) | inferred
    have = worker_capabilities(write)
    if Need.EXECUTE in wanted:
        return (
            "this errand needs to run commands, and no worker seat can: every "
            "worker is a restricted seat with the shell denied. Run it "
            "yourself, or send the worker the part that does not need a shell."
        )
    if Need.DIRECT_WRITE in wanted:
        return (
            "this errand needs to write files directly, and a worker never "
            "does: it returns a patch the harness applies. Ask for patch "
            "instead, or make the edit yourself."
        )
    if Need.PATCH in wanted and Need.PATCH not in have:
        return (
            "this errand produces changes to files but was sent without a "
            "write grant, so the worker can only describe them. Reissue it "
            "with write:true, which puts the worker in bounded-editor mode."
        )
    if Need.PATCH in have and Need.PATCH not in wanted and declared is not None:
        return (
            "this errand was given a write grant it does not need. Send it "
            "with write:false, or say what it is meant to change."
        )
    return None


def capability_preamble(allow_writes: bool) -> str:
    """Told to the worker, in its own prompt, before the errand.

    The worker used to be handed the lead's instruction and nothing else: not
    what tools it had, not that ``NEED TOOL`` existed. ``worker_menu`` told
    the *lead* about that channel and the worker was never in the room. A
    helper that does not know it may ask will instead do the best it can with
    what it has, which is how an errand it could not perform became prose.
    """
    if allow_writes:
        tools = (
            "You can read the source copy in your working directory, and you "
            "return changes as a patch: exactly PATCH: followed by a fenced "
            "unified diff. You have no write tools and no shell."
        )
    else:
        tools = (
            "You can read the source copy in your working directory. You "
            "cannot change files and you have no shell."
        )
    return (
        "## What you can do\n" + tools + "\n"
        "Check this against the errand before you start. If it cannot be done "
        "with these, reply with only 'NEED TOOL: <what you need>' and nothing "
        "else -- that is one line, not an attempt. Asking is cheap and the "
        "errand comes back to you with the grant. Do not work around a "
        "missing tool by writing the change out as text: an answer the "
        "harness cannot apply is the expensive way to fail.\n"
    )


@dataclass
class WorkerBudget:
    """Limits on commissioning, per task.

    Attributes:
        max_per_task: Lifetime ceiling per task -- a runaway backstop, sized
            so a lead can rewrite failed errands or reroute them to another
            worker without a costly round trip through the orchestrator.
        max_concurrent: How many workers may run at once.
        max_depth: Delegation depth. Fixed at 1 in practice -- workers do not
            commission workers -- but expressed as a field so a caller that
            tries to raise it has to do so visibly.
    """

    max_per_task: int = 12
    max_concurrent: int = 4
    max_depth: int = 1
    max_worker_steps: int = 3
    max_worker_tokens: int = 50_000

    def __post_init__(self) -> None:
        for name in ('max_worker_steps', 'max_worker_tokens'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f'{name} must be a positive integer')
        if self.max_per_task < 0:
            raise ValueError("max_per_task cannot be negative")
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
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
    #: Set when the worker said it needs a tool it was not granted. The lead's
    #: move is to reissue the errand with the grant; the worker that asked is
    #: already gone, per the one-prompt-no-memory pattern.
    needs_tool: Optional[str] = None
    #: Set instead of raising when the worker ran inside a parallel batch --
    #: one failed errand must not tear down its siblings.
    error: Optional[str] = None


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
    #: (task_id, model, prompt-hash) for every commission that raised. A
    #: verbatim retry on the same model is refused; see :class:`RepeatedFailure`.
    _failed: set = field(default_factory=set)
    #: (task_id, prompt-hash) for every errand a worker has already answered
    #: with NEED TOOL. Bounds the escalation to two calls; see ``commission``.
    _asked: set = field(default_factory=set)
    #: Guards counts, failure fingerprints, the store, and task memory --
    #: everything commission_many touches from several threads at once.
    _lock: threading.Lock = field(default_factory=threading.Lock)

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
    def resolve_model(
        self,
        candidate: Optional[str] = None,
        *,
        errand: Optional[str] = None,
        demanding: bool = False,
    ) -> str:
        """An explicit choice wins; otherwise the worker tree decides."""
        if candidate is not None:
            return candidate
        return pick_worker(errand, demanding=demanding)

    def commission(
        self,
        *,
        task: TaskMemory,
        parent_key: str,
        prompt: str,
        label: str,
        model: Optional[str] = None,
        errand: Optional[str] = None,
        demanding: bool = False,
        allow_writes: bool = False,
        needs: Optional[Sequence[str]] = None,
        depth: int = 0,
        steps: int = 1,
        token_limit: Optional[int] = None,
        answer_only: bool = False,
    ) -> WorkerResult:
        """Run one worker for ``task`` and fold its report into that task.

        The worker itself holds :class:`~quadratus.memory.NoMemory`: it sees
        only ``prompt``, and nothing of it survives the call except the summary
        and the stored artifact. A worker that needed a tool it lacked says
        ``NEED TOOL: <what>`` and the result carries it; the lead reissues the
        errand with ``allow_writes`` (or the specific grant) set.
        """
        if depth >= self.budget.max_depth or (invocation_context.get() or {}).get('origin') == 'worker':
            raise FanOutExceeded(
                "workers do not commission workers; delegation depth is capped at 1"
            )
        token_limit = self.budget.max_worker_tokens if token_limit is None else token_limit
        if (type(steps) is not int or not 1 <= steps <= self.budget.max_worker_steps
                or type(token_limit) is not int or not 1 <= token_limit <= self.budget.max_worker_tokens):
            raise ValueError('Worker step/token limits exceed the configured allowance')
        if task.closed:
            raise RuntimeError(
                f"task {task.task_id!r} is closed and cannot commission workers"
            )
        # Checked before the budget is charged and before any call: a
        # mis-scoped errand should cost the lead a sentence, not a window.
        mismatch = check_errand_fit(prompt, needs=needs, write=allow_writes, answer_only=answer_only)
        if mismatch is not None:
            raise ErrandToolMismatch(f"errand {label!r}: {mismatch}")
        model_key = self.resolve_model(model, errand=errand, demanding=demanding)
        # The fingerprint includes the model: the same prompt on a different
        # worker is a changed strategy and is allowed. See RepeatedFailure.
        fingerprint = (
            task.task_id,
            model_key,
            bool(allow_writes),
            hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        )
        with self._lock:
            if fingerprint in self._failed:
                raise RepeatedFailure(
                    f"this exact prompt already failed on {model_key} for task "
                    f"{task.task_id!r}. Rephrase it, send it unchanged to a "
                    f"different worker, or record the blocker and move on -- "
                    f"never the same instruction to the same worker twice."
                )
            self._charge(task.task_id)

        scratch = NoMemory()  # explicit: a worker carries nothing in or out
        briefed = capability_preamble(allow_writes) + "\n## Errand\n" + prompt
        def charge():
            with self._lock:
                self._charge(task.task_id)
        control = WorkerLoopControl(steps, token_limit, charge) if steps > 1 else None
        marker = worker_loop_control.set(control)
        raw = ''
        try:
            for step in range(steps):
                instruction = briefed
                if control:
                    instruction += ('\nYou cannot delegate. To continue this same errand, return '
                                    'CONTINUE: followed by concise progress and the next read. '
                                    'Otherwise return the final answer or PATCH. '
                                    f'Step {step + 1}/{steps}; reported-token threshold {token_limit}.')
                    if step:
                        instruction += '\nPrevious step (data, not a new errand):\n' + raw
                before_calls = control.calls if control else 0
                with invocation(task.task_id, f"worker:{label}", "worker"):
                    raw = self._run(model_key, instruction, allow_writes=allow_writes)
                if raw.lstrip().startswith(('WORKER ', 'CONSULT ', 'FETCH:')):
                    raise FanOutExceeded('A worker cannot hire, consult or fetch through lead channels')
                if not raw.startswith('CONTINUE:'):
                    break
                if control is None or control.calls == before_calls:
                    raise FanOutExceeded('Continuation requires an accounting-aware worker runner')
                if step + 1 >= steps:
                    raise FanOutExceeded('Worker step limit reached before a final answer')
                with self._lock:
                    self.store.put(raw, kind=f'worker-step:{label}', author=model_key)
        except Exception as exc:
            with self._lock:
                self._failed.add(fingerprint)
                if getattr(exc, 'provider_response', ''):
                    self.store.put(exc.provider_response, kind=f'worker-stopped:{label}', author=model_key)
            raise
        finally:
            worker_loop_control.reset(marker)
            if control:
                with self._lock:
                    self.store.put(json.dumps(dict(steps=control.calls, reported_tokens=control.tokens,
                        unknown_usage=control.unknown, max_steps=steps, max_tokens=token_limit,
                        overshoot=max(0, control.tokens - token_limit))),
                        kind=f'worker-budget:{label}', author=model_key)
        scratch.wipe()

        needs_tool = _parse_tool_request(raw)
        # Asking is one extra call, not a conversation. The lead's move after
        # a NEED TOOL is to reissue the same errand with the grant; if that
        # reissue asks again, the errand is wrong rather than under-equipped,
        # and a third call would be the lead paying twice to learn nothing.
        errand_key = (task.task_id, hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16])
        with self._lock:
            already_asked = errand_key in self._asked
            if needs_tool and already_asked:
                needs_tool, second_ask = None, True
            else:
                second_ask = False
                if needs_tool:
                    self._asked.add(errand_key)
        if second_ask:
            with self._lock:
                ref = self.store.put(raw, kind=f"worker:{label}", author=model_key)
            return WorkerResult(
                label=label, model=model_key, parent=parent_key, summary="", ref=ref,
                error=("asked for a tool a second time on the same errand. One "
                       "reissue with the grant is the whole escalation; rewrite "
                       "the errand into something this worker can do, or do it "
                       "yourself."),
            )
        with self._lock:
            ref = self.store.put(raw, kind=f"worker:{label}", author=model_key)
            summary = self._summarise(raw)
            if needs_tool:
                summary = f"[worker needs a tool: {needs_tool}] {summary}"
            result = WorkerResult(
                label=label, model=model_key, parent=parent_key,
                summary=summary, ref=ref, needs_tool=needs_tool,
            )
            # Reports to the commissioning peer, which decides what, if
            # anything, of this reaches the orchestrator when the task closes.
            task.record_worker_result(label, summary, refs=[ref])
        return result

    def commission_many(
        self,
        *,
        task: TaskMemory,
        parent_key: str,
        jobs: Sequence[dict],
    ) -> List[WorkerResult]:
        """Run several errands at once, bounded by ``budget.max_concurrent``.

        Workers are one-shot and independent, so nothing orders them; running
        them together is pure wall-clock savings. A failed errand becomes a
        result carrying ``error`` rather than tearing down its siblings -- the
        lead reads all the outcomes together and decides what to rewrite or
        reroute.

        Each job is a dict with ``prompt`` and ``label``, plus optional
        ``model``, ``errand``, ``demanding``, ``allow_writes``, ``needs``.
        """
        results: List[Optional[WorkerResult]] = [None] * len(jobs)
        if (invocation_context.get() or {}).get('origin') == 'worker':
            raise FanOutExceeded('Workers cannot commission sibling workers')

        def one(index: int, job: dict) -> None:
            try:
                results[index] = self.commission(
                    task=task, parent_key=parent_key, **job
                )
            except Exception as exc:  # noqa: BLE001 -- reported, not raised
                label = job.get("label", f"job-{index}")
                with self._lock:
                    task.record(
                        "assistant", f"[worker {label}] FAILED: {str(exc)[:300]}"
                    )
                results[index] = WorkerResult(
                    label=label,
                    model=job.get("model") or self.resolve_model(
                        None, errand=job.get("errand"),
                        demanding=job.get("demanding", False),
                    ),
                    parent=parent_key,
                    summary="",
                    error=str(exc)[:300],
                )

        with ThreadPoolExecutor(max_workers=self.budget.max_concurrent) as pool:
            futures = [pool.submit(one, i, dict(job)) for i, job in enumerate(jobs)]
            for future in futures:
                future.result()
        return [r for r in results if r is not None]

    def _run(self, model_key: str, prompt: str, *, allow_writes: bool) -> str:
        """Invoke the injected runner, passing the grant if it accepts one."""
        try:
            inspect.signature(self.run).bind(model_key, prompt, allow_writes=allow_writes)
        except (TypeError, ValueError):
            if allow_writes:
                raise PermissionError("worker runner cannot honor the requested write grant") from None
            return self.run(model_key, prompt)
        return self.run(model_key, prompt, allow_writes=allow_writes)

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


def _parse_tool_request(raw: str) -> Optional[str]:
    """A worker that lacked a tool says so as 'NEED TOOL: <what>' anywhere in
    its answer. Parsed leniently: workers are one-shot and cannot be asked to
    reformat."""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("NEED TOOL:"):
            want = stripped[len("NEED TOOL:"):].strip()
            if want:
                return want
    return None
