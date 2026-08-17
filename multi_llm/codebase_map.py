"""A per-repository memory that survives sessions.

The ledger is deliberately per-session: it records what one run did. This is
the other half -- what has been *learned about the codebase itself*, which is
worth exactly as much on the hundredth run as on the first. Blitzy's whole
enterprise moat is this idea taken to scale: a knowledge graph of the code
that every run strengthens, so institutional memory accrues instead of
evaporating with the session. Ours is a deliberately small version of the
same bet -- notes, not a graph -- because notes are what a model can both
produce reliably and consume verbatim, and a structure the models cannot
maintain is a structure that rots.

The rules are inherited from the ledger, for the same reasons:

* **Append-only.** There is no update or delete. A note that stops being true
  is answered with a new note saying so, which preserves the history of what
  was believed and when -- the same reasoning that keeps ledger entries
  frozen. Pruning, if it is ever needed, is an operator act on the file, not
  an API a model can be talked into calling.
* **Every note carries provenance.** Author and session, so a future reader
  can weigh a claim from a verified close-out differently from an operator
  assertion.
* **Rendered whole.** The map rides into prompts as one block. When it grows
  past what that can bear, the answer is topic filtering at render time --
  narrowing the view, never rewriting the store.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Sequence

__all__ = ["MapNote", "CodebaseMap"]


@dataclass(frozen=True)
class MapNote:
    """One durable fact about the codebase.

    ``topic`` is a coarse bucket ("architecture", "conventions", "gotchas",
    "dependencies", ...) used only for grouping at render time; it is free
    text on purpose, because a fixed taxonomy of what might be worth knowing
    about a codebase would be wrong within a month.
    """

    topic: str
    note: str
    author: str
    session: str

    def render(self) -> str:
        return f"- {self.note} _({self.author}, {self.session})_"


class CodebaseMap:
    """Append-only notes about one repository, stored beside it.

    Storage is a JSON-lines file: one note per line, appended and never
    rewritten. JSONL rather than a single document so that an append is an
    append at the file level too -- two sessions writing concurrently cannot
    lose each other's notes the way rewriting a whole document would.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._notes: List[MapNote] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._notes.append(MapNote(**json.loads(line)))

    def __len__(self) -> int:
        return len(self._notes)

    @property
    def notes(self) -> List[MapNote]:
        return list(self._notes)

    def amend(self, *, topic: str, note: str, author: str, session: str = "") -> MapNote:
        """Add one note. The only write operation there is."""
        if not note.strip():
            raise ValueError("an empty note teaches the next session nothing")
        if not topic.strip():
            raise ValueError("a note needs a topic to be findable under")
        entry = MapNote(
            topic=topic.strip().lower(),
            note=note.strip(),
            author=author,
            session=session,
        )
        self._notes.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")
        return entry

    def topics(self) -> List[str]:
        seen: List[str] = []
        for n in self._notes:
            if n.topic not in seen:
                seen.append(n.topic)
        return seen

    def render(self, *, topics: Optional[Sequence[str]] = None) -> str:
        """The map as one prompt block, grouped by topic.

        ``topics`` narrows the view without touching the store, which is the
        only sanctioned way this shrinks.
        """
        wanted = set(t.lower() for t in topics) if topics is not None else None
        shown = [n for n in self._notes if wanted is None or n.topic in wanted]
        if not shown:
            return ""
        blocks = ["## Codebase map (persists across sessions)"]
        for topic in self.topics():
            group = [n for n in shown if n.topic == topic]
            if group:
                blocks.append(f"**{topic}**\n" + "\n".join(n.render() for n in group))
        return "\n\n".join(blocks)
