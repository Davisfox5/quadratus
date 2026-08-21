"""Ledger persistence: the session's memory survives the process.

The artifact store and the codebase map already live on disk; the ledger --
the one memory that spans a whole run -- lived only in the process. A crash
or a closed laptop mid-run kept every raw artifact and lost the index that
made them a session. This module closes that gap with the same shape as
everything else here: an append-only JSONL file, one line per event, written
at the moment the event happens and never rewritten.

Two event types share the file, tagged by a ``type`` field: completed-task
entries and operator rulings. Replaying the file into a fresh
:class:`~multi_llm.memory.PersistentMemory` reconstructs the ledger exactly
-- same entries, same order, same rulings -- so a resumed run continues from
what was actually recorded rather than from anyone's recollection.

What resume deliberately does not restore: task working memories (wiped at
close by design, so there is nothing to restore) and unfinished tasks (a task
that never closed never reported; the orchestrator re-decides it from the
ledger, which is the same judgement it would have made in-process).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .artifacts import ArtifactRef
from .ledger import LedgerEntry

__all__ = ["SessionLog"]


class SessionLog:
    """Append-only on-disk record of ledger entries and rulings."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    # -- writing -------------------------------------------------------------
    def _append(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def append_entry(self, entry: LedgerEntry) -> None:
        self._append({
            "type": "entry",
            "task_id": entry.task_id,
            "author": entry.author,
            "summary": entry.summary,
            "reasoning": entry.reasoning,
            "dead_ends": list(entry.dead_ends),
            "open_questions": list(entry.open_questions),
            "refs": [asdict(r) for r in entry.refs],
        })

    def append_ruling(self, ruling: str) -> None:
        self._append({"type": "ruling", "text": ruling})

    def record_goal(self, goal: str) -> None:
        """Store the goal the run was recorded under.

        A resumed session must continue toward the goal its entries were
        written against -- resuming an old ledger under a new goal would
        interleave two projects.
        """
        self._append({"type": "goal", "text": goal})

    def stored_goal(self) -> Optional[str]:
        """The most recently recorded goal, or None."""
        goal: Optional[str] = None
        for event in self.load():
            if event.get("type") == "goal" and event.get("text"):
                goal = event["text"]
        return goal

    def rotate(self) -> Path:
        """Archive the current log aside and start empty. Returns the archive.

        Nothing is deleted -- the same rule as everywhere else. The archive
        name counts up so repeated fresh starts never overwrite each other.
        """
        n = 1
        while True:
            archive = self.path.with_suffix(f".{n}.jsonl")
            if not archive.exists():
                break
            n += 1
        self.path.rename(archive)
        return archive

    # -- reading -------------------------------------------------------------
    def load(self) -> List[dict]:
        """Every event, oldest first. Malformed lines are skipped, not fatal:
        a partially written final line from a crash must not make the rest of
        the record unreadable."""
        if not self.path.exists():
            return []
        events: List[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def restore_into(self, memory) -> int:
        """Replay the log into a fresh PersistentMemory. Returns entries restored.

        Refuses a memory that already has entries: replaying on top of live
        state would interleave two histories, and the ledger is append-only
        precisely so that never happens.
        """
        if len(memory.ledger) > 0:
            raise RuntimeError(
                "refusing to restore into a ledger that already has entries"
            )
        restored = 0
        for event in self.load():
            if event.get("type") == "ruling":
                text = event.get("text", "")
                if text:
                    memory.ledger.rulings.append(text)
            elif event.get("type") == "entry":
                try:
                    memory.ledger.append(
                        task_id=event["task_id"],
                        author=event["author"],
                        summary=event["summary"],
                        reasoning=event["reasoning"],
                        dead_ends=event.get("dead_ends") or [],
                        open_questions=event.get("open_questions") or [],
                        refs=[ArtifactRef(**r) for r in (event.get("refs") or [])],
                    )
                    restored += 1
                except (KeyError, TypeError, ValueError):
                    continue
        return restored

    def highest_task_number(self) -> int:
        """The largest ``t<N>`` seen, so a resumed session numbers onward.

        Redo ids (``t3-r1``) count under their root; unrecognised ids count
        zero. This is what keeps a resumed run from reissuing ``t1``.
        """
        highest = 0
        for event in self.load():
            if event.get("type") != "entry":
                continue
            root = str(event.get("task_id", "")).split("-r", 1)[0]
            if root.startswith("t") and root[1:].isdigit():
                highest = max(highest, int(root[1:]))
        return highest
