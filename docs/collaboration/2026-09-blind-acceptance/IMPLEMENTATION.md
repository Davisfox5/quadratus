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
  only writable disposable input and read-only runtime, using a non-root UID/GID. No home, Docker socket,
  examiner, Git history or credentials are mounted. Network is off by default.
  A host watchdog removes the entire container, including detached descendants;
  partial work remains. Docker cleanup has a bounded grace; a failed cleanup
  raises instead of claiming the workload was stopped.
- OpenAI CLI calls now carry enforced native multi-agent disable flags and
  reject conflicting config/enable/resume/fork arguments. Native observations
  remain visible as possible control failures. See Claude's implementation log.

## What is not complete

This is a tested control foundation, not an authenticated blind acceptance run.
The base Python Docker image used for isolation tests has no signed-in vendor
CLIs. Auth provisioning must expose only the credentials needed, with no home
instructions/session state; vendor readiness must be checked in that exact
container. Public network access is needed for vendors/standards lookup and
must not expose the private examiner bundle. A fresh directory alone does not
establish this boundary.

The task text in TASK_DRAFT.md adopts cloud Claude's export-compatible contract
(endpoint, columns, response shape, UI hooks and 5000-row limit), superseding
the earlier 500-row draft. Cloud Claude supplied validation tests, but they
were committed to this PUBLIC repository. They are public checks, not secret
held-out tests. Claude must prepare fresh private cases before any solver output; those
checks stay outside solver mounts. Freeze their hashes and the final task/input
hashes together before launching. Do not call the current example export a
fully preregistered or scored trial.

No live proof yet that Codex cannot spawn children under these controls, and no
new restricted Grok probe yet. Cloud Claude added an opt-in
`QUADRATUS_NATIVE_DELEGATION=off` request for Claude/Grok senior seats. It now
preserves tool denials, denies Claude Task/Agent, and rejects vendor extra args
that could override the request. Grok enforcement remains unverified because
its approval flag may defeat Agent denial. Default senior behavior is unchanged. Reported parent usage is not proof of total vendor activity.
These gaps must be resolved or explicitly bound and disclosed before claiming
an all-vendor bounded run. Budget limits do not fix the vendor aggregate meter.

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
