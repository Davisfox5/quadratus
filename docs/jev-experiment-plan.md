# Jev routing experiment proposal

Status: preparation only. This document does not authorize any paid request,
code-execution experiment, purchase or retry. The completed live allowance is
consumed. Claude owns integration; Codex reviews and is the sole local executor.
Canonical ownership/receipts remain in PR46's development record.

The previous one-task run established integration: two decisions, 1,527 reported
tokens, 0.639 seconds overhead, and no change to the Grok lead. It cannot estimate
routing benefit. This experiment separates classification consistency from the
quality and cost of actual assignments.

## Required preparation

- One exact independently reviewed candidate with all required CI checks passing.
  The label-definition scope at 93941b56005688c6245f07cd0d03e678c3413fab has scoped
  clearance. It is not a launch-qualified combined candidate.
- Resolve the SDK transport-retry finding on PR35 comment5923854904. Actual
  typesafe-sdk0.7.0 retried synthetic503 twice under one budget reservation. The
  next experiment requires zero hidden retries and an offline transport regression.
- Review approximately ten Grok-authored tasks on sample base
  f96e0c88e7a18156a9582e3aa19de4c9d70ff89f: three development examples, seven held-out
  cases; all four difficulties; ordinary controls and mixed/ambiguous work.
  Definitions must not be tuned against held-out cases or outcomes. The previous
  favicon case is a development example only.
- Independently approve acceptable answer sets before any new Jev output. Multiple
  defensible answers are allowed. If kind/difficulty acceptability depends on the
  pair, explicitly enumerate pairs before freezing; do not use a Cartesian product
  that admits an indefensible combination.
- Verify source references, named existing test collection and clean baseline.
  Write independent executable acceptance assertions before selecting coding tasks;
  proposed check descriptions are not sufficient for coding qualification.
- Freeze packet bytes, accepted answers, split, actual rendered input, engine SHA
  and source hashes, definitions/version, tool/runtime versions, request shapes,
  model settings, grader bytes and task starting hashes. Preserve earlier freezes.
  Any change creates a new reviewed freeze, never replaces old results.

## Offline tool

`tools/jev_experiment/packet.py` uses the actual explicit-task parser,
`TaskSpec` capability inference and `Session._pick_lead`. Each arm/task receives a
fresh session: rotation0, empty vendor history, normal tier, all configured seats
assumed available. These assumptions are explicit; this is not dispatch admission,
provider availability verification, a real execution, or a quality grade. Actual
live availability must match; otherwise stop rather than silently choose a new arm.

```sh
python tools/jev_experiment/packet.py prepare --engine /absolute/candidate \
  --packet /absolute/packet.json --out /absolute/new-freeze.json
python tools/jev_experiment/packet.py score --engine /absolute/candidate \
  --freeze /absolute/new-freeze.json --observations /absolute/observations.json \
  --out /absolute/new-score.json
python -m pytest -q tools/jev_experiment/test_packet.py
```

Outputs are exclusive-create. Inputs with injected routing metadata are rejected.
The immutable freeze includes the author's proposed default and the observed
selector result separately. The tool contains no provider client or launch command.
It records capability escalation: rote+patch can use the worker; rote+execute
selects a capable full lead. Existing kind pins and load rules are preserved.

Scoring input is a JSON array with one entry per task/repetition (0,1,2), containing
`task_id`, `repeat`, `freeze_digest`, `input_digest`, `answers` keyed by `task.kind`
and `task.difficulty`, and `refusal` (null on success). Preserve full actual
requests, returned model, probabilities, confidence, usages, timing, transport
attempts and shared ledger as additional fields; the scorer retains them. This
scorer verifies the freeze/input binding and routing, not the authenticity of
external provider records. The executor independently reconciles those records.
Unknown/duplicate rows and invalid answers fail; missing rows and refusals remain
visible. Never replace a refusal with a fresh successful sample.

## Stage A: classification-only proposal

Request approval for **60 attempts maximum**: ten tasks x two questions x three
repetitions, sequential concurrency1. No coding/model lead is invoked. Rule-default
routes are computed offline and cost no requests. Preserve the real session question
construction and task context; accepted answers and rationale never enter the input.
Use `jev-latest` as before, record the resolved version, and stop if it changes from
the reviewed expected model. Do not silently change model or effort settings.

Run tasks in fixed ID order, reverse order, then fixed ID order for the three
repetitions; keep kind before difficulty as production does. These repetitions
measure within-sample consistency, not independent deployment reliability.

Proposed shared limits: **60 actual HTTP attempts, 200,000 reported tokens,
$0.02 API-cost stop threshold, 300 seconds inner / 330 seconds outer, concurrency1**.
Timeout per request10seconds, no retries or replacements. Token/cost limits are
post-return stops: the last active call can overshoot. Preserve response and usage
before stopping. Configure cost rates for both requested and resolved model keys;
unknown model/price/usage stops the batch. These are proposals, not new authority.

TypeSafe's [official model page](https://docs.typesafe.ai/models), checked during
preparation on 2026-10-01 UTC, lists Jev1.13 at $0.042 per million input tokens,
output free, maximum64k total request context. At the 200k reported-token threshold,
input-only cost is at most about$0.0084 before the final-call overshoot. A conservative
60 x65,536 input-token bound is about$0.1652; propose **$0.20 total invoice allowance**
with no purchase/top-up and existing credit only. This envelope depends on the
verified price/context limit and no hidden retries; recheck before launch. It is
not an invoice guarantee or permission to raise the budget threshold.

Report development and held-out results separately. For each task and repetition:
accepted kind/difficulty/pair, actual default and selected leads, inferred capabilities,
refusal/fallback, confidence/probabilities as raw uncalibrated evidence, resolved model,
input/output/cached tokens without double-counting, decision seconds, total wall time,
request count and invoice/configured-price distinction. Report all denominators,
missing rows, failures and label/lead consistency; no success-only filtering.

## Stage B: separate matched coding proposal

Stage A completion does not automatically authorize Stage B. Bring its complete
results and selected IDs to Davis. Proposed maximum is **eight coding runs**:
first eligible held-out route-changing task and first eligible ordinary unchanged-lead
control, each with two arms and two repetitions. Order by frozen ID within each
predeclared stratum; do not choose by favorable coding outcomes. If no admissible
route-changing task or no ready independent grader exists, stop for a decision.
Describe the deliberately selected subset; do not generalize it to all tasks.

Each pair starts from identical immutable task source and candidate/runtime hashes,
with identical acceptance checks, safety policy, permissions, required reviews and
model/effort settings. Cold session/reset for every run. Counterbalance rule/Jev order
AB then BA. Use production routing; do not pin an observed lead to force a result.
The Jev arm makes fresh kind/difficulty decisions (at most8 additional Jev requests
across four Jev runs), so record whether the actual lead changed in each repeat.
Do not keep rerunning until it does. Availability divergence, refusal, budget stop
or model-version drift stops the experiment and preserves evidence.

Tentative per-run bounds for review after sample selection: 20 total attempts,
500,000 reported tokens, 600seconds inner/630seconds outer, two workers maximum,
one explicit task, no repair/recovery series. Overall eight-run ceiling160 attempts,
4M reported tokens and5,040seconds outer wall; token thresholds can overshoot on the
last active call. Requirements/review/audit/decision calls all share the limits.
Compare success only against the same independent grader; a scope violation or
required-check/review failure cannot count as an efficiency gain. If a complex sample
needs larger limits, change and approve the proposal before execution rather than
quietly raising them during a run. All native work stays on subscribed transports.

Record full roles, actual models, calls, token/cache counts, wall and role time,
checks, evidence, repairs, stop cause, admission/refusal and source changes. Separate
Jev decision overhead from task work and the whole run. Report successful grade rates
and within-pair deltas, including failures. A faster failed task is not a win; label
agreement is not task quality. Two repeats are a pilot, not a reliability estimate.

## Stop and handoff

Any budget/transport accounting inconsistency, unreviewed input/definition change,
source/grade mismatch, secret exposure risk, provider refusal, unknown usage or new
safety failure stops further attempts. Preserve all evidence and report once.
No automatic retries, purchases, series extension, policy relaxation, scheduler
creation or changes to other agents' schedules. Next authorization is requested only
after the complete preparation packet and exact remaining gates are reviewable.
