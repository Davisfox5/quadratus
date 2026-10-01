# Finding and stop state: projection contract (candidate)

Lane: Opus findings, PR #25. Base 9eabf69. Map rows: P3.4, `open_findings`
and `stop_reason`. Module: `quadratus/finding_state.py`. Tests:
`tests/test_finding_state.py`.

## TL;DR

- **What it is:** one read-only function, `project()`. It reads the typed
  task and run outcomes and the audit-finding ledger, and returns every
  unresolved item under an identity that doesn't change, plus the run's named
  stop.
- **What it isn't:** it never decides, writes, calls a model, derives
  completion, or keeps a second ledger. Nothing in the engine calls it yet.
- **Parity:** `legacy_parity()` compares the projection with what the run
  actually held: `open_findings`, `_design_unverified`, `stop_reason` and the
  `result.json` error. It agrees on every offline replay tested: seven named
  stops or exceptions, a clean run, a parallel child's design debt and an
  unmerged parallel child.
- **Full text is available on current typed facts.** Projection compares
  `full or detail` exactly. Only saved records made before `Fact.full` can
  report a bounded truncation gap. The post-loop exception gap applies only
  before engine commit a07e7bf.
- **Failure precedence is unchanged.** An exception is the stop. Open findings
  never replace a named stop. A dependency change at the end of the run
  replaces the stop unless that stop is a refusal or a security stop.
- **Decision for the incumbent:** switch the readers one at a time, in the
  order in "Integration seam" below. Each switch gets its own red.

## What the projection returns

`project(task_outcomes, run_outcome, ledger)` returns a frozen `FindingState`.
It also accepts the `result.json` form, `from_result(result)`, and a live
session, `from_session(session)`, which only reads attributes.

| Field | Meaning |
| --- | --- |
| `active` | Items still blocking, the stop excluded |
| `history` | Recovered or non-terminal facts, and resolved ledger rows; nothing is dropped |
| `stop` | The last active run fact carrying a legacy name (as `RunOutcome.stop()`), with its item id |
| `secondary` | Classes of the active items, highest first |
| `outranked_by` | Active items ranked above the stop's class; reported, never acted on |
| `problems` | Malformed record entries; each one fails closed |

**Identity is persistent.** Facts and ledger rows are append-only, so an id
never moves when the record grows:

- A ledger finding is `ledger:<F-id>`. This id is the ledger's own; it
  survives a reopen or a recheck.
- A task fact is `task:<task>[#n]:<stage>:<kind>:<k>`. `#n` is added only for
  a repeated task id. `k` is the ordinal among that task's facts with the
  same stage and kind.
- A run fact is `run:<stage>:<kind>:<k>`.

A recovered fact keeps its id and moves to `history`
(`test_identity_survives_a_later_record_and_a_recovery`).

**Categories.** Each is read from the writer that recorded the fact; the
source lines are listed in the next section.

| Category | Debt | Legacy `open_findings` text? |
| --- | --- | --- |
| `audit_debt` (ledger row), `ledger_missing` (snapshot id absent from ledger) | audit | no, the ledger is its own list |
| `design_evidence` (stage `design`, `invalid_proof`), `design_review` (stage `design`, `unverified`) | design | yes |
| `review`, `security`, `scope`, `cheap_gate`, `unmerged`, `run_finding` | none | yes |
| `check`, `cap`, `failed`, `transport`, `refusal`, `stopped`, `merge_gate`, `dependency`, `sent_back`, `stop`, `other` | none | no |

Audit debt and design debt never merge:

- **Audit debt** is repaid by a later RESOLVES task through the ledger.
- **Design debt** belongs to the task that left it, and
  `design_debt(tasks=…)` binds it to that task (map G8).

**A missing record is never an empty one** (review 5862287503). An absent
or non-list input is a `problem`, so `legacy_parity` disagrees. This covers:

- a `result.json` with no `workflow`, or a `workflow` that is None or holds
  `_workflow_record`'s error form;
- missing `tasks`, `run`, `run.facts` or `findings`;
- a task without `facts`;
- a session without its record.

An explicit empty record (`tasks: []`, `run.facts: []`, `findings: []`) is
valid and projects clean.

**The ledger is the authority.** A task's `open_at_close` is a snapshot.

- A snapshot id that the ledger since resolved is kept in history, with a
  note saying so.
- A snapshot id the ledger does not hold stays active as `ledger_missing`,
  and is reported as a problem.
- A ledger status other than `open` or `resolved` stays active and is
  reported.

## Audit: every reader and writer (9eabf69)

Line numbers are `quadratus/session.py` unless stated. The only other
`stop_reason` hits in `quadratus/` are vendor turn-stop fields
(`providers.py`, `cli_providers.py`, `delegation.py`, `config.py`) and the
budget's own `stop_reason` in `budget.json` (`run_budget.py:102`). Those are
different fields, so they are out of scope.

### `open_findings` writers

| Line | Writer | Typed fact | Category |
| --- | --- | --- | --- |
| 1924 | scope report blocking | `integrity`, current stage | `scope` |
| 2360 | cheap gates failed | `product`, `legacy_route` | `cheap_gate` |
| 2456 | review BLOCKING still open at the cycle cap | `unverified` (review/revision stage) | `review` |
| 2631 | security verdict has a finding | `security` | `security` |
| 2688 | security verification incomplete | `security` | `security` |
| 2705 | security verdict reject/other | `security` | `security` |
| 3262 | harness capture failed | `invalid_proof`, stage `design` | `design_evidence` |
| 3341 | design evidence not clean | `invalid_proof`, stage `design` | `design_evidence` |
| 3354 | no cross-vendor reviewer | `unverified`, stage `design` | `design_review` |
| 3363 | final design review BLOCKING / no verdict | `unverified`, stage `design` | `design_review` |
| 1033 | any of the above with no task running | run fact of its class | `run_finding` |
| 3587 | child `open_findings` extended into the parent | child's facts come with `task_outcomes` (3591) | as the child's |
| 3599 | unmerged parallel child (direct append, not `_open_finding`) | `mine.note(..., stage="merge")` 3602, only if the child has an outcome | `unmerged` |

All writers except 3599 go through `_open_finding` (1023). That function
appends the text and records the fact with `detail = text[:400]` and, since
8529b2a, `full = text` when cut. The
unmerged text differs from its fact (`Parallel task X was not merged (R); …`
against `not merged: R`). Parity matches them by task id and reason.

### `open_findings` readers

| Line | Reader | Use |
| --- | --- | --- |
| 3017, 3155 | batch and serial stop trigger | any open finding (or a failing last check) stops the loop via `_name_findings_stop` |
| 3045 | DONE conjunction | `not self.open_findings` |
| 3187 | cap conjunction | same |
| 3100, 4281 | `_settle_resolution` | a new open finding during this task refuses settlement (`len > open_before`) |
| 4434 | `_open_work` | count and first text in the stop reason |
| `tests/harness_pack/runner.py:317` | harness pack expectations | count and substrings |

### `_design_unverified` (design-debt mirror)

- **Writers:** 3264 and 3343 append; 3339 clears the task's own entries when
  a later check of the same task id verifies (map G8); 3590 extends from
  parallel children.
- **Reader:** `_name_findings_stop` 4408.
- **Parity** compares `(task, problem)` pairs as a multiset. Each entry is
  rendered as the fact detail `_check_design` writes at 3262 and 3341. The
  same task with a different problem, or a different count, disagrees.
- **Known disagreement:** the 3339 clear has no typed counterpart. The
  `invalid_proof` fact stays active. `legacy_parity(design_unverified=…)`
  reports this, and
  `test_design_debt_the_legacy_list_cleared_but_the_fact_keeps_is_reported`
  pins it.

### `stop_reason` writers

Every writer goes through `_stop_with` (1053), which also records a run fact
with `legacy = <prefix>`. The only exceptions are the two direct resets: 2965
at run start, and the initialiser at 975.

| Line | Stop name | Kind |
| --- | --- | --- |
| 1049 | `CompletionUnproven` (`_guard_completion`) | unverified |
| 2956 | `DependencyTreeChanged` / `DependencyIdentityUnavailable` at the end of the run | integrity |
| 2984 | `PlanDeclined` | operator |
| 3038, 3196 | `RequirementsUnmet` | unverified |
| 3049 | `DoneWithOpenWork` (`_stop_open_work`) | record primary, `clean` becomes unverified |
| 3124 | `TurnLimitBreaker` | cap |
| 3161 | `FindingsUnresolved` (RESOLVES not settled) | unverified |
| 3192 | `GoalUnconfirmedAtCap` (goal not confirmed) | cap |
| 3201 | `GoalUnconfirmedAtCap` (`_stop_open_work`) | record primary |
| 4084 | `FindingsUnresolved` (`_stop_findings_unresolved`) | unverified |
| 4411 | `DesignUnverified` | unverified |
| 4417 | `CheckFailing` / `FindingsOpen` (`_stop_open_work`) | record primary |

The exception stop is not a `stop_reason`. `run()` at 2930 records
`classify(exc)` with `legacy = type(exc).__name__`, then re-raises the
exception.

### `stop_reason` readers

| Line | Reader | Use |
| --- | --- | --- |
| 4409, 4413 | `_name_findings_stop` | first writer wins: names a stop only if none is named |
| 2960 | `run()` | `_annotate_open_findings(self.stop_reason[:240] …)` |
| `project_run.py:308-321` | result error | used when no exception; a `TurnLimitBreaker` prefix also writes `in-flight.json` |
| `project_run.py:450-457` | `_workflow_record` | `outcome.parity` compares the error name with `run.stop().legacy` |

`outcome._STOP_PREFIX` (`outcome.py:58`) is defined and never read. It also
lists only 4 of the 12 stop names.

## Failure precedence (as the engine behaves today; unchanged)

1. **An exception raised from `_run_tasks` is the stop.** The loop never
   reaches a session-chosen stop after it. `project_run` reports it as
   `<Class>: <message>`. Its kind is `classify()`, and an unknown class is
   `operator`. Open findings and design or audit debt are annotated and
   listed beside it; they never replace it. The projection keeps refusal,
   operator, budget and unknown exception stops whatever debt is open
   (`test_an_exception_stop_is_kept_whatever_debt_is_open`, and the
   RunStalled replay).
2. **Within one loop iteration, session-chosen stops are ordered:**
   - Serial task: `_name_findings_stop` (open finding or failing last check)
     comes before `FindingsUnresolved` (RESOLVES not settled).
   - Inside `_name_findings_stop`, the task's own `DesignUnverified` comes
     before `CheckFailing`, which comes before `FindingsOpen`.
   - DONE reply: audit findings are sent back up to
     `max_requirement_reopens` times and then stop as `FindingsUnresolved`.
     Unmet requirements are sent back and then stop as `RequirementsUnmet`.
     Otherwise the dependency check runs, then the conjunction:
     `DoneWithOpenWork`, else `_guard_completion` (`CompletionUnproven`).
   - Task cap: ledger debt stops as `FindingsUnresolved` without asking the
     goal question. Capped or partial work stops as `GoalUnconfirmedAtCap`
     (open work). A goal answered "not met" stops as `GoalUnconfirmedAtCap`
     (cap). Unmet requirements stop as `RequirementsUnmet`. Otherwise it is
     open work, else `_guard_completion`.
   - The breaker (`TurnLimitBreaker`) and `PlanDeclined` stand alone.
3. **The kind of an open-work stop is the record's primary** (4441). It can
   therefore be `security`, `refusal`, `integrity` and so on, not only
   `unverified`.
4. **The end-of-run dependency check (G9, 2946-2956)** replaces the named
   stop, unless the prior stop's kind ranks above `integrity` (refusal or
   security). In that case the change is kept only as an unnamed integrity
   fact. The projection reports it as `dependency` in `active`, and the
   earlier stop it replaced stays an active `stop` item.
5. **Post-loop exceptions are typed since a07e7bf.** They are recorded like
   loop exceptions and become the named stop; an earlier named stop stays as
   an active `stop` item. Older saved records can still have a post-loop
   exception error without a matching fact, which parity reports as a
   disagreement. The post-loop exception path at a07e7bf does not run the
   resolved-finding ledger recheck; projection follows the ledger status it
   receives and cannot infer an omitted distrust note.
6. **`project_run`:** an exception's error beats `stop_reason`, and
   `stop_reason` beats a blank.

**Observation, not acted on.** `DesignUnverified` is recorded as
`unverified`, but its cause is an `invalid_proof` fact, which ranks above
`unverified` in `PRECEDENCE`. So `outranked_by` names the stop's own cause.
The same applies to any stop recorded as `unverified` while a higher fact is
active. Whether the stop should carry its cause's class is the incumbent's
decision.

## Legacy record gaps and integration conditions

1. **Only older stop facts can lose text.** Before 8529b2a, `Fact.detail`
   kept 400 characters and exception facts kept 300 characters of the
   message. Current facts carry `full` when cut. The projection compares
   `full or detail` exactly whenever the field is present, including when
   its value is null. Finding and design text also compare in full.

   For a saved fact without the `full` field, parity reports a stop prefix
   as a truncation `gap` only when detail sits at one of these bounds:
   - 400 characters, for a `_stop_with` stop;
   - `<Class>: ` plus 300 characters, for an exception stop.

   A shorter detail was never cut, so it must equal the legacy text exactly;
   anything else is a disagreement (review 5862287503). Controls:
   - `test_a_truncated_stop_detail_is_a_gap_not_a_disagreement`;
   - `test_a_short_divergent_stop_text_is_a_disagreement_not_a_gap`;
   - `test_a_prefix_one_short_of_the_bound_is_a_disagreement`;
   - `test_an_exception_message_cut_at_its_own_bound_is_a_gap`.
2. **Post-loop exception typing depends on engine version.** a07e7bf
   records these exceptions. The synthetic older-record control remains
   `test_a_legacy_record_with_an_untyped_post_loop_exception_disagrees`.

## Integration seam (for the incumbent; nothing is wired here)

Compute `state = finding_state.from_session(self)` at the point of use. It is
cheap and pure; never cache it across a write. Switch each reader alone, with
a red that fails on the previous commit:

1. **`_name_findings_stop` (4408-4417):**
   - On the pre-ea7c9a2 engine, `own` could become
     `state.design_debt(stopping)`. Since ea7c9a2 the engine reads task
     evidence instead. Prove both typed readers agree after resolving the
     G8 recheck rule before switching this reader.
   - `not self.stop_reason` becomes `state.stop is None`.
   - Controls: `test_design_debt_binding.py`, `test_named_stops.py`.
   - Before this switch, decide the G8 clear at 3339: the fact needs a
     recovery, or the legacy clear is dropped. Parity shows the two disagree
     today.
2. **Stop triggers (3017, 3155):** `self.open_findings` becomes
   `state.findings()`. This is equal on every replay here.
3. **`_settle_resolution` (4281):** `len(self.open_findings) > open_before`
   becomes "`state.for_task(spec.task_id)` has a finding-category item".
   `open_before` is then unused.
4. **`_open_work` (4434):** the count and first text come from
   `state.findings()`, with the ledger line from `state.audit_debt()`.
5. **DONE and cap conjunctions (3045, 3187):** these belong to the completion
   lane. This module supplies only `state.findings()` and
   `state.audit_debt()`; it does not decide.
6. **`project_run.py:308-313` and `run()` 2960:** these may read
   `state.stop` only on an engine that records post-loop exceptions and
   full stop text (a07e7bf plus 8529b2a). The byte-identical control must
   compare the error with `state.stop.full or state.stop.detail` on every
   replay. Ledger recheck behavior after a post-loop exception still needs
   its own engine control.
7. **Then retire:**
   - `open_findings` as a decision input; it stays as report text.
   - `_design_unverified`.
   - `stop_reason` as a decision input.
   - `outcome._STOP_PREFIX`, which is unused.

   The harness pack reader (`runner.py:317`) stays on the report text.

## Evidence

The frozen image is
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`.
The source is mounted read-only, with `--network none`:

```
docker run --rm --network none -v "$PWD":/pkg:ro -w /pkg -e PYTHONDONTWRITEBYTECODE=1 \
  sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9 \
  python -m pytest -q -p no:cacheprovider tests/test_finding_state.py
```

The replays use `tests/lifecycle/harness.py`: fake CLI launches, no vendor
call. The parallel cases use the scripted `Orchestrated` sessions from
`tests/test_parallel_tasks.py` and `tests/test_design_debt_binding.py`. The
Session is read after `run()` returns, through a spy on `Session.run` that
changes nothing.

**Revision for review 5862287503.** The revised tests were run with the
reviewed 22eed61 module mounted read-only over the revised tree. They give
14 assertion failures and no import errors:

- 3 on design pairs;
- 7 on missing saved records;
- 1 on a session without its record;
- 3 on stop-text bounds.

The revised module passes all 45 tests.

There is no old-base behavioural red against 9eabf69. This is a new module with no engine
route, so on 9eabf69 its tests can only fail on the missing import. Reds
belong to each switch in the seam above.
