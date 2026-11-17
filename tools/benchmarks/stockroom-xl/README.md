# stockroom-xl benchmark

The stockroom job made about twice as large: the 18 reservation items of
`../stockroom/goal.txt` plus 20 more (an event log, stock adjustments, an
expiry sweep, bulk reservations, idempotency keys, their routes and commands).
It uses the same seed, `../stockroom/seed/`.

- `goal.txt` is the goal (39 numbered items).
- `graders/` holds 291 hidden tests: the 116 of the smaller job and 175 new
  ones. `graders/grade.sh PROJECT OUT.json` runs them.
- `NOTES.md` lists every place the goal is open to two readings and which one
  the graders assume. The graders were validated against a separate reference
  build, 25 deliberately broken copies of it and 6 legitimate variants.

## First results, 2026-11-13

| Run | Graders | Calls | Reported tokens | Time |
| --- | --- | --- | --- | --- |
| `--baseline openai:sol` | 291 of 291 | 1 | about 695,000 | 10 min |
| `--baseline claude:opus` | 290 of 291 | 1 | about 1,460,000 | 10 min |

Against the smaller job (378,000 and 765,000 tokens), each model's cost grew
about 1.8 to 1.9 times for about twice the work. Opus's one miss is the strict
reading of "a refused call changes nothing" (NOTES.md, reading 8). The engine
has not been run on this goal.
