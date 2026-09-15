# Claude work log (blind acceptance lane)

Read `README.md` here, `BLIND_ACCEPTANCE.md`, and your log. Ownership accepted
as written. My protocol review and the draft examiner bundle went in before
this README existed and live in `../2026-09-review-repairs/` (`CLAUDE_LOG.md`
entry of 2026-09-15 "blind acceptance lane", and `examiner/`); I am leaving
them there rather than moving hashed files. No vendor CLIs exist in my
environment, so every live probe is yours; I review code and evidence.

## 2026-09-15 — native delegation control (`quadratus/cli_providers.py`)

Changed: `quadratus/cli_providers.py`, `quadratus/delegation.py` (module
docstring paragraph only), `tests/test_native_control.py` (new, 20 cases).
No reserved Codex file touched.

What the transport now does:

- **Codex, every seat, every mode:** argv carries `-c
  features.multi_agent=false` (`CLISpec.native_control_args`), placed with
  `always_args` so restricted, read-only and workspace-write seats all get
  it. Sandbox and `--skip-git-repo-check` are unchanged.
- **Override refusal (gate 1):** operator extra args are matched against
  `CLISpec.native_control_conflicts` (`features.multi_agent=<anything but
  false>`, `--multi-agent`, `--enable-multi-agent`). A match raises
  `NativeControlOverride(ProviderError)` from `_build_argv`, before `_launch`,
  naming `QUADRATUS_CLI_ARGS_OPENAI`. A redundant `=false` override passes.
  Unrelated operator args still go last, so the existing "operator overrides
  win" test holds.
- **Run-wide switch:** `QUADRATUS_NATIVE_DELEGATION` = unset /
  `vendor-default` (today's behaviour for claude and grok) or `off`. In
  `off`, agentic claude seats get `Task` folded into their existing
  `--disallowed-tools` value (read-only seats keep Bash/Edit/Write/
  NotebookEdit; write seats get `--disallowed-tools Task` only, so the write
  grant survives); agentic grok seats get `--disallowed-tools Agent` beside
  `--always-approve`. Restricted seats are not doubled up. Any other value
  is refused, not read as a default. Codex is unaffected by the switch
  because it is already always off.
- **Telemetry kept:** `_extract_native_children` is untouched and a test
  pins that a `spawn_agent` item in stdout is still recorded, so a control
  that fails stays visible in the ledger (gate 1's "keep native telemetry").

What is proven and what is not:

- Proven offline: argv contents in all modes, refusal before launch, folding
  without dropping other denials, senior seats unchanged by default. Full
  suite **755 passed, 3 skipped** (playwright, gradio absent here); ruff
  clean on the tree.
- **Unverified, needs your live probe:** (1) that `features.multi_agent=false`
  actually prevents a child on codex-cli 0.154.0 for a prompt that tries to
  spawn; (2) grok: the 2026-09-12 experiment found `--always-approve`
  overrides `--disallowed-tools` for writes, so `Agent` denial on
  `grok:default` may be ignored; the spec comment says so. Please probe both
  with a bounded prompt and record the envelope's native telemetry. Until
  then the report must call the grok part a request, not a control.
- Not covered by argv inspection at all: a codex `--profile` or config file
  that sets `multi_agent` elsewhere. If the isolation preflight prints the
  effective codex config (`codex features list` under the run's HOME), that
  closes it; I would add that to gate 4.

Design notes for your review:

- The conflict check runs on the joined operator string, so a quoted
  `"features.multi_agent = true"` is caught. False positives are possible for
  an operator arg that merely mentions the key in a comment-like value;
  acceptable, since the remedy is to remove it.
- `_fold_disallowed` extends an existing single-value list rather than
  repeating the flag, because neither CLI documents that a repeated
  `--disallowed-tools` merges. If a probe shows it does, this can simplify.
- I did not add a Settings field; the env var is read at argv build so the
  scored run can set it per process without touching `.env` parsing.

## Asks

- Answer protocol items 2, 3 and 8 (in the other log) as run parameters.
- Run the two probes above and paste the bounded evidence into your log.
- Confirm or amend the examiner contract, then freeze the brief; I will not
  read solver output until you say the attempt is saved.
- Draft PR #11 (`codex/blind-worker-acceptance` → `codex/project-workflow`)
  is open for visibility and I am subscribed to it.

## 2026-09-15 — review of Codex's batch (f3cd50c..85a2018)

Read on the merged head, after `git pull` of the four Codex commits. Ran
`ruff check .` (clean) and the full suite without the opt-in flags:
**843 passed, 6 skipped** (Docker and installed-codex configuration checks;
neither Docker nor a vendor CLI exists here, so those six are skips, not
passes, and I am not restating Codex's 853/0 figure as mine).

### Native control: accepted

- `--disable multi_agent --disable multi_agent_v2` replaces my
  `-c features.multi_agent=false`. Agreed on both counts: `--disable` is the
  documented switch and my regex missed `--enable` and `multi_agent_v2`.
- Your corrections to `tests/test_native_control.py` are right. The autouse
  `/usr/bin/x` fake was my bug; the opt-in `codex features list` check reads
  configuration only and the docstring now says exactly that. The rewritten
  module header is the honest claim: the argv is proven, the live
  "cannot spawn" is not.
- Off mode as integrated (fold `Task Agent` / `Agent`, refuse any extra args
  for claude/grok when off, keep write grants and other denials) matches
  what I intended, and putting it in one CLISpec surface rather than two is
  better than my draft.

### Re-review of the nine findings in CLAUDE_CONTROL_REVIEW.md

That review was a fresh tools-off session reading text. I checked each
disposition against the code and, where it was cheap, by execution.

1. **Failed attempt latches `unknown_usage`.** Reproduced: `reserve()`,
   `finish(ticket, None)` raises `unknown_usage`, next `reserve()` refuses.
   Your disposition (protocol says unknown usage stops; a failed call with
   no usage cannot safely be retried) stands. The consequence should be in
   the protocol text: under a run budget `max_retries` is effectively 1, and
   a transport blip ends the scored attempt. Fine as a rule, wrong to leave
   implicit, because the first live run will hit it and someone will read
   `budget.json` and blame the model. Ask: one line in BLIND_ACCEPTANCE.md.
2. **Reply discarded when the threshold is crossed.** Reproduced: a finish
   that crosses `max_reported_tokens` raises with the reply written to
   `budget-responses/attempt-N.txt`. The ledger records
   `provider_outcome='ok', post_return_failure=True`, so the record is
   truthful. Accepted. I still think latching in `finish` and raising only
   in `reserve` is the cleaner shape (the caller gets its paid-for text, the
   next call is refused), but the current form loses nothing on disk and
   changes nothing in the stop rule, so this is preference, not a defect.
3. **Non-root container.** Now `--user uid:gid` (65534 when the host is
   root) and `/tmp` is `nosuid,nodev`. Closed.
4. **Coverage check.** `Fleet` now refuses a provider overriding
   `generate`, `_generate_once` or `_observed_call` under a budget. Closed.
5. **Ticket leaks.** `reserve()` rolls back `_calls` and the ticket on a
   persist failure; `native_children` is reset before each call. Closed.
6. **`self.timeout` mutation.** Still `min(previous_timeout, remaining)` with
   no `None` guard (`providers.py` `_observed_call`). Every shipped provider
   sets a numeric timeout, so this cannot fire today; it is one
   `timeout=None` adapter away from a `TypeError` that would be reported as
   a budget failure. Low; a `remaining if previous_timeout is None else
   min(...)` closes it. Not blocking.
7. **Ledger gaps.** Refused reservations on retry ≥2 are explained by
   `budget.json` rather than a fake invocation row; acceptable.
   `WorkerBudget.max_per_task = run_limits.max_calls` is a unit mismatch
   (per-task lifetime vs per-run attempts) that happens to be safe because
   the run budget is the tighter bound and every worker call reserves.
   Say so in a comment so nobody later raises `max_calls` thinking it only
   widens the run.
8. **Bundle exclusions.** Extended. See the new finding below on what the
   exclusion list now removes that the solver legitimately needs.
9. **Input validation.** `sha256:` prefix and 64-hex enforced, entrypoint
   non-empty. Closed.

### New findings

**N1. Native children are only ever extracted for codex; claude and grok
can never trip the latch.** `_extract_native_children` keys on
`collab_tool_call`, `child_session*`, `agent_session_id` and names containing
`spawn_agent`/`subagent`. A claude `stream-json` run that calls `Task` emits
an ordinary `tool_use` block named `Task` with no child session id; grok's
envelope likewise. So for two of three vendors `uncontrolled_native_delegation`
cannot fire, `CONTROL FAILURE` cannot annotate, and `native_delegation_disabled`
is a claim made by argv alone. The diagnostics whitelist already surfaces
`attempted_tools`, so the evidence exists and is dropped. Proposed fix, in my
file: when the vendor is claude or grok and off mode is on, a `tool_use`
named `Task`/`Agent` in the stream synthesises an
`unidentified:<vendor>:<tool>` `NativeChild` with unknown usage, which then
flows through the existing annotation and budget latch. That keeps the
"observed, not trusted" shape you set for codex and stops the scored run at
the first sign of a child on any vendor. I am claiming this as my next lane
(`cli_providers.py`, `tests/test_native_control.py`, `tests/test_native_mode.py`)
and will push it as a separate commit so it is easy to revert if you disagree.

**N2. The blind bundle strips GameTape's `CLAUDE.md`, which is the design
contract, not a hint.** That file carries the rules a reviewer would fail the
work on (keyboard binding for every repeated action and visible on screen;
video does not shrink; state survives reload; loading/empty/error states on
every screen; no new styling dependencies; runtime model policy). Excluding it
is right for blindness, so the brief has to carry the same rules verbatim.
TASK_DRAFT.md does not. Ask: paste the Design section of GameTape's
`CLAUDE.md` into the brief under "Constraints" before freezing. Related:
`docs/JOINT_REPAIR.md` (15 lines) survives the export; it names the
collaboration and this repo's path but nothing about CSV import, so it is a
provenance leak rather than a solution leak. Add `JOINT_REPAIR.md` to
`_EXCLUDED_NAMES` anyway; it costs nothing.

**N3. Two contract clarifications missing from TASK_DRAFT.md.** My contract
stated (a) unknown columns are ignored, and (b) blank lines are ignored and
not counted toward the row total. TASK_DRAFT.md adopts the contract but
carries neither sentence. Two of the private challenge inputs (p1, p9) depend
on them. Either add both sentences to the brief or tell me and I will drop
p1 and p9 from the private set and republish the hashes; scoring a solver on
a rule it was never given is exactly what the blind protocol forbids.

**N4. Isolation defaults leave the solver unable to work, by design or by
omission.** `run_isolated` defaults to `--network none` and
`HOME=/tmp/solver-home`. With no network the solver cannot reach a vendor,
and with an empty HOME it has no CLI credentials. The scored attempt needs
`network=True` plus a credential-only HOME injection (auth files, no
sessions, no history, no instruction files). Nothing in the batch does the
second part, and the log rightly does not claim an "authenticated container".
This is the open item on the runner, and I would rather it be listed as such
in IMPLEMENTATION.md than discovered at launch. Also: `--pids-limit 128` is
tight for the public validation suite, which launches Chromium under
Playwright (a headless Chromium alone is 20 to 40 processes, plus node,
plus pytest). Suggest 512 and a note that the limit was chosen, not
inherited.

**N5. `_fold_disallowed` lost its index guard.** If the flag is the last
token it indexes past the end. Unreachable today because every readonly arg
list that carries the flag carries a value, and off mode refuses extra args.
Two-line guard; I will include it in the N1 commit since the file is mine.

### What this review does not claim

No live control proof, no scored attempt, no solver output read. Docker and
installed-codex checks were not executable here. The private challenge set
stays outside Git; only the hashes on PR #11 are public.

## 2026-09-15 — N1 and N5 implemented (my lane: `cli_providers.py`, `delegation.py` whitelist, `tests/test_native_mode.py`)

Separate commit, easy to revert. What changed and what it is evidence of:

- **Grok, off mode.** A folded `--disallowed-tools Agent` is now remembered
  on the provider for that call (`_native_fanout_denied`, reset on every
  `_build_argv`). If the envelope's tool calls (read through the existing
  diagnostics whitelist, names only) still contain a denied name, it becomes
  a `NativeChild` `unidentified:grok:Agent` with unknown usage, annotated
  `CONTROL FAILURE: native child ran although the call sent
  --disallowed-tools Agent`, and flows into `finish(native_children=...)`,
  so the run budget latches `uncontrolled_native_delegation` on the first
  such call. This is the case the 2026-09-12 `--always-approve` experiment
  predicts, so it is the one that needed a stop, not a log line.
- **Claude, off mode.** `--output-format json` carries no tool calls, only
  `permission_denials`. A denial naming `Task`/`Agent` is the control
  holding, so it is recorded as `denied_tools` in the diagnostics (whitelist
  extended, same name filter as `attempted_tools`; tool inputs never reach
  the record) and not as a child. A claude child that was *not* denied is
  not observable in this output format. That gap stays open and is now
  stated in `native_delegation_disabled`'s docstring; closing it is either
  a live probe or a switch to `stream-json`, which changes the result
  parser and is not in this lane.
- **Default mode** is untouched: no denial sent means an `Agent` call is the
  vendor's default, not a failure, and produces no child.
- `native_delegation_disabled` is true for codex (control args) and for a
  claude/grok call that folded the denial. The CONTROL FAILURE note names
  whichever was sent.
- `_fold_disallowed` regained its index guard (a valueless trailing flag gets
  a fresh pair appended rather than an `IndexError`).

Verification: `ruff check .` clean, `git diff --check` clean, full suite
**851 passed, 6 skipped** (same six opt-in Docker/installed-codex checks),
eight new tests in `tests/test_native_mode.py`. Nothing here is a live
proof; it is what the harness will now do with the evidence a live run
returns.

## 2026-09-15 — Codex's correction on 7179c90 taken: grok evidence is "attempted", not "ran"

Codex is right that a tool name in grok's `toolCalls` does not prove a child
executed; a cancelled or refused request leaves the same record. Changed in
my lane:

- Synthesised grok children carry the `ATTEMPTED_DELEGATION` marker in their
  detail ("denied fan-out tool named in the envelope's tool calls; whether it
  executed and what it spent are unknown").
- The annotation now has two wordings. A child the vendor's own stream
  reports (codex) keeps `CONTROL FAILURE: native child ran although the call
  sent ...`. A denied-name child gets `CONTROL FAILURE (suspected): a denied
  fan-out tool was attempted although the call sent --disallowed-tools
  Agent; execution and usage unknown`. The two kinds stay distinct in the
  record.
- The budget latch is unchanged: an attempted denied tool still stops the
  run, on the reasoning that under `--always-approve` an attempt may well
  have run and stopping on suspicion is the conservative reading.
- Regression added: a grok envelope that lists `Agent` and ends `cancelled`,
  driven through the real `generate` path with a faked launcher and a live
  `RunBudget`. Asserts the turn fails as cancelled, the child is recorded as
  attempted (never "ran"), tool arguments do not leak into the detail, the
  budget's stop reason is `uncontrolled_native_delegation`, and the next
  reservation is refused.

Verification: ruff clean, `git diff --check` clean, full suite **852 passed,
6 skipped**. No runtime files outside my lane touched; no scored attempt.

## 2026-09-15 — live control failure on codex: root cause and the enforceable control

Codex's clean-image probe (parent 01a0a5bd-af5e…, child 01a0a5bd-c623…,
codex-cli 0.154.0, both feature switches reported `false` by preflight) showed
Sol spawning Sol. I read the codex source at tag `rust-v0.154.0` rather than
guess. The cause is precedence, not a broken flag.

`core/src/config/mod.rs`:

```rust
fn multi_agent_version_override(&self) -> Option<MultiAgentVersion> {
    if self.features.enabled(Feature::MultiAgentV2) { Some(V2) }
    else if !self.agents_enabled { Some(Disabled) }
    else { None }
}
fn multi_agent_version_for_model(&self, model: Option<MultiAgentVersion>) -> MultiAgentVersion {
    self.multi_agent_version_override()
        .or(model)
        .unwrap_or_else(|| self.multi_agent_version_from_features())
}
```

Highest first: `features.multi_agent_v2` on forces V2; `agents.enabled =
false` forces Disabled; otherwise the model's own declared version applies
(the built-in table leaves it `None`; the value comes with the model
metadata the CLI fetches and caches from the server); and only when the
model declares nothing do the feature flags decide. `--disable` edits that
last resort. GPT-5.6 evidently declares a version, so the flags were never
consulted, and `tools/spec_plan.rs` adds `spawn_agent` and friends whenever
the resolved version is not `Disabled`. `features list` printing `false` was
true and irrelevant. The config schema says the same in one line:
"agents.enabled: Whether multi-agent tools are enabled. Defaults to true.
An enabled features.multi_agent_v2 setting takes precedence."

Change (my lane, `cli_providers.py` + `tests/test_native_control.py`):

- `CODEX_NATIVE_DELEGATION_CONTROL` is now `--disable multi_agent --disable
  multi_agent_v2 -c agents.enabled=false`. The `--disable multi_agent_v2`
  stays because an enabled V2 feature is the one thing that outranks the
  agents override, and the scanner already refuses any operator attempt to
  enable it or to touch the `agents` table (an agreeing `-c
  agents.enabled=false` from `.env` is still refused: the harness sends its
  own, and the only reason to touch that table from outside is to re-admit
  something). `-c` beats profile and config-file values, which the earlier
  precedence probe already showed for `features`.
- Spec comments now carry the precedence chain and the source location, and
  say plainly that on this vendor a switch that reads `false` has already
  been shown not to be the switch. The opt-in `features list` test says the
  same.
- Suite **853 passed, 6 skipped**; ruff and diff-check clean.

What I could not verify here: no codex binary, no Docker, no subscription.
`agents.enabled` is accepted by 0.154.0 (your earlier note: it takes
`agents.enabled`, rejects `agents.bogus`), and the source says it forces
`Disabled`; that the model then has no `spawn_agent` is the live claim.

**Proposed single follow-up probe** (one unscored call, low effort, no
spawn attempted, so no child can cost anything):

```
codex exec --skip-git-repo-check --json --sandbox read-only \
  --disable multi_agent --disable multi_agent_v2 -c agents.enabled=false \
  -c model_reasoning_effort=low -m gpt-5.6-sol \
  "List the exact names of every tool available to you in this session, one per line, and nothing else."
```

Pass: no `spawn_agent`, `send_input`, `wait_agent`, `resume_agent`,
`close_agent`, `send_message`, `followup_task`, `interrupt_agent`,
`list_agents` in the reply, and no `collab_tool_call` item in the stream.
Control: the same command without `-c agents.enabled=false` should list
them (that is the failure you observed, restated cheaply). If the pass
holds, a second call may repeat the spawn-provoking prompt under the full
control; if the first fails, stop, because then the resolver differs from
the tagged source and I want the reply text before proposing anything else.
Keep the parent's `thread_id`, the `item.completed` items and `usage` from
both calls; that is all the evidence this needs.

## 2026-09-15 — review of Codex's runner and brief batch (d6cd165, 2d99600)

Read on the merged head after my rebase. `ruff check .` clean, `git diff
--check` clean, full suite **861 passed, 7 skipped** here (the seven are the
Docker, credential-container and installed-codex opt-ins).

Brief (N2/N3): both contract sentences are in `TASK_DRAFT.md` and match the
private set as generated: p1 (extra columns ignored, total 2) and p9 (blank
line ignored, total 1, physical line numbers kept). No private case changes,
so the published archive hash stands unchanged. The Design section is copied
without the runtime-model paragraph, which the CSV preview never touches.
Good.

Runner (N4), `isolated_run.py`:

- `_credential_tree` accepts exactly three auth paths, owner-only modes,
  no symlinks (rejected by `_tree` before the name check), no other files;
  the seed must be a tree separate from work and runtime; it is mounted
  read-only and copied into the tmpfs HOME by a bootstrap shell under
  `umask 077`. The real-container test proves the copy, the clean HOME, a
  writable refresh that does not reach the source, and container removal.
  This is the shape I asked for. Nothing to add.
- `--pids-limit 512`, `network=True` chosen explicitly by the preflight.
  Closed.
- `timeout=None` now receives the remaining deadline and is restored
  (`providers.py`, test added). Closes the local review's item 6.
- `JOINT_REPAIR.md` excluded from the export, with a test. Closed.

`tools/acceptance/native_probe.py`: one call, one attempt, host execution
refused, evidence under `/work`, parent and explicitly linked same-cwd
children copied out of tmpfs before cleanup, mode 600, symlinks skipped;
regression covers the selection. The prompt is the spawn-provoking one; it
is the right second step after the tool-list probe in my previous entry,
not a replacement for it.

Two notes, neither blocking:

- `Dockerfile` pins `grok-1.0.30-linux-aarch64`, so the image is
  arm64-only; the image id in the evidence already fixes that, but the
  README should say the recipe is architecture-specific.
- `evidence/container-preflight.json` records `native_features` both
  `false` as part of a passing preflight. After today's finding that field
  is a true statement about the wrong switch; the next preflight should
  print the effective tool list (the probe above) or drop the field from
  the pass criteria so nobody reads it as the control again.

No solver output exists; nothing read. Waiting on the tool-list probe result
before anything else in my lane.

## 2026-09-15 — scope check: the tools left after the Agent/Task denial

Codex's question: can Claude's ListAgents, SendMessage, Workflow,
TaskOutput, TaskStop, ToolSearch and scheduling tools, or Grok's workflow,
search_tool and use_tool, reach another session or start work despite the
denial? Read against code.claude.com/docs (cli-reference, tools-reference,
workflows, agent-teams, cross-session-messaging, env-vars) on 2026-09-15.
Grok's docs (docs.x.ai) are unreachable from this environment.

Claude, verified from the docs:

- **Workflow** "orchestrates many subagents in the background"; under `-p`
  the call goes through ordinary permission evaluation, so a deny rule
  stops it, and `CLAUDE_CODE_DISABLE_WORKFLOWS=1` removes it at startup.
  A real bridge; now closed both ways.
- **SendMessage / ListAgents** reach other sessions on the same filesystem
  (two solver calls in one container could message each other) and, only
  while connected to Remote Control, cloud and other-machine sessions. A
  container cannot see host sessions. The documented off switch is a bare
  deny of both names. Closed.
- **RemoteTrigger** creates and runs claude.ai Routines, which can start
  fresh sessions. Closed.
- **CronCreate** schedules a prompt that re-enters the same session;
  session-scoped, but it is a way to continue past the one turn the
  harness asked for. Closed.
- **mcp__\*** every MCP tool. The fresh HOME carries no MCP config, but the
  export does not exclude a project `.mcp.json`; the deny costs nothing.
- **TaskOutput, TaskStop, ToolSearch, ScheduleWakeup**: session-scoped per
  the tools reference; a denied tool stays denied however its schema was
  loaded. Left available.
- **Agent teams** are off unless `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
  and never form under `-p`; pinned to `0` in the call environment anyway.
- A bare name in `--disallowed-tools` "removes the matching tools from
  Claude's context", so the model never sees them (cli-reference).

Change in my lane (`cli_providers.py`, `tests/test_native_mode.py`):
`CLISpec.native_fanout_off_env`, applied only when the denial was folded
into that call; claude off mode now denies
`Task Agent Workflow SendMessage ListAgents RemoteTrigger CronCreate mcp__*`
and sets `CLAUDE_CODE_DISABLE_WORKFLOWS=1`,
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0`. Bash, Edit, Write, Read, WebFetch
and the rest are untouched and a test pins that. Default mode is unchanged.

Grok, **unverified**: `workflow`, `use_tool` and `search_tool` read as a
workflow runner and a by-name tool dispatcher, which is exactly the shape
that can route around a denied name. Off mode now denies all three next to
`Agent`; the direct read/write/exec tools are separate names and stay. Ask:
paste the shipped README's and `--help`'s lines for those three tools (no
model call) so this can move from suspicion to a reading; and re-run the
tool-list probe once under the wider denial to confirm read_file, bash and
write tools are still listed. A grok envelope naming any denied tool is
read as attempted delegation, same as Agent.

Not changed, for your decision: `--bare` would also skip hooks, skills,
plugins, MCP and CLAUDE.md discovery and not bind the inbox socket, which is
attractive for blindness, but it narrows the tool set to Bash and file
tools and is a bigger behavioural change than this lane should make alone.

Verification: ruff and `git diff --check` clean, full suite **864 passed,
7 skipped**.

## 2026-09-15 — review of 7b573c5 (live checks under agents.enabled, vendor probes, final image)

Read `evidence/agents-enabled-probes.json`, `vendor-control-probes.json`
and `application-preflight.json` on the merged head.

- **Sol under the full control**: tool list `functions.wait`,
  `functions.request_user_input`, `functions.exec`; spawn prompt answered
  "Native delegation unavailable"; no collab events, no children; parent
  rollouts retained and hashed. That is the pass criterion I set and it is
  met. Same image as the failure, so the only variable was
  `agents.enabled=false`. Verdict: the codex control holds on 0.154.0 for
  this model. Still a live claim per version, as the evidence says.
- **Claude Fable's tool list before e51f811** included `ListAgents`,
  `SendMessage`, `Workflow`, `RemoteTrigger`, `CronCreate` next to the
  ordinary tools. That confirms the scope question was real: the Agent/Task
  denial alone left every documented route to other sessions and to
  workflow fan-out exposed. e51f811 denies them; the next tool-list probe
  should show none of the five.
- **Grok's tool list** confirms the direct tools are separate names
  (`run_terminal_command`, `read_file`, `search_replace`, `write`, `grep`,
  `list_dir`, web tools) and that `search_tool`, `use_tool`, `workflow` sit
  beside them. No `Agent` appears under the denial. Denying the three
  meta-tools therefore cannot remove read/write/exec; what they do remains
  unverified until the README lines arrive.
- Grok restricted read: value retrieved, source unchanged, exact-reply
  check honestly failed on narration. Agreed with the recording; the
  extractor's "narration is not an answer" rule is the right one and this
  is a prompt-shape problem, not a control problem.
- Final image `sha256:d1f9…390e2`: 100 Python, 18 Node, 9 mutants killed,
  8 Chromium scenarios, 30 export hashes unchanged after testing. Good
  baseline for the freeze.

One repair, in your file: `tests/test_probe_evidence.py` asserted `'Agent'
in argv` as a standalone token; since e51f811 the value is one token with
four names, so the assertion now splits the value. Cause was my change, so
I fixed it rather than leave CI red. Full suite **865 passed, 7 skipped**
after the fix.

## 2026-09-15 — handoff: scope settled, freeze unblocked

Davis flagged that each side reported waiting on the other. The cause was
my "still open on my side" line in the fdcbd99 comment, which read as a
gate. It is not. Posted on PR #11: the scope review Codex gated the freeze
on is complete (comments 5684584074 and 5684624988, commits e51f811 and
fdcbd99); the grok README lines are for the record only; no further
preflight probe is required. Remaining steps are Codex's alone: freeze
input/runtime/config hashes on the current head, run the one bounded
attempt, save it, say so here. I then score against the private set. I
will not touch this branch until the attempt is saved, so the freeze has
no moving target.

## 2026-09-15 — grok denial separator corrected before the live check

Codex's documentation read (comment 5684672xxx on PR #11) settled two
things. First, grok's own guide says every workflow `agent()` call spends a
child-agent slot and `GROK_WORKFLOWS=0` disables workflows, so the widened
denial was right and the kill switch now goes in the environment too,
mirroring `CLAUDE_CODE_DISABLE_WORKFLOWS`. Second, `grok --help` says
`--disallowed-tools` is comma-separated. My e51f811 value was
space-joined, which grok would have read as one nonsense name that denies
nothing; the live tool-list check would have shown the three meta-tools
still present. Corrected before that check: `CLISpec` gained
`disallowed_tools_separator`, grok sets `","`, the fold and the denied-name
bookkeeping honour it, and the value is now
`Agent,workflow,use_tool,search_tool`. Claude keeps whitespace. Tests pin
both spellings and the no-space property. Full suite passes; ruff and
diff-check clean.

This is the one exception to "no branch edits until the attempt is saved":
a control that denies nothing must not be frozen. Nothing else changes.

## 2026-09-15 — grok off-mode denial widened to the documented spawner and scheduler

Codex's grok tool-list check on fdcbd99 (the space-joined value) listed
`spawn_subagent`, `scheduler_create`, `workflow`, `use_tool` and
`search_tool` still present, which confirms the separator bug fixed in
66dc387 and adds two names the installed docs identify: `spawn_subagent` is
the native spawner with `Agent` as its alias, and `scheduler_create`
schedules a later re-entry (the analogue of Claude's CronCreate). Off mode
on grok now denies `Agent,spawn_subagent,workflow,scheduler_create,use_tool,
search_tool`, comma-joined, with `GROK_WORKFLOWS=0` in the environment.
The spec comment is rewritten from the doc lines Codex quoted
(04-slash-commands.md:296, 05-configuration.md:364-372,
07-mcp-servers.md:213-218, 14-headless-mode.md:35,51-82). Default mode and
the restricted seat's own arguments are unchanged; two regressions pin the
normal and restricted off-mode argv byte for byte and the default-mode argv
as before. Full suite passes; ruff and diff-check clean. Codex runs one
grok tool-list check from this commit; pass means the five names are
absent and read_file, run_terminal_command, search_replace, write remain.

## 2026-09-15 — independent scoring of scored attempt 1 (run b7ccbd24)

Archive `source-after.tar.gz` sha256 `517fb2ff…d7f9` matches the posted
value; all 31 file hashes match `source-after-sha256.json`; extracted into a
fresh scratch directory; nothing overlaid; private cases unchanged (archive
hash `71cb8b06…71a` re-verified). Machine-readable summary in
`evidence/scored-attempt-1/claude-independent-scoring.json`.

### Correctness

- **Private set: 7 of 9.** Fails: `p3_duplicates` (no duplicate detection;
  every row reported valid) and `p4_players` (players always `[]`; an
  unknown player not flagged). Passes: extra columns, number forms, tag
  names and the 5000-row limit, UTF-16 and Latin-1 rejected as
  `bad_request`, BOM with trailing newline. Both failures are the two
  sub-features the orchestrator's own slice description deferred, so the
  slice that was built is correct for what it claims.
- **Public examiner: 9 of 13** (fails on duplicates, clip-id collisions,
  the duplicate row of the mixed manifest, and the missing
  `docs/CSV_IMPORT.md`). **Browser: 0 of 17**; no UI was written, the
  runner stops at the first missing control.
- **Application suite: 102 passed**, `node --check` clean. Regression
  preservation confirmed independently, matching Codex's container run.
- Verdict: **incomplete, partially correct.** One of four planned slices
  landed, and that slice holds up against held-out cases it never saw.

### Routing and coverage

Fable planned and decomposed into four slices (required columns; players
and duplicates; UI; docs), which is a sound decomposition. The ladder sent
"backend/simple" to `grok:default`, as `DIFFICULTY_LADDER` says it should.
No workers were commissioned, no OpenAI seat, no Opus review: the run
stopped before the first task closed, so review never came due. Coverage
of the roster is therefore one of three vendors invoked, by budget
exhaustion rather than by routing.

### Control enforcement

Native-off held on every vendor invoked: Claude `subagent_stats.spawned=0`,
grok 10 and 8 model calls with no observed child, codex never called. Scope
held: 132 lines in exactly the two permitted paths, inside the 150-line
tolerance. Store byte-identical. The budget latched after the closeout
returned, preserved the reply, and refused a fourth attempt; wall 439 s of
900. No unknown-usage attempt. Pass on every control.

### Usage, and the two questions Codex asked

**1. Closeout: 8 grok model calls, 258,413 reported tokens, for a record the
transcript already contained.** Cause, from the evidence:

- `session._close_out` calls `_invoke_model(lead, …)` on the lead's seat.
  For grok that is the full agent form: `--always-approve`, every tool,
  effort high (`invocations.jsonl` role `closeout`, `allow_writes: false`
  in `in-flight.json`, yet the same seat).
- The prompt (`closeout-prompt.txt`) carries the scope block ("You may
  change only these paths…", the acceptance list, the size bound) and the
  standing rule "Use actual files as evidence", appended by the invocation
  wrapper regardless of the read-only grant. The model read it as an
  invitation: its reply opens "I'll inspect the finished endpoint and tests
  so the record matches what actually shipped", then "skim nearby helpers
  and imports", eight turns in all.
- Grok re-sends the conversation on every turn. `vendor-usage-extract`
  shows 214,400 of the 258,413 as cache reads, which the protocol counts at
  full weight. Eight turns times a ~27k context is the whole bill.

The record it produced is good, and every fact in it is derivable from the
transcript plus the 132-line diff. Proposed fix, small, role policy
unchanged (same model line, same seat holder):

- Invoke closeout through the vendor's *bounded* call form (the shape the
  restricted seat already uses: `-p`, no write tools, effort low), with
  the task diff pasted inline and the scope block and evidence rule omitted
  for this role. Closeout is a record-writing errand, not a judgment, so
  bounding it does not conflict with "senior seats are never restricted";
  say so in the rule. Expected cost: one or two model calls, roughly 30 to
  40k tokens instead of 258k. Evidence for the estimate: the lead's own
  final message plus the diff is under 10k tokens of input.

**2. Haiku 2,817 auxiliary tokens outside the controller total.**
`_extract_claude_usage` reads the envelope's top-level `usage`, which is
the seat model's usage only. `modelUsage` carries one row per model the
CLI used; the Haiku row is Claude Code's own auxiliary call, not a
Quadratus worker. Budget coverage gap: 0.5 percent here, unbounded in
principle. Proposed fix, in my file: when `modelUsage` is present, sum
every row's input (including cache) and output into the reported usage,
keep the seat model's row for `resolved_model`, and record the other rows
by name and totals only under a new whitelisted diagnostics key
(`auxiliary_models`). The original ledger for this run stays as written.

**Observability limits, stated once:** Claude's json envelope shows turns
and denials but not tool calls; grok's envelope shows tool names only on a
cancelled turn; codex alone reports children. "Three attempts" and "23
vendor model calls" are both true and measure different things; the ledger
should carry both, which it now does via `vendor-usage-extract.json`.

**A policy question for Davis, not a defect:** 386,560 of the 614,386
counted tokens were cache reads. Under this protocol an agentic grok step
costs roughly 250k, so a 500k threshold buys two steps of a four-slice
plan. Either the threshold is raised for grok-led work, or cache reads are
weighted below fresh input for the stop rule while still being reported
raw. I would report both totals in `budget.json` and let the operator
choose which one stops the run; the run itself behaved exactly as agreed.

### Repair batch (recommended, not implemented)

1. Closeout on the bounded call form with inline diff; scope block and
   evidence rule omitted for the closeout role (`session._close_out`,
   Codex's file; the seat form is in mine).
2. Claude usage sums `modelUsage`; auxiliary rows to diagnostics
   (`cli_providers.py`, `delegation.py`, mine).
3. `budget.json` reports raw and cache-weighted totals side by side;
   the stop rule stays on raw until Davis decides (`run_budget.py`, Codex).
4. Optional: a pre-call guard that refuses to start an attempt when the
   remaining threshold is below the seat's last observed call, which would
   have avoided the 114k overshoot at the cost of one fewer attempt.

Nothing in the application, the private cases or the model roles was
changed by this review. No new trial.

## 2026-09-15 — corrections to the scoring record (from Codex's reconciliation)

Codex checked the review against the envelopes and the session code and
found five record details wrong. All five verified and accepted; the scores
and the two causal findings stand. The previous entry stays as written; this
entry supersedes the details below, and the summary JSON is updated.

1. **Vendors invoked: two of three** (Claude via Fable, Grok via lead and
   closeout), not one. OpenAI was the vendor not invoked.
2. **Cache reads total 410,538**: 23,978 Claude plus 172,160 Grok lead plus
   214,400 Grok closeout. The 386,560 I reported was Grok only. The policy
   observation is unchanged in direction: two thirds of the counted total
   was cached input.
3. **No Opus review on this task was by design, not by timing.**
   `Complexity._COLLABORATORS` maps SIMPLE to zero collaborators, and in
   `_run_task` collaborators run before `_close_out`. So "review never came
   due because the run stopped first" was the wrong reason: no review was
   ever assigned to a SIMPLE task. Later tasks were never attempted, so
   worker and reviewer coverage remain unvalidated by this run.
4. **Browser: zero passes with five of seventeen checks executed**; the
   runner stops at the first missing control. "0 of 17" overstated what
   ran. Likewise, 5 Claude turns and 10 plus 8 Grok `modelCalls` are
   different vendor fields and do not add to a uniform "23 model calls".
5. **Controls wording is scoped**: no native child was observed on either
   invoked vendor within that vendor's published observability. That is
   the supported claim; "pass on every control" read as universal and is
   withdrawn in favour of the scoped form.

Codex's dispositions on the repair batch: bounded closeout and auxiliary
accounting first, existing budget and roles retained; the raw-token stop
rule stays; weighted limits and historical-cost admission are deferred
policy questions, and my 30 to 40k closeout figure is an estimate, not a
validated number. Agreed on all of it.

## 2026-09-15 — repair item taken: complete auxiliary usage accounting (my lane)

STATUS and the reconciliation assign this to me, so it is implemented now
rather than waited on. Files: `cli_providers.py`, `delegation.py`
(whitelist), new `tests/test_claude_auxiliary_usage.py`.

- `_extract_claude_usage` now reads both the top-level `usage` (the seat
  model) and every `modelUsage` row, and reports the **larger of** the seat
  figure and the rows' sum. The two are never added: the seat's row is
  inside the sum, so each token is counted once. On the attempt-1 envelope
  this yields 62,738 + 2,801 input and 2,827 + 16 output, the 2,817 Haiku
  tokens the controller missed.
- `_extract_claude_diagnostics` (new, wired as `CLAUDE_SPEC.extract_diagnostics`)
  gives provenance: `auxiliary_models` (row names other than the seat, the
  seat being the row whose totals equal the top-level usage) and
  `auxiliary_tokens` (their input plus output). Rows that do not match the
  seat are marked `unattributed` with the excess over the seat reported. A
  malformed row sets `auxiliary_usage: unknown`; the seat's known figure is
  still reported, and the unparseable part is reported as missing, never as
  zero. `safe_diagnostics` admits exactly those three keys, names filtered
  like tool names, one bounded integer, two allowed states.
- Without `modelUsage` the figure is unchanged from before, pinned by test.
- Original run records are untouched; this changes future envelopes only.

Tests: eight new, covering the attempt-1 envelope, no-rows, seat-only rows,
malformed row, unattributed rows, rows without top-level usage, nothing
reported, and the whitelist. Full suite passes; ruff and diff-check clean.

Not done here, by assignment: the bounded closeout (Codex's `session.py`).
When you define its boundary, the seat form it needs already exists
(`for_seat(..., restricted=True)` builds the `-p`, no-write-tools call on
every vendor); say the word and I will expose a named closeout form if the
restricted worker shape is not the one you want.

## 2026-09-15 — two provider items for the closeout batch (my lane)

Codex's caller contract (PR #11, 18:11Z) confirmed with two stated
limitations; both provider items implemented, tests and full suite green.

**Accounting, tightened as asked.** Any malformed or partial `modelUsage`
now makes the whole figure unknown (`last_usage` None), so the run budget
stops instead of continuing on a count known to be incomplete; the seat's
own known figure survives in the diagnostics as `seat_tokens`. Fields are
validated as non-negative integers (strings, floats, booleans, negatives
and a missing required field are malformed, never coerced to zero); rows
whose sum is below the seat's own figure are partial and therefore
unknown; a tie between rows for the seat total is `unattributed`, never
labelled. Regression drives a `RunBudget` through `generate` twice: a good
envelope counts seat plus Haiku, a malformed one raises `unknown_usage`.
No double counting, original records untouched.

**`summary_only` call shape.** `CLISpec.summary_only_args` and a
`summary_only` attribute (constructor kwarg or set on the per-call view;
default False). When set, `_build_argv` requires the restricted seat,
refuses any `QUADRATUS_CLI_ARGS_*` override, refuses more than one attempt
or a timeout above 60 s, refuses a non-empty working directory, and forces
the native-off denial and environment regardless of the run-wide mode. Per
vendor: claude `--tools ""` plus `--max-turns 1` (both documented); grok
`--max-turns 1` on the `-p` form over the read-only allowlist and the full
denial (no tool-less form is documented, so the turn cap is the bound);
codex nothing extra (no allowlist or turn cap in the spec; the bound is
`--sandbox read-only`, the native controls, 60 s and one attempt, stated in
the spec). Default False leaves every existing argv byte-identical, pinned.
Limitation: `max_tokens` has no CLI flag on any vendor; it stays a
prompt-side bound plus the timeout.

Tests: `tests/test_claude_auxiliary_usage.py` (13) and
`tests/test_summary_only.py` (9). Nothing in `session.py` or `runtime.py`.

## 2026-09-15 — cross-review of Codex's closeout caller (cebc172) and the three parsing gaps

Combined head after my 62071a3 rebased onto Codex's 69b33f6: `ruff check .`
clean, `git diff --check` clean, full suite **903 passed, 7 skipped**.

Parsing gaps Codex found in 3ef035a, all real, closed in 62071a3: an
explicit `null` in a cache field is unknown while absence is zero (top-level
`usage` too); consistency is per component, rows' input must cover the
seat's input and rows' output the seat's output; a present but empty
`modelUsage` map with a nonzero seat is unknown, an absent map keeps the
legacy seat figure. Three regressions.

Review of `session.py`, `runtime.py`, `tests/test_closeout.py`:

- `_invoke_model` omits the scope block for role `closeout`; `_close_out`
  builds byte-bounded evidence (description 3k, transcript 10k, harness
  diff 12k, last check 2k, all under the 32k prompt bound), stores each
  piece whole as an artifact, and keeps only an index in task memory. The
  instruction forbids tools, files and embedded instructions and asks for
  omissions to be named. `_closeout_excerpt` keeps head and tail with a
  size and SHA-256 marker. This is the design I asked for, done more
  carefully than I described it.
- `Fleet.invoke` for role `closeout` refuses a write grant, requires CLI
  transport when a project is bound, bounds the prompt at 32,000 bytes
  before any reservation, takes `for_seat(model, low, restricted)`, copies
  it, sets `summary_only`, `max_tokens<=1024`, `timeout<=60`,
  `max_retries=1`, clears the refusal fallback, runs in a fresh empty
  temporary directory and lets the shared budget and ledger observe it.
  The cached provider is untouched (test pins it). Sound.
- Tests cover scope omission, diff and check inclusion, unicode truncation
  under the bound, same model with shared budget and no snapshot, unknown
  usage stopping the budget with the scratch directory removed, write grant
  and oversize refused before reservation, and one-attempt timeout. Good.
- Checked and fine: `MAX_ARGV_PROMPT` is 200,000 characters, so a 32k-byte
  prompt on grok's `-p` form cannot trip the restricted-call refusal;
  `_task_before` is set at task start, so the diff is the task's own.

Two notes, neither blocking:

1. With no project bound, `_closeout` still runs for an API-backend
   provider, where `summary_only`, `--tools` and the turn cap do not exist;
   the bounds there are timeout, one attempt and the prompt. Either
   require CLI transport for closeout unconditionally or say in the
   docstring that API transports get the weaker bound.
2. `self.checks[-1:]` may be an earlier task's check; the label says so.
   If check records ever carry a task id, filter on it.

Verdict: the caller matches the confirmed interface; combined suite green;
no live claim made. The bounded-closeout cost saving is still an estimate
until a run measures it, as the reconciliation says.

## 2026-09-15 — independent review of the live closeout replay (af9aa96, bbd7b75)

Evidence in `evidence/closeout-live-1/`, record in `CLOSEOUT_LIVE_1.md`.
Everything below was recomputed here, not read off the record.

**Verified.**

- Hashes: all seven published files match `sha256.json`; the prompt hash
  `b93e8d9c…cf42` is identical in `grok.json`, `claude.json` and
  `freeze.json`; the prompt is 11,456 bytes, under the 32,000 bound.
- Freeze: 35 of 36 runtime file hashes match the `c3a47c2` tree exactly.
  The 36th, `closeout_probe.py`, matches the committed harness
  (`714de05e…`) but was added at af9aa96, after c3a47c2; the record should
  say the harness postdates the frozen engine commit it ran with. The
  harness itself is small and does what it says: `Fleet.invoke` inside
  `invocation(…, 'closeout')`, one-call budget, ledger, launch capture.
- argv: rebuilt for both seats from the committed engine and the published
  prompt (restricted seat, `summary_only`, low effort, closeout system
  text from `runtime._closeout`); `sha256(json.dumps(argv))` equals the
  recorded `argv_sha256` for grok and for claude. So the live calls sent
  exactly: grok `--system-prompt-override … --output-format json --effort
  low --tools read_file,grep,list_dir,web_search,web_fetch
  --disallowed-tools Agent,spawn_subagent,workflow,scheduler_create,use_tool,search_tool
  --max-turns 1 -p <prompt>`, no `--always-approve`; claude `-p
  --system-prompt … --model fable --output-format json --effort low
  --disallowed-tools <off-mode denial> --tools "" --max-turns 1`. Both ran
  in an empty directory (`cwd_empty: true`) with a 60 s timeout.
- Usage arithmetic: the current extractors applied to the published
  envelope fields give grok 5,660 in (5,532 + 128 cache read) and 452 out,
  6,112 total, one model call; claude 5,103 in (2 + 5,101 cache creation)
  and 0 out. Claude's single `modelUsage` row equals the seat, so no
  auxiliary row, diagnostics None. Totals 11,215 as recorded.
- Prompt provenance: the structure is `_close_out`'s (instruction block,
  historical task description, transcript, harness diff, last check
  labelled "saved attempt; not rerun by this probe"); no scope block, no
  "use actual files as evidence" rule. The diff is attempt 1's (71 lines
  added in `app.py`, 61 in the test file).
- Summary fidelity: every checkable claim in `grok-reply.txt` holds
  against the prompt: ~70-line endpoint (71), 61-line tests, error strings
  differing from the spec's placeholders, `players` always `[]`,
  `duplicate` always 0, the four deferred items named, the old test result
  kept distinct from a new check. "DEAD ENDS: none recorded" is right for
  the supplied evidence (the 200-assertion note appears under REASONING as
  a recorded alternative).

**Refusal, reviewed without any attempt around it.** Claude returned
`stop_reason: refusal`, label `reasoning_extraction`, one turn, zero
output tokens, `subagent_stats.spawned 0`, no permission denials. The
provider raised `ProviderRefusal`, the budget counted the input, no
fallback ran because the caller cleared it. Observation only: the prompt
asks for a section titled REASONING derived from another model's
transcript, and the label names reasoning extraction; that association is
plausible and unproven, and whether the section wording changes is a
product decision for the disposition owner, not a workaround I recommend.
Successful Claude closeout remains unverified.

**On the comparison.** 6,112 against 258,413 is one observation under a
different prompt (the old call carried the scope block and ran eight
tool-using turns whose re-sent context was the dominant term; this one
was a single turn). The reduction is the design working as intended, not
a benchmark. Agreed with the record's qualification.

**Limits.** Grok's envelope carries no tool-call trace, so "no tools used"
is inferred from `--max-turns 1`, `modelCalls: 1` and 11 s, not observed.
One sample per vendor. Worker coverage untouched by this probe.

Verdict: the record is accurate and its claims are supported by the
evidence; two record notes above (harness postdates c3a47c2; inferred, not
observed, tool absence on grok). No implementation, no new run.

## 2026-09-15 — independent scoring and review of scored attempt 2 (run d9df9b6c)

Archive `source-after.tar.gz` sha256 `5ffac782…3891`; all 31 source hashes
and all 19 artifact hashes match; extracted fresh; private set unchanged
(archive hash re-verified, 14 of 14 fixture and script hashes OK). Summary
JSON in `evidence/scored-attempt-2/claude-independent-scoring.json`.

### Correctness

- **Private set: 0 of 9.** Every case gets `405`: no endpoint exists. The
  run wrote a per-row validator helper (`_validate_clip_row`, 85 lines)
  and nine unit tests (138 lines), nothing the contract can observe.
- **Public examiner: 0 of 13**; browser not run (no UI, no endpoint).
- **Application suite: 111 passed** (102 existing plus 9 new). Regression
  preservation holds, matching Codex's container run.
- Verdict: **incomplete, unobservable at the contract level.** Attempt 1
  delivered an endpoint scoring 7 of 9; attempt 2 delivered less, because
  the decomposition chose a lower-level first slice and then under-sized
  it. The engine repairs were never reached: no closeout ran, and the
  accounting fix only had the orchestrator call to count.

### Scope sizing (finding 1)

The orchestrator declared 100 lines for "a validator plus tests covering
ten named cases" and the lead delivered 223 within the two permitted
paths, split 85 code and 138 tests. The stop is the rule working as
written; the estimate was the error. Ten named test scenarios in the
task's own text cannot fit a 100-line budget with the function they test.
Two points follow:

- The stop message ("the shape of a task expanding into the whole
  feature") asserts a cause the evidence does not show; the work stayed
  inside the slice. The message should state the measurement and leave
  the cause to the record.
- The lead was told "if the work genuinely needs substantially more, say
  so and stop", and did not. That is the lead ignoring a bound, and it
  cost 379,377 tokens before the harness caught it. A cheaper catch is a
  size check on the lead's own PATCH text before it lands, but that is a
  larger change than this batch.

### The contradictory signature (finding 2)

The task description revises itself mid-text ("`len(cols)`? No — …
instead give the function signature `_validate_clip_row(project, cols,
width, row)`") while `intended_result` and the acceptance list still name
`(project, cols, row)`. The lead implemented the four-argument form the
prose settled on; a literal reading of acceptance would fail it. Cause:
the orchestrator thought aloud inside a task description and emitted SCOPE
from its first draft. Smallest repair: the decomposition prompt requires
the description to be final text (no self-corrections), and a harness
check that any code signature quoted in acceptance appears verbatim in
the description, failing into the existing one-correction round.

### Usage and worker evidence (finding 3)

- Fable: 66 + 60,660 + 23,500 + 2,801 = 87,027 input, 2,922 + 14 = 2,936
  output, 89,963 total with the Haiku row counted exactly once. Verified
  against `vendor-usage-extract.json`; my parser's rule gives the same.
- Grok lead: 85,448 + 281,984 = 367,432 input, 11,945 output, 379,377
  total, nine model calls, `end_turn`; cache reads are 74 percent of it.
  Same cost shape as attempt 1's lead: under this counting a Grok lead
  step costs 300 to 400k, and the 500k threshold permits about one. Noted,
  not re-argued; the policy question stands as recorded earlier.
- No workers, no OpenAI seat, no Opus review: SIMPLE task, zero
  collaborators, stopped before any second task. Worker coverage remains
  unvalidated by this run too. Controls: Claude `subagent_stats.spawned 0`,
  no grok child within its observability, store untouched, budget not
  crossed, wall 336 s, ledger row `PartialWorkStopped` with
  `provider_outcome ok` and `post_return_failure true`, usage retained.

### Post-run fix review (finding 4)

`runtime.py` now keeps `safe_diagnostics(...)` on successful rows instead
of `{}`; one-line change, correct, and the regression pins the Fable row's
`auxiliary_models`/`auxiliary_tokens`, the unchanged 89,963 total, one
ledger event, and persistence. Approved. The frozen attempt-2 evidence
correctly still shows `diagnostics: {}` for that row.

### Smallest repair batch (recommended, not implemented)

1. Decomposition prompt: description is final text; acceptance must quote
   the description's signatures verbatim; estimate code and test lines
   separately (Codex, `session.py`/prompt text, plus a scope lint).
2. Scope stop message: measurement only, no asserted cause; report the
   code/test split (Codex, `scope.py`).
3. Runtime provenance fix: approved as pushed (no further work).
4. Policy for Davis, unchanged from before: Grok lead cost per step versus
   the 500k threshold, and whether test lines count fully against a
   declared code estimate.

No application feature, private-case edit, role or budget change, or
model run by this review.

## 2026-09-15 — disposition of the attempt-2 review, run by Claude

Davis asked me to run the disposition myself because Codex is running out
of context. This entry implements repair items 1 and 2 from my attempt-2
review. Item 3 was already pushed by Codex; item 4 stays a policy question
for Davis and is unchanged. Nothing else: no app feature, no private-case
edit, no budget, role or tolerance change, no model run.

### What changed

`quadratus/scope.py`
- `lint_declaration(body, scope)` runs at the end of `read_scope`. A
  function is *declared* when the description writes `def name(` or when
  `intended_result`/`acceptance` quote `` `name(...)` ``. For declared
  names, every `def` line and every backticked `name(args)` across the
  whole declaration is collected with whitespace normalised; more than one
  distinct form raises `ValueError` naming both. Incidental calls
  (`float()`, `len(row)`) are never declared and cannot trip it. The
  exact attempt-2 declaration from `result.json` is rejected:
  `_validate_clip_row(project, cols, row)` versus
  `(project, cols, width, row)`. The error feeds the existing single
  correction round in `Session.next_task`; a second failure stalls.
- `ScopeReport` carries `code_lines` and `test_lines`
  (`count_change_lines_by_path` attributed per `+++`/`---` header;
  `is_test_path` by convention: `tests/`, `test/`, `spec/`, `__tests__/`
  directories or `test_*`, `*_test`, `*.spec`, `conftest` basenames).
  Tests still count in full against `max_lines`; the split is reported,
  not excused.
- The oversized wording no longer asserts a cause. Attempt 2 now renders:
  `223 changed lines (85 in code, 138 in tests) against a stated bound of
  ~100, stopped past 150. Tests count in full. Say what the estimate
  missed, or what grew.`

`quadratus/session.py`: `_SCOPE_REQUEST` adds: estimate code and test
lines separately and set `max_lines` to their sum, tests count in full; the
description is final text with no revisions, alternatives or thinking
aloud; one signature per function, quoted verbatim in `intended_result`
and `acceptance`; a disagreeing declaration is rejected and comes back for
correction. The same text is re-sent with the correction.

`quadratus/project_run.py`: `result.json` scope reports gain
`code_lines`/`test_lines`.

`tests/test_scope_declaration_lint.py`: twelve tests plus a nine-case
parametrised one. Rejection of the attempt-2 shape naming both forms;
acceptance disagreeing with the description; a final description passes;
incidental calls ignored; whitespace and trailing colon normalised; lint
failure drives the correction round and a second failure stalls; assess
splits code/test; the oversized message contains the measurement and no
"expanding"/"whole feature"; test-path conventions; the decomposition
prompt carries the new sentences.

### Verification

Suite 934 passed, 7 skipped (Docker and installed-CLI checks skipped
here) in 44.58 s; ruff and diff-check clean. Lint re-run against the
frozen attempt-2 `result.json` declaration and `changes.diff`: rejected,
and the split is 85/138 of 223. Frozen evidence untouched.

### Not done, on purpose

- The lead-side bound ("say so and stop") that the Grok lead ignored is
  unchanged; a pre-landing size check on the lead's PATCH text is a
  larger change than this batch.
- Whether test lines should count fully, and the Grok per-step cost
  against the 500k threshold, remain Davis's calls.
- No live attempt is queued. Any attempt 3 is a new authorisation.

## 2026-09-15 — operator rulings and attempt-3 preparation

Davis ruled on the two open policy items:

1. **Test lines count in full.** They are written code; no reason to
   exclude them unless it obstructs testing. This is already the engine's
   behaviour (`count_change_lines` counts every added or removed line, the
   report only splits the figure). No change made. Standing ruling.
2. **Grok stays.** The point is an end-to-end test of the whole lineup on
   subscription windows; token spend is what the subscription is for.
   Roles, ladder and the 500k reported-token threshold are unchanged.
   Standing ruling.

Davis then authorised attempt 3 ("start the process of testing the product
as we had initially intended").

### What is prepared here

`evidence/scored-attempt-3-freeze.json`: runtime commit 4795062 with all 36
runtime file hashes recomputed from the tree; four differ from the
attempt-2 freeze and each is named with its commit (Codex's runtime.py
provenance fix, my scope.py/session.py/project_run.py disposition). Input
re-verified: all 29 application files hash-match `a8772ab` from the
GameTape checkout, and `TASK_DRAFT.md` hashes to the frozen `TASK.md`
commitment (71909dad…). Private archive commitment unchanged
(71cb8b06…), contents unopened by the launcher. Configuration and limits
copied verbatim from attempt 2: 24 calls, 500,000 reported tokens, 900 s,
two workers, native delegation off, `python -m pytest -q` as the check.
The two rulings are recorded in the freeze as `operator_rulings`.

### What cannot happen from this container

This session runs on a cloud x86_64 container with only the `claude`
binary installed, no `codex` or `grok` CLI, no subscription credentials
for any vendor, and the acceptance image is Linux arm64. A scored run
needs all three authenticated CLIs (Davis just confirmed Grok is
required), so the launch must happen on the authenticated host as before.
The freeze's `image_id` is carried from attempt 2 and must be re-inspected
there; a different ID re-issues the freeze.

### Launch handoff (for Codex or Davis on the Mac)

1. `git fetch && git checkout <freeze commit>`; confirm
   `sha256sum` of the 36 runtime files against the freeze.
2. `docker image inspect quadratus-blind-preflight` and compare the ID.
3. Export `a8772ab` with the frozen path list and `TASK_DRAFT.md` as
   `TASK.md` (as for attempt 2); verify the file hashes.
4. Stage the auth-only credential seed; run
   `python /opt/quadratus/blind_trial.py` under `run_isolated(...,
   wall_seconds=900, network=True, credentials=seed)`.
5. Remove the container and seed; save `.quadratus/runs/<id>` plus the
   source-after archive under `evidence/scored-attempt-3/` with
   `artifact-sha256.json` and `source-after-sha256.json`, as for attempt 2.
6. Post the handoff on PR #11. I score against the unchanged private set
   and review the run, as before; disposition afterwards is mine unless
   Codex is back.

No solver coaching, no prior output, no model names in the solver input.
Not a retry of attempt 2: a fresh run from the original input.
