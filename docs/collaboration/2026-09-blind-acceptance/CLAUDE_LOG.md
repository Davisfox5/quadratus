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
