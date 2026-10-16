"""One strict reading of a judgment, shared by every seat that judges.

Root reconciliation of the strategy reset (2026-10-16, #73): the reviewer,
the requirements auditor and the reassessment each combined repeated
per-requirement lines differently. The reviewer let NOT MET win in either
order, the audit kept the last line and the reassessment the first, so a
contradictory audit or reassessment reply could approve a requirement.

Every site now reads its own lines with its own grammar (the line patterns
are unchanged) and combines them here, with one rule:

- any NOT MET line for a requirement makes it NOT MET, in either order;
- two or more lines that all say MET are a duplicate, which approves
  nothing and is not a verdict;
- exactly one MET line is MET.

A recheck verdict is read the same strict way it always was (one standalone
RESOLVED line at the start or end, nothing contradicting it), and a reply
that is not RESOLVED is now told apart: an explicit UNRESOLVED (or a
BLOCKING line) is a genuine finding, while a reply with neither is
``INVALID`` -- no judgment at all, never a pass and never a finding. Nothing
here extracts a verdict from inside other text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

__all__ = ["MET", "NOT_MET", "DUPLICATE", "Judgment", "combine", "has_blocking",
           "RESOLVED", "UNRESOLVED", "INVALID", "recheck_state"]

MET, NOT_MET, DUPLICATE = "met", "not met", "duplicate"
RESOLVED, UNRESOLVED, INVALID = "resolved", "unresolved", "invalid"


@dataclass(frozen=True)
class Judgment:
    """One requirement's combined verdict: ``state`` is MET, NOT_MET or
    DUPLICATE; ``why`` is the text after the deciding line (the first NOT MET
    line, or the single MET line); ``lines`` counts the lines read."""

    state: str
    why: str
    lines: int

    @property
    def approved(self) -> bool:
        return self.state == MET


def combine(entries: Iterable[Tuple[str, bool, str]]) -> Dict[str, Judgment]:
    """``entries`` are (requirement id, met, why) in reply order."""
    grouped: Dict[str, list] = {}
    for rid, met, why in entries:
        grouped.setdefault(rid.upper(), []).append((bool(met), (why or "").strip()))
    out: Dict[str, Judgment] = {}
    for rid, lines in grouped.items():
        unmet = [why for met, why in lines if not met]
        if unmet:
            out[rid] = Judgment(NOT_MET, unmet[0], len(lines))
        elif len(lines) > 1:
            out[rid] = Judgment(DUPLICATE, f"{len(lines)} MET lines for {rid}; a duplicate approves nothing",
                                len(lines))
        else:
            out[rid] = Judgment(MET, lines[0][1], 1)
    return out


_BLOCKING_LINE = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?BLOCKING\s*:", re.IGNORECASE)


def has_blocking(text: str) -> bool:
    """Only a finding's explicit prefix, at the start of a line, counts."""
    return any(_BLOCKING_LINE.match(line.replace("**", "")) for line in (text or "").splitlines())


_UNRESOLVED = re.compile(r"\bUNRESOLVED\b|\bNOT\s+RESOLVED\b", re.IGNORECASE)


def recheck_state(text: str) -> str:
    """RESOLVED only for one standalone RESOLVED line at the start or end of
    the reply with nothing contradicting it (the rule ``_resolved_verdict``
    always applied). UNRESOLVED when the reply says UNRESOLVED or NOT
    RESOLVED anywhere, or raises a BLOCKING line: a genuine finding, never
    treated as a format gap. INVALID otherwise: narration with a verdict
    glued into it, a duplicate RESOLVED, an empty reply."""
    if has_blocking(text) or _UNRESOLVED.search(text or ""):
        return UNRESOLVED
    lines = [line.strip().upper() for line in (text or "").splitlines() if line.strip()]
    if lines and lines.count("RESOLVED") == 1 and "RESOLVED" in (lines[0], lines[-1]):
        return RESOLVED
    return INVALID
