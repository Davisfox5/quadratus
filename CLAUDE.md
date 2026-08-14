# Working notes for Claude

## Communication

**Lead with a TL;DR on anything long or technical.** Plain language, bullets,
no jargon, at the very top — what it means and what the decision is. Put the
technical detail below it for when it's wanted. Don't make the summary an
afterthought at the bottom; it goes first.

This applies to design discussions, research findings, architecture proposals,
and post-change reports. A short answer to a short question doesn't need one.

## Project context

This repo is being rebuilt into a multi-model coding system that runs on
consumer *subscriptions* (Claude Max, ChatGPT, Google AI, SuperGrok) by
driving the vendor CLIs, rather than on billed API keys. See
`multi_llm/cli_providers.py` for why that is permitted for individual use and
where the line is.

Key design decisions already settled:

- **Fable 5 orchestrates and is a hard dependency.** If it is unavailable the
  run halts rather than substituting. It is the only participant that persists
  across a session; everyone else is re-invoked fresh each round.
- **The brain trust is fixed** (Opus 5, GPT-5.6 Sol, Gemini 3.1 Pro, Grok 4.6).
  Nobody is displaced from it, and Sonnet is not in it.
- **Security work is peeled into a bounded excursion**: Opus takes the seat,
  GPT-5.6 Sol does the work, Opus verifies, the excursion closes, Fable
  resumes. Never an open-ended handover.
- **Three memory scopes, one per role.** The orchestrator persists for the
  whole session; a brain-trust member keeps full working memory for one task
  and is wiped when it closes; worker bees keep nothing. Continuity where a
  thread must be held, isolation where independence is worth more.
- **A summary is an index, never a replacement.** Raw output is written to the
  artifact store and kept; summaries carry pointers, and any model can fetch
  the original when a decision turns on a detail. An earlier version of this
  file said "nothing raw reaches Fable" -- that was wrong, and it is the one
  design error worth remembering, because a mandatory lossy hop loses specifics
  irrecoverably.
- **The ledger is append-only and never re-summarised.** Summarising a summary
  compounds loss. Reasoning is carried, not just conclusions. Dead ends are
  carried as distilled lessons, never as raw transcripts.
- **Invariants live outside the ledger** and are re-emitted verbatim on every
  render. A rule findable only by reading the log will eventually be
  summarised out of existence.
- Model tables are seeds, not truth. The lineup churns monthly — prefer a
  probe against the installed CLIs over anything hardcoded.
- **Task size beats model choice.** Review quality falls by roughly an order
  of magnitude between a small diff and a large one — a wider gap than any
  gap between reviewers. The decomposition prompt therefore carries a hard
  size ceiling (`task_kinds.MAX_TASK_LINES`), and that is the single
  highest-value instruction in the loop.
- **Routing opinions live apart from the roster.** `registry.py` says what
  shape a model is; `task_kinds.py` says what to do with that. The second
  ages far faster than the first and every entry carries its evidence.
- **Low confidence rotates on purpose.** A routing entry with no measured
  basis does not pin a model, because pinning on a hunch freezes the hunch
  and destroys the head-to-head data that would have corrected it. An empty
  `prefer` is a statement, not an omission.
- **Some work is gated, not routed.** Where every model is bad at something —
  concurrency, performance — there is nobody to prefer, so the policy attaches
  a deterministic check instead of a model. Performance additionally names a
  profiler as the first move.
- **Never one reviewer.** The two pinned reviewers fail in opposite directions
  (recall-leaning vs precision-leaning), so a review task always draws its
  counterpart even at the lowest complexity.
- Grok 4.20's 2M window is unverified vendor marketing and deliberately does
  not carry `LONG_CONTEXT`. Restore the tag if it passes the probe at depth.
