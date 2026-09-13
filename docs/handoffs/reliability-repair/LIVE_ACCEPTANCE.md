# Mac acceptance of f030fb3 — not accepted

The repair was fast-forwarded onto `codex/project-workflow` and tested on the Mac. **634 tests passed and Ruff was clean**, matching the cloud report. A fresh isolated GameTape worktree at `f93d8b1` passed **79 tests** and JavaScript syntax checking before the live run. The same 79 tests and syntax check passed after it stopped. No reliability implementation changes were made during acceptance.

The live run used the original goal bytes from `evidence/routing-trial/goal.txt`, the normal subscription-CLI transports and a 12-task cap. No model names, forced classifications or scope settings were added. External observation wrappers only recorded the original arguments, returns and errors. Run ID: `20260913T213024Z-c6140207`.

## What ran

Fable naturally selected `architect/complex`, with Opus lead and Sol/Grok collaborators. Opus commissioned restricted Luna. The first Luna patch applied a 119-line `docs/BULK_TAGGING.md`, and Opus resumed. It then commissioned another Luna edit.

The operator stopped the run after the acceptance defects below were confirmed, during the second Luna invocation. **The feature did not complete, no task closed, and the in-run integration gate had not executed.** The post-stop 79-test result is independent verification of the preserved tree, not a successful run gate. Sol and Grok were selected but never invoked. This run therefore does not prove peer review or live recovery from a malformed worker response; the existing deterministic tests cover the latter.

## Required corrections

### 1. Scope bounds are not active on the normal project path

`Session.next_task` sets `scope=self.config.default_scope`, but `run_project` and `SessionConfig` default it to None and the CLI supplies no scope. `_assess_scope` returns immediately when it is absent. The real selected task's scope is None and the run records `scope_reports: []`.

The new TaskScope class is useful plumbing, but it does not fix the observed task-expansion problem for ordinary CLI/GUI users. The 119-line document is evidence of what was saved, not by itself proof of a configured bound being exceeded: **there was no enforced bound**. Wire task declarations into normal dispatch and measure them before acceptance; retain the preservation/no-silent-rollback behavior.

### 2. Invocation attribution and selection reconciliation are not wired through

`Fleet.invoke` calls `_generate` without its task, role or origin keyword arguments. Actual invocations consequently have task="-", role="-", origin="seat" — including Luna commissioned by WorkerPool. `_record_invocation` uses the CLI argument as resolved model (`opus`), while `_record_selection` uses the canonical key (`claude:opus`). `selected_never_invoked` subtracts those unequal strings and wrongly reports Opus as never invoked after two completed Opus calls.

See `acceptance-f030fb3/invocations.jsonl` and `invocation-attribution-proof.json`. Thread the real invocation context through orchestrator, lead, collaborator, worker, revision, closeout and retry paths. Keep canonical seat identity distinct from vendor-resolved identity, and reconcile by task/invocation identity rather than comparing incompatible model strings. The vendor alias alone is not proof of a resolved release.

### 3. Native accounting still misses the real captured Codex activity

The new `_extract_native_children` was replayed against the exact prior live stdout from Sol's review. It returned **zero children**. That stdout contains `item.*` records with a nested `collab_tool_call`/`wait`, rather than the flat top-level `spawn_agent`/`child_session` shape the new parser expects. The stream does not include enough child identity/usage to recover the known total on its own.

The native child and its additional 135,105 tokens were proved from the scoped vendor session files in the original investigation. Parsing invented flat shapes does not implement that reconciliation. Support the real nested activity shapes, explicitly record unknown native activity when identity is unavailable, and use scoped session records where available to reconcile actual children. Do not scan unrelated sessions or invent missing counts. Preserve de-duplication and the separation from Quadratus worker budgets.

See `native-parser-replay.json`, the earlier `evidence/routing-trial/review/native-delegation-evidence.json`, and the portable replay helper in this directory's acceptance folder. The raw original stdout remains local; the helper includes a normalized extraction of the actual nested wait events and independently established child evidence.

### 4. Process cleanup can still hang after timeout

`_terminate_group` sends TERM to the process group, but returns as soon as the parent exits. If a descendant ignores TERM, it is never sent KILL. `_launch` then calls `communicate()` without a timeout; inherited stdout/stderr pipes keep it blocked.

A bounded reproduction launched a normal parent with a child that ignores TERM and inherits the pipes. With a one-second timeout, `_launch` was still blocked at ten seconds (past the configured termination grace) and the child was alive. Explicitly killing the probe's own group released the call. All probe processes were cleaned up. See `process-cleanup-proof.json` and `repro_process_cleanup.py`.

Wait for/terminate the **group**, not merely the parent, and bound the final output drain. Cover the case where the parent responds to TERM but a child does not, in addition to both ignoring TERM. The actual live stop did clean up its observed process groups; this controlled reproduction is a distinct failure case.

### 5. Real cancellation is still absent from telemetry and in-flight state

The external observer recorded the second Luna CLI invocation ending in KeyboardInterrupt after 23.67 seconds. The application's invocations.jsonl contains no row for that call. result.json reports `unknown_invocations: 0` and `in_flight: {}`, even though the first task was still in flight and its earlier document edit was preserved.

`Fleet._generate` catches Exception, which excludes KeyboardInterrupt. The provider replay/partial-work path also handles Exception rather than real interruption, and worker calls bypass Session._edit's source capture. Do not swallow cancellation or retry it. Record it in a finally/error path, preserve the current task/worker and already-applied work, clean up, then propagate the stop. Preserve unknown usage as unknown. In this specific stop, the interrupted restricted worker itself had not applied a patch; that does not make the invocation disappear or mean the task had no partial work.

## Passing behavior and unexercised claims

- Metadata selected the complex design correctly, and the existing exact prefaced-test regression passes.
- Restricted worker editing still returned a patch through the harness, applied to the correct project. No tool grant was widened.
- The successful worker result returned to Opus.
- The live stop left no observed vendor process groups requiring manual cleanup.
- Full offline suites pass; no application source was changed by the independent acceptance probes.
- No live malformed-worker recovery, editing timeout recovery, Sol/Grok collaboration, full feature completion, or all-worker coverage is claimed by this run. They remain acceptance work after these corrections.

## Usage and preserved state

The four completed CLI calls report **830,110 tokens** including cached/repeated input: Fable 99,968; two Opus calls 707,463; Luna 22,679. The interrupted second Luna call has unknown usage. This excludes supervising Codex work and is not a dollar invoice. See `usage-summary.json`.

The isolated `quadratus/reliability-acceptance` branch retains only the uncommitted design document. Original GameTape and prior trial worktrees are unchanged. The detailed local run, stdout and observer logs are in `output/live-acceptance-f030fb3/`; selected sanitized proofs are tracked in `acceptance-f030fb3/` beside this report.

Fix these as a coordinated follow-up, rerun the full suites, then repeat live acceptance. The implementation report's “all six problems are fixed” conclusion is not supported by this acceptance result.
