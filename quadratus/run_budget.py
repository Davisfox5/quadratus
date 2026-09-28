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


@dataclass(frozen=True)
class RunLimits:
    max_calls: int = 24
    max_reported_tokens: int = 500_000
    wall_seconds: float = 900
    max_concurrent_workers: int = 2
    max_cost_usd: float | None = None
    api_cost_rates: tuple[APICostRate, ...] = ()

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
        if (isinstance(self.wall_seconds, bool)
                or not isinstance(self.wall_seconds, (int, float))
                or not math.isfinite(self.wall_seconds) or self.wall_seconds <= 0):
            raise ValueError('wall_seconds must be positive and finite')


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

    def reserve(self, *, transport="cli", price_key=""):
        """Atomically authorize one attempt and return its ID and time remaining."""
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
            self._calls += 1
            ticket = self._calls
            self._active[ticket] = (transport, price_key)
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
            transport, reserved_key = self._active.pop(ticket)
            usage = usage if isinstance(usage, dict) else {}
            counts = [usage.get('input_tokens'), usage.get('output_tokens')]
            if any(type(n) is not int or n < 0 for n in counts):
                self._unknown += 1
                self._reason = self._reason or 'unknown_usage'
            else:
                # Provider normalization already folds in cache where needed.
                self._input += counts[0]
                self._output += counts[1]
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
