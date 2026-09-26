"""Reading an orchestrator's reply without silently guessing at it.

An orchestrator reply carries two things at once: a *control message* (ASK,
FETCH, CONSULT, WORKER, DONE) or a *task*, and -- for a task -- a ``KIND:``
line naming the kind and difficulty that decide routing. The old parser read
only the first physical line. That is the whole bug behind the 2026-09-13
trial's silent misroute: Fable prefaced ``KIND: test rote`` with a paragraph
of confirmation prose, the first line was not a label, and the reply fell all
the way through to the ``general``/``simple`` default. A testing assignment
pinned to Sol became general simple work on the difficulty ladder, and nothing
anywhere said so.

Three properties matter, and they pull against each other:

**A label placed after prose must still be found.** Models preface things.
Refusing every prefaced reply would trade a silent misroute for a lost task,
which is worse. So the scan is bounded rather than first-line-only.

**A guess must never look like a reading.** The old code's failure mode was
that the default was indistinguishable from a real label. Every result here
carries :attr:`TaskMetadata.confidence` and :attr:`TaskMetadata.notes`, so a
defaulted route is visible in the ledger and in diagnostics rather than
inferred later from a routing table.

**Ambiguity fails loudly; a wrong label degrades loudly.** Two ``KIND:`` lines
that disagree have no single reading, so :class:`AmbiguousMetadata` is raised
and the caller spends one bounded correction re-prompt. A label naming a kind
that does not exist does have a reading, just a wrong one: it degrades to the
default and says so, keeping the established trade that a mislabelled task
costs a routing preference while a rejected round costs the task. Neither case
is silent, which is the only property the old code lacked.

The control-message half has the same shape. ``ASK:`` buried under prose used
to read as a task description containing the word ASK, so the operator was
never asked and the run proceeded on an unanswered question. Control messages
are now recognised through the same bounded scan, and a reply that mixes a
control message with substantive prose is reported rather than silently
resolved in either direction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

__all__ = [
    "AmbiguousMetadata",
    "ControlMessage",
    "TaskMetadata",
    "parse_control",
    "parse_metadata",
    "MAX_PREFACE_LINES",
]

#: How far into a reply a ``KIND:`` line or a control verb may be buried before
#: the reply is treated as prose that happens to mention one. Six lines covers
#: the observed failure -- the trial's preface was a single wrapped paragraph
#: occupying two lines before the label -- without letting a label mentioned in
#: passing halfway down a long answer capture the routing decision.
MAX_PREFACE_LINES = 6

#: The control verbs. ``DONE`` is matched as a whole word rather than a prefix:
#: a task description legitimately beginning "DONE-criteria: ..." is a task.
_CONTROL_RE = re.compile(
    r"^(?:(ASK|FETCH)\s*:|(CONSULT|WORKER)\b|(DONE)\s*$)",
    re.IGNORECASE,
)

_KIND_LINE_RE = re.compile(r"^\s*KIND\s*:(.*)$", re.IGNORECASE)


class AmbiguousMetadata(ValueError):
    """A reply's kind/difficulty could not be read without guessing.

    Raised only where there is no single reading to take: two ``KIND:`` lines
    that disagree. The caller's move is one bounded correction re-prompt. An
    unrecognised kind is not this -- it has a reading, just a wrong one, and
    degrades loudly instead.
    """

    def __init__(self, message: str, *, candidates: Optional[List[str]] = None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(frozen=True)
class ControlMessage:
    """A recognised control message and where it was found.

    ``preface`` is whatever stood before the verb. It is carried rather than
    discarded so the caller can tell a clean ``ASK: ...`` from a paragraph of
    reasoning with a question at the end -- the second is still served, but the
    reasoning belongs in the record.
    """

    verb: str
    body: str
    preface: str = ""
    line_index: int = 0

    @property
    def prefaced(self) -> bool:
        return bool(self.preface.strip())


@dataclass(frozen=True)
class TaskMetadata:
    """The routing decision recovered from a reply, with its provenance.

    Attributes:
        kind: A key of :data:`quadratus.task_kinds.ROUTING`.
        difficulty: A :class:`quadratus.session.Complexity` value.
        description: The task text, with the label line and any preface that
            was only scaffolding removed.
        confidence: How the route was arrived at. ``"labelled"`` -- the model
            stated it. ``"degraded"`` -- it stated something unrecognised and
            the default was substituted. ``"defaulted"`` -- it stated nothing.
            Three grades, all explicit; the bug being fixed was a default that
            looked exactly like a statement.
        notes: Human-readable provenance, rendered into diagnostics.
    """

    kind: str
    difficulty: str
    description: str
    confidence: str = "labelled"
    notes: List[str] = field(default_factory=list)

    @property
    def defaulted(self) -> bool:
        """True when the route was not taken from a label the model stated."""
        return self.confidence in ("defaulted", "degraded")

    def render(self) -> str:
        base = f"kind={self.kind} difficulty={self.difficulty} ({self.confidence})"
        return base + ("; " + "; ".join(self.notes) if self.notes else "")


def parse_control(reply: str, *, max_preface_lines: int = MAX_PREFACE_LINES):
    """Find a control message in ``reply``, tolerating a bounded preface.

    Returns a :class:`ControlMessage` or ``None``. The scan stops at the first
    verb found: a reply that says ``FETCH: abc`` and then goes on to muse about
    consulting someone is a fetch, and re-reading the musing as a second
    control message would serve a request the model did not make.
    """
    lines = (reply or "").splitlines()
    for index, line in enumerate(lines[:max_preface_lines]):
        stripped = line.strip()
        if not stripped:
            continue
        match = _CONTROL_RE.match(stripped)
        if match is None:
            continue
        verb = next(g for g in match.groups() if g).upper()
        body = stripped[match.end():].strip() if verb in {"ASK", "FETCH"} else stripped
        return ControlMessage(
            verb=verb,
            body=body,
            preface="\n".join(lines[:index]).strip(),
            line_index=index,
        )
    return None


def parse_metadata(
    reply: str,
    *,
    known_kinds,
    known_difficulties,
    default_kind: str,
    default_difficulty: str,
    max_preface_lines: int = MAX_PREFACE_LINES,
) -> TaskMetadata:
    """Recover kind, difficulty and description from an orchestrator reply.

    ``known_kinds`` and ``known_difficulties`` are passed in rather than
    imported so this module stays free of the routing tables it serves, and so
    a caller with a narrowed vocabulary (a security excursion, a test) can say
    so.

    Raises:
        AmbiguousMetadata: when two ``KIND:`` lines disagree. There is no
            single reading to take, so a default would be inventing one. An
            *unrecognised* kind is different and does not raise: it degrades
            to ``default_kind`` with ``confidence="degraded"``, keeping the
            established trade that a mislabelled task costs a routing
            preference while a rejected round costs the task.
    """
    text = (reply or "").strip()
    if not text:
        return TaskMetadata(
            kind=default_kind, difficulty=default_difficulty, description="",
            confidence="defaulted", notes=["empty reply"],
        )

    lines = text.splitlines()
    found: List[tuple] = []
    for index, line in enumerate(lines[:max_preface_lines]):
        match = _KIND_LINE_RE.match(line)
        if match:
            found.append((index, match.group(1).strip()))

    if not found:
        # No label anywhere in the preface window. This is the honest default,
        # and it says so: a defaulted route is visible, not indistinguishable
        # from a stated one.
        return TaskMetadata(
            kind=default_kind, difficulty=default_difficulty, description=text,
            confidence="defaulted",
            notes=[f"no KIND label in the first {max_preface_lines} lines"],
        )

    parsed = [(index, _split_label(label, known_kinds, known_difficulties))
              for index, label in found]
    distinct = {p[:2] for _, p in parsed}
    if len(distinct) > 1:
        raise AmbiguousMetadata(
            "the reply carries more than one KIND line and they disagree; "
            "re-ask for a single label rather than routing on a guess",
            candidates=[f"{k or '?'} {d or '?'}" for k, d in sorted(distinct)],
        )

    index, (kind, difficulty, unknown) = parsed[0]
    notes: List[str] = []
    confidence = "labelled"
    if kind is None:
        # A label naming a kind that does not exist. This degrades rather than
        # failing the round -- a mislabelled task costs a routing preference,
        # a rejected round costs the task, and that trade is a settled
        # decision here. What changes is that it is no longer silent: the
        # degrade is recorded with the offending token, so a lost pin is
        # visible in the record instead of being indistinguishable from a
        # task nobody labelled.
        notes.append(
            f"labelled {unknown!r}, which is not a known kind; routed as "
            f"{default_kind} and the stated label was not honoured"
        )
        kind = default_kind
        confidence = "degraded"

    preface = "\n".join(lines[:index]).strip()
    if preface:
        # Kept, not dropped: the prose before a label is often the reasoning
        # behind the classification, which the ledger wants. But it is no
        # longer allowed to *be* the task description by accident.
        notes.append(f"label found after {index} line(s) of preface")
    if difficulty is None:
        difficulty = default_difficulty
        notes.append(f"no difficulty stated; defaulted to {default_difficulty}")

    description = "\n".join(lines[index + 1:]).strip()
    if not description:
        # "KIND: x" with nothing after it, but prose before it. The prose is
        # the task -- this is the shape the trial's reply actually had.
        description = preface
        if description:
            notes.append("description taken from the text before the label")

    return TaskMetadata(
        kind=kind, difficulty=difficulty, description=description,
        confidence=confidence, notes=notes,
    )


def _split_label(label: str, known_kinds, known_difficulties):
    """Split a ``KIND:`` line's payload into (kind, difficulty, unknown).

    ``unknown`` is the offending token when the kind is not recognised, and
    ``None`` otherwise. Difficulty is optional and may appear in any position
    after the kind.
    """
    parts = [p for p in re.split(r"[\s,]+", label.strip().lower()) if p]
    if not parts:
        return None, None, ""
    difficulty = next((p for p in parts if p in known_difficulties), None)
    kinds = [p for p in parts if p in known_kinds]
    if kinds:
        return kinds[0], difficulty, None
    # Nothing in the line names a known kind. Report the first token that is
    # not a difficulty as the offender, so the correction prompt can quote it.
    offender = next((p for p in parts if p not in known_difficulties), parts[0])
    return None, difficulty, offender


#: How much explanation may precede a lead's closing request.
MAX_REQUEST_PREFACE_LINES = 20


#: A line that opens a WORKER request: the verb alone, or the verb and then
#: whitespace. ``WORKERS`` or ``WORKER:`` in prose is not one.
_WORKER_LINE = re.compile(r"WORKER(?:\s|$)")


def _is_request_line(line: str) -> bool:
    """A preface line that is itself a request, not prose about one."""
    upper = line.upper()
    return (bool(re.match(r"WORKER\s*(?:\{|$)", line)) or upper.startswith("FETCH:")
            or (upper.startswith("CONSULT ") and ":" in line))


def _terminal_worker(lines: List[str]) -> Optional[tuple]:
    """``(start, request)`` for a WORKER request closing ``lines``, else None.

    The JSON object may follow the verb on the same line or on the lines
    after it, and must be the only thing left: one object, then nothing.
    """
    start = next((i for i in range(len(lines) - 1, -1, -1) if _WORKER_LINE.match(lines[i])), None)
    if start is None:
        return None
    text = "\n".join(lines[start:])[len("WORKER"):].strip()
    try:
        value, end = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or text[end:].strip():
        return None
    body = text[:end]
    # One line, the shape every dispatcher parses; a request that was already
    # one line is passed on byte-for-byte.
    return start, "WORKER " + (body if "\n" not in body else json.dumps(value, ensure_ascii=False))


def split_lead_request(reply: str) -> Optional[tuple]:
    """``(preface, request)`` if a lead's reply ends with a FETCH / CONSULT / WORKER request.

    The request must close the reply, with at most a short preface before it,
    no CHANGED line anywhere, and no second request in the preface. GameTape
    run 7 (2026-09-25): an Opus lead explained its plan in a few lines and
    ended with a WORKER write errand; the editing dispatcher and the drafting
    loop both required the request to be the whole reply, so the run stopped
    as misreported edits. Run 9 (2026-09-26): after write refusals an Opus
    lead ended with ``WORKER`` on its own line and the JSON object on the
    next, and the same stop recurred. A WORKER request must carry exactly one
    JSON object, so prose that happens to end with the word WORKER is still a
    draft. Consecutive closing CONSULT lines are one request; the drafting
    loop serves each of them against the consult budget.

    Run 11 (2026-09-26): a Grok lead ended ``...the client fixture.FETCH:
    2185e3cf5881``, the request run onto the end of its last sentence. A
    request that follows sentence-ending punctuation on its line is split off
    and parsed the same way; see :func:`_inline_request_start`.

    ``request`` is normalised for the dispatchers: ``WORKER {...}`` on one
    line, ``FETCH: id``, or the closing CONSULT lines.
    """
    text = (reply or "").strip()
    split = _split_request_lines(text)
    if split is not None or any(line.strip().startswith("CHANGED:") for line in text.splitlines()):
        return split
    start = _inline_request_start(text)
    if start is None:
        return None
    return _split_request_lines(text[:start] + "\n" + text[start:])


#: A request verb run onto the end of a sentence: ``.``, ``!`` or ``?``,
#: optional spaces, then the verb.
_INLINE_REQUEST = re.compile(r"(?<=[.!?])[ \t]*(?=(?:FETCH:|CONSULT |WORKER(?:\s|$)))")


#: An artifact id as ``ArtifactStore`` issues it. An inline FETCH must name
#: exactly this and nothing else, so a quoted example (``"...FETCH: id"``)
#: keeps its closing quote in the token and is not a request.
_ARTIFACT_ID = re.compile(r"[0-9a-f]{12}")

#: A fence opener or closer: up to three spaces, then three or more backticks
#: or tildes (CommonMark).
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def _code_spans(text: str) -> List[tuple]:
    """``(start, end)`` offsets of fenced code blocks and inline code spans.

    Fences of backticks or tildes close on a run of the same character at
    least as long; an unclosed fence runs to the end. Inline spans close on a
    backtick run of the same length. An unclosed run is treated as code to the
    end of the text: conservative, since a false request is worse than a
    missed one here (a missed request surfaces as a loud stop, not an action).
    """
    spans, offset, fence = [], 0, None
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1]:
                spans.append((fence[2], offset + len(line)))
                fence = None
        elif match:
            fence = (match.group(1)[0], len(match.group(1)), offset)
        else:
            position = 0
            while True:
                opener = re.compile(r"`+").search(line, position)
                if opener is None:
                    break
                closer = re.compile(r"(?<!`)" + re.escape(opener.group()) + r"(?!`)").search(line, opener.end())
                if closer is None:
                    spans.append((offset + opener.start(), len(text)))
                    return spans
                spans.append((offset + opener.start(), offset + closer.end()))
                position = closer.end()
        offset += len(line)
    if fence is not None:
        spans.append((fence[2], len(text)))
    return spans


def _inline_request_start(text: str) -> Optional[int]:
    """Where a request starts mid-line, if exactly one such start exists.

    Only after sentence-ending punctuation, never inside fenced or inline
    code, never inside an open double quote on its line, and never at the
    start of a line (the line-based parser already covers those). More than
    one candidate is ambiguous and yields None, so a preface that runs two
    requests together stays a draft. An inline FETCH must name exactly one
    artifact id. Every rule errs toward leaving a reply as a draft.
    """
    code = _code_spans(text)
    found, offset = [], 0
    for line in text.splitlines(keepends=True):
        for match in _INLINE_REQUEST.finditer(line):
            at = offset + match.end()
            head = line[:match.start()]
            if not head.strip() or any(a <= at < b for a, b in code):
                continue
            if (head.count('"') + head.count("\u201c") - head.count("\u201d")) % 2:
                continue
            found.append((at, line[match.end():].strip()))
        offset += len(line)
    if len(found) != 1:
        return None
    start, tail = found[0]
    if tail.startswith("FETCH:") and not _ARTIFACT_ID.fullmatch(tail[len("FETCH:"):].strip()):
        return None
    return start


def _split_request_lines(text: str) -> Optional[tuple]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or any(line.startswith("CHANGED:") for line in lines):
        return None
    worker = _terminal_worker(lines)
    if worker is not None:
        start, request = worker
    else:
        last = lines[-1]
        if last.upper().startswith("FETCH:") and last.split(":", 1)[1].strip():
            start = len(lines) - 1
        elif last.upper().startswith("CONSULT ") and ":" in last:
            start = len(lines) - 1
            while start and lines[start - 1].upper().startswith("CONSULT ") and ":" in lines[start - 1]:
                start -= 1
        else:
            return None
        request = "\n".join(lines[start:])
    preface = lines[:start]
    if len(preface) > MAX_REQUEST_PREFACE_LINES or any(_is_request_line(line) for line in preface):
        return None
    return "\n".join(preface), request


def lead_request(reply: str) -> Optional[str]:
    """The normalised request closing a lead's reply; see :func:`split_lead_request`."""
    split = split_lead_request(reply)
    return split[1] if split is not None else None

