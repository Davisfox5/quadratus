# stockroom-change benchmark

A job that changes existing code, where `stockroom` builds onto a small seed.

- `seed/` is the finished stockroom build from the eighth lean run (about
  2,100 lines with its tests), with six bugs planted and the 77 tests that
  caught them removed. 432 tests pass on it.
- `goal.txt` asks for the six bugs fixed (reported as symptoms) and four
  features: `extend`, partial commit, `adjust` and `list_reservations`, across
  the store, the HTTP API, the command line and the docs. No existing test
  may be edited.
- `graders/` holds 205 hidden tests. One of them re-runs the seed's own tests
  (`graders/original/`) against the finished project. The unchanged seed
  passes 15 of 205.

Run it as the stockroom README describes, with this folder's seed and goal.

## First results, 2026-11-17 (engine at 9046a4a)

| Run | Graders | Time | Calls | Reported tokens | Outcome |
| --- | --- | --- | --- | --- | --- |
| `--baseline claude:opus` | 205 of 205 | about 4 min | 1 | about 983,000 | check passed |
| `--baseline openai:sol` | 205 of 205 | about 7 min | 2 | about 736,000 | check passed after one fix call |
| engine, `--lean` | 205 of 205 | 49 min | 49 | about 3,465,000 | complete |

The two baselines also served to check the graders: two independent builds
pass all 205.

The engine's first five tasks (two serial, three side by side) took it to
its first audit. That audit and the three after it each sent work back, and
tasks t6 to t9 answered them. The graders test none of what those four tasks
added; t9 added a 500 `internal_error` response the goal does not name. By
role: the audit 14 calls and 1,789 s, planning 9 calls and 868 s, lead
drafts 687 s, reviews 509 s. The last audit kept 1 of 31 verdicts, because
t9 changed the store and the API widely.

The plan queue ran both of its waves with no planning call between them. No
audit question went to the planner as a ruling.
