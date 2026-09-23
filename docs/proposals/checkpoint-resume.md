# Proposal: crash recovery and authorized budget extension for a project run

Proposal only; the core change is sensitive-path. Source: "Core follow-up for Claude",
`docs/harness-canary/fixture-v2/REVIEW.md`. Citations: `codex/blind-worker-acceptance` at `36ab9b6`.

## 1. TL;DR

- **Two operations, kept apart.** *Crash recovery* (`--resume`) continues an
  interrupted run under the same limits and hashes. *Budget extension*
  (`--extend`) is a separate, recorded operator ruling that raises one named
  limit so a run stopped on tokens, calls or wall time can continue.
- **Both read one new file, `checkpoint.json`,** rewritten atomically at points
  the loop already passes; an extension adds `extension.json`. No event system.
- **Both refuse before spending anything** if the source, policy or runtime hash
  differs, and say which.
- **Both keep what was finished:** closed tasks, ledger entries (verbatim, never
  re-summarised), gate receipts, rulings, open findings and a review in
  progress at its last completed step. Those reviewers are not called again,
  and a restored review is labelled restored, never fresh.
- **Spend is never reset.** Recovery counts it against the same limits; an
  extension counts it against the new ceiling and puts an entry in the ledger.
- **A call whose outcome is unknown is never re-sent,** and an extension never
  releases one. Scope stops are never replayed.
- **A security excursion is never resumed partway.** Resume refuses and names
  the recovery paths; a restart needs the operator's choice, a clean baseline,
  no pending call and every consumed call on record.
- **This proposal authorizes no spend** and raises no current limit.
- Today's records lack a runtime hash, ledger structure, review state and in-flight
  calls (section 2). The checkpoint adds those.

## 2. What exists today

A run directory is `<state>/runs/<UTC stamp>-<8 hex>` (`quadratus/project_run.py:94-96`),
created under the project lock (`project_run.py:37-49`, `81`). Records written:

| Record | When written | Content | Citation |
|---|---|---|---|
| `artifacts/` | every `put` | content-addressed raw text, append-only, never removed | `artifacts.py:65-72`, `120-141`, `project_run.py:104` |
| `usage.jsonl` | after each successful call | model, tokens, cost, measured flag | `usage.py:112-145`, `project_run.py:105` |
| `invocations.jsonl` | at the acceptance boundary after each call | `InvocationEvent`: task, role, origin, outcome, tokens | `delegation.py:148-191`, `328-337`, `project_run.py:106` |
| `native-children.jsonl` | when a vendor-native child is observed | observed children | `delegation.py:350` |
| `budget.json` | every reserve and finish (bounded runs only) | reserved attempts, tokens, cost, `in_flight`, `stop_reason`, elapsed | `run_budget.py:92-123`, `project_run.py:107` |
| `budget-responses/attempt-N.txt` | when a reply lands after a stop latched | the captured reply, mode 0600 | `run_budget.py:191-198` |
| `policy-plan.json` | once, before the session | resolved policy with `policy_hash`, `library_digest`, `hash` | `project_run.py:116-118`, `policy.py:305-313` |
| `in-flight.json` | only in the `except` handler | partial-edit inspection plus `_active_call` | `project_run.py:142-150`, `session.py:1054-1064` |
| `changes.diff`, `ledger.md`, `report.md`, `delegation.md` | once, at the end | rendered text | `project_run.py:189-192`, `210` |
| `result.json` | once, at the end | completion, checks with gate receipts, `source_fingerprint`, `policy_plans`, budget, scope reports | `project_run.py:193-209` |

Not persisted anywhere:
- **Orchestration state.** Ledger entries and rulings live in memory
  (`ledger.py:100-111`, `119-147`); only rendered `ledger.md` reaches disk, at
  the end. So do `history`, `open_findings`, `checks` and the rotation counter
  (`session.py:507-528`). RunStalled's `previous_description` is a local
  (`session.py:1600-1615`); task ids derive from `len(self.history)` (`session.py:1544`).
- **Open task state.** `TaskMemory` turns are in memory (`memory.py:95-114`).
  Drafts, reviews and revisions are artifacts (`session.py:1149`, `1181`,
  `1216`, `1232`), but not the step reached, the BLOCKING set, the fix-cycle
  count or unresolved verdicts; recheck verdicts are not kept at all
  (`session.py:1843-1844`). `_task_before` (`session.py:1049`) and worker
  `RepeatedFailure` fingerprints (`workers.py:406`, `495`) are memory-only.
- **In-flight calls.** `invocations.jsonl` is written after the call: "This is
  not a write-ahead journal" (`delegation.py:80-82`). `in-flight.json` needs the
  `except` path, which a wall-ceiling kill skips (`isolated_run.py:91-95`). Only
  `budget.json`'s `in_flight` count survives a hard kill, in bounded runs only.
- **Hashes.** The source fingerprint (`project.py:84-88`) is recorded only at
  the end (`project_run.py:196`); a policy hash exists (`policy.py:307`, `313`).
  **No runtime hash is recorded**; runtime commits are hand-written in
  `docs/harness-canary/RESULT.md:18`.
- **Failed-call usage.** The meter skips failed calls (`usage.py:147-164`;
  `docs/GAMETAPE_TRIAL.md:50-53`). `RunBudget` starts its clock and counters
  from zero and has no restore (`run_budget.py:77-90`).

## 3. The contract

Two operations share the preconditions and the restore:

- **(a) Crash recovery**, `quadratus --resume <run-dir>`: same limits, same
  hashes, continues an interrupted run. A latched budget stop refuses it.
- **(b) Authorized budget extension**, `quadratus --extend <run-dir> --limit
  <name>=+<amount> --authorized-by <who> --source <where>`, then `--resume`.
  The native GameTape sequence of 2026-09-22 (`docs/harness-canary/
  gametape-native-20260922/README.md` and its `attempts.json` at b96994d,
  PR #24) is the case: twelve supervised continuations, eleven stopped on
  reported tokens and one on scope, 32 transport calls, 9,049,489 reported
  primary tokens, the independent Opus review never invoked. Recovery alone
  keeps that evidence but never finishes the interrupted workflow. That
  sequence is supervised continuation, not twelve independent trials.

**Preconditions.** Both refuse, naming each mismatch, unless all of these hold:

1. `source_fingerprint` now equals the checkpoint's (taken at checkpoint time).
2. `policy_hash`: `load_policy` plus `resolve` reproduce `policy-plan.json` `hash`.
3. `runtime_hash`: sha256 over the installed `quadratus` files, as `load_library`
   hashes the harness library (`policy.py:80-92`), plus the `QUADRATUS_ALIAS_*`
   and `QUADRATUS_CLI_ARGS_*` overrides, which change who answers a seat.
4. `run_args_hash`: goal, project, `allow_writes`, mode, `max_tasks`, check,
   scope, forbid and the original `run_limits`. A different goal is a new run.
   The effective limits are the original ones plus valid extension records;
   any other limit change refuses.
5. No `pending_call` is set, and `budget.json` `in_flight` is 0 (section 5).
6. The project lock is free (`project_run.py:37-49`).

**Restored.**
- Closed tasks: `history` and ledger entries, appended in their original order
  through `Ledger.append` with the stored fields verbatim, plus rulings and
  invariants. Task ids continue from the restored count.
- Gate receipts (`session.py:1893-1895`), policy plans, scope reports, open findings.
- The pending review state of one open non-security task, at its last completed step.
- Spend: `RunBudget` is seeded from `budget.json` (attempts, tokens, cost,
  unknown-usage counts, preserved responses, elapsed seconds, so wall time
  continues). `usage.jsonl` and `invocations.jsonl` keep appending and are
  loaded for the final report, so totals cover every segment.
- `previous_description` and worker fingerprints, so RunStalled and RepeatedFailure hold.

**Never done.**
- Re-issuing a call whose result is unknown. That includes a writing call cut
  off partway (`providers.py:148-160`, `364-380`) and any call recorded as
  `pending_call` with no matching outcome.
- Presenting a restored or captured response as a fresh review. Restored notes
  reach the lead under the same anonymous labels, but the checkpoint, report and
  `result.json` mark them `restored` with artifact id and origin attempt, and
  no new `InvocationEvent` is emitted. A `budget-responses/` reply was never
  accepted into task state and is never fed back in.
- Re-summarising: the ledger reloads from structured entries, never from
  `ledger.md`, and no closed task gets a second close-out.
- Resetting spend: limits are never replaced wholesale over spent ones.

**The extension record.** `--extend` writes `<run-dir>/extension.json`, an
append-only list with one entry per extension: `original_run_id` (the run
directory name), `checkpoint_hash` (sha256 of `checkpoint.json` at that moment),
`limit` (`max_reported_tokens`, `max_calls` or `wall_seconds`,
`run_budget.py:48-52`), `old_value`, `new_value`, `authorized_by`, `source`
(where the ruling was given) and `recorded_at`. Rules:

- It applies only to the checkpoint it names; a stale one refuses.
- It raises the named ceiling by the stated amount and nothing else. Spend
  already made (`budget.json` totals) counts against the new ceiling. The
  latched `stop_reason` clears only if it matches that limit and the new
  ceiling is above what was spent.
- It never clears `unknown_usage`, `uncontrolled_native_delegation` or
  `api_cost_threshold` (`run_budget.py:166-188`), never releases a pending
  call and never replays a scope stop. `max_cost_usd` is not extendable in
  the first implementation; making it so is a separate ruling from Davis.
- It stays inside any outer batch allowance. When the run was admitted
  against a canary allowance record (`tools/acceptance/allowance.py`), the
  extension refuses if the new ceiling, summed with the batch's other
  consumed slots, would exceed `max_reported_tokens_batch`; raising the outer
  ceiling needs its own recorded ruling on that record, never an extension
  on one run.
- The ledger gets an appended `Ledger.append` entry (task id `extension-<n>`,
  author `authorized_by`, summary naming the limit and both values, reasoning
  quoting `source`, which `ledger.py:130-136` requires). Nothing earlier changes.
- Proposing this mechanism is not authority to spend or to raise any current
  limit. Each extension needs its own operator ruling.

**Stopped outputs are kept.** Before continuing, either operation moves
`report.md`, `result.json`, `ledger.md`, `changes.diff`, `delegation.md` and
`in-flight.json` into `stops/<n>/`: each stop stays a terminal observation.

## 4. Where the state lives

One file, `<run-dir>/checkpoint.json`, written as a temp file plus `replace`,
the same pattern `RunBudget._persist` already uses (`run_budget.py:114-120`).
Large text goes into the existing `ArtifactStore`, and the checkpoint holds only ids.

Fields: `schema_version`, `seq`, `written_at`, `source_fingerprint`, `policy_hash`,
`runtime_hash`, `run_args_hash`, `stop_reason`, `resumes` (prior stops), `ledger`
(entries as stored, with ref ids), `rulings`, `invariants`, `history_ids`, `checks`,
`open_findings`, `scope_reports`, `policy_plans`, `previous_description`,
`rotation`, `worker_failed`, `open_task` (below) and `pending_call`.

`open_task`: `spec` (task id, description, kind, complexity, needs, scope,
lead), `work_class`, `phase` (`declared`, `drafted`, `reviewed`, `revised`,
`rechecked`, `gated`), `baseline` (path to a copy of `_task_before` in
`baselines/<task_id>/`), `turns` (a role plus an artifact id for each),
`draft_ref`, `reviews` (peer, label, artifact id, blocking flag),
`revision_ref`, `fix_cycles`, `unresolved` (peer, verdict artifact id),
`gate_fixes_used`, `tickets` (budget tickets the task consumed) and `restored`.

`pending_call`: task, role, model, `allow_writes`, prompt artifact id and
budget ticket. It is set before dispatch and cleared after the acceptance boundary.

Write points, all at existing lines:
| Point | Citation |
|---|---|
| Run start, after the policy plan (hashes, args) | `project_run.py:118` |
| Task declared, before dispatch | `session.py:1614-1620` |
| Baseline captured | `session.py:1049-1051` |
| Before and after each model call (`pending_call`) | `session.py:543-551`, `567` |
| Draft, each review, each revision kept | `session.py:1149`, `1181`, `1216`, `1232` |
| Recheck result known; gate result appended | `session.py:1220-1222`, `1234-1238`, `1893` |
| Task closed and absorbed | `session.py:1161-1162`, `1257-1258`, `1334-1335` |
| ASK ruling recorded; excursion opened (marker only) | `session.py:1474`, `1277` |
| Final records | `project_run.py:189-210` |

## 5. Failure modes

- **Partially written checkpoint.** The atomic replace keeps the previous one.
  An unparseable file or unknown `schema_version` refuses; nothing is rebuilt
  from `ledger.md`. An artifact written just before a crash is a harmless orphan.
- **Hash mismatch.** Refuse, listing every differing hash with both values.
  No override flag: a resume on a different tree is a different run. An
  extension naming an older checkpoint hash is refused the same way.
- **Ambiguous in-flight call.** `pending_call` set, or `budget.json` `in_flight`
  above 0: refuse, naming the call, model and whether it could write.
  `--release-call <ticket>` records it as consumed with unknown usage and
  restarts its phase only if the tree matches the phase baseline. In a bounded
  run unknown usage latches `unknown_usage` (`run_budget.py:166-168`) and
  REVIEW.md stops admission on it, so the run stays refused. Fail-closed on
  purpose; an extension does not change it.
- **After a scope stop.** `PartialWorkStopped` (`session.py:552-558`) left
  out-of-scope or oversized edits on disk. Replaying would be the "second,
  different edit" `providers.py:364-380` prevents. Resume refuses and names
  the scope report, with or without an extension (open question 1).
- **After a token, call or wall stop.** Recovery refuses with the spent totals
  and points to `--extend`. With a valid extension for that limit, the run
  continues from the checkpoint with all spend counted.
- **After RunStalled.** Both refuse; the remedy is the operator's (`session.py:137-148`).
- **Inside a security excursion.** Never restore it. CLAUDE.md's memory-scope
  rule wipes a brain-trust member's working memory when its task closes, so an
  excursion's state is not meant to outlive it. The excursion closes
  unconditionally (`session.py:1336-1341`), and its seat is recomputed rather
  than held (`session.py:1277`; CLAUDE.md, "lapses by recomputation"). A
  half-run excursion would carry a stale seat and a draft whose verdict hash
  binds the old source (`session.py:1343-1350`). A matching source baseline
  alone does not show that replay has no external effects: the draft is an
  editing call (`session.py:1286-1291`), and security work may act outside the
  tree. So resume refuses by default and names the recovery paths: a fresh run
  for the task, or an explicit `--restart-excursion <task>`. That flag is
  accepted only if (1) the tree matches the task baseline, (2) no invocation
  is pending for the task (`pending_call` empty, `budget.json` `in_flight` 0),
  and (3) every ticket in `open_task.tickets` appears in `invocations.jsonl`
  and is counted in `budget.json`. Otherwise it refuses.

## 6. Test plan
Offline only, with fakes for `invoke`, `fleet_type`, `session_factory` (`project_run.py:91-92`).

| Test | Asserts |
|---|---|
| `test_checkpoint_roundtrip_is_verbatim` | save then load gives identical ledger entries, rulings and receipts |
| `test_checkpoint_write_is_atomic` | a failure partway through a write leaves the previous checkpoint readable |
| `test_resume_refuses_on_hash_mismatch` | parametrised over source, policy, runtime (digest or alias override) and run args; each names its hash; no call is made |
| `test_resume_keeps_closed_tasks_and_task_ids` | two closed tasks survive, the next id is `t3`, and no close-out runs again |
| `test_resume_never_resummarises_ledger` | the ledger rendered after resume equals the original rendering plus the new entries |
| `test_resume_restores_pending_review_without_reinvoking_reviewers` | the fake records zero reviewer calls, and the lead gets the stored notes |
| `test_restored_review_is_labelled_restored` | report and `result.json` mark it restored with its artifact id; no new collaborator event; no prompt contains a `budget-responses/` file |
| `test_resume_counts_prior_usage` | the seeded budget reports original plus new tokens and attempts; elapsed time continues |
| `test_resume_refuses_ambiguous_call` | `pending_call` set or `in_flight` 1: refuses, names the call, zero invocations |
| `test_release_call_consumes_slot_and_requires_clean_tree` | a released call stays counted, and a changed tree still refuses |
| `test_resume_refuses_after_token_stop` | without an extension, a latched stop refuses before any reserve |
| `test_extension_continues_with_spend_counted` | tokens spent before the stop count against the new ceiling |
| `test_extension_is_scoped` | an older checkpoint hash refuses; only the named limit moves, `run_args_hash` holds |
| `test_extension_appends_ledger_entry` | one `extension-<n>` entry is added; earlier entries render unchanged |
| `test_extension_stays_fail_closed` | a pending call, an `unknown_usage` stop or a scope stop still refuses; zero invocations |
| `test_resume_refuses_after_scope_stop` | names the scope report and leaves the tree untouched |
| `test_resume_keeps_loop_guards` | a repeated description raises RunStalled; a repeated errand on the same model raises RepeatedFailure |
| `test_security_excursion_default_refuses_naming_paths` | resume without the flag refuses and lists both recovery paths |
| `test_security_restart_requires_all_three_conditions` | changed tree, pending call or unrecorded consumed call each refuse |
| `test_security_restart_does_not_reuse_draft` | an allowed restart re-declares from `spec`; the old draft is not reused |
| `test_prior_stop_archived_and_lock_held` | `stops/1/report.md` and `result.json` exist unchanged; a concurrent run is refused |

## 7. Implementation sequencing

Each slice lands with its own tests; behaviour is unchanged until slice 6.
1. `quadratus/checkpoint.py` (new): dataclass, atomic save, validated load,
   `runtime_hash` and `run_args_hash`. Not a sensitive path.
2. Loaders: `RunBudget.restore` (`run_budget.py`), `UsageMeter.load` (`usage.py`),
   `DelegationLedger.load` (`delegation.py`, **sensitive**).
3. Closed-state export and import, with the close and ASK write points.
   `session.py` (**sensitive**), `workers.py`.
4. `pending_call` around `_invoke_model`; recheck verdicts kept as artifacts.
   `session.py` (**sensitive**).
5. Open-task restore by phase, baseline copy, excursion refusal and
   `--restart-excursion`. `session.py` (**sensitive**).
6. `resume_project`, `stops/<n>/` archiving, lock, Fleet with the restored budget,
   `--resume`/`--release-call`. `project_run.py`, `runtime.py` (**sensitive**), `cli.py`.
7. Budget extension, its own slice after recovery: `extension.json` writer and
   validator, the ceiling raise and latch rule, the ledger entry, `--extend`.
   `run_budget.py`, `project_run.py`, `session.py` (**sensitive**), `cli.py`.

## 8. Open questions for Davis
The earlier ambiguous-call question is settled: fail-closed, and budget stops use `--extend`.
1. **Scope stop.** Refuse outright (proposed), or offer "close this task as
   failed and let the orchestrator decide", which needs one close-out call?
2. **Runtime hash strictness.** An exact package digest (proposed) blocks resume
   across any Quadratus fix, even the one that motivated it. Accept that, or
   allow a recorded, named override?
3. **Extension scope.** Settled conservatively per Codex's review: an
   extension stays inside the outer batch allowance and `max_cost_usd` is not
   extendable. The remaining question is how a run learns which allowance
   record admitted it (proposed: the series runner writes the record path and
   its sha256 into `series.json`, which `--extend` reads).
