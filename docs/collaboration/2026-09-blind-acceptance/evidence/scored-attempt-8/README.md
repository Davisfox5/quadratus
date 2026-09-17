# Scored attempt 8 — the first code this lane has produced, and a threshold spent on cache

Run **18d728fd**, frozen engine **0d2c629**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 7 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run.
See [the pre-launch freeze](../scored-attempt-8-freeze.json).

The preflight was run as a launch gate and passed clean for the first time:
work tree readable, Codex and Grok sessions live, an OpenAI seat able to read
the mounted source.

## Result

**Incomplete after 297 seconds and three reserved calls**, 527,794 reported
tokens, stopped at the 500,000 threshold. **Source was changed**: 105 lines
across `app.py` and a new `tests/test_import_preview.py`.

Private **0 of 9**, public examiner **0 of 13** — the endpoint is not wired yet,
so every case is still HTTP 405. Reconstructed offline in a fresh
network-disabled credential-free container: **108 tests pass** (the 100-test
baseline plus the 8 the run wrote), source hashes matched before and after.

| Call | Seat | Outcome | Tokens | Seconds |
| --- | --- | --- | ---: | ---: |
| 1 | Claude Fable | vendor window limit | 0 | 1.6 |
| 2 | GPT-6 Astra (orchestrator) | ok — decomposed task 1 | 77,192 | 32.2 |
| 3 | Grok (lead, task 1) | ok, then stopped post-return | 450,602 | 263.4 |

## What it built

Astra sliced the feature deliberately — header reading first, with endpoint
wiring and row validation explicitly deferred — and Grok implemented that
slice: `_read_clip_manifest_header`, decoding `utf-8-sig` strictly, trimming
headers, matching the three required names case-insensitively, positioning the
reader so `line_num` counts physical lines, and raising `ValueError` for bad
encoding, empty input or a missing header. Plus eight focused tests.

It is correct as far as it goes, it matches the brief's contract, and it is one
slice of maybe five. **The first working code this lane has produced.**

## Where the budget went

The lead's call ran 15 turns and metered 450,602 tokens — 85% of the run's
whole threshold for one slice. The breakdown matters:

| | Tokens |
| --- | ---: |
| `cache_read_input_tokens` | 383,104 |
| fresh input | 54,069 |
| output | 13,429 |

**Cached re-reads are 88% of that call's input.** They are the agent loop
re-sending its conversation on each of 15 turns, not new work. The vendor's own
figure for the whole call is **$0.129**; our API-price counterfactual reports
$0.955, because the meter deliberately folds cache reads into input — right for
a counterfactual bill, and the same number is being used as the run's stop
threshold.

This is the second time a token-threshold stop has been mostly cache:

| Run | Seat that hit it | Cache share of its input |
| --- | --- | ---: |
| Attempt 3 | Haiku worker | **99%** (224,844 read + 45,893 created, 1,507 fresh) |
| Attempt 8 | Grok lead | **88%** (383,104 of 437,173) |

So `max_reported_tokens` is not currently bounding what it reads as though it
bounds. **No change has been made** — what the threshold should count is a
budget decision, and enlarging budgets is not a review-lane repair.

## Fixed here

The collection script archived the integration gate's own `.pytest_cache` as
though it were model output, which made an otherwise clean offline
reconstruction report a hash mismatch. Generated caches are now excluded, and
the re-run reports `source_hashes_match_before: true`. A reporting defect in
the evidence tooling, not in the run.

## Still unexercised

The worker tool-fit check (`a0f16ec`), five attempts on. Attempt 8 got closer
than any before it — a lead was invoked and wrote code — but the lead never
commissioned an errand, so no worker has run.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. Raw stdout and vendor sessions stay
private outside every repository; `vendor-usage-extract.json` names each call's
private file and its SHA-256. Container and credential seed were removed.
