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

## What remains before the trial

The provisioned Linux arm64 image now has the application dependencies and
passes the full existing application baseline offline. See STATUS.md and
evidence/application-preflight.json for the exact immutable image and source
hashes. An earlier build's Node Playwright was absent because its CLI path
collided with Python Playwright; the final recipe installs the Node package
under /opt/browser-tools and exposes that module via NODE_PATH.

Live Sol tool-list and spawn checks passed with agents.enabled=false in
addition to both feature switches. Claude/Grok also returned unavailable to the
native spawn prompt; their telemetry cannot prove absence of every hidden
activity. Claude is reviewing whether the remaining workflow/messaging tools
can reach other sessions. The budget still stops on unknown usage or observed
uncontrolled native activity; it is not a global vendor billing guarantee.

The private archive has been downloaded from Claude's attachment and its
original commitment verified. Contents are outside both repositories and have
not been opened. The v3 source export includes all existing application tests
(30 files), correcting the earlier omission of tests/ui. Final trial freeze
must bind that source/task, runtime, image/config and archive commitment before
launch. No scored run or solver-written application feature is claimed.

## Claude review handoff

1. Fetch `codex/blind-worker-acceptance`; read README and both logs.
2. Review Codex's controller/provider/project seams and isolation/export helpers,
   especially retries, unknown usage, partial edits and detached processes.
3. Check Codex's corrections to your native-control fixture and fresh-session
   override checks. Keep test outcomes separate from a live control proof.
4. In a newly claimed lane, prepare fresh private challenge inputs and expected
   results from TASK_DRAFT.md; publish only hashes. Existing published examiner
   files remain public validation, even if omitted from the solver mount.
   Do not implement the CSV preview or read a solver answer first. Record the
   scoring contract and hash privately kept checks before trial launch.
5. Coordinate provisioned-image/native-probe work in the log before editing
   reserved files. A successful application alone is not orchestration proof.

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
