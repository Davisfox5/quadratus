# Working notes for Claude

## Communication

**No TL;DRs. Ever.** Davis's standing instruction (2026-10-01): explain
everything in succinct, simple, plain English. One short explanation, not
a summary followed by the same thing again. Lead with the answer, keep it
short, no headers or bullet walls on short replies, no closing offers.
Technical detail only when it changes what Davis does next.

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
- **The installed product starts the way the README says** (Codex
  installation assessment on 4a273a3). Setup installs the package (`pip
  install -e .` in a venv, Python 3.11+), since only that puts `quadratus`
  and `quadratus-gui` on PATH; `requirements.txt` is for source work. The
  GUI's Project tab carries the CLI's `--capture-profile`, `--extra-check`
  and `--readiness` inputs; `quadratus-gui --port N` or
  `QUADRATUS_GUI_PORT` chooses the port and a busy port is reported with
  the way out. `Settings.__post_init__` refuses numeric settings no run can
  use (non-finite or non-positive timeouts, a negative delay, token or
  retry counts below their floor) naming the variable; unparsable text
  still falls back to the default, which a test pins.
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
  gap between reviewers. The decomposition prompt therefore carries a size
  guide (`task_kinds.TASK_SIZE_GUIDE`, ~100 lines), and that is the single
  highest-value instruction in the loop. **Since 2026-10-20 the guide is
  advisory** (operator directive, Davis): see the note on advisory sizing
  below. The harness measures and reports; it stops nothing for size.
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
- **A test assertion is one raised through the test's own frame** (Codex
  attribution assessment on 4a273a3). Pytest reports a failure from a
  `pytest_runtest_call` or `pytest_pyfunc_call` hook in the call phase, so
  a hook that asserted before the body ran was attributed as a product
  failure and could buy an application repair. The producer
  (`quadratus-pytest/4`) records `body`: whether the exception's traceback
  passes through the unwrapped test function's code object, and
  `assertion` now requires it. Expected-exception failures (`pytest.raises`
  with nothing raised, `builtins.Failed`) stay unattributable: that is a
  stated capability gap, not something this change widens.
- **The GUI can bound a run, and off is the old run** (Codex GUI plan on
  #35, 2026-10-09). The Project tab's Run limits form builds the existing
  `RunLimits` and `SurveyConfig` in the callback, refuses a bad field by
  name before any project or provider is touched, and forwards both through
  `run_project_ui` to the shared runner. A preset only fills the fields.
  `project_run` writes `run-limits.json` before the first call and repeats it
  as `selected_limits` in `result.json`, with one plain wording
  (`run_budget.describe_limits`) that never calls a post-return threshold a
  ceiling. Limits off and zero recovery passes `None` for both, exactly the
  call made before the form existed. The wording states what the engine
  does, not a simpler promise: a one-turn summary call reserves
  `min(reserve, SUMMARY_CALL_RESERVE_TOKENS)`, and the record carries the
  lead turn cap the session is built with and its source
  (`run_budget.effective_lead_turns`, the same rule the runner applies).
- **Capture-only samples belong to one run** (Codex review on #53,
  2026-10-09). Task ids restart at t1, so a sample an earlier run left under
  `.quadratus/capture-fixtures/t1/` counted as supplied for the next run's
  t1. Before any model call `project_run` renames it to
  `.quadratus/capture-fixtures.retired-<run id>`, through a directory handle
  on the project's own `.quadratus` opened without following links: never
  deleted, never a source change, never a pathname move (a `.quadratus`
  swapped for a link between a check and a pathname move relocated another
  project's fixtures; Codex review of 391f3c8). A linked `.quadratus` or
  `capture-fixtures` is left alone and named; any other failure stops the
  run before a model call, and the record says whether the retired path
  still leads to the samples (Codex review of 6a338a1), read through the
  project pathname itself, so a project folder replaced as a whole reads
  false (Codex review of 7515f28). A capped design review still records the
  delivery: the model ran, so the renders reached its copy.
- **A task record says what happened, not what the stage list implies**
  (batch 2 recovery-v2 on 5d9f5ff). Three simple tasks drew no collaborator
  (`Complexity.collaborator_count`) yet recorded `review: true`, and each
  clean close carried the stopped-call note "already on disk when the call
  stopped". The review edge is now `None` when no peer reviewed, and a clean
  close says "The task closed with these changes on disk"; a capped or
  failed close keeps the stopped-call wording. Reporting only: who reviews
  and what completes are unchanged.
- **A child the transcript names is on the delegation record** (batch 2
  recovery-v2 on 5d9f5ff). Claude's JSON envelope carries no tool calls, so
  the orchestrator's `Agent` child (it ran the checks itself) was invisible
  to the provider while the trace recorded `Agent: success`, and the record
  said zero native children. `project_run` now files each `Agent`, `Task`
  or `spawn_subagent` call a saved transcript names as an unidentified
  child with unknown usage: its spend is already inside the parent's
  reported total, so nothing is added, guessed or attributed, and it never
  stops a run. The parent's unattributed auxiliary tokens stay unattributed.
- **A test file no check names is reported as unnamed, never as unrun**
  (batch 2 gui-ui-v3 on 5d9f5ff: t2 added `tests/ui/project_search.test.js`,
  the frozen Node check names five other files, and the lead's `node --test
  tests/ui` was correctly refused). Commands are never widened. The lead's
  guidance says a new test file no listed command names is not shown to
  have run, so new tests belong in a listed file. After a task's gate,
  `integration.uncovered_tests` finds changed test files of a family some
  check lists explicitly that no check names, covers by folder or
  discovers; they are an observation (on the task, the close-out and
  `result.json` `unnamed_test_files`), never a finding, because a wrapper
  can run a file no check names (Codex review of 7515f28).
  Native children named in a transcript count once per vendor call id; only
  a successful spawn is a child; a refused, errored or unresolved one is a
  ledger note that says whether it ran is unknown or that none ran.
- **The GUI serves copies of a run's files, never the run folder** (batch 2
  gui-ui-v3 on 5d9f5ff, the first real GUI run). Gradio serves only its
  working directory, the system temp directory and `allowed_paths`; the
  run's files live in the project's `.quadratus/runs/<id>`, so returning
  them raised InvalidPathError and blanked the report, diff and downloads.
  `gui.downloadable_files` copies exactly `RUN_FILES` into a fresh 0700 temp
  folder named after the run, bound to descriptors: `.quadratus/runs/<id>`
  must not contain a link (Codex review of 7515f28), only the project's own
  location above it is resolved once (macOS `/var` is a link), every
  component is opened from `/` without following links; each file is
  examined first, then opened non-blocking and confirmed as the same regular
  file (a FIFO in a file's slot blocked the open; Codex review of 35f198e),
  and a linked, non-regular or unreadable file is named in a note while a
  missing one is simply not offered. Bytes are read from the held
  descriptor and a partial copy is never offered. `allowed_paths` is never widened, which
  would expose the whole project tree.
- **The planner reads the harness record before the lead's account**
  (batch 2 gui-ui-v3 on 5d9f5ff). t6's close-out said the preview failed
  and R4 was blocked, because its review seat had tried to start the
  preview inside its own sandbox; the harness had in fact verified both
  views and Opus had approved. The planner saw only the account, dispatched
  t7 to capture again (about 680k tokens), and the run ended on the token
  reserve with every requirement covered. Each ledger entry now opens with
  the harness's own record for the task (`Session._harness_record`: design
  capture verified or not and its views, the final reviewer's verdict, the
  required checks), labelled as measured and as winning over the account.
  This is the summary-is-an-index rule applied to the harness's facts.
  The design review is read whole (`session._review_reading`): APPROVED
  only when the review approved, otherwise every BLOCKING line; the record
  stores the decision beside the 600-character text, because a first-line
  APPROVED followed by a BLOCKING line read as an approval (Codex review of
  35f198e). The audit prompt uses the same reading.
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
- **Run-start tests are checked in their run-start form; acceptance that did
  not run is never audited MET** (series rule-119c83f f2, Codex decision D,
  #35 6076286834). f2's t2 changed the original helper `tests/ui/load_app.js`
  so new code loaded under it, the gate ran the edited helper and passed, and
  every original Node test failed against the delivered code; t3 and t4 said
  their browser scenario did not run and the audit marked all five
  requirements met. Every test and test-support file
  (`integration.is_test_support`: test-file patterns, `conftest.py`, anything
  under `tests/`, `test/`, `__tests__/`, `spec(s)/`) is kept at run start.
  When one differs or is gone, the gate's own commands also run in a copy of
  the current source with those bytes restored and the run's added test files
  left out (`run_original_tests`); its receipts (`original-tests:<id>`) join
  the check, so a failure buys the ordinary gate-fix and attribution rules,
  and DONE runs it again. A command naming a test file the run added cannot
  run there and is an open unverified finding, as is a gate whose commands
  cannot be read; neither is a pass. Unchanged originals are not re-run. The
  audit is shown the unnamed test files and every `NOT RUN: <item> - <why>`
  (or `BLOCKED:`) line an editing call wrote, and lowers a MET
  deterministically when every citation is an unnamed test file, or when a
  task covering the requirement reported NOT RUN, or when its required check
  skipped cases the run-start source did not skip. That baseline is the gate's
  own commands run once on the run-start source, the first time a check skips
  anything (`skip_baselines`): a product edit that turns a case into a skip
  counts (Codex review of 81adcc7, R3), a case skipped since before the run
  does not, and the restored-original receipts never record one (R4). A skip
  is historical only when its name is one case in both runs, that case was
  skipped at run start, and the one test file holding it (the file in a pytest
  node id, or the one file with the bare name quoted) is the same file with
  that case's own definition unchanged since run start: for pytest the test
  function with its decorators, resolved along its whole class path with each
  enclosing class's decorators (an ambiguous or missing path is unknown); for
  a bare name every registration call quoting it, read whole by a bounded scan
  that matches bracket types and skips strings and comments, and is unknown on
  anything it cannot read for certain (a `/` that may start a regex, a
  template with `${`), never a prefix (02accbd, X1), so a changed callback
  body counts and tests added beside it do not (Codex review of e530b89, U1;
  5292fc2, V2; f2f66ff, W2, W3); names are read from the whole output before
  its tail is kept (`case_record`, Codex review of 67c9fad, S1), and a name
  over 300 characters is keyed by its SHA256 (`case_key`, d157378 T1). A
  shared name, a record past its bound (the tail is never used as identity,
  T2) or no baseline: every skip counts. A pytest XFAIL is not execution (S5).
  Optional commands never record one. Only a *complete run* discharges a NOT
  RUN item (Codex review of b6ba3ba, F3, F4, F6): a later passing required
  command, no filter in argv, in pytest's `addopts`/`PYTEST_ADDOPTS` or
  `NODE_OPTIONS`, no case reported deselected (R2), no `-p` plugin or
  `PYTEST_PLUGINS` (T3), no collection or execution hook in a conftest.py
  pytest would load, no node configuration file (S2), cases executed and zero
  skipped, naming every path of the item as a whole operand read from the
  command's own cwd (R5; a single-command gate's cwd and selection are kept on
  its check and on the receipt synthesized when original tests join, S3 and
  S4). A named skip fact is discharged, decided when each check runs against
  that check's source, by a later run of the same command whose complete case
  record shows those cases executed and none skipped. A pytest node id,
  recorded from pytest's own verbose line in a pytest command's output (case
  lines are read per the command's runner, taken from the executable's
  position and never from a test-file operand, so text a node test prints is
  never a pytest case, W1, X2; a runner the executable does not name, such as a shell wrapper around python, has selection not established and never counts as a whole run, Y1), carries its file, so its own execution is
  enough whatever else was skipped (U2); a `::` in any other name proves
  nothing (V1); a bare name must be held by the same single test file as when
  it was recorded, so a name reused in another file never stands in (T4, U1).
  An unnamed skip fact needs such a run skipping nothing. A substring, an
  optional or skipped receipt, a filtered run, a lower skip total, or a
  model's word never does; a report written after a run is a new report. An
  unnamed file is reconciled by a later complete run that names it or names a
  file that names it. Parallel children's reports come back to the parent,
  re-based to its checks (F5). Test-only runner configs (`pytest.ini`,
  jest/vitest/playwright/mocha configs) are kept and restored with the tests,
  and one added during the run (or an added `conftest.py`) triggers the
  original run with it left out (R1); a change to test selection in a mixed
  file (`pyproject.toml [tool.pytest]`, `setup.cfg`/`tox.ini` pytest sections,
  `package.json` type/imports/runner keys) means the original suite cannot be
  established: an open unverified finding, never a pass (F1). The project is
  re-read after the copy runs, and a change fails the check (F2). Downward
  only: no reply raises a verdict, and prose without a marker is not read.
  Reviewers are told which run-start test files changed. A goal that
  legitimately changes behaviour an original test pins will fail this check;
  there is no exemption and no new grant.
- **Step 1: execution failures surface at once; planner, audit and family
  card agree with the task** (#35, 2026-10-13, corrected after root's review
  of d51ae79). A sandbox launcher's own refusal (`sandbox-exec: sandbox_apply:`
  or one of bwrap's namespace-setup errors, as the whole line) raises
  `InfrastructureUnavailable` from Fleet on the editing and review paths,
  unless a later local command in the same call stated its own exit status
  0 (a `command_execution` in stdout, or a shell tool's output envelope or
  first-line exit status in the journal; a search result or command output
  that quotes `"exit_code": 0` proves nothing): that call recovered, and the
  refusal stays in its recorded tool failures. Any other line that
  starts with a launcher's name is ordinary output. On a Mac the refusal
  exists only in Codex's session journal, as the second part of a
  `custom_tool_call_output` whose first part says "Script completed"; the
  provider reads tool-output items there (never reasoning or messages) and
  only from a journal it can prove is this call's: one file, its
  `session_meta` id and cwd matching, begun inside the call. Two editing
  calls in one task with the same environmental tool failure (exit 126/127,
  a shell's own command-not-found or permission-denied line) on unchanged
  source fail the task as `environment`, work kept. The orchestrator and
  audit see configured checks by id and a public runner label (never a
  command line, operand, script name or skip reason). A test command only a
  plan or model prescribed is not a requirement; one the goal itself
  requires is, and when the checks do not run it the planner ASKs and the
  audit reports a named configuration conflict, never a waiver. The
  built-in default family's `no-io` item is narrowed, kind by kind
  (database, network, filesystem), to the kinds the scope names outside any
  clause that rules something out; every other kind stays forbidden, so
  SQLite work with "No network access" keeps its database and its network
  ban. A family chosen by a path rule or the project's policy keeps the
  item whole, and so does a scope that rules all I/O out.
- **Step 2: recovery changes approach, conflicts can be continued, and an
  interrupted job resumes from a durable quiescent checkpoint** (#35,
  2026-10-13). An unfinished task's ledger entry carries held facts (cause,
  first error line, the environment's own tool failures, the harness's run
  of the checks on the source it left), never the lead's account; a failed
  task's source now meets the gate once as evidence, as a capped one did. A
  CONTINUES that re-sends its predecessor's text and declared scope unchanged
  is sent back before any lead call. A parallel child that wrote outside its
  scope is a failed, unmerged task (nothing it wrote reaches the project; its
  files are an artifact) that a scoped CONTINUES may finish; a child stopped
  by integrity, refusal, budget or an unknown error is unchanged. Merged work
  that fails the check after the merge fix round is a `check.failed` finding
  (with the ledger and COVERS) that a RESOLVES task repairs. The run writes
  `checkpoint.json` (digest-bound) at every point between tasks; `--resume RUN`
  starts a new episode only from an interrupted run, never a sealed one, never
  twice, never with a call in flight or unknown usage, and only when source,
  goal, policy, limits, settings (credential fields excluded by exact
  name, `max_tokens` bound), gate, dependency and run-configuration
  (mode, tier, decider, write grant, scope, capture, readiness) identities
  all match. The checkpoint and `budget.json` come from one persisted
  snapshot, an interruption persists the time up to it, and every spend
  counter must be present, finite, non-negative and no lower than the
  checkpoint's.
  The record, unfinished work, findings, check debt, typed outcomes and the
  job's run-start test bytes carry over; the budget starts from what the
  parent's budget record spent, so the allowance is the job's. State the
  checkpoint does not carry (design evidence, test repairs, explicit lists)
  makes the point not resumable rather than partly resumed.
- **The planner plans against the job's allowance, and recovery is serial**
  (Step 2 medium job on 3e59970). Every next-task prompt states the task
  slots left, counting the one being named, and the requirements no task
  has covered yet: the planner had never been told the cap of 3 and spent it
  on storage and HTTP while the CLI and docs went unplanned. A parallel
  batch carrying CONTINUES is sent back before any lead call, as RESOLVES
  is: a continuation inside a batch skipped the recovery allowance, the
  hypothesis record, the unchanged-retry check and its contract's
  predecessor. The cap, the size ceiling and the completion gates are
  unchanged.
- **Automated acceptance is what the configured checks can show** (Step 2
  r02 on 3b629cf, narrowed after root's review of 86e2841). r02's t1 named a
  scratch two-Store Python session as acceptance, the lead reported it NOT
  RUN, and R1-R7 stayed open after committed tests covered the same
  behaviour. Only the configured-checks note, which renders only when a check
  is configured, now says automated code-behaviour acceptance belongs in
  collected tests committed in the task's files, never a scratch session,
  one-off command or script outside the tests. The shared SCOPE text carries
  no such rule: read-only (`edits: none`) reviews, design or audit judgment,
  harness captures and operator acceptance keep their own channels. The NOT
  RUN rule and its discharge are unchanged.
- **A capped draft in the last task slot meets an independent review**
  (Step 2 r03 on f4eafea). t3's Grok lead hit its 8-turn cap in slot 3 of 3
  with the recovery spent; the checks passed on its in-scope source, nothing
  could continue it, and the job ended GoalUnconfirmedAtCap with nobody
  having read the work. `Session._capped_final_draft` now sends that source
  through the existing review, fix, gate and close-out path, with one
  reviewer from another vendor, only when the task is serial, in the last
  slot, scoped, inside its scope and passing its own checks on that exact
  source. The lead stays capped (a capped revision or fix closes capped),
  the review must give a verdict, the cap's goal question and audits still
  decide, and the cap stays on the task as a non-blocking fact. A capped
  task with a slot left still closes unfinished and is continued by name.
- **Every task review judges the original goal** (root review of d0f3263,
  then r04 on 5366412). A reviewer saw the role packet, the task's
  paraphrase and the draft, never the goal; r03's and r04's CLI tasks
  dropped the goal's JSON refusal contract, closed, and left malformed IDs
  on argparse's plain-text exit. `Session._goal_authority` now appends the
  goal verbatim and the covered requirements' original text to every
  collaborator prompt, ranked above the paraphrase. It grants nothing; a
  finding that needs a change outside the task's paths stays open. Root's
  review of 4ed4d30: r04's CLI task was a SIMPLE parallel child, and SIMPLE
  draws no collaborator, so the goal never reached a review on the route
  that failed. A task that covers ledger requirements now draws one
  existing peer from another vendor whatever its difficulty
  (`collaborators_for`), and a parallel child is handed its covered
  requirement text by the parent. Missing review is held, never waived
  (root review of 1a58853): goal-bearing work with no reviewer from another
  vendor available opens an unverified finding
  (`_hold_unreviewed_goal_work`), and a direct label on work that covers
  requirements is refused (`_direct_refusal`), so it runs at the normal
  tier with its review. With the ledger on, every task carries COVERS, so
  the direct tier now admits only ledger-off work.
- **A goal-bearing review owes a verdict per covered requirement** (r05 on
  1655cf1). The CLI reviewer carried the goal and R10 verbatim and still
  replied NO FINDINGS while malformed IDs left through argparse's plain
  exit. A review of work that covers requirements now gives one
  `R<n>: MET - <file>:<line>` or `R<n>: NOT MET - <condition>` line per
  covered requirement (`_judge_goal_review`). An approval missing a line
  is an unfinished review, held unverified; a NOT MET line is a BLOCKING
  finding even when the reviewer wrote none; a clean review is NO FINDINGS
  followed only by MET lines and buys no revision. This makes a miss
  harder, not impossible: the verdict is still the reviewer's judgement.
- **Reviews and audits trace every error path; the goal outranks requirement
  wording** (r06 on 5b31818). The reviewer cited per-requirement lines and
  the auditor judged the CLI requirement, yet both read only the handler that
  formats errors while input parsing exited before it. `_ERROR_PATH_RULE`
  now tells both seats to trace parsing and validation ahead of the main
  operation, outside any error handler, and the helpers, in general words
  with no runner or known case named. The auditor also held R14 to
  "exactly", which the requirements review added and the goal never states:
  the audit prompt now names the goal as the authority, judges the goal's
  own condition over stricter wording and says so on the line, keeps
  decisions and rulings binding, and never relaxes a condition the goal
  states. NOT RUN discharge is unchanged; full-file evidence for discovery
  commands is a design proposed to root, not implemented.
- **The final audit is not a proven backstop for refusal conditions** (r08
  on f247f0a). The reviewer and the final auditor both had the goal, the
  requirement and the error-path rule, cited a success-path test and
  called the CLI refusal condition MET; malformed input still left before
  the handler in another form. Prompt prose did not move either seat.
  `_PROBE_RULE` asks a review seat whose copy takes scratch
  (`CLISpec.copy_args`, today codex) to add a scratch test feeding the
  interface each kind of invalid input the condition covers, including input
  the interface's own parsing rejects, and report the test's own name as
  executed and passed; otherwise NOT RUN and no MET line, so the review is
  held unverified. It applies only when an approved command would discover a
  new test file (`integration.discovers_added_tests`: verbosity flags only,
  operands only project-relative directories that exist in the project or
  copy; no filter, explicit file, absolute or outside path, or other flag;
  root reviews of b24b6db and 2d9be52, so a private path is never named). The copy-bound role keeps the harness
  records and "do not run the checks again" and adds one named exception for
  that scratch file (`runtime._probe_exception`). The auditor gets no probe
  and no command line. No new command, grant or role. Whether it changes the
  verdict is unmeasured until a run shows it.
- **A parallel child's finding on a sibling's work is rechecked on the
  merged source, once** (r09 on ded0095). t3's reviewer blocked twice on
  routes the running sibling owned, spent two revisions and two rechecks,
  and the finding stayed open after the merge although the merged source
  held the routes. A child's reviewer now sees each sibling's paths and
  intended result and may write `DEFERRED: <task id> - <condition>`; that,
  or a finding left unresolved after the child's own rounds, is held open
  as unverified and, only when the merge gate passes, rechecked once by
  the same reviewer on the merged source (`_recheck_after_merge`).
  RESOLVED closes exactly those findings; anything else keeps them. No
  lead call follows, no MET is written, and a sibling id outside the batch
  is ignored. The same run showed the scratch proof asked for in r08 was
  not done: the reviewer read source and reran the suites. Prose cannot
  enforce it, so the runtime now measures new test files the probe call
  left in its copy (`context["scratch_tests"]`) and the session records
  that as an unverified fact. It informs, it does not gate: which
  requirements need a demonstration is a policy call for root and Davis.
- **Causal addendum to the r09 note above** (root on 40b2133, from the
  actual parallel prompt and native rollout). The r09 codex reviewer never
  received `_PROBE_RULE` or the runtime exception: `_run_batch` gives each
  child `integration_gate=None` and `_probe_applies` required a gate, so
  the rule and the probe flag never reached a parallel child. Earlier
  qualification covered the parent path only. So r09 shows no scratch
  run, not a model ignoring a delivered rule, and 40b2133's file
  measurement never activated there either. A child now reads its parent's
  approved gate commands (`_inherited_checks`, scratch only; its own gate
  stays off). Under root's decision, a new file name is not execution:
  when the probe call leaves a test file in its copy, the runtime runs the
  approved discovering commands itself with `GateSuite` in that copy
  (`runtime._scratch_proof`). Each scratch case counts only by identity:
  a pytest line naming the file (verbosity raised through
  `PYTEST_ADDOPTS` only, never argv), or a node name no handed test
  declares. Only every declared case executed and passed is `passed`.
  Failed, skipped, absent, unnamed or a handed file changed is `failed`
  or `not run`, recorded unverified in `scratch_proofs`, never a MET.
  Nothing from the project's dependency trees is linked into the copy.
- **A scratch run has the reviewer's authority, not the gate's** (root
  review of 1c14ea9). Through the gate runner, a reviewer's scratch test
  changed a marker outside its copy and the proof still said passed; the
  gate's authority is not the reviewer's. `runtime._scratch_isolation` now
  puts each scratch run in an OS sandbox matching `CODEX_SPEC.copy_args`:
  the copy is the only writable root (TMPDIR moved inside it), no network,
  and the live project plus every absolute path an approved check names
  outside the copy are unreadable. Linux uses `/usr/bin/bwrap`, macOS
  `/usr/bin/sandbox-exec`, both pinned paths, never PATH. No sandbox, or a
  launcher refusal (`SANDBOX_REFUSAL`, e.g. a nested seatbelt under an
  outer profile), is `not run`. The run uses the reviewer's model-facing
  argv. And each pytest instance keeps its id: a parametrized case passes
  only when every printed instance executed and passed.
- **A capped revision in the last slot meets its own reviewer, once**
  (r10 on f6c028c). t3's capped draft took the last-slot handoff to a
  review that found a real CLI defect; the revision fixed it inside scope
  with the checks passing, but stopped at its cap and closed turn-limited
  with no recheck. `_capped_revision_recheck` now sends a capped revision
  or capped fix back to exactly the reviewers whose blocking findings it
  answered, once, on the current source. That uses the capped draft's
  eligibility (`_checked_final_source`): last slot, no operator list, write
  grant, measured in-scope edits, checks passed on exactly this source.
  It also needs every one of those reviewers available and no design
  evidence. RESOLVED lets the task close normally; anything else stays
  open. The writer is never called again (no fix cycle, gate fix zero). A
  note with no blocking finding, failing checks or a scope breach keep the
  capped close. Whole-goal confirmation and the final audit still decide.
- **Size is advisory; nothing stops, trims, forces or rejects on length
  alone** (operator directive 2026-10-20, Davis, after the r11, r13, R15
  and R16 trim rules and the proof-planning diagnostic). The hard stop at
  1.5 x `max_lines`, the one trim call it bought (`_edit_within_stop`,
  `_trim_overrun`, `_trim_capped_draft`), the trim evidence and
  preservation-debt subsystem (`_trim_snapshot` and the rest, `TRIM`
  dispositions, `trim_records` on the checkpoint and the packet), the
  revision's room figure with its BLOCKER channel, and the plan refusal
  above 100 lines are gone. What remains: `max_lines` is the planner's
  estimate, any positive integer, declared at the task's real size; the
  harness measures every change (`ScopeReport`, `oversized` is telemetry
  for well past the estimate), records it, and tells the lead, every
  editing call and every reviewer the measured size against the estimate
  (`_scope_size_note`), and the planner sees it on the task row. Priority
  is correctness and complete requirements, the tests that prove them,
  then conciseness: no unrelated edits, no duplication, no padding, and
  never a test squeezed or left out to fit a number. The reviewer judges
  whether the size was necessary; length alone is not a finding. Paths,
  original tests, budgets, roles, the independent review, goal
  confirmation, the final audit and the stale, missing, failed and NOT RUN
  evidence rules are unchanged; the direct tier's own bound stays
  conservative, and a task past it runs on the normal tier with its full
  review rather than being refused. A task's declared estimate is kept as
  declared; the policy's `scope_max_lines` is the default for a task that
  declares none and never clamps it (the pinned library schema bounds
  only that default). Controls: `tests/lifecycle/test_advisory_sizing.py`
  (a ~190-line draft against ~40 reviewed, audited and completed; a plan
  at 400 dispatched; a revision that grows rechecked; a capped large
  draft on the capped route; a path breach, a reviewer's finding on
  unrelated bulk, an edited original test and the direct bound all still
  holding), harness pack case 02, `test_joint_scope_telemetry`.
- **The installed vendor client is a recorded fact and a run precondition;
  the engine never retries a capacity 429 itself** (Davis, 2026-10-21,
  after the proof-planning and settings-migration runs both ended on
  codex 429s). Root read the upstream source: codex 0.154's
  `codex-api/src/api_bridge.rs` maps every 429 but the usage-limit codes
  to a bare `RetryLimit` with status and request id, discarding the
  `turn_rate_limit_exceeded` body and its Retry-After, so the JSONL
  `turn.failed` carried only "exceeded retry limit, last status: 429" and
  the attempt settled as unknown usage; 0.162 carries Retry-After through
  the transport error and waits inside the same sampling session under
  its own retry budget and the call deadline; 0.163.0 is the first version
  this engine qualified, live by root (not a claim about which upstream
  release first carried the wait). So the fix is the client,
  not an outer replay (no `CapacityRejected` loop, no parallel retry
  framework, no raw trace capture: rejected as scope). What the engine
  does: `CLISpec.minimum_client` (`0.163.0` for codex; none for claude or
  grok, which showed no such fault) and `version_args`; `_build_client`
  reads `<binary> --version` once per executable identity (path, device,
  inode, size, mtime: a replaced binary at the same path is read again,
  so a long-lived process sees the remedy) and keeps only the bounded
  `major.minor.patch[-pre]` form from a zero exit's first stdout line,
  never the binary's own line, stderr, or any path or error text
  (`client_version`, never raises); it records `client_version` and
  `minimum_client` on the provider without refusing; `Fleet.status_lines` shows `client <v>,
  qualified minimum <m>` with STALE or "version unreadable; not refused";
  `Fleet.stale_clients` lists an available CLI vendor whose version parses
  below the minimum, and `project_run` raises `StaleClient` before the
  session is created, so a stale client runs nothing and the result names
  both versions. Unreadable is unknown, not stale, and there is no
  environment override: the remedy is updating the client. Diagnostics
  are bounded and allowlisted: `_extract_codex_diagnostics` records a
  `turn.failed` as `stop_reason` plus the HTTP `status` its message
  names, nothing else (the message is vendor free text, kept only in the
  ledger's stderr tail; no `retry_after` is invented, since a successful
  0.163 call's JSONL carries no 429 body), and `_observe_output` stamps
  `client_version` on every row. Native retry defaults, the deadline,
  the budget's unknown-usage settlement, `_looks_exhausted` and every
  review, usage and partial-work protection are unchanged. Controls:
  `tests/test_client_version_preflight.py` (parse and semver pre-release
  ordering, read once per identity and again after the executable is
  replaced, non-zero exit and stderr and unrecognised lines all unknown,
  provider recording, failed and completed turn
  diagnostics, the allowlist dropping free text, status lines, the
  preflight refusing before any call at 0.154.0 and running at 0.163.0,
  an environment variable lifting nothing).
- **Every vendor client is held to a qualified minimum, and a failed call
  is recorded by the same bounded facts** (Davis, 2026-10-21, the batch
  after the codex fix). Root's qualification targets: claude 2.1.304 (npm
  stable, from 2.1.296), grok 1.0.62 (Homebrew/x.ai stable, from 1.0.30),
  codex 0.163.0 unchanged; set as `minimum_client` on all three specs, same
  preflight, unreadable stays unknown and is not refused. No upstream
  fault is established for claude or grok; the baseline records what root
  qualified. `failure_facts(status, text)` is shared by the three
  extractors and yields at most `status` (an integer field, or a bare 4xx
  or 5xx in text the vendor reported as an error) and `failure_class` in
  `cap`, `auth`, `rate_limited`, `unavailable`: the window's own wording
  (`EXHAUSTION_MARKERS`, one tuple with `runtime._looks_exhausted`) outranks
  a 429, 401 or 403 or sign-in wording is auth, 429 alone is rate limiting,
  502 to 504 unavailable; anything else is unknown by absence, no retry
  delay is inferred, no text is kept, and prose in a successful answer that
  mentions 429 or 503 records nothing because only error envelopes are
  read (codex `turn.failed`, claude `is_error` with `api_error_status` or
  `errors`, grok `error` or `type: error`). Root's offline controls on
  f1d9ba2 found the false-success boundary: `_extract_codex_result` returned
  an earlier `agent_message` when a later `turn.failed` carried the 429.
  The error now dominates partial text (kept on the error as
  `partial_text`, never returned), and a `turn.completed` after a
  `turn.failed` is the client's own recovery and is read as the success it
  is: the last terminal event in stream order decides. The failed flag is
  held apart from its detail (root's review of ace40f8: `{"status": 503}`
  with no message left the old message-as-boolean empty and let an earlier
  message through), so a `turn.failed` with a missing, empty or malformed
  error is still a failed turn, with a default detail. A status is read
  from error text only behind status, HTTP or error wording ("last status:
  429", "API Error: 429", "HTTP 503", "status code 502"); a bare number in
  prose ("file429.py", "line 503") is not a status. Claude's
  `stop_reason: max_tokens` is likewise an error with partial text (the
  API's documented stop, not yet seen from the binary), and an error
  envelope with no auxiliary rows now keeps its ending facts where it used
  to report None. Native retry defaults, the deadline, reviews, usage and
  partial-work protections are unchanged; no outer retry, override or
  retry system was added. The workflow fake vendor reports 99.0.0 because
  one fake serves three vendors. Controls: `tests/test_provider_contract.py`
  and the vendor cases in `tests/test_client_version_preflight.py`.
- **The PROVES rule is stated to every seat that writes or judges a
  delivery, and a malformed line comes back with its measured reason**
  (the settings-migration run, 2026-10-21: P11 first named a test class,
  then a comma-separated list; `_record_proves` rightly rejected both, the
  reviewer asked for source revisions, and the second revision was spent
  on a format the prompt had never spelled out). `scope.PROVES_RULE`: one
  `PROVES: P<n> - path/test_x.py::Class::test_case` per named case,
  repeated with the same P<n> for several cases, never a class, a file or a
  comma list. It is in the lead's packet (the scope render), the revision
  prompt (`_binding_feedback`, which also lists each scenario whose
  binding is an invalid declaration with the reason `_proves_problem`
  measured from the line's shape) and the reviewer's proof rows. A
  declaration fault alone asks for the corrected line(s) and no code or
  test rewrite; findings about the code or the cases still need their
  fix. The grammar is unchanged and strict: no normalization, no class
  expansion, no extra model round, and the reasons name the fault, never
  repair it. Beside it, `scope.TEST_SUBSTANCE_RULE` (the same run's
  independent finding: a standard-library-only test forbade every import
  and looped over nothing): a test asserts the stated operation's result
  from the stated prior state, a test with no assertion or one that cannot
  fail proves nothing, 'standard library only' permits standard-library
  imports and forbids third-party packages, and a test never forbids what
  the goal's later work may validly use. Shown to the lead when the task
  declares scenarios, in every revision, and to the reviewer, who treats a
  vacuous or over-forbidding case as a BLOCKING finding on that test.
  Nothing task-specific is graded. Controls:
  `tests/lifecycle/test_proof_declaration_feedback.py`.
- **A task's record is written by the harness; no model summary call
  exists** (Davis, 2026-10-21, after the ticket-workflow run on 439d757:
  t1's lead, review, revision and recheck all returned, then the close-out
  summary call on Sol timed out at the 60 s clamp with only thread.started
  and turn.started in its stream, the run ended with the task's work
  measured but unrecorded, and the budget latched unknown usage; the
  backend cause stays unknown). `_task_close_out` now returns the direct
  tier's mechanical record or `_factual_close_out` for every other task,
  reviewed or not, whether or not a later planning call reads it; the
  model close-out, its refusal record and the SUMMARY/DECISIONS/DEAD
  ENDS/MAP NOTES parser are deleted, and `_closes_factually` is always
  true, so no plan stage, remainder or exposure reserves a close-out call
  (`runtime.Fleet._closeout`, root's file, stays as the bounded summary
  view and is called by nothing in the session). The record carries the
  task's measured changes and source fingerprint, the last check with
  whether it ran on this source (a check on another source or a failed
  one is named as such, never promoted), each review's disposition and
  recheck, design evidence, every proof scenario's binding and execution,
  open facts, unresolved and deferred findings, NOT RUN reports, scratch
  proofs and finish refusals, bounded (eight items of 300 characters per
  list) with the whole transcript and diff kept as artifacts and the
  index on the task; it states that no decision, dead end or map note was
  written by a model. Reviews, the goal question, the final audit, the
  budget policy and the unknown-usage stop are unchanged; a stale or
  failed check is never promoted. The harness stage edge `closeout` and
  the monitor's stage names are kept. Root's review of 8bc9805 shaped the
  record further: with no pre-task snapshot the change list is
  "unknown" with the measurement status, never "no files"; design
  evidence reads the task's contract or the live applicability, so a
  required record that is missing is MISSING and a disabled one is
  disabled, and an approval is current only on this source fingerprint
  (an earlier source is historical, an unrecorded identity is said so,
  and source equality alone does not make it current: root's final
  review reuses the design review's own drift checks, so an approval
  whose renders or goal/requirement context moved since the review reads
  as "validity unverified" with the drift named);
  the model-facing summary is bounded per field (twelve paths, eight
  items per list, explicit "and N more" counts) and points at the
  unbounded full record artifact, so no debt is truncated away. With no
  close-out call there is no stage to drop, so the terminal route
  (admission/3) can no longer differ from the full route and never
  fires: the lifecycle scenarios written on it now show the same
  protections on the last slot or as mandatory-deficit refusals, and
  the planner's allowance note and the direct-tier text no longer
  budget a close-out call. Controls:
  `tests/test_closeout.py` (the record's contents, bounds, gate-command
  privacy, no map amendment, no model asked), `tests/lifecycle/
  test_factual_closeout.py` (a reviewed t1 closes on the record, the
  planner reads it and dispatches t2, reviews and the audit still run,
  zero summary calls, a failed check recorded FAILED, a scripted refusal
  never reached) and the lifecycle files that asserted a close-out call.
- **Codex usage is the sum of completed parent turns and an unaccounted
  turn is never hidden** (root, 2026-10-21, `_codex_usage_account`). The
  saved failed call's thread state read tokens_used=0 only as a default,
  with no token_count or completed event in the rollout, so nothing could
  say what it spent. Upstream 0.163 `exec/src/event_processor_with_jsonl_output.rs`
  emits the thread's cumulative total on every `turn.completed`
  (`usage_from_last_total`), so the account is the last valid snapshot
  on a single thread, never a sum (root's correction of root's own
  earlier sum): snapshots must be monotonic, a repeated total charges
  nothing extra, cached input is a subset never added, and a second
  thread id in one stream is not the same account. The call is unknown
  when a turn is open, failed, started twice before completing or
  malformed, when a total falls, or when a second thread appears; a later
  `turn.completed` does not clear an earlier `turn.failed`, `error` or
  double start, and the last valid snapshot is kept as the observed lower
  bound. Nothing recovers a historical call's usage or verifies a bill. The answer semantics are
  separate: `_extract_codex_result` still honours a completed turn after
  a failed one as the client's recovery. Diagnostics carry
  `usage_completeness` (complete or unresolved), `completed_turns`,
  `cached_input_tokens` when any, and `usage_lower_bound` (the completed
  turns' totals with their source named) on an incomplete call;
  `safe_diagnostics` allowlists exactly those, beside the rollout's
  integer floor. No zero is fabricated, no estimate is made. Root's native
  adapter (`tools/step2_native_execution.py`) sets a call-local `RUST_LOG`
  selecting `codex_otel.trace_safe=info` and `responses_retry=warn`, and
  `tools/native_call_diagnostics.transport_facts` keeps only allowlisted
  event names, numbers, booleans, the failure class and a stated backoff
  (never a message, header, prompt or identity) in
  `native-transport.json`, removing the selected trace lines from ordinary
  stderr; `usage_total` is always None there. It also keeps the
  provider's request correlation id in a bounded opaque shape
  (`auth.request_id` upstream, never a header, endpoint or account
  field), `ttft_ms` and `tool_token_count` (the upstream spelling of the
  response total, never summed into a bill), and counts routine stream
  deltas, in-progress notices and pings as `routine_rows_omitted`
  instead of letting them spend the 256-row cap before the completion or
  failure row. Diagnostic observation only: no retry, model, permission
  or config change.
- **Historical: a revision past the size stop spent the remaining fix
  allowance coming back under it** (r11 on 136a3ec; retired 2026-10-20).
  t2's in-bound draft was revised past the hard stop by one line, though
  its prompt stated the exact room, and the task failed with a fix cycle
  unused and no slot left; the trim call that answered it is gone with
  the stop.
- **Every editing call carries the original goal** (r11). Only the draft
  had it, through the memory render; revision, fix and gate-fix prompts
  held the generated task text, which in r11 prescribed the defective
  route branches and called the plain CLI exit a refusal. `_writer_goal`
  enters once, at the boundary in `_invoke_model_call` where every
  editing role (`_EDITING_ROLES`) already gets the lead's packet, so
  design-fix, security-fix, fixture-supply and capture-redeclare are held
  to it too (root review of af5814d); the draft keeps the memory render's
  goal, and a parallel child's draft adds its covered requirements. It
  says a task text or generated recipe does not change them, grants
  nothing and changes no bound. Only the complete current block counts as
  already bound (root review of 21639d6): a copied heading over other text,
  or a block with another covered requirement, never suppresses it.
- **A capped writer after the draft is told its round budget** (r12 on
  56ff155). t3's capped Grok revision spent its 8 rounds on 19 reads,
  greps and commands and wrote nothing; its prompt had the goal and the
  headroom, but only the draft had ever been told a cap existed.
  `_writer_turn_budget_note` adds it at the editing boundary for revision,
  gate-fix and design-fix (`runtime.LEAD_CAPPED_ROLES`) with the write
  grant, on a CLI with a round flag, using the draft's limit and planning
  arithmetic. It says the call stops with its edits kept and that checks,
  reviewers and the final audit still decide; no status promise. Codex,
  security-fix and uncapped runs are told no cap. The cap itself is
  unchanged.
- **Historical (retired 2026-10-20): a finished draft past the size stop
  got the same one trim** (r13 on
  5e8926b). Two finished parallel drafts crossed the hard stop inside their
  paths and failed before any check or peer. `_trim_overrun` (shared with
  the r11 revision trim) now also serves a normally finished draft: one
  call, charged as a fix cycle against the task's later revision and
  fixes, and only while a cycle is left for that revision. Never reverted,
  measured again; still over, a path breach, a capped or transport-failed
  draft, or no cycle left fails or holds as before. The trimmed source
  still meets the checks, the peer and the final audit. Its fact is
  recorded only after the trim lands, so a failed trim leaves the failure
  record unchanged.
- **The cap goal question is the goal decision, not a second audit** (R14
  on 164859f). It rendered every task's full work inline (43k characters)
  to an uncapped native seat that also read the delivered files; its 183k
  reported tokens left 163,762, below the unchanged 200,000 call reserve,
  and the final audit was never invoked. `_confirm_goal_met` now renders
  with `with_previews=False`: goal, standing rules, requirements, rulings,
  harness records and summaries as before, each task's full work as
  artifact ids that FETCH serves whole. With the requirements ledger on, it
  says DONE is the goal decision and the independent audit follows. No
  reserve, order, cap or seat change: reordering would only move the
  starvation, since native usage cannot be known before a call.
- **A trim aims at the stated bound; a no-edit capped revision gets one
  measured retry** (R15 on 7f628ae). Asked for "at most 150", t2's draft
  trim landed at 152, as r11's revision had at 151: the model aims at the
  number given. The trim now aims at the task's stated bound and names the
  stop as the failure line. t3's capped revision had a concrete blocking
  finding, read for 8 rounds and wrote nothing, with a fix cycle unused.
  `_no_edit_cap_note` gives such a revision (source byte-identical, a
  blocking finding open, write grant, a cycle left) one more revision from
  that cycle, carrying the measured outcome rather than the same prompt;
  its own cap takes the capped paths. Whether it helps is unproven.
  A cap raised inside a size trim is tagged and never read as a no-edit
  revision (root review of b5ea0ef: it bought a third writer call).
- **Historical (retired 2026-10-20): a capped draft past the size stop got
  the same one trim** (R16 on
  78f76bb). t2's draft stopped at its cap with 153 in-path lines against a
  stated 85 and failed on scope with both cycles unused.
  `_trim_capped_draft` gives its kept edits the r13 trim (charged as the
  first cycle, only while one is left after it, prompt stating the cap and
  aiming at the stated bound). Under the stop it is a capped draft like
  any other: the cap stays recorded and the existing capped paths decide
  (the last-slot review, the final-batch child review, or the capped
  close). Still over fails as before; a capped trim goes straight to the
  capped close; a path breach gets no trim.
  A capped trim marks the turn-limited record `trim_capped`, and the
  final-batch capped-child review holds it rather than reviewing past two
  caps (root review of 860769e).
- **The planner is told which unfinished work is still owed** (R17 on
  a3cf7e4). After t2 CONTINUES t1 closed and a real pause and resume, the
  planner still read t1's "not done ... CONTINUES: t1" record with nothing
  saying it was settled or that the one recovery task was spent; it named
  t3 CONTINUES t1 and the run stopped SurveyAllowanceSpent. The next-task
  prompt now carries `_unfinished_state_note`: what is still owed (the same
  typed-or-legacy set that blocks completion), what a later task took over,
  and recovery tasks used of the allowance. Records stay as written; no
  CONTINUES is stripped, and the allowance stop is unchanged.
- **A disputed finding is tested by the harness, never argued** (Davis,
  2026-10-15, after R18's false audit finding). Reviewers and the auditor
  state a finding with its condition, location and a concrete failing
  example. The orchestrator may reply `EVIDENCE: <R<n>|U<n>|none>`, a
  `FILE:` line and one new test file (`_parse_evidence`). The harness runs it
  with the configured discovering checks in a disposable, sandboxed copy of
  the current source (`_run_evidence` over `runtime._scratch_proof`); no
  command line or path goes to the planner or the auditor. Failed confirms
  the finding with no further call; not run (skip, no sandbox, a test that
  rewrote handed source, a project that moved) leaves it held; passed goes
  once to the model that made the finding, which reassesses it alone
  (`_reassess`). An audit MET there still needs a resolved citation and is
  lowered by any standing NOT RUN record; it sets the requirement met and
  the next DONE audits everything again with the evidence in its prompt
  (`_evidence_block`). A review RESOLVED closes exactly that held finding.
  Only the latest audit's own NOT MET lines on the current source and held,
  non-deferred review findings can be tested (`_disputable`); one request per
  finding per source, kept in `evidence_requests` (checkpointed, written at
  a quiescent point before the planner is re-asked). Requests share the
  decision's ASK bound; the reassessment is an ordinary reserved call.
  Held review findings with COVERS, a slot left and a discovering check are
  debt instead of a FindingsOpen stop (`_review_debt_continues`), as a
  failed check is; they still block completion.
  After the task cap's final audit (root boundary on 83b658c: seven
  controls stopped RequirementsUnmet at max_tasks=1 with 8 of 20 calls
  used and no challenge), `_final_challenge` offers the seated orchestrator
  the same channel read-only: no task, writer, slot or recovery, only the
  orchestrator's turns and the same auditor's one reassessment, each a
  reserved call. A budget that cannot admit one ends the challenge and the
  audit stands; a requirement leaves the unmet set only when reassessed MET.
  A proof is bound to `_evidence_identity` (source, effective gate or
  inherited checks, root, excludes) and relied on only while it holds:
  drift after the run spends no reassessment, drift during it discards the
  verdict (root review of 8f68ea0). The request is checkpointed before it
  runs and its receipt before the reassessment is reserved; the run's
  receipts and check ids stay on the record, never in a model prompt. A
  checkpoint from the cap's challenge carries `_cap_goal` and
  `_final_audit` and is the one spent-slot checkpoint a resume admits: it
  finishes the decision with no goal question, audit or execution
  repeated. Each request records its stage durably (intent, awaiting
  reassessment, reassessment started, done): a resumed job finishes an
  unstarted reassessment once with the same auditor, from the retained
  test, finding and bound identity (`_finish_pending_evidence`); a started
  one is never retried. The reused final audit restores the coverage the
  ledger had when it was asked, so its own NOT MET cannot read as
  "never covered" (root review of a93334b). Evidence files are at most 16,000 bytes and shown
  to the reassessor whole.
  Every reassessment reads `_authority_context`, the same block the final
  audit reads (verbatim goal, the requirements judged, redacted check facts
  and configured checks, decisions and operator rulings, design evidence,
  NOT RUN records), plus `_EVIDENCE_AUTHORITY`: a test cannot authorize
  another reading or stand in for renders or unrun checks. A review
  reassessment gets the goal and its task's covered requirements, not only
  the generated task text (root review of 8f68ea0).
- **A goal contract is never waived by a lower authority** (R19 on
  e2a46a4). t3's reviewer found the goal's JSON refusal broken for malformed
  new-command IDs, then waived it because the task asked for the type=int
  convention and the old commands behaved that way, and marked R12 MET; the
  audit followed. The goal block already said it outranks the task text.
  `_AUTHORITY_ORDER` now states the order (goal and rulings, then
  requirements and decisions, then task text or plan) and that no
  prescribed convention, consistency with existing commands or older
  behaviour excuses a goal contract on the work it asks for, and that a
  requirement is never MET beside a note that it is not. It reaches the
  writer after the draft, every task reviewer, the final auditor and the
  evidence reassessment. Prompt only: no severity is forced and nothing is
  marked MET or NOT MET by the harness, and a wording rule is not a
  measured accuracy fix. The structural half: the final auditor's prompt
  (9538 chars) never held the reviewer's warning. `_record_review_notes`
  keeps each non-blocking review paragraph naming a covered requirement,
  with its task and source fingerprint; `_authority_context` carries those
  on the requirements judged, labelled current or earlier source, as
  observations to reconcile. Resolved BLOCKING findings and notes on
  uncovered requirements are not carried. Notes are kept and carried whole,
  every one (root review of 728c0fc: a 600-character slice kept a benign
  prefix and a last-20 cap dropped the oldest warning); review volume is
  already bounded by the job. A BLOCKING line ends what its paragraph can
  contribute: its continuation belongs to the blocking and recheck route.
  A paragraph that states a condition without any id (as `_FINDING_SHAPE`
  allows) is kept against the task's covered requirements and labelled as
  naming none, its association uncertain; one naming only uncovered ids
  stays out (root review of 225367f). An executed-demonstration
  requirement for every error-boundary MET was built and held back as
  broader than this fact supports (patch kept in the #73 record).
- **Every failed required check carries its own report** (R21 on
  319385322b). The final check failed on a plain test-body assertion and the
  run stopped CheckUnattributable with budget left. pytest 9 reports each
  subtest through the item's hook, so producer `quadratus-pytest/5` tallies
  subtests apart and counts each item once, failed when it or any subtest
  failed, its one record an assertion only when every cause is a body
  `AssertionError` (a parent pytest already flipped to failed with no
  exception of its own is not a cause). A `python -m unittest ...` check that
  declares `--quadratus-report={report}` runs the same arguments under the
  same interpreter through `_gate_producer/quadratus_unittest_report.py`
  (`unittest.main(module=None)` with a recording result class, copied under
  a nonce name, unittest's own output and exit), producer
  `quadratus-unittest/1`, the expected producer bound per invocation. Plain
  unittest stays undeclared and unattributable; root rejected letting a
  sibling check's report stand in for another, and `attribute()` is unchanged.
  Root on f0b4827: with no failure, a skipped subcase makes its test skipped
  in both producers (it had vanished, so `skipped_count` read zero), and the
  selected shipped producer's bytes are part of `GateSuite._runner_hash`, so
  a changed reporter misses the cache. unittest's own summary for a failed
  and a skipped subcase reads as zero executed and still fails closed.
- **The finish is held, the last wave closes factually, and error clauses
  need a run** (Davis, 2026-10-16, after R22: three tasks closed, 20 of 20
  calls spent, the cap's goal question refused at call 21, no audit).
  `RunBudget.protect_finish` holds the goal question and the requirements
  audit (`run_budget.FINISH_STAGES`) one call and one full per-call reserve
  each from ordinary calls, counting reserves in flight; an ordinary call
  that would spend them raises `FinishReserved` before it starts, is
  recorded (`finish_refusals`, `budget.json`) and latches nothing. The
  session marks the finishing calls (`finishing("goal")`, `"audit"`),
  recomputes the held stages each episode from its own record, and treats a
  refused review, recheck, revision, fix or post-merge recheck as not run:
  the finding stays open and every existing debt gate still decides, so a
  run can stop FindingsOpen instead of finishing. No limit, reserve or
  shape changes; finishing calls never take the summary shape. The job's
  last dispatched wave (and a model close-out refused to keep the finish)
  closes with `_factual_close_out`: reviews as they ended, last check and
  its source, open debt, NOT RUN, scratch proofs and artifact pointers, no
  invented decision or map note; not the direct tier's text. A requirement
  whose own text is about errors, refusals or invalid input
  (`_error_clause`) is accepted only on executed evidence, by exact case
  identity (`_cited_cases`, `_executed_cases`; root review of 17a96d3): a
  cited `file::[Class::]case` a complete approved run's case record shows
  executed and passed on the current source (a quiet or unittest run names
  no case and credits nothing; a same-named case elsewhere never stands
  in), or a case of a scratch demonstration the harness ran and passed in
  full, cited on that requirement's own MET line (in the review), or bound
  by provenance (source and checks identity, covered requirements) at the
  final audit. The audit lowers any other MET to NOT MET; a review's other
  MET becomes an UNVERIFIED line the lead answers, never an approval.
  Root on 5ca254c: a scratch proof is bound to the source and checks its
  copy was handed (`handed_source`, `handed_checks`, measured by the runtime
  on the copy), so a live change during the review leaves it stale with its
  raw result kept; and the own-report producers (`quadratus-pytest/6`,
  `quadratus-unittest/2`) name every case they count passed
  (`passed_cases`, one per count, validated), so the unchanged quiet
  declared gates show exact executed identity without verbose output. Whether the executed test establishes the clause stays the
  seat's judgment. The R22 ledger replayed through the hold refuses row 17
  (t2's recheck), which leaves its finding open: the hold makes the stop
  honest, it does not make that run finish.
- **An editing call is held at the per-call ceiling, and a first editor
  refused before any write is NOT RUN** (root reconciliation of R23 on #73,
  6208197375). R23's t3 lead was admitted on its 200k reserve with 670,394
  left, reported 346,846, and from then on the goal question itself could
  not fit beside the audit's hold; the scope trim was only where that
  surfaced. The hold now exposes a call holding the write grant
  (`Fleet.invoke` sets `run_budget.admitted_as`) at `max_tokens_per_call`,
  the most it can report without stopping the run, and counts each call in
  flight at its own exposure; every other call stays at its reserve, an
  estimate, and with no ceiling the record says the hold does not bound an
  edit. A call kept out only by calls in flight (it fits alone) waits on the
  budget's condition with no ticket, attempt or request and is decided
  again when one settles (`finish_protection.deferrals`); the wall still
  runs and is never reset, and a worker never waits, because its lead is in
  flight. Root's zero-model replay: t3's lead refused before any write, and
  t2's revision deferred behind t1's. A serial task whose first editing call
  is refused, with no edit admitted and the measured source unchanged, is
  `not_started` (`Session._settle_not_started`, `not_started` in
  result.json): never closed, no coverage, no further planning, and the
  cap's own decision runs every gate. Partial work blocks the goal
  question; a not-started task blocks completion by name ("NOT RUN"). A
  refusal after any edit, in an explicit list or in a parallel child stops
  the run exactly as before; the R23 trim refusal is unchanged.
- **A serial task the run is certain to stop after closes factually**
  (R24 on 54d729: t1 closed with three unverified review facts, a model
  close-out of 11,772 tokens ran, and the run stopped FindingsOpen before
  any planning call). `Session._stop_follows_close` reads, without
  changing anything, what the stop decision in `_run_tasks` reads: a
  legacy open finding the review debt cannot hold (`_review_debt_holds`,
  the read-only half of `_review_debt_continues`), or a failed last check
  with no ledger or no COVERS to hold it as debt. Only a task the serial
  loop dispatched is asked (`_serial_dispatch`); a direct `run_task`, a
  parallel child or a held-debt task keeps its model close-out. Reviews,
  verdict parsing, debt and stops are unchanged.
- **Judgment integrity: one strict reading, one same-seat completion per
  job, an obligation view and a wall hold** (strategy reset approved by
  Davis, 2026-10-16, from 1b9f0aa).
  - `quadratus/judgment.py` combines repeated per-requirement lines for the
    reviewer, the audit and the reassessment alike: any NOT MET wins in either
    order, and duplicate MET lines approve nothing. Before, the audit kept the
    last line and the reassessment the first. A recheck reply is RESOLVED,
    UNRESOLVED (a genuine finding, which buys the fix round) or INVALID (no
    verdict, which buys nothing).
  - A review or recheck that ARRIVED without a valid verdict may be completed
    by the same seat, in its existing role (`collaborator` or `recheck`: turn
    cap, read-only copy, scratch, privacy), on the same source,
    `SessionConfig.max_judgment_completions` times per whole job (default 1).
    The claim is written to budget.json before the call, never refunded, shared
    by parallel children and carried by `checkpoint.consumed`. A cap, a refusal,
    a finish-hold refusal or a changed source gives nothing and is never
    retried. Other failures propagate as the review call's would. The raw reply
    is kept (`judgment-raw:*`). A genuine BLOCKING, NOT MET or UNRESOLVED is
    never routed here. If the completion still gives no verdict, the open
    finding stops the run as before; no other seat and no audit stands in.
  - Root review of d264b0c:
    - The completion is bound (`_judgment_binding`) to the task, seat, role,
      contract, source, effective gate identity, approved-check digest, goal,
      and every requirement's text. The binding is taken before the claim,
      persisted with it, and compared before the call and before the reply
      is relied on; any drift voids the answer and spends the claim.
    - A refusal by the seat, transport, integrity or any other failure is
      recorded, then raised as the review call's own would be.
    - Only the strict reader marks an answer a valid judgment
      (`_settle_completion`).
    - `checkpoint.consumed` requires the claim counter and refuses claim
      records that exceed it: a missing counter is unknown usage, never an
      unused allowance.
  - `quadratus/obligations.py` (pure) builds the one view the planner and the
    audit both read, and result.json records it (`obligations`). It covers:
    requirement status and owners; each task's state (held, continues,
    continued more than once) and its measured code/test/line consumption
    against its ceiling; each recorded check's passed cases, current only on
    this source and gate (otherwise stale or wrong gate); judgments without
    a valid verdict; and capacity after the held finish (calls, tokens,
    seconds, slots, recovery).
  - `dispatch_floor` and `_mandatory_stages` refuse a serial task before any
    call when the calls left cannot pay its known mandatory stages: lead,
    each required review, a contract-required design review, or a security
    task's draft and verification. These are exact counts that never
    predict revisions.
  - `RunLimits.finish_wall_seconds` (policy `finish-wall/1`, 120 s per
    pending stage) holds wall time for the goal question and the audit:
    ordinary calls get what remains after the holds, and the goal question
    leaves the audit's.
- **A trim gets what the harness measured** (R20 on c7e9449). t2's one
  scope trim, told only the total, spent 8 rounds and 373,366 reported
  tokens, more than its draft, and the run ended with 141,947 left under the
  200k ordinary-call reserve. `_trim_evidence` gives every trim site
  (finished draft, capped draft, revision) per-file counts split into code
  and tests and the task's diff (`project.diff_contents`, the one
  rendering `Project.diff` uses), measured fresh and filtered by file
  identity on both sides to what every scope over the task permits; a diff
  over `_TRIM_DIFF_MAX_CHARS` is stated as omitted, never cut. No call,
  cap, target, stop or allowance changes; the token effect is a hypothesis.
  `result.json` `budget_note` records, from the terminal budget snapshot,
  when what was left sat under the ordinary-call reserve: an observation,
  never the stop or an outcome fact. It is token capacity only: whether the
  remainder covers the summary-call reservation
  `min(reserve, SUMMARY_CALL_RESERVE_TOKENS)`, never a prediction of
  admission, since calls, time and workflow are other gates (root reviews
  of b4d8398 and 38ec681).
- **Capped work in the final parallel batch meets one review** (r05). A
  child has no gate and no slot context, so t2's capped Grok draft merged
  and the job stopped GoalUnconfirmedAtCap with calls left.
  `_review_capped_final_child` sends the merged source to one reviewer
  from another vendor when the batch spent the last slot and the merge
  gate passed. A clean verdict lifts the cap and records coverage;
  anything else is held. No lead call follows, so a capped lead stays
  capped.
  An approval there is a finished review (NO FINDINGS or a REVIEW: COMPLETE
  line) with no BLOCKING and every covered requirement MET; non-blocking
  notes beside it are kept on record, not answered (r07 on 820bed6: R8-R10
  MET, REVIEW: COMPLETE and six low-severity notes were held because only a
  bare NO FINDINGS counted). Missing lines, NOT MET in either order and
  BLOCKING still hold.
- **Native delegation is denied to every seat without the worker bridge**
  (r04 on 5366412). The goal question at the cap used Claude's own Agent
  tool, reported four children still running and 1,206,662 unattributed
  auxiliary tokens, and stopped the job on the per-call ceiling.
  `runtime.NO_NATIVE_DELEGATION_ROLES` (orchestrator, auditor,
  requirements-review, consultant and every review seat) gets the
  verifier's denial with read tools kept. Leads keep the vendor default and
  the bounded WorkerBridge. Grok's unrestricted seats still take their
  prompt by file, where `--disallowed-tools` is ignored; there the record
  stays the check.
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
- **The Stage B check declares the harness report, and a cache-free pytest
  is the same check** (series rule-7590b13 f3, 2026-10-07). A gate failure
  buys a fix call only when the check carries `--quadratus-report={report}`
  and the report shows plain assertion failures (phase 3, #25); the packet's
  check never carried the token, so the first failing gate of the cycle
  (the lead's own `tests/test_ui_rename.py` asserting on a node runner's
  `# pass 0`) was `CheckUnattributable` and the run stopped with six tasks
  closed and no repair call. The packet now declares the token. The same
  suite had also been running twice per gate, once as the operator's
  `check` and once as the scanned `declared-python`, because
  `-p no:cacheprovider` made the two argv differ; `_check_identity` now
  treats that switch as the non-selecting plumbing it is, so the declared
  pytest is named by the operator's and runs once, with the report. The
  node `extra_checks` gate has no report producer and stays unattributable
  by design: a node failure is still the operator's.
- **The Stage B cell budget is 5M and the reserve 250k** (Davis,
  2026-10-06: "the reserve and the overall token count are now in play").
  Raised from 4M after series rule-7590b13 f2: all three tasks' work was
  finished (drafted, reviewed, revised, rechecked, design-reviewed) and
  the cell stopped at 3.76M with 237k of headroom when the third task's
  one-turn close-out asked for the full 250k reserve, because a codex
  summary call is not argv-capped and so never gets the 64k allowance
  (`CLISpec.summary_turn_capped`). The DONE turn and the audit would have
  needed about 250k more, so the cell needed about 4.05M; f1 used 3.45M
  on three tasks. On this engine a task costs about 1.2M (an Opus
  collaborator review 260k to 560k, an Opus design review about 200k, a
  revision 100k to 450k), so 5M covers four tasks with the 1.5M per-call
  ceiling unchanged. The 4M history follows.
- **The Stage B cell budget was 4M** (Davis, 2026-10-06).
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
  write is confined before it happens (`_confined_fixture_target`: exactly
  `.quadratus/capture-fixtures/<task id>/<name>`, no symlink at any
  component, the resolved target inside the resolved project) and at the
  operation itself (`_write_fixture_bound`: every component opened
  `O_NOFOLLOW` relative to the handle of the directory before it, the file
  created `O_EXCL`, so a link swapped in after the check is refused and an
  unexpectedly present file is never overwritten; Codex reviews of 351d3ba
  and 6844b97). The bytes go to a private temporary name in the task
  folder and the declared name is created only by linking that inode after
  the whole write succeeded, which fails if anything appeared there; so the
  declared name never names a partial or foreign file, nothing is deleted
  by the declared name, and a failed write leaves the declared name absent
  (Codex reviews of cc045a1 and a7cde45: a pathname unlink can delete a
  replacement, and a partial file under the declared name became evidence
  later). Any supply problem stops the design check unverified before a
  capture or review can run on it (Codex review of ada4c75). Every line
  that starts like a `FIXTURE` header is accounted
  for, well-formed or not, so a trailing or malformed duplicate keeps
  nothing for its path. No write grant changes, and a committed sample
  elsewhere is still checked by the capture itself.
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
  `_capture_candidates` reads the whole reply: a declaration is
  marker-backed (any-case `capture:` followed by `{` or `none`, wherever it
  sits on its line), and every other attempt is a conflict only, never a
  declaration (a marker with any other tail or none, a line outside the
  marker lines that starts with `{`); exactly one declaration with nothing
  conflicting is read, anything else is refused without a call (Codex
  reviews of 351d3ba, 6844b97 and ada4c75). A
  reply with no marker still stands as the verdict it was: one bounded
  declaration round and no second call because parsing failed (Codex,
  terminal audit of rule-3572b72), so there is no re-ask. The one
  redeclaration is shared by both routes that can ask for it (the capture
  check and the blind review): after it is spent, a blind review stays
  unverified, and the first receipt is kept as written.
- **A capture step can answer a dialog** (`{"action": "confirm",
  "selector": ...}`; Davis, 2026-10-07: "add the verification to f5 so it
  can use screenshot capture effectively"). Codex's static read of the
  saved f5 source: the empty state shows only when `/api/projects` is
  empty, the only UI route there is a delete behind a `confirm` dialog,
  and the capture's click, wait and file steps cannot answer one, so no
  declared route to the state existed and f5 stayed unverified on every
  cycle with all four graders passing. Playwright dismisses an unanswered
  dialog, so a plain click on a delete control cancels the delete. A
  confirm step declares the text its dialog must show
  (`"message"`), registers a handler for its own click, and accepts that
  one dialog only when it is a confirm whose whole message equals the
  declared text (whitespace collapsed; a longer message that merely
  contains it is another operation, Codex review of 4a51291); any other
  dialog is dismissed and the step fails, and no dialog is a failed step,
  so an unexpected, wrong or missing dialog stays an honest unverified
  outcome and nothing is ever auto-accepted (Codex, 6038178890). The
  permission covers one dialog, the first: every later dialog the click
  opens is dismissed, recorded under `extra_dialogs`, and fails the step
  (Codex review of ca60892: two matching confirms were both accepted and
  a prompt after a valid confirm went unrecorded). The recorded message
  is kept whole (600 characters, twice the declared maximum) so the
  evidence check compares it untruncated, and the check reads only a
  string message, never a stringified container. The dialog's type, message and whether it was accepted are
  recorded on the step, and the evidence check accepts a confirm step
  only when its record shows the declared dialog accepted (Codex review of
  ce35fb6: a record with no dialog, or another dialog, passed the
  checker). The handler is bound to that one click and removed on every
  exit, dialog or none, click error or timeout, so a dialog a later step
  opens is never answered under an earlier step's declaration (same
  review: a `once` handler stays armed until an event arrives). It is
  validated, carried through the CLI as `--confirm SEL MESSAGE`, and named
  in the orchestrator's capture rule.
  Because it changes the preview's state, such a capture is rendered one
  view per fresh preview (`preview.mutates_preview`, `--view`): the first
  view's delete must not empty the list for the second. The views of one
  capture share one attempt token (`--attempt`) and one capture allowance
  spent across them beside the one total deadline; a partial render keeps
  the other view only as a sibling (same attempt, target, source
  fingerprint and checked steps, every step passed, files present), never
  relabels old bytes with a new identity, answers with its exit code for
  the views it rendered, and leaves final acceptance to the checker, which
  needs every view (Codex review of 4a51291: a kept view took the new
  render's source and declaration, and a stale failed sibling ended the
  next attempt). Whether the preview reseeds on start is the profile's
  property (the Stage B profile's server seeds on each start); a second
  view whose final wait was already satisfied before its steps is caught
  by the evidence check as a declaration problem. No goal, grader or
  profile change was made.
- **A node check has a producer, and the report token never reaches a
  model** (series rule-58a4625 f3, 2026-10-07). Two findings from one cell.
  The packet's pytest check now carried `--quadratus-report={report}`, and
  the APPROVED CHECK COMMANDS guidance showed the lead that argv verbatim,
  so two leads ran it and got exit 4 (`unrecognized arguments`); the token
  is the harness's own and `integration.model_facing` strips it from every
  command a seat is told to run. Then the node extra check failed in the
  lead's own `tests/ui/load_app.test.js` and, with no producer, was
  unattributable by design: the second cycle in a row that f3 ended on a
  failing node UI test with the work done and the graders passing.
  `_gate_producer/quadratus_gate_report.mjs` is a harness-owned node:test
  reporter (`quadratus-node/1`, nonce- and digest-bound like the pytest
  plugin): a `node --test` check declares
  `--test-reporter-destination={report}`, and the harness replaces that
  argument with `--test-reporter=tap --test-reporter-destination=stdout`
  (the count reader needs the runner's text) plus its reporter and the
  report path, inserted directly after `--test` because node reads an
  option after the first test file as another file (probed on node 22;
  custom reporters need node 19.6 or later, so a Mac probe is the
  qualification). A failed test body whose cause is node:assert's own
  `ERR_ASSERTION` is a plain assertion; any other thrown type, a hook
  failure, a cancelled subtest and a file whose process failed (a
  collection error) are not the application's to repair, exactly as the
  pytest rules read them. Node also fails the parent of a failed child
  (`subtestsFailed`); that aggregate is skipped, since the leaf's own event
  carries the failure, or an ordinary nested suite reads as a setup error
  (Codex review of f8d8c03).
- **A declared step that does not happen is a declaration defect, and a
  redeclaration ends in a wait** (series rule-58a4625 f2, 2026-10-07). t2
  was drafted, reviewed, revised and rechecked, then the harness capture
  exited 1 on the orchestrator's declared wait (`#project-search-count:empty`
  never matched after the clear click) and the task stayed unverified with
  no redeclaration, while the sibling defect (a final wait visible at load)
  already bought one. The capture tool exits 1 only for a failed step
  (`_step_failure`; a crash inside the capture exits 3 since the Codex
  review of f8d8c03, because an uncaught exception used to exit 1 as well
  and a browser crash must never buy a declaration call), so that case now
  gets the same one
  `_recapture_declared` round with the capture named as the source; a
  recapture that succeeds continues into the ordinary check and review, one
  that fails stays unverified with no fix call, and exit 2, preview and
  integrity failures keep their old handling. In t1 the redeclaration was
  `{"path": "/", "steps": []}`, accepted and verified because there was no
  wait to check; a recapture must now end in a wait on the changed state or
  it is no usable declaration (the prompt's own rule, enforced).
- **A capped declaration call is a missing declaration, never a run stop**
  (series rule-7590b13 f5, 2026-10-07). The 6-round cap above did its job
  on grok's capture-redeclare call (6 of 6, 159k tokens, `cancelled`), and
  the `TurnLimitReached` it raised passed straight out of the design check
  and ended a run whose four graders all passed, with its one task never
  closed. Both declaration calls (`_recapture_declared`, `_supply_fixtures`)
  now catch the cap through `_capped_declaration`: the partial text is kept
  as `<role>-capped` evidence and never read for a `CAPTURE:` line or a
  `FIXTURE` block, the record says the call stopped at N rounds with no
  declaration, the redeclaration stays spent, nothing is re-asked, and the
  check proceeds exactly as after a reply with no marker (unverified, no
  fix call). A cap on a review seat is already handled this way
  (`_capped_review`); the declaration seats were the two left raising.
- **A view's files are bound to its entry by digest, and a dialog record is
  the compared text** (Codex review of ca0ad65). The attempt token said two
  partial captures were siblings; nothing said the retained screenshot was
  still the one that render wrote, and a replaced `page.png` of the same
  size was kept and accepted. `capture` now records `files` (sha256 of
  `page.png` and `evidence.json`) in every view entry; `_kept_views` keeps a
  sibling only while its files hash to that record, and `check_records`
  verifies every view's digests before reading the screenshot (a missing
  record is unverified, a disagreeing one is an observed mismatch). The
  session's settlement compares the approved snapshot before re-running the
  check, so a replaced render still lands as the J9b observed comparison
  rather than as the check's own failure. The confirm step's record holds
  the message as it was compared (whitespace collapsed, whole up to 600
  characters) so the evidence check compares exactly what the step did; a
  non-string message is recorded by repr under `message_repr`, never
  matches, and `message` is null. The dialog records are written in the
  click's `finally`, so a click or pause that raises after the declared
  confirm was accepted still says so (Codex review of f30e8b4).
- **The digest manifest is held to a receipt the harness keeps outside the
  project** (Codex review of 180012d). A manifest in the same mutable
  summary as the files proves only that nobody rewrote one without the
  other. `preview.capture_task` fills a `receipt` with `view_receipt` right
  after each view's capture process ends; `Session._harness_capture` keeps
  it per task, and every `check_records` of a harness capture (the design
  check, a recapture, the design-fix recheck, settlement) passes it: a view
  the harness did not measure is no evidence, a record that disagrees with
  the measurement is an observed mismatch. Self-captures have no receipt.
  The checker also requires a digest for every file a render writes
  (`VIEW_FILES`), refuses a symlink anywhere from `.quadratus` down to the
  view folder, and a sibling refused in a partial merge because its bytes
  changed is recorded under `not_kept` with `mismatch=True`, which the
  check reports ahead of the incomplete set it leaves behind.
- **An absent receipt refuses; a new attempt retires the old one first**
  (Codex review of 2e57e94). `_capture_receipt` returns an empty receipt
  for a harness task with none, so every view reads as unmeasured; only a
  self-capture gets None. `_harness_capture` adds the task to
  `_harness_tasks` and drops its receipt before the eligibility and
  profile checks, so no exit leaves an older measurement standing, and
  settlement asks the same accessor. Reuse is held to the receipt too: the
  harness passes `--measured VIEW FILE SHA256` for the views it has taken
  and `_kept_views` keeps a sibling only when its digests equal them. A
  refusal names the file and both digests (`file`, `recorded`,
  `observed`), never inventing an observed digest for a missing file, and
  the approval snapshot (`_capture_state`, `_snapshot_files`) covers each
  view's `evidence.json` beside its screenshot. A measurement that could
  not be read is never dropped (Codex review of 4a273a3): `capture_task`
  stops the attempt unverified when a view's files cannot be measured, and
  `capture_argv` writes an unreadable digest as `missing`, which the parser
  keeps as a present measurement that matches nothing.
- **A frontend label must name something that renders** (recovery
  diagnostic lane, 2026-10-09). The orchestrator picked kinds from bare
  label names and was never told that `frontend` commits a task to a
  real-browser capture; it labelled a CommonJS state model frontend and the
  label alone ended the run before any lead call. `_KIND_REQUEST` now
  states the rule, and a frontend task whose declared `permitted_paths`
  name no rendered file (`_UI_PATH`) is sent back once
  (`_FRONTEND_WITHOUT_RENDER`, counted against the dispatch corrections).
  Named again as frontend, the label stands and capture applies in full:
  this is a correction, never a waiver. A frontend task with no declared
  paths is unchanged.
- **A capture profile is checked at load as far as it can be without
  running it** (UI diagnostic lane on 4a273a3: the operator wrote
  `{project}` in the preview command, which `env` accepts but the command
  ran literally; it exited 2 at the first preview, after 1.48M tokens of
  editing and review). The preview command now resolves `{project}` and
  `{project}/relative` to project-relative paths, the way `env` does,
  refuses any other `{name}`, and refuses a script an interpreter is told
  to run (`python x.py`, `node x.mjs`) that is not a file in the project.
  The profile loads before any model call, so these cost nothing. The
  script is found through a bounded option grammar (Codex review of
  3e95645: `python -u missing.py` and extensionless scripts slipped by):
  Python single-letter flags, `-W`/`-X` values and `--`; Node long
  options and the known valued ones (`-r`, `--require`, `--import`, ...).
  `python -m`/`-c` and `node -e`/`-p` name no script; an option the grammar
  does not know, or one that prompts or exits (`-i`, `--version`,
  `--check`), is refused. A project file merely named like an interpreter
  (`./python3`) is the program itself and is not parsed.
- **Stage A+B is connected: typed delivery records are the controller's
  account** (blueprint 2026-10-17; connected batch 2026-10-18). `delivery.py`
  holds the records and pure transitions; `session.py` is the adapter.
  Review findings get stable ids (`RF<TASK>-<n>`) with a basis (requirement,
  reproduction, evidence or none) and an impact read off the reviewer's
  own BLOCKING line (`| basis: ... | impact: ...`). Every recheck, the
  post-merge recheck, the reassessment and the final audit read per id
  (`judgment.recheck_verdicts`): UNRESOLVED wins, a duplicate or missing
  line settles nothing, INVALIDATED needs checkable evidence, NEW lines open
  findings of their own with their basis and impact, foreign ids apply to
  nothing, and only the designated judge settles a finding (the seat that
  raised it in recheck or reassessment; the audit for a concern carried to
  it), on the binding it was handed. A finding with no basis and no
  immediate risk does not stop the run: it is carried to the audit by id,
  and the requirement it touches is not met until a judge closes it. A
  data-loss, integrity, security or irreversible impact holds the close
  whatever its basis. `recheck_state` is not a live reader any more.
  Evidence obligations (`O<n>`) are registered for every NOT RUN item, skip
  fact and declared acceptance behavior, with the harness's gate as runner
  when it can run what the item names and None (a reported prerequisite)
  otherwise; execution is discharged only by a receipt bound to the current
  source, gate, runner, environment and current requirement revisions, and
  sufficiency comes only from the audit (an explicit `O<n>: SUFFICIENT`
  line, else its requirement verdict), bound to exactly that evidence. The
  run's state is a durable job store (`jobstore.py`, `generations.py`):
  immutable content-addressed records under one manifest, fsync before
  rename, a latched refusal on any failure, readers that take the manifest
  over `budget.json` and `checkpoint.json` (now exports), and nothing falls
  back to an older generation.
- **The A+B boundary correction** (root's review of f4fe5b7, 2026-10-18)
  closed the connected batch on three boundaries. *One packet, one
  predicate*: `Session._packet` (`delivery.EvidencePacket`) is the binding
  of every judgment, cache, status restore, reassessment carry and final
  acceptance, and its digest moves with the source, each requirement's
  revision and text, the gate, the environment, the evidence identity,
  every recorded check (outcome, receipts, output, the environment and
  revisions it ran under), the findings with the binding each was last
  judged on, the obligations with their receipts, the rulings, the
  decisions and the authority record. An audit whose packet moved while it
  ran is void, a failed audit is not a final audit on record, and the
  status snapshot and a reassessment are carried only on the exact packet.
  After sufficiency, `delivery.acceptance` decides: a MET stands only when
  every bound obligation has an authorized runner, ran on the current
  identity and was judged sufficient about exactly that evidence, and no
  finding touching the requirement is open; INSUFFICIENT, two sufficiency
  lines for one id (a conflict is no verdict), unjudged, stale and a
  prerequisite with no runner all lower it. A receipt carries the check's
  own environment and revisions, never now's; an execution goes stale on
  gate or environment drift; a NOT RUN item is routed to the gate only when
  the gate names its paths whole; INVALIDATED closes a finding only on
  evidence the record resolves (a project file or test, a recorded check,
  receipt, command, finding, obligation or requirement id), and the audit
  invalidates or keeps a carried finding, never resolves it. *The route*:
  the task's selected finite route (mandatory stages, one revision and one
  recheck per reviewer, a parallel child's post-merge rechecks, the
  close-out where a model makes one) is admitted whole before the first
  call or the task is NOT RUN; a second fix cycle is admitted on its own
  before it starts; the route's checks count on the wall at their measured
  time, never at the configured timeout. The gate fix is deliberately not
  on the route: it is bought at the moment a check fails and a refusal
  there is recorded (the failed check stands), because costing it in
  advance refuses every task under the normal bounds (2M tokens, 500k
  editing exposure: the route is exactly 1.6M). *The effect lifecycle*:
  every model call, check, observed check, original-test run, skip
  baseline, evidence run, capture, probe, fixture write and merge is an
  operation under the session's own owner id (`OP<owner>-<n>`, record
  `operations-<owner>`, a parallel child's own); an exception settles it as
  failed, an interruption leaves it uncertain and unsettled; an effect
  settled after the checkpoint's watermark refuses the resume (a late
  read-only call is only named and asked again); the terminal record is
  committed with the seal before `result.json`, and a run whose terminal
  could not be committed is not completed; the authority record (the
  requirement revisions, rulings, decisions and the plan gate's receipt) is
  committed at each quiescent point and a resume needs the same one; the
  store decides on the manifest on disk, never its cache, and refuses to
  replace a manifest that moved under it. Accepted limits: `.quadratus/`
  metadata writes (source excludes, retired fixtures, the run lock,
  resumed-by) are harness-owned and not operations; a capped parallel
  child's clean-review lift is not packet-bound, the final audit that
  follows it is; a shared store under two concurrent Sessions is refused at
  the manifest, not coordinated (root's critical 3, non-blocking).
- **The remaining-boundary correction** (root review of 04a967d and
  Astra's durability review, 2026-10-18) closed what the first correction
  left as shared contracts, not sample patches. *Packet*: every record an
  acceptance path consumes is digested whole (checks and observed checks
  with their receipts' provenance, selection, requiredness, skips and
  output; design checks; scratch proofs; evidence requests), and a check's
  execution identity (environment, requirement revisions, gate, source) is
  frozen before the command launches (`_execution_identity`) and carried
  frozen with any drift named (`_consumed_identity`): a run is never
  credited to a revision that did not exist when it launched, and a receipt
  built from it binds the superseded revision, which discharges nothing
  current. *Continuation*: any operation settled after the last quiescent
  point and never consumed by the record refuses the resume, a read-only
  judgment included (a NOT MET the coordinator died before parsing is not
  asked again as if never given); the reply or outcome is retained as the
  operation's bounded immutable receipt (`receipt-<opid>`, 64,000 chars
  with the whole's digest) committed in the settlement generation and
  named in the refusal, and a receipt that cannot be read back refuses on
  its own. Only an outcome the harness itself reported (one of this
  package's exception classes) settles an operation as failed; a suspected
  partial turn, an interruption and any library or OS error leave it
  uncertain. *Authority*: the authority record is committed before a
  task's or a batch's first call (`_authority_before`) as well as at each
  quiescent point; every operation and every budget reservation names its
  digest; the cross-vendor requirements review is recorded beside a
  requirement row as a review of exactly that text digest, never as the
  human's decision receipt, so an inferred requirement is never
  `approved` by a model and a revision the review never saw carries no
  review. *Readers*: the committed terminal carries the goal, the
  explicit-task semantics, each task's closing state and the budget stop,
  and the monitor, the history and the story derive completion from it
  when no export exists; no committed terminal is unfinished, never
  completed. *Route*: a hypothetical gate fix is still not pre-reserved at
  dispatch; once a real check has failed, the repair it selected (the gate
  fix, what the task still owes, and the check's measured wall time, with
  an unmeasured wall said so) is admitted whole before the editor writes,
  or the failed check stands and the refusal is recorded
  (`_gate_repair_stages`, scope `gate-repair`). *Residuals closed on the
  same contracts* (root and Astra on 6bf1afc): the frozen execution
  identity carries the gate, its selection and its cwd through every check
  consumer, so a gate reconfigured at the consumer seam is named as drift
  and the record keeps the gate that executed; the authority is committed
  at the common effect-admission boundary (`_operation` and every model
  call, intake and the requirements review included), its digest is what
  the effect names, and the requirements review freezes exactly the texts
  its prompt sent, so a text changed in flight is unreviewed, while its own
  ADD lines are recorded as its proposals (nobody compared them with the
  goal) on an id no row holds; readers consult the committed terminal when
  the export is truncated or malformed as well as absent, keeping the
  export's problem as a diagnostic, and say when the committed state
  itself cannot be read. From the author's adversarial review of e688af5:
  a parallel child's authority is its parent's (`_authority_parent`; a
  child published a record built from its own empty ledger over the
  parent's, every child call named it, and no run that ran a batch could
  be resumed); a routing decision is an operation under the committed
  authority with the decision as its receipt; operation ids are issued and
  rows settled under one lock (a sibling's settlement committed a row as
  settled in a generation holding no receipt for it), a receipt name is
  written once and a refused settlement is committed uncertain, and a
  resume checks the receipt it reads back is the operation's own (opid and
  digest); the acceptance on record consumes each served evidence record
  (`_consume_evidence`: the final audit's binding moves to the packet the
  record and its judgment left, a reassessment MET replaces the verdict,
  the auditor's own packet stays named), since digesting evidence records
  whole had left every served challenge 'not current' and re-drew the
  final audit on resume, and an evidence intent a resumed job never runs
  is closed as it stands and consumed; a failed call's receipts keep why
  it failed (the budget's stop on unknown usage names the transport
  failure as its cause, and a failure receipt keeps the cause); a
  checkpoint with no consumption watermark resumes only when nothing
  settled at all. *Repair routes*: a task's
  route peers are its own (set when its reviewers are drawn, cleared per
  task; before, only a lead recovery set them, so the pre-review subset
  gate's route omitted its reviews, revision and rechecks), the merge
  gate's route lists the post-merge rechecks and capped final reviews and
  no close-out, the subset route's wall counts the full gate after the
  revision where measured, and the security fix and the design fixes are
  admitted through the same helper (`_admit_repair`) before their editor
  writes, a refusal recorded and the verdict or render standing as found.
- **The evidence loop's scripted judges answer the typed protocol** (root's
  Mac run of `tests/lifecycle/test_step2_evidence.py` on bc421f8: 27 failed,
  10 passed, no skips; the earlier "environment" label was wrong and is
  withdrawn). `Session._reassess` passes the audit's implication no found
  map on purpose, so a positive reassessment of one requirement never
  overwrites the insufficiency its earlier NOT MET implied: the scripted
  auditor must judge the executed obligation explicitly, on a basis the case
  authors (`tests/lifecycle/replies.py`: `audit_reply`, `recheck_reply`,
  `requested_obligations`; nothing inferred from a MET, a receipt or an
  obligation's presence, and a malformed-reply case writes its raw reply).
  Reviewer findings answer one disposition per expected id, and a finding
  the evidence loop may test is a standing one (a basis given; a bare
  BLOCKING line is carried to the audit by id instead). The missing,
  explicit-insufficient and contradictory shapes stay held
  (`test_a_positive_reassessment_without_a_sufficiency_judgment_keeps_the_
  requirement_held`). Two product changes came out of the same receipt: the
  proof the harness itself ran for a challenge is an executed case for that
  requirement alone, bound to the record's source and identity
  (`_executed_cases`; without it no error-clause requirement could be
  established by the challenge the cap offers), and the final path
  reconciles carried pending evidence before coverage is read
  (`_requirements_satisfied` calls `_finish_pending_evidence` first; an
  awaited record moved the packet, the status restore was skipped, and a
  resumed cap stopped on "never covered"; a judgment that may have started
  stays held). *The ratio family's bound*: the family runs under
  `_ROOT_LIMITS` (2M, the default shape) since admission/2 below; a
  starved allowance (`_STARVED_LIMITS`, 1.2M) that cannot pay the known
  stages still refuses the task before any call and is kept as the
  control. The call calibrations make the route's calls (`blocking=True`:
  a standing finding per reviewer resolved on the recheck) so the
  allowance is spent exactly at the reassessment.
- **A task's route is admitted at its known stages; a correction is
  admitted when a finding selects it** (admission/2, `Session._plan_stages`,
  the fresh end-to-end A+B job on 21b342d). The old route costed every
  hypothetical call at its exposure ceiling (draft, revision, two rechecks,
  reviews, close-out: 1,600,000) and refused the first reviewed task of a
  default-bound run against the 1,473,864 left after a 126,136 intake and
  the 400,000 finish hold; the slot latch (`used = max_tasks`) then
  forfeited the other two slots and the stop read "the task cap (3) was
  reached" with nothing written. Root reconciled that no replan reaches the
  default case, and the policy was corrected deliberately rather than the
  latch deleted. The dispatch route is now the stages known before any call
  (the lead's draft at the editing ceiling, every required review, a
  required design review or security verification), the model close-out
  where one is made, and the finishing holds: 900,000 for a reviewed task,
  1,100,000 before the last slot. The revision with the write grant and one
  recheck per answering reviewer are not on it: `_fix_cycle_stages` admits
  them whole the moment a review has made a finding, every cycle including
  the first, through `_admit_plan` (scope `fix-cycle`), which still counts
  the held finish, so a correction can never invade the reserve. Unpayable,
  no editor is called, the findings stay open as the reviewer wrote them
  (`open`, never `unresolved`: nothing rechecked them), the task closes
  with them and the run ends incomplete and says so (`FindingsOpen`). No
  review is dropped to fit, no lower tier is counted as a saving, the
  explicit-list, parallel-batch and uncertain cases keep their fail-closed
  admissions, and nothing synthesises a slot or a completion. The stop is
  honest: a route refused at dispatch is recorded on the session
  (`_route_forfeit`, carried through the checkpoint) and every cap stop
  names it ("t2's route could not be paid from the allowance left (...),
  so 2 of 3 task slot(s) were forfeited") instead of the cap
  (`completion_decision._reached`). The forfeit is an election, not a
  proof: no replanning of the remaining slots is attempted and nothing
  claims a cheaper scope could not exist. Last-slot and last-wave tasks
  have no close-out, so there the known stages are the route and a
  shortfall is found at the mandatory admission (`dispatch_floor`); the
  serial full-route control therefore runs over three slots.
- **Two route candidates before NOT RUN; the second ends the job**
  (admission/3, the fresh job on bfd7a9a: t2's ordinary route, 900,000,
  missed the 884,138 left by 15,862 and the run forfeited two slots with
  nothing of t2 run). `_refuse_unpayable_dispatch` now costs the ordinary
  selected route and, when it cannot be paid, the same task, reviewers,
  gates, design and security stages closed by the harness's factual record
  (`scope: terminal`). Admitted, `_commit_terminal` records the job's
  commitment to end after that task before any call (`_terminal_route`:
  task, reason, selected cost, actual slots used, relinquished slots;
  carried by the checkpoint, kept as a `terminal-route` artifact, exported
  in result.json), `_closes_factually` and `_task_close_out` consult it so
  every repair's known remainder and the actual close-out agree, the main
  loop dispatches no ordinary task after it (a resumed episode included),
  and the cap's decision follows with the goal question and audit where the
  existing gates allow; the stop names the choice and the relinquished
  slots (`completion_decision._reached`). It is a policy choice to
  relinquish later slots, not a proof that no later task could fit:
  reported usage can be below an exposure and a read-only task is cheaper.
  No review is dropped, `_slots_used` stays the actual count, the finish
  hold is counted, a mandatory deficit is refused as before with no
  candidate, explicit lists and parallel batches are unchanged, and a
  failed predecessor, an open finding or a capped task still blocks
  completion. Two consumers were missed on 0ae5bb6 (Astra A1, A2) and
  are corrected together: `_final_work_slot` is the one task-bound
  boundary the capped-source handoffs (`_capped_final_draft`,
  `_checked_final_source`) read, so a terminal task's capped, checked,
  in-scope source meets the same bounded cross-vendor review as the
  numeric last slot's (every other guard stays with its caller, explicit
  lists and parallel children excluded); and `_admission_notes` tells the
  planner and audit what the record holds: NOT RUN only when
  `not_started` says so, the terminal choice with its spent and
  relinquished slots and never as execution, a refused correction as a
  correction after the task had started. `obligations.Capacity` shows
  usable slots (none after the commitment) beside the spent and
  relinquished counts.
- **A task is sized for its implementation and its executed proof
  together.** `_COVERS_REQUEST` says a requirement is delivered only with
  the tests that prove it in the same task (a passing case per rejecting
  path for an error clause); `_SCOPE_REQUEST` asks for implementation,
  proof and setup estimates summed into `max_lines` and refuses a delivery
  described with its tests merely deferred, and the blanket "make the
  setup its own task" advice is gone. The optional SCOPE `estimate`
  object (`scope.read_estimate`: exactly implementation, proof, setup;
  exact non-negative ints; sum equal to `max_lines`; a bool or an extra key
  refused) is kept on `TaskScope.estimate`, rendered to the lead and
  serialized; omitted is unknown, and it widens nothing: the bound, the
  1.5 stop and every counted line are unchanged. `_slots_note` now shows
  the planner the allowance after the held finish in the exposure classes
  admission charges (ordinary calls, tokens, wall; editing and reserve
  exposures; what a reviewed route and a correction cost), facts from the
  budget's snapshot. A revision told its room is also told to report a
  test that does not fit as `NOT RUN: <case> - <why>` (recorded as owed,
  the finding stays open, nothing resolved by saying so) rather than
  compress tests onto one line.
- **A task's proof is declared, bound and judged; never inferred from a
  green check** (proof/1, the fresh job on 136ebee: the planner's
  acceptance omitted three rejection paths the reviewer then demanded, its
  "empty history" expectation was wrong for the prior state, and the
  size trim deleted a mutation assertion inside an unchanged case that no
  record showed). SCOPE may carry `"proofs"`: scenarios with a stable
  `P<n>` id, the requirement ids shown, the operation, the starting state,
  the expected behaviour and the intended test location (`scope.read_proofs`,
  strict, refused whole when malformed; omitted is unknown, never complete).
  A planned write task under the requirements ledger must map every COVERS
  requirement to a scenario and name no uncovered one (`_proof_problem`,
  corrected once through the COVERS send-back allowance); the harness
  validates the mapping, not its completeness. Each scenario is an evidence
  obligation at dispatch; the lead binds it with `PROVES: P<n> -
  file::Class::case` lines at any editing delivery (`_record_proves`:
  every PROVES line is read; a malformed line naming a scenario makes that
  scenario's declaration invalid, cases cleared, and one naming none
  invalidates the whole reply's declaration, every scenario reset; a later
  reply of only well-formed lines restores a binding; problems are
  recorded). An acceptance obligation is discharged only by one passing
  check on the current source, under its gate and environment, whose own
  complete runs name every bound case as passed (`_acceptance_receipt`); a
  green check that ran something else discharges nothing, and a rebinding
  on the same source resets execution and sufficiency before any
  re-discharge (`evidence_binding` covers the case set and behaviour).
  **Modality is declared, never inferred, and never an exemption** (root
  and Astra on d515d51 and a1b1840): a scenario is an executable claim
  wherever its location points, and unexecuted it lowers its requirement
  (`_lower_unexecuted`) and fails the acceptance predicate. Only a scenario
  declaring `"evidence": "reading"` is a documentation claim, admitted at
  dispatch only for a documentation file and never for an error or refusal
  requirement (`_proof_problem`, applied to serial and parallel intake
  alike). It is never discharged by a check, never lowered as unexecuted,
  and never judged by the audit's MET on its requirement, however that MET
  cites: it counts only on the audit's own explicit per-obligation line
  (`O<n>: SUFFICIENT <why reading demonstrates this requirement and
  scenario, citing the file>`), and a SUFFICIENT that does not cite the
  scenario's file is not counted (`_judge_obligations`). The planner's
  choice of modality or file authorizes nothing: a runtime requirement
  mapped to README with a bare MET stays unproved. A legacy acceptance
  string (modality `legacy`) is the same way an unknown mapping that stays
  visibly unknown on the record: no case binds, no check discharges, no
  verdict is read as its sufficiency, and only an explicit per-id line
  judges it; it claims no execution, so it is not lowered as unexecuted,
  and unjudged it keeps its requirement unmet. `EvidenceObligation` carries
  `modality`, `location` and the consumed `scenario` metadata, all in
  `evidence_binding`; a reading or legacy row's binding carries the
  current source, effective gate and environment on every refresh, so an
  edit, a reconfigured gate, a moved location or a changed scenario after
  the judgment makes it stale by the ordinary `sufficiency_current` rule.
  The evidence packet (packet/3) carries each obligation's behaviour,
  cases, bound revisions, gate, environment, modality, binding, location
  and scenario, and every trim record; judgment fields stay out. PROVES
  is a strict protocol: distinct cases bind a set, an exact duplicate line
  is an invalid declaration, never collapsed.
  **The audit reads the whole scenario or accepts nothing** (Astra on
  00cfc02 and b18ca98): the audit and a reassessment are shown each
  obligation's behaviour whole and its full canonical scenario
  (`_scenario_line`); over `_SCENARIO_RENDER_MAX_CHARS` the line says the
  scenario is not shown, and the harness then counts no SUFFICIENT for
  that row, explicit or implied (`_scenario_shown` in
  `_judge_obligations`), drops an earlier approval on refresh, and keeps
  INSUFFICIENT. Wording alone is not enforcement.
  **A served evidence proof can be the receipt for an obligation it was
  bound to at intent, and nothing else** (root and SAME Astra on b18ca98:
  the final read-only challenge's execution lives in `evidence_requests`,
  which `_obligation_receipt` never read). `_serve_evidence` captures
  `bindings` before the proof runs: only existing explicitly bound
  test-modality obligations on the audited requirement or the reviewed
  task qualify, each with its ids, modality, location, whole scenario,
  bound cases, requirement revisions, policy, environment, source,
  effective and configured gate, and the proof's id, file and hash.
  `_evidence_receipt` turns a passed, undrifted proof into a receipt only
  when every captured field equals the obligation and the run as they are
  now and every bound case is among the proof's passed cases; the receipt
  keeps its scratch provenance (`E<n>:O<m>`, the evidence id and file) and
  is never an integration pass of the whole project. It feeds
  `delivery.discharge` through the ordinary refresh, so it is available
  to the reassessment before `_lower_unexecuted`; sufficiency stays the
  auditor's explicit full-scenario line. A rebinding, a moved source, a
  changed gate or environment, a reworded requirement, or a record without
  its bindings is no receipt. Nothing is synthesized from a requirement id
  or a green case: the lead still binds the case (`PROVES`), which is a
  claim of where the proof lives, never execution. The receipt carries the
  gate that actually produced the run (`producer_gate`, persisted at intent
  and confirmed after execution), never the obligation's; the typed
  predicate refuses a producer gate that is not the authorized one (root's
  F1 on 9bedde0). Exact parametrized case names survive, and a base name
  counts only when every recorded instance passed (F2).
  **A reviewer's scratch demonstration is the receipt the same way**
  (`_snapshot_scratch_bindings`, `_scratch_receipt`; root on 9bedde0): the
  task's bound obligations are snapshotted before the collaborator runs,
  the runtime measures the scratch files' hashes and the handed source and
  checks while the copy exists, and the `scratch_proofs` record carries
  them with the producer gate; the receipt holds only for the current
  handed identity, a passed proof, measured hashes and every bound case
  passed. Nothing is reconstructed from current state for an old record.
  **A capped lead's structured PROVES lines are read at the accepted
  capped-work handoff** (`_close_turn_limited` before the turn-limited
  check), since `_record_proves` ran only after a finished edit; the cap
  stays recorded, a declaration is neither execution nor a verdict, and
  no extra call is made.
  **`evidence: capture` is the modality for visual proof** (root on
  9bedde0): admitted only for design work with design evidence in force
  and never for an error or refusal requirement; a UI file is never routed
  through test, reading or legacy. Execution is the harness's own
  measurement (`_capture_measurement`): the task's latest design record
  verified, taken on this exact source, delivered to a reviewer from
  another vendor who returned a verdict, with target, steps, fixture,
  viewports and digests still those on disk and still passing the
  identity check. Sufficiency is only the design reviewer's explicit
  per-obligation line, given alongside APPROVED or BLOCKING
  (`_design_approved` admits APPROVED followed by nothing but those lines)
  and judged once the measurement is on record; the requirements audit
  may add INSUFFICIENT and never SUFFICIENT for a capture row. Missing,
  wrong or stale state or viewport, a changed source, fixture or image, a
  disabled or unavailable capture, an undelivered or capped review, or a
  missing per-row judgment all hold the requirement.
  The design reviewer reads the original goal and the authoritative text
  and revision of the requirements its capture rows bind (ids alone are no
  authority; text not on the ledger leaves the rows unjudgeable), and its
  per-row lines are read only for the rows it was actually handed
  (`review_rows`): a line naming another task's row judges nothing and
  overwrites nothing. The design record is labelled with what the reviewer
  was handed, measured before its call (state snapshot, source, delivered
  file hashes) and revalidated after it (`_design_review_drift`); a render
  replaced, removed or re-taken in another state, or a source that moved,
  makes the verdict usable for nothing, and the final replacement stop
  stays as it was. The record also keeps, immutably, the exact context
  the reviewer was shown before its call (`review_context`: the goal,
  each covered requirement's text and revision, each row's scenario and
  bound revisions); a per-row judgment is applied and a measurement
  counts only while the authoritative context still equals it
  (`_design_context_drift` names what changed), and a prior reply is
  never relabelled with live row contents. A capture scenario's
  canonical identity carries the declared visual state
  (`_capture_context`: the SCOPE capture declaration normalized by
  `preview.validate_capture`, route and ordered steps with every
  supported field, the file step's path and the confirm step's message
  included, or the lead's undeclared self-capture), so two scenarios that
  read the same but show different states, fixtures or confirmations are
  different proofs.
  **A later task that declares the exact same scenario on the same
  requirement revisions and modality takes the proof over**
  (`_supersede_obligation`; root, SAME Astra and SAME Sol on 4fac862):
  the earlier task's row, run or not, is marked `superseded_by` and kept
  on the record, immutable, and no longer holds the requirement; the
  successor must establish fresh proof and sufficiency of its own (a
  capped repair continued by a later one, a repair redone by a later
  repair: the debt moves to the declaration that owns it, never
  disappears). A scenario differing in any field, including a capture's
  declared visual state, another requirement set or revision, another
  modality, a legacy row, a NOT RUN or skip fact, predecessor cap debt
  and open findings are untouched; sharing COVERS alone supersedes
  nothing.
  **The rows claim the effective capture, and supersession follows it**
  (`_rebind_capture_rows`, `_reconcile_supersession`; root's recapture
  identity gap on c9a665b). The one accepted redeclaration
  (`_recapture_declared`) replaced the captured state while the task's
  capture rows kept the dispatched one, so the fresh review was compared
  with a row it was not about and the rows said ran with no mismatch. At
  the accepted declaration, before any capture or review of it, this
  task's capture rows take the effective declared state as their
  scenario; the dispatched declaration, its problem and whatever was
  measured or judged under it are kept on the row (`capture_declared`,
  `capture_lineage`, never rewritten; `declared_capture` and
  `rebound_rows` on the design record), and execution, receipts and
  sufficiency start again, so a reviewer shown the dispatched state is
  never relabelled as having judged the redeclared one
  (`_design_context_drift` finds the shown row changed). Goal and
  requirement texts, operation, state, expectation and location are not
  touched; rows of other tasks, other modalities and superseded rows are
  not touched; a repeated, wait-less, fixture-less, invalid or withdrawn
  declaration, and any declaration after the one is spent, rebinds
  nothing. Supersession follows the effective state: a row this task's
  dispatched state superseded is lifted with its own execution and
  judgment, insufficient or otherwise, exactly as they were
  (`supersession_lifted`; the pre-supersession reason is stashed and
  restored), and an earlier task's row that declares the effective state
  is superseded now by the ordinary rule. Fixture-side, a scripted design
  reviewer that approves verified renders judges each row it is shown
  with a concrete observation of that fixture's page
  (`test_harness_capture._observed`), and a fixture whose page is not the
  shared REPAIR_SCOPE's Import toolbar declares its own scenarios: a
  verdict about an Import button the page never has describes nothing.
- **Historical (retired 2026-10-20 with the size trim it served): a trim
  is evidence, and its assertion preservation is owed a verdict**
  (`_trim_snapshot`, `_trim_settle`, `_trim_pre_contents`, `_trim_owed`,
  `_trim_block`, `_read_trim_disposition`). Every trim has a record id
  `T-<task>-<n>`, unique across the run and its parallel children (a merge
  remaps a collision once). Before a draft, capped-draft or revision trim
  writes, every permitted Python source file and every permitted test or
  support file by name (`_TRIM_SUPPORT_MARKS`), whole, filtered by the
  task's own scope and the operator limits bound at that moment, is kept
  with its hash, that policy, the proof bindings and the source identity
  as an immutable artifact whose sha256 the record carries
  (`pre-trim-tests`). No import grammar decides what an assertion leans on
  (root's ninth control on 00cfc02: a walk that follows some import forms
  and slices in silence describes partial evidence as complete); the
  capture is bounded by `_TRIM_SUPPORT_MAX_FILES` files and
  `_TRIM_EVIDENCE_MAX_CHARS` characters, and past either bound the record
  says so, keeps only hashes, and the trim stays owed. The artifact is read
  back and verified against that digest on every consumption; missing,
  unreadable or altered is "not on file". Unknown is owed, never clear: a
  project that could not be read before or after the trim leaves a record
  whose effect is unknown and which stays owed. A judging seat (the task's
  reviewer and rechecker; the final audit for every owed trim) is shown,
  per id, the comparison from the immutable pre-trim evidence to the test
  and support files *as they are now under the record's own persisted
  policy* (never an unbounded fallback for a late review; a record without
  its policy is not judged), so an edit after an earlier PRESERVED is
  re-judged against the full pre-trim assertions and the earlier approval
  is named stale. Every render first clears what an earlier render showed,
  and a verdict is consumed only against the current readability and
  identity. Case names and counts decide nothing: the seat judges whether
  every assertion the covered requirements and declared scenarios need
  survived (a helper, loop or renaming is preservation; a deleted mutation
  or state assertion is not) and answers one line per id, `TRIM <id>:
  PRESERVED` or `TRIM <id>: LOST - <what>` (a bare `TRIM:` line applies
  only when one trim was shown). A disposition is bound to exactly the
  evidence shown: record id, verified artifact digest, current source,
  the policy, and the current identity of the task's covered requirement
  revisions and proof scenarios, so a reworded requirement or a rebound
  scenario after an approval owes the trim again (root's controls on
  a1b1840). PRESERVED without the evidence on file, readable now and shown
  is refused and recorded as such, two lines for one id settle nothing, a
  verdict on another id judges nothing, and an owed trim sends DONE back
  after the audit (`_requirements_satisfied`) rather than being approved
  by silence. `trim_records` and `proof_binding_problems` are carried by
  the checkpoint and merged from parallel children.
- **Every selected repair carries the task's known remainder** (root's
  control on fef1e27, the default bound: a fix cycle admitted as
  revision + recheck, 700,000 against 773,864, omitted t1's known 200,000
  model close-out; the revision ran and resolved the finding and the job
  stopped `FinishReserved` at the close-out with the goal question and
  audit never asked). One helper, `_known_remainder(spec, after=)`, names
  what the task still owes after a point on its route: after the draft,
  each required review; after the reviews, the required design review
  (`_design_review_ahead`, the contract's `design_review`); at every
  point, the model close-out where one is made. It is on the route of
  every optional editor: the fix cycle (first and further), the gate fix
  after a subset or full gate, the render fix (a required design review
  of the repaired capture is counted when no reviewer has blocked yet),
  the security fix, the size-stop trims (`_admit_trim`: the trim, then
  what followed the overrunning call on its admitted route) and the
  no-edit-cap revision retry (admitted before the note that offers it;
  refused, the capped revision takes the capped paths as before). The
  measured gate wall is counted where known (`_route_wall`: nothing to
  know without a gate, else the measured run or reported unmeasured).
  Nothing is listed twice: a correction's rechecks follow its findings
  and are named by the caller, the held finish is counted once by the
  admission, and a last-slot task owes no close-out. A refused repair
  stands as found: the finding open, the overrun failed, the cap capped.
  Hypothetical later repairs stay conditional; known judgments never
  leave the cost.
- **The close family** (Davis, 2026-10-26; one cohesive batch, not one
  fix per live run). recovery-handoff-01 ended FindingsOpen with slots
  unspent: Sol's SCENARIO claim on t3 used an em dash, `_SCENARIO_CLAIM`
  took only `[-:]`, the claim went to the editor as code work, stayed open
  as held review debt, and the batch close after t4 and t5 (both rechecked
  clean after the merge) stopped on it because the batch close never
  consulted the hold the serial close applies. Five rules now, all pinned
  by `tests/lifecycle/test_close_family.py`: (1) a typed claim types with
  any normal separator (`_TYPED_SEP`: hyphen, en dash, em dash, minus,
  colon) and only with its full semantics; a blank field is no field
  (root's old-source control) and `_typed_finding` stays `(None, None)`
  for it. (2) A claim that opens as typed but is incomplete, names a
  scenario the task does not declare, uses an id that is not `P` plus
  digits, or repeats a corrected scenario is held `unroutable`
  (`_typed_near_miss`, `_plan_findings`): the reviewer is told why at the
  recheck (`_routing_facts`) and may restate it in full, which the next
  routing reads before any editor round (`_read_restatement`,
  `_restated`); it is never the editor's instruction, and a kept or
  refused correction leaves it the reviewer's to judge against the planner's
  answer, not the editor's. (3) The planner's correction is one per
  scenario (`_scenario_allowance`, recorded as `task:pid`), and a reply
  with conflicting lines for one scenario changes nothing (`refused:
  ambiguous reply`). (4) One close policy (`_close_policy`,
  `_close_decision`) decides every close, serial task or merged batch, and
  `_stop_follows_close` predicts with it: a failing check without debt
  stops; open findings stop unless every one is held review debt
  (`_review_debt_hold`: ledger on, planner mode, a discovering check, and
  the legacy list and the typed record in positive correspondence both
  ways, an empty typed record failing closed, root's probe); held debt
  continues to the planner while a slot is left and goes to the cap's
  completion decision when none is (the goal question and the audit run;
  nothing completes over it). The closing task's COVERS are not required:
  inherited debt is debt. (5) Every close decision is a record
  (`close_decisions`), carried, sealed into the terminal state and published
  in result.json; the cap records why the goal question was or was not
  asked. `_record_deferred` shares the separators (root's probe: an em dash
  dropped a deferred condition); a deferred entry kept after the post-merge
  recheck is still not disputable debt (`_disputable`) and ends the batch
  as before. Unchanged on purpose: a generic provider failure, an unmerged
  child, a failing merge gate without debt, missing usage, budgets, and the
  independent reviews and audit. A judge's `NEW:` line is no finding only
  when its whole claim is the standalone sentinel (`judgment._empty_claim`;
  `re.match("(none|n/?a|nothing)\b")` dropped "Nothing rejects nonnumeric
  inputs…" in silence). Five older controls that pinned a
  FindingsOpen end in the last slot now pin the cap's `RequirementsUnmet`
  naming the finding its judge never closed, with the goal question and the
  audit spent from the capacity the finish reservation held for them.
- **A cap is a stop reason the vendor names, not a failure the count happens
  to fit** (recovery handoff repair, 2026-10-25, after proof-workflow-01 ended
  FindingsOpen with slots unspent). The installed grok CLI (1.0.62) ends an
  agent loop with `max_turn_requests`; the extractor knew only `max_turns`
  and the `cancelled`-at-the-count rule, so t4's second revision became a
  `ProviderError`, the parallel child was "not merged", a legacy open
  finding stopped the batch, and no planning call could continue t3 or t4.
  `_GROK_CAP_STOPS` now names both spellings; the typed cap takes the same
  path serially and in a child (`TurnLimitReached`, kept, merged,
  `turn_limited_records`, exact usage and turns on the row, the planner told
  what is owed and allowed to CONTINUES within the recovery allowance). What
  did **not** change, by root's boundary evidence on the old source: a
  refusal, a `cancelled` below the configured count, a stop this code has
  never seen, a cap stop that also carries an `error` (permission denied,
  authentication required), a pre-turn authentication failure, a refused
  budget and unknown usage are none of them a cap and none of them a
  continuation; each still stops after that one revision with the files
  preserved and no further writer or planning call. A generic
  `ProviderError` is never made recoverable because a turn began or a tree
  changed. The one record change beside the cap: a raised parallel child
  now leaves the durable handoff a scope conflict leaves (a `failed_records`
  entry naming its kept `parallel-unmerged` artifact, its cap baselines,
  `failed_tasks`), and the sealed terminal state carries the unfinished-work
  records, so the stop names a failed task with a handoff and not only a
  finding. `max_turns_reached` is an event name in that binary, not a stop
  reason, and is not read.
- **Frontend evidence comes from a real browser** (`browser.py`, optional
  `playwright` extra): screenshot, console errors (including late throws),
  failed requests. Deterministic and dumb by design — it produces evidence,
  reviewers produce judgement.
- **The requirements auditor sees the harness's own check facts** (overnight
  r02, 20261011T134747Z-b14aa88b: the audit saw only "gate suite: passed",
  ran the browser tests in its own copy without Playwright, and marked R6
  NOT MET against a same-source pass of 121 Python and 64 Node tests). Each
  recorded command id now reaches the audit with its status, parsed
  passed/failed/skipped counts, and whether it ran on the delivered source;
  never a command line, and a reason is path-redacted like check output (a
  missing runner's exception names its path). It still judges for itself: a
  failure on the delivered source is evidence against it, a skip, earlier
  source or unknown count establishes no pass, and nothing turns into a MET.
- **Seats judging a source get the harness's run of it** (r08,
  20261011T203457Z-eaf86c90: reviewers' source copies carry no dependency
  tree, so their runs of the browser tests failed with MODULE_NOT_FOUND while
  the same tests passed in the project; revisions and re-checks looped on it
  until the token reserve). `Session._observe_checks` runs the task's own
  gate on the exact source before the draft review, before each re-check,
  and on what a turn-capped lead left, once per source and with no fix call.
  The seats get those receipts in place of the command list; the
  orchestrator sees them labelled as evidence only. They are never a task
  check, never a handoff and never a MET. Every receipt is bound to the
  gate in force as well as the source (`_gate_identity`: commands, cwd,
  timeouts, minimum tests): a check of another gate on the same bytes is
  history, never a seat's receipt, and a verification handoff needs a check
  of the gate the task itself would run.
- **A capture path keeps its #fragment, and a redeclaration sees the goal**
  (r05, 20261011T183849Z-869a1896: the goal prescribed /#q=Alpha; the
  orchestrator declared "/" expecting the harness to apply the hash "if
  supported", step 1 timed out, and the one redeclaration, asked without the
  goal, spent 228,533 reported tokens and hit its round cap). The capture
  rules now say a path may end in a fragment loaded exactly as written and
  that nothing is added to it; the capture-redeclare prompt carries the goal
  verbatim and the numbered requirements. Validation, the step allowlist and
  the one-redeclaration bound are unchanged.
- **A verification-only task over a source the harness already verified is
  not dispatched** (overnight r02 and r03: after a same-source harness pass
  and approved renders, a review-only t2 re-ran the tests where it had no
  browser, reported failures and NOT RUN, and in r03 spent 574,721 of
  1,843,534 reported tokens with no edits, so the run hit its reserve before
  the audit). `Session._verification_handoff` sends such a task back before
  any lead call only when the latest check passed on this exact source with
  every command executing cases and none failing or skipping, nothing open
  (check failure, finding, capped task, NOT RUN, unrun test file), and the
  latest renders are verified, approved and captured from this source. Its
  requirements become "covered by harness verification", never met: the
  final audit still judges each. Anything short of that dispatches the task
  as before, with the reason in `verification_dispatched`. The orchestrator
  sees the same facts each round, and review seats judging a source the
  harness checked get the receipts in place of the command list.
- **A reviewer's blocking design judgement gets one fix** (overnight r01,
  20261011T052348Z-1a2969ca: the reviewer rightly blocked a hint that
  squeezed the search field to 46 px, and the run ended FindingsOpen after 7
  of 20 calls with no repair). With harness capture, BLOCKING lines from the
  cross-vendor design reviewer get one design-fix call, then the gate, a
  harness recapture and the same reviewer again. One call, no loop; a second
  BLOCKING verdict stays open. Delivery, capture and no-verdict blockers
  written by the harness get no fix, and lead-captured ("self") evidence is
  unchanged.
- **A test repair is named, not inferred** (run 20261010T225947Z-fec6da52:
  the goal authorized repairing a broken generated browser test, the
  repaired suite passed, and the original-test check restored the broken
  run-start version and timed out). `--repair-test PATH` / the GUI's "test
  files this run may repair" names exact existing test files; a glob, folder,
  helper or forbidden path is refused before any call. Only those files stand
  as repaired in the original-test copy; every other original is still
  restored. The repair must keep every case its run-start version declared,
  each executed and unskipped in a passed check, with names no other test file
  shares; otherwise the check fails. The broken original is kept as an
  artifact. Kept names and executed cases do not prove the assertions were
  not weakened (a same-name `assert True` satisfies both), so
  `test_repairs[].assertions_verified` is never set by the engine, the report
  says so, and `tools/truth_check.py` reports the run unverified until an
  independent review of the repaired file against the kept original.
- **A check command owns its process group** (`integration._run_owned`). It
  runs in a new session; on a timeout, or when it exits leaving its own
  processes behind, only that group's members are stopped, pid by pid,
  SIGTERM then SIGKILL to a pid whose group and start time are re-read
  just before the signal. The group is scanned again until it is empty or
  the bounded grace ends, so a member started during cleanup is stopped too;
  cleanup is reported complete only when a final read shows the group empty.
  Never a whole-group signal; a process that detached into its own session is
  not touched. Output printed before a timeout is kept on the receipt. A
  timeout is still an error, never an attributable product failure.
- **The Monitor follows the project you opened.** A folder typed into the
  Monitor wins, then the project opened on the Project tab, and only then the
  project the GUI was started watching (`QUADRATUS_MONITOR_PROJECT`), which no
  longer pre-fills the field.
- **A role packet is sized before the calls that carry it** (run
  20261010T135407Z-f3d33e9d). The operator's forbidden paths alone put every
  packet over the 12,000-byte cap, and the run found out only when building
  t1's lead packet, after planning and requirements review were spent.
  `project_run._packet_floor` now builds the lead and reviewer packets from
  the operator scope before any model call, and `_prepare_dispatch` builds
  every packet a task will carry before its lead is called. Same builder,
  same cap, nothing truncated.
- **A run's success claim is judged outside the engine** (`tools/truth_check.py`,
  2026-10-10). It reads only the run record, imports nothing from
  `quadratus`, calls no model, and answers verified, unverified or not met;
  missing evidence is unverified, never success. Offline canaries in
  `tests/lifecycle/test_truth_canaries.py` drive the real run with scripted
  replies through the known bad cases. Review priority and batching rules are
  in `docs/DEVELOPMENT_PROCESS.md` ("Spending less per fix").
- **The run monitor is read-only, and that is the whole design**
  (`monitor.py`, the GUI's Monitor tab, `quadratus --monitor`). The engine
  already writes everything a human wants to know under
  `.quadratus/runs/<id>`; nothing rendered it live, so the operator was
  asking two models whether a run was even running. The monitor reads the
  series lock's pid, the tail of `invocations.jsonl`, `budget.json` or
  `usage.jsonl`, and `result.json`, and it never writes, never takes a lock
  (the project's `run.lock` is an flock with no pid, and probing it could
  make a starting run believe the project is busy), never raises on bad
  data (every field is `unknown` with a reason) and bounds every read: only
  regular files are opened, through a non-blocking descriptor read to a byte
  bound, so a FIFO or device cannot stall a refresh, and a count outside
  any range a run produces reads as unknown (Codex review of 736e94b, Z3).
  It finds runs exactly where `run_project` writes them (`~` expanded, a
  relative state dir joined to the project once, Z2), reports a
  listed-task run as listed tasks complete with the goal not judged rather
  than as a completed goal (Z1), and the phone page says which finished
  call its Task and Seat describe (Z4). It
  also never calls a model: a status that depends on a seat answering is
  the problem it exists to remove. The stage is inferred from the latest
  role and the newest artifact kind, and says so when it cannot tell.
  `--monitor --serve` renders the same dict as a self-reloading page on
  loopback only (`monitor_server.py`, no bind flag); Tailscale Serve is the
  way onto a phone, and the Gradio GUI is never exposed that way because
  its Project tab can run commands. The page has no controls at all, which
  is what makes it the thing that may be reached from another device.

- **A reviewer judges evidence the harness has already shown on the exact
  source** (workflow repair, 2026-10-24, after the ticket run stopped on
  open findings). `_observe_checks` already ran the task's gate before
  every review and recheck, but only `self.checks` could discharge a proof
  row, so reviewers read "execution missing" on cases the harness had just
  run and passed, and spent rounds on it. `_obligation_receipt` now reads
  the observations too (receipt ids `observed-N`, same identity rules,
  only a passing run discharges, a failed one is named in the rows);
  `_observe_checks` skips a source a task check or merge gate already ran
  under the same gate; the rows are refreshed before any judgment binding
  is taken; the post-merge recheck is handed the child's rows under the
  merge gate's receipt. A parallel child runs no checks in its copy
  (snapshots carry no dependency tree, the r08 lesson), so its rows say
  so and settle after the merge. The task's own gate still runs after the
  fix cycles: the observation is evidence, never the mandatory check.
- **Two typed findings leave the editor out of it** (same batch).
  `BLOCKING: SCENARIO P<n> - <contradiction> | proposal: <operation|state|
  expect>: <text>` goes to the seated planner once per task
  (`_correct_scenarios`): it may change that one field, KEEP the text, or
  say the REQUIREMENT itself would have to change, which raises the
  operator's question (`OperatorInputNeeded` with no channel) and changes
  nothing. The goal, requirement texts, scope, the scenario's requirements,
  location, evidence kind and id never move; the row keeps its prior text,
  execution and judgment as `scenario_lineage` and starts again; the
  reviewer who raised it re-checks the corrected scenario. `BLOCKING:
  EVIDENCE P<n> - <what is not shown>` goes to the harness (an observation
  if none exists on this source) and back to the same reviewer; in a child
  it is held for the post-merge recheck. A round whose every blocking line
  was routed makes no editing call and the planner stage is costed in the
  fix-cycle admission. A kept, unanswered or stray SCENARIO finding, and
  every UNRESOLVED verdict, takes the ordinary editor round: the plan
  channel cannot make a finding disappear, and nothing closes one but its
  reviewer. Records: `scenario_corrections`, `evidence_routes`.
- **A requirement no task took on is said at the stop.** The ledger and
  the planner's obligation view already held it ("taken on by no task
  yet"; DONE refused until covered); `report.md` now names it when a run
  stops incomplete, because the ticket run's report did not say R23 had
  never been scheduled.

- **A seat names a family; the installed client names the release**
  (2026-10-24). Roster keys are families: `claude:opus`, `openai:sol`,
  `openai:astra`, `openai:luna`, `openai:terra`, `grok:default`. Legacy
  version keys (`openai:gpt-5.6-sol`) still resolve through
  `registry.resolve`/`canonical_key` so histories and operator inputs keep
  reading; they are never wire strings. `latest.resolve_model` answers per
  vendor: Claude's family alias goes on the wire as written (the CLI
  resolves it; the probe's alias cache is a record, never a source, so a
  dated id probed months ago cannot pin the seat); Codex takes the family's
  latest visible, client-compatible slug from the installed client's own
  `~/.codex/models_cache.json` (`catalog.read_codex_catalog`: schema,
  freshness within `QUADRATUS_CATALOG_MAX_AGE_HOURS`, default 168, and the
  writing client compared to the installed one), ordered numerically
  (6.10 above 6.9, a 7 above any 6.x); Grok sends no flag and the
  `grok models` default is recorded beside the call. A missing, stale,
  malformed or mismatched catalogue makes the family *unavailable*, with
  the reason on the status line and in `result.json` `models`; nothing
  older is selected from memory and no slug is invented (the installed
  Terra is 5.6; no 6 exists). `QUADRATUS_ALIAS_<VENDOR>_<FAMILY>` is the one
  visible exception. A job resolves each family once (`Fleet._resolution`),
  a parallel child shares the parent's answers, a resumed episode keeps
  the job's (`model_resolution` rides the checkpoint; the current
  catalogue's drift is recorded, never followed). The API backend still
  needs dated ids: the configured OpenAI model stands for Sol, every other
  family an explicit `QUADRATUS_API_MODEL_*`.
- **Effort is decided from facts the harness holds, on every call**
  (`effort.select`, policy `effort/1`). Inputs: role, the task's declared
  complexity and kind, work class, security, the fix cycle, the worst
  impact a recheck is about. Planner `high` and held for the session
  (the cache survives); `decide` and declarations `low`; implementation
  `medium`, `low` for rote, `high` for complex or security-grade work
  (security, concurrency, perf, architect, iac); every judging seat floors
  at `medium`, `high` for complex, security-grade or review-kind work, a
  repeated or immediate-risk recheck, the verifier and the final audit; a
  second fix cycle raises the editor one step; `xhigh`/`max` only for
  complex, risk-bearing work still open at cycle 2 on a ladder that has
  the level, and `ultra` never (the catalogue calls it automatic task
  delegation). Bounded seats keep their roster effort. The session passes
  the inputs on the invocation context (`effort_inputs`); the Fleet decides
  against the resolved model's ladder (codex per slug, claude
  low/medium/high/max, grok low/medium/high/xhigh), validates, and refuses
  a level the ladder lacks (`UnsupportedEffort`) instead of lowering a
  floor; only an elevated level is bounded, to high, with the reason. Every
  ledger row carries `effort`, `effort_reasons`, `effort_policy` and
  `resolution_source`; no call inherits a CLI default (the installed Sol
  defaults to `low` where its API documents `medium`). The budget never
  reads effort.
- **Root's seven dispatch corrections on bcd2b52** (2026-10-25). An
  unknown effort ladder is not permission: a catalogue entry that lists no
  ladder, or only levels this engine cannot send (`ultra`, names it does
  not know), makes the family *unavailable* before any call, and a pinned
  slug outside the catalogue needs its ladder declared in
  `QUADRATUS_EFFORT_LADDER_<VENDOR>_<FAMILY>` (recorded as the operator's
  assertion; never overriding a catalogued ladder). The job's resolution
  state is one shared object (`runtime._JobResolution`) across the parent
  and every forked child, keyed by family, under one lock, so a family
  first resolved in a worker is the parent's resolution and its record;
  legacy spellings share it and the ledger keeps the requested spelling.
  Exhaustion is marked and read by family (`canonical_key`), so an alias
  cannot retry a spent seat. Risk outranks the size label: a `security
  rote` task runs its lead at high. The sealed checkpoint carries the
  terminal `model_resolution` (`checkpoint.seal(state_patch=...)`) and
  every quiescent point refreshes it first; a resumed episode freezes the
  job's resolutions before any seat or availability decision. The bare
  factory (`providers.build_provider`) resolves a configured family through
  `latest.resolve_model` after the provider is built (so the installed
  client's version is known) and reports an unresolvable family as
  unavailable instead of sending its name as a model id; a Fleet passes
  `resolve_family=False`, because it binds each seat's family per call and
  a vendor's transport must not depend on the settings' default family
  (root's isolation control on aab4fe7: a catalogue without Sol took Luna
  and Astra down with it). A view that sets its own effort (the retained closeout view,
  a probe at low) records the effort it sends, with the seat's policy
  decision named beside it. The effort inputs a recheck sets are cleared
  on every exit of the call, and a task entry resets them.
- **One finding identity, from the review to the planner to the close**
  (Davis, 2026-10-28; controller-continuation-01). t4's second revision hit
  the turn cap, `_close_turn_limited` returned before the fix loop's block
  that writes open findings into `unresolved_reviews`, and eight of Opus's
  open ids were in the registry but in no held entry: the post-merge
  recheck asked about one DEFERRED finding, the planner read the eight
  under "Review findings open by id", named them in RESOLVES, and
  `_resolves_problem` sent it back as naming findings that do not exist
  (it read only the audit ledger `self.findings`); the replacement planning
  call cost the room the last task needed. Now: `_hold_open_finding` is the
  one block (the legacy text or the audit-carried note, plus the held
  entry) and `_hold_task_findings` runs it at every capped and failed close
  with `hold` on the record saying why. RESOLVES accepts `RFT<task>-<n>`
  ids beside `F<n>`; a review finding settles only in
  `_settle_review_findings`, after a clean close with its checks passing,
  by one recheck call per original reviewer (`_recheck_open_findings`, the
  post-merge recheck generalised); a continuation inherits its chain's open
  findings (`inherited_by`, the lead's handoff block names them) and the
  same recheck follows its clean close without naming them. The resolving
  task's lead, another reviewer and the harness close nothing; a closed
  finding makes no requirement met. An explicit RESOLVES kept open is the
  FindingsUnresolved stop as for an audit finding; an inherited finding
  kept open stays held debt. A reviewer that is unavailable cannot be
  named. The rechecks are costed on the route (`resolution recheck by
  <peer>`), recorded in `review_resolutions` (result.json, checkpoint).
- **Deferred child evidence is declared, never discovered** (same batch).
  A parallel child measures no execution, so its reviewer was told every
  unexecuted row was an EVIDENCE gap and filed one finding per row (t4:
  seven findings, seven holds) for a fact the harness held. The child marks
  its executable rows `deferred` to the merge gate (`_mark_deferred_rows`),
  the proof rows say so, the reviewer is told not to file their
  non-execution, and the deferral is declared per (task, reviewer)
  (`deferred_evidence`) when the reviewer is handed the rows. The parent
  clears the mark on its re-based rows, the merge gate runs them, and the
  same reviewer's post-merge recheck carries them (`_deferred_rows_block`):
  `P<n>: NOT SHOWN` registers that reviewer's EVIDENCE finding held open;
  `SHOWN` is a recorded reading that makes nothing met (the audit judges
  sufficiency) and is refused on a row that did not run; no line is no
  reading. A merge gate that does not pass leaves the deferral "not
  rechecked" and the rows missing, listed for the planner. The recheck is
  the one call per reviewer the EVIDENCE findings already bought; the
  saving is review lines and hold records.
- **A correction line carries the operation first and its explanation
  after `| because:`, and its size bound is the declaration's** (same
  batch; root on 6c1b922). The live correction was refused at 869
  characters with the explanation inside the text, against a 400 bound
  the prompt never stated; the first repair stated the 400 and root's
  probe showed a 683-character state `read_proofs` accepts refused only
  for being over it. There is no character count now: `_split_explanation`
  takes the text before the segment, the explanation is kept on the record
  and never counted, the shape rules stay (empty, multi-line, equal to
  the declared text, ambiguous lines), and the one size bound a corrected
  field meets is the one a declared field meets, the task's role packet
  limit, checked on a trial scope that is discarded
  (`_correction_packet_problem`). The prompt asks for a field as concise
  as a declared one and says what the limit is. KEEP and REQUIREMENT take
  their reason the same way. Correctness and authority are stated as
  mandatory: the text must hold against the requirement texts, which only
  a REQUIREMENT line can question, and the finding's reviewer re-checks the
  corrected scenario.
- **FETCH serves an artifact whole, in exact windows** (root on 6c1b922:
  with the completed work rendered as an index, FETCH is the one path to
  an original, and a ~25,000-character draft came back as its first
  12,000 and "[...truncated]"). `FETCH: <id>` returns characters 0 to
  `_FETCH_CHAR_CAP` (40,000); a window that ends before the end says how
  many characters remain and the exact next request, `FETCH: <id> FROM
  <n>`; each window is one fetch against `max_fetches`; nothing inside a
  window is elided; an offset past the end says so. The window bounds one
  prompt's growth per request, not what can be read.
- **The sealed terminal state is one snapshot of the end** (root on
  6c1b922: a last-slot batch's sealed checkpoint kept the pre-batch
  `review_findings=[]` and `unresolved_reviews=[]` beside the end's capped
  lists, so the terminal record named no reviewer's debt while result.json
  did). `_terminal_state_patch` now carries `checkpoint.carried_snapshot`,
  every carried key, the task outcomes, the history and the ledger as the
  session ends, encoded per key with any key that cannot be encoded named
  under `terminal_patch_omitted`; sealed runs stay non-resumable and older
  sealed records are left as they were.
- **A raised or unmerged child's open findings are held before its failed
  record is written** (`_record_unmerged_failure` calls
  `_hold_task_findings` with the child's outcome; root on 6c1b922: the
  raised child's RFT2-1 had no hold and its failed handoff carried no
  review debt). The scope conflict goes through the same call. Nothing is
  merged, retried, or turned into a continuable cap; the findings stay
  their reviewer's and a continuation inherits them by task id.
- **The completed work reaches the planner and the lead as an index, and
  every prompt is measured** (same batch; root's measurement on the last
  saved run: the final ledger rendered 100,752 characters with previews
  against 14,251 as the index, inside a 149,250-character planning prompt;
  the last lead prompt was 130,574 characters with 23 earlier artifacts
  shown in full). `with_previews=False` at the planner, the plan gate and
  the lead draft, which keep the goal, rules, rulings, requirements, every
  summary, reasoning, dead end and harness record, every artifact id, and
  FETCH for any of them; the handoff and source pack of a continued task,
  the map, the obligations and the open debt blocks are unchanged. Review,
  revision, recheck and audit prompts carried no ledger previews and are
  untouched. `_measure_context` records each such prompt's characters by
  block, with the completed block both as sent and as the preview render
  would have been (`prompt_measurements`, result.json): a rendering
  comparison, never a token claim; tokens are the CLI's reported usage in
  the delegation ledger. A route's admission now counts the planning call
  that follows it (`_planning_stages`: one reserve call at the larger of
  the reserve and the last planning call's reported size; none in the
  final slot, for a terminal route, an explicit list or a child), because
  the two post-audit planning calls reported 451,817 and 358,436 tokens
  against a 200,000 reserve.
- **An ordinary review covers the whole error boundary and inherited
  failures** (`_ERROR_BOUNDARY_LENS`, same batch): malformed, missing,
  out-of-range and oversized input, edge values, missing resources,
  failures of what the code calls, where each is caught and what the caller
  sees, construction and parsing outside the handler, and a failure the
  task inherits from code its own path reaches. Generic by design: it names
  classes, never a candidate's defect.
- **A NOT RUN report binds to an identity the harness can execute, or
  to nothing** (evidence lifecycle batch, Davis, 2026-10-29;
  `Session._bind_not_run`). finding-lifecycle-02: t7's lead reported
  scenarios (`P5`) and cases (`ApiDependencies.test_handler_...`) it could
  not run in its sandbox, the harness's own gate then ran the suite whole
  five times, and five requirements stayed NOT MET on runner-less rows no
  record could discharge. Each item is now bound at the report: a declared
  scenario (`P<n>`, every one the item names, through the cases its
  acceptance row binds; PROVES is read before NOT RUN so the same reply
  binds both, and a later rebinding re-binds the item and resets its row),
  an explicit `file::[Class::]test` id checked against its file, or a
  `Class.test`/bare `test_*` name defined in exactly one test file (read
  with `ast`), reading the whole item in every grammar at once: a mixed
  claim (`P1 and Other.test_absent`, `TestX.test_complete and P2`) binds
  the union or nothing, a parametrized id keeps its whole bracketed
  parameter (spaces included; `_CASE_CITATION` and `_CASE_NAME` read an
  id to a boundary, so `test_x[not a real parameter]` is that instance,
  never its base, and `test_x.extra` or a stray bracket is "a case id the
  harness cannot read whole", held as written and never a path; root's
  controls on d88d8c0; an id ends at whitespace, a separator, a closing
  quote or backtick, or sentence punctuation, so a quoted id followed by
  prose still cites, and a NOT RUN reason is split at the first ` - `
  outside the brackets, so `test_x[a - b]` keeps its parameter; root's
  addendum on 3b99f5f) and is discharged only by a run showing that
  instance, and the complete item is the identity (root's control on bd0164f: a 300-character display
  prefix of `P1 <detail> and P2` bound P1 alone; prompts bound the display
  with `_display_item`, the record, the dedupe key, the merge, the
  checkpoint and the checker read it whole). Ambiguous, undefined,
  wrong-class, suffix, partial or prose items bind nothing and are held
  as written, with the problem on the record. An identity claim, valid or
  not, never falls back to the file-level route (root's control: an
  undefined case cleared when its file ran other cases). The discharge is
  one decision with its evidence (`_discharge_record`: the check, the
  command receipt within it and the case identities that run was verified
  to have executed for the record), and the typed receipt
  (`_obligation_receipt`) carries exactly that record: root's six-case
  diagnosis on 01cfaf8 found the receipt re-reading the check under a
  whole-run rule, which dropped bare Node names and a named-case recovery
  beside an unrelated historical skip, so the prompts said discharged
  while the typed row read invalid. A skip fact's decision
  (`_discharge_named`: same command, each name executed and unskipped,
  held by the same file or runner-qualified) now keeps its run and names
  on the entry (`discharged_run`, `discharged_cases`); never the claim
  itself, and a marker from an earlier checkpoint that carries no names
  is re-verified from the recorded check through the same rule
  (`_named_skip_run`) or is no discharge. The checker's skip route
  (`_skipped_cleared`) reads the same receipt id's own executed and
  skipped counters (integers, each name at least as often as the fact
  names it, skipped zero; never a set of keys), demands a complete case
  record first (`_complete_cases`: `executed` and `skipped` mappings
  whose counters are non-negative ints, the skipped counters accounting
  for exactly the skipped total the runner itself reported, read from the
  parsed report's count else the runner's summary line, unknown never
  zero; root's control on 5ce8ebc: the real Node recovery with its
  `cases.skipped` emptied under a raw `skipped 1` cleared, when the record
  could no longer show the named case was not among the counted skips),
  and holds a fact with a typed row in the record to that row's current
  identity read
  independently (the final source recorded on the check, the row's gate
  and environment, its requirement revisions); only a record with no
  typed row and no identity keys on the entry takes the separate
  historical rule (same command's counters, final source where recorded).
  Root's review of e1d9c01: zero executions, a duplicate debt with one
  execution, a check without a source and another gate all cleared. A bound item's typed row
  gets the gate as runner and the cases as identity (kept even
  runner-less, so a parallel child's row re-bases to the parent's gate);
  it is discharged only by a later complete required run on the current
  source, under the entry's own gate and environment, whose own case
  record shows every bound case passed. `_standing_not_run` and
  `_discharging_check` now apply that identity too (root's control: a
  passing run on another source, gate or environment left the typed row
  unexecuted while the prompts showed no debt), so prompt and record
  agree. The lead is told the two forms.
- **Sufficiency is an explicit scoped judgment, never a verdict on a
  requirement** (`_judge_obligations`, `sufficiency_source`; root's
  correction on finding-lifecycle-02 and review of bd0164f). The old
  implication read requirement flags over every requirement a shared row
  binds: `_read_audit`'s lowering of R9 for a NOT RUN item cascaded into
  R3, R4 and R11 the auditor had passed, and on first judgment R1 MET plus
  R5 NOT MET labelled a shared row insufficient while all MET labelled an
  unjudged row sufficient. There is no implication now: an executed test
  row is judged only by its own `<O-id>: SUFFICIENT|INSUFFICIENT` line in
  the reply the seat was already asked for (no call added), the audit and
  reassessment prompts and the obligations block say so, a fresh or stale
  row left without one stays unjudged and lowers its requirement through
  the acceptance predicate, a judgment about exactly this evidence
  (`sufficiency_current`) stands until a seat speaks again, and conflicts
  stay unjudged. The fixture contract follows and is as strict: a
  scripted auditor in `tests/lifecycle` authors its own row lines for the
  named evidence it means to approve (`replies.judged`, keyed by the
  scenario text the obligations block renders), an overridden handler's
  reply is carried exactly as written, and only the default `Script`
  auditor judges, and only the default fixture's own add scenario
  (`default_audit`). No test-side transform adds lines from verdicts
  (root's blocker on the first draft of this correction: a blanket
  adapter would have moved the removed inference into the test
  transport and masked missing-judgment negatives). Execution facts,
  explicit sufficiency and requirement verdicts are three records; none
  is derived from another.
- **A requirement that names a command is shown by the harness's exact
  receipt** (`_command_requirement`, `_command_receipt_citation`,
  `citation_receipts`). R21 ("run the suite before declaring completion")
  was lowered as uncited while the gate had run that suite five times. A
  MET with no file or test citation now stands when a required receipt on
  the current source, gate and environment, from the project root, ran the
  named command (interpreter by public name, report token and
  `-p no:cacheprovider` removed, everything else exact; `_command_key`)
  whole and successfully: cases executed, a known zero skipped, none
  failed or errored, no selection, and the audit record names the
  receipt. A check whose source, gate or environment is not recorded is
  an unknown identity and is refused (root's control on bd0164f), as is a
  failed, optional, filtered, other-source, other-gate,
  other-environment, other-directory, zero-case, skipping or failing
  receipt, each with its reason. The citation names the command in the
  requirement's own public spelling and never the argv that ran (a
  private interpreter path; root's addendum), which stays in the check
  record. The receipt is a citation of that successful complete run and
  nothing more: the requirement's scenarios still have to execute and be
  judged. The reassessment uses the same resolver.
- **The finish routes each unmet requirement and withholds a challenge
  that cannot clear it** (`_evidence_route`, `finish_routing`, carried and
  exported). finding-lifecycle-02 spent three challenges, about 594k
  tokens, on requirements held by NOT RUN rows. A standing NOT RUN item
  (execution debt), an obligation with no runner (prerequisite), a failed
  required execution, or an open review finding touching the requirement
  is not offered: a read-only proof cannot discharge any of them, and the
  prompt and the DONE refusal say where each goes instead (a task whose
  checks run what the item binds, a repair task, the finding's own
  reviewer). A finding on the behaviour, including one whose bound case is
  missing or stale, stays challengeable, because the challenge's own proof
  may be that case executed on this source and a false positive is a
  legitimate dispute.
- **Framing is part of the error boundary** (`_ERROR_BOUNDARY_LENS`). The
  one product defect finding-lifecycle-02 left (a short body parsed as a
  complete prefix and acted on) is not named in the lens; the lens says a
  body shorter or longer than its declared length, an early EOF, a partial
  read and a record cut mid-field are input, that code must not act on
  what it did not fully receive, and that a rejected input must leave no
  change behind, checked by reading the state after the refusal.
- **The independent checker validates a bound NOT RUN entry itself**
  (`tools/truth_check.py::_bound_not_run_cleared`). It reads the saved
  binding's provenance (a declared scenario of the same task whose
  acceptance row binds exactly these cases, or an item that itself names
  each bound case), then a later check on the final source under the
  entry's own gate and environment, required, passed, unfiltered, zero
  skips, from the project root, whose own report shows every bound case
  passed, in both directions (every bound case is spelled by the item or
  its scenarios; every case the item spells is bound). It never reads an
  engine discharge mark, and an entry that carries a `binding` key is
  decided by the binding alone, never by the older file-level route
  (root's control on bd0164f: a rejected explicit-case record cleared on
  its file); only a record without the key takes that route. Forged,
  stale, mismatched, partial, parametrized-instance, malformed or
  historical entries stay unverified. Since d88d8c0 the item's whole text
  decides the route (`_claim`): an identity claim (a scenario, an id, a
  class or bare case name, anything shaped like a case id) is decided by
  the saved binding alone, whether the key is present, empty, missing or
  forged as `paths`; a path-only item takes `_paths_cleared`, which keeps
  every identity check (final source, the entry's gate and environment
  where recorded, required, unfiltered, zero skips, project root) and
  needs the command to name the file whole and the report to show a case
  of it passed; a historical record without identity keys is held to the
  final source and the same passed-case reading. The checker reads both
  record shapes (root's control on 3b99f5f): a gate suite's aggregate
  command is the literal `gate suite` with no aggregate report and the
  evidence in one receipt per command, so `_rows` normalises a check into
  per-command rows (receipt status, required flag, command, cwd,
  selection, report, count) or one row from a single-command gate, and
  `_row_ok` demands of each row a whole successful run (passed, required,
  no selection or filter, project root, a report in state `parsed` with
  known counts, zero skipped, failed and errored and at least one passed
  case; a report in any other state carries nothing, whatever fields it
  still holds); nothing is fabricated across the shapes and `gate suite`
  is never read as argv. A path-only record written before bindings
  existed (no `binding` key) keeps the older executed-case reading, held
  to the final source wherever the check records one. A parsed
  report that lost its skipped count reads as unknown
  (`integration.skipped_count`), never zero and never a crash, so a
  citation refuses it.

## How development is coordinated

`docs/DEVELOPMENT_PROCESS.md` carries the short rules adopted on
2026-10-01 from the plan published on #35; `docs/dev-record.json` is the one
shared development record (candidate SHA, gate, each task's scope, author,
independent reviewer, delivery and review SHAs, blockers), edited only
through `tools/dev_record.py` (`claim`, `deliver`, `review`, `receipt`,
`ready`, `render`, `candidate`, `note`, `extend`). Claude coordinates and integrates; Codex is the root
reviewer and owns local execution and live runs. The acceptance manifest and
the workflow scorecard keep their own evidence.

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
