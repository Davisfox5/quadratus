# Q9-v2 live records, 2026-09-23

Run on Davis's Mac (native, zsh) by a local Claude Code session (Claude Opus
5.5), following `docs/harness-canary/mac-run-2026-09-23.md` at `d13beee`, with
the deviations listed below. Two live runs: one baseline, one candidate.
Records only; review of what they mean is Claude's (Q10 posture).

## Heads

| what | sha |
| --- | --- |
| baseline runtime | `5d70d312868a3452c6aec9fd8461fd12fcdc31cb` |
| candidate runtime | `986c934804dcfb16869562400f3655019c41c66c` |
| fixture | `d6ce0e236ec523ae3eb49316ffb4e1d9312c4267` |
| grader `test_contract.py` sha256 | `80f1cd5af0112c3f8a786429e38bd4d8648acdb332a87eab8baf27a0138eb9a9`, unchanged after both runs |
| operator prompt | `d13beee` (the scratch-path fix below is `2742f15`) |

## Results

| | baseline | candidate |
| --- | --- | --- |
| allowance batch | `q9v2-pair-2026-09-23` | `q9v2-candidate-1m-2026-09-23` |
| per-run reported-token stop | 500,000 | **1,000,000** |
| series exit | 1 | 1 |
| launcher exit | 1 | 1 |
| controller `completed` | false | false |
| engine error | `RunBudgetExceeded: Run stopped: reported_token_threshold` | none (`""`) |
| tasks closed | 1 of 2 (task 1 by `grok:default`; stopped during task 2) | 2 of 2 (task 1 by `grok:default`, task 2 by `openai:gpt-5.6-sol`) |
| provider attempts | 6 | 8 |
| reported tokens | 556,518 | 634,736 |
| unknown-usage attempts | 0 | 0 |
| wall seconds | 326 | 371 |
| external grader, 13 cases | **13 passed, 0 failed** | **13 passed, 0 failed** |
| files changed vs the prepared copy | `presentation.py`, `app.py`, `access.py` | `presentation.py`, `app.py`, `access.py` |
| policy plan | none (baseline has no policy engine) | present; only declared paths changed |
| roles invoked | orchestrator > orchestrator > lead > closeout > orchestrator > lead | orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier > closeout |

Both runs: `claude:fable` reported its window exhausted on the first call and
the orchestrator seat fell to `openai:gpt-6-astra`, as designed. Neither run
touched `auth.py`, `catalog.py`, `README.md`, tests or policy.

`series-report.md` is the aggregator's output, unedited. Its per-version
`grader passed` column shows `0 of 1 graded` for both versions although each
grader passed 13 of 13; that column only counts runs whose launcher exited 0.
The per-run sections below the table carry the grader's own result.

## Deviations from the prompt, in order

1. **Step 2 diagnostic.** Run from this session's default directory (a stale
   Quadratus checkout), `python -c "import quadratus"` printed that checkout.
   Cause: `-c` puts the working directory first on `sys.path`. The launch path
   was verified to bind each version to its own runtime and commit, and every
   later step ran from inside the scratch directory.
2. **Step 4, first attempt blocked.** `grok models` answered `You are not
   authenticated.` and the grok CLI rewrote `~/.grok/auth.json` in the same
   second (14:56:28Z); every later call reported signed in. Re-run on Davis's
   instruction at 15:18Z: `ok: true`, `blockers: []`. The blocked report is
   `deviations/preflight-prebatch.BLOCKED-1.json`.
3. **Step 6, first attempt refused at admission.** `mktemp -d /tmp/...` gave a
   `/tmp` path; `/tmp` is a symlink to `/private/tmp` on macOS, and
   `series.py` requires `--grader-command` to name the resolved grader path.
   Refused before any slot was claimed (`deviations/step6.refused-1.out`,
   `deviations/evidence-baseline.refused-1.log`). Re-run with the resolved
   scratch path; the prompt is fixed at `2742f15`.
4. **The candidate ran under a second allowance with a 1,000,000 stop.** The
   pair series stopped after the baseline as the prompt requires (launcher
   exit 1). The baseline's in-flight overshoot (556,518 against a 500,000
   stop) also left the pair batch unable to admit the candidate: 556,518 spent
   plus 500,000 reserved exceeds the 1,000,000 batch ceiling. Davis then
   authorized, verbatim: "Yes, I want the candidate run. Let's expand token
   limits to 1 million and see what happens. You may publish any time. Don't
   ask me". Record: `deviations/allowance-1m.json` (only `batch_id`, `source`,
   `instruction`, `recorded_at`, `runs` and `max_reported_tokens_each` differ
   from the pair record) with its own ledger and a fresh pre-batch preflight.
5. **The candidate's launcher is not byte-identical to the candidate
   checkout's.** `run_fixture.py` hard-codes a 500,000 ceiling and refuses a
   record that asks for more, so the candidate ran a copy with
   `LAUNCHER_TOKENS = 1_000_000` and the preflight banner reading that
   constant. Diff: `deviations/run_fixture.1m.diff`; sha256 in
   `deviations/run_fixture.1m.sha256`. `preflight.py` beside it is unchanged.
   The engine under test (`quadratus/`) is exactly `986c934`; the runtime
   commit and binding checks passed against it.

The two runs therefore did not run under the same token stop. The baseline
never reached its second close-out; the candidate spent more than the
baseline's stop to close both tasks.

`__pycache__` directories and empty ledger lock files were not copied.
