# stockroom benchmark

One job for comparing the session engine with a single model on the same goal.

- `seed/` is the starting project (a small inventory service; 12 tests).
- `goal.txt` is the goal: stock reservations across the store, the HTTP API,
  the command line and the docs, with refusal codes, strict JSON, a schema
  upgrade and a no-overselling rule under threads.
- `graders/` holds 116 hidden tests. They are never shown to a run.
  `graders/grade.sh PROJECT OUT.json` runs them against a finished project and
  writes `{"passed", "total"}`, the grade file `tools/compare_runs.py` reads.

To run it: copy `seed/` somewhere, `git init` and commit it, then start a run
with `--project` on the copy, the text of `goal.txt` as the goal, and
`--check "python -m pytest -q --quadratus-report={report}"`. The check needs
that report option: without it no test case is named and no requirement can
be shown.

## First results, 2026-11-12 (engine at 916c7e3)

| Run | Graders | Calls | Reported tokens | Outcome |
| --- | --- | --- | --- | --- |
| `--baseline openai:sol` | 116 of 116 | 1 | about 378,000 | check passed |
| `--baseline claude:opus` | 116 of 116 | 1 | about 765,000 | check passed |
| engine, 500k per call | not graded | 5 | about 1,200,000 | stopped in task 1: a revision reported 511,672 tokens against the 500,000 per-call ceiling |
| engine, 800k per call, 10 rounds, 6M total | 114 of 116 | 22 | about 5,000,000 | stopped before task 4 (docs): its route was priced at 1,000,000 tokens with 725,944 left; no audit ran |

| engine, 800k per call, 10 rounds, 9M total (2026-11-13, at e3f6aee) | 116 of 116 | 34 | about 8,740,000 | six tasks closed; the audit ran and held 6 of 22 requirements NOT MET; the goal question was then refused with 59,011 tokens left, so the run reported incomplete |

The second engine run's two misses are the two documentation graders; its
code passed every code grader. Grok was not seated in any engine run: no
task was labelled rote.

The third run spent, by role: revisions 2.2M, lead drafts 1.8M, reviews 1.7M,
rechecks 1.2M, planning 1.1M, the audit 0.7M. Its audit findings were edge
cases the graders do not test. One was probed afterwards: a timestamp of
10**400 raises OverflowError in that build and in both baseline builds.
