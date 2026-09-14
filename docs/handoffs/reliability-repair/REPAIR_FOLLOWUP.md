# Mac acceptance repairs

This follow-up addresses the five gaps recorded in `LIVE_ACCEPTANCE.md`. That document remains the historical acceptance result for f030fb3.

## Implemented behavior

- Normal editing runs require the orchestrator to declare `SCOPE` JSON alongside `KIND`: narrow project-relative paths, an intended result, acceptance conditions, and a positive line estimate capped at 100. One correction is allowed before dispatch fails. Optional operator bounds apply in addition to the task declaration. Every successful editing call, including workers, revisions and gate fixes, measures the cumulative task diff. A path violation or a line estimate exceeded by more than 50 percent stops the task and preserves its work. Binary and empty-file changes count for path checks. Acceptance conditions remain stated criteria evaluated by the lead/review/gate; they are not an automatic semantic proof.
- Invocation context crosses orchestrator, lead, consultant, collaborator, worker, revision, recheck, verifier, gate-fix and closeout paths. Each transport attempt records task, phase, origin, canonical model, wire alias, resolved release when supplied, duration and known/unknown usage. Selection reconciliation compares canonical identities within each task. Rejected returned patches are recorded as failures after return. Retry accounting does not duplicate the final attempt.
- Nested Codex `collab_tool_call` records expose native activity even when child IDs are missing. Unknown activity is separate from identified child counts. The session reader locates the exact dispatched parent, bounds candidate metadata by the invocation's time interval, then requires matching cwd and an explicit native parent link before reading a child's usage. Inherited fork history is excluded; cumulative updates use maxima. Native observations persist separately in `native-children.jsonl`. Native accounting remains observational, outside Quadratus's worker budgets. Missing or inaccessible records remain unknown.
- Process cleanup signals the stable process-group ID after the launcher exits, escalates TERM to KILL for surviving descendants, and bounds the final pipe drain. Tests exercise both responsive and TERM-ignoring launchers with a TERM-ignoring child, on timeout and KeyboardInterrupt.
- KeyboardInterrupt records the interrupted invocation and propagates without retry. The run persists `in-flight.json`, the task declaration, current worker/phase, interrupted prompt artifact, and measured partial work. The CLI write-grant property now also reaches the existing no-replay guard for editing timeouts.

Model roles, fallback seating, subscription CLI transport and restricted worker edit grants are unchanged. No automatic commit, push or rollback was added to Quadratus.

## Validation

The full suite passed 656 tests. Focused rechecks cover final accounting/reporting refinements. Ruff and whitespace checks pass. The fresh GameTape acceptance worktree starts at f93d8b1 with 79 passing tests and a passing JavaScript syntax check.

The original scoped Sol session replay recovers exactly 127,405 input + 7,700 output = **135,105 child tokens**. The original process-cleanup reproduction now reports `returned_within_10_seconds: true` and `child_alive_after_timeout: false` (about 6.1 seconds).

The live results below distinguish observed model calls from deterministic coverage. Passing tests do not establish that every live model or worker was exercised.

## Live acceptance of 800a91a

Run `20260914T013604Z-a59f28ca` used the original goal bytes and normal subscription CLI routing in the fresh `quadratus/reliability-acceptance-v2` GameTape worktree. The task cap was 12; the operator deliberately interrupted task 2 after partial edits to test the cancellation path. This was a bounded reliability acceptance probe, **not a completed bulk-tagging feature**.

Fable's first scope JSON was malformed. The bounded correction produced a valid declaration for `docs/BULK_EDIT.md`, with a 90-line estimate. It selected Sol as lead and Opus as reviewer. Sol wrote 113 lines; Opus raised blocking findings; Sol revised to 121 lines; Opus returned `RESOLVED`. Scope reports show only the declared file, inside the 135-line tolerance. The same-tree integration gate passed all 79 tests, and Sol closed task 1.

Fable then selected Grok for the persistence-locking task, scoped to `app.py` and `tests/test_bulk_edit.py`, with a 100-line estimate. After Grok saved those files, an actual KeyboardInterrupt stopped the call. The application records:

- Exactly one interrupted Grok invocation, task `t2`, role `lead`, usage **unknown**, with no replay.
- `in-flight.json` identifying the task, scope, current call, interrupted prompt artifact, and **125 changed lines** across the two declared files.
- One completed task and an incomplete run, rather than a false success.
- Byte-identical hashes for all three saved files before and after stopping.
- No surviving observed runner or Grok process; no extra process cleanup was needed.

Independent post-stop checks passed **80 tests** and `node --check static/js/app.js`. These describe the preserved partial tree; task 2 did not close or pass an in-run gate. The original GameTape checkout and the earlier trial worktrees were not modified.

| Model | Actual role(s) | Calls | Known tokens | Unknown calls |
|---|---|---:|---:|---:|
| Fable 5.1 | Orchestrator | 3 | 309,692 | 0 |
| GPT-5.6 Sol | Lead, revision, closeout | 3 | 1,064,291 | 0 |
| Opus | Collaborator, recheck | 2 | 390,509 | 0 |
| Grok default | Lead, interrupted | 1 | Unknown | 1 |

The known total is **1,764,492 tokens**, including cached/repeated input. It excludes supervising Codex work and is not a dollar invoice. The historical 135,105-token native replay is separate from this new run's usage.

All three senior model families were naturally invoked. No Quadratus worker was commissioned during this bounded live probe; worker attribution, rejected-patch recovery, and worker cancellation were exercised in regressions. Scope-violation stops, mixed-response process cleanup, and editing-timeout no-replay are also covered by deterministic reproductions. No all-worker coverage, new live native child, or complete GameTape feature is claimed.

Evidence is in [`acceptance-800a91a/`](acceptance-800a91a/): application invocation/result/scope records, preserved diff, interrupted prompt, usage summary, byte/process interruption proof, native-session replay, and process-cleanup reproduction. Raw vendor outputs remain local under `output/live-acceptance-800a91a/`.

After this live probe, selection-only rows were normalized to put the seat key in `canonical_model` and leave `resolved_model` unknown until a vendor actually reports it. Selection reconciliation already used the canonical/requested identity during the probe. The final full suite verifies that reporting refinement.
