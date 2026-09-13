"""Addressing a model by its line rather than by one release.

The roster stores an alias per model. For most rows that alias names one
iteration (``grok-4.6``) and goes stale on a known schedule -- someone reads
the release notes and edits the table. For the orchestrator seats it must not:
the seat is chosen once and held for a whole session, nothing re-picks it
mid-run, and a pinned alias there is how a system keeps running last quarter's
model for months without anything ever reporting a problem. So those rows
carry ``floating=True`` and name the *line* -- ``fable``, ``opus``,
``gpt-6-astra`` -- and this module answers the only question that then
matters: what string do we hand the CLI today?

The strongest form of this is not an alias at all. Where a CLI has its own
notion of a current model -- ``grok models`` prints one and marks it
``(default)`` -- the row carries ``vendor_default`` and resolves to the empty
string, and the invocation simply names no model. The vendor moves its own
pointer and the run follows: nothing to edit, nothing to guess, nothing to
probe. An operator override still outranks it, for the day a vendor's default
moves somewhere you did not want to go.

Otherwise, three sources, in order, and the order is the whole design:

1. **An operator override** in the environment. Whoever is watching the
   vendor's release notes beats anything cached or compiled in, and needs to
   be able to say so without editing Python.
2. **What ``quadratus probe`` last saw a CLI actually accept**, from a small
   JSON cache. This is the only source grounded in evidence rather than in
   someone's belief about what a vendor ships.
3. **The registry seed**, which is a guess made when the table was written.

A vendor CLI that resolves a bare line name -- ``claude --model opus`` -- needs
none of this: the seed is already floating and the CLI does the work. The
machinery exists for the vendors that do not, where "the latest Astra" is a
string only the installed binary knows. That is why resolution is a cache
refreshed by probing rather than a hardcoded version ladder: a ladder would be
one more table going stale in exactly the place we just said must not.

Nothing here calls a CLI. Resolution must be cheap enough to run on every
invocation and must never fail a run, so it reads what the probe left behind
and shrugs if there is nothing there.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from .registry import ModelSpec, resolve

log = logging.getLogger(__name__)

__all__ = [
    "alias_env_var",
    "alias_for",
    "cache_path",
    "load_cache",
    "record_resolution",
    "resolution_source",
]

#: Where probe results live. One file, JSON, per user rather than per repo:
#: which alias a CLI accepts is a fact about the installed binary, not about
#: the checkout.
DEFAULT_CACHE_PATH = Path.home() / ".quadratus" / "aliases.json"

_SANITISE = re.compile(r"[^A-Z0-9]+")


def cache_path() -> Path:
    """The probe cache location, overridable for tests and odd setups."""
    override = os.getenv("QUADRATUS_ALIAS_CACHE", "").strip()
    return Path(override) if override else DEFAULT_CACHE_PATH


def alias_env_var(key: str) -> str:
    """The environment variable that overrides one model's alias.

    ``claude:fable`` -> ``QUADRATUS_ALIAS_CLAUDE_FABLE``. Derived rather than
    listed so a new roster row needs no new plumbing.
    """
    provider, _, alias = key.partition(":")
    return "QUADRATUS_ALIAS_" + _SANITISE.sub("_", f"{provider}_{alias}".upper()).strip("_")


def load_cache(path: Optional[Path] = None) -> Dict[str, dict]:
    """Read the probe cache. A missing or corrupt file is simply empty.

    Never raises: this sits on the invocation path, and a cache is an
    optimisation over the seed, not a dependency.
    """
    target = path or cache_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def record_resolution(
    key: str,
    alias: str,
    *,
    reported: str = "",
    path: Optional[Path] = None,
) -> None:
    """Remember that ``alias`` is what the CLI accepted for ``key``.

    Written by the probe, read by everything else. ``reported`` is whatever
    the model said it was running as when asked -- untrusted (a model
    misnaming itself is common) and stored for the operator to read, never
    for code to branch on.
    """
    target = path or cache_path()
    models = load_cache(target)
    models[key] = {"alias": alias, "reported": reported, "probed_at": time.time()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"models": models}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - disk problems are the operator's
        log.warning("could not write the alias cache at %s: %s", target, exc)


def _cached_alias(key: str, spec: ModelSpec, cache: Mapping[str, dict]) -> Optional[str]:
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    alias = entry.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        return None
    # A cached alias for a pinned row is ignored: the row names one release on
    # purpose, and honouring a cache there would let a stale probe silently
    # move work onto a different model than the table says.
    if not spec.floating:
        return None
    return alias.strip()


def alias_for(
    key: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    cache: Optional[Mapping[str, dict]] = None,
) -> str:
    """The string to hand the vendor CLI for this model key, today.

    Falls through override -> probe cache -> registry seed. An unknown key
    comes back as its own alias half (``foo:bar`` -> ``bar``) rather than
    raising: routing tables and the roster can disagree during an edit, and a
    hard failure here would turn a table typo into a dead run.
    """
    environ = os.environ if env is None else env
    spec = resolve(key)

    override = environ.get(alias_env_var(key), "").strip()
    if override:
        return override

    if spec is None:
        return key.partition(":")[2] or key

    # A vendor-default row is addressed by naming no model: the empty string
    # travels down to _build_argv, which omits the flag. Checked after the
    # override so an operator can still pin one when a vendor's default moves
    # somewhere they do not want to go.
    if spec.vendor_default:
        return ""

    cached = _cached_alias(key, spec, load_cache() if cache is None else cache)
    if cached:
        return cached

    return spec.alias


def resolution_source(
    key: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    cache: Optional[Mapping[str, dict]] = None,
) -> Tuple[str, str]:
    """``(alias, where it came from)``, for status output and the probe report.

    Kept separate from :func:`alias_for` so the hot path stays a string lookup
    and only the human-facing code pays for the explanation.
    """
    environ = os.environ if env is None else env
    spec = resolve(key)
    if environ.get(alias_env_var(key), "").strip():
        return environ[alias_env_var(key)].strip(), "operator override"
    if spec is None:
        return key.partition(":")[2] or key, "unknown model"
    if spec.vendor_default:
        return "", "the CLI's own default (no model flag sent)"
    cached = _cached_alias(key, spec, load_cache() if cache is None else cache)
    if cached:
        return cached, "probed"
    return spec.alias, "registry seed (floating)" if spec.floating else "registry seed (pinned)"
