# Stage B: paired feature runs, Jev versus the rule (plan, 2026-10-01, revised 2026-10-02)

Seven real GameTape features, each built twice on identical checkouts
under identical limits, once with `--decider jev` and once without. Each
build is a planned run: the orchestrator decomposes the feature itself,
and the survey rule lets it continue through failures as long as every
re-plan logs a HYPOTHESIS line in the harness (Davis, 2026-10-02: go deep,
find the failure points, log the breakages instead of stopping on them). The two arms of a pair start together so both see the same vendor
availability. Claude quarterbacks the runs from the IDE on Davis's Mac
(Davis's call, 2026-10-01); Codex is asked for a usage reading and a
light review, not for execution, because its weekly window is near its
end. Nothing here spends until Davis is in the IDE and the packet is bound
to the real tree.

## What the comparison answers

Two questions, from the same fourteen runs.

1. Routing: does labelling by Jev instead of by the orchestrator change
   which leads take a feature's tasks, and does that change what gets
   delivered and what it costs? Stage A answered only the first half on
   rote and simple samples (`#35` comment 5924736888).
2. Reliability: where does a deep, multi-task run break? Every cell's
   survey section records each failure, its cause, whether it was
   recovered, and the orchestrator's hypothesis about it. That evidence
   does not depend on the arm and comes for free from the same runs.

It does not answer whether Jev is "right": there is no ground truth for a
feature's kind or difficulty. The paired design makes the arms comparable;
the outcome measures are completion, closed-clean count, failed checks, the
lead actually chosen, calls, reported tokens and wall time. A faster failed
cell is not a win.

## Design

- Seven features, two arms, one repeat: fourteen runs. The runner supports
  `repeats` if a second pass is authorized later.
- Every cell is a planned run (`goal` only, no listed task text): the
  orchestrator writes the tasks under `max_tasks` (10), each inside
  `MAX_TASK_LINES`, with `declared_paths` as the scope fence. Survey mode
  is on (`survey_recovery` 4): a failed or capped task is kept, the
  orchestrator re-plans with a HYPOTHESIS line that the harness records
  and never acts on, a same-cause repeat with nothing new changed stops
  the run, and the fifth repair stops it. Leads still have to quote the
  command, exit status and error on any blocked report.
- Arms differ in who labels. On the jev arm the orchestrator is asked not
  to write a KIND line and Jev labels every task (`decider_labels="all"`,
  a small engine addition on #50: a planned orchestrator labels nearly
  every task, which would have left Jev nothing to decide). On the rule
  arm the orchestrator labels as it always has. So the comparison is
  Jev's labels against the orchestrator's, both routed by the same rule
  table. The packet validator refuses a KIND line in any goal.
- Each cell is its own detached worktree at the baseline SHA with its own
  `.quadratus` state, so runs never share source or state.
- Order is counterbalanced: even tasks start jev first, odd tasks rule
  first, and both arms of a pair are launched together. Pairs run one at a
  time by default (`--pairs 1`); `--pairs 2` doubles vendor load and is a
  call to make after the first pair's timings are in.
- Limits per cell, raised for depth: 90 calls, 2,500,000 reported
  tokens, 3,600 s wall, two workers. Fourteen cells bound the series at
  1,260 calls and 35M reported tokens; sequential pairs take at most about
  14 hours, so the series runs across sessions and the manifest carries
  the state between them. These are ceilings, not expectations: Codex's
  single-task outline was 20 calls and 500k tokens, and a feature of four
  to six tasks with up to four repairs should land well under the cap.
- Same `check`, `extra_checks` and `capture_profile` for every cell, read
  from the packet. The engine's own integration gate, capture and design
  review apply unchanged.

## The seven features (drafts; the bound goals are in packet.stage-b.json)

The goals in `tools/jev_stage_b/packet.example.json` are drafts written
without the GameTape tree in front of me. Each carries four to six
requirements so the orchestrator has to plan several tasks, and the
requirements are where the failure points are expected: a UI transition
without a reload, a round-trip through the import preview, a symlink
escape, an atomic write with a backup. Before `prepare`, each gets real
`declared_paths` and requirement wording a check can execute. Nothing in
a goal may hint at a label.

| id | feature | why it is in the set |
|---|---|---|
| f1-preview-errors | per-row error table under the import preview | frontend plus a response shape; a plausible standard |
| f2-project-search | client-side search over the project list | frontend/simple control |
| f3-project-rename | rename endpoint, persistence, card control, test | backend plus frontend plus data; the likeliest to route standard |
| f4-tags-export | CSV export endpoint with a round-trip test | backend/simple with a test pin question |
| f5-empty-state | empty-state panel on the project list | frontend/rote-to-simple control |
| f6-download-guard | path-traversal guard on video downloads | security has a pinned route (Sol); the largest possible routing swing |
| f7-projects-migration | schema version and migration of the projects file | data kind; migration reasoning is the likeliest standard or complex |

Two controls, five where a Jev label could plausibly move a task's lead
to Sol or Opus. On the rule arm the orchestrator's own labels decide, so
a lead change between arms is Jev disagreeing with the orchestrator, and
the record keeps both labels per task. The last two cover the kinds Stage
A could not: security and data, the two with the strongest routing
consequences.

## The OpenAI window is the binding constraint

Every run spends on the ChatGPT subscription whether or not Sol leads: the
requirements review and the final audit are Sol seats (about 28k and 43k
reported tokens on the Stage A live run), and a Sol lead adds the lead's
own context (about 117k per task on that run). A planned run of four to
six tasks multiplies the per-task part, so fourteen deep runs are several
million tokens on the OpenAI side, and Davis reports about 10% of the
weekly window left. So:

1. Ask Codex for one number before launch: what the ChatGPT window shows
   remaining. No analysis, no review.
2. Run the first pair alone (`--only f2-project-search`, the cheapest
   control) and read `usage.jsonl` for the OpenAI-side total per cell.
3. Extrapolate to fourteen cells. If the window will not hold the series,
   there are two orders, and Davis picked the second (2026-10-02):
   - pairs in this order, stopping where the window runs out: f6, f7,
     f3, f1, f4, f2, f5 (largest expected routing swing first, controls
     last);
   - `--arm rule` across all seven features first, then `--arm jev`. The
     rule arm alone is the reliability series (question 2) and needs no
     counterpart; every jev cell that follows completes a pair. The cost
     is that the two arms of a pair no longer start together, so vendor
     availability can differ between them; the manifest records each
     cell's start time and the comparison notes any seat change between
     the two.
   Say which order ran in the record rather than running until a seat
   goes dark.
4. If any seat becomes unavailable mid-series the running pair finishes
   or stops on its own limits, the next pair is not launched, and the
   series is reported as partial. An arm that ran with a seat missing is
   not comparable to one that did not.

Jev's own cost is negligible (about 1,500 tokens and a quarter of a cent
per cell) and comes from existing TypeSafe credit.

## Running it (from the IDE on the Mac)

    python3 tools/jev_stage_b/pairs.py prepare --packet /abs/packet.json --out /abs/stage-b
    python3 tools/jev_stage_b/pairs.py run --out /abs/stage-b --only f2-project-search
    python3 tools/jev_stage_b/pairs.py collect --out /abs/stage-b
    python3 tools/jev_stage_b/pairs.py run --out /abs/stage-b          # the rest, one pair at a time
    python3 tools/jev_stage_b/pairs.py collect --out /abs/stage-b

Rule arm first, then Jev (the order Davis chose if the window is short):

    python3 tools/jev_stage_b/pairs.py run --out /abs/stage-b --arm rule
    python3 tools/jev_stage_b/pairs.py collect --out /abs/stage-b       # reliability series readable now
    python3 tools/jev_stage_b/pairs.py run --out /abs/stage-b --arm jev
    python3 tools/jev_stage_b/pairs.py collect --out /abs/stage-b

`run` calls `quadratus.project_run.run_project` directly with the packet's
`RunLimits`, `max_tasks`, `SurveyConfig(recovery_tasks)` and, on the jev
arm, `decider="jev", decider_labels="all"`, because the CLI does not expose
limits. A task with a `text` instead of a goal still runs as a one-task
listed cell, the Stage A shape. A failed cell is a
result with an error, never a reason to stop its sibling. `collect` reads
`result.json`, `budget.json` and `invocations.jsonl` per cell and writes
`comparison.md` and `comparison.json`.

Needed in the IDE before `prepare`: the GameTape repo path and baseline
(1cd9264 or current main), `TYPESAFE_API_KEY` in the project's `.env`, the
capture profile and check command used on run `20261001T001902Z`, Codex's
window reading, the candidate SHA carrying `decider_labels` (the engine the cells run on), and Davis's yes on the seven goals or edits to them.

## After series b332951: the three fixes (Davis, 2026-10-03: fix and rerun)

The first series (ten cells executed, held before f6 and f7) was
access-confounded and every cell ended on the token threshold. Three
fixes, each a recorded task with its own Codex review, before the rerun:

- **The Claude write grant** (`T-claude-write-grant`). `CLAUDE_SPEC` had
  no `write_args`, so a Claude lead with writes launched in permissionMode
  default and `claude -p` denied every project Edit/Write; ten Opus lead
  calls spent their budgets on denials. A granted call now sends
  `--permission-mode acceptEdits` plus one exact `--allowedTools Bash(...)`
  rule per check the harness will run (and its `:*` prefix form); nothing
  else is allowed. A denied file tool on a granted call is now a
  `CapabilityUnavailable` stop, like a denied check, not a budget burn.
  Verified live on claude 2.1.288: default denies Write, acceptEdits
  writes, `Bash(python3 -V)` grants that command and denies `python3 -c`.
- **The per-call token reserve and ceiling** (`T-call-token-cap`). The
  2.5M threshold was post-return only and single agentic calls ran 0.5M
  to 3.4M. `RunLimits.reserve_tokens_per_call` refuses to start a call
  unless that much budget remains (`reported_token_reserve`);
  `max_tokens_per_call` names a call that reported more than it
  (`call_token_ceiling`, post-return, recorded in `oversized_calls`). The
  packet sets 500,000 and 1,500,000 under the unchanged 2.5M threshold.
  `lead_max_turns` is a packet field, null in this series: no CLI's capped
  envelope has been probed for a value.
- **Capability-aware comparability** (`T-runner-tool-denials`). The runner
  reads each lead call's denied file-tool calls and files written from
  the run's `trace.jsonl`; a lead denied its writes that wrote nothing,
  or an engine-saved `CapabilityUnavailable`, is a `capability-failure`
  series stop and makes the pair non-comparable, with the lead, task and
  invocation named. A ran cell with no lead trace to check is reported as
  such.

The rerun is the same seven features, same bounds otherwise, fresh
worktrees, a new series directory and a new freeze on the fixed SHA.

## Independent graders (Davis via Codex, 2026-10-03)

The engine's own completion claim is not the grade. Each feature gets a
frozen grader: a script in a directory outside the GameTape repository
(`graders.dir`, refused if it lies inside the project), one entry per
feature in `graders.tasks`, written before either arm runs. `prepare`
hashes every file in that directory and the capture profile into the
manifest; `run` re-hashes before every launch and refuses a change. After
each cell finishes, its graders run from the grader directory with
`STAGE_B_PROJECT` (the cell's worktree), `STAGE_B_ARM` and
`STAGE_B_RUN_DIR` in the environment; exit 0 is a pass, and the exit
code, output tail and time are recorded on the cell. `collect` reports
the grade per cell and per pair beside the engine's completion claim, so
a cell the engine called done and the grader failed shows both.

Graders are executable requirement checks: an HTTP test against the
feature's endpoint, a browser assertion through the existing UI test
runner, a fixture file through the migration, a traversal request against
the download route. They read the project; they never import the
builders' own tests as the verdict. Writing them is IDE work done before
`prepare`, and their hashes are part of the frozen packet record on #46.
`run` also records each vendor CLI's reported version and the engine SHA
on every launch. A browser grader that provokes an HTTP error on purpose
(the duplicate-name 400 in f3) names it as `expected_http`; the shared
collector excuses Chromium's resource diagnostic for exactly that URL and
status, only when the response was observed, and nothing else.

## Bound on the Mac (2026-10-03)

The committed packet is `tools/jev_stage_b/packet.stage-b.json`: GameTape
at `/Users/davisfox/Documents/GitHub/sports-video-tagger`, base `1cd9264`,
the Stage A base. The seven goals are rewritten against that tree so that
every requirement names the exact endpoint, DOM id, response shape or file
shape a grader checks; `packet.example.json` keeps the drafts. The check
is the project's pytest run under a separate interpreter outside both
repos (`/Users/davisfox/Documents/GitHub/stage-b/venv`, Flask, openpyxl,
pytest, Playwright), and the extra check is the five baseline Node UI
tests by name, since the engine refuses a glob. `engine_sha` is the one
placeholder: the launch step copies the packet into the series directory
with the engine checkout's HEAD filled in, and `prepare` reads that copy,
so the engine binding is the SHA Codex reviewed, never a hand-typed one.

**One grader argv per requirement.** `graders.tasks[<feature>]` lists one
`pytest -k R<n>` invocation per requirement, so a cell's grade is
requirements passed over requirements total, and `comparison.md` shows
which requirement failed, not only how many. The graders live in
`tools/jev_stage_b/graders/` (the engine repo, outside the GameTape repo):
`conftest.py` imports the cell's `app.py` fresh with its data directories
on a temp dir, or serves it through the grader-owned `serve.py` on a free
loopback port and drives real Chromium through Playwright, recording
console errors and page errors. The last requirement of every feature
runs the baseline tests frozen under `graders/baseline/tests` (copied from
`1cd9264`) against the cell's `app.py`, `templates/` and `static/` in a
temp tree, so the builders' own edits to `tests/` never decide a grade.
Run against the untouched baseline before freezing, every feature
requirement fails and every regression requirement passes, and no grader
errors on its fixtures; that is the finite verification the graders get
before either arm runs.

**Per-arm capture profiles.** Both arms of a pair run at once and a
capture profile pins one loopback port, so the packet names
`capture_profiles` (one per arm, ports 52055 and 52056, both hashed into
the frozen inputs) and the launcher picks the cell's arm's profile.

**One run per series, and a launch that never finished is never
retried.** `run` takes `run.lock` in the series directory (pid and time)
and refuses while another holds it; a stale lock is removed by hand after
reading the manifest. Each cell is written to the manifest as `running`
before its launch and as `ran` or `failed` with `finished_at` after, with
atomic manifest writes; a later `run` marks any cell still `running` as
`interrupted`, keeps it, never launches it again, and `collect` reports
the pair as not comparable.

## Evidence and reporting

Per cell: `result.json` (with its `survey` section: failures, causes,
recovered or open, hypotheses, repeat stops), `budget.json`,
`usage.jsonl`, `invocations.jsonl`, `changes.diff`, `report.md`, captures
and design verdicts under the cell's `.quadratus/runs/<id>`. Series:
`manifest.json` (packet digest, order, states, timings) and
`comparison.md`. The report separates the five route-sensitive pairs from
the two controls, gives both arms' task counts, closed-clean counts,
failed checks, recovery used and hypotheses logged, every lead that ran,
calls, tokens and seconds, and the per-pair deltas. Because the two arms
of a planned feature need not decompose alike (the jev arm's orchestrator
prompt lacks the KIND instruction), the comparison also lists each
engine task with the lead that was invoked and the decider's labels for
it, so lead attribution is read per task and `lead_changed` at feature
level is a summary, not the finding. The reliability
reading lists each distinct failure cause across all cells with the task
it hit and whether a repair recovered it. Jev decision tokens are reported apart from the
work. Recorded on `#46` as T-jev-stage-b with the authorization comment
and the terminal receipt.

## Stop rules (enforced by the runner, 2026-10-03)

Any cell's budget stop is that cell's result, not a series stop. After
each batch `run` reads the finished cells and stops the series before the
next launch on: an orchestrator call answered by a model other than the
chain's primary (a seat fallback) or a cell that raised
`OrchestratorUnavailable`; unknown usage on any cell; Jev answering as a
model other than the packet's `jev_model` (`jev-1.13.0`); a decider
refusal naming the key or an auth status; or any cell that raised before
the engine produced a result. The stop is written to `manifest.json`
(`stopped`: reason, the cell, the time) and a later `run` refuses until
an operator passes `--override-stop "<reason>"`, which is recorded under
`overrides`. Before any launch, `run` re-hashes the manifest's packet
against the digest `prepare` wrote and refuses a changed packet, and it
reads the importable engine's checkout: a dirty tree or a HEAD other than
the packet's `engine_sha` is refused, and the SHA is recorded on the
manifest and on every cell. These were plan text only until Codex and
the Mac session read the runner (#35, 2026-10-03). Nothing is retried,
re-labelled or replaced after seeing a result.

`collect` marks each pair `comparable` or not, with reasons: an arm that
did not run, unknown usage, different engine SHAs, different orchestrator
seats between the arms, or Jev refusals on the jev arm. It also records
the gap between the two arms' start times, which is hours by design when
the rule arm runs first; a non-comparable pair is still reported.
