# Q9-v2 series, five runs per version, 2026-09-24

Codex's step 3. Baseline `5d70d31`, candidate `ce0ceef` (exact-DONE terminal
question, verifier fan-out off), fixture `d6ce0e2`, allowance
`q9v2-series-5x-2026-09-24`: five runs per version at a 1,000,000
reported-token stop (post-return, not a ceiling), 12,000,000 batch. Runs
alternated baseline and candidate. Every attempted run is here; none was
refused or skipped. Both used a copy of the candidate's `run_fixture.py` with
`LAUNCHER_TOKENS = 1_000_000` (`run_fixture.1m.diff`).

| | baseline `5d70d31` | candidate `ce0ceef` |
| --- | --- | --- |
| grader 13 of 13 | 3 of 5 | **5 of 5** |
| review returned a verdict | 3 of 5 (2 cut off by the token stop) | **5 of 5** |
| verdict accepted code that fails the grader | 2 (runs 2, 3) | 0 |
| stopped by the token threshold | 2 of 5 | 0 of 5 |
| controller `completed` | 0 of 5 (unreachable by construction) | 3 of 5 |
| only the three declared files changed | 5 of 5 | 5 of 5 |
| reported tokens, median (range) | 954,911 (819,308 to 1,121,358) | **594,858** (497,050 to 733,210) |
| wall seconds, median | 345 | **273** |
| verifier usage beyond the seat | about 1,800 each (no delegation) | about 3,200 each (no delegation) |

Baseline runs 2 and 3 wrote `record["tenant_id"] == tenant` in `visible_to`: a
record with no `tenant_id` raises `KeyError` rather than failing closed, which
fails `test_security_shared_predicate`. Both verifiers accepted it.

Candidate runs 1 and 2 closed both tasks, passed both gates and the grader, and
their verifiers accepted, but the verdicts contained "## Two minor notes,
neither blocking" and "One non-blocking note for the record". The session's
finding detector matches BLOCKING anywhere on a line, so each was recorded as an
open finding, the loop stopped early, and the terminal question was never asked.
Baseline run 2 tripped the same detector ("neither blocking").

No verifier call in either version delegated, so the verifier restriction was
never exercised live. The single 1.1M-token verifier call of 2026-09-23 did not
recur in five baseline runs.
