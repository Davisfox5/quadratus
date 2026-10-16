"""Opt-in, shared run limits. Reported tokens are a stop threshold, not a cap.

Reservations happen before every transport attempt (including retries). Already
running calls may overshoot the token threshold; no new attempt can start after
it is reached. A supervisor must enforce the whole-process wall deadline.
"""

from __future__ import annotations

import json
import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path


class RunBudgetExceeded(RuntimeError):
    """Terminal run control, deliberately not a recoverable ProviderError."""


class _Deferred(Exception):
    """Internal to ``RunBudget.reserve``: only calls in flight keep this one
    from fitting beside the held finish."""

    def __init__(self, behind):
        super().__init__("deferred behind calls in flight")
        self.behind = list(behind)


class FinishReserved(RunBudgetExceeded):
    """An ordinary call refused before it started because it would spend the
    capacity held for the run's finishing judgments (``FINISH_STAGES``).

    Not latched: nothing about the run's own allowance is exhausted, so the
    finishing calls themselves may still start. Every later ordinary call is
    refused the same way (spend and attempts only grow), and the work it
    would have done stays undone and on the record: a refused review is not
    a review, a refused recheck resolves nothing."""


#: The run's finishing judgments, in order (Davis, 2026-10-16): the task
#: cap's goal question, then the independent requirements audit when the
#: goal answer permits it. Each is held one call and the operator's full
#: per-call reserve from ordinary work; neither takes a smaller shape.
FINISH_STAGES = ("goal", "audit")

#: The finishing stage the current call belongs to (Session sets it around
#: the goal question and the audit). None is ordinary work.
finish_stage: ContextVar = ContextVar("finish_stage", default=None)


#: How the current call is admitted against the held finish (Fleet sets it
#: around each seat call): ``editing`` when the call holds the operator's
#: write grant on the project, ``may_wait`` when no call it depends on is in
#: flight (a worker served inside its lead's call never waits: the lead it
#: would wait on is its own parent). Outside a Fleet call: neither.
admission: ContextVar = ContextVar("budget_admission", default=None)


@contextmanager
def admitted_as(*, editing: bool, may_wait: bool):
    """Mark the calls made inside as editing (or not) and as free to wait
    for in-flight calls to settle (or not)."""
    token = admission.set(dict(editing=bool(editing), may_wait=bool(may_wait)))
    try:
        yield
    finally:
        admission.reset(token)


@contextmanager
def finishing(stage):
    """Mark the calls made inside as ``stage`` of the finish."""
    if stage not in FINISH_STAGES:
        raise ValueError(f"unknown finishing stage {stage!r}")
    token = finish_stage.set(stage)
    try:
        yield
    finally:
        finish_stage.reset(token)


@dataclass(frozen=True)
class APICostRate:
    """Operator-supplied upper rates for an exact vendor:model API identifier.

    Input rate must cover cache reads/writes and any context or service tier
    used by the run. These are enforcement inputs, not usage.py seed prices.
    """

    model: str
    input_per_mtok: float
    output_per_mtok: float
    source: str

    def __post_init__(self):
        if not isinstance(self.model, str) or ':' not in self.model:
            raise ValueError('API rate needs an exact vendor:model identifier')
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError('API rate needs pricing provenance')
        for value in (self.input_per_mtok, self.output_per_mtok):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError('API rates must be nonnegative and finite')


#: Tokens one agentic lead turn costs once its context has grown, measured
#: on Claude leads in Stage B series rule-b1ff751 (2026-10-06): 2,092,420
#: reported input over 36 turns on f1 and 1,510,139 over 27 on f3, because
#: the CLI re-sends the whole conversation every turn and reports cached
#: input at full weight. A seed, like every other number here.
#: What a one-turn summary call reserves: a measured allowance, not a hard
#: bound. The close-out's prompt is bounded at 32,000 bytes
#: (runtime.Fleet._closeout) and, on the CLIs whose argv carries
#: ``--max-turns 1`` (claude, grok; CLISpec.summary_turn_capped), the call
#: is one model turn. Nothing caps its output tokens at the CLI: the view's
#: ``max_tokens`` is a Python attribute the argv does not emit, and saved
#: close-outs reported 1,465 and 1,629 output tokens. The largest close-out
#: measured across three series totalled 24,006 reported tokens (series
#: rule-3f9c548 f3: a 394k lead finished its task and the 500k reserve
#: refused the close-out that would have closed it). 64k is about 2.7 times
#: that largest measurement; the operator's reserve applies when it is
#: smaller, and a CLI without an enforced turn cap (codex) reserves the
#: operator's figure in full (Codex review of 961d2da on #53).
SUMMARY_CALL_RESERVE_TOKENS = 64_000

TURN_CONTEXT_TOKENS = 60_000
#: The share of a per-call ceiling a derived turn cap plans to use; the
#: rest is headroom for the turns that read more than the average.
TURN_CAP_MARGIN = 0.8
#: Fewer rounds than this and a lead cannot read, edit and check at all
#: (Run 14: leads capped at 14 spent every round reading).
MIN_LEAD_TURNS = 8


def lead_turns_for(max_tokens_per_call) -> int | None:
    """A lead turn cap derived from a per-call token ceiling, or None.

    Both Stage B series on b1ff751 ended Claude-led cells on the post-return
    ceiling: with no turn cap the lead ran 27 to 36 turns, each re-sending
    its context, and the one call reported 1.5M to 2.1M tokens. The ceiling
    fired correctly and could only fire after the fact. A turn cap is the
    control that acts before the spend, and the lead is told its round
    budget so it plans against it (``Session._round_budget``). An operator
    who sets ``Settings.lead_max_turns`` keeps that; this applies only when
    none was set and a ceiling was.
    """
    if not max_tokens_per_call:
        return None
    return max(MIN_LEAD_TURNS, int(max_tokens_per_call * TURN_CAP_MARGIN // TURN_CONTEXT_TOKENS))


@dataclass(frozen=True)
class RunLimits:
    max_calls: int = 24
    max_reported_tokens: int = 500_000
    wall_seconds: float = 900
    max_concurrent_workers: int = 2
    max_cost_usd: float | None = None
    api_cost_rates: tuple[APICostRate, ...] = ()
    #: Pre-call gate (Stage B, 2026-10-03: single agentic calls ran 0.5M to
    #: 3.4M tokens and ten of ten cells overshot the post-return threshold by
    #: up to 1.7M in one call). No attempt starts unless the remaining token
    #: budget covers this many tokens; 0 keeps the old post-return-only rule.
    reserve_tokens_per_call: int = 0
    #: Post-return ceiling naming an oversized call: a single attempt that
    #: reports more than this stops the run with ``call_token_ceiling``.
    #: None is no ceiling. It cannot cut a call short; it says which one.
    max_tokens_per_call: int | None = None

    def __post_init__(self):
        if self.max_cost_usd is not None and (
                isinstance(self.max_cost_usd, bool) or not isinstance(self.max_cost_usd, (int, float))
                or not math.isfinite(self.max_cost_usd) or self.max_cost_usd <= 0):
            raise ValueError('max_cost_usd must be positive and finite')
        object.__setattr__(self, 'api_cost_rates', tuple(self.api_cost_rates))
        if any(not isinstance(r, APICostRate) for r in self.api_cost_rates):
            raise ValueError('api_cost_rates must contain APICostRate records')
        if len({r.model for r in self.api_cost_rates}) != len(self.api_cost_rates):
            raise ValueError('Duplicate API cost rate')
        for name in ('max_calls', 'max_reported_tokens', 'max_concurrent_workers'):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if type(self.reserve_tokens_per_call) is not int or self.reserve_tokens_per_call < 0:
            raise ValueError('reserve_tokens_per_call must be a non-negative integer')
        if self.reserve_tokens_per_call > self.max_reported_tokens:
            raise ValueError('reserve_tokens_per_call cannot exceed max_reported_tokens')
        if self.max_tokens_per_call is not None and (
                type(self.max_tokens_per_call) is not int or self.max_tokens_per_call < 1):
            raise ValueError('max_tokens_per_call must be a positive integer or None')
        if (isinstance(self.wall_seconds, bool)
                or not isinstance(self.wall_seconds, (int, float))
                or not math.isfinite(self.wall_seconds) or self.wall_seconds <= 0):
            raise ValueError('wall_seconds must be positive and finite')


def effective_lead_turns(operator_turns, limits):
    """The lead turn cap a run will use and where it comes from: the
    operator's ``Settings.lead_max_turns`` when set, else one derived from
    the per-call threshold when the run has limits, else none. The same rule
    ``project_run`` applies, so what is shown and saved is what runs."""
    if operator_turns is not None:
        return operator_turns, 'operator setting'
    derived = lead_turns_for(getattr(limits, 'max_tokens_per_call', None)) if limits is not None else None
    if derived:
        return derived, f'derived from the {limits.max_tokens_per_call:,}-token per-call threshold'
    return None, None


def describe_limits(limits, survey=None, max_tasks=None, lead_turns=None, lead_source=None) -> str:
    """The selected allowance in plain words, one wording for the GUI form,
    the progress stream and the saved record (Codex GUI plan on #35,
    2026-10-09). Token limits are counted from what each CLI reports after a
    call returns, so the text never calls them a hard ceiling or a cost."""
    parts = []
    if max_tasks is not None:
        parts.append(f'Task limit: {int(max_tasks)}.')
    if limits is None:
        parts.append('No shared run allowance: model calls, reported tokens and time are not capped '
                     'for this run.')
    else:
        parts.append(f'At most {limits.max_calls:,} model calls and {limits.max_reported_tokens:,} reported '
                     f'tokens in total; {limits.max_concurrent_workers} parallel worker(s).')
        parts.append(f'Time limit {limits.wall_seconds:,g} seconds: no call starts after it and each call '
                     'gets only the time left, but checks and captures between calls are not cut off.')
        if limits.reserve_tokens_per_call:
            reserve = limits.reserve_tokens_per_call
            parts.append(f'An ordinary call starts only while at least {reserve:,} tokens of the total '
                         f'remain; a one-turn summary call on a CLI whose flags hold it to one turn '
                         f'(Claude, Grok) needs {min(reserve, SUMMARY_CALL_RESERVE_TOKENS):,}.')
        if limits.max_tokens_per_call:
            parts.append(f'A single call that reports more than {limits.max_tokens_per_call:,} tokens '
                         'stops the run after it returns; it cannot cut that call short.')
        parts.append('Token counts are what the CLIs report after each call, not a hard ceiling '
                     'and not an invoice.')
    if lead_turns:
        parts.append(f'Lead turn cap: {lead_turns} rounds per lead or reviewer call ({lead_source}); '
                     'Codex seats have no turn flag and are not held to it.')
    if survey is not None:
        parts.append(f'Recovery: up to {survey.recovery_tasks} extra continuation or repair task(s) '
                     'after failures.')
    return ' '.join(parts)


class RunBudget:
    def __init__(self, limits: RunLimits, *, path=None, clock=time.monotonic, carried=None):
        """``carried`` is what earlier episodes of the same job spent
        (quadratus.checkpoint.consumed): the allowance is the job's, so a
        resumed episode starts from it and never from zero (step 2)."""
        self.limits = limits
        self.path = Path(path) if path else None
        self._clock = clock
        carried = dict(carried or {})
        self._carried = carried
        self._started = clock() - float(carried.get('elapsed_seconds') or 0.0)
        self._lock = threading.Lock()
        #: Signalled whenever a call settles or a held stage is released: a
        #: call deferred behind in-flight commitments re-evaluates then.
        self._settled = threading.Condition(self._lock)
        self._calls = int(carried.get('calls') or 0)
        self._active = {}
        self._rates = {r.model: r for r in limits.api_cost_rates}
        self._api_cost = Decimal(str(carried.get('api_cost_usd') or 0))
        self._unknown_cost = 0
        self._input = int(carried.get('input_tokens') or 0)
        self._output = int(carried.get('output_tokens') or 0)
        self._unknown = 0
        self._reason = ''
        self._responses = []
        self._oversized = []
        self._shaped = 0
        #: Finishing stages still held from ordinary work, stages already
        #: started, and every ordinary call refused to keep them.
        self._finish_pending = []
        self._finish_started = []
        self._finish_released = []
        self._finish_refusals = []
        self._finish_deferrals = []
        if self.path:
            # Written before any call, so an episode that ends before its
            # first call still leaves the job's record (and what it carried).
            try:
                self._persist()
            except RunBudgetExceeded:
                pass   # _reason is latched: the first reserve stops the run

    def _snapshot(self):
        return {
            'limits': asdict(self.limits), 'reserved_attempts': self._calls,
            'api_cost_usd': float(self._api_cost),
            'api_cost_overshoot_usd': float(max(Decimal(0), self._api_cost - Decimal(str(self.limits.max_cost_usd or 0)))),
            'unknown_api_cost_attempts': self._unknown_cost,
            'cost_boundary': 'configured API upper-rate threshold; in-flight calls can overshoot; CLI excluded',
            'in_flight': len(self._active), 'input_tokens': self._input,
            'output_tokens': self._output,
            'reported_tokens': self._input + self._output,
            'unknown_usage_attempts': self._unknown, 'stop_reason': self._reason,
            'preserved_responses': list(self._responses),
            'elapsed_seconds': max(0, self._clock() - self._started),
            'token_boundary': 'post-return threshold; in-flight calls can overshoot',
            'reserve_boundary': ('no attempt starts unless the remaining token budget covers '
                                 'reserve_tokens_per_call; a call that reports more than '
                                 'max_tokens_per_call stops the run after it returns'),
            'oversized_calls': list(self._oversized),
            'shaped_reservations': self._shaped,
            # Included in every total above: spent by earlier episodes.
            'carried_from_earlier_episodes': dict(self._carried),
            'shape_boundary': ('a summary-only call whose CLI argv enforces one model turn reserves '
                               'min(reserve_tokens_per_call, SUMMARY_CALL_RESERVE_TOKENS), a measured '
                               'allowance (largest saved close-out 24,006 reported tokens), not a hard '
                               'bound: output tokens are not capped at the CLI; every other call, '
                               'including a summary call on a CLI with no turn flag, reserves '
                               'reserve_tokens_per_call in full'),
            'input_boundary': 'normalized provider input includes cached input; do not add it again',
            'finish_protection': {
                'pending': list(self._finish_pending), 'started': list(self._finish_started),
                'released': list(self._finish_released), 'refusals': list(self._finish_refusals),
                'deferrals': [dict(d) for d in self._finish_deferrals],
                'editing_exposure': self._editing_exposure(),
                'boundary': ('an admission policy, not a guarantee about reported usage: while a finishing '
                             'stage is pending, an ordinary call starts only if the attempts after it and '
                             'the reported tokens left after its own exposure and the exposure of every '
                             'call in flight still fit one full per-call reserve per pending stage. A call '
                             'holding the write grant is exposed at the post-return per-call ceiling '
                             '(max_tokens_per_call), the most it can report without stopping the run; '
                             'any other call at its reserve, an estimate. With no ceiling an editing '
                             'call is exposed at its reserve and the hold does not bound it. A call that '
                             'only calls in flight keep out waits, unadmitted and without a ticket, until '
                             'one settles, then is evaluated again; a call that cannot fit even with '
                             'nothing in flight is refused, recorded, and latches nothing'),
            },
            'wall_boundary': 'attempt timeout plus required external process supervisor',
        }

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def persist(self):
        """Write the budget record now and return the snapshot written: the
        quiescent checkpoint and an interrupted run's record then carry the
        same spend, time included (root review of aaa06a6: a record written
        only at reserve and finish under-counted the time between calls)."""
        with self._lock:
            snapshot = self._snapshot()
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(snapshot, indent=2) + '\n')
                    temporary.replace(self.path)
                except OSError as exc:
                    raise RunBudgetExceeded('Run stopped: budget_state_unavailable') from exc
            return snapshot

    def _persist(self):
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(self._snapshot(), indent=2) + '\n')
                temporary.replace(self.path)
            except OSError as exc:
                self._reason = self._reason or 'budget_state_unavailable'
                raise RunBudgetExceeded('Run stopped: budget_state_unavailable') from exc

    def _remaining(self):
        return self.limits.wall_seconds - (self._clock() - self._started)

    def _check(self):
        if self._remaining() <= 0:
            self._reason = self._reason or 'wall_deadline'
        if self._reason:
            self._persist()
            raise RunBudgetExceeded(f'Run stopped: {self._reason}')

    def protect_finish(self, stages):
        """Hold ``stages`` (a subset of ``FINISH_STAGES``) from ordinary work.

        Idempotent and never additive: the held set is replaced, so a resumed
        episode that names the same pending stages holds them once, and a
        stage this budget saw start is never held again. Holding grants
        nothing: the finishing calls still meet every limit."""
        with self._lock:
            wanted = [s for s in FINISH_STAGES if s in set(stages or ())]
            self._finish_pending = [s for s in wanted if s not in self._finish_started]
            self._persist()

    def release_finish(self, stage, why):
        """Stop holding ``stage``: the run will not ask it (recorded)."""
        with self._lock:
            if stage in self._finish_pending:
                self._finish_pending.remove(stage)
                self._finish_released.append(dict(stage=stage, why=str(why)[:300]))
                self._settled.notify_all()
                self._persist()

    def finish_pending(self):
        with self._lock:
            return list(self._finish_pending)

    def _editing_exposure(self):
        """What the hold counts for a call holding the write grant: the
        post-return per-call ceiling, never below the reserve; None when no
        ceiling is set (then the reserve, an estimate, and said so)."""
        ceiling = self.limits.max_tokens_per_call
        if ceiling is None:
            return None
        return max(ceiling, self.limits.reserve_tokens_per_call)

    def _held(self, stage):
        """The pending stages a call of ``stage`` must leave room for: all of
        them for ordinary work, only the later ones for a finishing call."""
        if stage is None:
            return list(self._finish_pending)
        later = FINISH_STAGES[FINISH_STAGES.index(stage) + 1:]
        return [s for s in self._finish_pending if s in later]

    def reserve(self, *, transport="cli", price_key="", expected_tokens=None):
        """Atomically authorize one attempt and return its ID and time remaining.

        ``expected_tokens`` names a measured allowance for a call the transport
        holds to one model turn (a summary-only close-out on a CLI whose argv
        enforces the cap); the reserve asked of the remaining budget is then
        the smaller of the operator's reserve and that allowance. It is not a
        hard size bound. It never raises the reserve, and the post-return
        threshold is unchanged.
        """
        shape = admission.get() or {}
        deferral = None
        with self._lock:
            while True:
                try:
                    ticket = self._admit(transport, price_key, expected_tokens, shape, deferral)
                except _Deferred as wait:
                    if deferral is None:
                        deferral = dict(attempt=self._calls + 1, editing=bool(shape.get('editing')),
                                        behind=wait.behind, waited_seconds=0.0, outcome='waiting')
                        self._finish_deferrals.append(deferral)
                        self._persist()
                    started = self._clock()
                    # Unadmitted: no ticket, no attempt, nothing sent. The
                    # wall still runs and is never reset by waiting.
                    self._settled.wait(timeout=max(self._remaining(), 0.0))
                    deferral['waited_seconds'] = round(deferral['waited_seconds'] + self._clock() - started, 3)
                    continue
                except RunBudgetExceeded as exc:
                    if deferral is not None:
                        deferral['outcome'] = ('refused' if isinstance(exc, FinishReserved)
                                               else f'stopped: {self._reason or exc}')
                        try:
                            self._persist()
                        except RunBudgetExceeded:
                            pass
                    raise
                if deferral is not None:
                    deferral['outcome'] = 'admitted'
                    deferral['admitted_as'] = ticket
                    try:
                        self._persist()
                    except RunBudgetExceeded:
                        pass   # latched; the admitted call settles through finish()
                return ticket, self._remaining()

    def _admit(self, transport, price_key, expected_tokens, shape, deferral):
        """One admission decision, under the lock: a ticket, or a raise.
        ``_Deferred`` means only calls in flight stand in the way."""
        self._check()
        if transport not in ('api', 'cli'):
            raise ValueError('Unknown budget transport')
        if self.limits.max_cost_usd is not None and transport == 'api' and price_key not in self._rates:
            self._reason = 'api_price_unavailable'
            self._check()
        if self._calls >= self.limits.max_calls:
            self._reason = 'call_limit'
            self._check()
        stage = finish_stage.get()
        if stage is not None and stage not in FINISH_STAGES:
            raise ValueError('Unknown finishing stage')
        full = self.limits.reserve_tokens_per_call
        reserve = full
        shaped = bool(reserve and expected_tokens and 0 < expected_tokens < reserve and stage is None)
        if shaped:
            # A finishing judgment never takes the summary shape.
            reserve = int(expected_tokens)
        left = self.limits.max_reported_tokens - (self._input + self._output)
        if reserve and left < reserve:
            # Refused before the call: the budget left could not hold a
            # call of the size the operator said to expect.
            self._reason = 'reported_token_reserve'
            self._check()
        held = self._held(stage)
        exposure = reserve
        if shape.get('editing') and stage is None:
            exposure = max(reserve, self._editing_exposure() or 0)
        if held:
            in_flight = sum(a[3] for a in self._active.values())
            calls_after = self.limits.max_calls - (self._calls + 1)
            tokens_after = left - exposure - in_flight
            if calls_after < len(held) or (full and tokens_after < len(held) * full):
                if (shape.get('may_wait') and self._active and calls_after >= len(held)
                        and left - exposure >= len(held) * full):
                    # Only commitments in flight keep it out, and they
                    # settle: wait for one, unadmitted, then decide again.
                    raise _Deferred(sorted(self._active))
                # Ordinary work may not spend what the finish needs. Not
                # latched: the finishing calls are still admissible.
                self._finish_refusals.append(dict(
                    attempt=self._calls + 1, stage=stage, held=list(held),
                    calls_after=calls_after, tokens_after=tokens_after,
                    held_tokens=len(held) * full, in_flight=len(self._active),
                    exposure=exposure, in_flight_exposure=in_flight,
                    deferred=deferral is not None))
                self._persist()
                raise FinishReserved(
                    f"Ordinary call refused to keep the finish: {', '.join(held)} "
                    f"need{'s' if len(held) == 1 else ''} {len(held)} call(s)"
                    + (f" and {len(held) * full:,} reported tokens" if full else "")
                    + f"; {calls_after} call(s) and {max(tokens_after, 0):,} tokens would remain")
        if stage is not None and stage in self._finish_pending:
            self._finish_pending.remove(stage)
            self._finish_started.append(stage)
        self._shaped += int(shaped)
        self._calls += 1
        ticket = self._calls
        self._active[ticket] = (transport, price_key, reserve, exposure)
        try:
            self._persist()
        except RunBudgetExceeded:
            self._active.pop(ticket)
            self._calls -= 1
            self._shaped -= int(shaped)
            raise
        return ticket

    def finish(self, ticket, usage, *, native_children=(), reply='', price_key=None):
        """Account once, latch terminal conditions and reject further calls."""
        with self._lock:
            if ticket not in self._active:
                raise RuntimeError('Unknown or already-finished budget reservation')
            transport, reserved_key, _, _ = self._active.pop(ticket)
            self._settled.notify_all()
            usage = usage if isinstance(usage, dict) else {}
            counts = [usage.get('input_tokens'), usage.get('output_tokens')]
            if any(type(n) is not int or n < 0 for n in counts):
                self._unknown += 1
                self._reason = self._reason or 'unknown_usage'
            else:
                # Provider normalization already folds in cache where needed.
                self._input += counts[0]
                self._output += counts[1]
                ceiling = self.limits.max_tokens_per_call
                if ceiling is not None and counts[0] + counts[1] > ceiling:
                    self._oversized.append(dict(attempt=ticket, reported_tokens=counts[0] + counts[1],
                                                ceiling=ceiling))
                    self._reason = self._reason or 'call_token_ceiling'
                if self._input + self._output >= self.limits.max_reported_tokens:
                    self._reason = self._reason or 'reported_token_threshold'
            if self.limits.max_cost_usd is not None and transport == 'api':
                rate = self._rates.get(price_key or reserved_key)
                if rate is None or any(type(n) is not int or n < 0 for n in counts):
                    self._unknown_cost += 1
                    self._reason = self._reason or 'unknown_api_cost'
                else:
                    self._api_cost += (Decimal(counts[0]) * Decimal(str(rate.input_per_mtok))
                                       + Decimal(counts[1]) * Decimal(str(rate.output_per_mtok))) / 1_000_000
                    if self._api_cost >= Decimal(str(self.limits.max_cost_usd)):
                        self._reason = self._reason or 'api_cost_threshold'
            if native_children:
                # Their spend/control is outside these reservations. Do not
                # silently count this run as bounded or guess counter overlap.
                self._reason = self._reason or 'uncontrolled_native_delegation'
            if self._remaining() <= 0:
                self._reason = self._reason or 'wall_deadline'
            if self._reason and reply and self.path:
                folder = self.path.parent / 'budget-responses'
                try:
                    folder.mkdir(exist_ok=True)
                    target = folder / f'attempt-{ticket}.txt'
                    target.write_text(reply)
                    target.chmod(0o600)
                    self._responses.append(str(target.relative_to(self.path.parent)))
                except OSError as exc:
                    raise RunBudgetExceeded('Run stopped: response_storage_unavailable') from exc
            self._persist()
            self._check()
