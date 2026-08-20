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
- **Context pressure shrinks the render, never the ledger**
  (`Ledger._fit`, `SessionConfig.context_budget_tokens`). A long session will
  eventually build a body bigger than the window, and the industry default
  answer — summarise the old turns — is the one move this ledger forbids. So
  the render degrades in two steps and rewrites nothing: drop artifact
  previews (all or none), then elide whole entries from the oldest end, each
  replaced by an index line naming its task and artifacts. Goal, standing
  rules, operator rulings and the current question are never trimmed; if they
  alone exceed the budget the render goes over rather than cut. `recent`
  guesses at size, this measures.
- **A task's transcript is flushed to the store before its memory is wiped**
  (`TaskMemory._flush`). The lead's own turns were the one genuinely
  unrecoverable thing here: drafts, reviews and worker output are all kept as
  they happen, but the reasoning between them lived only in the context
  window, so whatever the close-out prose missed died with it. OpenClaw's
  pre-compaction memory flush is the same fix; ours is better in one respect
  — no model is in the loop, so the flush cannot itself be lossy. The
  transcript's reference carries a *description*, not a preview, because a
  preview would walk raw working turns into every orchestrator render.
  `wipe()` still flushes nothing: close is an ending, wipe is an abandonment.
- **Delegation is bounded in time, not just in count**
  (`WorkerBudget.timeout_seconds`, 300s). A budget that counts calls but not
  seconds still lets one stuck worker hold a task open forever. A batch
  deadline (the errands run concurrently, so the wait is the slowest of them)
  turns a stall into a failure the lead can reroute. Two honest limits: a
  Python thread cannot be interrupted, so the hard kill stays with the
  transport's own timeout; and a timeout is deliberately *not* recorded as a
  `RepeatedFailure` fingerprint — a failure is information, a stall is an
  unknown, and re-sending an unknown is not the death spiral.
- **Permissions flow one way: a worker never widens its own access.** The
  `NEED TOOL` channel is a request from the least trusted participant in the
  system, made in text that may be repeating something it just read, so it
  reaches the lead capped (`TOOL_REQUEST_CAP`) and explicitly flagged as the
  worker's own words, and every granted write goes on the task record before
  the call. The number behind this: measured on OpenClaw, one agent's
  probability of compromise was 0.24, but a system that acts when *any* agent
  proposes an action reached 0.86 across seven. Union-of-proposals is the
  compounding step, and a grant is the one thing a worker can end up holding
  that the lead did not type itself.
- **Borrow their mechanisms; refuse their delegated judgement.** Everything
  taken from OpenClaw and Grok Bot is plumbing — flush before you wipe, put a
  clock on a helper, cap what an untrusted participant can ask for. Everything
  refused is a case where they hand a *decision* to the agent that we keep
  written down. That split is not squeamishness, it follows from a difference
  in product shape: their systems run with no operator present and no ending,
  so a rule the AI infers is the only rule that can exist. Ours has one
  operator and a finish line, so an explicit rule is available — and an
  explicit rule can be read, argued with, and corrected. Weight the two
  sources differently too: OpenClaw is open source with several independent
  teardowns, so its mechanisms are checkable; Grok Bot is a closed beta with
  no published reliability data, so it is a source of ideas about shape, not
  evidence about what works. Adopting an unmeasured design choice from a beta
  product is the same mistake as trusting an unprobed model table.
- **Studied and deliberately not adopted, from OpenClaw and Grok Bot.** Each
  is recorded with *their* reason as well as ours, because a rejection with
  only half the argument gets re-litigated every time somebody rereads it.
  - *Heartbeat / always-on runs.* Theirs: work arrives from outside on no
    schedule — mail, calendar, notifications — so an assistant that acts only
    when spoken to is not an assistant; waking up and looking is the product.
    Even OpenClaw defers the wake when the agent is already busy, so the timer
    exists purely to fill idle time. Ours: the work arrives once, as the goal,
    and nothing changes while we are not looking. There is no idle time to
    fill and the run ends at DONE, so a periodic wake spends a window to
    discover nothing happened — and our windows are fixed monthly allowances,
    not a metered bill where a wasted check-in is a rounding error.
  - *Agents routing work to each other by reading each other's name and
    description* (Grok Bot's handoff). Theirs: customers will not write a
    routing table, and being the switchboard between specialists is the
    friction the product removes; their bots do genuinely different jobs
    (inbox vs recruiting vs expenses), so the right owner is usually obvious
    from the label and a wrong guess is cheap and visible. Ours: the brain
    trust is four general coding models that overlap almost entirely, so "who
    takes this" is a judgement about quality, not category — and models carry
    priors about other models, so peers dividing work among themselves would
    divide it by reputation. That is the correlated, reputation-driven choice
    the anonymised reviewer rule exists to prevent, and correlation is the one
    thing that makes four subscriptions worth less than one. It would also
    erase the scoreboard: rotation and the difficulty ladder exist partly to
    record which model actually leads best on this codebase, and a
    privately-negotiated handoff leaves nothing to grade. The orchestrator
    names the lead, and it costs nothing because it is already being asked
    what comes next.
  - *Agents learning when to interrupt for approval.* Theirs: their agents act
    in the world — sending mail, writing to a CRM, spending money — where
    approving everything is unusable and approving nothing is dangerous, and
    the vendor cannot write the rule in advance because what is routine at one
    customer is a firing at another. A learned threshold is the only thing
    that scales across that many jobs. Ours: every action lands in one
    repository, so the consequential set is enumerable in advance and we have
    enumerated it — writes, the check command, security work. Nothing needs
    learning, and a learned threshold cannot be audited: months later there is
    no answer to "why did it not ask me?", only drift. The reviewers of that
    feature found the same failure from the other side — the action that costs
    you is the one the agent classified as routine and never surfaced. So the
    gates stay fixed and legible instead: the plan gate up front, the
    integration gate at the end, ASK as an explicit bounded channel with the
    answers kept as standing rulings, and every write grant on the record.
