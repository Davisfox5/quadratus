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
from typing import Dict, Iterable, Optional, Sequence, Tuple

__all__ = ["MET", "NOT_MET", "DUPLICATE", "Judgment", "combine", "has_blocking",
           "RESOLVED", "UNRESOLVED", "INVALID", "INVALIDATED", "recheck_state",
           "Verdict", "NewFinding", "RecheckReading", "recheck_verdicts", "checkable_reference",
           "sufficiency_verdicts"]

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


# --- per-finding rechecks (blueprint section 4) ------------------------------

INVALIDATED = "invalidated"
_ID_LINE = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?(?P<fid>[A-Z][A-Z0-9]*-?\d+)\s*:\s*"
                      r"(?P<state>RESOLVED|UNRESOLVED|NOT\s+RESOLVED|INVALIDATED)\b\s*[-:,]?\s*(?P<why>.*)$",
                      re.IGNORECASE)
_NEW_LINE = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?(?:NEW|BLOCKING)\s*:\s*(?P<claim>.+)$", re.IGNORECASE)
_NOTE_LINE = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?(?:NOTE|SUGGESTION)\s*:\s*(?P<text>.+)$", re.IGNORECASE)
_COVERAGE_LINE = re.compile(r"\s*COVERAGE\s*:\s*(?P<state>COMPLETE|PARTIAL)\b\s*[-:,]?\s*(?P<why>.*)$",
                            re.IGNORECASE)
_SUFFICIENCY_LINE = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])\s+)?(?P<oid>O-?\d+)\s*:\s*"
                               r"(?P<state>SUFFICIENT|INSUFFICIENT)\b\s*[-:,]?\s*(?P<why>.*)$", re.IGNORECASE)
#: What counts as a checkable reference in an INVALIDATED line: a
#: requirement, finding, obligation or receipt id, a test function, a file
#: path with an extension, or a quoted command. Prose alone is not evidence.
_CHECKABLE = re.compile(r"(?:\b[A-Z][A-Z0-9]*-?\d+\b)|(?:\btest_\w+)|(?:[\w./-]+\.[A-Za-z0-9]{1,6}\b)|(?:`[^`]+`)"
                        r"|(?:\brcpt-\w+)|(?:\bcheck \d+\b)|(?:\battempt \d+\b)")


def checkable_reference(text: str) -> bool:
    """Whether ``text`` names something the record can be checked against."""
    return bool(_CHECKABLE.search(text or ""))


@dataclass(frozen=True)
class Verdict:
    """One finding's reading: ``state`` is RESOLVED, UNRESOLVED, INVALIDATED
    or INVALID (no judgment the reader accepts); ``why`` says what the line
    said, or why it was not accepted."""

    state: str
    why: str


@dataclass(frozen=True)
class NewFinding:
    """A finding the judge raised that was not expected: the whole line,
    whose ``| basis:`` and ``| impact:`` segments the caller reads."""

    line: str


@dataclass(frozen=True)
class RecheckReading:
    """A recheck reply read per finding id. ``dispositions`` has one entry for
    every expected id; ``new`` are findings the judge raised that were not
    expected; ``foreign`` are ids it judged that were never asked; ``notes``
    are advisory lines that settle nothing; ``coverage`` is the reviewer's
    own statement of what it read: ("complete"|"partial"|"invalid", why), or
    None when it gave none. Two coverage lines contradict each other and
    approve nothing."""

    dispositions: Dict[str, Verdict]
    new: Tuple[NewFinding, ...]
    foreign: Tuple[str, ...]
    notes: Tuple[str, ...]
    coverage: Optional[Tuple[str, str]]

    @property
    def unjudged(self) -> Tuple[str, ...]:
        return tuple(fid for fid, v in self.dispositions.items() if v.state == INVALID)

    @property
    def judged_any(self) -> bool:
        """Whether the reply judged at least one expected finding."""
        return any(v.state != INVALID for v in self.dispositions.values())

    @property
    def coverage_complete(self) -> bool:
        return self.coverage is not None and self.coverage[0] == "complete"


def recheck_verdicts(text: str, expected: Sequence[str]) -> RecheckReading:
    """Read a recheck reply one finding at a time.

    Every expected id gets exactly one verdict. A line ``F3: RESOLVED why``
    at the start of a line is the only form read; a marker inside prose is
    narration. Any UNRESOLVED (or NOT RESOLVED) line for an id makes it
    UNRESOLVED, whatever else was said. Otherwise exactly one RESOLVED line
    resolves, exactly one INVALIDATED line naming checkable evidence
    invalidates, and a missing, duplicated, contradictory or evidence-less
    line is INVALID: no judgment, never a pass. With exactly one expected
    finding, the old standalone form (one bare RESOLVED or UNRESOLVED line,
    ``recheck_state``) is read for it too. A ``NEW:`` or ``BLOCKING:`` line
    is a new finding; a ``NOTE:`` or ``SUGGESTION:`` line settles nothing;
    an id that was never expected is reported as foreign and applied to
    nothing.
    """
    wanted = {fid.upper(): fid for fid in expected}
    lines: Dict[str, list] = {}
    new, foreign, notes, coverages = [], [], [], []
    for raw in (text or "").splitlines():
        line = raw.replace("**", "").strip()
        if not line:
            continue
        hit = _ID_LINE.match(line)
        if hit:
            fid = hit.group("fid").upper()
            state = re.sub(r"\s+", " ", hit.group("state").upper())
            state = "UNRESOLVED" if state == "NOT RESOLVED" else state
            if fid in wanted:
                lines.setdefault(fid, []).append((state, hit.group("why").strip()))
            elif not _SUFFICIENCY_LINE.match(line):
                foreign.append(hit.group("fid"))
            continue
        hit = _NEW_LINE.match(line)
        if hit:
            claim = hit.group("claim").strip()
            if claim and not re.match(r"(?i)(none|n/?a|nothing)\b", claim):
                new.append(NewFinding(claim))
            continue
        hit = _NOTE_LINE.match(line)
        if hit:
            notes.append(hit.group("text").strip())
            continue
        hit = _COVERAGE_LINE.match(line)
        if hit:
            coverages.append((hit.group("state").lower(), hit.group("why").strip()))
    if len(wanted) == 1 and not lines:
        # The single-finding form: one standalone verdict line for the one
        # finding asked about, read by the rule recheck_state always applied.
        (key, fid), = wanted.items()
        bare = recheck_state(text)
        if bare == RESOLVED:
            lines[key] = [("RESOLVED", "")]
        elif bare == UNRESOLVED:
            why = next((ln.split(":", 1)[1].strip() for ln in (text or "").splitlines()
                        if _UNRESOLVED.search(ln) and ":" in ln), "")
            lines[key] = [("UNRESOLVED", why)]
    dispositions: Dict[str, Verdict] = {}
    for key, fid in wanted.items():
        read = lines.get(key, [])
        unresolved = [why for state, why in read if state == "UNRESOLVED"]
        if unresolved:
            dispositions[fid] = Verdict(UNRESOLVED, unresolved[0])
        elif not read:
            dispositions[fid] = Verdict(INVALID, "no line judged it")
        elif len(read) > 1:
            dispositions[fid] = Verdict(INVALID, f"{len(read)} lines judged it; a duplicate settles nothing")
        elif read[0][0] == "RESOLVED":
            dispositions[fid] = Verdict(RESOLVED, read[0][1])
        elif checkable_reference(read[0][1]):
            dispositions[fid] = Verdict(INVALIDATED, read[0][1])
        else:
            dispositions[fid] = Verdict(INVALID, "INVALIDATED without checkable evidence (a test, file, "
                                                 "receipt, command or id)")
    if not coverages:
        coverage = None
    elif len(coverages) == 1:
        coverage = coverages[0]
    else:
        coverage = ("invalid", f"{len(coverages)} COVERAGE lines; a contradiction approves nothing")
    return RecheckReading(dispositions, tuple(new), tuple(foreign), tuple(notes), coverage)


def sufficiency_verdicts(text: str, expected: Sequence[str]) -> Dict[str, Tuple[str, str]]:
    """``O3: SUFFICIENT why`` / ``O3: INSUFFICIENT why`` lines for the
    expected obligation ids: exactly one line per id counts; a duplicate or
    a contradiction is no verdict and the id is absent from the result."""
    wanted = {oid.upper(): oid for oid in expected}
    seen: Dict[str, list] = {}
    for raw in (text or "").splitlines():
        hit = _SUFFICIENCY_LINE.match(raw.replace("**", "").strip())
        if hit and hit.group("oid").upper() in wanted:
            seen.setdefault(hit.group("oid").upper(), []).append((hit.group("state").lower(),
                                                                   hit.group("why").strip()))
    return {wanted[key]: rows[0] for key, rows in seen.items() if len(rows) == 1}
