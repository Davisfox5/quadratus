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
- **A channel the prompt offers is a channel the harness serves** (Q9
  canary, 2026-09-22). The security excursion advertised WORKER and then
  bypassed the channel loop; the baseline lead answered as instructed and its
  request was filed as the draft. `_run_security_task` now drafts through
  `_draft_with_channels(consults=False)`: FETCH and WORKER served, CONSULT
  refused in words. And **a blocked report is evidence or it is nothing**:
  the lead must quote the command, exit status and verbatim error
  (`_BLOCKED_REPORT_RULE`), and the ledger keeps the CLI's own `stderr_tail`
  and failed `tool_failures` per attempt so the claim can be checked later.
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
- **A runtime-dependency tree is part of what a check proves**
  (`deptree.py`, contract v2 on #25). Run 19: a lead wrote a
  `node_modules` shim the source checks could not see, and the project's
  check resolved it. Every `node_modules`, `.venv`, `venv` and `env` in the
  project is content-hashed at run start and must hold before and after
  every editing call, check and preview, at settlement and at DONE; any
  difference is `DependencyTreeChanged`. Bounds fail closed (250,000
  entries, 2 GiB read, 120 s per pass). No allow flag; only an operator's
  hidden cache path inside a tree may be exempt, and none is by default.
  Nothing outside the project is guarded. The lead's cap
  (`lead_max_turns`) binds revision, gate-fix and design-fix as well as the
  draft; a capped fix is one attempt spent and the checks still decide.
- **The measured diff is the truth; the CHANGED line is the lead's account of
  it** (operator ruling, 2026-09-28, `docs/DIRECTION.md`). Fleet diffs the
  project around every editing call, so it already knows what changed. The
  declaration was required to equal that diff exactly or the run stopped,
  and four live runs ended on the declaration's shape alone (runs 5, 9, 11
  and the phase-4 run, where a revision re-listed a file its own draft had
  changed). `runtime.changed_report` now classifies the line (missing,
  malformed, undeclared, overdeclared, misplaced) and the session records a
  disagreement as a non-terminal `unverified` fact with the reply kept; the
  scope, gate and design checks read the diff, so nothing on that line can
  widen what was measured. The one stop that survives is a request line that
  failed to parse with no CHANGED line: neither a request nor a delivery
  (run 9), never filed as a draft.
- **A rule the orchestrator is held to is a rule its prompt states, and the
  one correction states all of them** (phase-4 rerun on a6c9576,
  2026-09-28). The rerun closed two tasks, then the orchestrator declared
  capture steps as `{"click": ...}` objects, spent its single correction on
  the schema, and named the sample its previous task had just committed
  under `tests/fixtures/`; the fixture-path rule appeared only in the
  validator's error, and the run stalled. `_CAPTURE_SCOPE_REQUEST` now
  spells out the step shape and both valid upload paths, the scope
  correction appends it, and the scope and dispatch gates accept a
  committed non-hidden project sample exactly as the capture itself
  (`design_evidence._fixture`) and the lead's instructions already did. Three
  gates with three rules was the defect; there is one rule now.
- **A task that breaks its own rules fails the task, not the run**
  (operator ruling, 2026-09-28; `session.TaskFailed`). Two live runs each
  ended on the first task-level fault, so every later task's faults stayed
  unseen and each run could teach one lesson. Scope overrun, a reply that is
  neither a request nor a delivery, a lead channel that does not converge,
  and a transport stop after writes now take the capped-task path: work
  kept, a terminal `failed` fact on the task, the orchestrator told to name
  the remaining work with CONTINUES, a clean continuation recovering the
  fact, and two unfinished tasks in a row tripping the same breaker a cap
  does. What still stops the run is what no re-plan repairs: a refusal, a
  denied capability, a tree the harness cannot inspect, dependency or
  evidence integrity, a spent budget, an operator question, and the
  orchestrator's own stalls.
- **A check that still fails after its fix round is requirement debt, not
  a run stop** (survey plan, 2026-09-30; `_record_check_debt`). With the
  ledger on and a COVERS line, the task's requirements go NOT MET under a
  `check.failed` finding, the orchestrator sees it with the check's output
  artifact and names a RESOLVES task, and that task settles it when its own
  integration check passes and it closes with no new finding; no renders
  are asked for. The original task keeps its failed check on record; its
  `product` facts become history when the finding resolves. Without the
  ledger or a COVERS line the `CheckFailing` stop stands. Unverified design
  evidence is not routed this way yet: map G8 says only the task's own
  renders discharge it.
- **A survey run continues through failures and reports them apart from
  acceptance** (Davis, 2026-09-30; `SessionConfig.survey`, `quadratus
  --survey-recovery N`). Synthesis of Davis's idea with Codex's and Claude's
  answers on #39: the orchestrator gets no discretion over budget. The
  operator sets a recovery allowance of N continuation or repair tasks in
  advance and the harness spends it; the next repair past it is
  `SurveyAllowanceSpent`. The two-unfinished breaker gives way to a
  same-cause repeat stop (`SurveyRepeatStop`: a continuation that fails
  the same way as its predecessor with nothing newly changed), so distinct
  failures keep the run going. Every re-plan after an unfinished task or an
  open finding must carry `HYPOTHESIS: <what the failure showed and what
  changes>`, kept as an artifact and in `result.json`'s `survey` section
  beside the harness's own record, never acted on. The acceptance verdict
  is computed exactly as on an ordinary run; the survey section (unique
  causes, recovered vs open, repeats, allowance spent, hypotheses) is
  diagnostic data, not success.
- **Prompt parity is audited, not assumed** (2026-09-28). An inventory of
  every site that rejects or stops on how a reply is written, checked
  against the prompt each seat receives, found 15 rules never stated, 24
  stated in part and 3 stated backwards (the bounded editor was offered
  lead channels the worker pool refuses; the verifier was told prose is a
  note while capitalised BLOCKING in prose counts). All are stated now, the
  bounds are quoted from the constants the parsers enforce
  (`MAX_PREFACE_LINES`, `MAX_REQUEST_PREFACE_LINES`, `MAX_STEPS`,
  `MAX_SELECTOR_CHARS`, `_MAX_ASKS_PER_DECISION`, the parallel limit from
  config), and `tests/test_prompt_rule_parity.py` pins each statement to its
  parser. A new enforcement site gets its prompt sentence and its guard test
  in the same change.
- **The direct execution tier is opt-in and admitted by facts, never by
  the label** (first slice, 2026-09-30; `--direct-tier`, `Required.tier`).
  The diagnostic run's favicon task spent a draft collaborator, a revision
  round and a model close-out on a four-file change. The orchestrator may
  label a task `TIER: direct`; `_direct_refusal` admits it only when writes
  are granted, SCOPE names exact paths outside dependency trees and
  policy-denied or sensitive paths, `max_lines` is within
  `direct_max_lines`, the task is not an audit, security, review, RESOLVES
  or CONTINUES task, and the run has a check. The tier is fixed on the
  contract at dispatch with the refusal reason; an admitted task draws no
  draft collaborator (so no revision round), gets a harness-written
  close-out (no map notes), and keeps every check, the harness capture, the
  cross-vendor design review where it applies, scope measurement and every
  stop. `result.json.workflow.calls_by_task` records calls, tokens and
  seconds per task and role, so a saving is read from what ran.
- **Direct edits bind to exact file identities** (Codex on #42, P2). A
  bare directory in SCOPE is a prefix grant, so admission refuses a path
  spelled with a trailing slash or that exists as a directory, and an
  admitted task's scope is marked `TaskScope.exact`: each permitted path
  permits only itself, so a declared file that becomes a directory puts
  every descendant out of scope. The normal tier keeps prefix grants.
- **The explicit-task entry runs the operator's list with zero orchestrator
  calls** (`run_project(tasks=[...])`, `--tasks FILE`, 2026-09-30). Each
  text is read by the orchestrator reply's own parsers (KIND, SCOPE, TIER,
  COVERS) under the runner's lifecycle: lock, gate plan, budget, readiness,
  dependency watch, every per-task check and stop. No planner call, no
  acknowledgment call, no task the list did not name; a text the loop would
  send back is `TaskListInvalid`, found for the whole list before any call
  with every problem named at once (live run 20260930T134226Z: the ledger
  is on by default, so the first text needs a `REQUIREMENTS:` block and
  every text a `COVERS:` line, and the operator's list had neither; the
  refusal landed after launch and the record said the task "ran"). `ran`
  is now recorded at dispatch, and a list longer than `max_tasks` is
  refused because the cap's goal question is an orchestrator call. The
  list ending is not a DONE: `completed` means every listed task closed
  clean and every requirement a listed task claimed audited met;
  requirements no task claimed are recorded as `requirements_unclaimed`
  and left unjudged (live run 20260930T141209Z: the favicon task closed
  clean on the direct tier with zero orchestrator calls and the
  end-of-list audit marked the goal's baseline requirements NOT MET), and
  `result.json.explicit_tasks` says `goal_judged: false`. Task completion
  and whole-goal proof stay distinct. The same run showed the audit prompt
  never carried the design review's verdict, so the auditor reported
  approval absent; each design evidence line now names the reviewer and
  verdict.
- **An external decider answers only what the orchestrator left
  unlabelled** (2026-09-30, `quadratus/decisions.py`,
  `docs/decisions-api.md`). Decision-only models (TypeSafe AI's Jev; OpenAI's
  Decisions API) return one of a fixed answer set with probabilities, which
  is the shape of three routing decisions here: task difficulty, task kind,
  worker escalation. None of the parsers and not the tier admission: those
  are rules the prompts state and the tests pin. `--decider jev` is wired
  against the vendor SDK's contract (`typesafe-sdk`, `TYPESAFE_API_KEY`,
  billed and metered under `jev:<model>`), consulted for kind and
  difficulty only on a defaulted or degraded route, never overriding a
  stated label; every verdict or refusal is on `result.json.decisions` and
  in the task's record, and a refusal keeps the rule's default. OpenAI's
  Decisions API (DevDay, 2026-09-29) has no published endpoint, schema,
  model id or pricing, so its decider is a placeholder that refuses with
  what is missing until the contract lands.
- **The decider is asked with definitions, never bare names**
  (`quadratus/decision_labels.py`, 2026-10-01). The first live run sent
  twenty label names with null criteria and got `backend` for a favicon
  task that touched a route, an SVG, a template and a test. Every kind and
  difficulty answer now carries a one-line definition with an example from
  this repository, the question carries a stated tie-break for mixed work,
  difficulty is defined by reasoning burden and dependencies rather than
  line count, and `LABELS_VERSION` rides on every decision record. The docs
  section is rendered from the same table and a test holds them equal.
  Routing, stated-label precedence, admission and the budget are unchanged:
  this changes the question, not what the engine does with the answer.
- **Four repairs from the rule-only series on b1ff751** (2026-10-06, seven
  cells, 34 of 35 requirements passed, one engine-complete). Each was
  reproduced at least twice before it was touched.
  - *A per-call ceiling implies a lead turn cap.* Claude leads ran 27 and 36
    turns in one call, re-sending ~58k of context per turn, and the 1.5M
    post-return ceiling fired after the spend. `run_budget.lead_turns_for`
    derives a cap (1.5M → 20 rounds) when the operator set none, and the
    lead is told its round budget. The ceiling's arithmetic is unchanged.
  - *A disposable source copy is its own containment.* Under codex
    `--sandbox read-only` Python found no writable temp dir, so a reviewer
    told to run pytest could not and blocked the task with every grader
    passing. `CLISpec.copy_args` (codex: workspace-write) applies only to an
    ungranted call in a copy the Fleet deletes, with `/tmp`, `$TMPDIR` and
    configured roots switched off so the copy is the only writable root,
    and `TMPDIR` points inside the copy. The project root is still reached
    only with a write grant.
  - *A render that misses the change is a capture defect, not a design
    one.* The dispatched capture never reached the empty state (f5, three
    leads). The reviewer's exact reply buys one `CAPTURE:` redeclaration
    from the lead, one recapture and one more review; a second miss is the
    open finding it was, and a verdict that names any other blocker gets
    no recapture at all.
  - *The verifier ends with a VERDICT line.* An accepting security report
    was filed as the open finding because a marker appeared somewhere in its
    prose. `VERDICT: ACCEPT` or `REJECT` decides; ACCEPT still loses to a
    prefixed `BLOCKING:` with content; no line falls back to the scan.
- **An elided call is not a signature** (series rule-2ffa7f6, f6, 2026-10-06).
  `scope.declared_signatures` collects every backticked `name(args)` for a
  `def`-declared name so a mid-description revision is caught before
  dispatch. It also collected `_serve_guarded(...)`, a reference to the
  function, as a second signature; the correction round then named `(...)`
  as the contradiction, the orchestrator could not act on that, and the cell
  stalled on three calls with no lead invoked. An argument list that is only
  `...` or `…` says nothing about the parameters and is skipped; a real
  second signature beside an elision is still refused.
- **Review seats share the lead's turn cap, and a capped review is no
  verdict** (series rule-2ffa7f6 f3 and f5, 2026-10-06). Every editing call
  was bound by the derived 20-round cap while the Opus collaborators ran 15
  to 20 rounds uncapped at 0.87M to 1.18M tokens, and both cells stopped on
  the token threshold. `runtime.REVIEW_CAPPED_ROLES` (collaborator,
  recheck, design-review, verifier) now takes `Settings.lead_max_turns` too;
  closeout, workers and the orchestrator stay uncapped. A reviewer that hits
  the cap has given no verdict: its narration is never read as findings, the
  task carries an `unverified` fact naming the reviewer, a capped recheck
  buys no fix round, a capped design review is "gave no verdict" rather than
  a design defect, and a capped verifier leaves the verification edge unset.
  Review copies also get the editing call's exact check guidance (#51) plus
  the fact that the copy has no `.git`: f1's reviewers ran `node --test
  tests/ui/` in place of the five listed files, and `git status`.
  The cap is a transport fact tracked beside the loop, never read back from
  the reply (Codex review of 0983dac: a prefix filter dropped an ordinary
  reply with a BLOCKING line as if capped), and a capped recheck keeps the
  reviewer's whole original finding on the record as unverified. **A reply
  that arrived is not a review that finished**: a collaborator's reply
  counts as a verdict only as NO FINDINGS, at least one BLOCKING finding,
  or a closing `REVIEW: COMPLETE` line, which the prompt asks for; anything
  else is recorded as an unfinished review (unverified) and still reaches
  the lead as a note. A marker as written, like the verifier's VERDICT
  line, never a reading of the prose.
- **The lead is told what the harness measures, not only the estimate**
  (series rule-2ffa7f6 f3, f4 and f7, 2026-10-06). The decomposition prompt
  already tells the orchestrator that test lines count in full, and it
  sized first tasks at 85 to 100 lines; the leads then wrote 112 to 171
  test lines beside 23 to 65 code lines and every first task stopped past
  the 1.5x line. `TaskScope.render()` now states the counting rule, the
  exact stop line and "compact named cases", so the seat writing the tests
  knows the bound it is writing against. The estimate rules and the stop
  itself are unchanged.
- **A summary-only call reserves its own shape** (series rule-3f9c548 f3,
  2026-10-06). The pre-call reserve was one size for every call, so a
  finished task's 20k close-out was refused on 160k of headroom because the
  operator's reserve for an agentic call is 500k. The close-out is bounded
  in one respect only: a 32,000-byte prompt and, where the CLI argv carries
  `--max-turns 1` (claude, grok; `CLISpec.summary_turn_capped`), one model
  turn. Nothing caps its output at the CLI (the view's `max_tokens` is not
  emitted in argv; saved close-outs reported up to 1,629 output tokens), and
  codex's summary call has no turn flag at all. So the 64k
  (`SUMMARY_CALL_RESERVE_TOKENS`) is a measured allowance, about 2.7 times
  the largest saved close-out (24,006), not a hard bound, and it is passed
  only where the one-turn cap is enforced; codex close-outs reserve the
  operator's figure in full (Codex review of 961d2da). It never raises a
  reserve, every other call reserves the operator's figure in full, the
  post-return threshold is unchanged, and the snapshot counts
  `shaped_reservations`.
- **The Stage B cell budget is 4M and the reserve 250k** (Davis,
  2026-10-06: "the reserve and the overall token count are now in play").
  On engine 3f9c548 the cells that finished (f6, and f7 on 2ffa7f6) used
  about 0.8M; the cells that reached a review cycle stopped at 2.0M to
  2.3M with the cycle unfinished, and finishing them needs one revision,
  one recheck and a close-out more, about 0.3M to 1.0M on the measured
  sizes. 4M covers that with the 1.5M per-call ceiling still bounding a
  single call. The reserve history follows.
- **The Stage B reserve is 250k, not 500k** (Davis, 2026-10-06, after
  f1 to f4 of series rule-3f9c548 all stopped on `reported_token_reserve`
  with their work unfinished). The reserve says what the next call is
  expected to cost; on this engine the calls that follow a draft measure
  128k to 378k (revision, collaborator, design review) and 20k to 42k
  (close-out, recheck), and a 500k reserve refused them on 160k to 300k of
  headroom. 250k lets a fix cycle finish; the 2.5M threshold and the 1.5M
  per-call ceiling still bound the overshoot. The ruling was to keep
  getting results without burning every token, not to remove the bound.
- **A capture-only sample is dictated by the lead and written by the
  harness** (series rule-3572b72 f1 t3, 2026-10-06). The built-in policy
  tells every builder "never change `.quadratus/**`" while the capture note
  said "you must write `.quadratus/capture-fixtures/<task id>/…`". The
  Opus lead of t2 wrote its sample anyway; the Sol lead of t3 obeyed the
  ban, the capture exited 2 on the missing file, and that path spent no
  call, so three clean tasks ended as `DesignUnverified`. The note now says
  not to create it. Before the harness captures, `_missing_own_fixtures`
  lists the declared own-fixture paths that do not exist and
  `_supply_fixtures` asks the lead once (role `fixture-supply`, read-only
  copy) for `FIXTURE <path>:` plus a fenced block per file; the harness
  validates the path against the declaration and the size against
  `MAX_FIXTURE_BYTES` and writes it. An unexpected path is never written;
  a missing, oversized or duplicated block, or a reply whose fence
  swallowed the next header, leaves the capture to fail as before. The
  write is confined before it happens (`_confined_fixture_target`): exactly
  `.quadratus/capture-fixtures/<task id>/<name>`, no symlink at any
  component, the resolved target inside the resolved project (Codex review
  of 351d3ba: four linked layouts had written outside the project before
  the capture's reader could refuse them). No write grant changes, and a
  committed sample elsewhere is still checked by the capture itself.
- **A final wait that was visible at load is a declaration defect and
  gets a redeclaration, never a design-fix** (series rule-3572b72 f2 t1,
  2026-10-06). The qualifier rightly refused both renders because the
  declared final wait (`#project-search`) was visible before any step ran,
  and the engine spent its one design-fix call asking the lead to fix the
  source, which was sound; the call ran to the 20-round cap at 961k tokens
  and the run ended `DesignUnverified`. `check_records` now reports that
  case under `CAPTURE_DECLARATION`, and `_check_design` answers a record
  set that is only that kind with the one `_recapture_declared` round the
  blind review gets (the prompt names the capture check as the source).
  Still wrong after the redeclaration, or `CAPTURE: none`, stays unverified
  with no fix call spent; a page problem beside it still gets the fix.
- **A one-line answer is bounded like one, and the marker is read wherever
  it sits** (series rule-3572b72 f5, 2026-10-06). The recapture round from
  the earlier f5 fix fired, the grok lead's capture-redeclare call ran 12
  rounds and 403k tokens in the read-only copy, and its reply held
  `load.CAPTURE: {...}` after a sentence on the same line; the line-anchored
  parser recorded "no CAPTURE: line" and the blocking verdict stood.
  `runtime.DECLARATION_ROLES` (capture-redeclare, fixture-supply) now take
  `DECLARATION_MAX_TURNS` (6), never above the operator's lead cap, and
  `_capture_declaration` reads the one `CAPTURE:` marker wherever it sits
  on its line, with the JSON object decoded from where it starts; two
  markers, or a second object after the first, are a choice the harness
  never makes and are refused without a call (Codex review of 351d3ba). A
  reply with no marker still stands as the verdict it was: one bounded
  declaration round and no second call because parsing failed (Codex,
  terminal audit of rule-3572b72), so there is no re-ask. The one
  redeclaration is shared by both routes that can ask for it (the capture
  check and the blind review): after it is spent, a blind review stays
  unverified, and the first receipt is kept as written.
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
