# Reviewable implementation and next gates

## Implemented interfaces

- `RunLimits` and `RunBudget` in `quadratus/run_budget.py` implement opt-in
  global provider-attempt counts, normalized reported-token thresholds and a
  monotonic deadline. Concurrent reservations are atomic. Unknown usage and
  observed uncontrolled native children stop further attempts. Threshold
  overshoot from already active calls is preserved and reported.
- Pass `run_limits=RunLimits(...)` to `run_project(...)`. The shared Fleet
  attaches the controller to each provider view, including worker calls.
  Retries with reported usage reserve separately; missing usage prevents retry. Worker concurrency defaults to two
  for this mode. `budget.json` and `result.json` preserve control state;
  invocation records distinguish a completed provider response from a subsequent
  budget stop. Completed replies at that boundary are retained privately under
  `budget-responses/`, including already in-flight workers. Ordinary runs without limits retain existing behavior.
- `freeze_input(repo, revision, paths, destination, brief=...)` exports an
  explicit selection of tracked regular files from a Git commit and hashes each
  file. It excludes agent/run configuration, symlinks, missing selections and
  reused output trees. The working checkout's edits do not leak into the export.
- `run_isolated(image=..., work=..., runtime=..., command=[...])` requires an
  immutable local Docker image ID and separate ordinary directories. It mounts
  only writable disposable input and read-only runtime, using a non-root UID/GID. No host home, Docker socket, examiner or Git history is mounted. Network
  is off by default; vendor preflight explicitly selects `network=True`.
  Optional `credentials=` accepts a separate private tree containing only
  `.codex/auth.json`, `.claude/.credentials.json`, and/or `.grok/auth.json`.
  It mounts the seed read-only and copies it into a fresh tmpfs HOME for
  credential refresh; neither refreshed credentials nor new sessions are
  copied back to the host. The caller removes its private seed after use.
  This validates paths and permissions, not the auth method inside the files.
  Stage subscription credentials only and check vendor readiness separately.
  The process limit is explicitly 512 to allow browser checks.
  A host watchdog removes the entire container, including detached descendants;
  partial work remains. Docker cleanup has a bounded grace; a failed cleanup
  raises instead of claiming the workload was stopped.
- OpenAI CLI calls now carry enforced native multi-agent disable flags and
  reject conflicting config/enable/resume/fork arguments. Native observations
  remain visible as possible control failures. See Claude's implementation log.

## Current execution handoff

See STATUS.md for current ownership. The app baseline, isolated subscription
authentication, private archive transfer, independent control review and live
control probes are complete. A real Grok delimiter failure was preserved and
repaired before freezing; do not infer a pass from the earlier failed list.
The final frozen input/runtime/image/configuration and examiner archive hash
are in evidence/scored-attempt-1-freeze.json. No private cases are mounted.

Codex executes one uncoached attempt with the approved limits, saves all partial
work and telemetry, and publishes a precise result handoff. Claude then scores
the saved output against its already precommitted private cases and reviews
routing/usage separately. Neither agent changes private cases or coaches the
solver. A run that stops is a recorded partial result, not a reason for an
automatic continuation or another preflight cycle.

Native checks are scoped live evidence; unknown usage or observed uncontrolled
native activity still stops the run. They do not prove global vendor billing.
Only STATUS and explicit PR handoffs assign the next action. Historical review
requests in logs do not create new gates.

## Verification commands

```
QUADRATUS_TEST_DOCKER=1 QUADRATUS_LIVE_CODEX_FEATURES=1 /tmp/quadratus-review-env/bin/python -m pytest -q
/tmp/quadratus-review-env/bin/ruff check .
git diff --check
```

Docker tests and the installed-Codex configuration check are opt-in in CI;
without those flags they report skips, not passes. Configuration checks do not
call a model. The local checks include them. Native CLI flags may evolve;
unknown disable flags fail loudly rather than silently reopening delegation.
