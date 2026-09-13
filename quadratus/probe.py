"""Ask the installed CLIs what they actually accept, and remember the answer.

Every model table in this repo is a seed. The aliases were written from vendor
documentation, most of it second-hand, and the one thing that settles whether
``codex --model gpt-6-astra`` is a real thing is running it. This module does
that: for each model it walks the alias chain newest-first, sends the smallest
prompt that proves a round trip, and records the first alias the binary
accepts into the cache :mod:`quadratus.latest` reads.

That is also how "always the latest release" stays true without a hardcoded
version ladder. Where a CLI resolves a bare line name -- ``claude --model
opus`` -- the seed already floats and the probe merely confirms it. Where a
vendor requires a versioned alias, the probe is what discovers which
versioned alias exists today, and re-running it after a release is what moves
the seat forward. A ladder compiled into this file would be one more table
going stale in exactly the place the design says must not.

Two deliberate limits:

* **Probing spends subscription budget**, so the default set is the seats a
  run cannot start without -- the orchestrator chain and the brain trust --
  and the whole roster is opt-in.
* **What a model says it is, is not evidence.** Models misname their own
  version routinely. The reported string is recorded for the operator to
  read and is never branched on; the fact the probe trusts is that the
  invocation succeeded at all.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .config import Settings
from .latest import alias_env_var, record_resolution
from .registry import MODE_ROSTERS, ORCHESTRATOR_CHAIN, ROSTER, VENDORS, resolve
from .runtime import Fleet, _looks_exhausted

__all__ = [
    "BinaryStatus",
    "ProbeResult",
    "default_probe_set",
    "probe_binaries",
    "probe_models",
    "render_report",
]

#: Small enough to cost almost nothing, specific enough that a wrong-model
#: answer is visible in the transcript.
PROBE_PROMPT = (
    "Reply with only the exact model name and version you are running as. "
    "No explanation, no punctuation beyond the name itself."
)


@dataclass(frozen=True)
class BinaryStatus:
    vendor: str
    binary: str
    path: Optional[str]
    backend: str

    @property
    def ok(self) -> bool:
        return self.backend != "cli" or self.path is not None


@dataclass(frozen=True)
class ProbeResult:
    key: str
    #: The alias that worked, or the last one tried when none did.
    alias: str
    ok: bool
    #: What the model claimed to be. Untrusted -- recorded, never branched on.
    reported: str = ""
    detail: str = ""
    seconds: float = 0.0
    #: Aliases tried before this one, all of which the CLI rejected.
    rejected: Sequence[str] = ()


def default_probe_set() -> List[str]:
    """The seats a run cannot start without: the chain, then the brain trust.

    Probing the whole roster costs a call per model against windows the run
    then has to share. These are the ones whose failure stops a run rather
    than degrading it.
    """
    keys: List[str] = list(ORCHESTRATOR_CHAIN)
    for peer in MODE_ROSTERS["adversarial"]["peers"]:
        if peer not in keys:
            keys.append(peer)
    return keys


def probe_binaries(settings: Optional[Settings] = None) -> List[BinaryStatus]:
    """Which vendor CLIs are installed. Cheap, and runs before anything else.

    A missing binary explains every model failure for that vendor at once, so
    reporting it separately saves the operator reading one identical error
    per model behind it.
    """
    from .cli_providers import CLI_SPECS

    conf = settings or Settings.from_env()
    out = []
    for vendor in VENDORS:
        spec = CLI_SPECS.get(vendor)
        binary = spec.resolved_binary() if spec else ""
        out.append(
            BinaryStatus(
                vendor=vendor,
                binary=binary,
                path=shutil.which(binary) if binary else None,
                backend=conf.backend_for(vendor),
            )
        )
    return out


def probe_models(
    keys: Optional[Sequence[str]] = None,
    *,
    settings: Optional[Settings] = None,
    fleet: Optional[Fleet] = None,
    write_cache: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[ProbeResult]:
    """Try each model's aliases until one answers; cache the winner.

    Only floating rows walk a chain of candidates -- a pinned row names one
    release on purpose, and silently succeeding on a different one would make
    the table a lie. A pinned row is therefore tried once, and a failure is
    reported rather than worked around.
    """
    conf = settings or Settings.from_env()
    owned = fleet is None
    active = fleet or Fleet(conf)
    results: List[ProbeResult] = []
    try:
        for key in keys if keys is not None else default_probe_set():
            spec = resolve(key)
            if spec is None:
                results.append(ProbeResult(key, "", False, detail="not in the roster"))
                continue
            override = os.getenv(alias_env_var(key), "").strip()
            if override:
                candidates = [override]
            elif spec.vendor_default:
                # Nothing to walk: the whole point is to name no model. The
                # probe still makes the call, because "does the CLI answer at
                # all, and what does it say it is" is the question that
                # matters for this row.
                candidates = [""]
            elif spec.floating:
                candidates = list(spec.alias_chain)
            else:
                candidates = [spec.alias]
            rejected: List[str] = []
            for alias in candidates:
                if on_progress:
                    on_progress(f"{key} -> {alias}")
                started = time.monotonic()
                try:
                    reply = active.provider_for(key).for_model(alias).generate(
                        PROBE_PROMPT, system="Answer in as few words as possible."
                    )
                except Exception as exc:  # noqa: BLE001 -- the probe reports, never raises
                    rejected.append(alias)
                    last = ProbeResult(
                        key, alias, False,
                        detail=str(exc).strip()[:300],
                        seconds=round(time.monotonic() - started, 2),
                        rejected=tuple(rejected[:-1]),
                    )
                    continue
                result = ProbeResult(
                    key, alias, True,
                    reported=" ".join(str(reply).split())[:120],
                    seconds=round(time.monotonic() - started, 2),
                    rejected=tuple(rejected),
                )
                # Nothing to cache for a vendor-default row: there is no
                # alias to remember, and a cached empty string would be
                # indistinguishable from a missing entry.
                if write_cache and alias:
                    record_resolution(key, alias, reported=result.reported)
                results.append(result)
                break
            else:
                results.append(last)
    finally:
        if owned:
            active.close()
    return results


def render_report(
    binaries: Sequence[BinaryStatus],
    models: Sequence[ProbeResult],
) -> str:
    """The probe as text: what is installed, what resolved, what to do next."""
    lines: List[str] = ["Vendor CLIs", "-----------"]
    for status in binaries:
        if status.backend != "cli":
            lines.append(f"  {status.vendor}: {status.backend} backend (no CLI needed)")
        elif status.path:
            lines.append(f"  {status.vendor}: {status.binary} -> {status.path}")
        else:
            lines.append(
                f"  {status.vendor}: {status.binary} NOT FOUND on PATH — install it "
                f"and sign in with your subscription, or set "
                f"{status.vendor.upper()}_BACKEND=api"
            )

    lines += ["", "Models", "------"]
    for result in models:
        if result.ok:
            tried = f" (after {', '.join(result.rejected)})" if result.rejected else ""
            said = f" — says it is {result.reported!r}" if result.reported else ""
            shown = result.alias or "(the CLI's own default, no model flag)"
            lines.append(f"  OK   {result.key} -> {shown}{tried}{said}")
        elif _looks_exhausted(result.detail):
            # The alias was fine; the window is not. Telling an operator to go
            # edit a working alias because their quota ran out sends them to
            # fix the one thing that is not broken.
            lines.append(f"  SPENT {result.key} -> {result.alias}: {result.detail}")
            lines.append(
                "       the alias is fine and the window is not: wait for it to "
                "roll over. Nothing to change."
            )
        else:
            lines.append(f"  FAIL {result.key} -> {result.alias}: {result.detail}")
            lines.append(
                f"       set {alias_env_var(result.key)} to an alias your CLI "
                f"accepts, or correct the roster row"
            )

    failed = [r for r in models if not r.ok]
    spent = [r for r in failed if _looks_exhausted(r.detail)]
    lines += ["", "Summary", "-------"]
    lines.append(
        f"  {len(models) - len(failed)} of {len(models)} models answered; "
        f"resolutions cached for the next run."
    )
    if failed:
        chain = [r for r in failed if r.key in ORCHESTRATOR_CHAIN]
        if chain and all(r in spent for r in chain):
            lines.append(
                f"  Orchestrator seats out of window: "
                f"{', '.join(r.key for r in chain)}. The seat falls to the next "
                f"link in ORCHESTRATOR_CHAIN, which is what it is for -- a run "
                f"only stops when every seat is gone."
            )
        elif chain:
            lines.append(
                f"  The orchestrator chain is not whole: "
                f"{', '.join(r.key for r in chain)}. A run stops rather than "
                f"seating a model with other work to do, so fix these first."
            )
    lines.append(
        "  Reported model names come from the models themselves and are not "
        "evidence; the round trip is."
    )
    return "\n".join(lines)


def roster_keys() -> List[str]:
    """Every addressable model, for ``--probe-all``."""
    return [m.key for m in ROSTER]


def alias_overrides_in_effect(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Any operator alias overrides, so the probe report can say they are on."""
    import os

    environ = os.environ if env is None else env
    out = []
    for spec in ROSTER:
        var = alias_env_var(spec.key)
        value = environ.get(var, "").strip()
        if value:
            out.append(f"{spec.key}: {var}={value}")
    return out
