# Scored attempt 7 — an OpenAI seat read the project, then an expired login ended the run

Run **7303ff7d**, frozen engine **cd33f7d**, GameTape **a8772ab**. The 30 input
files and task brief match attempts 1 to 6 byte for byte. The private examiner
archive hash is unchanged and its contents were not opened before the run.
See [the pre-launch freeze](../scored-attempt-7-freeze.json).

## Result

**Incomplete after 41.6 seconds and three reserved calls**, 82,051 reported
tokens. Nothing was written; the saved source is byte-identical to the frozen
input. Private **0 of 9**, public examiner **0 of 13**, unchanged baseline
**100 tests passing** offline — all determined by the empty diff.

| Call | Seat | Outcome | Tokens | Seconds |
| --- | --- | --- | ---: | ---: |
| 1 | Claude Fable | vendor window limit | 0 | 1.5 |
| 2 | GPT-6 Astra (orchestrator) | ok — decomposed task 1 | 82,051 | 39.7 |
| 3 | Grok (lead, task 1) | ProviderError: not signed in | unknown | 0.4 |
| — | GPT-5.6 Sol (lead, recovery) | selected, never invoked | — | — |

## The containment repair is confirmed live

**No `bwrap` string appears anywhere in this run**, and Astra ran three real
shell commands inside its disposable source copy:

```
/usr/bin/bash -lc 'pwd; rg --files'
/usr/bin/bash -lc 'rg -n "csv|import_preview|def |player|tag_types" app.py tests/...'
/usr/bin/bash -lc "sed -n '1,82p' app.py; ...; cat TASK.md"
```

It then produced a task grounded in what it had read — *"following the export
column format in `app.py`"*, with a 40-line implementation and 60-line test
budget and an explicit deferral of classification, wiring, UI and docs. This is
the **first grounded work by an OpenAI seat in this harness**, across seven
attempts. The working directory in the transcript is
`/tmp/quadratus-review-onsp9knu`, which confirms the other half of the
containment argument: a seat without a write grant is held by the disposable
copy, not by the vendor's sandbox flag.

## What stopped it

Two faults, in sequence.

**Grok's subscription session had expired.** The CLI answered in 0.37 seconds:

```
{"type":"error","message":"Not signed in. To authenticate without a browser,
run:\n  grok login --device-code\n..."}
```

The token in the staged seed expired on **2026-09-14T22:13Z**. This is a
credential fact, not an engine defect, and it is reproducible on the host:
`grok models` prints "You are not authenticated." Re-authenticating is the
operator's to do.

**Then that refusal ended the run.** It reported no usage, so the budget
latched `unknown_usage` — and the recovery on Sol that the engine had *already
selected* was refused before it could be invoked. One expired login stopped a
run in which every other seat was working.

## Repairs

**A vendor refusal that never reached a model reports zero spend, not unknown.**
An error envelope carrying no usage, no session id, no text and no stop reason
is the CLI refusing before it contacted the service; such a call demonstrably
spent nothing. The rule is deliberately narrow and errs towards unknown —
anything that might have started a turn, including anything unparseable, still
latches the budget. That guard is load-bearing and is pinned by its own test: a
timeout after partial work has genuinely unknown spend and must still stop the
run. This is the correct, evidenced version of a change that was attempted and
reverted after attempt 4.

**The preflight now asks whether each CLI is signed in.** An expired login is
exactly as fatal as an unreadable tree and exactly as cheap to detect. `codex
login status` gives positive proof; `grok models` prints a dead session and
**exits 0**, so only its failure wording is recognisable and the report says so
rather than implying a stronger check than it made. Claude offers no model-free
readout, which is recorded as *not applicable* rather than passing silently.

Verified in the real container with the seed mounted: the preflight exits 1 and
names the blocker —

```
grok is not signed in, so every seat on it will fail the moment it is called:
You are not authenticated.
```

Engine suite **1030 passed, 1 skipped** (gradio absent) with Docker and
installed-CLI checks; Ruff and diff-check clean. No model calls in the repair.

## Still unexercised

The worker tool-fit check (`a0f16ec`) has now been present and unreached for
four attempts. Attempt 7 got closer than any before it — a lead was selected
and a task was written — but no errand was ever commissioned.

## Blocked on

`grok login`. The preflight will refuse to launch until the session is live,
which is the intended behaviour.

## Reproduce

Verify `artifact-sha256.json`. Extract `source-after.tar.gz` (`source/` prefix)
and verify `source-after-sha256.json`. Raw stdout and vendor sessions stay
private outside every repository; `vendor-usage-extract.json` names each call's
private file and its SHA-256. Container and credential seed were removed.
