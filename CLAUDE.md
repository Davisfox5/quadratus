# Working notes for Claude

## Communication

**Be concise. Get straight to the point in every sentence.** No hedging, no
restating the question, no narrating process. Cut anything that doesn't change
what the reader does next. This applies to everything, always.

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
- **Difficulty is the primary routing axis; kind pins are the exception**
  (`task_kinds.DIFFICULTY_LADDER`). Complex → Opus, standard → Sol, simple
  (the bulk of well-sized tasks) → Grok 4.6, rote → Gemini 3.1 Pro. The
  earlier kind-pinned table concentrated nearly everything on two
  subscriptions while Grok and Google sat idle — exactly how one window
  exhausts early and forces a degraded run. An unavailable or excluded rung
  escalates upward before it degrades downward. Surviving pins: security and
  testing → Sol (operator directive), review → the Sol+Opus pair,
  scope/decompose → Fable; Gemini still never leads mobile. Revisit rungs on
  Grok 4.7 and Gemini 3.5 Pro releases.
- **An empty `prefer` is a statement, not an omission** — it means the kind
  rides the ladder rather than anyone having earned a pin.
- **Workers are picked by errand, not vendor loyalty** (`workers.WORKER_TREE`):
  lookup → Grok 4.1 Fast, read/visual → Gemini Flash, check/code → Haiku,
  format/draft → Luna. One skill per vendor, so picking by skill also spreads
  the four windows. The same-vendor default was retired: caches stay warm
  through regular use, which the tree guarantees. **Escalation stays in the
  family** (`WORKER_ESCALATION`): a demanding errand bumps one tier up its own
  vendor's line — Haiku→Sonnet, Flash→3.6 Thinking, Luna→Terra, Grok
  Fast→Grok 4.20 — so the skill stays matched while the horsepower rises.
  The two unverified bump targets are acceptable there because escalation
  fires rarely, after the verified base already failed, and degrades back to
  the base if the target leaves the roster.
- **Worker failure rules**: same prompt + same model twice is refused
  (`RepeatedFailure`); the same prompt on a *different* worker is a changed
  strategy and allowed. Budgets: `max_concurrent` (4) bounds parallel width,
  `max_per_task` (12) is the lifetime backstop — sized so a lead can rewrite
  or reroute failed errands without a round trip through the orchestrator.
  Workers run concurrently (`commission_many`); a failed errand returns as a
  result with `error`, never tearing down siblings.
- **Tool requests flow worker → lead → reissue.** A worker lacking access says
  `NEED TOOL: <what>` in its one answer; the lead reissues the errand with the
  grant (`allow_writes`). The asking worker is wiped as normal. No mid-task
  dialogue, no orchestrator involvement. Grok 4.3 is deliberately not a
  worker (dominated by 4.1 Fast); Gemini 3.6 Thinking and Grok 4.20 serve
  only as in-family escalation targets, never as base workers.
- **Some work is gated, not routed.** Where every model is bad at something —
  concurrency, performance — there is nobody to prefer, so the policy attaches
  a deterministic check instead of a model. Performance additionally names a
  profiler as the first move.
- **Reviewers are anonymous to the lead, named in the record.** Critiques
  reach the lead as "Reviewer A/B" so they are weighed on content, not
  letterhead — models carry priors about other models, and reputation-based
  discounting is exactly the filtering the lead must not do. Full
  attribution survives in artifact kinds and the ledger for the operator's
  scoreboard. Consults stay named: there, choosing the expert is the point.
- **Never one reviewer.** The two pinned reviewers fail in opposite directions
  (recall-leaning vs precision-leaning), so a review task always draws its
  counterpart even at the lowest complexity.
- Grok 4.20's 2M window is unverified vendor marketing and deliberately does
  not carry `LONG_CONTEXT`. Restore the tag if it passes the probe at depth.
- **Verification crosses vendor lines.** Same-vendor checking shares the
  author's lineage and blind spots; Blitzy's audited record was built on one
  family checking another. `cross_family_verifier` enforces it, including in
  security excursions when the chain degrades to a same-vendor pair.
- **The lead's task is recited at the end of its prompt** (Manus's fix for
  goal drift): the end of context is the position attention favours, so the
  objective is re-emitted there, not just stated up front.
- **A verbatim retry of a failed action is refused** (`RepeatedFailure`), and
  a run stalls loudly (`RunStalled`) when the orchestrator names the same
  task twice in a row. Repeating a known-bad action is the canonical agent
  death spiral; the fix is a changed strategy, not a second pull.
- **The codebase map is the cross-session memory** (`codebase_map.py`):
  append-only notes about the repository itself, with provenance, rendered
  into every orchestrator and lead prompt and amended from close-outs. The
  ledger records what a run did; the map records what was learned about the
  code, which is worth as much on the hundredth run as the first.
- **The plan gate is opt-in** (`SessionConfig.plan_gate`): the operator can
  review the full expected task list before any window is spent. Reviewing
  the plan is reviewing the work at a fraction of the cost.
- **Every invocation is metered against API list prices** (`usage.py`):
  measured token counts when the CLI reports them, ~4-chars/token estimates
  otherwise, marked as such. Metering is observational only — a meter that
  can fail a run has negative value. Prices are a seed sheet, stale by
  assumption.
- **Critique gets a rebuttal round, then fix→verify — never more open
  debate.** The measured failure of extra debate rounds is conformity
  (uncritical majority-adoption up to ~85%, peer rationales destabilising
  correct answers, 2–3× tokens for equal or worse accuracy); the measured
  success case for iteration is external feedback on a concrete named
  defect. So reviewers mark findings BLOCKING, re-check exactly those
  against the revision (never seeing each other, never voting, never
  widening scope), and the cycle is capped (`max_fix_cycles`); leftovers go
  to the close-out as open questions.
- **Browser piloting is core infrastructure** (`browser_pilot.py`,
  playwright is a baseline dependency): a model drives a real page through a
  strict one-action-per-turn JSON protocol, the harness executes and
  observes. Complex flows get the frontend policy's pinned lead
  (brain-trust grade); routine automations downshift to a cheap coder —
  recognising an automation and downshifting is how windows are saved.
- **Two request channels keep one-shot calls honest.** A reply that *is* a
  request gets served and the model re-asked: `FETCH: <artifact-id>` opens
  the full original behind any summary (orchestrator and leads; budgeted,
  `max_fetches`), and a lead may `CONSULT <member>: <question>` — one bounded
  question to one named brain-trust member, answered blind (no draft, no
  session), for cross-expertise input that in-family escalation cannot give
  (`max_consults`, default 2 — a consult is a question, not a conversation).
  Consults never happen inside security excursions, and peers still never
  chat.
- **Questions only the operator can answer go through ASK** — the
  orchestrator emits `ASK: <question>`; the answer becomes a standing ruling
  re-emitted on every render (never re-asked). No channel configured means
  `OperatorInputNeeded` is raised, not guessed around. Three asks per
  decision, then it is interrogating, not deciding (`RunStalled`).
- **The integration gate is the deterministic half of "does it fit
  together"** (`integration.py`): after a task's work is final, the harness
  runs the project's own check command (tests, build). A failure buys the
  lead one fix round with the real output in hand (`max_gate_fixes`); a
  survivor is carried loudly into the close-out. Sequencing + the map keep
  pieces consistent; only execution proves them, and no model is in this
  loop.
- **Frontend evidence comes from a real browser** (`browser.py`, optional
  `playwright` extra): screenshot, console errors (including late throws),
  failed requests. Deterministic and dumb by design — it produces evidence,
  reviewers produce judgement.
