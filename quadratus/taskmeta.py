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
