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

| Legacy input | Replaced by | Removed in | Status at aa03e72 |
| --- | --- | --- | --- |
| `open_findings` as a decision input | `TaskOutcome.unresolved` / `primary` | P3; kept as report text | **still decides**; see P3.4 |
| `_partial_tasks`, `turn_limited`, `_design_unverified` | derived from outcomes | P3 | **still decide**; see P3.4 |
| `stop_reason` strings | typed terminal outcome (same names) | P3 | **still decides**: written beside the typed stop, it gates whether `_name_findings_stop` names a stop and is read by `project_run` for the result error; see P3.4 |
| Generic gate-fix for any failure | structured attribution routing | P3 | **retired** in P3.2 |
| 11 `is_design_task` applicability checks | `TaskContract.required` computed at dispatch | P2 | **still decide** (11 call sites; the contract only records disagreement); see P3.4 |
| DONE computed twice (2546, 2657) | one function reading outcomes and ledger | P3 | **still two sites**, both ending in `_guard_completion`; see P3.4 |

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

**Correction round after Codex's review of 7cbd35f (5857029459):**

- **The owner and work facts are measured, not left as placeholders.**
  - `lead` is the lead actually selected, including after recovery and the
    security worker.
  - `source_before` / `source_after` hold the fingerprint, or an explicit
    `"n/a"` or `"unavailable"`.
  - `partial` holds the changed paths and lines against the task's start, or
    an explicit uninspected note; it never implies "no edits".
  - `dependency` holds the run's dependency identity status.
  - `unresolved` holds ledger references: finding ids still open after
    settlement, and covered requirement ids not covered or met.
- **Every executed check is an attempt record:**
  - the receipts in full (id, status, reason, tests, returncode, cached,
    source and runner hash);
  - the output kept as a `check-output` artifact;
  - the source it ran against.

  A failed attempt adds a `legacy_route` product fact. A failure the same
  gate's fix repaired is marked recovered, and a failure still standing at
  the end stays active (legacy G12).
- **A handled close-out refusal** is a historical, non-terminal `refusal` fact
  with its stage, category and the `closeout-refused` artifact id.
- **Completeness sits beside parity.** `parity(...)` returns `complete` and
  `missing` from `missing_facts`:
  - a closed task must carry its owner, source identity before and after,
    work and dependency status;
  - a closed task that reached its checks must carry an attempt.

  The replay harness asserts both agreement and completeness, and unit
  negative controls show routing agreement alone is not a complete record.
- **Dedicated observations** (`tests/test_workflow_session.py`):
  - security accept is clean, with the worker as owner;
  - security reject is `security`;
  - a merged parallel batch records each child with its owner;
  - an unmerged child is `integrity`, and its debt stays open.

  G7 ("no BLOCKING findings" read as a finding) is a strict xfail for P3.


## 11. Phase 2 status

Pending Codex review. The readiness correction was cleared in 5857729578;
the security edge (0cd4dcb) and the owner/debt corrections (82c2287) are
under review. No legacy decision changes route in P2. The
one new route is operator-declared readiness probes: absent a declaration,
nothing about a run changes.

**`quadratus/contract.py`, the `TaskContract`:**

- Built once in `run_task` at dispatch and frozen for that invocation.
- `owner` is bound at the selection point (the lead in `_run_task`, the excursion worker in `_run_security_task`), before the first model call. A task that stops before selection has no contract, and that is recorded as complete.
- Its intent is validated as audit / implementation / repair.
- It references existing grants and never mints authority:
  - `authority.write_grant`: operator or none;
  - `authority.edits`: none / `scoped:<max_lines>` / unscoped.
- Its fields:
  - `required` (checks, design_evidence, design_review, security_verification, settlement), derived from the same facts the legacy applicability decisions read;
  - `required_checks`, the gate ids;
  - `intended_state`, the harness capture page and steps, plus the RESOLVES findings as measured;
  - `acceptance`;
  - `capabilities`, the probes that passed;
  - `allowed_next`, the stages the contract expects;
  - a digest.
- **Consistency:** `_contract_agrees` records any disagreement at the security branch, the full gate, `_check_design` and the design review. The legacy decision still decides. P3 switches these sites to the contract and removes the duplicates.
- **CONTINUES:** a continuation gets a new contract, with a different digest. Its `inherits` carries the predecessor's intended state, `open_at_close`, changed files and close state.

**`quadratus/readiness.py`, operator readiness probes:**

- Declared with `--readiness` / `run_project(readiness=)` and validated before the run:
  - at most 8 probes;
  - at most 120 s each;
  - no shell syntax;
  - unique ids;
  - no extra fields.
- Run once after the dependency baseline and before the plan gate and any model call, under `os.environ` in each probe's own process group.
- Each probe gets a harness-owned scratch directory and bytecode prefix, removed afterwards. Its output is kept as a 2,000-character tail and a `readiness-output` artifact.
- The first failure ends probing and stops the run with `CapabilityProbeFailed`: class operator, zero model calls.
- A probe that changes source gives `PartialWorkStopped` (integrity).
- Passing proves readiness only. It is never a check or acceptance, and there is no retry, replay or allowance.

**Delivery and response edges:**

- `delivered` records the files and hashes as bound (`TaskOutcome.delivery`), or false when the renders were refused, changed or not delivered.
- `reviewer` is true only for an APPROVED response with no BLOCKING line.
- `unsatisfied` lists the contract's mandatory edges that were not satisfied. A missing delivery or response keeps the task incomplete; legacy already stops on it.

**Security verification:** the excursion records draft, checks, verification and closeout stages. The `verification` edge is true on an accepted verdict and false on a structured-reply error or a final reject.

**Owed references on every exit:** `open_at_close` is a snapshot of the ledger's open COVERS/RESOLVES references taken on every exit (normal close, cap, interruption, exception). The normal close takes it after `_mark_covered`. A merged batch child's snapshot is refreshed after coverage; an unmerged child is `stopped:unmerged` with an integrity fact. The ledger stays authoritative.

**Check attempts:** each attempt keeps its receipts and an `output_artifact` stored with author "harness". A failed store is kept as `output_artifact_error` and counts as a missing fact.

**Completeness:** a task must carry its contract, with an owner equal to the lead unless a lead recovery was recorded, and any contract/legacy mismatch is a missing fact. The replay harness asserts this on every run.

**J30 (required errands):** nothing declares an errand required today. Every errand is optional, and its failure stays an errand result. A required-errand declaration would be a new contract field that no current source fills; it's noted, not invented.

**Tests:**

- `tests/lifecycle/test_workflow_contract.py`: 28 cases.
- `tests/test_readiness.py`: 7 real-process cases, including a SIGTERM-ignoring descendant, a leak after a clean exit, continuous output and 20 MiB of output.
- **Red on 3efac78:** 11 of the 19 fail because the features don't exist there. The other 8 are declaration-validation cases for the new module, which has no prior counterpart.
- **Red on 0cd4dcb:** 5 of 5 owner/debt cases fail (empty snapshots on capped COVERS/RESOLVES and interrupted exits; a contract before selection with no owner).

## 12. Phase 3 status

P2 cleared by Codex at 9c6024b (5857957044). Each P3 commit changes one
decision site and carries its own red control on the commit before it.

**P3.1, completion guard (b2446c3, corrected in 6ca0cb4; cleared by Codex
in 5858419581).** Both DONE sites (the DONE reply and the cap's goal
confirmation) now also ask the typed record, and only after the legacy inputs
already said complete. `outcome.completion_blockers` lists:

- a task that never closed;
- any missing fact, including a malformed record: an unsupported dispatch
  state, an empty owner or reason, an incomplete owner change, a contract
  naming another task, or a missing or mistyped requirement declaration;
- a mandatory contract edge not satisfied and not discharged by fact;
- an active terminal fact on any task;
- a reference the ledger still owes, read live through `_open_refs`.

Only an unmet edge is ever discharged, and only by fact. A later task in the
CONTINUES chain, recorded complete with no active fact, must have satisfied
that same edge under a contract requiring it; for evidence, delivered and
reviewer it must also carry the predecessor's own declared intended state.
Settled audit debt (findings that audit recorded, all resolved) discharges
only the audit's evidence, delivered and reviewer edges, never checks,
verification or settlement. An unknown, later or cyclic CONTINUES reference
is a blocker. Any blocker turns completion into `CompletionUnproven` (typed
`unverified`) with no model call.

**P3.2, gate attribution (0a4ae0d, privacy f2b664d).** Retires "generic gate-fix for any
failure" (section 8) and closes J4, J5, J6 and J33 (G10).

- The only supported report producer is harness-owned:
  `quadratus/_gate_producer/quadratus_gate_report.py`, a pytest plugin that
  imports nothing from quadratus. A check declares it with
  `--quadratus-report={report}`; the harness then substitutes a path it owns
  outside the project, loads the plugin for that one invocation
  (`PYTHONPATH`, `PYTEST_PLUGINS`), sets a nonce, reads the JSON (at most
  1 MiB) and removes the path.
- The report records facts pytest already has: exit status, collection
  errors, per-phase counts, and for each failed phase its `when`, its type
  name (shown, never trusted) and whether its type *is* `AssertionError`.
- `integration.attribute` allows product repair only when every failed
  required check exited 1 with reason `nonzero exit`, its report is this
  invocation's (nonce, producer), exit status 1, no collection or
  setup/teardown error, a consistent and untruncated record, and every
  failure is `when=call` with `assertion` true. Anything else raises
  `CheckUnattributable` (operator) with the reasons, the returncode and the
  output artifact, and makes no repair call.
- A check with no declared report is never repaired. Other runners have no
  producer yet, so their failures are always handoffs. The operator
  profile's checks are P4 work.
- `redact_command_paths` now also redacts the relative and bare spellings of
  a gate's command files, which pytest prints for a grader outside the
  project. It matches only the private file's own trailing path components,
  so a public file sharing the basename in another folder keeps its
  diagnostics; a bare shared name is redacted. Commands are displayed with
  shell quoting so a path with spaces is still found; the operator record
  keeps it exactly.

**P3.3, one decision site per package.** Each carries a behavioural red on
the commit before it and was cleared by Codex before the next began.

| Package | Commit | Site |
| --- | --- | --- |
| G12 / J38 | 0550dde | standing check failures judged per task at DONE |
| G7 | 1920af8, 46bf2fe | security verdict findings read as anchored markers |
| G4 | ca69c13 | capture eligibility from the current task's own checks |
| G9 | aa8adb9 | every silent stop named; an end-of-run dependency change outranks a cap |
| G1 / G2 | 28cbacc | a capped recovery redraft or security draft closes as capped |
| G5 | a291d88 | the merge gate has its own gate-fix allowance |
| G6 | c5b4eed | a sent-back parallel batch counts like a serial send-back |
| G8 | a582a26 | design debt bound to its own task; carried back from parallel children |
| G11 | dedb571 | a design-fix call carries the lead's role packet |
| J9b | 1126251, b5fa308, f1a704f | observed evidence identity mismatch is an `integrity` stop |
| J27 | aa03e72 | an unclassified worker failure is handed to the operator unchanged |

**P3.4, retirement status (documentation only; nothing is removed).** A
legacy input is removed only when the same journeys prove both parity and
failure behaviour with it gone; green typed tests alone are not that proof.
None of the seven below has that proof yet, so each stays and says why.

What already holds on every whole-controller replay (`harness.run`):
completion, the legacy error name and each closed task agree between the
legacy state and the typed record (`parity.agree`), and the record is complete
(`parity.complete`), which includes no contract/legacy applicability mismatch.
That is agreement on outcomes, not proof that a legacy input can go.

| Legacy input | Where it still decides | Typed counterpart | Controls that bear on it | Needed before removal |
| --- | --- | --- | --- | --- |
| `open_findings` | serial and batch stop after a task (`if self.open_findings or ...`), both DONE conjunctions, `_open_work` reasons, settlement's "closed with open findings" | every writer goes through `_open_finding`, which notes a fact of the route's class, except the batch: it merges each child's list (typed in the child's outcome) and appends an unmerged child's finding directly | J15 (`test_j15_...`), J10, G9 `test_named_stops.py`, settlement tests in `test_audit_findings.py` | the one direct writer (an unmerged parallel child) typed on every path; each decision site switched alone with a red; report text kept |
| `_partial_tasks` | `_unresolved_partial` at both DONE sites; `_open_work` reasons | `cap` facts, `_recover_continued`, CONTINUES discharge in `completion_blockers` | J20, J22, J23, `test_capped_drafts.py`, `test_completion_guard.py` CONTINUES cases | outcome-derived partiality proven equal on every CONTINUES shape, including the parallel merge path |
| `turn_limited` | names the capped tasks in the breaker's `TurnLimitBreaker` stop (the count itself is `_turn_limited_in_a_row`) | `cap` facts per task | J23 | the breaker's names read from consecutive `cap` outcomes, byte-identical on J23 |
| `_design_unverified` | the named design stop (`DesignUnverified`) | `invalid_proof` / `unverified` facts per task | `test_design_debt_binding.py` (G8), J9a, J9b controls | the stop's name and task binding derived from facts, on serial and parallel paths |
| `stop_reason` | written by `_stop_with` beside the typed stop; decides whether `_name_findings_stop` still names a stop (`not self.stop_reason`); read by `project_run` for the result error and by the open-finding annotations | the run's terminal fact (`legacy` name) | parity (legacy error name equals the typed stop's) on every replay; `test_named_stops.py` | the report and `result.json` error read from the typed stop, byte-identical on every replay |
| `is_design_task` applicability | 11 call sites (design brief, evidence, review, capture eligibility, RESOLVES, prompts) | `TaskContract.required` | `_contract_agrees` mismatches are missing facts on every replay | each site switched alone with a red, starting with `design_evidence` and `design_review`, which `_contract_agrees` already watches |
| DONE computed twice | the DONE reply and the cap's goal confirmation each compute their own legacy conjunction | `_guard_completion` / `completion_blockers` at both | `test_completion_guard.py` (17 cases), G9, G12 | one function for both, with the short-circuit order at the cap kept (the goal question only when no capped or audit debt) |

**Known gap, recorded here and closed in the next package.** An unmerged
parallel child appends to `open_findings` directly and notes a typed fact
only when the child has a `TaskOutcome`. `run_task` built the contract
before it appended the outcome, so an exception from `_build_contract` left
that one finding untyped. The run still stopped typed: such an exception is
fatal to the batch and re-raised. Closed by recording the outcome first
(see "P3.4 packages" below).

**Unproven limits (none is claimed as covered):**

- **Step-record forgery.** Malformed, mismatched or failed step records are
  invalid proof. Nothing establishes that a step record was forged, and J9b
  does not classify them.
- **J9b timing.** A mismatch present at the first evidence check spends no
  fix. One observed only after the one authorized design-fix comes after that
  fix.
- **J9b bytes.** The approved snapshot is kept as digests only; its bytes
  cannot be reconstructed from the `evidence-identity` record.
- **J27 in-flight call.** An open lead call cannot be interrupted. On the
  in-session bridge it is told to stop and the exception is re-raised when it
  returns; it can write before then. The harness diff and dependency checks
  still apply.
- **J27 coverage.** A harness bug that raises a declared type
  (`ProviderError`, `FanOutExceeded`, `RepeatedFailure`, `ErrandToolMismatch`)
  follows that declared route. `RunBudgetExceeded` from a worker keeps its
  pre-existing route.
- **G8.** The serial stale-name case is a defended invariant, unreachable in
  today's serial loop.
- **G11.** The control is at the policy-backed invocation seam. A replay
  without a repository policy cannot tell role packets apart.
- **Gate attribution.** Only the harness-owned pytest producer is supported;
  any other runner's failure is always a handoff.

**P4 entry criteria (P4 is Codex-owned):**

1. Every P3 package above independently cleared on its exact base.
2. An independent installed-wheel validation of the head commit, outside the
   source tree.
3. Integration and operator-profile review, including the profile's checks
   declaring the harness report producer where product repair is wanted.
4. The limits above accepted as limits for the live run, or closed first.
5. The existing bounds unchanged: 120 calls, 6 million reported tokens as a
   post-return stop threshold, 7,200 s internal and 7,500 s outer wall time,
   two concurrent workers, 20 tasks, unknown usage kept unknown.
6. No live call, profile activation or adoption into the 90cc5d9 execution
   tree before that review.

Overall P3 is not complete: the seven legacy inputs above still decide.

**P3.4 packages, one decision site each.**

1. *Pre-contract failures are typed.* `run_task` puts the task's
   `TaskOutcome` on the record, and clears the previous contract, before it
   builds the contract. An exception there reaches the existing handler: a
   fact of its class, `closed_as = stopped:<Exception>`, `dispatch =
   not_dispatched` with the reason, then re-raised unchanged. An unmerged
   parallel child's "not merged" finding now always has its typed fact. The
   stop, the sibling's merge and dispatch authority are unchanged
   (`tests/test_contract_failure_outcome.py`).
2. *`design_evidence` applicability from the contract.* `_check_design`
   decides "none", "disabled", "harness" or "self" from the task's own
   contract, fixed at dispatch, through `_required`. Today's live reading is
   still computed and handed to `_contract_agrees`, so a disagreement is a
   recorded mismatch and the run cannot count as complete. With no contract
   for the task the live reading decides, as before, and the missing
   contract is recorded. Configuration drift between dispatch and the check
   can no longer drop or add the evidence requirement
   (`tests/lifecycle/test_contract_applicability.py`). `design_review` is
   package 3.
3. *`design_review` applicability from the contract.* Whether
   `_check_design` requires the cross-vendor design review is the task's own
   `required.design_review`, fixed at dispatch, through `_required`; the
   live `design_cross_check` is still compared and a disagreement recorded.
   Collaborator selection and the prompt text still read the live setting
   and are unchanged. Drift between dispatch and the check can no longer
   drop or add the review (`tests/lifecycle/test_contract_applicability.py`).
4. *Dispatch preparation before the contract (prerequisite for `checks`).*
   With a repository policy, `_run_task_recorded` chose the task's gate and
   narrowed its scope after `run_task` had built the contract, so the
   contract named the previous task's gate (Codex confirmed, 5861089949).
   The declaration check, policy resolution and refusals, the operator path
   bound, scope narrowing and `task_gate` now run in `_prepare_dispatch`,
   after the outcome and its `source_before` are recorded and before
   `_build_contract`, so a task refused at dispatch keeps its source record
   (Codex, 5861147842). Nothing is run and no model is called there. The only observable difference: if
   contract building and preparation would both raise, preparation's error
   is now the one reported (`tests/test_dispatch_preparation.py`).

5. *`checks` applicability from the contract.* A task's mandatory gate is
   its own `required.checks`, fixed at dispatch, and runs the gate that
   contract was built with (held beside it as `_task_gate`); the live
   setting is still compared and a disagreement recorded. With no contract
   for the current task (the parallel merge gate) the configured gate
   decides and runs as before, with the same allowance. Explicit subset gates
   (the cheap view) are unchanged (`tests/test_checks_applicability.py`).

6. *`security_verification` as a floor.* `_run_task` takes the security
   route when the task's contract (fixed at dispatch) **or** its live
   classification says security, and records a disagreement. A security
   classification fixed at dispatch cannot be dropped by drift, and a live
   one can never be routed round. This **retains a live
   security-classification guard**; it is not retirement to a contract-only
   input, because following the contract alone would route a task now
   classified as security down the standard route (Codex, 5861415325). The
   dispatch-security/live-general drift case now takes the security
   excursion instead of the standard route and may make its security worker
   and verifier calls: intended enforcement of the original classification,
   not a claim that no call changes. Ordinary routes with no drift are
   identical (`tests/lifecycle/test_security_applicability.py`).

7. *`_partial_tasks` read from the typed record.* `_unresolved_partial`
   (both DONE sites and the cap's goal-question short-circuit) derives the
   partial set from the outcomes (an active `cap` fact, recovered by a
   completed CONTINUES, or an active not-merged fact on a parallel child)
   and blocks if either that set or the legacy one does; a disagreement is
   recorded on the task. The legacy set stays the report mirror. DONE
   unification stays deferred until its typed inputs are ready (Codex,
   5861527374) (`tests/lifecycle/test_partial_from_outcomes.py`).

8. *Breaker names from what the counter counted.* The serial breaker
   counter never counted or reset on a parallel batch, but a batch's capped
   children were appended to `turn_limited`, whose tail the stop named, so
   a mixed run named a child it never counted. The outcomes the counter
   counts are captured when it counts them, the stop names those, and when
   a batch ran between them it says so instead of "in a row". The counter,
   its timing, limit and stop kind are unchanged; how a batch should affect
   the counter is a separate, unauthorised question (Codex, 5861810828).
   `turn_limited` stays the report mirror (`tests/test_breaker_names.py`).

9. *Design debt read from the typed evidence.* `_name_findings_stop`
   names `DesignUnverified` from the stopping tasks' `evidence` records (a
   design check that ended `verified=False`), excluding audit debt (a
   record with `findings`), and compares the legacy `_design_unverified`
   list; a disagreement is recorded on the task and either one names the
   stop. The message text is unchanged (`tests/test_design_debt_typed.py`).
   Parity compares facts, not only task ids (Codex, 5862205507): each
   task's latest problem on both sides, and which task names the stop. An
   earlier legacy entry for a rechecked task is not a disagreement.

10. *Stop fixtures for the findings and completion candidates* (Codex,
    5862193380). `tests/lifecycle/test_stop_fixtures.py` runs 18 existing
    journey tests as written (plus one check that every fixture key has a
    journey) and records each Session's legacy stop inputs
    (`stop_reason`, `result.error`, `open_findings`, `_design_unverified`,
    audit findings) beside the typed stop fact in
    `tests/lifecycle/stop_fixtures.json`. No route changes. Exception stops
    (`RunStalled`, `DependencyTreeChanged`) leave `stop_reason` empty; their
    text is in the typed fact, and in `result.error` only when the run went
    through `project_run` (a direct-`Session` journey records
    `result_error: null`).

11. *Full stop text on the typed fact.* `Fact.detail` stays cut at 400
    characters (300 for an exception's message); `Fact.full` carries the
    whole text only when it was cut, so a long `stop_reason` or exception
    `result.error` is on the typed record intact. Additive report field;
    names, kinds and routes unchanged (`tests/test_stop_full_text.py`).

12. *Post-loop exceptions are typed.* An exception from the end-of-run
    dependency check or the final findings record used to reach
    `result.error` with no typed stop, and the session could still read
    complete. It is now recorded as the loop's exceptions are (same kind,
    legacy name and text), `completed` is false, and it is re-raised
    unchanged. The integrity route for a changed dependency tree at the
    end is untouched (`tests/test_post_loop_exception.py`).

13. *Approved evidence replaced later (E2; Codex, 5862294492).* A later
    task could rewrite an approved design task's renders, which its CHANGED
    line cannot name, and the run completed over bytes no reviewer saw. The
    completion guard, at both DONE sites, compares each approved delivery
    with the project; a difference is an active `unverified` fact
    (stage `delivery`) on the approved task, so the run stops
    `CompletionUnproven`. Not integrity: the J9b settlement boundary is
    unchanged, and nothing is recaptured or re-reviewed
    (`tests/lifecycle/test_replaced_evidence.py`). Integrating the Opus
    evidence controls needs their E2 pair flipped: the prospective xfail
    now passes and the current-behaviour pin no longer holds.

14. *Preview failures carry a proven origin (E1; Codex, 5862699144,
    option b).* `PreviewFailed` and `capture_task`'s result carry an
    `origin`, set where the failure is raised and never read from prose.
    Only two failures are proven environment: the port was already
    listening, so the preview was never launched; or a conventional runner
    outside the project (a bare name, or an absolute path outside it, as
    profile validation accepts one) could not be launched. A project file
    named like a runner (`./python3`) is a project file (Sol review,
    5862984388). Those stop as
    `PreviewUnavailable` (operator), with no repair call and the text kept
    byte-identical. Everything else is unattributed and keeps the existing
    `invalid_proof` / `DesignUnverified` route: an app that exits before or
    as it becomes ready, a preview never ready, a project-file preview that
    cannot launch, budget and capture timeouts, and a failed capture
    (`tests/lifecycle/test_preview_provenance.py`, with the table). The
    Opus E1 control's scenario (exit 1 before ready) is an application exit
    and stays unverified, so its strict xfail should be recast rather than
    flipped.

15. *A post-loop exception still re-checks the ledger* (Opus audit 23d6460).
    After a07e7bf a post-loop exception was typed, but it skipped the
    findings re-check an incomplete ending gets, so a resolution a later
    task undid stayed `resolved`. It now runs the same `_finalise_findings`
    as a loop exception (re-check and annotate, or distrust every
    resolution if that fails). Whole journey: F1 resolved by t2, t3 changes
    the page and stops DesignUnverified, an end-of-run `OSError` is
    injected (`tests/lifecycle/test_post_loop_ledger.py`).
    The audit's G8 probe E (one task id with two outcomes) is not reachable
    in one run: an unmerged child's open finding stops the loop after its
    batch, and no suite session produced a duplicate id; unchanged here.

**Retirement list additions (Codex, 5861036424):** collaborator selection
(`collaborators_for`) and the design prompt's applicability text still read
the live `design_cross_check` / `is_design_task`, and can drift from the
contract, including during lead recovery. Design applicability is not
retired while they do.
