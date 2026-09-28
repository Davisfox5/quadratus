# Coordinated reliability repair — cloud implementation report

## TL;DR

- All six problems are fixed and committed locally. **634 tests pass, Ruff clean**
  (572 baseline + 62 new regressions). GameTape's supplied tree still passes its
  **79** tests, untouched.
- Every regression replays real trial evidence, not a paraphrase. Each fix was
  verified old-vs-new against that evidence; the proof run is reproducible.
- **No live acceptance run was performed.** Three of four vendor CLIs are absent
  from this container, so the live step is deferred to the Mac as the handoff
  directs. Instructions are in "Live acceptance" below.
- No role, routing, permission or transport policy was changed. Fable/Astra,
  the Opus/Sol/Grok seats, the Sol testing pin, the Sol+Opus review pair,
  worker-family and escalation rules, restricted editing, project/snapshot
  grants, subscription-CLI defaults and Grok fix `022de34` are all as they were.

Repair base: `e40e18e75c02c651d31fdbd3d261681d599049f5` (`9ac8bac` confirmed
ancestor; handoff `sha256sum -c` 20/20 OK).

---

## What changed

Three new modules, and surgical edits elsewhere. Nothing was refactored that a
fix did not require.

| Module | Why it is new rather than an edit |
| --- | --- |
| `quadratus/taskmeta.py` | Reply parsing had no home; it was inlined in `session.py` and read only the first line. |
| `quadratus/scope.py` | Task bounds did not exist at all, so there was nothing for an observed change to disagree with. |
| `quadratus/delegation.py` | Origin, unknown usage and native children are a different question from the API-price counterfactual in `usage.py`, and merging them is what produced a number that was neither a budget nor a bill. |

### 1. Task metadata and control messages

`session.py:_parse_kind` inspected `lines[0]` only. The trial's Fable reply
prefaced `KIND: test rote` with a paragraph, so the label was never seen and
the reply fell through to `general`/`simple` — the Sol testing pin silently
dropped.

- `taskmeta.parse_metadata` scans a bounded preface window (6 lines) instead.
- Every result carries `confidence` — `labelled`, `degraded` (a kind that does
  not exist), or `defaulted` (no label). A defaulted route is no longer
  indistinguishable from a stated one; it is noted to the progress channel and
  recorded in the task's own memory. `TaskSpec` carries
  `metadata_confidence`/`metadata_notes`.
- Contradictory `KIND:` lines raise `AmbiguousMetadata` and buy exactly one
  bounded correction re-prompt, then `RunStalled`.
- An **unrecognised** kind still degrades to `general` rather than failing the
  round. That was a settled decision here ("a mislabelled task costs a routing
  preference; a rejected round costs the task") with an existing test; the
  change is that it is now loud instead of silent.
- `taskmeta.parse_control` recognises ASK, FETCH, CONSULT, WORKER and DONE
  through the same bounded scan, so a prefaced `ASK:` reaches the operator
  instead of becoming a task description containing the word ASK. `DONE` is
  matched as a whole line, so a task beginning "DONE-criteria:" stays a task.

### 2. Worker failure recovery

`WorkerPool.commission` records the failed fingerprint and re-raises. The
single-worker path in `_draft_with_channels` did not catch it, so Luna's one
malformed answer aborted the run before Opus could act. `commission_many`
already reported errors as outcomes; the two paths now agree.

- The commission is wrapped. Transport, response-format and patch-validation
  failures come back to the lead as an actionable outcome plus
  `_WORKER_RECOVERY`, which names the moves that are actually legal: rewrite,
  re-send to a different worker, mark demanding, do it itself, or close
  incomplete.
- `RepeatedFailure` and `FanOutExceeded` are reported to the lead the same way
  rather than crashing, and both still fire.
- **Nothing was weakened.** Patch validation is untouched; the corrupt diff is
  still rejected by `git apply --check`. Restricted tools were not widened.
  Failed fingerprints are still recorded and the budget is still charged.
- New `SessionConfig.max_worker_failures` (default 4) stalls a lead that only
  fails workers, so recovery cannot become an unbounded retry budget.

### 3. Task scope

`TaskScope` declares permitted paths, intended result, acceptance criteria and
a change bound. `render()` states them in the lead's prompt; `assess()` measures
the actual diff after the work.

- An out-of-scope **path** is blocking and becomes an open finding — that is the
  bound the operator actually set.
- A size overrun past 1.5× the stated bound is loud and advisory.
- **Work is never reverted.** The trial's over-wide change was also the only
  work that existed. Partial work and the operator's own edits stay exactly
  where they are; the report is the response.
- An unscoped task records that it was unscoped rather than passing silently.

### 4. Timeout and cancellation recovery

`_generate_once` retried `TimeoutError` unconditionally, replaying a writing
prompt against a tree the timed-out call may already have changed.

- `LLMProvider._replay_would_be_unsafe` raises `PartialWorkSuspected` when a
  call that **could write** stops in a way that leaves the outcome unknown
  (timeout, cancellation, interruption). A read-only timeout and a rate limit
  are still genuine retries and still retry.
- `Session._edit` catches it, inspects the tree against a pre-call capture, and
  raises `PartialWorkStopped` carrying a JSON-shaped record of what was already
  written. Nothing is rolled back. Where no pre-call capture exists, the state
  is reported as **unknown**, not none.
- `cli_providers._launch` replaces `subprocess.run` as the single execution
  seam: the CLI gets its own process group, and a timeout sends TERM then KILL
  to the **group**, so the vendor's own children do not survive holding the
  working tree. Partial stdout/stderr is preserved on the raised
  `TimeoutExpired`. Degrades to single-process behaviour off POSIX.
- `project_run` persists `in_flight` into `result.json` and the report, so a
  stopped run hands over a resumable picture.

### 5. Native delegation and usage

Sol used the Codex CLI's `spawn_agent` to create a second Sol. That child never
passed through `WorkerPool`, its errand tree or any budget, and its 135,105
tokens appeared in no total Quadratus reported.

- `delegation.Origin` separates `seat`, `worker`, `native` and `auxiliary`.
  Vendor-internal Haiku rows are auxiliary, not evidence that Quadratus
  dispatched a Haiku worker.
- `InvocationEvent` records task, role, requested vs resolved model, **selected
  vs actually invoked**, duration, outcome, attempt number, session id, and
  whether the failure was in transport or after the vendor returned text.
- `input_tokens=None` means unknown. `Fleet._generate` now records failed calls
  too, so a cancelled invocation that burned a window is present as unknown
  rather than absent (absent reads as zero, and zero is a claim).
- `cli_providers._extract_native_children` reads spawn events from a CLI's own
  stream. Children are keyed by session id and folded at their **maximum**
  reading, never summed — vendor streams restate cumulative totals, so summing
  updates multiplies the figure. `reconcile()` also refuses to add a child whose
  session id matches a call Quadratus itself dispatched.
- `DelegationLedger.blind_spots` states plainly what the harness cannot observe
  or bound. No model authority was changed and no senior capability was removed.
- Subscription usage stays separate from the API-price counterfactual in
  `usage.py`; cache semantics there are untouched. The two reports are written
  to different files for the same reason.

### 6. Verification and reporting

- `Session._check` captures contents before and after and reports **which files**
  added, modified or removed. Same-tree verification is unchanged, and
  unexpected new source files are still caught — narrowing the fingerprint to
  tracked files would have hidden the trial's `data/recordings/` failure
  instead of merely making it unhelpful.
- `_revision_prompt` now asks `_revision_delivery()`, which reads the **grant**,
  not merely whether a project is selected. A read-only run is told so, and told
  explicitly that a review concluding its subject is not ready is a complete
  and successful review.
- `_review_subject_note` keeps "this review is wrong" apart from "the thing
  reviewed is wrong" on review-kind tasks. The probe already came out right;
  this pins that outcome deliberately rather than by luck.
- `runtime._relativise_snapshot_paths` rewrites absolute paths into the
  disposable review copy while it still exists, so exported findings survive its
  deletion. Reviewers are also asked up front for project-relative citations.

---

## Regression evidence

62 new tests: 60 in `tests/test_reliability_repair.py`, 2 end-to-end in
`tests/test_project_workflow.py` (real subprocesses, scripted vendors).

They replay the actual fixtures: `task-metadata-response.txt`,
`worker-first-valid-response.txt`, `worker-second-rejected-response.txt`,
`BULK_TAGGING-first-applied.md`, `native-delegation-evidence.json` and
`usage-summary.json`.

**Old-vs-new verification.** Each fix was checked to change behaviour on the
real evidence, not merely to pass a test written after it:

| Fix | Old | New |
| --- | --- | --- |
| 1 | `kind='general' difficulty='simple'` — pin lost | `kind='test' difficulty='rote'` |
| 2 | `fullmatch` fails → `ProviderError` → **run aborts** | patch still rejected; failure returned to lead |
| 4 | 3 attempts, writing prompt replayed | 1 attempt + `PartialWorkSuspected`; read-only still retries 3× |
| 5 | 209,549 tokens (parent only) | 209,549 controlled + 135,105 native = 344,654 known minimum |
| 6a | "Project source changed …" | names `data/recordings/c.webm`, 1 added / 1 modified |
| 6c | `/tmp/quadratus-review-abc/app.py line 503` | `app.py line 503` |

The run-abort fix was additionally verified by reverting only that hunk: both
worker-recovery tests fail with exactly the `ProviderError` that ended the
trial, and pass once restored.

The corrupt patch is still rejected after the fix —
`git apply --check` returns 128, `corrupt patch at line 139`, matching
`worker-failure-proof.json`. Its hunk header claims `@@ -1,87 +1,127 @@` while
supplying no context or removal lines at all, which is why stripping the prose
does not make it applicable.

---

## Test results

| Suite | Environment | Result |
| --- | --- | --- |
| Quadratus (baseline, before changes) | `.venv` (Python 3.11.15, Linux) | 572 passed |
| Quadratus (after changes) | same | **634 passed** |
| Ruff | same | **All checks passed** |
| GameTape (supplied `f93d8b1` tree) | its own venv, separate directory | **79 passed** |
| GameTape `node --check static/js/app.js` | — | OK |

The two projects were kept in separate environments and directories; neither
was used as evidence for the other.

**One platform difference.** The container ships Chromium build 1194, and a
fresh `pip install -e '.[dev]'` resolves playwright 1.62.0, which wants build
1234 — 12 browser tests error on the missing executable. Pinning **playwright
1.56.0 in the venv only** fixes it; `pyproject.toml` was not changed, since this
is a property of this container rather than of the project. Both the 572
baseline and the 634 result above are with that pin, so they are comparable.

---

## Live acceptance: NOT performed

`codex`, `gemini` and `grok` are absent from this container (only `claude` is
present), so a live multi-provider run is not possible here. Per the handoff,
this is deferred rather than a blocker.

To run it on the Mac after importing the patch series:

```sh
# 1. fresh isolated worktree on the GameTape baseline; existing trial
#    worktrees and their evidence are untouched
git -C <gametape> worktree add ../gametape-acceptance f93d8b1
cd ../gametape-acceptance
/tmp/gametape-trial-env/bin/python -m pytest -q      # expect 79 passed

# 2. the natural-language goal, unmodified. No model names, no forced kind.
quadratus "$(cat docs/handoffs/reliability-repair/evidence/routing-trial/goal.txt)" \
  --project $(pwd) --allow-writes --check "/tmp/gametape-trial-env/bin/python -m pytest -q"
```

Expected telemetry in `.quadratus/runs/<id>/`:

- `invocations.jsonl` — one line per invocation and retry, each with `origin`,
  `selected`, `invoked`, `outcome`, `seconds`, and token counts or `null`.
- `delegation.md` — totals split into Quadratus-dispatched, native children,
  and unknown; plus a "selected but never invoked" section and the blind-spot
  list.
- `result.json` — now also carries `in_flight`, `delegation` and `scope_reports`.

What to check, and what not to conclude:

- Report **actual invocations** from `invocations.jsonl`, never the roster.
  A model appearing under "selected but never invoked" is not coverage.
- A worker returning a bad patch should now appear as a recovered errand, with
  the run continuing, rather than as a terminal error.
- Stop on a repeated known failure and keep the evidence rather than retrying
  into a spent window.

---

## What was actually exercised here, and what was not

**Exercised in the cloud:** deterministic local fixtures only. Real `git apply`
against real temporary repositories, real subprocess process-group kills, real
concurrent `WorkerPool` fan-out, and the scripted-vendor end-to-end path in
`test_project_workflow.py`.

**Not exercised:** any live vendor call. No subscription tokens were spent by
this session. No model was invoked by Quadratus at any point.

**Still uncovered by any evidence**, cloud or trial: Haiku, Sonnet, Terra and
Grok worker/expert seats; in-family escalation; the Astra orchestrator fallback;
and the security excursion. The trials invoked Fable, Opus, Sol and Luna only.

---

## Limitations

- Native-child detection reads the shapes seen in the captured Codex stream
  (`spawn_agent`, `child_session`). A vendor that reports delegation differently
  will not be detected, and `DelegationLedger` cannot prove the absence of
  delegation it never saw. This is stated in the report rather than implied.
- The harness still cannot *prevent* native delegation or bound its budget. It
  records it. Fixing that would require changing model authority, which the
  constraints rule out and which would be the wrong trade anyway.
- Scope bounds only bind when something sets them. `SessionConfig.default_scope`
  is `None` by default, so existing callers are unchanged; the orchestrator does
  not yet emit per-task scopes, which is the natural next step.
- `count_change_lines` counts a moved line twice, overstating a pure reshuffle.
- The `_launch` process-group kill is POSIX-only and degrades silently
  elsewhere.
- The 1.5× overrun tolerance and the 6-line preface window are judgement calls
  sized to the observed failures, not measured thresholds.

---

## Artifact paths

- Repair base: `e40e18e75c02c651d31fdbd3d261681d599049f5`
- Branch: `claude/quadratus-reliability-repair-pkrenc`
- New source: `quadratus/taskmeta.py`, `quadratus/scope.py`,
  `quadratus/delegation.py`
- Modified source: `quadratus/session.py`, `quadratus/runtime.py`,
  `quadratus/providers.py`, `quadratus/cli_providers.py`,
  `quadratus/project_run.py`
- New tests: `tests/test_reliability_repair.py`
- Modified tests: `tests/test_project_workflow.py`,
  `tests/test_cli_providers.py`, `tests/test_refusal.py` (the last two only
  repoint their fake from `subprocess.run` to the new `_launch` seam; every
  assertion is unchanged)
- Evidence consumed, unmodified: `docs/handoffs/reliability-repair/evidence/`

Export the repair with:

```sh
git format-patch --binary --stdout e40e18e..HEAD
```
