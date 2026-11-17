"""Immutable record generations and the one manifest that names them (pure).

Blueprint section 8 (2026-10-17): keep file artifacts and the existing
single-project coordinator lock; publish immutable record generations, then
one atomic current-generation manifest naming their digests and schema
version. This module is the validation half of that boundary: what a
publication must look like to be committed, what can never be overwritten,
when an expected generation is stale, and when falling back to an older
generation is forbidden. It writes nothing. The durable half (fsync, the
rename, the lock, the readers) is root's wiring, and the rules here are the
ones it must call before and after each step.

Rules:

- A record is content-addressed: its published name carries its digest, so
  the same name never holds two contents. A publication that would give a
  name already committed a different digest is refused.
- A generation names its parent; a publication carries the generation number
  it expects to succeed, and a different current number is a conflict, never
  a merge. Atomic rename alone is not compare-and-swap; the caller holds the
  coordinator lock across the check and the rename.
- A generation with a started operation that did not settle, or spend that
  was reserved and not finished, is not a safe fallback target for anything
  earlier: an older generation would omit a started action or spent budget.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Mapping, Optional, Sequence, Tuple

__all__ = ["SCHEMA_VERSION", "GenerationConflict", "ImmutableRecordViolation", "Generation",
           "Publication", "record_path", "plan_publication", "fallback_allowed"]

SCHEMA_VERSION = "delivery-records/1"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class GenerationConflict(RuntimeError):
    """The current generation is not the one the publisher expected."""


class ImmutableRecordViolation(RuntimeError):
    """A publication would give a committed record name different bytes."""


def record_path(name: str, digest: str) -> str:
    """Where a record of this name and content lives, forever."""
    if not _NAME.match(name or ""):
        raise ValueError(f"record name {name!r} is not a plain lowercase name")
    if not _DIGEST.match(digest or ""):
        raise ValueError(f"{name}: digest must be 64 hex characters")
    return f"records/{name}-{digest[:16]}.json"


@dataclass(frozen=True)
class Generation:
    """One committed state: ``records`` maps each record name to the digest
    of its bytes; ``unsettled`` names operations started and not settled in
    this generation; ``reserved_unfinished`` is budget reserved for attempts
    that had not finished when it was committed."""

    number: int
    schema: str
    records: Mapping[str, str]
    parent: Optional[int] = None
    unsettled: Tuple[str, ...] = ()
    reserved_unfinished: int = 0

    def __post_init__(self):
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number < 1:
            raise ValueError("generation number must be a positive integer")
        if self.schema != SCHEMA_VERSION:
            raise ValueError(f"schema {self.schema!r} is not {SCHEMA_VERSION!r}")
        if self.parent is not None and self.parent != self.number - 1:
            raise ValueError(f"generation {self.number} names parent {self.parent}; generations are consecutive")
        if self.number > 1 and self.parent is None:
            raise ValueError(f"generation {self.number} names no parent")
        for name, digest in self.records.items():
            record_path(name, digest)
        if self.reserved_unfinished < 0:
            raise ValueError("reserved_unfinished cannot be negative")

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @property
    def paths(self) -> Dict[str, str]:
        return {name: record_path(name, digest) for name, digest in self.records.items()}

    @property
    def clean(self) -> bool:
        """Nothing started and unsettled, nothing reserved and unfinished."""
        return not self.unsettled and self.reserved_unfinished == 0

    def to_dict(self) -> dict:
        return dict(asdict(self), records=dict(self.records), digest=self.digest)


@dataclass(frozen=True)
class Publication:
    """What the publisher writes, in order: every path in ``write`` (records
    whose bytes are not yet on disk), durably, then the manifest for
    ``generation``. Paths in ``keep`` are already committed and untouched."""

    generation: Generation
    write: Tuple[str, ...]
    keep: Tuple[str, ...] = field(default_factory=tuple)


def plan_publication(current: Optional[Generation], *, expected: Optional[int], records: Mapping[str, str],
                     history: Sequence[Generation] = (), unsettled: Sequence[str] = (),
                     reserved_unfinished: int = 0) -> Publication:
    """Validate one publication against the current generation and every
    committed one.

    ``expected`` is the generation number the publisher read before deciding
    what to write (None when it expects no generation yet). It must equal the
    current number, or the publisher's decision was made on stale state and
    nothing is written. ``records`` is the full record set of the new
    generation, name to digest: an unchanged record keeps its committed path,
    a changed one gets a new path, and no committed path is ever reused for
    other bytes.
    """
    current_number = current.number if current else None
    if expected != current_number:
        raise GenerationConflict(f"expected generation {expected}, current is {current_number}; "
                                 "decide again on the current generation")
    committed: Dict[str, str] = {}
    for old in list(history) + ([current] if current else []):
        for name, digest in old.records.items():
            path = record_path(name, digest)
            if committed.get(path, digest) != digest:
                raise ImmutableRecordViolation(f"history already holds {path} with two digests")
            committed[path] = digest
    write, keep = [], []
    for name, digest in records.items():
        path = record_path(name, digest)
        if path in committed:
            if committed[path] != digest:
                raise ImmutableRecordViolation(f"{path} is committed with other bytes; a record is never rewritten")
            keep.append(path)
        else:
            write.append(path)
    generation = Generation(number=(current_number or 0) + 1, schema=SCHEMA_VERSION, records=dict(records),
                            parent=current_number, unsettled=tuple(unsettled),
                            reserved_unfinished=int(reserved_unfinished))
    return Publication(generation, tuple(write), tuple(keep))


def fallback_allowed(current: Generation, target: Generation) -> Tuple[bool, str]:
    """Whether a reader may act on ``target`` instead of ``current``. Never
    when the current generation holds a started operation or unfinished
    reservation the target does not know about: that fallback would forget
    an effect or spend. Equal generations are always allowed."""
    if target.number == current.number:
        return True, "same generation"
    if target.number > current.number:
        return False, f"generation {target.number} is not committed; current is {current.number}"
    if not current.clean:
        return False, (f"generation {current.number} holds unsettled operations {list(current.unsettled)} "
                       f"and {current.reserved_unfinished:,} reserved tokens; an older generation would forget them")
    return False, "an older generation is history, not a fallback; reconcile on the current one"
