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

**The render shrinks; the ledger never does.** A long session will eventually
build a body bigger than the window it is rendered into. That pressure is
handled at render time, by dropping artifact previews and then eliding whole
entries from the oldest end, each replaced by an index line naming its task and
artifacts -- never by rewriting an entry, which is the one thing that would
compound loss. See :meth:`Ledger._fit`.

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
from .usage import CHARS_PER_TOKEN

__all__ = ["LedgerEntry", "Ledger", "estimate_tokens", "ELISION_INDEX_CAP"]

#: Beyond this many elided entries the per-task index is itself a cost, and a
#: range plus a count carries the same information for a reader who is going
#: to ask for a task by id anyway.
ELISION_INDEX_CAP = 25


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
        #: Operator answers to questions only they could answer. Same
        #: treatment as invariants -- re-emitted verbatim on every render --
        #: because a ruling that gets buried will be asked again, and asking
        #: the operator the same question twice is the failure the ASK channel
        #: exists to prevent.
        self.rulings: List[str] = []

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
        extra: str = "",
        budget_tokens: Optional[int] = None,
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
            extra: An additional pre-rendered block (e.g. the codebase map),
                placed after the completed work and before the current
                question, so the goal keeps the first position and the live
                question keeps the last.
            budget_tokens: Approximate ceiling for the whole rendered body.
                A fixed ``recent`` cannot know how big its entries turned out
                to be; this measures. See :meth:`_fit`.
        """
        head: List[str] = [f"## Goal (verbatim, unchanged)\n\n{goal.strip()}"]

        if self.invariants:
            head.append(
                "## Standing rules (always in force)\n\n"
                + "\n".join(f"- {rule}" for rule in self.invariants)
            )

        if self.rulings:
            head.append(
                "## Operator rulings (asked and answered; do not re-ask)\n\n"
                + "\n".join(f"- {r}" for r in self.rulings)
            )

        tail: List[str] = []
        if extra.strip():
            tail.append(extra.strip())
        if current.strip():
            tail.append(f"## Now\n\n{current.strip()}")

        shown = self._entries if recent is None else self._entries[-recent:]
        elided = self._entries[: len(self._entries) - len(shown)]

        def assemble(kept: List[LedgerEntry], dropped: List[LedgerEntry],
                     previews: bool) -> str:
            return "\n\n".join(
                [*head, _work_block(kept, dropped, previews), *tail]
            )

        if budget_tokens is None:
            return assemble(shown, elided, with_previews)
        return self._fit(assemble, shown, elided, with_previews, budget_tokens)

    @staticmethod
    def _fit(assemble, shown, elided, with_previews, budget_tokens) -> str:
        """Shrink the render until it fits, without abridging anything.

        Context pressure is not a maybe in a long session, and the way it
        usually announces itself is a provider error mid-run. Handling it in
        advance is the easy part; the trap is *how*. The standard answer --
        summarise the old turns -- is the one move this ledger exists to
        forbid, because summarising a summary compounds loss and the second
        pass takes the reasoning with it.

        So the render degrades in two steps and never rewrites a word:

        1. Drop artifact previews, oldest position first, so entries stay
           whole and only the inline sample of their attachments goes.
        2. Elide whole entries from the oldest end, replacing each with an
           index line naming its task and artifacts.

        An elided entry is not lost in any sense: it is still in the ledger,
        its artifacts are still on disk, and the index tells the orchestrator
        exactly what to ask for. This is "a summary is an index" applied to
        the render itself -- the content leaves the context window, not the
        system.

        If the parts that are never trimmed -- goal, standing rules, operator
        rulings, the current question -- exceed the budget on their own, the
        fully-elided render is returned over budget rather than cut. Those are
        the load-bearing text; a budget that cannot hold them is the wrong
        budget, and silently dropping a standing rule to satisfy a number
        would be a far worse failure than an over-long prompt.
        """
        # Step one is tried once and whole: previews are the cheapest thing to
        # lose, so every entry keeps its previews or none does. Interleaving
        # the two steps would trade a whole entry away to keep one sample,
        # which is the wrong thing to lose first.
        if with_previews:
            candidate = assemble(shown, elided, True)
            if estimate_tokens(candidate) <= budget_tokens:
                return candidate
        candidate = ""
        for cut in range(len(shown) + 1):
            candidate = assemble(shown[cut:], elided + shown[:cut], False)
            if estimate_tokens(candidate) <= budget_tokens:
                return candidate
        return candidate


def estimate_tokens(text: str) -> int:
    """Rough token count, on the same ~4-chars/token rule the meter uses.

    Good enough to decide whether a prompt is about to blow a window; not
    good enough to bill against, which is why nothing here reports it as a
    measurement.
    """
    return int(len(text or "") / CHARS_PER_TOKEN)


def _work_block(shown: List[LedgerEntry], elided: List[LedgerEntry],
                with_previews: bool) -> str:
    """The completed-work section, with an index for whatever is not shown."""
    if not shown and not elided:
        return "## Completed work\n\n_Nothing completed yet._"

    header = "## Completed work"
    if elided:
        header += "\n\n" + _elision_index(elided)
    if not shown:
        return header
    return (
        header + "\n\n"
        + "\n\n".join(e.render(with_previews=with_previews) for e in shown)
    )


def _elision_index(entries: List[LedgerEntry]) -> str:
    """Name what is not shown, so it can still be asked for by id."""
    notice = (
        f"_{len(entries)} earlier task(s) not shown in full here. Their entries "
        f"are intact in the ledger and nothing has been re-summarised -- ask "
        f"for any of them, or fetch an artifact below, when a decision turns "
        f"on one._"
    )
    if len(entries) > ELISION_INDEX_CAP:
        return notice + (
            f"\n\n_Tasks {entries[0].task_id} through {entries[-1].task_id}._"
        )
    lines = []
    for entry in entries:
        refs = ", ".join(f"artifact {r.id}" for r in entry.refs) or "no artifacts"
        lines.append(f"- {entry.task_id} (by {entry.author}): {refs}")
    return notice + "\n\n" + "\n".join(lines)
