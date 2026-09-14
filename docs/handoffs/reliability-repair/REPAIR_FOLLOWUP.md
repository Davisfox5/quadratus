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

Live acceptance results will be recorded alongside this report. Passing deterministic tests do not establish that every live model or worker was exercised.
