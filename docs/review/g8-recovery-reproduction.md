# G8 recovery reproduction at 7d294ce

Opus 5.5 bounded tests-only lane, 2026-09-27. Coordination log: #25 (claim
5862835380). Only this file and `tests/test_g8_recovery_reproduction.py`
were added. No engine, wiring, fixture or budget change. No vendor call.

## TL;DR

- **Same-task recovery is reachable, and it is clean.** A design task
  whose first check fails, gets one design-fix, and then verifies leaves one
  outcome with `verified=True`. Both readers report no debt and record no
  mismatch. Audit 23d6460's split (probe E) never forms on this path.
- **Two outcomes with the same id aren't reachable through the loop.** The
  task counter does reach a reused id after an unmerged parallel child. That
  child's open finding stops the loop in the same iteration, so the reused id
  is never dispatched. This matches the incumbent's finding (5862919384).
- **Probe E needs synthetic state.** A "later verified check for the same
  id" can only exist through injection. Following root (5862969439), this
  lane adds no synthetic latest-id test and no prospective xfail.
- **Keep the two issues apart.** If id reuse ever became reachable, the
  typed reader keeping the earlier debt would be the correct result. A
  latest-evidence rule would let one task discharge a different task's debt.
  That is a reason not to adopt the latest-evidence rule as a fix for reuse.

## Reachable vs synthetic

| Case | Path | Kind | Result at 7d294ce |
| --- | --- | --- | --- |
| Fail, fix, verified recheck in one task | `run_project` replay (`_design_script`) | reachable | one `t1` outcome, evidence verified with `first_problem`; `_design_unverified == []`, `_design_debt() == []`, no mismatch; stop `GoalUnconfirmedAtCap` |
| Control: the fix doesn't recapture | same replay, design-fix answers "Looked" | reachable | both readers report `t1`, no mismatch; stop `DesignUnverified: task t1` |
| Batch, second child unmerged, follow-up planned | `Session.run` with fork (`test_parallel_tasks`) | reachable state, **unreachable dispatch** | outcomes `t1,t2`, history `t1`; next counter id `t2` collides. Loop stops `FindingsOpen`; follow-up never asked |
| Batch, first child unmerged | same | reachable state, unreachable dispatch | history `[t2]`; the counter collides with the **merged** sibling `t2` |
| Control: fully merged batch | same | reachable | outcomes equal history; next id is fresh; completed |
| Probe E: earlier `t1` unverified, later `t1` verified | direct state edit only | **synthetic** | not reproduced here, per root |

## Why the split can't form today

- `_check_design` runs once per `run_task`, and it writes one record. The
  outcome's `evidence` is that final record (session.py:2493–2496).
- Legacy entries are appended only after the final result (3305, 3384). So
  the clear at 3380 finds nothing to remove on any reachable path. The clear
  would only matter under id reuse.
- Every unverified check calls `_open_finding`. Every unmerged child appends
  to `open_findings`. Both loop branches break on a non-empty
  `open_findings` (3058–3060, 3196–3198), and nothing ever clears it.
- `run_project` never supplies a fork. Parallel batches come in only through
  `runtime`'s session builder (runtime.py:763–778), which the Session-level
  replays exercise.

## Latent hazard (pinned, not fixed)

`t{len(history) + 1}` (session.py:2899, 3526) counts only merged summaries.
After an unmerged child, it names an id that is already on the record. Today
nothing dispatches it. If a later change lets the loop continue after an
unmerged child, the fix belongs in id allocation (unique ids). A
latest-evidence rule would be the wrong fix. The two pinning tests will fail
on any such change, which marks where root's policy decision comes back.

## Evidence

Frozen image `sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`,
`--network none`, `--read-only`, source mounted read-only at `/pkg`:

```
python -m pytest -q -p no:cacheprovider tests/test_g8_recovery_reproduction.py \
  tests/test_design_debt_typed.py tests/test_design_debt_binding.py \
  tests/test_parallel_tasks.py tests/lifecycle/test_stop_fixtures.py
43 passed
```

- **Not run:** ruff (not installed in the frozen image) and the broad suite.
- **Harness parity:** the `run_project` cases also pass the harness's
  typed-parity assertion.
- **A first draft failed on the test script, not the engine.** It ran a
  follow-up serial task in the merged control, and the scripted lead only
  knows `a.py`/`b.py`. The control now asserts the counter directly.
