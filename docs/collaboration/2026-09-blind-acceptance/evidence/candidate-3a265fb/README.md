# Candidate attempt on the blind-acceptance task, 2026-09-24

One real application run of the candidate harness, Codex's step 4 as agreed on
2026-09-24 and authorized by Davis (`freeze.json` quotes him).

## Setup

- **Engine:** candidate `3a265fb` (`claude/candidate-q9v2`), including the exact-DONE terminal question, the finding-marker detector and scoped-endpoint 0.2.0.
- **Input:** GameTape `a8772ab` plus the frozen `TASK.md`, byte-identical to every earlier attempt. The 30 file hashes are in `freeze.json`.
- **Image:** `sha256:707363c1…`. This is `quadratus-blind-preflight:20260915-application` plus `jsonschema==4.26.0`. The Q5 policy engine imports that package, and the acceptance Dockerfile omitted it (now fixed). The vendor CLIs are unchanged.
- **Isolation:** `run_isolated`, network on, native delegation off, a sign-in seed staged for the run and deleted afterwards.
- **Limits:** 60 calls, a 3,000,000 reported-token stop (post-return, not a ceiling), 2,700 s internal and 3,000 s outer. Earlier attempts ran at 24 calls, 500,000 tokens and 900 s. `blind_trial.limits.diff` is the only change to the launcher.
- **Private examiner:** opened only after the run. Its hash matched the precommitted `71cb8b06…`.

## Result

| | this run | best earlier attempt (1) | most earlier attempts |
| --- | --- | --- | --- |
| controller | incomplete, `RunBudgetExceeded` | incomplete | incomplete |
| slices closed | 4 of 6 planned (parser, row validation, assembler, endpoint) | 1 | 0 |
| private cases | **9 of 9** | 7 of 9 | 0 of 9 |
| public examiner | **11 of 13** | 9 of 13 | 0 of 13 |
| browser checks | 4 of 17 before the UI slice was stopped | 0 of 17 | 0 of 17 |
| GameTape suite | 135 passed (baseline 100) | 102 | 100 |
| calls, reported tokens, seconds | 20, 3,990,578, 2,007 | 3, 614,386, 439 | |

Gates passed on all four closed slices, and every change stayed in scope.

The two public failures:
- A Players cell of `#9 Alex QA`, number and name together, is rejected as unknown, though the examiner expects it to resolve. No reviewer caught this.
- `docs/CSV_IMPORT.md` does not exist, because the docs slice never started.

**This is not a like-for-like comparison.** This run had six times the token stop, 2.5 times the calls and three times the time of the earlier attempts. The earlier attempts stopped inside their first or second slice. This run had spent 912,735 tokens by the end of slice 2.

## Where the tokens went

| slice | lead | lead tokens | review |
| --- | --- | --- | --- |
| 1 parser | grok:default | 365,138 | none (simple) |
| 2 row validation | gpt-5.6-sol | 101,015 | Opus collaborator 188,721, revision, recheck |
| 3 assembler | gpt-5.6-sol | 138,626 | Opus collaborator 100,929, revision |
| 4 endpoint | grok:default | 629,156 | none (simple) |
| 5 UI | grok:default | **1,905,496 in one call, 552 s** | stopped |

Orchestration, with Astra holding the seat because Fable was out of quota, took 41k to 71k per decision. The stop is checked after each call returns, so the single slice-5 call took the run from 2,085,082 to 3,990,578. That is 990,578 over the stop.

## Why this attempt did better, and where it can improve

**What the records support.** Astra split the brief into bounded slices, each with a line budget. Each slice passed its gate. The two standard slices got a cross-vendor review, and both reviews led to a revision. **What they don't.** The budget increase is a large confound, and a single run cannot separate it from the harness.

**Improve:**
1. **Nothing bounds a single call.** Grok leads run as full agents: 365k, 629k and 1.9M, against 101k to 139k for Sol leads.
2. **Family resolution.** Every slice, including the endpoint and the UI, resolved to `pure-logic`, because GameTape has no `.quadratus/policy.json`. The scoped-endpoint rules never applied.
3. **The `#number name` players case.** It went unreviewed because slice 2 had review, and the Opus collaborator did not raise it.

**Never exercised:** workers (Q1, Q7), the JSON security verdict, security excursions, and the scoped-endpoint and ownership rules.

## Files

Run records as the engine wrote them, `source-after.tar.gz` with per-file hashes, `independent-scoring.json`, the freeze and outer result, and the launcher's limits diff. The private vendor sessions and the model artifacts are hash manifests only (`private-evidence-sha256.json`, `artifact-sha256.json`). Nothing from the private examiner is here beyond its pass count.
