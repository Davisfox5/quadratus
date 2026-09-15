# Next acceptance: independent routing with bounded spend

Status: protocol and delegation gap recorded; no new model run started.
Branch: `codex/blind-worker-acceptance`.

## Objective and operator correction

Quadratus is the product under evaluation: a subscription-backed coding system
that independently decomposes work, assigns suitable models, controls helper
cost, verifies changes and reports what actually happened. GameTape is a test
application. A correct application patch alone does not pass this evaluation.

The operator requests broad enough work to exercise every model naturally,
without naming that objective in the solver prompt, and without large compute.
These are separate measurements: application correctness, routing quality,
model/seat coverage, and consumption. Do not award coverage for a selected but
uncalled model, or for vendor-native children observed after the fact.

Astra is fallback-only; Sonnet and Terra are currently escalation targets.
Therefore a successful ordinary task cannot guarantee all roster entries will
run. Record unused entries and their reasons. Never manufacture failures,
exhaust subscriptions, inflate the task or coach a rerun to fill the table.
Test conditional paths separately with deterministic fault injection, clearly
labelled as such; it is not natural live coverage. Broad natural coverage is
an aim, not a promise or a reason to change existing roles.

## Blindness boundary

The current Codex and Claude conversations have seen the earlier work and
cannot truthfully claim to be blind. They are the experiment designers.
The application solver must be a fresh session with no resumed/forked history,
prior acceptance solutions, repair logs, scoring rubric or routing expectations.

Freeze a neutral application brief, source snapshot, input fixtures, evaluator
checks, runtime configuration and budgets before launch. Hash both solver and
examiner bundles. The solver receives only its source/brief/test contract and
ordinary Quadratus operating policy; the examiner bundle remains inaccessible.
Use an actual filesystem isolation boundary with minimal mounted input, not
just another cwd or read-only snapshot, which does not restrict host reads.
Inspect inherited vendor instructions/memories/plugins and verify a withheld
canary cannot be read. Do not remove legitimate application requirements.

Codex owns runner/control changes; ask Claude through the shared review lane
to check protocol and prepare held-out checks before viewing solver output.
Both reviewers already know the earlier application; independence must be
limited to the fresh solver, not attributed to the reviewers themselves.
No manual application edits, hints or orchestration changes during the scored
attempt. Save an unsuccessful first attempt before repairing the harness.

## Candidate application brief (not yet frozen)

Add a read-only CSV clip-manifest import preview to GameTape. Validate fields
and time ranges, report malformed and duplicate rows, safely display unusual
clip names, and provide keyboard-operable results. Include representative
fixtures, automated checks and a short format-compatibility note referencing
the CSV specification. Do not import or rewrite media.

This bounds execution to text data and a small UI while offering real parsing,
source reading, standards lookup, implementation, testing and review work.
Do not add per-model errands to this brief. Judge whether decomposition and
helper use are sensible; unused cheap helpers are evidence to assess, not
permission for manual dispatch.

Proposed initial ceiling: 15 minutes, 24 total harness invocations including
workers/reviews/retries, at most two simultaneous helpers, and 500,000 reported
tokens including cached input. These are ceilings, not targets. Preflight the
actual enforcement before launch. Token usage arrives after a call and may be
unknown, so it cannot be advertised as an exact prepaid cap: stop at the first
reported crossing or unknown usage, with an independent wall-clock watchdog
for in-flight work. No automatic continuation or paid API fallback. Report
cached/uncached/output separately where the vendor supplies them.

## Sol delegation defect: verified, not yet fixed

`quadratus/delegation.py` and published native-delegation evidence show Sol
using vendor-native `spawn_agent`, outside WorkerPool. One recorded request
uses `fork_turns: all` without a model override; the child is Sol. Current
CODEX_SPEC has no disable setting. Local codex-cli 0.154.0 reports multi_agent
true; `codex -c features.multi_agent=false features list` reports false.
That is local configuration proof, not yet live proof that no child can run.

The earlier claim that the harness cannot forbid native delegation is too
broad. Official configuration documents the switch, and subagents inherit the
parent model when no override is supplied:
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/agent-configuration/subagents

Required next implementation: disable native Codex spawning on Quadratus
calls and require helpers to pass through WorkerPool. Enforce this after
operator extra arguments or reject conflicting overrides; do not merely add
a prompt instruction or a default child model that can be overridden.
Verify writable, read-only and restricted calls, preserved tool/write grants,
worker budgets and actual absence of native spawning in a bounded probe.
Keep native telemetry so a control failure remains visible. Review equivalent
vendor-native escape paths before claiming a run-wide spend boundary.

Interpret the operator's one-Sol rule as one Sol parent per assigned unit of
work, with no Sol child fan-out. Repeated existing test/review calls are not
new helper seats. OpenAI helpers use Luna for small errands and Terra for
harder ones; retain existing cross-vendor errand choices and senior verification
roles. The current worker tree already maps Luna to Terra when demanding.
Any broader change to that routing policy must be explicit and documented.

## Execution order

1. Reviewed PRs #10 and GameTape #2 merged on September 15 into their respective
   workflow branches (not main): Quadratus a9e1b79, GameTape ecaef0d.
2. Implement/review the native delegation boundary and verify restricted Grok
   behavior with a small probe. No large acceptance before these controls.
3. Freeze and isolate the blind task and enforce budgets; run once uncoached.
4. Publish correctness, justified routing, all-model coverage and usage as
   separate results, including missing coverage and uncertainty.
5. Repair observed failures as one bounded batch; media locking remains separate.
