"""The durable job store: immutable record generations under one manifest.

Blueprint section 8 (2026-10-17, stage B): keep the existing single-project
coordinator lock and file artifacts; publish immutable record generations,
then one atomic current-generation manifest naming their digests and schema
version. :mod:`quadratus.generations` holds the pure rules; this module is
the writer and the readers.

Durability, in order, for every publication:

1. each new record's bytes are written to a temporary file, flushed and
   fsynced, then renamed to its content-addressed path and the ``records``
   directory is fsynced: a record a manifest will name is on disk before the
   manifest can name it;
2. the manifest is written the same way (temporary, fsync, atomic replace)
   and its directory is fsynced;
3. a failure anywhere latches the store: nothing later is published, and
   the caller treats the state as unavailable (the budget stops the run
   before any effect, as it always did on a failed write). A failure after
   the manifest replace and before its directory fsync is an uncertain
   commit, reported as such: the caller never rolls back and never invokes.

Readers read only the committed manifest. A manifest whose records are
missing or do not hash to what it names is corrupt, and a corrupt current
generation is refused, never replaced by an older one: an older generation
would omit a started action or spent budget (:func:`generations.fallback_allowed`).
Compatibility exports (``budget.json``, ``checkpoint.json``) are written
after the manifest for historical readers; a reader that finds a manifest
never trusts an export over it.

Nothing here makes source edits or remote effects transactional.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from . import generations as G

__all__ = ["MANIFEST", "StoreUnavailable", "GenerationCorrupt", "JobStore", "read_manifest",
           "read_record", "read_budget", "read_terminal", "unsettled_operations", "operation_records",
           "owner_of"]

MANIFEST = "manifest.json"
RECORDS = "records"


class StoreUnavailable(RuntimeError):
    """The store refused or failed a publication; it stays refused."""


class GenerationCorrupt(RuntimeError):
    """The committed manifest names records that are missing or altered."""


def _canonical(record) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode()


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_durable(target: Path, data: bytes) -> None:
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


class JobStore:
    """One writer per run directory, under the project's run lock. ``fault``
    is a test hook called with a stage name before each durable step; a
    hook that raises or exits models a crash there."""

    def __init__(self, run_dir, *, fault: Optional[Callable[[str], None]] = None):
        self.run_dir = Path(run_dir)
        self._lock = threading.RLock()
        self._fault = fault
        self._refused: Optional[str] = None
        self._current: Optional[G.Generation] = None
        self._history: list = []
        self._loaded = False
        #: Operations started and not settled, by the publisher that owns
        #: them: the budget's tickets, and each session's own effects
        #: (checks, captures, probes, fixture writes, evidence runs) under
        #: that session's owner id. The manifest carries their union; one
        #: owner never replaces another's set.
        self.budget_unsettled: set = set()
        self.session_unsettled: Dict[str, set] = {}
        self.reserved_unfinished: int = 0

    # -- state -----------------------------------------------------------------

    @property
    def refused(self) -> Optional[str]:
        return self._refused

    def _check(self) -> None:
        if self._refused:
            raise StoreUnavailable(f"the job store is refused: {self._refused}")

    def _step(self, stage: str) -> None:
        if self._fault is not None:
            self._fault(stage)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        manifest = read_manifest(self.run_dir)
        if manifest is None:
            return
        self._current = manifest
        self._history = _history(self.run_dir, manifest)
        self.budget_unsettled = {o for o in manifest.unsettled if o.startswith("O-")}
        self.session_unsettled = {}
        for opid in manifest.unsettled:
            if not opid.startswith("O-"):
                self.session_unsettled.setdefault(owner_of(opid), set()).add(opid)
        self.reserved_unfinished = int(manifest.reserved_unfinished)

    def current(self) -> Optional[G.Generation]:
        """The committed manifest as it is on disk now: re-read on every
        call, so a reader never sees the cached older generation after a
        publication that failed past the manifest replace (an uncertain
        commit is still the commit on disk), and never trusts memory over
        the file. A corrupt manifest raises; it is never read around."""
        with self._lock:
            self._loaded = False
            self._load()
            return self._current

    # -- publication -----------------------------------------------------------

    def publish(self, updates: Mapping[str, object], *, budget_unsettled: Optional[Sequence[str]] = None,
                session_unsettled: Optional[Tuple[str, Sequence[str]]] = None,
                reserved_unfinished: Optional[int] = None,
                expected: Optional[int] = "current") -> G.Generation:
        """Commit one generation: the current records with ``updates`` (name
        to JSON value) replacing or adding theirs. ``expected`` is the
        generation number the caller decided on; the default is whatever is
        current under the lock, the single-writer case. The unsettled sets
        and the unfinished reservation are kept unless given. Returns the
        committed generation; raises StoreUnavailable (latched) on any
        failure, after which nothing is published again."""
        with self._lock:
            self._check()
            try:
                # Decided on the generation on disk now, never on this
                # store's cache: another writer (a second store on the same
                # run, a concurrent process) may have committed since this
                # one last read, and a publication planned on stale state
                # would replace its manifest.
                self._loaded = False
                self._load()
                current = self._current
                planned = current.digest if current else None
                if expected == "current":
                    expected = current.number if current else None
                if budget_unsettled is not None:
                    self.budget_unsettled = set(budget_unsettled)
                if session_unsettled is not None:
                    owner, ids = session_unsettled
                    self.session_unsettled[str(owner)] = set(ids)
                if reserved_unfinished is not None:
                    self.reserved_unfinished = int(reserved_unfinished)
                encoded = {name: _canonical(value) for name, value in updates.items()}
                digests = dict(current.records) if current else {}
                digests.update({name: _digest_bytes(data) for name, data in encoded.items()})
                unsettled = sorted(self.budget_unsettled | set().union(*self.session_unsettled.values())
                                   if self.session_unsettled else self.budget_unsettled)
                publication = G.plan_publication(current, expected=expected, records=digests,
                                                 history=self._history, unsettled=unsettled,
                                                 reserved_unfinished=self.reserved_unfinished)
                folder = self.run_dir / RECORDS
                folder.mkdir(parents=True, exist_ok=True)
                for name, data in encoded.items():
                    path = self.run_dir / G.record_path(name, digests[name])
                    if str(path.relative_to(self.run_dir)) in publication.write:
                        self._step("record-write")
                        _write_durable(path, data)
                if publication.write:
                    self._step("records-fsync")
                    _fsync_dir(folder)
                body = dict(publication.generation.to_dict(), schema=G.SCHEMA_VERSION)
                self._step("manifest-replace")
                on_disk = read_manifest(self.run_dir)
                if (on_disk.digest if on_disk else None) != planned:
                    raise G.GenerationConflict("the manifest changed under this publication; another writer "
                                               "committed first, and nothing of this one is published")
                _write_durable(self.run_dir / MANIFEST, _canonical(body))
                self._step("manifest-fsync")
                _fsync_dir(self.run_dir)
            except StoreUnavailable:
                raise
            except BaseException as exc:
                self._refused = f"{type(exc).__name__}: {str(exc)[:200]}"
                raise StoreUnavailable(f"publication failed: {self._refused}") from exc
            if current is not None:
                self._history.append(current)
            self._current = publication.generation
            return publication.generation

    # -- reading ---------------------------------------------------------------

    def record(self, name: str):
        """One record of the manifest as committed on disk now."""
        manifest = self.current()
        if manifest is None or name not in manifest.records:
            return None
        return read_record(self.run_dir, name, manifest=manifest)


def _history(run_dir: Path, manifest: G.Generation) -> list:
    """Earlier committed generations are not kept on disk as manifests; the
    record files they named are, and their paths are content-addressed, so
    the immutability rule holds from the files alone. The in-memory history
    starts at the current generation."""
    return []


def read_manifest(run_dir) -> Optional[G.Generation]:
    """The committed manifest, or None when the run has none. A manifest
    that cannot be read or parsed is corrupt, never silently absent."""
    path = Path(run_dir) / MANIFEST
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text())
        stored = body.pop("digest", None)
        body.pop("paths", None)
        body.pop("clean", None)
        body["records"] = dict(body.get("records") or {})
        body["unsettled"] = tuple(body.get("unsettled") or ())
        manifest = G.Generation(**body)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise GenerationCorrupt(f"the manifest is unreadable: {type(exc).__name__}: {str(exc)[:160]}") from exc
    if stored != manifest.digest:
        # The stored digest is the manifest's own account of itself: a body
        # that no longer hashes to it was altered or truncated, and its
        # uncertainty fields are not to be believed.
        raise GenerationCorrupt("the manifest does not hash to its stored digest; it was altered or truncated")
    return manifest


def read_record(run_dir, name: str, *, manifest: Optional[G.Generation] = None):
    """One record named by the committed manifest, verified against the
    digest the manifest holds. None when the manifest names no such record;
    GenerationCorrupt when the bytes are missing or altered."""
    run_dir = Path(run_dir)
    manifest = manifest or read_manifest(run_dir)
    if manifest is None or name not in manifest.records:
        return None
    path = run_dir / G.record_path(name, manifest.records[name])
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GenerationCorrupt(f"record {name} named by generation {manifest.number} is missing") from exc
    if _digest_bytes(data) != manifest.records[name]:
        raise GenerationCorrupt(f"record {name} does not hash to what generation {manifest.number} names")
    return json.loads(data)


def read_budget(run_dir) -> Tuple[Optional[dict], Optional[str]]:
    """The budget record a reader may act on: the manifest's when a manifest
    exists (corrupt means ``(None, why)``, never the export), else the
    historical ``budget.json`` export. Returns ``(record, problem)``."""
    run_dir = Path(run_dir)
    try:
        manifest = read_manifest(run_dir)
    except GenerationCorrupt as exc:
        return None, str(exc)
    if manifest is not None:
        if "budget" not in manifest.records:
            return None, f"generation {manifest.number} names no budget record"
        try:
            record = read_record(run_dir, "budget", manifest=manifest)
        except GenerationCorrupt as exc:
            return None, str(exc)
        return (record, None) if isinstance(record, dict) else (None, "the budget record is not a record")
    path = run_dir / "budget.json"
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, f"budget.json: {type(exc).__name__}"
    return (record, None) if isinstance(record, dict) else (None, "budget.json is not a record")


def owner_of(opid: str) -> str:
    """The session owner id inside an operation id (``OP<OWNER>-<n>``)."""
    head = opid.split("-", 1)[0]
    return head[2:] if head.startswith("OP") else ""


def operation_records(run_dir, *, manifest: Optional[G.Generation] = None) -> Dict[str, list]:
    """Every session operations record the manifest names (``operations-
    <owner>``), each verified against its digest, by owner."""
    run_dir = Path(run_dir)
    manifest = manifest or read_manifest(run_dir)
    if manifest is None:
        return {}
    out = {}
    for name in manifest.records:
        if name.startswith("operations-"):
            rows = read_record(run_dir, name, manifest=manifest)
            if not isinstance(rows, list):
                raise GenerationCorrupt(f"record {name} is not a list of operations")
            out[name[len("operations-"):]] = rows
    return out


def unsettled_operations(run_dir) -> Tuple[str, ...]:
    """Operations the committed generation lists as started and unsettled,
    checked against the records that are supposed to prove it: the budget
    record's started calls and every owner's operations record must say the
    same, or the generation is corrupt and nothing is resumed on it."""
    manifest = read_manifest(run_dir)
    if manifest is None:
        return ()
    listed = set(manifest.unsettled)
    recorded = set()
    if "budget" in manifest.records:
        budget = read_record(Path(run_dir), "budget", manifest=manifest)
        started = ((budget or {}).get("operations") or {}).get("started") or []
        recorded |= {str(o.get("opid")) for o in started if isinstance(o, dict)}
    for rows in operation_records(run_dir, manifest=manifest).values():
        recorded |= {str(o.get("opid")) for o in rows
                     if isinstance(o, dict) and o.get("status") in ("started", "uncertain")}
    if listed != recorded:
        raise GenerationCorrupt("the manifest's unsettled operations do not match its records "
                                f"(manifest {sorted(listed)}, records {sorted(recorded)})")
    return tuple(sorted(listed))


def read_terminal(run_dir) -> Optional[dict]:
    """The committed terminal record of the run (what it ended as and on
    what acceptance basis), or None when the run never committed one. The
    only completion a reader may act on once a manifest exists."""
    record = read_record(run_dir, "terminal")
    return record if isinstance(record, dict) else None


def record_names(run_dir) -> Dict[str, str]:
    manifest = read_manifest(run_dir)
    return dict(manifest.records) if manifest is not None else {}
