"""Addressing a model by its family rather than by one release.

The roster names families: ``claude:opus``, ``openai:sol``, ``grok:default``.
A family is the thing the routing tables, the price sheet and the ledger
mean; which release answers for it today is a fact about the installed
client, read here without a model call (2026-10-24 rewrite of the
alias-cache design, whose canned seeds -- ``gpt-5.6-sol`` beside an
installed 6.1 -- were exactly the stale table the floating design was meant
to retire).

Three vendors, three answers:

* **Claude** resolves a family alias itself: ``claude --model opus`` is the
  current Opus (model-config docs). The alias goes on the wire as written.
  The probe's alias cache is no longer consulted for it: a dated id probed
  months ago must not defeat the alias the CLI keeps current.
* **Codex** takes a concrete slug, and the installed client keeps the list
  it fetched for itself (:mod:`quadratus.catalog`). The family's latest
  visible, client-compatible release is the slug; a missing, stale,
  malformed or mismatched catalogue makes the family *unavailable*, with
  the reason, and never selects an older edition from memory.
* **Grok** is addressed by sending no model flag; the vendor's own default
  applies. What that default is right now is read from ``grok models`` and
  recorded beside the call, never acted on.

An operator override (``QUADRATUS_ALIAS_<VENDOR>_<FAMILY>``) still outranks
all three: an explicit, visible exception, recorded as such.

A job resolves each family once (``runtime.Fleet``) and holds that answer
for every call it makes, so a release that ships mid-run changes nothing
until the next job; the resolution and its provenance are part of the run
record. Legacy version keys (``openai:gpt-5.6-sol``) still resolve, to the
family they named, so histories and operator inputs written against them
keep reading.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from . import catalog as _catalog
from .effort import VENDOR_LADDERS
from .providers import ProviderError
from .registry import ModelSpec, canonical_key, resolve

log = logging.getLogger(__name__)

__all__ = [
    "ModelUnavailable",
    "Resolution",
    "alias_env_var",
    "ladder_env_var",
    "alias_for",
    "cache_path",
    "load_cache",
    "record_resolution",
    "resolution_source",
    "resolve_model",
]

#: Where probe results live. One file, JSON, per user rather than per repo:
#: what a CLI accepted is a fact about the installed binary, not about the
#: checkout. A record for the operator and the status line; not a source
#: of the wire string.
DEFAULT_CACHE_PATH = Path.home() / ".quadratus" / "aliases.json"

_SANITISE = re.compile(r"[^A-Z0-9]+")


class ModelUnavailable(ProviderError):
    """A family that resolves to nothing on this machine right now, with
    the reason. Raised at dispatch, never papered over with an older id."""


@dataclass(frozen=True)
class Resolution:
    """What one roster key resolves to for this job: the key as asked
    (legacy spellings included), its family key, the string sent on the
    wire (``""`` for a vendor default, None when unavailable), where that
    answer came from, and the ladder of efforts the resolved model
    supports when the source said."""

    requested: str
    key: str
    vendor: str
    family: str
    wire: Optional[str]
    source: str
    status: str
    reason: str = ""
    supported: Optional[Tuple[str, ...]] = None
    provenance: dict = field(default_factory=dict)
    observed_default: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.status == "resolved"

    def to_dict(self) -> dict:
        return dict(requested=self.requested, key=self.key, vendor=self.vendor, family=self.family, wire=self.wire,
                    source=self.source, status=self.status, reason=self.reason,
                    supported=list(self.supported) if self.supported is not None else None,
                    provenance=dict(self.provenance), observed_default=self.observed_default)

    @classmethod
    def from_dict(cls, data: Mapping) -> "Resolution":
        supported = data.get("supported")
        return cls(requested=str(data.get("requested") or data.get("key")), key=str(data["key"]),
                   vendor=str(data.get("vendor") or str(data["key"]).partition(":")[0]),
                   family=str(data.get("family") or ""), wire=data.get("wire"), source=str(data.get("source") or ""),
                   status=str(data.get("status") or "resolved"), reason=str(data.get("reason") or ""),
                   supported=tuple(supported) if isinstance(supported, list) else None,
                   provenance=dict(data.get("provenance") or {}), observed_default=data.get("observed_default"))


def cache_path() -> Path:
    """The probe cache location, overridable for tests and odd setups."""
    override = os.getenv("QUADRATUS_ALIAS_CACHE", "").strip()
    return Path(override) if override else DEFAULT_CACHE_PATH


def alias_env_var(key: str) -> str:
    """The environment variable that overrides one model's wire string.

    ``claude:fable`` -> ``QUADRATUS_ALIAS_CLAUDE_FABLE``. Derived rather than
    listed so a new roster row needs no new plumbing. A legacy key maps to
    its family's variable, so an old ``.env`` keeps working.
    """
    provider, _, alias = canonical_key(key).partition(":")
    return "QUADRATUS_ALIAS_" + _SANITISE.sub("_", f"{provider}_{alias}".upper()).strip("_")


def ladder_env_var(key: str) -> str:
    """The variable an operator declares a pinned model's effort ladder in
    (``QUADRATUS_EFFORT_LADDER_OPENAI_SOL=low,medium,high``) when the pinned
    slug is outside the installed catalogue: an explicit, recorded
    assertion; without it the pin cannot be validated and is unavailable."""
    return alias_env_var(key).replace("QUADRATUS_ALIAS_", "QUADRATUS_EFFORT_LADDER_", 1)


def _declared_ladder(environ, key: str) -> Optional[Tuple[str, ...]]:
    raw = str(environ.get(ladder_env_var(key), "") or "").strip()
    if not raw:
        return None
    levels = [part.strip().lower() for part in raw.split(",") if part.strip()]
    return tuple(level for level in ("low", "medium", "high", "xhigh", "max") if level in levels)


def load_cache(path: Optional[Path] = None) -> Dict[str, dict]:
    """Read the probe record. A missing or corrupt file is simply empty."""
    target = path or cache_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def record_resolution(key: str, alias: str, *, reported: str = "", path: Optional[Path] = None) -> None:
    """Remember that ``alias`` is what the CLI accepted for ``key`` when the
    probe called it. ``reported`` is whatever the model said it was,
    untrusted and stored for the operator to read, never for code to branch
    on. The record informs the status line; the wire string comes from
    :func:`resolve_model`."""
    target = path or cache_path()
    models = load_cache(target)
    models[canonical_key(key)] = {"alias": alias, "reported": reported, "probed_at": time.time()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"models": models}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk problems are the operator's
        log.warning("could not write the alias record at %s: %s", target, exc)


def _ladder(spec: Optional[ModelSpec], vendor: str) -> Optional[Tuple[str, ...]]:
    documented = VENDOR_LADDERS.get(vendor)
    return tuple(documented) if documented else None


def resolve_model(key: str, *, env: Optional[Mapping[str, str]] = None, catalog=None,
                  installed_client: Optional[str] = None, grok=None, now: Optional[float] = None) -> Resolution:
    """The resolution for ``key`` now. Never raises.

    ``catalog`` is a read Codex catalogue (:func:`catalog.read_codex_catalog`);
    when None and the key is OpenAI's, the installed client's file is read.
    ``grok`` is a read ``grok models`` listing for the observed default.
    """
    environ = os.environ if env is None else env
    spec = resolve(key)
    family_key = canonical_key(key)
    vendor = family_key.partition(":")[0]
    family = spec.family or spec.alias if spec is not None else key.partition(":")[2]
    override = str(environ.get(alias_env_var(key), "") or "").strip()
    if override:
        provenance = dict(source="operator override", variable=alias_env_var(key))
        supported = _ladder(spec, vendor)
        declared = _declared_ladder(environ, key)
        if spec is not None and vendor == "openai":
            read = catalog if catalog is not None else _catalog.read_codex_catalog(installed_client=installed_client,
                                                                                   now=now, env=environ)
            entry = read.by_slug(override) if read.ok else None
            supported = entry.usable_efforts if entry is not None else None
            provenance.update(catalog=read.provenance(),
                              note=("pinned slug is in the catalogue" if entry is not None
                                    else "pinned slug is not in the catalogue"))
            if declared is not None:
                # The operator's own assertion of the pinned slug's ladder,
                # recorded as such; it never overrides a ladder the
                # catalogue lists.
                provenance.update(declared_ladder=list(declared), declared_in=ladder_env_var(key))
                if supported is None:
                    supported = declared
            if supported is None:
                return Resolution(key, family_key, vendor, family, None, "operator override", "unavailable",
                                  reason=(f"pinned slug {override!r} is not in the installed client's catalogue and "
                                          f"its effort ladder is unknown; declare it in {ladder_env_var(key)} "
                                          "(for example low,medium,high) or pin a catalogued slug"),
                                  provenance=provenance)
            if not supported:
                return Resolution(key, family_key, vendor, family, None, "operator override", "unavailable",
                                  reason=f"pinned slug {override!r} lists no usable effort; nothing can be sent "
                                         "at an explicit effort", provenance=provenance)
        return Resolution(key, family_key, vendor, family, override, "operator override", "resolved",
                          supported=supported, provenance=provenance)
    if spec is None:
        return Resolution(key, family_key, vendor, family, None, "unknown model", "unavailable",
                          reason=f"{key} is outside the active roster")
    if spec.vendor_default:
        observed = grok.default if grok is not None and getattr(grok, "ok", False) else None
        provenance = dict(source="vendor default", grok=grok.record() if grok is not None else None)
        return Resolution(key, family_key, vendor, family, "", "the CLI's own default (no model flag sent)",
                          "resolved", supported=_ladder(spec, vendor), provenance=provenance, observed_default=observed)
    if vendor == "openai":
        read = catalog if catalog is not None else _catalog.read_codex_catalog(installed_client=installed_client,
                                                                               now=now, env=environ)
        found, why = _catalog.latest_for_family(read, family, installed_client=installed_client)
        provenance = dict(source="codex catalogue", catalog=read.provenance())
        if found is None:
            return Resolution(key, family_key, vendor, family, None, "codex catalogue", "unavailable", reason=why,
                              provenance=provenance)
        provenance.update(slug=found.slug, display_name=found.display_name, default_effort=found.default_effort,
                          listed_efforts=list(found.efforts) if found.efforts is not None else None,
                          listed=[m.slug for m in read.models if m.family == family])
        usable = found.usable_efforts
        if usable is None:
            # An unknown ladder is not permission to use the documented one:
            # no explicit effort can be validated, so nothing is sent.
            return Resolution(key, family_key, vendor, family, None, "codex catalogue", "unavailable",
                              reason=f"the catalogue lists no effort ladder for {found.slug}; no explicit effort can "
                                     "be validated (run the codex client once to refresh the catalogue)",
                              provenance=provenance)
        if not usable:
            return Resolution(key, family_key, vendor, family, None, "codex catalogue", "unavailable",
                              reason=f"the catalogue lists no usable effort for {found.slug} (listed: "
                                     f"{', '.join(found.efforts) or 'nothing'}); nothing can be sent at an explicit "
                                     "effort", provenance=provenance)
        return Resolution(key, family_key, vendor, family, found.slug,
                          f"latest {family} in the installed client's catalogue", "resolved",
                          supported=usable, provenance=provenance)
    if spec.floating:
        return Resolution(key, family_key, vendor, family, spec.alias, "family alias; the CLI resolves it",
                          "resolved", supported=_ladder(spec, vendor),
                          provenance=dict(source="family alias", alias=spec.alias))
    return Resolution(key, family_key, vendor, family, spec.alias, "registry seed (pinned)", "resolved",
                      supported=_ladder(spec, vendor), provenance=dict(source="registry seed", alias=spec.alias))


def alias_for(key: str, *, env: Optional[Mapping[str, str]] = None, cache: Optional[Mapping[str, dict]] = None,
              catalog=None, installed_client: Optional[str] = None) -> str:
    """The string to hand the vendor CLI for this model key, today, or
    :class:`ModelUnavailable` with the reason. ``cache`` is accepted for
    callers written against the old signature and is not consulted: a
    probe's dated record never outranks the installed client."""
    found = resolve_model(key, env=env, catalog=catalog, installed_client=installed_client)
    if not found.available:
        raise ModelUnavailable(f"{key}: {found.reason}")
    return found.wire or ""


def resolution_source(key: str, *, env: Optional[Mapping[str, str]] = None,
                      cache: Optional[Mapping[str, dict]] = None, catalog=None,
                      installed_client: Optional[str] = None) -> Tuple[str, str]:
    """``(wire string, where it came from)`` for status output; an
    unavailable family reads ``("", "unavailable: <reason>")``."""
    found = resolve_model(key, env=env, catalog=catalog, installed_client=installed_client)
    if not found.available:
        return "", f"unavailable: {found.reason}"
    return found.wire or "", found.source
