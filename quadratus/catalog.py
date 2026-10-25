"""What the installed vendor CLIs say they can reach, read without a model
call (2026-10-24).

Two catalogues, both authoritative because the installed client wrote them
and neither costs a token:

* **Codex** keeps ``$CODEX_HOME/models_cache.json`` (default
  ``~/.codex/models_cache.json``), the ``/models`` listing the client
  fetched for itself: each entry a slug such as ``gpt-6.1-sol``, its
  visibility, the reasoning efforts it supports and the minimal client
  that may use it. The file carries when it was fetched and by which
  client, so it can be judged fresh or stale and matched to the binary on
  PATH. A family (``sol``, ``astra``, ``luna``, ``terra``) is the suffix of a
  slug; its latest available release is the highest numeric version among
  the visible, client-compatible slugs that carry it. Nothing here guesses
  a slug: a family the catalogue does not list resolves to nothing, with the
  reason, and a stale, missing or malformed catalogue is a status, never a
  fallback to a compiled-in edition.
* **Grok** prints its own list with ``grok models`` and marks one entry
  ``(default)``. That is the whole observation: the seat still sends no
  model flag, and what the vendor's default is at this moment is recorded
  beside the call, never acted on.

Claude needs no catalogue: ``claude --model opus`` names a family the CLI
resolves to its current release (model-config docs), and that alias is what
goes on the wire.

The API documentation of these models is not consulted. A model's API
capabilities differ from the CLI's (the installed Sol defaults to ``low``
where the API documents ``medium``), and this repo drives the CLIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "CODEX_EFFORTS",
    "Catalog",
    "CatalogModel",
    "DEFAULT_MAX_AGE_HOURS",
    "GrokCatalog",
    "codex_catalog_path",
    "family_of",
    "grok_catalog",
    "latest_for_family",
    "parse_grok_models",
    "parse_slug",
    "read_codex_catalog",
]

#: How old a Codex catalogue may be before it is stale. The client refreshes
#: it on its own start, so a week-old file means nobody has run codex for a
#: week and the lineup may have moved. ``QUADRATUS_CATALOG_MAX_AGE_HOURS``
#: overrides it; the reason always says which figure applied.
DEFAULT_MAX_AGE_HOURS = 168.0
#: The reasoning efforts the Codex client documents, in order. ``ultra`` is
#: listed so a catalogue entry that names it is read, never selected by the
#: effort policy (its description is automatic task delegation, which would
#: widen the worker tree in silence).
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")

_SLUG = re.compile(r"^(?P<prefix>[a-z]+)-(?P<version>\d+(?:\.\d+)*)-(?P<family>[a-z][a-z0-9-]*)$")
_HIDDEN = {"hide", "hidden", "disabled", "deprecated", "retired", "internal"}
_GROK_ID = re.compile(r"\bgrok-[0-9][\w.-]*", re.IGNORECASE)


@dataclass(frozen=True)
class CatalogModel:
    slug: str
    family: str
    version: Tuple[int, ...]
    visible: bool
    efforts: Tuple[str, ...]
    default_effort: Optional[str] = None
    minimal_client: Optional[str] = None
    priority: Optional[int] = None
    display_name: str = ""

    @property
    def usable_efforts(self) -> Tuple[str, ...]:
        """The efforts an explicit call may ask for: never ``ultra``."""
        return tuple(e for e in self.efforts if e != "ultra")


@dataclass(frozen=True)
class Catalog:
    """A read of the Codex catalogue: its status, why, and its provenance."""

    status: str
    reason: str = ""
    models: Tuple[CatalogModel, ...] = ()
    fetched_at: Optional[str] = None
    client_version: Optional[str] = None
    installed_client: Optional[str] = None
    path: str = ""
    sha256: str = ""
    age_hours: Optional[float] = None
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def provenance(self) -> dict:
        return dict(source="codex models_cache.json", path=self.path, sha256=self.sha256,
                    fetched_at=self.fetched_at, client_version=self.client_version,
                    installed_client=self.installed_client, status=self.status, reason=self.reason,
                    age_hours=None if self.age_hours is None else round(self.age_hours, 2),
                    max_age_hours=self.max_age_hours)

    def by_slug(self, slug: str) -> Optional[CatalogModel]:
        return next((m for m in self.models if m.slug == slug), None)


def codex_catalog_path(env: Optional[dict] = None) -> Path:
    """Where the client keeps its catalogue: an explicit override, else
    ``$CODEX_HOME/models_cache.json``, else ``~/.codex/models_cache.json``."""
    environ = os.environ if env is None else env
    override = str(environ.get("QUADRATUS_CODEX_CATALOG", "") or "").strip()
    if override:
        return Path(override).expanduser()
    home = str(environ.get("CODEX_HOME", "") or "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "models_cache.json"


def parse_slug(slug: str) -> Optional[Tuple[Tuple[int, ...], str]]:
    """``gpt-6.1-sol`` -> ``((6, 1), "sol")``; ``gpt-6-astra`` -> ``((6,), "astra")``;
    a future ``gpt-7-sol`` orders above both. None for a slug with no
    numeric release segment (``codex-mini-latest``), which belongs to no family."""
    match = _SLUG.match(str(slug or "").strip().lower())
    if not match:
        return None
    version = tuple(int(part) for part in match.group("version").split("."))
    return version, match.group("family")


def family_of(slug: str) -> Optional[str]:
    parsed = parse_slug(slug)
    return parsed[1] if parsed else None


def _parse_version(text) -> Optional[Tuple[int, ...]]:
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(text or ""))
    return tuple(int(x) for x in match.group(1).split(".")) if match else None


def _parse_fetched(value) -> Optional[datetime]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _efforts(raw) -> Optional[Tuple[str, ...]]:
    """The supported efforts as the catalogue lists them (objects with an
    ``effort`` key, or bare strings); None when the entry does not say."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return ()
    out: List[str] = []
    for item in raw:
        name = item.get("effort") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            level = name.strip().lower()
            if level not in out:
                out.append(level)
    return tuple(out)


def _model(raw) -> Optional[CatalogModel]:
    if not isinstance(raw, dict) or not isinstance(raw.get("slug"), str) or not raw["slug"].strip():
        raise ValueError("a models entry has no slug")
    slug = raw["slug"].strip()
    parsed = parse_slug(slug)
    visibility = raw.get("visibility", "list")
    visible = not (isinstance(visibility, str) and visibility.strip().lower() in _HIDDEN)
    if isinstance(visibility, bool):
        visible = visibility
    efforts = _efforts(raw.get("supported_reasoning_levels"))
    default = raw.get("default_reasoning_level")
    priority = raw.get("priority")
    return CatalogModel(
        slug=slug, family=parsed[1] if parsed else "", version=parsed[0] if parsed else (),
        visible=visible, efforts=efforts if efforts is not None else (),
        default_effort=default.strip().lower() if isinstance(default, str) and default.strip() else None,
        minimal_client=str(raw["minimal_client_version"]).strip()
        if isinstance(raw.get("minimal_client_version"), str) and raw["minimal_client_version"].strip() else None,
        priority=priority if isinstance(priority, int) and not isinstance(priority, bool) else None,
        display_name=str(raw.get("display_name") or ""))


def read_codex_catalog(path: Optional[Path] = None, *, installed_client: Optional[str] = None,
                       now: Optional[float] = None, max_age_hours: Optional[float] = None,
                       env: Optional[dict] = None) -> Catalog:
    """Read and judge the Codex catalogue. Never raises: the status says
    ``ok``, ``missing``, ``unreadable``, ``malformed``, ``stale`` or
    ``client mismatch``, with the reason, and only ``ok`` resolves anything."""
    environ = os.environ if env is None else env
    target = Path(path) if path is not None else codex_catalog_path(environ)
    if max_age_hours is None:
        raw_age = str(environ.get("QUADRATUS_CATALOG_MAX_AGE_HOURS", "") or "").strip()
        try:
            max_age_hours = float(raw_age) if raw_age else DEFAULT_MAX_AGE_HOURS
        except ValueError:
            max_age_hours = DEFAULT_MAX_AGE_HOURS
    base = dict(path=str(target), installed_client=installed_client, max_age_hours=float(max_age_hours))
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        return Catalog("missing", f"no catalogue at {target}; run the codex client once to write it", **base)
    except OSError as exc:
        return Catalog("unreadable", f"{type(exc).__name__}: {exc}", **base)
    base["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return Catalog("malformed", f"not JSON: {type(exc).__name__}", **base)
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        return Catalog("malformed", "no models list", **base)
    fetched = _parse_fetched(body.get("fetched_at"))
    fetched_text = body.get("fetched_at")
    base["fetched_at"] = str(fetched_text) if fetched_text is not None else None
    client = body.get("client_version")
    base["client_version"] = str(client).strip() if isinstance(client, str) and client.strip() else None
    if fetched is None:
        return Catalog("malformed", "fetched_at missing or unparseable", **base)
    try:
        models = tuple(_model(m) for m in body["models"])
    except ValueError as exc:
        return Catalog("malformed", str(exc), **base)
    moment = time.time() if now is None else float(now)
    age = (moment - fetched.timestamp()) / 3600.0
    base["age_hours"] = age
    if age > float(max_age_hours):
        return Catalog("stale", f"fetched {age:.1f} h ago, over the {float(max_age_hours):g} h bound; run the codex "
                                "client once to refresh it", models=models, **base)
    if age < -1.0:
        return Catalog("malformed", f"fetched_at is {-age:.1f} h in the future", **base)
    if base["client_version"] and installed_client:
        have, wrote = _parse_version(installed_client), _parse_version(base["client_version"])
        if have is not None and wrote is not None and have != wrote:
            return Catalog("client mismatch", f"catalogue written by client {base['client_version']}, installed "
                                              f"client is {installed_client}; run the installed client once",
                           models=models, **base)
    return Catalog("ok", "", models=models, **base)


def _client_ok(model: CatalogModel, installed_client: Optional[str]) -> bool:
    if not model.minimal_client or not installed_client:
        return True
    have, need = _parse_version(installed_client), _parse_version(model.minimal_client)
    return have is None or need is None or have >= need


def latest_for_family(catalog: Catalog, family: str, *, installed_client: Optional[str] = None
                      ) -> Tuple[Optional[CatalogModel], str]:
    """The highest numeric release of ``family`` among the visible,
    client-compatible entries, with the reason when there is none. ``6.10``
    orders above ``6.9``; a ``7`` above any ``6.x``."""
    if not catalog.ok:
        return None, f"catalogue {catalog.status}: {catalog.reason}"
    wanted = str(family or "").strip().lower()
    candidates = [m for m in catalog.models if m.family == wanted]
    if not candidates:
        return None, f"no {wanted} model in the catalogue"
    usable = [m for m in candidates if m.visible and _client_ok(m, installed_client or catalog.installed_client)]
    if not usable:
        return None, (f"no visible {wanted} model the installed client may use (listed: "
                      + ", ".join(m.slug for m in candidates) + ")")
    usable.sort(key=lambda m: (m.version, -(m.priority if m.priority is not None else 10**6)), reverse=True)
    return usable[0], ""


@dataclass(frozen=True)
class GrokCatalog:
    """What ``grok models`` printed: the default the vendor marks, every id it
    listed, and any notice (an unauthenticated session still lists models;
    the notice is kept, not read as a failure)."""

    status: str
    default: Optional[str] = None
    available: Tuple[str, ...] = ()
    notices: Tuple[str, ...] = ()
    reason: str = ""
    command: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def record(self) -> dict:
        return dict(source="grok models", status=self.status, default=self.default, available=list(self.available),
                    notices=list(self.notices), reason=self.reason, command=list(self.command))


def parse_grok_models(text: str) -> GrokCatalog:
    """Read the listing: the id on a line that says ``default`` is the
    default; every ``grok-...`` id is available; other lines are notices."""
    default, available, notices = None, [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        ids = [m.group(0).rstrip(".,:;") for m in _GROK_ID.finditer(line)]
        if not ids:
            if re.search(r"authenticat|sign in|log in|login", line, re.IGNORECASE):
                notices.append(line[:200])
            continue
        for found in ids:
            if found not in available:
                available.append(found)
        if "default" in line.lower() and default is None:
            default = ids[0]
    if not available:
        return GrokCatalog("unavailable", reason="no model ids in the listing", notices=tuple(notices))
    if default is None:
        return GrokCatalog("no default marked", available=tuple(available), notices=tuple(notices),
                           reason="the listing marks no default")
    return GrokCatalog("ok", default=default, available=tuple(available), notices=tuple(notices))


def grok_catalog(binary: str, *, args: Sequence[str] = ("models",), timeout: float = 20.0,
                 run=None) -> GrokCatalog:
    """Run the listing command (no prompt, no generation) and parse it. A
    missing binary, a non-zero exit or a timeout is an ``unavailable``
    status with the reason; nothing is retried."""
    argv = [str(binary), *args]
    runner = run or subprocess.run
    try:
        done = runner(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GrokCatalog("unavailable", reason=f"{type(exc).__name__}: {str(exc)[:160]}", command=tuple(argv))
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return GrokCatalog("unavailable", reason=f"exit {done.returncode}" + (f": {tail[-1][:160]}" if tail else ""),
                           command=tuple(argv))
    parsed = parse_grok_models((done.stdout or "") + "\n" + (done.stderr or ""))
    return GrokCatalog(parsed.status, default=parsed.default, available=parsed.available, notices=parsed.notices,
                       reason=parsed.reason, command=tuple(argv))
