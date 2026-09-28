# Post-loop exception and the resolved-finding ledger: reproduction

Base `7d294ce`. Tests-only. No engine edits. Test file:
`tests/test_postloop_ledger_reproduction.py`.

## TL;DR

- **The journey is reachable.** t1 audit records F1, t2 resolves it, and
  t3 is an in-scope edit that changes the source and fails the gate. That
  ends the loop with **no re-check** of F1. Only `_finish_run` would look at
  F1 again.
- **The exception after the loop is injected.** In the code as it stands,
  nothing deterministic raises there. `DependencyWatch.verify` turns every
  I/O failure into a dependency exception, and `_finish_run` catches those.
  What is left is a signal (Ctrl-C during the end-of-run hash pass, which
  `project_run` persists), an interpreter failure, or a future defect.
- **When it does raise, the ledger is stale.** The run reads
  `completed=false` with the exception as its typed stop. But F1 is still
  `resolved` by t2, with no reopen note, no distrust note and no reason, and
  R1/R2 are not `NOT MET`. The skipped re-check would have reopened it.
- **Both other exits re-check.** With no exception, F1 is reopened and
  R1/R2 are `NOT MET`. With an exception inside the loop, at the same point
  or at a reachable scope stop, F1 is also reopened.
- **Status: not run.** Docker, `gh` and dependency installs were all denied
  in this session, so no test has run in the frozen image or anywhere else.
  The expected values come from reading the source. Run the file before
  relying on it.
- This does not overlap E2 (`bbb4956`). E2 blocks *completion* over replaced
  approved deliveries. This is the *findings ledger* on an already
  incomplete run.

## The seam (source)

`Session.run` (`quadratus/session.py:2952-2975`):

- Loop exception (2954): `_recheck_resolved_findings()`, then
  `_annotate_open_findings`. If the re-check itself fails,
  `_distrust_resolutions`.
- Post-loop (2966-2974): `_finish_run()` in a try. The except sets
  `completed=False`, calls `_note_exception` and re-raises. It does **not**
  re-check or distrust.

`_finish_run` (2982-3002) re-checks at 3001 only after
`_verify_dependencies("at the end of the run")` returns (or raises one of
the two dependency exceptions it catches). So a different exception from
2987, or one from 3001 itself, leaves every `resolved` row as it was.

The four `_recheck_resolved_findings()` call sites are 2959 (loop
exception), 3001 (`_finish_run`), 3215 (cap) and 4135 (DONE, through
`_findings_block_done`). Loop exits that reach `_finish_run` **without** an
earlier re-check:

- `_name_findings_stop` after a failed check or open reviewer findings
  (3058-3060 batch, 3196-3198 serial). **Used here.**
- The turn-limit breaker (3161-3171).
- A `RESOLVES` task that did not settle (3199-3206).

## Journey and results (expected, not run)

Shared steps: t1 `AUDIT` records F1 (mobile 450px) on R1, R2. t2
`REPAIR RESOLVES: F1` wraps the toolbar, recaptures and is approved, so F1
is resolved by t2. t3 is `DOCS` with `app.py` added to its scope. It writes
README.md and a broken `add`, so the gate fails, the gate-fix changes
nothing, and the loop breaks at `_name_findings_stop`.

| Test | Exception | completed | stop | F1 | R1/R2 |
| --- | --- | --- | --- | --- | --- |
| `control_without_t3…` | none, no t3 | true | — | resolved/t2 | — |
| `ordinary_end_rechecks…` | none | false | CheckFailing path | **open**, "its resolving evidence no longer holds" | `NOT MET: open finding F1` |
| `in_loop_exception_at_the_same_point…` | synthetic `OSError` in `_name_findings_stop` | false | OSError | **open**, same reopen | `NOT MET` |
| `in_loop_reachable_exception…` | reachable scope stop (t3 writes `templates/index.html`) | false | the scope exception | **open**, same reopen | `NOT MET` |
| `post_loop_…over_a_valid_resolution…` | synthetic `OSError` at end, no t3 | false | OSError | resolved/t2, **still verifies** | — |
| **`post_loop_exception_leaves_the_undone_resolution_resolved[OSError]`** | synthetic `OSError` at `"at the end of the run"` | **false** | **OSError** | **resolved/t2, `reopened=None`, `unresolved_reason=""`** | **not NOT MET** |
| **`…[KeyboardInterrupt]`** | same, `KeyboardInterrupt` | **false** | **KeyboardInterrupt** | same | same |
| **`post_loop_failing_recheck_is_not_distrusted`** | synthetic `OSError` from `_recheck_resolved_findings` | **false** | **OSError** | **resolved/t2, no distrust note** | **not NOT MET** |

The main case also checks that the row is wrong and not just unlabelled.
After the run, `session._identity_problem(...)` on F1 returns a problem.
The valid-resolution control returns none.

## Outcome vs ledger, exactly

In the reproduction, the run outcome and the ledger disagree:

- **Outcome** (`result.error`, typed stop): incomplete, `OSError` or
  `KeyboardInterrupt`.
- **Ledger** (`result.json` `findings[0]` and `requirements.status`): F1
  `resolved` by t2, and R1/R2 carry whatever t2/t3's coverage wrote, not
  `NOT MET: open finding F1`.

Any reader that trusts the ledger (the candidate projection in the
`23d6460` audit moves `resolved` rows to history) reports F1 as settled,
even though its resolving renders no longer match the source.

## Scope of the claim

- **Defect claim:** the post-loop except path lacks the re-check/distrust
  step that the loop except path has. Given any exception there, a
  resolution undone by a later task is persisted as resolved on an
  incomplete run.
- **Not claimed:** that a real run has hit this. The only non-injected
  trigger found is an operator interrupt during the end-of-run dependency
  pass, which can take up to 120 s under the deptree bounds.
- **Not proposed here:** the remedy. The shape the audit already names is
  to mirror 2956-2964 in the post-loop except. That belongs to the
  incumbent's separate change. If that change lands, the three bold rows
  above should flip to `open`/`NOT MET` and the tests should be inverted.

## Correction after the frozen run

Root ran the eight tests in the frozen image. Five passed and three failed,
all on one fixture expectation. The saved F1 row has a `reopened` key with
the value `None`, not a missing key, so `f1.get("reopened", "")` returned
`None` and the expected `""` did not match. The actual row was
`("resolved", "t2", None, "")`. The expectation in the three reproduction
cases is now `None`. Nothing else changed: F1 still has to be `resolved`
by t2 with no reopen note and no reason, R1/R2 still must not be
`NOT MET`, and the main case still requires that the evidence check fails
on the persisted row. The stale-ledger claim is unchanged, and this
correction has not yet been re-run.

## What was not done

- **No frozen-image run.** The `docker` invocation was denied. The local
  `python3` (3.9) lacks `jsonschema`, and installing it was out of scope.
- **No PR25 log claim, progress or handoff.** `gh` was denied. The handoff
  text is this file.
