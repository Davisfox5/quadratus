# Q9-v2 rerun at a 1,000,000 token stop, 2026-09-23

Both versions under one allowance (`q9v2-rerun-1m-2026-09-23`, 1,000,000 per run,
2,500,000 batch), after the completion-only terminal question landed
(`201f90d`, candidate `d011a5c`). Same launcher change as the first candidate
run: `LAUNCHER_TOKENS = 1_000_000` in a copy of the candidate's
`run_fixture.py` (`run_fixture.1m.diff`, `run_fixture.1m.sha256`); the engine
under test is exactly `d011a5c`. The runner script is `rerun.zsh`: the
candidate starts only if the baseline launched, since the baseline cannot
report complete at a task cap.

## Result: stopped by the Grok quota, not measured

| | baseline `5d70d31` | candidate `d011a5c` |
| --- | --- | --- |
| engine error | `WindowExhausted: grok:default` | `WindowExhausted: grok:default` |
| attempts, reported tokens, seconds | 3, 378,070, 88 | 3, 65,639, 34 |
| grader, 13 cases | 5 failed, 8 passed | 8 failed, 5 passed |
| files changed | `presentation.py` | none |

The grok CLI answered "You've reached your free Grok Build usage limit for
now. Get SuperGrok for much higher limits, or try again later." on the first
task-1 call in both runs. Fable was also out of quota; the orchestrator seat
fell to `openai:gpt-6-astra` as designed. Neither run reached task 2 or the
terminal question, so this batch says nothing about the completion fix.
Grader unchanged (`80f1cd5a…`). Both slots closed with known usage.
