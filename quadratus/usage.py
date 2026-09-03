"""Usage metering: what this run would have cost on API keys.

Subscription transport makes marginal cost invisible, which is convenient
right up until it isn't: invisible cost cannot be optimised, and a future
move to billed API keys (or an enterprise platform) would be priced blind.
The meter makes the counterfactual visible -- every invocation is recorded
with its estimated tokens and what those tokens would have cost at API list
prices -- without ever being load-bearing: metering failure must never fail
a run, so the meter observes and writes, and nothing reads it back on the
hot path.

Two grades of number, clearly separated:

* **Measured** -- when the CLI reports real token counts (the Claude CLI's
  JSON envelope carries a ``usage`` block), those are recorded as-is.
* **Estimated** -- otherwise tokens are estimated at ~4 characters per token,
  the long-standing English/code rule of thumb. Estimates are marked as
  estimates in the record; they are good enough to rank hot spots and size a
  migration, not good enough to bill against.

Prices are seeds, not truth, like everything else in the model tables: a
per-Mtok list compiled from vendor pricing pages at build time, kept in one
obvious place, and expected to be stale within months. The report says which
price sheet it used.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

__all__ = ["PRICES", "Price", "InvocationRecord", "UsageMeter", "CHARS_PER_TOKEN"]

#: The estimation rule of thumb. Real tokenisers vary by ±25% either side of
#: this on code; the ranking of hot spots survives that error comfortably.
CHARS_PER_TOKEN = 4.0


@dataclass(frozen=True)
class Price:
    """API list price per million tokens, in USD."""

    input_per_mtok: float
    output_per_mtok: float


#: Seed price sheet, per model key, compiled 2026-08. Stale within months by
#: design assumption -- correct it from the vendor pages, not from memory.
#: A missing model falls back to DEFAULT_PRICE so the meter keeps counting
#: tokens even when the dollar figure is a shrug.
PRICES: Dict[str, Price] = {
    "claude:fable": Price(10.0, 50.0),
    "claude:opus": Price(5.0, 25.0),
    "claude:sonnet": Price(2.0, 10.0),
    "claude:haiku": Price(1.0, 5.0),
    "openai:gpt-5.6-sol": Price(1.25, 10.0),
    "openai:gpt-5.6-terra": Price(0.6, 5.0),
    "openai:gpt-5.6-luna": Price(0.25, 2.0),
    "gemini:gemini-3.1-pro": Price(2.0, 12.0),
    "gemini:gemini-3.6-flash": Price(0.3, 2.5),
    "gemini:gemini-3.6-thinking": Price(2.0, 12.0),
    "grok:grok-4.6": Price(3.0, 15.0),
    "grok:grok-4-1-fast": Price(0.2, 0.5),
    "grok:grok-4.3": Price(3.0, 15.0),
    "grok:grok-4.20": Price(5.0, 25.0),
}

DEFAULT_PRICE = Price(3.0, 15.0)


@dataclass(frozen=True)
class InvocationRecord:
    """One model call, as the meter saw it."""

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    #: False when tokens were estimated from characters rather than reported
    #: by the CLI. Estimates rank hot spots; they do not bill.
    measured: bool


class UsageMeter:
    """Records every invocation; never sits on the critical path.

    Thread-safe because worker fan-out may invoke concurrently one day, and a
    meter is exactly the kind of code that would otherwise be the surprise.
    Optionally persists each record as a JSONL line so cost history survives
    the process -- the same append-only shape as everything else here.
    """

    def __init__(self, path=None) -> None:
        self.path = Path(path) if path else None
        self._records: List[InvocationRecord] = []
        self._lock = threading.Lock()

    # -- recording -----------------------------------------------------------
    def record(
        self,
        *,
        model: str,
        prompt: str = "",
        reply: str = "",
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> InvocationRecord:
        """Record one call. Real token counts win; characters estimate the rest."""
        measured = input_tokens is not None and output_tokens is not None
        if input_tokens is None:
            input_tokens = int(len(prompt or "") / CHARS_PER_TOKEN)
        if output_tokens is None:
            output_tokens = int(len(reply or "") / CHARS_PER_TOKEN)
        price = PRICES.get(model, DEFAULT_PRICE)
        cost = (
            input_tokens * price.input_per_mtok
            + output_tokens * price.output_per_mtok
        ) / 1_000_000
        entry = InvocationRecord(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            measured=measured,
        )
        with self._lock:
            self._records.append(entry)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(entry)) + "\n")
        return entry

    def wrap(self, invoke):
        """Wrap a ``(model, prompt, ...) -> reply`` callable with metering.

        The wrapped call is observationally identical to the original --
        including on failure, where nothing is recorded and the exception
        passes through untouched. A meter that can break a run has negative
        value.
        """

        def metered(model, prompt, *args, **kwargs):
            reply = invoke(model, prompt, *args, **kwargs)
            try:
                self.record(model=model, prompt=prompt, reply=str(reply or ""))
            except Exception:  # noqa: BLE001 -- metering must never fail a run
                pass
            return reply

        return metered

    # -- reporting -----------------------------------------------------------
    @property
    def records(self) -> List[InvocationRecord]:
        with self._lock:
            return list(self._records)

    def total_cost(self) -> float:
        return round(sum(r.cost_usd for r in self.records), 6)

    def by_model(self) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for r in self.records:
            agg = out.setdefault(
                r.model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            agg["calls"] += 1
            agg["input_tokens"] += r.input_tokens
            agg["output_tokens"] += r.output_tokens
            agg["cost_usd"] = round(agg["cost_usd"] + r.cost_usd, 6)
        return out

    def render_report(self) -> str:
        """The counterfactual bill, human-readable."""
        rows = self.by_model()
        if not rows:
            return "No usage recorded."
        estimated = sum(1 for r in self.records if not r.measured)
        lines = ["# Usage report (API-price counterfactual)", ""]
        for model in sorted(rows, key=lambda m: -rows[m]["cost_usd"]):
            agg = rows[model]
            lines.append(
                f"- {model}: {agg['calls']} calls, "
                f"{agg['input_tokens']:,} in / {agg['output_tokens']:,} out tokens, "
                f"${agg['cost_usd']:.4f}"
            )
        lines.append("")
        lines.append(f"**Total: ${self.total_cost():.4f}**")
        if estimated:
            lines.append(
                f"_{estimated} of {len(self.records)} calls token-estimated at "
                f"~{CHARS_PER_TOKEN:g} chars/token; treat totals as sizing, "
                f"not billing._"
            )
        lines.append("_Prices are the seed sheet in usage.py; verify before deciding._")
        return "\n".join(lines)
