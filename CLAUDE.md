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
consumer *subscriptions* (Claude Max, ChatGPT, SuperGrok) by driving the
vendor CLIs, rather than on billed API keys. See
`quadratus/cli_providers.py` for why that is permitted for individual use and
where the line is.

Key design decisions already settled:

- **A coding run is bound to one persistent project.** `--project` / `--repo`
  and the GUI select it. `project_run.run_project` is the shared runner.
  `SessionConfig.project`, `Fleet.project`, provider cwd and integration-gate
  cwd must agree. Never test the launch directory after editing elsewhere.
- **Write permission belongs to each call.** Only authorized editing calls
  get the real project. Orchestrators, reviewers, consultants and close-outs
  get fresh source copies; restricted editing seats return validated text
  patches without widening their tool access. Snapshots are not OS sandboxes.
- **Source and run records are separate.** Source stays in the project;
  `.quadratus/runs/<id>` keeps report, ledger, raw artifacts, usage, diff and
  machine-readable status. Failed or interrupted model runs keep their report.
  Task caps, unresolved findings and failed checks are incomplete outcomes.
- **Public GUI sharing is disabled.** Project controls can access local files
  and execute check commands; API transport does not make those controls safe
  to expose without authentication. GitHub clone and new local branches are
  explicit options. Commits, pushes and PRs are not automatic.

- **Three vendors today: Anthropic, OpenAI, xAI** (`registry.VENDORS`).
  Google left the lineup on 2026-09-12 -- a decision about which
  subscriptions are being paid for, not a finding about the models. Its rows
  live in `registry.RETIRED_ROSTER` so the notes survive while nothing can
  resolve, route to, or pick a model the harness cannot invoke. Restoring it
  is: move the rows back, add the vendor to `VENDORS`, give the difficulty
  ladder its fourth rung.
- **Subscription transport is the default and is not silently abandoned.**
  `DEFAULT_BACKEND = "cli"`; a provider whose CLI is missing reports itself
  unavailable rather than falling back to a billed API key, because an
  unexpected invoice is a worse failure than a clear error.
- **`runtime.Fleet` is the bridge** from roster keys to the CLIs that answer
  them: one provider per vendor (not per model, which would multiply scratch
  directories and lose the vendor's warm prompt cache), aliases resolved per
  call, and an exhausted subscription window turned into a routing fact
  (`available()` goes False) rather than an error to retry. It never
  substitutes a different model: routing decisions are made with reasons
  upstream, and a transport that quietly swapped models would make them
  unfalsifiable.
- **Orchestrator seats address a model line, never a release.**
  `ModelSpec.floating` marks an alias the vendor CLI resolves to its current
  release, `assert_floating_orchestrators()` enforces it at import, and
  `latest.py` resolves one at call time from an operator override, then the
  probe cache, then the seed. The seat is chosen once and held for a whole
  session, so it is the single worst place to freeze an iteration -- the
  failure is silent, and nothing revisits the choice.
- **`quadratus --probe` is how a table of second-hand aliases becomes
  grounded.** Only the Claude CLI has been verified against a live binary.
  The probe walks each floating model's alias chain, records what the binary
  accepted, and reports what did not; a pinned row is tried once and never
  walked, because succeeding on a different release would make the table a
  lie. What a model says it is is recorded and never branched on -- the fact
  being trusted is that the round trip happened.
- **Fable orchestrates; the seat is the hard dependency, not the model.**
  Fable is the only participant that persists across a session; everyone else
  is re-invoked fresh each round. An earlier rule halted the run when Fable
  was unavailable, on the reasoning that substituting it would quietly turn
  the run into a different run. Right about the risk, wrong about the remedy:
  "quietly" was the problem. The seat now passes to GPT-6 Astra once, the
  substitution is recorded on the `Seat`
  (`SeatReason.FALLBACK_UNAVAILABLE`), and it lapses by recomputation the
  moment the primary is reachable. Only an empty chain halts the run.
- **GPT-6 Astra is the orchestrator's only fallback, and there is no third
  seat.** Operator directive. Opus 5 carries ORCHESTRATE and is deliberately
  absent from `ORCHESTRATOR_CHAIN`: it is a brain-trust peer and half the
  pinned reviewer pair, so seating it would make the supervisor a competitor
  in the debate it supervises and put the fleet's strongest reviewer on
  bookkeeping. A longer chain is not a safer one — every extra link is
  another way for a run to change hands quietly, and the link past Astra
  would only ever fire in a state the operator would rather be told about.
  Both seats gone is a full stop (`OrchestratorUnavailable`), even with peers
  sitting idle.
- **One chain, both yield reasons.** A security segment yields the seat
  because Fable's classifiers would refuse the subject, and lands on the same
  single deputy. Astra's own security refusal behaviour is undocumented — an
  unknown, not a clean bill of health.
- **The excursion's verifier is left as it was — operator decision,
  2026-09-12.** Seating Astra makes the deputy and the preferred security
  model both OpenAI's, so `Excursion.verifier` names Astra while the
  pre-existing rule in `session._run_security_task` redraws the actual check
  to Opus via `cross_family_verifier`. The verification that happens is the
  right one; the Excursion record names the seat rather than the checker, and
  that mismatch is accepted rather than fixed. Do not "tidy" it.
- **An excursion refuses to form when its named worker cannot answer** —
  operator decision, same date. `route_security_work` returns the chain's
  last resort rather than raising when everything is down, which is right for
  its own caller; with the deputy no longer a member of `SECURITY_WORK_CHAIN`
  the old `worker == seat` guard stopped catching that, so availability is
  now checked explicitly. Refusing beats letting the invocation fail later at
  transport.
- **The brain trust is one seat per vendor** (Opus 5, GPT-5.6 Sol, Grok 4.6).
  Nobody is displaced from it, and Sonnet is not in it. Its size follows from
  the number of vendors -- when Google left, the seat left with the vendor
  rather than being reassigned.
- **Security work is peeled into a bounded excursion**: the deputy takes the
  seat, GPT-5.6 Sol does the work, verification crosses vendor lines, the
  excursion closes, Fable resumes. Never an open-ended handover.
- **A vendor's catalogue is not what your transport can invoke** (probe,
  2026-09-12). Grok 4.1 Fast, 4.3 and 4.20 were roster rows written from
  xAI's published lineup; all three are real API models and all three are
  rejected by the Grok Build CLI as `unknown model id`. A consumer
  subscription reaches `grok-4.6` and `grok-4.5`, full stop. While they sat
  in `ROSTER` the ROTE ladder rung and the `lookup` worker errand both
  pointed at nothing, and `--status` could not see it because it only checks
  the binary is on PATH — only `--probe`, which actually calls, finds this
  class of fault. The rows moved to `RETIRED_ROSTER`, which now covers two
  cases: a vendor left, or a model exists somewhere this transport cannot
  reach. xAI's seats are now `grok:default` (brain trust, no model flag),
  `grok:grok-4.5` (ROTE rung and the lookup errand) and `grok:default` again
  as the in-family escalation above 4.5. Grok 4.5 is not a cheap tier and is
  not documented as one — it is the older flagship, kept on that rung by the
  load-spreading argument rather than by price.
- **A seat is an invocation, not just a model** (`CLISpec.restricted_args` /
  `.agentic_args` / `.effort_flag`, `ModelSpec.effort` / `.restricted`), and
  it applies to all three vendors. Every one of these CLIs is an *agent* by
  default, so a worker bee was running a full agent loop everywhere; grok
  merely made it visible by reporting the bill. Each vendor spells the dial
  its own way and the roster row says only `low` or `high`: claude takes
  `--effort` (levels `low…max`) and denies `Task` to block subagents; codex
  has no flag and takes `-c model_reasoning_effort=` (a misspelled key is
  rejected, which is how it was confirmed to land) with `--sandbox read-only`
  as its only lever; grok takes `--effort` (`low…xhigh`) plus a tool
  allowlist. Senior seats — orchestrators, leads, brain trust — are never
  restricted, because bounding those is not a saving but a different job;
  every seat in the worker tree is. Two tests pin that split so it cannot
  drift. **`always_args` and `agentic_args` are deliberately separate**: the
  first survives into a restricted call (codex's `--skip-git-repo-check` is
  mandatory — the scratch directory is not a repository), the second is
  exactly what a restricted seat withholds (grok's `--always-approve`).
  Collapsing them broke every restricted codex call in the first draft.
- **Escalation buys reasoning depth, not more tools** — provisional. An
  in-family bump now runs the same bounded call at `high` effort, so an
  errand that failed because it genuinely needed to *run* something still
  fails. Coherent, not yet verified against real escalations; revisit when
  there is output to look at.
- The original grok finding, for the reasoning: `readonly_args` / `write_args`
  express *permission*, and nothing expressed *agenticness*. The grok CLI is
  a coding agent — asked for a one-line function it explored the tree, wrote
  files, spawned subagents and re-sent the conversation each turn: 120K
  tokens, against 22K for the orchestrator supervising it. The same model
  line called as a bounded worker did it in ~6K. So xAI now holds three seats
  that differ only in how they are called: `grok:default` (full agent, effort
  high) in the brain trust, where exploring the tree is what a peer is *for*;
  `grok:worker` (tools reduced to `read_file,grep,list_dir`, no subagents,
  effort low) on the ROTE rung and the `lookup` errand; and `grok:expert`
  (same bounded call, effort high) as the escalation above it. Escalation is
  a reasoning step rather than a different model because a consumer
  subscription reaches one model line — the honest version of the old Grok
  Fast → Grok 4.20 bump, whose two IDs this transport rejects.
- **Every grok seat sends no `--model` flag at all.** Both IDs that broke a
  run — `grok-4-1-fast` and `grok-build` (from a table Grok itself wrote) —
  were pinned names that are real on the billed API and rejected by the
  subscription CLI. There are none left: all three seats are
  `vendor_default`, so the operator directive "never tied to an iteration"
  now holds across the whole vendor, and `grok-4.5` left the roster because
  effort tiers removed the need for a second ID.
- **A restricted call delivers its prompt in argv, and refuses rather than
  degrading.** `--tools` and `--disallowed-tools` are honoured only in
  headless `-p`; under `--prompt-file` they are ignored *in silence* and the
  call returns a cancelled agent turn — which is how read-only was first
  concluded to be impossible here. Over `MAX_ARGV_PROMPT` the provider raises
  instead of falling back to a prompt file, because that fallback would drop
  the restrictions with it and turn a bounded worker into a full agent with
  writes, invisibly.
- **Grok has no read-only mode in the sense the other two vendors do**, and
  the scratch directory remains the containment for an unrestricted seat.
  `--always-approve` is required for tool use at all (`--permission-mode
  acceptEdits` does not cover it; `plan` cancels the same way) and it
  overrides `--disallowed-tools` outright — asked to create a denied file
  under both, it created it and ran a shell check on it. Hence
  `readonly_args` is empty *on purpose*: encoding a denial that experiment
  disproved is worse than admitting there is none. The restricted seat gets
  its safety a different way — the write tools are *absent* rather than
  denied, so there is nothing for an approval flag to approve.
- **The shipped vendor docs are a seed too.** In one session `grok`'s own
  110KB README and `--help` disagreed with the installed binary three times:
  `--agent-profile` does not exist, two conflicting tool-ID tables, and seven
  advertised `--effort` levels of which four are accepted (`low, medium,
  high, xhigh`). The rule that probes outrank tables was written for model
  rosters; it applies to flag documentation exactly as hard.
- **Grok answers must be read out of a JSON envelope.** `--output-format
  json` is in `output_args` and the extractor rejects any turn that is not
  `end_turn`. Without the flag the CLI streams an agent's narration and the
  provider returned whatever it was mid-sentence about: a live session closed
  a ROTE task on 119 characters — "I'll implement add(a, b)… checking the
  workspace" — with no function in it, reported as success. A cancelled turn
  is the same trap wearing a different hat, since it still carries the
  preamble in `text`, so an unrecognised stop reason is treated as failure
  rather than assumed benign. The envelope also carries real token counts, so
  grok calls meter as `measured` instead of ~4-chars/token estimates.
- **All three CLI specs are now probe-verified.** `GROK_SPEC` carried
  `verified=False` until signed-in calls on 2026-09-12 showed it both
  round-trips a prompt and rejects an unknown model id loudly. A flag set
  read off a `--help` page is a hypothesis; the provider warns on every call
  until someone has made one.
- **The worker tier holds no role in security, verification, or refusal
  re-routing.** Sonnet was second in both `SECURITY_WORK_CHAIN` and
  `REFUSAL_CHAIN`, ordered there by classifier aggressiveness -- it carries
  none, so it cannot false-positive. Removed from both on 2026-09-12 by
  operator directive: carrying no classifier is an argument for not being
  *refused*, not an argument for being trusted with the work, and a declined
  request is where that distinction matters most. Security is Sol then Opus;
  refusal re-routing is Opus, then cross-vendor.
- **Sonnet may hold the interview, never conduct it** (operator directive,
  2026-09-12). `CONTROL_PLANE` entries are `ControlPlaneRole` records, and
  the interview's carries `supervised_by="orchestrator"` plus a mandate: the
  orchestrator writes the questions, reads every answer, and decides each
  follow-up; the interviewer asks what it is given and reports back verbatim.
  The interview is not yet implemented — the constraint is in the table so
  whoever implements it cannot wire the model up and call it done. A cheap
  model is cheap enough to *be* the voice and not trusted to decide what gets
  asked, which is not a contradiction.
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
- **The Grok seat is not tied to an iteration either** (operator directive,
  2026-09-12). Its roster row carries `vendor_default=True` and the key is
  `grok:default`: the CLI is handed *no model flag*, so xAI's own default
  applies and moves when they move it. That is stronger than a floating
  alias — nothing to guess, nothing to probe, no table to edit — and it is
  available because `grok models` prints a model marked `(default)`. An
  operator override still outranks it. `GROK_CLI_MODEL_HIGH` is empty for the
  same reason; putting an ID there pins the seat against the vendor's choice.
- Model tables are seeds, not truth. The lineup churns monthly — prefer a
  probe against the installed CLIs over anything hardcoded. `quadratus
  --probe` is that probe, and `QUADRATUS_ALIAS_*` / `QUADRATUS_CLI_ARGS_*`
  let an operator correct a wrong alias or a wrong flag from `.env` rather
  than by patching Python. Two of the three CLI specs are still written from
  documentation.
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
  (the bulk of well-sized tasks) → the current Grok, rote → Grok 4.5. The
  earlier kind-pinned table concentrated nearly everything on two
  subscriptions while the others sat idle — exactly how one window exhausts
  early and forces a degraded run. Four rungs across three vendors means one
  vendor takes two, and it is xAI: Anthropic carries the primary seat and
  every complex task, OpenAI carries standard plus the pinned kinds plus the
  fallback seat, xAI was carrying one rung. An unavailable or excluded rung
  escalates upward before it degrades downward. Surviving pins: security and
  testing → Sol (operator directive), review → the Sol+Opus pair,
  scope/decompose → Fable. Revisit rungs on the Grok 4.7 release.
- **An exclusion that names a model outside the lineup is deleted, not
  kept.** Mobile carried the table's only directly verified exclusion (Gemini
  3.1 Pro, ~20 points behind on a real Android benchmark). When that model
  left, the exclusion left with it: an exclusion that cannot fire still reads
  as a live finding, and outlives the evidence behind it. The evidence is
  recorded in the row's `evidence` line instead.
- **An empty `prefer` is a statement, not an omission** — it means the kind
  rides the ladder rather than anyone having earned a pin.
- **Workers are picked by errand, not vendor loyalty** (`workers.WORKER_TREE`):
  lookup → Grok 4.5, read → Luna, visual/check/code → Haiku,
  format/draft → Luna. Picking by skill also spreads the windows. The
  same-vendor default was retired: caches stay warm through regular use,
  which the tree guarantees. When Google left, its two errands moved on
  different reasoning — long reading to Luna, which has the widest cheap
  window *that is corroborated by something other than its own vendor's
  marketing*, and visual to Haiku, because the Claude CLI reads image files
  off disk and that capability, not the window, is what the errand needs.
  **Escalation stays in the family** (`WORKER_ESCALATION`): a demanding
  errand bumps one tier up its own vendor's line — Haiku→Sonnet, Luna→Terra,
  Grok 4.5→the current Grok — so the skill stays matched while the horsepower
  rises. The one unverified bump target is acceptable because escalation
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
  dialogue, no orchestrator involvement. Terra and Sonnet serve only as
  in-family escalation targets, never as base workers; xAI's line is two
  models long, so its base worker escalates to the vendor default.
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
- **An advertised window is not a capability.** Grok 4.20's 2M was
  unverified vendor marketing and deliberately never carried `LONG_CONTEXT`;
  the row is retired but the rule is cited live by the Astra row, which is
  held to it. Restore a tag when a probe at depth earns it, not before.
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
- **An exhausted seat re-asks once rather than ending the run** (operator
  decision, 2026-09-12). Availability is *learned by calling*: nothing knows a
  subscription window is spent until a request says so, so the first call of a
  run discovered the primary was out and the run died on the very failure the
  fallback exists for. `Session._ask_seat` treats an exhaustion as the
  liveness check arriving late — the transport records it, the seat is
  recomputed, the question is re-asked, exactly once. Recognised by a
  `window_exhausted` attribute rather than an exception class, because
  `session.py` is handed its `invoke` and must not learn what is behind it.
  Verified live: Fable 429 → seat fell to Astra → the run continued.
- **The session engine runs from the command line**: `quadratus --session
  "<goal>"`, with `--plan-gate`, `--check`, `--max-tasks`, `--mode`,
  `--allow-writes` and a `--state-dir` (default `.quadratus`) holding
  artifacts, the codebase map and the usage log. The four-phase pipeline is
  still what a bare `quadratus "<goal>"` runs; switching that default is an
  operator decision, not a side effect of the engine becoming usable.
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

## Model routing: this repo is build-time tooling

`df-model-routing` audits **shipped products** against the Haiku/Sonnet/Opus
runtime policy. It does not apply here, and a routing audit should skip this
repo rather than file findings against it.

Two reasons:

- **Multi-vendor is the product.** This tool exists to put Claude, GPT and
  Grok on one task and have them argue. Calls to non-Anthropic models are
  the feature, not a policy violation.
- **Fable 5 holds the orchestrator seat on purpose.** `routing.py` and
  `ORCHESTRATOR_CHAIN` are deliberate, and `registry.py` documents the hazards
  that were weighed (strictest safety classifiers, 2x Opus 5 against the same
  subscription window, the 19-day June 2026 withdrawal). The
  "Fable is never a runtime dependency" rule protects Flex, LINDA, and
  R3CRUIT3R, where a suspension would hit paying customers. This runs on
  Davis's machine against his own subscriptions.

Do not "fix" either of these. See `docs/routing-and-local-model-proposal.md`.
