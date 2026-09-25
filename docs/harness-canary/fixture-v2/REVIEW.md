# Fixture v2: review before any live run

Status: proposed measurement instrument. No live allowance is attached.

The solver receives only `project/` and a generated `.quadratus/policy.json`.
`control/` stays outside the solver tree. Preparation copies only the grader
to `<target>-instrument/test_contract.py`; the policy and manifest point only
there and hash that copy. The post-run launcher must use the manifest's grader
path and verify its hash. No live policy or manifest points into `control/`.
The reference repair is for offline controls only and must not be exposed to
model seats, including through a runtime checkout containing these control
files. Path separation prevents accidental disclosure; it is not an OS access
boundary. Freeze a runtime export without controls or reference repairs before
live execution. The prepare command does not grant live-run permission.

Two ordered tasks:

1. Non-security lookup and one-file display adaptation (`presentation.py`).
2. Security repair spanning endpoint and predicate (`app.py`, `access.py`).

The public task does not name the display helper. The grader proves the helper
is actually called, tests whitespace and truncation, and separately proves the
shared access predicate is called and rejects cross-tenant and missing records.
`auth.py` and `catalog.py` are fixed source; changing them invalidates a result.

## Measurement constraints that need Claude's review

- A natural lookup does not force a WORKER request, a rejected worker request,
  or a sibling retry. Those are observed coverage, not promised coverage. Keep
  deterministic regression tests for paths that a live run does not exercise.
- A full two-task grader used after task 1 would demand that task 2 already be
  repaired. That would encourage an out-of-scope edit. The identical common
  integration gate therefore checks five preservation cases on both engines.
  Models can run the appropriate `-k lookup` or `-k security` checks. The full
  external grader runs after the controller stops, and all 13 cases must pass
  for application acceptance. Controller completion is reported separately.
- Candidate family gates explicitly disclose the deferred correctness checks;
  a green preservation gate is never reported as full task acceptance. This is
  a proposed adaptation, not a silent change to the frozen original Q9 grader.
- Use the native Mac, no container substitution. Actual Codex sandbox checks
  must pass. Vendors with no sandbox self-test are reported not applicable,
  not as tested. Authentication without a positive probe remains unverified.
- Final runtime heads need agreement: baseline 5d70d31 is the historical
  baseline, candidate must include reviewed #25 plus any explicitly selected
  #23/#24 repairs. Do not silently backport candidate fixes into baseline or
  attribute the whole comparison to #25 alone. Pin both immutable heads and
  list their exact difference before requesting the allowance.

## Offline verification

Run `control/verify.py` with the acceptance Python environment. It creates four
fresh controls: broken, lookup-only, security-only, and fully repaired. Each runs
the two task subsets, preservation subset, and full grader. Only the reference
must pass the full grader; no control can pass by repairing the other task.
Fresh solver copies never include the repair. Hash the grader and input files
before and after every live run. Compare all source paths, not model-reported
CHANGED lines. Any changed undeclared path is an invalid result.

## Budget and reporting changes from the GameTape trial

One pair is a separate experiment from the twelve GameTape continuations.
Use fresh identical source and empty run state for each member. Preserve each
failure as a terminal observation; no repair feedback or rerun counts as an
independent sample. No automatic live continuation.

The initial pair requests two runs total, each with at most 24 transport calls,
a 500,000 reported-token stop threshold, 840 seconds internal wall time,
900 seconds supervised wall time, two concurrent workers and two tasks. The
pair's aggregate reported-token stop threshold is 1,000,000. Check aggregate
usage before admitting the next run; reserve its full per-run allowance first.
In-flight overshoot is possible and must be reported. Never reset aggregate
usage by assigning a new attempt id. A failed or ambiguous invocation still
consumes its slot. Unknown usage stops further admission.

The later five-per-version series requires its own explicit allowance and
Claude's review of the first pair. Do not infer permission from a clean pair.
Report completed successes out of all five attempts, including stopped runs.
Five trials give a small descriptive sample, not strong reliability evidence.

For each run preserve: source/runtime/instrument hashes; task order and scopes;
planned roles and actually invoked roles; worker/lookup/refusal/sibling coverage;
internal gate receipts and final independent grader; declared and actual edits;
cached and fresh input separately where the provider exposes them, otherwise
unknown; output tokens; total normalized tokens; per-role cost and elapsed time;
controller completion, code acceptance and review completion separately;
operator interventions; raw provider evidence privately; process cleanup.
Provider auxiliary usage with uncertain overlap stays separate. Do not infer
invoice cost from subscription token counts.

After the harness comparison works, a separately authorized single-agent run
on this frozen fixture can assess multi-model benefit. This comparison alone
does not establish that benefit.

## Core follow-up for Claude

The GameTape trial preserved files but restarted planning and review state.
Propose an explicit resume operation using the existing run records, with
source/policy/runtime hashes checked, completed task receipts retained, pending
reviews restored, original usage counted, and ambiguous in-flight calls never
automatically repeated. A restart must never label a captured response replay
as fresh independent review. This needs a separately reviewed core change;
fixture v2 does not pretend to implement it or introduce a new event system.
