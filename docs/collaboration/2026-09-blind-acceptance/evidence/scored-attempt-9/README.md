# Scored attempt 9 — the first completed task, at 46% less per slice

Run **81ba8b88**, frozen engine **65a5b41**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 8 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run. The
preflight was run as a launch gate and passed clean.
See [the pre-launch freeze](../scored-attempt-9-freeze.json).

## Result

**Incomplete after 404 seconds and six calls**, 532,795 reported tokens,
stopped at the 500,000 threshold. **Task 1 completed and closed out**, task 2
named and begun. 233 diff lines across `app.py` and a new test file.

Private **0 of 9**, public examiner **0 of 13** — the endpoint is still not
wired; tasks 1 and 2 are the parsing layer beneath it. Reconstructed offline in
a fresh network-disabled credential-free container: **113 tests pass** (the
100-test baseline plus 13 written by the run), source hashes matched before and
unchanged after.

| Call | Seat | Outcome | Tokens | Seconds |
| --- | --- | --- | ---: | ---: |
| 1 | Claude Fable | vendor window limit | 0 | 1.5 |
| 2 | GPT-6 Astra (orchestrator) | ok — named task 1 | 73,642 | 35.8 |
| 3 | Grok (lead, task 1) | **ok — task complete** | 241,700 | 228.5 |
| 4 | Grok (close-out, task 1) | ok | 7,337 | 8.1 |
| 5 | GPT-6 Astra (orchestrator) | ok — named task 2 | 57,953 | 27.6 |
| 6 | GPT-5.6 Sol (lead, task 2) | stopped at the threshold | 152,163 | 99.9 |

**The first completed task, close-out and integration check this lane has
produced**, and the first run to reach a second task.

## The measured effect of carrying orientation forward

Same first slice, same model, same seat:

| The task-1 lead | Attempt 8 | Attempt 9 | |
| --- | ---: | ---: | --- |
| Model calls | 15 | **9** | −40% |
| Cache re-reads | 383,104 | **158,336** | −59% |
| Total tokens | 450,602 | **241,700** | **−46%** |

Attempt 9's slice was the larger of the two (30 implementation + 60 test lines,
against 30 + 50) and it *finished*, where attempt 8's was cut off mid-flight.

The mechanism behaved as intended. Astra recorded four facts while choosing
task 1 — each one something a lead would otherwise have gone and found:

```
imports:      app.py:8–9 already imports io and csv; pytest is already used
export:       app.py:1291 defines export_csv; its header includes all eight
              columns in the requested manifest contract
tests:        tests/test_basic.py:1–10 adds the project root to sys.path and
              imports app as application
side effects: app.py:28–29 creates video/recording directories at module
              import; keep the new helper itself pure
```

The scan's new layout note named `app.py` (1,453 lines) and
`static/js/app.js` (1,593) as the principal source, and the three directories
holding tests. The whole block costs ~144 tokens and is re-sent per call; the
exploration it replaces cost far more than that, and cost it quadratically.

## Where the budget went this time

Six calls instead of three, for more than twice the work. The threshold is
still reached, and still mostly by cache — but the run now spends it on
finished slices rather than on one unfinished one. The open question from
attempt 8 stands unchanged and unaddressed here: **what `max_reported_tokens`
should count** is a budget decision, not a review-lane repair.

## Repaired after this run

A map note recorded its provenance as the whole `Seat` record, so the map read
`{'key': 'openai:gpt-6-astra', 'reason': 'fallback-unavailable', ...}` where a
reader expects a model name. A note's author answers *who established this
fact*, and that is the model; why it held the chair belongs in the ledger. Now
records the seat's key, with the plain-string case pinned too.

## Still unexercised

The worker tool-fit check (`a0f16ec`), six attempts on. Both leads did their
own work rather than commissioning an errand, which is a legitimate choice on
slices this size — but it means the repair has still never run.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. Raw stdout and vendor sessions stay
private outside every repository; `vendor-usage-extract.json` names each call's
private file and its SHA-256. Container and credential seed were removed.
