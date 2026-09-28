# Scored attempt 10 — the re-read split is visible; the delegation nudge failed

Run under engine **95fee5c**, GameTape **a8772ab**. The 30 input files and task
brief match attempts 1 to 9 byte for byte. The private examiner archive hash is
unchanged and its contents were not opened before the run. The preflight was
run as a launch gate and passed clean.
See [the pre-launch freeze](../scored-attempt-10-freeze.json).

## Result

**Stopped by the outer watchdog at 900 seconds**, not by any run limit. Task 1
completed and closed out; task 2 was in flight. 486,031 reported tokens across
five invoked calls, with the inner budget at 822 seconds and **no stop reason
latched** — it had not reached any of its own limits.

Private **0 of 9**, public examiner **0 of 13**; the endpoint is still not
wired. The surviving tree passes **135 tests** (the 100-test baseline plus 35
the run wrote), with the baseline intact.

## What worked: the split is in the record

This is the first run whose own ledger shows where the tokens went, without
opening a private vendor envelope:

| Seat | Counted | Re-read | Vendor price |
| --- | ---: | ---: | ---: |
| Astra, orchestrator (task 1) | 59,991 | 43,520 | — |
| Grok, lead (task 1) | 353,635 | **290,944** | $0.1161 |
| Grok, close-out (task 1) | 7,389 | 0 | $0.0055 |
| Astra, orchestrator (task 2) | 65,016 | 53,120 | — |
| **Total** | **486,031** | **387,584 (80%)** | |

New work was 98,447 tokens. The vendor's own price for the largest call was
**$0.12**, against the $0.30 our API-price counterfactual assigns it.

## What failed: the lead did not delegate

**No workers were commissioned.** The task-1 lead took **12 model calls**
against attempt 9's 9, and 353,635 tokens against 241,700. Attempt 10's task 1
was a different slice (a pure validator rather than the CSV reader), so this is
not a clean comparison — but it is the wrong direction, and the instruction
plainly did not take.

The likely reason is in the prompt rather than the model. The lead is told
`Inspect the project source in your working directory` as a direct instruction,
and then, several blocks later, invited to commission a worker for the
inspecting instead. A direct instruction beats a hedged one placed after it,
and this project's own finding is that the *end* of the prompt is the position
attention favours — where the task recitation already sits, and where this
guidance does not.

**The honest scoreboard for the two efficiency changes:** carrying the
orchestrator's reading forward is measured and worked (−46% on a like-for-like
slice, attempt 9). Telling the lead to delegate its reading has one data point
and it is negative.

## A defect this exposed: the watchdog cannot let a run finish

`RunLimits.wall_seconds` and `run_isolated`'s `wall_seconds` are **both 900**.
A run that uses its full time is therefore killed by the supervisor *before*
`run_project` writes its records, which is what happened here: `result.json`,
`report.md`, `ledger.md`, `delegation.md` and `changes.diff` were never
written. The work survived only because `/work` is a bind mount.

`tools/acceptance/README.md` anticipated the shape of this — "the outer
watchdog may terminate before final session-copy/result steps" — but the
remedy is to stop guaranteeing it. The outer wall should exceed the inner
limit by enough to write the records. That does not enlarge any budget: the
inner 900-second limit is what bounds the run, and it is untouched.

Vendor session evidence is also absent for the same reason: the launcher copies
it in a `finally` block that the kill pre-empted.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. `watchdog-truncation.json` names the run
records that do not exist and why. Raw stdout stays private outside every
repository. Container and credential seed were removed.
