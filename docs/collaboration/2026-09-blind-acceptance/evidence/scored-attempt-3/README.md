# Scored attempt 3 — preserved token-threshold stop

Run **f19eee8c**, frozen engine **4795062**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 and 2 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run.
See [the pre-launch freeze](../scored-attempt-3-freeze.json).

Launched by Claude on Davis's Mac at Davis's direction, because Codex was out of
context. One isolated container, one attempt, no retry and no continuation.

## Result

**Incomplete**, stopped after **394.22 seconds** and **three provider calls** at
the reported-token threshold: **523,473 tokens** against 500,000, an overshoot of
23,473 from the last in-flight call. No scope stop: **nothing was written at
all**, so `changes.diff` is empty and the tree is byte-identical to the input.

| Call | Seat | Role | Reported tokens | Seconds |
| --- | --- | --- | ---: | ---: |
| 1 | Claude Fable | orchestrator | 105,531 incl. 2,920 Haiku auxiliary | 41.08 |
| 2 | GPT-5.6 Sol | lead | 105,424 (86,912 cached) | 28.92 |
| 3 | Claude Haiku | worker:code-1 | 312,518 | 324.13 |

Total input 479,710; output 43,763. Claude Opus was selected as the task's
collaborator and never invoked. Grok was not invoked: Fable labelled the task
backend/standard, which seats Sol as lead under the existing ladder.

**Three vendors ran in one scored attempt for the first time, and a controlled
worker ran for the first time.** Full-worker coverage is still not established:
no Opus review and no closeout were reached.

## Where the budget went

The worker is the whole story. `worker:code-1` is Claude Haiku on the `code`
errand, a restricted seat: `--effort low` and
`--disallowed-tools "Bash Edit Write NotebookEdit Task"`. The restriction holds
exactly what it claims — `subagent_stats.spawned` is 0, no native child, no
write tool, and the tree is untouched — but it bounds *permission*, not
*volume*. The vendor envelope reports **11 turns**, 40,274 output tokens
(5,635 of them thinking) and 224,844 cache reads, for 312,518 reported tokens:
**60 percent of the run in one cheap-tier errand**, and 324 of the 394 seconds.

Its preserved reply is 101,074 bytes over 2,576 lines: the helper and about
thirty-four test functions, written out as text because the seat has no write
tools. None of it could land. The lead's next call was refused by the budget.

## Preserved application state

The archive contains the 30 input files unchanged. Independent reconstruction
in a fresh, network-disabled, credential-free container: all 30 hashes verified,
**100 tests passed in 2.04s**, hashes unchanged afterwards. That is the existing
GameTape suite. No feature work of any kind was delivered.

## Findings

1. **A restricted worker has no turn cap.** This is the same shape as the
   original Grok worker finding, now on Haiku and now measured inside a scored
   run. A worker is meant to answer once; this one ran an eleven-turn agent
   loop. The seat's own controls cannot catch it, because denying tools does not
   bound turns or output.
2. **The lead delegated the entire task to one worker.** Sol emitted 559 output
   tokens, commissioned `code`, and waited. The errand was the whole task rather
   than a bounded sub-question, so the cheapest seat in the fleet was handed the
   most expensive job.
3. **Both attempt-2 repairs behaved as intended.** The declaration named
   `_import_preview_rows(project, text)` exactly once and quoted it verbatim in
   `intended_result` and every acceptance line; the new lint passed it and still
   rejects attempt 2's two-signature declaration. The scope report carries the
   new `code_lines`/`test_lines` split (0/0 here).
4. **The successful-row provenance fix is confirmed live.** Fable's successful
   invocation row carries `auxiliary_models` and `auxiliary_tokens`; the same
   row was `{}` in attempt 2.
5. **Auxiliary accounting held on a hard case.** The worker's `modelUsage` had a
   single Haiku row that did not equal the seat's own total. The extractor
   reported the larger figure (272,244 input) once and marked provenance
   `unattributed` rather than labelling an arbitrary row as the seat.
6. **The lint had a false-positive risk, found here and fixed.** Acceptance
   quoted the CSV columns `Start (s)` and `End (s)`, which parse as
   `name(args)`. A name is now declared only where the description writes
   `def`. Two regressions use this run's real wording.
7. **The separate code/test estimate is unverifiable.** The decomposition prompt
   asks for one, but `SCOPE` has no field for it, so only the sum is recorded.

## Reproduce and review

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` into a fresh
directory (`source/` prefix) and verify `source-after-sha256.json`. Run
`python -m pytest -q` with the existing application dependencies. For private
scoring use the original archive committed by SHA-256
`71cb8b06a34ed94d6936bd9dfc45ed3889a0f6c5a80be1424850f1140e66371a`; set
`GAMETAPE_ROOT` to the extracted source. Do not modify the app or examiner.

Raw stdout, vendor sessions and thought fields stay private outside every
repository; `vendor-usage-extract.json` names each call's private file and its
SHA-256. The container and the credential seed were removed after the run.
