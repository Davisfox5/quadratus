"""Probes: replace the roster's claims with what your machine actually has.

Model tables are seeds, not truth -- the lineup churns monthly, and the
``alias`` column is explicitly the least reliable field in the registry. A
probe is the antidote: instead of trusting that ``grok-4.6`` resolves on the
installed CLI, ask the CLI.

Two depths, priced accordingly:

* **Binary probe** (free): is the vendor's CLI on PATH at all? This is what
  the run entry point uses for its availability callable -- it answers "can
  this subscription be used" without spending a single call.
* **Live probe** (one cheap call per model): does this exact alias resolve
  and answer? This is ``multi-llm probe`` -- run it after installing or
  updating a CLI, and before trusting a new roster row.

The long-context probe at depth (the one that would earn Grok 4.20 its
LONG_CONTEXT tag back) is deliberately not here yet: it costs real window to
run honestly, and a cheap version would produce exactly the unearned
confidence the registry warns against.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from .cli_providers import CLI_SPECS, cli_provider_classes
from .registry import CONTROL_PLANE, MODE_ROSTERS, ORCHESTRATOR_CHAIN, resolve
from .workers import WORKER_ESCALATION, WORKER_TREE

__all__ = [
    "ProbeResult",
    "system_model_keys",
    "binary_available",
    "probe_binaries",
    "probe_models",
    "render_probe_report",
]


@dataclass(frozen=True)
class ProbeResult:
    key: str
    ok: bool
    detail: str


def system_model_keys() -> List[str]:
    """Every model key the system can actually route work to.

    Drawn from the live routing structures rather than the whole roster, so
    the probe measures what a run would touch -- orchestrator chain, brain
    trust, control plane, worker tree and its escalation targets.
    """
    keys: List[str] = []
    for key in (
        *ORCHESTRATOR_CHAIN,
        *MODE_ROSTERS["adversarial"]["peers"],
        *CONTROL_PLANE.values(),
        *WORKER_TREE.values(),
        *WORKER_ESCALATION.values(),
    ):
        if key not in keys:
            keys.append(key)
    return keys


def binary_available(vendor: str) -> bool:
    spec = CLI_SPECS.get(vendor)
    return bool(spec and shutil.which(spec.binary))


def probe_binaries(keys: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    """Per-model availability from binary presence alone. Free."""
    return {
        key: binary_available(key.split(":", 1)[0])
        for key in (keys or system_model_keys())
    }


def _default_runner(key: str, prompt: str) -> str:
    """One real CLI call against the model's own alias, read-only, short."""
    vendor, alias = key.split(":", 1)
    provider_cls = cli_provider_classes()[vendor]
    provider = provider_cls(alias, timeout=90, max_retries=1)
    try:
        return provider.generate(prompt, system="Reply with exactly what is asked.")
    finally:
        provider.cleanup()


def probe_models(
    keys: Optional[Sequence[str]] = None,
    *,
    run: Optional[Callable[[str, str], str]] = None,
) -> List[ProbeResult]:
    """Live-probe each model: does the alias resolve and answer?

    ``run`` is injectable for tests; the default drives the real vendor CLI
    with a minimal read-only call. A vendor whose binary is absent is
    reported without an attempted call -- no point timing out four times on
    a CLI that is not installed.
    """
    run = run or _default_runner
    results: List[ProbeResult] = []
    for key in (keys or system_model_keys()):
        vendor = key.split(":", 1)[0]
        if not binary_available(vendor):
            spec = CLI_SPECS.get(vendor)
            binary = spec.binary if spec else vendor
            results.append(ProbeResult(key, False, f"'{binary}' not on PATH"))
            continue
        try:
            reply = run(key, "Reply with exactly: OK")
        except Exception as exc:  # noqa: BLE001 -- a probe reports, never raises
            results.append(ProbeResult(key, False, str(exc)[:200]))
            continue
        if (reply or "").strip():
            results.append(ProbeResult(key, True, "answered"))
        else:
            results.append(ProbeResult(key, False, "empty reply"))
    return results


def render_probe_report(results: Sequence[ProbeResult]) -> str:
    lines = ["# Probe report", ""]
    for r in results:
        spec = resolve(r.key)
        label = spec.label if spec else r.key
        mark = "ok  " if r.ok else "FAIL"
        lines.append(f"[{mark}] {r.key:28s} {label:22s} {r.detail}")
    failures = sum(1 for r in results if not r.ok)
    lines.append("")
    lines.append(
        f"{len(results) - failures}/{len(results)} models answered."
        + (" Fix the failures before trusting a full run." if failures else "")
    )
    return "\n".join(lines)
