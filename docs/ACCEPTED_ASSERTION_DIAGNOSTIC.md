# Accepted-assertion currency diagnostic

Base: Davisfox5/quadratus PR73, `bc63971914400116bf5b7b699e1c4222b22c47a2`.

This note describes `tests/test_accepted_assertion_currency.py`. It is a
test-only regression. It changes no runtime source, no policies, roles or
spend, and it makes no provider or model calls.

## Question

Task t1 adds a meaningful passing test and a reviewer judges it sufficient.
Task t2 then weakens the same named test without renaming it. The weakened
suite still passes. Can the old sufficiency judgment keep acceptance
established on the changed source?

The tests check that it cannot.

## How the tests are wired

- A tiny project is built in `tmp_path`. It has `app.py` with a correct
  `add()`, a smoke test, and a `pytest.ini` that disables the cache plugin so
  the source fingerprint is not disturbed by pytest's own files.
- The requirement R1 reads: add returns the arithmetic sum for negative
  operands.
- A `Session` is built with an invoke that raises if called. `Session.run`
  and `run_task` are never used.
- The gate is a real `IntegrationGate` running
  `sys.executable -m pytest -q --quadratus-report={report}` in a subprocess,
  with `minimum_tests=1` and a 30 second timeout. The result is copied with
  `asdict` into `session.checks`, with `command_cwd='.'`, the source
  fingerprint, the gate identity, the environment identity and the current
  requirement revisions. Reports and case ids are measured. No passed-case
  report is faked.
- The source fingerprint is captured before each recorded gate run and must
  be unchanged afterward. The recorded check carries the pre-run value, so it
  cannot be labelled with the wrong source.
- Run-start tests are snapshotted with `_snapshot_original_tests()` before
  t1 adds `tests/test_negative.py`. The test checks that the snapshot is not
  `None`, contains `tests/test_app.py`, and does not contain
  `tests/test_negative.py`.
- Reviewer verdicts are **synthetic**. They are explicit `O1: SUFFICIENT` or
  `O1: INSUFFICIENT` lines passed to `_judge_obligations`. This is unit
  evidence wiring. It is not a live handoff and not a whole-run proof.

## Controls

There are five test cases: two weakening variants, one recovery case and two
removal or rename variants.

1. **Weakening control** (two variants).
   - `assertion_only`: t2 weakens the assertion and the app stays correct.
     The original assertion is not claimed to fail here.
   - `assertion_and_app`: t2 also regresses the app. Before the assertion is
     switched to the weak form, the real gate runs once with the regressed
     app and the strong assertion. The test checks that the run did not pass
     and that the structured report records an assertion failure in the bound test body. This expected
     failure is not recorded as a session check. Then the weak assertion is
     written and the weakened suite goes green.

   In both variants the weakened file bytes differ from the strong ones while
   the bound case identity is unchanged, and the source fingerprint differs
   from t1. Before a new run, `_refresh_obligations` makes the execution
   stale, the row is not established, and R1 MET is lowered. A real green run
   of the weakened suite restores execution, but the old sufficiency judgment
   is no longer current, so the row is still not established. A raw R1 MET
   with no per-O1 verdict does not count. An explicit synthetic INSUFFICIENT
   does not count. A green rerun alone is therefore insufficient without a
   fresh verdict.
2. **Recovery control.** The correct app and the meaningful assertion are
   restored and a real run goes green. Execution returns, but acceptance is
   permitted only after a fresh synthetic SUFFICIENT.
3. **Removal and rename control** (two variants). The bound test is renamed
   or removed. The suite is green through the smoke test, but the bound case
   is not among the passed cases, so even a fresh synthetic SUFFICIENT cannot
   re-discharge the row.

## Scope and limits

- Run-start protection does not freeze tests added later. The snapshot covers
  only tests that existed at run start, so a test added by t1 and weakened
  by t2 is not restored.
- Source identity requires fresh sufficiency. A changed source makes the old
  judgment non-current. This does not stop a new approval.
- There is no assertion-equivalence detector. Nothing compares the old and
  new assertion text.
- An erroneous fresh reviewer approval can still accept weak evidence.
  These unit controls do not assess the other review or final-audit paths.
  There is deliberately no test that requires such a bad approval to pass.
- The reviewer verdicts are synthetic, and the tests exercise the unit seams
  (`_refresh_obligations`, `_judge_obligations`, `_apply_acceptance`) only.

## Results

No results or test counts are claimed here. Run
`python -m pytest tests/test_accepted_assertion_currency.py -q` to obtain
them.
