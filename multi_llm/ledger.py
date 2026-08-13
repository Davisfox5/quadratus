"""The orchestrator's memory: an append-only record of completed tasks.

This is what Fable reads. One entry per finished task, written when the task
closes, never rewritten afterwards.

Three properties are load-bearing, each for a measured reason.

**Append-only, never re-summarised.** Summarising a summary compounds loss:
practitioners running repeated compaction report that after two or three
passes the reasoning is gone and after three even the high-level direction is
fuzzy. An entry here is written once from full task context and then frozen.
Keeping earlier text byte-identical also keeps the prompt prefix stable, which
matters because cached input runs at roughly a tenth the price of uncached.

**Reasoning is carried, not just conclusions.** Dropping reasoning traces
between turns measures at a real cost, and a bare conclusion cannot be
re-derived from itself. Every entry has a ``reasoning`` field and it is not
optional.

**Dead ends are carried as lessons, never as transcripts.** These pull in
opposite directions in the literature and the reconciliation is specific:
reflections on failures are worth a large gain on retry, while raw errors
sitting in context make a model measurably more likely to repeat them. So the
schema has a place for "we tried X, it failed because Y" and no place for the
failure itself -- that stays in the artifact store, fetchable.

Rendering puts the invariants first and the current task last. Those are the
two positions a model uses most reliably; the middle of a long prompt is the
least reliable real estate there is, so nothing important is placed there.
Invariants live outside the ledger entirely and are re-emitted verbatim on
every render, because a rule that can only be found by reading the log will
eventually be summarised out of existence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .artifacts import ArtifactRef

__all__ = ["LedgerEntry", "Ledger"]


@dataclass(frozen=True)
class LedgerEntry:
    """One completed task, as the orchestrator will remember it.

    Frozen because an entry is written once from full task context and must
    never be revised by a later, poorer view of the same events.
    """

    seq: int
    task_id: str
    author: str
    #: What was done and decided, in prose.
    summary: str
    #: Why it was decided that way. Never dropped: a conclusion cannot be
    #: re-derived from itself, and a later model with only the conclusion will
    #: re-propose the alternatives that were already rejected.
    reasoning: str
    #: Distilled lessons from what failed. "Tried X, failed because Y" -- the
    #: lesson, never the transcript.
    dead_ends: List[str] = field(default_factory=list)
    #: Pointers to the full raw work. This is what makes the entry an index
    #: rather than a replacement.
    refs: List[ArtifactRef] = field(default_factory=list)

    def render(self, *, with_previews: bool = True) -> str:
        parts = [f"### Task {self.task_id} (by {self.author})", self.summary.strip()]
        if self.reasoning.strip():
            parts.append(f"**Why:** {self.reasoning.strip()}")
        if self.dead_ends:
            parts.append(
                "**Already tried and rejected:**\n"
                + "\n".join(f"- {d}" for d in self.dead_ends)
            )
        if self.refs:
            if with_previews:
                parts.append(
                    "**Full work:**\n" + "\n".join(r.render() for r in self.refs)
                )
            else:
                parts.append(
                    "**Full work:** "
                    + ", ".join(f"artifact {r.id} ({r.kind})" for r in self.refs)
                )
        return "\n\n".join(parts)


class Ledger:
    """Append-only sequence of completed-task entries.

    There is deliberately no update, delete, or recompact method. The only way
    the ledger shrinks is by rendering fewer entries, which loses nothing
    because the entries themselves remain and the artifacts they point at are
    still on disk.
    """

    def __init__(self, invariants: Optional[Sequence[str]] = None) -> None:
        self._entries: List[LedgerEntry] = []
        #: Rules that must survive every render. Held outside the entries so
        #: they cannot be displaced by volume.
        self.invariants: List[str] = list(invariants or [])

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> List[LedgerEntry]:
        return list(self._entries)

    def append(
        self,
        *,
        task_id: str,
        author: str,
        summary: str,
        reasoning: str,
        dead_ends: Optional[Sequence[str]] = None,
        refs: Optional[Sequence[ArtifactRef]] = None,
    ) -> LedgerEntry:
        """Record a completed task. Reasoning is required, not optional."""
        if not summary.strip():
            raise ValueError("a ledger entry needs a summary")
        if not reasoning.strip():
            raise ValueError(
                "a ledger entry needs its reasoning: a bare conclusion cannot be "
                "re-derived, and a later model will re-propose what was rejected"
            )
        entry = LedgerEntry(
            seq=len(self._entries),
            task_id=task_id,
            author=author,
            summary=summary,
            reasoning=reasoning,
            dead_ends=list(dead_ends or []),
            refs=list(refs or []),
        )
        self._entries.append(entry)
        return entry

    def refs(self) -> List[ArtifactRef]:
        """Every artifact reference across all entries."""
        out: List[ArtifactRef] = []
        for entry in self._entries:
            out.extend(entry.refs)
        return out

    def render(
        self,
        *,
        goal: str,
        current: str = "",
        recent: Optional[int] = None,
        with_previews: bool = True,
    ) -> str:
        """Build the orchestrator's prompt body.

        Args:
            goal: The original instruction, verbatim. Placed first and never
                paraphrased -- drift from the early specification is the
                documented multi-turn failure mode, and paraphrase drift across
                many rebuilds compounds invisibly.
            current: The question in front of the orchestrator now. Placed last.
            recent: Render only the last N entries. Older entries stay in the
                ledger and their artifacts stay on disk; this narrows the view,
                it does not discard anything.
            with_previews: Include artifact previews. Off gives a terser index.
        """
        blocks: List[str] = [f"## Goal (verbatim, unchanged)\n\n{goal.strip()}"]

        if self.invariants:
            blocks.append(
                "## Standing rules (always in force)\n\n"
                + "\n".join(f"- {rule}" for rule in self.invariants)
            )

        shown = self._entries if recent is None else self._entries[-recent:]
        if shown:
            omitted = len(self._entries) - len(shown)
            header = "## Completed work"
            if omitted > 0:
                header += (
                    f"\n\n_{omitted} earlier task(s) not shown; their entries and "
                    f"full artifacts remain available on request._"
                )
            blocks.append(
                header + "\n\n"
                + "\n\n".join(e.render(with_previews=with_previews) for e in shown)
            )
        else:
            blocks.append("## Completed work\n\n_Nothing completed yet._")

        if current.strip():
            blocks.append(f"## Now\n\n{current.strip()}")

        return "\n\n".join(blocks)
