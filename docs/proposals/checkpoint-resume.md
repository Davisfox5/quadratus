# Proposal: explicit checkpoint and resume for a project run

Proposal only; the core change is sensitive-path. Source: "Core follow-up for Claude",
`docs/harness-canary/fixture-v2/REVIEW.md`. Citations: `codex/blind-worker-acceptance` at `36ab9b6`.

## 1. TL;DR

- **Decision:** add `quadratus --resume <run-dir>`. It continues the same run in
  the same run directory from one new file, `checkpoint.json`, which is rewritten
  atomically at about ten points the loop already passes through. No event system.
- **It refuses before spending anything** if the source, policy or runtime hash
  differs, and says which.
- **It keeps what was finished:** closed tasks, their ledger entries (verbatim,
  never re-summarised), gate receipts, operator rulings and open findings.
- **It restores a review in progress** at the last completed step (draft,
  reviews, revision, recheck), without calling those reviewers again.
- **It counts what was already spent** against the same limits. No fresh budget.
- **It never re-sends a call whose outcome is unknown.** It names the call and
  stops. Only the operator can release it.
- **A restored review is labelled restored**, with its artifact id and original
  attempt. It is never presented as a fresh independent review.
- **A security excursion is never resumed partway through.** It restarts from the
  task's beginning, and only if the tree still matches that task's baseline.
- Existing records lack a runtime hash, ledger structure, review state and any
  trace of a call in flight at a hard kill (section 2). The checkpoint adds those.

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
- **Orchestration state.** The ledger's structured entries and rulings exist
  only in memory (`ledger.py:100-111`, `119-147`). Only the rendered `ledger.md`
  reaches disk, at the end. The same is true of `history`, `open_findings`,
  `checks` and the rotation counter (`session.py:507-528`). The RunStalled
  guard's `previous_description` is a local variable (`session.py:1600-1615`).
  Task ids derive from `len(self.history)` (`session.py:1544`).
- **Open task state.** `TaskMemory` turns are in memory (`memory.py:95-114`).
  Drafts, reviews and revisions are kept as artifacts (`session.py:1149`,
  `1181`, `1216`, `1232`). Nothing records which step the task had reached,
  which findings were BLOCKING, the fix-cycle count or the unresolved verdicts.
  Recheck verdicts aren't kept as artifacts at all (`session.py:1843-1844`).
  The pre-task baseline `_task_before` is in memory (`session.py:1049`), and so
  are the worker `RepeatedFailure` fingerprints (`workers.py:406`, `495`).
- **In-flight calls.** `invocations.jsonl` is written after the call: "This is
  not a write-ahead journal" (`delegation.py:80-82`). `in-flight.json` needs the
  `except` path, which a wall-ceiling kill skips (`isolated_run.py:91-95`). Only
  `budget.json`'s `in_flight` count survives a hard kill, in bounded runs only.
- **Hashes.** A source fingerprint exists (`project.py:84-88`) but is recorded
  only at the end (`project_run.py:196`). A policy hash exists (`policy.py:307`,
  `313`). **Quadratus doesn't record a runtime hash at all.** Runtime commits
  are recorded by hand in `docs/harness-canary/RESULT.md:18`.
- **Failed-call usage.** The meter skips failed calls (`usage.py:147-164`;
  `docs/GAMETAPE_TRIAL.md:50-53`). `RunBudget` starts its clock and counters
  from zero and has no restore (`run_budget.py:77-90`).

## 3. The resume contract

**Preconditions.** Resume refuses, naming each mismatch, unless all of these hold:

1. `source_fingerprint` of the project now equals the checkpoint's value, taken
   when the checkpoint was written (not at run start).
2. `policy_hash`: `load_policy` plus `resolve` with the recorded arguments
   reproduces the recorded `policy-plan.json` `hash`.
3. `runtime_hash`: sha256 over the installed `quadratus` package files, computed
   the way `load_library` hashes the harness library (`policy.py:80-92`), plus
   the `QUADRATUS_ALIAS_*` and `QUADRATUS_CLI_ARGS_*` overrides in effect,
   because those change which model answers a seat.
4. `run_args_hash`: goal, project, `allow_writes`, mode, `max_tasks`, check,
   scope, forbid and `run_limits`. A different goal is a new run, not a resume.
5. No `pending_call` is set, and `budget.json` `in_flight` is 0 (section 5).
6. The project lock is free (`project_run.py:37-49`).

**Restored.**
- Closed tasks: `history` and ledger entries, appended in their original order
  through `Ledger.append` with the stored fields verbatim, plus rulings and
  invariants. Task ids continue from the restored count.
- Gate receipts (`session.py:1893-1895`), policy plans, scope reports, open findings.
- The pending review state of one open non-security task, at its last completed step.
- Spend: `RunBudget` is seeded from `budget.json` (reserved attempts, tokens,
  cost, unknown-usage counts, preserved responses, elapsed seconds). Wall time
  continues from the recorded elapsed value. `usage.jsonl` and
  `invocations.jsonl` keep appending in the same directory and are loaded for
  the final report, so the totals cover both segments.
- `previous_description`, so RunStalled still fires across the resume, and the
  worker failure fingerprints, so RepeatedFailure does too.

**Never done.**
- Re-issuing a call whose result is unknown. That includes a writing call cut
  off partway (`providers.py:148-160`, `364-380`) and any call recorded as
  `pending_call` with no matching outcome.
- Presenting a restored or captured response as a fresh review. Restored notes
  reach the lead under the same anonymous labels, but the checkpoint, report and
  `result.json` list them as `restored` with the artifact id and the attempt it
  came from, and no new `InvocationEvent` is emitted for them. A
  `budget-responses/` reply was never accepted into task state and is never fed
  back in.
- Re-summarising. The ledger is reloaded from structured entries, never parsed
  back out of `ledger.md`, and no close-out runs again for a closed task.
- Resetting spend: fresh limits are never put over spent ones.

**Stopped outputs are kept.** Resume first moves `report.md`, `result.json`,
`ledger.md`, `changes.diff`, `delegation.md` and `in-flight.json` into
`stops/<n>/`, so each stop stays a terminal observation, as REVIEW.md asks.

## 4. Where the state lives

One file, `<run-dir>/checkpoint.json`, written as a temp file plus `replace`,
the same pattern `RunBudget._persist` already uses (`run_budget.py:114-120`).
Large text goes into the existing `ArtifactStore`, and the checkpoint holds only ids.

Fields: `schema_version`, `seq` (monotonic), `written_at`, `source_fingerprint`,
`policy_hash`, `runtime_hash`, `run_args_hash`, `stop_reason`,
`resumes` (list of prior stops), `ledger` (entries as stored, with ref ids),
`rulings`, `invariants`, `history_ids`, `checks`, `open_findings`,
`scope_reports`, `policy_plans`, `previous_description`, `rotation`,
`worker_failed` (fingerprints), `open_task` (below) and `pending_call`.

`open_task`: `spec` (task id, description, kind, complexity, needs, scope,
lead), `work_class`, `phase` (`declared`, `drafted`, `reviewed`, `revised`,
`rechecked`, `gated`), `baseline` (path to a copy of `_task_before` in
`baselines/<task_id>/`), `turns` (a role plus an artifact id for each),
`draft_ref`, `reviews` (peer, label, artifact id, blocking flag),
`revision_ref`, `fix_cycles`, `unresolved` (peer, verdict artifact id),
`gate_fixes_used` and `restored` (what came from a prior attempt).

`pending_call`: task, role, model, `allow_writes`, prompt artifact id and
budget ticket. It is set before dispatch and cleared after the acceptance boundary.

Write points, all at existing lines:
| Point | Citation |
|---|---|
| Run start, after the policy plan (hashes, args) | `project_run.py:118` |
| Task declared, before dispatch | `session.py:1614-1620` |
| Baseline captured | `session.py:1049-1051` |
| Before and after each model call (`pending_call`) | `session.py:543-551`, `567` |
| Draft kept | `session.py:1149` |
| Each review kept | `session.py:1181` |
| Each revision kept | `session.py:1216`, `1232` |
| Recheck result known | `session.py:1220-1222`, `1234-1238` |
| Gate result appended | `session.py:1893` |
| Task closed and absorbed | `session.py:1161-1162`, `1257-1258`, `1334-1335` |
| ASK ruling recorded | `session.py:1474` |
| Excursion opened (marker only) | `session.py:1277` |
| Final records | `project_run.py:189-210` |

## 5. Failure modes

- **Partially written checkpoint.** The atomic replace keeps the previous one.
  An unparseable file or unknown `schema_version` refuses; nothing is rebuilt
  from `ledger.md`. An artifact written just before a crash is a harmless orphan.
- **Hash mismatch.** Refuse, listing every differing hash with both values.
  No override flag: a resume on a different tree is a different run.
- **Ambiguous in-flight call.** `pending_call` is set, or `budget.json`
  `in_flight` is above 0. Resume refuses and names the call, the model and
  whether it could write. An operator release (`--release-call <ticket>`)
  records the call as consumed with unknown usage and restarts its phase, but
  only if the tree matches the phase's baseline. Under a bounded run, unknown
  usage already latches `unknown_usage` (`run_budget.py:166-168`), and
  REVIEW.md says unknown usage stops further admission, so a bounded resume
  after an ambiguous call stays refused (open question 1).
- **After a scope stop.** A `PartialWorkStopped` from the scope check
  (`session.py:552-558`) left out-of-scope or oversized edits on disk. The
  source no longer matches the task baseline, and replaying the edit would be
  the "second, different edit" that `providers.py:364-380` exists to prevent.
  Resume refuses and names the scope report. See open question 2.
- **After a token, call or RunStalled stop.** A latched `stop_reason` refuses up
  front with the spent totals; raising limits changes `run_args_hash`, a new run.
  RunStalled's remedy is the operator's (`session.py:137-148`), so it refuses too.
- **Inside a security excursion.** Refuse to restore, and restart the excursion.
  CLAUDE.md's memory-scope rule gives a brain-trust member full working memory
  for one task and wipes it when the task closes, so none of an excursion's
  working state is meant to outlive it. The excursion is bounded and closes
  unconditionally (`session.py:1336-1341`). Its seat is recomputed from
  availability, never held (`session.py:1277`; CLAUDE.md, "lapses by
  recomputation"). Restoring a half-run excursion would carry a stale seat and
  a draft whose verdict hash binds the old source (`session.py:1343-1350`). So
  resume drops the open task and declares it again from `spec`. The draft is an
  editing call (`session.py:1286-1291`), so this happens only if the tree still
  matches the task baseline. Otherwise resume refuses.

## 6. Test plan
Offline only, with fakes for `invoke`, `fleet_type`, `session_factory` (`project_run.py:91-92`).

| Test | Asserts |
|---|---|
| `test_checkpoint_roundtrip_is_verbatim` | save then load gives identical ledger entries, rulings and receipts |
| `test_checkpoint_write_is_atomic` | a failure partway through a write leaves the previous checkpoint readable |
| `test_resume_refuses_on_source_change` | names `source_fingerprint`; no call is made |
| `test_resume_refuses_on_policy_change` | edited `.quadratus/policy.json` names `policy_hash` |
| `test_resume_refuses_on_runtime_change` | changed package digest or alias override names `runtime_hash` |
| `test_resume_refuses_on_changed_goal_or_limits` | names `run_args_hash` |
| `test_resume_keeps_closed_tasks_and_task_ids` | two closed tasks survive, the next id is `t3`, and no close-out runs again |
| `test_resume_never_resummarises_ledger` | the ledger rendered after resume equals the original rendering plus the new entries |
| `test_resume_restores_pending_review_without_reinvoking_reviewers` | the fake records zero reviewer calls, and the lead gets the stored notes |
| `test_restored_review_is_labelled_restored` | the report and `result.json` mark the review restored with its artifact id, and no new collaborator event is emitted |
| `test_budget_response_never_becomes_review` | no prompt after resume contains a `budget-responses/` file |
| `test_resume_counts_prior_usage` | the seeded budget reports original plus new tokens and attempts |
| `test_resume_continues_wall_clock` | elapsed time continues from the checkpoint |
| `test_resume_refuses_ambiguous_call` | `pending_call` set, or `budget.json` `in_flight` 1: refuses, names the call, zero invocations |
| `test_release_call_consumes_slot_and_requires_clean_tree` | a released call stays counted, and a changed tree still refuses |
| `test_resume_refuses_after_token_stop` | a latched stop refuses before any reserve |
| `test_resume_refuses_after_scope_stop` | names the scope report and leaves the tree untouched |
| `test_resume_keeps_runstalled_guard` | the same description after resume raises RunStalled |
| `test_resume_keeps_repeated_failure_guard` | the same worker errand on the same model is refused |
| `test_security_excursion_restarts_not_restores` | an open security task restarts from `spec` and its draft is not reused |
| `test_security_excursion_resume_refuses_on_changed_tree` | refuses when the tree no longer matches the task baseline |
| `test_prior_stop_outputs_archived` | `stops/1/report.md` and `stops/1/result.json` exist unchanged |
| `test_resume_holds_project_lock` | a concurrent run is refused |

## 7. Implementation sequencing

Each slice lands with its own tests; behaviour is unchanged until slice 6.
1. `quadratus/checkpoint.py` (new): dataclass, atomic save, validated load,
   `runtime_hash` and `run_args_hash`. Not a sensitive path.
2. Loaders: `RunBudget.restore` (`run_budget.py`), `UsageMeter.load` (`usage.py`),
   `DelegationLedger.load` (`delegation.py`, **sensitive**).
3. Export and import of closed state (ledger, rulings, history, checks, findings,
   `previous_description`, rotation, worker fingerprints) with the close and ASK
   write points. `session.py` (**sensitive**), `workers.py`.
4. `pending_call` around `_invoke_model`; recheck verdicts kept as artifacts.
   `session.py` (**sensitive**).
5. Open-task restore by phase, baseline copy, excursion restart. `session.py` (**sensitive**).
6. `resume_project`, `stops/<n>/` archiving, lock, Fleet with the restored budget,
   `--resume`/`--release-call`. `project_run.py`, `runtime.py` (**sensitive**), `cli.py`.

## 8. Open questions for Davis
1. **Ambiguous call under a bounded run.** REVIEW.md's "unknown usage stops
   further admission" makes a bounded run unresumable after one. Keep that, or
   let `--release-call` admit it with a usage figure the operator attests to?
2. **Scope stop.** Refuse outright (proposed), or offer "close this task as
   failed and let the orchestrator decide", which needs one close-out call?
3. **Runtime hash strictness.** Requiring an exact package digest (proposed)
   blocks resume across any Quadratus fix, including the fix that motivated
   the resume. Accept that, or allow a named override that is recorded?
