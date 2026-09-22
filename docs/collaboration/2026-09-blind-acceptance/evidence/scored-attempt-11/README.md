# Scored attempt 11 — two tasks completed, and the cheapest lead call yet

Run **3eb4b947**, frozen engine **c0755d0**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 10 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run. The
preflight was run as a launch gate and passed clean.
See [the pre-launch freeze](../scored-attempt-11-freeze.json).

## Result

**Incomplete after 502 seconds and eight calls**, 533,060 reported tokens,
stopped cleanly at the 500,000 threshold — and, unlike attempt 10, it wrote its
own records before stopping. **Two tasks completed**, each closed out, each
followed by an integration check that **passed**. 192 diff lines.

Private **0 of 9**, public examiner **0 of 13**; the endpoint is still not
wired. Reconstructed offline in a fresh network-disabled credential-free
container: **113 tests pass**, source hashes matched before and unchanged
after.

| Call | Seat | Tokens | Re-read |
| --- | --- | ---: | ---: |
| 1 | Claude Fable | 0 | — |
| 2 | Astra, orchestrator | 63,225 | 45,184 |
| 3 | Grok, lead (task 1) | **201,764** | 96,128 |
| 4 | Grok, close-out (task 1) | 6,900 | 0 |
| 5 | Astra, orchestrator | 56,818 | 35,328 |
| 6 | Grok, lead (task 2) | 157,172 | 121,216 |
| 7 | Grok, close-out (task 2) | 6,968 | 0 |
| 8 | Astra, orchestrator | 40,213 | 29,184 |
| **Total** | | **533,060** | **327,040 (61%)** |

## The prompt change worked; the half of it I argued for did not

Removing the instruction to *"inspect the project source in your working
directory"* — which contradicted the guidance that followed it — produced the
cheapest first-slice lead call this lane has measured:

| Task-1 lead | Tokens | Re-read | Re-read share |
| --- | ---: | ---: | ---: |
| Attempt 9 | 241,700 | 158,336 | 66% |
| Attempt 10 | 353,635 | 290,944 | 82% |
| **Attempt 11** | **201,764** | **96,128** | **48%** |

**No workers were commissioned**, in either task. The invitation to delegate
reading is now nought for two. What moved the number was telling the lead *not*
to go exploring, not telling it to delegate — and per the commitment made
before this run, the delegation wording will not be tuned a third time.

The re-read share across the whole run fell from 80% (attempt 10) to **61%**,
and new work rose from 98,447 to **206,020** in a comparable budget. The run
did roughly twice the work for the same spend.

## The watchdog: a false positive of mine, then a clean pass

This attempt was launched twice. The first launch was killed as `stalled` after
240 idle seconds — with `in_flight: 1`, that is, **in the middle of a perfectly
healthy lead call**. The heartbeat watched the run budget's mtime, which
advances only at call boundaries, and the stall window had been set to 240
seconds when three earlier runs had already measured lead calls at 228 to 263.
A threshold below a known-good duration is a bug however it is spelled. Two
calls and 59,296 tokens were spent; no source was changed.

The remedy was not a larger number. `run_isolated` now takes a *callable*
heartbeat, and the launcher answers the question that matters — it reads the
budget's `in_flight` count and reports an outstanding call as progress, so only
genuine idleness between calls accrues. On this launch the stall window never
fired, idle at exit was effectively zero, and the run stopped on its own
threshold with every record written.

## Still unexercised

The worker tool-fit check (`a0f16ec`), seven attempts on. It cannot fire until
a lead commissions an errand, and leads keep choosing not to.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. Raw stdout and vendor sessions stay
private outside every repository; `vendor-usage-extract.json` names each call's
private file and its SHA-256. Container and credential seed were removed.
