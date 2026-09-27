# Workflow map and whole-journey matrix (phase 0)

Phase 0 of the shared workflow plan agreed on #25 (5853881701 to 5853910988,
initiated in 5856716643), amended by Codex's review (5856768605, folded in
below). No engine behaviour changed in phase 0. Phase 1's status is in
section 10. This map
is the reference that phases 1 to 3 are measured against. Line numbers are
from `6382cf2`.

## TL;DR

- **One real path, several side paths.** A task runs through contract checks,
  a lead draft, review and revision, the gate, the design check, settlement and
  DONE. That skeleton exists and works.
- **What a task achieved is scattered across many fields.** Its outcome lives in 12 session
  attributes, 9 exception classes and 3 `stop_reason` prefixes. DONE is
  computed in two places from different inputs.
- **Every failed check is treated as a product defect.** The gate can't tell
  "tests ran and failed" from "the runner never started", and there is no
  structured test report anywhere.
- **Twelve concrete gaps** turned up while mapping (section 6). Some are real
  defects, such as a turn cap escaping on two paths and stale state read
  across tasks. They go into the matrix as journeys, not as side fixes.
- **The matrix has 38 journeys** (section 7; J9 is split into J9a and J9b, and J32 to J38 were added from review). Each one names what the current
  engine does, what the agreed plan requires, and the phase that changes it.
  Phase 1 adds no behaviour: it records a typed outcome beside today's
  decisions and asserts they agree on every whole-run replay.

## 1. The journey as the code runs it today

| # | Stage | Owner | Calls (role) | Mandatory artifact | Code |
| --- | --- | --- | --- | --- | --- |
| 0 | Run start: source exclusions, dependency baseline, optional plan gate | harness | orchestrator (plan) | `dependency_identity` baseline | `_run_tasks` 2488-2507 |
| 1 | Name a task: metadata, SCOPE, COVERS, RESOLVES, capture, CONTINUES | orchestrator; harness validates | orchestrator; requirements-review once | TaskSpec + scope | `next_task` 2245, 2551-2594 |
| 2 | Pre-dispatch checks: policy, scope present, capture declared, capability | harness | none | `task-scope`, `task-needs` | `run_task` 1796-1818, `_capture_problem` 3505 |
| 3 | Lead draft, serving FETCH / CONSULT / WORKER | lead | lead, consultant, worker:* | `draft`, edits in the tree, CHANGED line | `_draft_with_channels` 1577 |
| 4 | Scope measurement | harness | none | `scope_reports` | `_assess_scope` 1928 |
| 5 | Cheap gates, if declared | harness, then lead | gate-fix | `checks[]` | 1930-1941 |
| 6 | Review, revision, blocking recheck (≤ `max_fix_cycles`) | collaborators, lead | collaborator, revision, recheck | `review:*`, `revision` | 1955-2030 |
| 7 | Integration gate plus fix (≤ `max_gate_fixes`, shared per task) | harness, then lead | gate-fix | `checks[]` with receipts | `_run_integration_gate` 3976 |
| 8 | Design verification: capture, qualify, one design-fix, cross-vendor review | harness, lead, reviewer | design-fix, design-review | `design_checks[]`, evidence set, `findings[]` | `_check_design` 2691 |
| 9 | Close-out | lead | closeout (one turn, no tools) | TaskSummary | `_close_out` 4074 |
| 10 | Settle: RESOLVES, COVERS, partial-task bookkeeping, stop checks | harness | none | `findings[].status`, requirement status | 2600-2643, `_settle_resolution` 3602 |
| 11 | DONE: findings block, requirements review and audit, dependency check | harness, auditor | auditor, requirements-review | `requirements` record | 2531-2550, `_requirements_satisfied` 3137 |
| 12 | Task cap reached | harness | orchestrator (confirm), auditor | as 11 | 2644-2664 |
| 13 | Run end: dependency re-check, finalise findings | harness | none | `result.json`, `report.md` | `run` 2445-2485, `project_run._run` |

**Side paths:**

- **Security** (`_run_security_task` 2114) replaces stages 3 to 9.
  - It uses a worker lead, the gate, a cross-family verifier, and at most one
    security-fix from the same gate-fix allowance.
  - There are no collaborators, no scope measurement and no design check.
- **Parallel batch** (`_run_batch` 2917) runs children with no gate and no
  ledger, merges in-scope files, then runs one merge gate.
- **Turn cap** (`_close_turn_limited` 2048) ends a task after stage 3 or 6 with
  no gate and no close-out, and hands a CONTINUES back to the orchestrator.

## 2. Where "what happened" lives today

| Fact | Where it is recorded | Read by |
| --- | --- | --- |
| Open problems (free text) | `open_findings`, 12 writers | batch stop, serial stop, DONE, cap |
| Check outcome | `checks[]` (single writer, 4013) | stops, DONE, harness capture (`checks[-1]`), settlement |
| Design evidence | `design_checks[]`, `_design_unverified` (never cleared) | `_name_findings_stop` |
| Audit debt | `findings[]` plus requirement status | DONE, settlement, orchestrator prompt |
| Capped and partial tasks | `turn_limited`, `turn_limited_records`, `_partial_tasks` | DONE (`_unresolved_partial`), breaker |
| Scope | `scope_reports` | result.json |
| Requirements | ledger status, `requirement_reviews`, `requirement_audits` | DONE |
| Dependency identity | `dependency_watch.record` | every edge, result.json |
| Run stop | exception class name, or `stop_reason` text | `result.json.error` |

**Stops that record no reason** (`error == ""`, `completed == False`):

- requirement reopens exhausted (2542);
- open findings with no design entry (2527, 2633);
- cap reached with the goal unconfirmed or requirements open;
- plan gate declined.

## 3. Failure sources and today's routing

| Source | Signal | Today | Precedence today |
| --- | --- | --- | --- |
| Provider refusal | `ProviderRefusal` | preserved and stops; close-out refusal gets a harness record | never retried or rerouted (Fleet disables the fallback) |
| Relevant permission denial | `CapabilityUnavailable` from runtime | stop after the one call, even at the cap | above the cap |
| Scope overrun or unmeasurable scope | `PartialWorkStopped` | stop, work preserved | above everything but refusal |
| Dependency tree changed or unavailable | `DependencyTreeChanged` / `…Unavailable` | stop at the next edge; latched | integrity |
| CHANGED mismatch | `PartialWorkStopped` | stop, reply kept | integrity |
| Timeout after writes | `PartialWorkSuspected` becomes `PartialWorkStopped` | stop, tree inspected | integrity |
| Turn cap | `TurnLimitReached` | lead or revision: capped-task path; gate-fix or design-fix: attempt spent | below denial |
| Transport error | retried by `_retryable` (at most 4, backoff), then `ProviderError` | lead gets one recovery on a fresh lead if the tree is unchanged | below cap |
| Window exhausted | `WindowExhausted` | one re-seat for the orchestrator; stops for others | |
| Run budget | `RunBudgetExceeded` (latched) | stop; responses kept | |
| Gate failed (any cause) | receipt `failed` / `error` / `blocked` | **always** a gate-fix, the same prompt for every cause | none: environment and product look alike |
| Capture failed | `capture_task` string / `integrity` record | one design-fix, whatever the cause | none |
| Product overflow in an audit | `product.overflow` | audit debt `F<n>` | |
| Reviewer BLOCKING | review text | revision; open finding if still unresolved | |
| Evidence not delivered | `EvidenceNotDelivered` | becomes a synthetic BLOCKING | |
| Operator question | `OperatorInputNeeded` | stop | |
| Orchestrator stall | `RunStalled` | stop | |

**The gate today:**

- It parses test counts from text only. There is no JUnit, TAP or JSON report.
- A runner that crashes before any test reads the same as failing assertions (`failed`, "nonzero exit").
- A missing binary is the only clear infrastructure signal (`blocked`).

## 4. Authority and attempt budgets (unchanged by this plan)

| Budget | Value | Scope | Where |
| --- | --- | --- | --- |
| Run calls / reported tokens / wall / workers | 120 / 6,000,000 / 7,200 s / 2 (operator's run) | run | `RunLimits` |
| Task slots | 20 (`max_tasks`) | run; a batch costs one per task | `_run_tasks` |
| Lead rounds | `lead_max_turns` (14 in the trial) | lead, revision, gate-fix, design-fix | `LEAD_CAPPED_ROLES` |
| Close-out | 1 turn, no tools, ≤ 60 s, 1 attempt | per task | runtime 512 |
| Fix cycles (revision plus recheck) | 2 | per task | `max_fix_cycles` |
| Gate fixes | 1 | per task, shared by gate-fix, design re-gates and security-fix | `max_gate_fixes` |
| Design fix | 1 | per design task | `_check_design` |
| Security verification rounds | 2 | per security task | 2203 |
| Consecutive capped tasks | 1 | run | `max_turn_limited_in_a_row` |
| DONE reopens | 3 | run | `max_requirement_reopens` |
| Task-naming corrections | 3 (a separate counter on the same limit) | run | `_covers_corrections` |
| Operator ASKs | 3 per decision | decision | `_MAX_ASKS_PER_DECISION` |
| FETCH / CONSULT | 3 / 2 | per draft | config |
| Worker errands | 12 per task, 4 concurrent, 4 failures close the channel, 3 steps, 50k tokens | per task | `WorkerBudget` |
| Transport retries | 4, exponential backoff | per call | `max_retries` |
| Lead recovery | 1, unchanged tree only | per task | 1887-1924 |

## 5. Proposed shape

These are the plan's words made concrete. Names are provisional until phase 1
review.

**`TaskContract`**, fixed at dispatch (phase 2):

- `intent`: audit / implementation / repair.
- `covers`, `resolves`, `continues`.
- `scope`, from the existing `TaskScope`.
- `authority`: write grant, edits none.
- `capabilities`: declared probes it depends on.
- `required_checks`: ids from the gate plan.
- `intended_state`: capture path and steps, or the finding's measured state for a repair.
- `acceptance`.
- `owner`: the lead key.
- `allowed_next`: the transitions this intent permits.

**`TaskOutcome`**, one per task (phase 1):

- `stage_reached`.
- `partial_edits`: changed paths and lines.
- `source_before` / `source_after`, and the dependency status.
- `check_receipts`.
- `evidence`: validity, delivered files and hashes, reviewer, verdict.
- `unresolved`: requirement and finding ids.
- `attempts`: gate-fix, design-fix, revision cycles, lead recovery, worker failures.
- `primary`: one precedence class.
- `secondary`: all others, with diagnostics.

**Precedence for `primary`**, highest first:

1. refusal;
2. security;
3. integrity (scope, dependency, CHANGED, partial-work, evidence tampering);
4. relevant denial;
5. operator or environment (preflight, capability lost, runner or setup error, ASK);
6. budget;
7. cap;
8. transport;
9. product (structured attributable failure);
10. invalid proof;
11. unverified (delivery, review or settlement missing);
12. clean.

**Rules for using the precedence** (amendment 1):

- **`primary` is for reporting.** It never licenses ignoring another active
  constraint.
- **Stops dominate repair.** Refusal, security, integrity and relevant denial
  outrank repair wherever they occur, worker errands included.
- **A latched budget forbids another call** whatever ranks first.
- **Security acceptance is not a security failure.** An accepted security task
  can end clean.

**History versus terminal blockers** (amendment 3):

- A fact is history.
- A recovered failure keeps its record and stops blocking once every
  mandatory edge and inherited debt is satisfied: a transport error the lead
  recovered from (J25), a successful repair (J2), a recapture (J8), or a
  CONTINUES that finished (J22).
- A cap or budget that ends the work stays incomplete.
- `stages` records what was attempted; `edges` records which mandatory edges
  were satisfied (amendment 5).

**Ownership** (amendments to the names):

- `TaskContract.scope` and `.authority` reference grants that were already
  validated; they never mint authority.
- The requirements ledger stays authoritative for requirement and finding
  identity and settlement. Outcomes carry references to it.
- An outcome may be finalised by runtime-owned updates while its dispatch
  contract stays fixed.

**Structured failure attribution** (phase 3):

- A check declares its report format: JUnit XML for pytest (`--junitxml`) or a
  TAP/JSON reporter for node, written to a harness-owned path.
- Only a parsed, attributable assertion failure with valid setup and no
  dominating signal routes to product repair.
- Everything else routes to operator handoff with bounded diagnostics.

## 6. Gaps found while mapping (each becomes a journey)

- **G1 (1924):** the lead's second draft after recovery isn't wrapped, so a
  turn cap there escapes as a raw `TurnLimitReached`.
- **G2 (2140):** a turn cap on the security draft likewise escapes.
- **G3 (2036):** the full gate's fix text is discarded, unlike the cheap-gate
  and security paths. Harmless for editing runs; the close-out still sees the
  transcript.
- **G4 (3549):** harness capture reads `checks[-1]` without scoping it to the
  task. A task with no gate inherits an earlier task's failed check.
- **G5 (3038):** the merge gate runs with the parent's stale `_gate_fixes_used`.
- **G6 (2946):** a batch sent back spends its slots and doesn't count as a
  correction.
- **G7 (2173):** the security prose verdict uses a substring test, so
  "no BLOCKING findings" counts as a finding. Codex's branch already has
  `_has_security_finding`; take that one.
- **G8 (3706, 2717):** `_design_unverified` is never cleared, so a stop can
  name an earlier task's problem.
- **G9:** four stop paths leave `error == ""` (section 2).
- **G10:** the gate can't tell a runner crash from an assertion failure.
- **G11 (1004):** `design-fix` gets the reviewer role packet, not the lead's.
- **G12 (DONE, 2546 / 2657):** any failed `checks` entry blocks DONE, even
  when a later check in the same task passed. A task whose gate failed, then
  passed after its design-fix, ends the run incomplete with a blank error.
  Found by the phase 1 prospective case (checks `[False, True]`, `error ""`).

## 7. Whole-journey acceptance matrix

**Columns:**

- **Today** is what `6382cf2` does.
- **Required** is the agreed plan.
- **Phase** is where Today changes. P1 journeys change nothing; they add the
  typed outcome and a parity assertion.

**How journeys are tested in each phase** (amendment 4):

- In P1 a journey asserts today's route, known-wrong routes included, and
  never decides them.
- Where the plan changes a route, the prospective expectation sits beside it
  as a strict xfail naming its phase, so it must flip when that phase lands.
- A changed-route test must be red on the immediate pre-change commit of its
  own phase.
- J2 needs no structured report in P1.

**Every journey asserts:**

- the calls made (by role);
- the final `primary` outcome and the `result.json` fields;
- the exact evidence identity where evidence exists;
- the preserved partial work.

**Red:** a journey whose route changes must fail on the prior commit.

| id | Journey | Today | Required | Phase |
| --- | --- | --- | --- | --- |
| J1 | Clean completion: draft, review, gate, close-out, DONE, audit met | completed | `clean`; completed | P1 |
| J2 | Product repair succeeds: structured assertion failure, gate-fix, gate passes | gate-fix, then pass | `clean`; repair attempt recorded | P1 (P3 requires the structured report) |
| J3 | Product repair fails: gate still failing after the allowance | open failure, incomplete, `error ""` | `product`, unresolved; stop names it | P3 (G9) |
| J4 | Runner crashes before any test (exit ≠ 0, no summary) | gate-fix sent to the app | operator handoff, **no gate-fix** | P3 |
| J5 | Runner has no structured report and fails | gate-fix | operator handoff with diagnostics | P3 |
| J6 | Capability lost mid-suite (some tests ran, then the browser died) | gate-fix | operator handoff | P3 |
| J7 | Preflight capability probe fails | no probe exists | stop before any model call | P2 |
| J8 | Evidence invalid, source and capability fine: recapture intended state | one design-fix | `invalid proof`, bounded recapture | P1, then P3 |
| J9a | Evidence missing, stale or invalid | design-fix, then unverified | `invalid proof`, then unverified; never approved | P1 |
| J9b | Evidence tampered, or its identity mismatches (sha, source, fixture) | design-fix, then unverified | `integrity`; never recapture or product repair (amendment 2) | P3 |
| J10 | Checks pass, reviewer response missing or not delivered | synthetic BLOCKING, open finding | `unverified`; not completed | P1 |
| J11 | Harness capture after an earlier task's failed check (G4) | capture skipped wrongly | scoped to this task | P3 |
| J12 | Audit finds overflow, becomes debt `F<n>` | debt recorded | same, typed | P1 |
| J13 | Repair resolves `F<n>` with verified, approved renders | resolved | same, typed | P1 |
| J14 | Repair fails to resolve | FindingsUnresolved | same | P1 |
| J15 | Scope overrun | PartialWorkStopped | `integrity`, preserved | P1 |
| J16 | Dependency tree changed or unavailable | DependencyTreeChanged | `integrity`, above cap and product | P1 |
| J17 | Provider refusal (lead, fix, close-out) | stop or harness record | `refusal`, never rerouted | P1 |
| J18 | Relevant denial at the cap | CapabilityUnavailable | `denial`, above cap | P1 |
| J19 | Security task: verifier accepts / rejects / refuses | open finding on reject; G7 text bug | `security`; G7 fixed | P1, then P3 (G7) |
| J20 | Turn cap on lead / revision / gate-fix / design-fix | capped path or attempt spent | `cap`; partial checkpoint | P1 |
| J21 | Turn cap on the recovery redraft or security draft (G1, G2) | raw TurnLimitReached | `cap`, capped path | P3 |
| J22 | Capped task, then a CONTINUES task that completes | continuation | new contract inherits intended state | P2 |
| J23 | Two caps in a row | TurnLimitBreaker | `cap`, breaker | P1 |
| J24 | Budget exhaustion mid-task | RunBudgetExceeded, in-flight kept | `budget`; edits and outcome kept | P1 |
| J25 | Transport failure, lead recovered once on an unchanged tree | recovery | `transport`, one attempt | P1 |
| J26 | Operator ASK with no channel | OperatorInputNeeded | `operator`, terminal handoff | P1 |
| J27 | Unknown failure (unclassifiable) | varies | operator handoff, no edit | P3 |
| J28 | Requirements audit disagrees at DONE: reopen, then stop | reopens, then `error ""` | named stop | P1 (G9 in P3) |
| J29 | Parallel batch: one child merged, one out of scope, merge gate | merged / unmerged / gate | per-child outcomes; G5 and G6 fixed | P1, then P3 |
| J30 | Worker errand: optional failure vs required failure | error-as-result | optional recorded; required fails its edge | P2 |
| J31 | Design-fix role packet (G11) | reviewer packet | lead packet | P3 |
| J32 | Preflight passes, then the source or dependency tree changes, or the identity becomes unavailable | dependency stop | `integrity` above everything but refusal and security | P1 (dependency), P2 (preflight) |
| J33 | Compound: product assertion failure plus a setup failure, denial or latched budget | gate-fix, or the stop | the dominating stop; **no repair call** | P3 |
| J34 | CONTINUES carries unresolved requirement and finding ids | ids re-listed | carried once, never double-counted or settled early | P2 |
| J35 | Parallel child debt | child finding kept open | the merge gate cannot close unresolved child debt | P1, then P3 |
| J36 | CHANGED report mismatch | PartialWorkStopped | `integrity`, reply kept | P1 |
| J37 | Transport timeout after writes | PartialWorkSuspected, then Stopped | `integrity`, tree inspected | P1 |
| J38 | A check fails, then passes later in the same task (G12) | incomplete, blank error | the recovered failure is history; DONE can stand | P3 |

J1 to J31 are whole-controller replays through `tests/lifecycle/harness.py`
(only the CLI launch is faked). Existing lifecycle cases already cover parts of
J1, J8 to J10, J12 to J18 and J20 to J24. Phase 1 adds the `TaskOutcome`
parity assertion to those and fills the rest.

## 8. Retirement list (each is removed in the phase that replaces it)

| Legacy input | Replaced by | Removed in |
| --- | --- | --- |
| `open_findings` as a decision input | `TaskOutcome.unresolved` / `primary` | P3; kept as report text |
| `_partial_tasks`, `turn_limited`, `_design_unverified` | derived from outcomes | P3 |
| `stop_reason` strings | typed terminal outcome (same names) | P3 |
| Generic gate-fix for any failure | structured attribution routing | P3 |
| 11 `is_design_task` applicability checks | `TaskContract.required` computed at dispatch | P2 |
| DONE computed twice (2546, 2657) | one function reading outcomes and ledger | P3 |

Each temporary parity field added in P1 names its P3 removal step in code.

## 9. Not in this plan

- Live runs; P4 is Codex-owned after independent review.
- Profile activation.
- New roles, authority or limits.
- Patching preserved outputs.
- The interview / empty-repository stage.

## 10. Phase 1 status

**What phase 1 adds, and what it doesn't change:**

- `quadratus/outcome.py` holds `TaskOutcome`, `RunOutcome`, the precedence,
  `classify` and `parity`.
- The session records facts at the points where it already acts:
  - `_open_finding` wraps every open-finding writer, with the class today's
    route implies;
  - `_stop_with` wraps every `stop_reason`;
  - run-level facts cover exceptions, silent stops (`legacy ""`), DONE and the
    task cap;
  - task facts cover the gate, design evidence, caps and recovery;
  - CONTINUES recovery mirrors `_partial_tasks.discard`.
- **No decision reads the typed record.** `result.json` gains `workflow`
  (`tasks`, `run`, `parity`).
- **Parity** checks three things:
  - completion agrees;
  - an incomplete run's legacy error name is the one its typed stop recorded;
  - every task the legacy history closed has a typed outcome closed the same way.

  It is observational: a disagreement is recorded, never raised in a run.
- **Every whole-controller replay** in `tests/lifecycle` asserts parity
  (`harness.run`).

**Temporary duplicates and where they go:**

| Field | Removed in |
| --- | --- |
| `TaskOutcome.closed_as` (mirror of history) | P3, when history reads outcomes |
| `_open_finding` writing both the list and the fact | P3, when `open_findings` stops being a decision input |
| `_stop_with` writing `stop_reason` | P3, when the terminal outcome replaces `stop_reason` |
| `_recover_continued` beside `_partial_tasks` | P3, with `_partial_tasks` |
| `legacy_route` product facts from any failed check | P3, structured attribution |

**Journey coverage in P1:**

- `tests/lifecycle/test_workflow_outcomes.py` pins the typed record for
  J1, J2, J3, J9a, J10, J15, J16, J17, J18, J20, J22, J23, J24, J25 and J26.
- Strict xfails: J4 (runner crash, no gate-fix) and J38 / G12.
- Every other lifecycle replay (184 cases, among them audit debt J12 to J14,
  harness capture, dependency identity J16 and J32, caps J20, and parallel
  batches where present) asserts parity.
- J27, J29, J30, J33, J34 and J35 get dedicated cases in the phase that
  changes them.
