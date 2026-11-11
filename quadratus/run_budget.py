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
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .safe_io import make_private_dir, write_private


class RunBudgetExceeded(RuntimeError):
    """Terminal run control, deliberately not a recoverable ProviderError."""


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


#: The run-limit fields a form or a command line carries, in form order.
LIMIT_FIELDS = ('max_calls', 'max_reported_tokens', 'wall_seconds', 'max_concurrent_workers',
                'reserve_tokens_per_call', 'max_tokens_per_call', 'recovery_tasks')

#: What each field is called when a value is refused. These are the Project
#: form's labels; the command line passes its flag names instead.
LIMIT_LABELS = {
    'max_tasks': 'Task limit',
    'max_calls': 'Maximum model calls',
    'max_reported_tokens': 'Total reported tokens',
    'wall_seconds': 'Time limit (seconds)',
    'max_concurrent_workers': 'Parallel workers',
    'reserve_tokens_per_call': 'Tokens that must remain before a call starts',
    'max_tokens_per_call': 'Stop after a single call reports more than (tokens, 0 for none)',
    'recovery_tasks': 'Extra recovery tasks after failures (0 for none)',
}


def whole_number(value, label, minimum):
    """``value`` as a whole number of at least ``minimum``, or ValueError
    naming ``label``. Accepts what a form or a command line hands over: an
    int, a whole float or a numeric string; never a bool, a blank or a
    fraction."""
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f'{label}: enter a whole number.')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label}: {value!r} is not a number.') from None
    if not math.isfinite(number) or number != int(number) or number < minimum:
        raise ValueError(f'{label} must be a whole number of at least {minimum}.')
    return int(number)


def limits_from_fields(enabled, fields, labels=LIMIT_LABELS):
    """The validated RunLimits and SurveyConfig that ``fields`` describe.

    One rule for the Project form and the command line, so a run is bounded
    the same way from either. ``fields`` maps LIMIT_FIELDS names to raw
    values and ``labels`` says what to call each when it is refused. Returns
    ``(None, None)`` when limits are off and no recovery task is asked for,
    which is the run made before these options existed. Raises ValueError
    naming the field otherwise, before any project, provider or model is
    touched.
    """
    from .session import SurveyConfig
    recovery = whole_number(fields.get('recovery_tasks', 0) or 0, labels['recovery_tasks'], 0)
    survey = SurveyConfig(recovery_tasks=recovery) if recovery else None
    if not enabled:
        return None, survey
    per_call = whole_number(fields.get('max_tokens_per_call', 0) or 0, labels['max_tokens_per_call'], 0)
    limits = RunLimits(
        max_calls=whole_number(fields.get('max_calls'), labels['max_calls'], 1),
        max_reported_tokens=whole_number(fields.get('max_reported_tokens'), labels['max_reported_tokens'], 1),
        wall_seconds=whole_number(fields.get('wall_seconds'), labels['wall_seconds'], 1),
        max_concurrent_workers=whole_number(fields.get('max_concurrent_workers'),
                                            labels['max_concurrent_workers'], 1),
        reserve_tokens_per_call=whole_number(fields.get('reserve_tokens_per_call', 0) or 0,
                                             labels['reserve_tokens_per_call'], 0),
        max_tokens_per_call=per_call or None,
    )
    return limits, survey


#: The source ``effective_lead_turns`` reports for the operator's own cap.
OPERATOR_LEAD_SOURCE = 'operator setting'


def effective_lead_turns(operator_turns, limits):
    """The lead turn cap a run will use and where it comes from: the
    operator's ``Settings.lead_max_turns`` when set, else one derived from
    the per-call threshold when the run has limits, else none. The same rule
    ``project_run`` applies, so what is shown and saved is what runs."""
    if operator_turns is not None:
        return operator_turns, OPERATOR_LEAD_SOURCE
    derived = lead_turns_for(getattr(limits, 'max_tokens_per_call', None)) if limits is not None else None
    if derived:
        return derived, f'derived from the {limits.max_tokens_per_call:,}-token per-call threshold'
    return None, None


def lead_cap_overrun(lead_turns, limits):
    """The tokens ``lead_turns`` rounds plan for one call when that is more
    than the per-call threshold, else None.

    A derived cap never goes below MIN_LEAD_TURNS, so under a threshold
    smaller than those rounds cost (the "Small" shape with a 150k threshold
    plans about 480k) the cap cannot honour the threshold it was derived
    from. The arithmetic is left alone; this only lets the text say so."""
    ceiling = getattr(limits, 'max_tokens_per_call', None) if limits is not None else None
    if not lead_turns or not ceiling:
        return None
    planned = int(lead_turns) * TURN_CONTEXT_TOKENS
    return planned if planned > ceiling else None


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
                         f'(Claude, Grok) needs {min(reserve, SUMMARY_CALL_RESERVE_TOKENS):,}. '
                         'While a call is running, the amount it needed to start stays set aside, so '
                         'a second call at the same time needs its own amount on top.')
        if limits.max_tokens_per_call:
            parts.append(f'A single call that reports more than {limits.max_tokens_per_call:,} tokens '
                         'stops the run after it returns; it cannot cut that call short.')
        parts.append('Token counts are what the CLIs report after each call, not a hard ceiling '
                     'and not an invoice.')
    if lead_turns:
        parts.append(f'Lead turn cap: {lead_turns} rounds per lead or reviewer call ({lead_source}); '
                     'Codex seats have no turn flag and are not held to it.')
        overrun = lead_cap_overrun(lead_turns, limits)
        if overrun:
            parts.append(
                f'At about {TURN_CONTEXT_TOKENS:,} tokens a round, {lead_turns} rounds plan about '
                f'{overrun:,} tokens for one call, more than the {limits.max_tokens_per_call:,}-token '
                'per-call threshold: this cap does not keep a call under that threshold, and the '
                'threshold still stops the run only after such a call returns.'
                + (f' A derived cap is never below {MIN_LEAD_TURNS} rounds, which fit only in a '
                   f'threshold of at least {MIN_LEAD_TURNS * TURN_CONTEXT_TOKENS:,} tokens.'
                   if lead_source != OPERATOR_LEAD_SOURCE else ''))
    if survey is not None:
        parts.append(f'Recovery: up to {survey.recovery_tasks} extra continuation or repair task(s) '
                     'after failures.')
    return ' '.join(parts)


class RunBudget:
    def __init__(self, limits: RunLimits, *, path=None, clock=time.monotonic):
        self.limits = limits
        self.path = Path(path) if path else None
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._calls = 0
        self._active = {}
        self._rates = {r.model: r for r in limits.api_cost_rates}
        self._api_cost = Decimal(0)
        self._unknown_cost = 0
        self._input = self._output = self._unknown = 0
        self._reason = ''
        self._responses = []
        self._oversized = []
        self._shaped = 0
        self._price_fallbacks = {}

    def _held(self):
        """Tokens set aside for calls still running: the reserve each was
        admitted with. They have reported nothing yet, so this much of the
        remaining budget is already spoken for."""
        return sum(reserve for _, _, reserve in self._active.values())

    def _snapshot(self):
        return {
            'limits': asdict(self.limits), 'reserved_attempts': self._calls,
            'api_cost_usd': float(self._api_cost),
            'api_cost_overshoot_usd': float(max(Decimal(0), self._api_cost - Decimal(str(self.limits.max_cost_usd or 0)))),
            'unknown_api_cost_attempts': self._unknown_cost,
            'cost_boundary': 'configured API upper-rate threshold; in-flight calls can overshoot; CLI excluded',
            'in_flight': len(self._active), 'in_flight_reserved_tokens': self._held(),
            'api_price_fallbacks': [dict(reported_model=reported, priced_as=priced_as, attempts=count)
                                    for (reported, priced_as), count in self._price_fallbacks.items()],
            'input_tokens': self._input,
            'output_tokens': self._output,
            'reported_tokens': self._input + self._output,
            'unknown_usage_attempts': self._unknown, 'stop_reason': self._reason,
            'preserved_responses': list(self._responses),
            'elapsed_seconds': max(0, self._clock() - self._started),
            'token_boundary': 'post-return threshold; in-flight calls can overshoot',
            'reserve_boundary': ('no attempt starts unless the remaining token budget, less the '
                                 'reserve each in-flight call was admitted with, covers '
                                 'reserve_tokens_per_call; a call that reports more than '
                                 'max_tokens_per_call stops the run after it returns'),
            'oversized_calls': list(self._oversized),
            'shaped_reservations': self._shaped,
            'shape_boundary': ('a summary-only call whose CLI argv enforces one model turn reserves '
                               'min(reserve_tokens_per_call, SUMMARY_CALL_RESERVE_TOKENS), a measured '
                               'allowance (largest saved close-out 24,006 reported tokens), not a hard '
                               'bound: output tokens are not capped at the CLI; every other call, '
                               'including a summary call on a CLI with no turn flag, reserves '
                               'reserve_tokens_per_call in full'),
            'input_boundary': 'normalized provider input includes cached input; do not add it again',
            'wall_boundary': 'attempt timeout plus required external process supervisor',
        }

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def _persist(self):
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Owner-only from creation and written whole (safe_io): the
                # old temporary name was fixed and took the umask's mode.
                write_private(self.path, json.dumps(self._snapshot(), indent=2) + '\n')
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

    def reserve(self, *, transport="cli", price_key="", expected_tokens=None):
        """Atomically authorize one attempt and return its ID and time remaining.

        ``expected_tokens`` names a measured allowance for a call the transport
        holds to one model turn (a summary-only close-out on a CLI whose argv
        enforces the cap); the reserve asked of the remaining budget is then
        the smaller of the operator's reserve and that allowance. It is not a
        hard size bound. It never raises the reserve, and the post-return
        threshold is unchanged.

        The reserve is asked of what is left after the calls still running:
        each ticket holds the reserve it was admitted with until it finishes
        (three parallel attempts were each admitted on the same 500k of
        headroom against a 400k reserve before this).
        """
        with self._lock:
            self._check()
            if transport not in ('api', 'cli'):
                raise ValueError('Unknown budget transport')
            if self.limits.max_cost_usd is not None and transport == 'api' and price_key not in self._rates:
                self._reason = 'api_price_unavailable'
                self._check()
            if self._calls >= self.limits.max_calls:
                self._reason = 'call_limit'
                self._check()
            reserve = self.limits.reserve_tokens_per_call
            if reserve and expected_tokens and 0 < expected_tokens < reserve:
                reserve = int(expected_tokens)
                self._shaped += 1
            remaining = self.limits.max_reported_tokens - (self._input + self._output) - self._held()
            if reserve and remaining < reserve:
                # Refused before the call: the budget left, after what the
                # calls in flight were admitted on, could not hold a call of
                # the size the operator said to expect.
                self._reason = 'reported_token_reserve'
                self._check()
            self._calls += 1
            ticket = self._calls
            self._active[ticket] = (transport, price_key, reserve)
            try:
                self._persist()
            except RunBudgetExceeded:
                self._active.pop(ticket)
                self._calls -= 1
                raise
            return ticket, self._remaining()

    def finish(self, ticket, usage, *, native_children=(), reply='', price_key=None):
        """Account once, latch terminal conditions and reject further calls."""
        with self._lock:
            if ticket not in self._active:
                raise RuntimeError('Unknown or already-finished budget reservation')
            transport, reserved_key, _ = self._active.pop(ticket)
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
                reported = price_key or reserved_key
                rate = self._rates.get(reported)
                if rate is None and reserved_key and reserved_key != reported:
                    # The response named a model the operator gave no rate for
                    # (jev-latest answered as jev-1.13.0). The call was
                    # admitted under the reserved key's rate, so it is priced
                    # there and both names are kept. With no reserved key
                    # there is nothing to fall back to and the stop stands.
                    rate = self._rates.get(reserved_key)
                    if rate is not None:
                        pair = (reported, reserved_key)
                        self._price_fallbacks[pair] = self._price_fallbacks.get(pair, 0) + 1
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
                    make_private_dir(folder)
                    target = folder / f'attempt-{ticket}.txt'
                    write_private(target, reply)
                    self._responses.append(str(target.relative_to(self.path.parent)))
                except OSError as exc:
                    raise RunBudgetExceeded('Run stopped: response_storage_unavailable') from exc
            self._persist()
            self._check()
