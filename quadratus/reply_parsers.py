"""How the session engine reads what a model or the operator wrote: capture
declarations, fixture blocks, the directive lines of a task (COVERS,
RESOLVES, TIER, CONTINUES, HYPOTHESIS), requirements, labels and metadata,
FETCH requests and the close-out.

Moved out of session.py unchanged on 2026-11-11; quadratus.session imports
every name here again. Verdicts are read in verdicts.py. The CONSULT
readers stay in session.py, because a test replaces ``_member_matches`` on
that module and they must see the replacement.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Dict, List, Optional

from .session_types import Complexity, _needs_lines, _scope_path
from .task_kinds import ROUTING, TaskKind
from .taskmeta import AmbiguousMetadata, TaskMetadata, lead_request, parse_metadata
from .verdicts import is_empty_note as _is_empty_note
from .verdicts import unstyled as _unstyled

#: How many facts one decomposition may add, and how long each may be. The map
#: is re-sent on every orchestrator and lead call for the rest of the run, so
#: an unbounded one would replace a one-off exploration cost with a permanent
#: per-call one -- which is the trade this whole change exists to avoid.
_MAX_ORIENT_NOTES = 4
_MAX_ORIENT_NOTE_CHARS = 180

_CAPTURE_LINE = re.compile(r"^\s*CAPTURE\s*:\s*(.*\S)\s*$", re.IGNORECASE)
#: The marker wherever it sits on a line: series rule-3572b72 f5's lead wrote
#: prose and then ``load.CAPTURE: {"path": "/", ...}`` on the same line, and a
#: line-anchored read saw no declaration at all. Any case (the old contract),
#: and a marker is a candidate only when something declarable follows it.
_CAPTURE_MARKER = re.compile(r"CAPTURE\s*:", re.IGNORECASE)


def _capture_candidates(reply: str):
    """``(declarations, conflicts)`` across the whole reply. A declaration
    is marker-backed: a ``CAPTURE:`` marker (any case) followed by ``{`` or
    ``none``. Everything else that looks like an attempt is a conflict and
    never a declaration: a marker with any other tail or no tail at all
    (``Correction.capture: invalid``, ``Correction.CAPTURE:``), and a line
    outside the marker lines that starts with ``{`` (a second object, an
    unmarked example, an incomplete ``{"path":``). The harness reads exactly
    one declaration and only when nothing conflicts with it (Codex reviews
    of 351d3ba, 6844b97 and ada4c75)."""
    text = (reply or "").replace("`", "")
    declarations: List[str] = []
    conflicts: List[str] = []
    lines = text.splitlines()
    marker_lines = set()
    for index, line in enumerate(lines):
        for match in _CAPTURE_MARKER.finditer(line):
            marker_lines.add(index)
            tail = line[match.end():].strip()
            if tail.startswith("{") or re.match(r"none\b", tail, re.IGNORECASE):
                declarations.append(tail)
            else:
                conflicts.append(f"marker with no declaration after it: {line.strip()[:60]}")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index not in marker_lines and stripped.startswith("{"):
            conflicts.append(f"an object outside any CAPTURE: marker: {stripped[:60]}")
    return declarations, conflicts


def _capture_declaration(reply: str):
    """``(tail, "")`` for the one declaration candidate in ``reply``, or
    ``(None, why)``."""
    declarations, conflicts = _capture_candidates(reply)
    if not declarations and not conflicts:
        return None, "no CAPTURE: line in the reply"
    if len(declarations) + len(conflicts) > 1:
        return None, (f"{len(declarations)} capture declaration(s) and {len(conflicts)} conflicting line(s) "
                      "in the reply; one declaration is read, never a choice")
    if not declarations:
        return None, f"no usable CAPTURE: declaration ({conflicts[0]})"
    return declarations[0], ""


def _parse_capture_line(reply: str, task_id: str):
    """``(capture, "")`` from a CAPTURE: line, or ``(None, why)``.

    The same syntax as a SCOPE capture (``preview.validate_capture``), and a
    capture-only fixture must belong to this task. ``CAPTURE: none`` and a
    missing or malformed line both return None with the reason; the caller
    keeps the reviewer's verdict in that case.
    """
    line, why = _capture_declaration(reply)
    if line is None:
        return None, why
    if re.match(r"none\b", line, re.IGNORECASE):
        return None, "the lead declared that no page and steps reach the changed state"
    try:
        from .preview import validate_capture
        obj, end = json.JSONDecoder().raw_decode(line)
        trailing = line[end:].strip()
        if trailing and ("{" in trailing or "}" in trailing or "[" in trailing):
            return None, "more than one object after the CAPTURE: marker"
        capture = validate_capture(obj)
    except (ValueError, TypeError) as exc:
        return None, f"the CAPTURE line did not parse ({str(exc)[:120]})"
    for step in capture["steps"]:
        if step.get("action") == "file":
            parts = PurePosixPath(step["path"]).parts
            if parts[:2] == (".quadratus", "capture-fixtures") and (len(parts) < 3 or parts[2] != task_id):
                return None, f"a capture-only fixture must live under .quadratus/capture-fixtures/{task_id}/"
    return capture, ""


#: A well-formed header: the path alone, with or without its colon.
_FIXTURE_HEAD = re.compile(r"[ \t]*FIXTURE[ \t]+(\S+?):?[ \t]*")
#: A fence line: a run of three or more backticks, then anything (the
#: opener may name a language). A closing fence is the run alone.
_FIXTURE_FENCE = re.compile(r"[ \t]*(`{3,})([^\n]*)")


#: Any line that starts like a header, well-formed or not (Codex review of
#: ada4c75: ``FIXTURE a.csv: corrected content follows`` after a valid block
#: escaped the duplicate accounting). The path is the first token; an
#: empty one is a malformed header.
_FIXTURE_HEADER = re.compile(r"^[ \t]*FIXTURE\b[ \t]*(\S*)", re.MULTILINE)


def _fixture_body(lines):
    """``(content, "")`` for the one fenced block opening ``lines`` (what
    follows a header, up to the next header or the end of the reply), else
    ``(None, why)``; ``why`` is "unclosed" when a fence opened and never
    closed.

    The block must end at the last fence in that stretch. A closing fence is
    a line of backticks alone, at least as long as the opening run, so a
    sample that itself holds a line of three backticks is fenced with four.
    When more than one line could close the block the content has two
    readings; the first used to be taken in silence, cutting such a sample
    short and writing the stump as if it were whole."""
    opener = _FIXTURE_FENCE.fullmatch(lines[0]) if lines else None
    if opener is None:
        return None, "no fence"
    run = len(opener.group(1))
    closers = [index for index, line in enumerate(lines[1:], 1)
               if (fence := _FIXTURE_FENCE.fullmatch(line)) and len(fence.group(1)) >= run
               and not fence.group(2).strip()]
    if not closers:
        return None, "unclosed"
    if len(closers) > 1:
        return None, "ambiguous"
    return "\n".join(lines[1:closers[0]]), ""


def _parse_fixture_blocks(reply: str):
    """``({path: content}, problems)`` for every ``FIXTURE <path>:`` line
    followed by one fenced block. The path is taken as written; the caller
    matches it against the declared fixtures, so an unexpected path is
    ignored, never written. Every header is accounted for, complete or not:
    a path with more than one header, or a header with no complete block
    (an unclosed or missing fence), is a problem for that path and nothing
    is kept for it; so is a block that more than one fence line could close
    (see :func:`_fixture_body`). A block whose content holds another header
    (an unclosed fence swallowed the next block) makes the whole reply
    malformed (Codex reviews of 351d3ba and 6844b97)."""
    lines = (reply or "").splitlines()
    starts = [index for index, line in enumerate(lines) if _FIXTURE_HEADER.match(line)]
    headers: Dict[str, int] = {}
    blocks: Dict[str, List[str]] = {}
    unclear: set = set()
    for position, start in enumerate(starts):
        path = _FIXTURE_HEADER.match(lines[start]).group(1).strip().rstrip(":").strip("`'\"")
        if not path:
            return {}, ["malformed reply: a FIXTURE header names no path"]
        headers[path] = headers.get(path, 0) + 1
        last = position == len(starts) - 1
        body, why = _fixture_body(lines[start + 1:len(lines) if last else starts[position + 1]])
        if not _FIXTURE_HEAD.fullmatch(lines[start]):
            continue    # a header with words after its path opens no block
        if why == "unclosed" and not last:
            return {}, ["malformed reply: a fenced block holds another FIXTURE header (an unclosed fence)"]
        if why == "ambiguous":
            unclear.add(path)
        elif body is not None:
            blocks.setdefault(path, []).append(body)
    found: Dict[str, str] = {}
    problems: List[str] = []
    for path in sorted(headers):
        count, bodies = headers[path], blocks.get(path, [])
        if count > 1:
            problems.append(f"{path}: more than one FIXTURE header for the same path")
        elif path in unclear:
            problems.append(f"{path}: more than one fence line could close the block, so its content has two "
                            "readings and nothing is written (fence a sample that holds a line of backticks "
                            "with a longer run)")
        elif len(bodies) == 1:
            found[path] = bodies[0]
        else:
            problems.append(f"{path}: a FIXTURE header with no complete fenced block")
    return found, problems


_DECIDE = re.compile(r"^\s*DECIDE:\s*(R\d+)\s*[-:—]\s*(.+?)\s*$", re.MULTILINE)
_REQ_BLOCK = re.compile(r"^\s*REQUIREMENTS:\s*\n((?:\s*R\d+\s*[:.)-].*\n?)+)", re.MULTILINE)
_REQ_LINE = re.compile(r"^\s*(R\d+)\s*[:.)-]\s*(.+?)\s*$", re.MULTILINE)
_COVERS = re.compile(r"^\s*COVERS:\s*(.+?)\s*$", re.MULTILINE)
#: An editing call's own account of acceptance it could not run (runtime
#: check guidance). Read only to lower an audit verdict, never to raise one.
_NOT_RUN_LINE = re.compile(r"^\s*(?:NOT RUN|BLOCKED):\s*(.+?)\s*$", re.MULTILINE)


def _read_requirements(reply: str):
    """Split a REQUIREMENTS block off an orchestrator reply."""
    match = _REQ_BLOCK.search(reply or "")
    if not match:
        return {}, reply
    found = {rid: text for rid, text in _REQ_LINE.findall(match.group(1))}
    rest = (reply[:match.start()] + reply[match.end():]).strip()
    return found, rest


def _covers_ids(text) -> List[str]:
    """The requirement ids a task text claims: every id on every ``COVERS:``
    line, in the order written, each once.

    The one reading of a COVERS declaration. The task's own record
    (``_read_covers``) and an operator list's claims (``Session.run``) both
    come from here: read apart, the record said a task covered R2 while the
    run said nobody claimed it, and the run completed with R2 never audited
    (review 1110, engine 2)."""
    return list(dict.fromkeys(rid for line in _COVERS.findall(text or "") for rid in re.findall(r"R\d+", line)))


def _read_covers(spec):
    """Split the ``COVERS: R1, R3`` lines off a task.

    More than one line is one declaration: every id, in the order written,
    each once. The first line used to be read and all of them deleted, so
    "COVERS: R1" and a later "COVERS: R4" dropped R4 without a word."""
    if not _COVERS.search(spec.description or ""):
        return [], spec
    ids = _covers_ids(spec.description)
    description = _COVERS.sub("", spec.description).strip()
    return ids, replace(spec, description=description or spec.description)


_RESOLVES = re.compile(r"^\s*RESOLVES:(.*)$", re.MULTILINE)


def _resolves_tokens(line: str) -> tuple:
    return tuple(t for t in re.split(r"[\s,]+", line.strip()) if t)


def _read_resolves(spec):
    """Split a ``RESOLVES: F1, F2`` line off a task. None when absent; the
    raw tokens otherwise, so an empty or malformed line can be refused.

    A line repeated word for word is one line. Lines that differ are all
    returned, never only the first: the dispatch loop refuses a RESOLVES on
    more than one line, and nothing is dropped unread before it does."""
    found = _RESOLVES.findall(spec.description or "")
    if not found:
        return None, spec
    tokens = [t for line in dict.fromkeys(_resolves_tokens(line) for line in found) for t in line]
    description = _RESOLVES.sub("", spec.description).strip()
    return tokens, replace(spec, description=description or spec.description)


_PARALLEL_HEAD = re.compile(r"^\s*PARALLEL\s*$", re.MULTILINE)


def _parallel_blocks(reply: str):
    """The task blocks of a 'PARALLEL' reply, or None for a single task."""
    match = _PARALLEL_HEAD.search(reply or "")
    if not match:
        return None
    blocks = [b.strip() for b in re.split(r"^\s*---\s*$", reply[match.end():], flags=re.MULTILINE)]
    blocks = [b for b in blocks if b]
    return blocks if len(blocks) >= 2 else ([blocks[0]] if blocks else None)


def _cited_path(token) -> str:
    """A path as an auditor wrote it, in the form project paths are compared
    in: a leading "./" or "/" dropped as a prefix, then ``_scope_path``. ""
    for one that names nothing inside the project (empty, or a ".." part).

    ``lstrip("./")`` strips characters, not a prefix, so a citation of
    ".storybook/main.js" was looked up as "storybook/main.js": never found,
    or found as another file that carried that name (review 1110, engine 4)."""
    text = str(token)
    while text.startswith(("./", "/")):
        text = text[1:] if text.startswith("/") else text[2:]
    path = _scope_path(text)
    return "" if path in ("", ".") or ".." in path.split("/") else path


_TIER = re.compile(r"^\s*TIER:\s*(direct|normal)\s*$", re.MULTILINE | re.IGNORECASE)


def _read_tier(spec):
    """Split a ``TIER: direct`` line off a task. Returns (tier, spec).

    Two TIER lines that disagree are sent back before a task is built (see
    :func:`_read_metadata`). Read here anyway, they ask for the normal tier:
    the label that drops the review never wins by being written first."""
    values = list(dict.fromkeys(v.lower() for v in _TIER.findall(spec.description or "")))
    if not values:
        return "normal", spec
    description = _TIER.sub("", spec.description).strip()
    return (values[0] if len(values) == 1 else "normal"), replace(spec, description=description or spec.description)


_CONTINUES = re.compile(r"^\s*CONTINUES:\s*(\S+)\s*$", re.MULTILINE)


def _read_continues(spec):
    """Split a ``CONTINUES: <task id>`` line off a task, naming the capped
    task it finishes. Returns (task id or None, spec without the line).

    Two CONTINUES lines naming different tasks are sent back before a task
    is built (see :func:`_read_metadata`). Read here anyway, they name no
    task: neither capped task's debt is discharged by a guess."""
    values = list(dict.fromkeys(_CONTINUES.findall(spec.description or "")))
    if not values:
        return None, spec
    description = _CONTINUES.sub("", spec.description).strip()
    return (values[0] if len(values) == 1 else None), replace(spec, description=description or spec.description)


_HYPOTHESIS = re.compile(r"^\s*HYPOTHESIS:\s*(.+?)\s*$", re.MULTILINE)


def _read_hypothesis(spec):
    """Split a ``HYPOTHESIS: <text>`` line off a task (survey runs): what
    the orchestrator inferred from the last failure and what it changes.
    Returns (text or None, spec without the line)."""
    found = list(dict.fromkeys(_HYPOTHESIS.findall(spec.description or "")))
    if not found:
        return None, spec
    description = _HYPOTHESIS.sub("", spec.description).strip()
    # Diagnostic text: more than one line is kept whole, never only the first.
    return " / ".join(found), replace(spec, description=description or spec.description)


def _read_task_orientation(description: str):
    """Split ``MAP NOTES:`` lines out of a task description.

    Returns ``(notes, remaining_description)``. The notes are removed from the
    description because they are not part of the task: leaving them in would
    put them through the scope lint and into the task text the lead is told to
    satisfy verbatim.

    Malformed notes are dropped rather than guessed at, exactly as in a
    close-out: the map is long-lived and re-sent on every call, so a wrong note
    there is worse than no note. Bounded in count and length for the same
    reason -- this exists to remove a cost, not to relocate it.
    """
    lines, notes, collecting = [], [], False
    for line in description.splitlines():
        match = re.match(r"\s*MAP\s+NOTES\s*:(.*)$", line, re.IGNORECASE)
        if match is not None:
            collecting = True
            rest = match.group(1).strip()
            if rest:
                notes.append(rest)
            continue
        stripped = line.strip()
        if collecting:
            # The section runs until a blank line or a line that is plainly
            # not one of its entries, so a description continuing underneath
            # is not swallowed.
            if stripped.startswith(("-", "•", "*")) or (stripped and ":" in stripped
                                                        and len(stripped) <= 300
                                                        and not stripped.endswith(".")):
                notes.append(stripped)
                continue
            collecting = False
        lines.append(line)
    parsed = []
    for raw in notes:
        raw = raw.lstrip("-•* ").strip()
        topic, _, note = raw.partition(":")
        if topic.strip() and note.strip():
            parsed.append((topic.strip()[:40], note.strip()[:_MAX_ORIENT_NOTE_CHARS]))
    return parsed[:_MAX_ORIENT_NOTES], "\n".join(lines).strip()


def _settle_directives(description: str):
    """``(description, problem)``: a task's directive lines given one reading
    before any reader sees them.

    A RESOLVES, CONTINUES or TIER line repeated word for word is one line,
    and the repeat is dropped. Two TIER lines, or two CONTINUES lines, that
    disagree have no single reading, and a NEEDS line that cannot be read
    used to end the run: each is a problem for the caller to send back. A
    RESOLVES on two lines that differ is left for the dispatch loop, which
    has always refused it. Two NEEDS lines that disagree are not a problem
    here: the prompt says they are refused with no correction."""
    kept, seen, dropped = [], {}, False
    for line in (description or "").splitlines():
        repeat = False
        for name, pattern, key in (("TIER", _TIER, str.lower), ("CONTINUES", _CONTINUES, str),
                                   ("RESOLVES", _RESOLVES, _resolves_tokens)):
            match = pattern.match(line)
            if match is None:
                continue
            value = key(match.group(1))
            values = seen.setdefault(name, [])
            if value in values:
                repeat = True
            else:
                values.append(value)
        if repeat:
            dropped = True
        else:
            kept.append(line)
    if len(seen.get("TIER", ())) > 1:
        return description, ("the task carries more than one TIER line and they disagree ("
                             + ", ".join(seen["TIER"]) + "); state one")
    if len(seen.get("CONTINUES", ())) > 1:
        return description, ("the task carries more than one CONTINUES line naming different tasks ("
                             + ", ".join(seen["CONTINUES"]) + "); a task continues one task, so name "
                             "the other in a task of its own")
    problem = _needs_lines(description)[2]
    if problem:
        return description, f"invalid task requirements: {problem}"
    return ("\n".join(kept).strip() if dropped else description), ""


def _parse_fetch(reply: str) -> Optional[str]:
    """An artifact request: the whole reply is ``FETCH: <artifact-id>``.

    Deliberately strict -- only a reply that *is* a fetch request counts, so a
    draft that merely mentions the word FETCH in code or prose is never
    mistaken for one.
    """
    stripped = _parse_kind(reply)[2].strip()
    stripped = lead_request(stripped) or stripped
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) == 1 and lines[0].upper().startswith("FETCH:"):
        wanted = lines[0].split(":", 1)[1].strip()
        return wanted or None
    return None


def _read_labels(reply: str) -> TaskMetadata:
    """Kind, difficulty and description as :mod:`quadratus.taskmeta` reads
    them, with nothing checked beyond the labels."""
    return parse_metadata(
        reply,
        known_kinds=set(ROUTING),
        known_difficulties=set(Complexity._COLLABORATORS),
        default_kind=TaskKind.GENERAL,
        default_difficulty=Complexity.SIMPLE,
    )


def _read_metadata(reply: str) -> TaskMetadata:
    """Recover kind, difficulty and description, tolerating a bounded preface.

    Delegates to :mod:`quadratus.taskmeta`, which scans a few lines rather
    than only the first. The old first-line-only rule is what let a prefaced
    ``KIND: test rote`` fall through to general/simple, dropping the testing
    pin without a word in the record; see that module for the full account.

    The task's directive lines are settled here too (:func:`_settle_directives`),
    so every path that names a task sends the same problems back the same
    way: TIER or CONTINUES lines that disagree, and a NEEDS line that cannot
    be read.

    Raises:
        AmbiguousMetadata: on contradictory or unknown labels, or directive
            lines with no single reading. The caller re-asks once with a
            correction rather than defaulting, because the default is
            precisely the silent misroute being fixed.
    """
    meta = _read_labels(reply)
    description, problem = _settle_directives(meta.description)
    if problem:
        raise AmbiguousMetadata(problem)
    return meta if description == meta.description else replace(meta, description=description)


def _parse_kind(reply: str) -> tuple:
    """(kind, difficulty, description), degrading rather than raising.

    The tolerant face of :func:`_read_metadata`, kept for the callers that
    only want the description and have no way to re-ask -- an ambiguous label
    there is still better handled as the documented default than as a crash.
    Callers that *can* re-ask use :func:`_read_metadata` directly.
    """
    try:
        # The labels only: a lead's draft is read through here, and its lines
        # are not a task's directives.
        meta = _read_labels(reply)
    except AmbiguousMetadata:
        return TaskKind.GENERAL, Complexity.SIMPLE, (reply or "").strip()
    return meta.kind, meta.difficulty, meta.description


#: A close-out section header: the name, then a colon and the first of its
#: text, or the name alone where markdown marks it as a heading.
_CLOSEOUT_HEADER = re.compile(r"(SUMMARY|DECISIONS|REASONING|DEAD\s+ENDS|MAP\s+NOTES)\s*(?::(.*))?", re.IGNORECASE)
#: What markdown puts around such a header: stars, backticks, a heading mark.
#: A list bullet is not removed: a bulleted line is an entry, as it always was.
_CLOSEOUT_STYLING = re.compile(r"[*`]+|^#{1,6}\s+")
#: "No dead ends." and the like: the section's way of saying it is empty.
_NO_DEAD_ENDS = re.compile(r"no\s+dead\s+ends?(?:\s+(?:were\s+|was\s+)?(?:recorded|found|noted|hit|encountered))?"
                           r"[\s.!]*", re.IGNORECASE)


def _map_entry(line: str) -> Optional[tuple]:
    """``(topic, text)`` for a 'topic: text' line, bulleted or not; None for
    anything else."""
    topic, colon, note = line.lstrip("-\u2022* ").partition(":")
    topic, note = topic.strip(" *_`"), note.strip()
    return (topic, note) if colon and topic and note else None


def _parse_closeout(reply: str):
    """Split a close-out into summary, reasoning, dead ends and map notes.

    Tolerant by design: a missing section degrades to a usable record rather
    than failing the task, since the raw work is stored either way. Reasoning
    falls back to the summary because the ledger refuses an empty one, and a
    weak reason recorded honestly beats a lost task. Map notes that do not
    parse as 'topic: fact' are dropped rather than guessed at -- the map is
    long-lived, so a malformed note is worse there than nowhere.
    """
    sections: Dict[str, List[str]] = {
        "SUMMARY": [], "DECISIONS": [], "REASONING": [], "DEAD ENDS": [], "MAP NOTES": [],
    }
    current = "SUMMARY"
    notes_open = True
    for line in (reply or "").splitlines():
        stripped = line.strip()
        # A header is read as written, or failing that without its markdown:
        # "**SUMMARY:**" and "## Dead ends" used to be body text, which put
        # the whole reply in the summary and the reasoning and dropped its
        # map notes.
        plain = _CLOSEOUT_STYLING.sub("", stripped).strip()
        header = _CLOSEOUT_HEADER.fullmatch(stripped) or _CLOSEOUT_HEADER.fullmatch(plain)
        if header and header.group(2) is None and plain == stripped:
            # The bare word with no colon and no markdown is text, as it
            # always was: a wrapped sentence can leave "summary" or "dead
            # ends" alone on a line, and the lines after it would be filed
            # under that section (as dead-end lessons, at worst).
            header = None
        if header:
            current = " ".join(header.group(1).upper().split())
            notes_open = True
            stripped = (header.group(2) or "").strip()      # what follows the colon opens the section
        if not stripped:
            continue
        if current == "MAP NOTES":
            # The section is its entries and nothing after them: it ends at
            # the first line that is not 'topic: text', so a sentence that
            # follows the notes (and happens to hold a colon further on) is
            # never stored as one.
            notes_open = notes_open and _map_entry(stripped) is not None
            if not notes_open:
                continue
        sections[current].append(stripped)

    summary = " ".join(sections["SUMMARY"]).strip() or (reply or "").strip() or "(no summary)"
    # DECISIONS replaced REASONING in the prompt; an older-style reply still parses.
    reasoning = " ".join(sections["DECISIONS"] + sections["REASONING"]).strip() or summary
    # The prompt says "or none": that answer is not a lesson. It was stored
    # as the dead end "none" and rendered in every later orchestrator prompt.
    dead_ends = [d for d in (d.lstrip("-•* ").strip() for d in sections["DEAD ENDS"])
                 if d and not _NO_DEAD_ENDS.fullmatch(_unstyled(d)) and not _is_empty_note(d)]
    map_notes = [entry for entry in map(_map_entry, sections["MAP NOTES"]) if entry is not None]
    return summary, reasoning, dead_ends, map_notes
