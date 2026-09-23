# Q9-v2 second rerun at a 1,000,000 token stop, 2026-09-23

Grok signed into the paid account (the first rerun hit the free-tier limit).
Candidate `d011a5c` carries the completion-only terminal question (`201f90d`).
Both runs used a copy of the candidate's `run_fixture.py` with
`LAUNCHER_TOKENS = 1_000_000` (`run_fixture.1m.diff`); the engines under test
are exactly `5d70d31` and `d011a5c`.

| | baseline `5d70d31` | candidate `d011a5c` |
| --- | --- | --- |
| allowance | `q9v2-rerun2-1m-2026-09-23` | `q9v2-rerun2-candidate-1m-2026-09-23` |
| series / launcher exit | 1 / 1 | **0 / 0** |
| controller `completed` | false | **true** |
| engine error | `RunBudgetExceeded: reported_token_threshold` | none |
| tasks closed | 1 of 2 (stopped in task 2's verification) | 2 of 2, then goal confirmed at the cap |
| attempts, reported tokens, seconds | 7, 1,701,844, 571 | 9, 614,411, 295 |
| grader, 13 cases | 13 passed | 13 passed |
| files changed | `presentation.py`, `app.py`, `access.py` | same three, all declared |

Candidate sequence: Fable out of quota, seat to `openai:gpt-6-astra`; task 1
closed by `grok:default`; task 2 closed by `openai:gpt-5.6-sol` after a
`claude-opus-5` verifier; then "task cap reached; asking openai:gpt-6-astra
whether the goal is met" and "the orchestrator confirms the goal met at the
task cap".

Baseline overshoot: one call, the `claude-opus-5` verifier on task 2, reported
1,096,136 tokens (1,076,547 input) from a run total of 605,708. The stop is
checked after a call returns, so it cannot bound a single call. That left batch
`q9v2-rerun2-1m` (2,500,000) unable to admit the candidate (1,701,844 +
1,000,000), so the candidate ran under its own record; no candidate slot was
taken in the first.

The two allowance records here have account email addresses in `source`
replaced with `[email redacted]`. The ledgers' `record_sha256` refer to the
unredacted originals, whose sha256 are in `originals.sha256`.

Grader unchanged after both runs (`80f1cd5a…`). All slots closed with known usage.
