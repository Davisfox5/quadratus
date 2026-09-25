# Canary series

**live sample: 1 runs per version, below the 5-run reliability threshold**

Each row indexes a run directory; the raw records there are the evidence.

## Per version

Three outcomes per version, kept apart: launch and instrument (launcher exit 0,
grader not refused), controller completion (result.json), and what the grader
measured. None of them is an overall success on its own.

| version | runs | launch sound | launcher failed | instrument refused | controller completed | grader passed | median attempts | median tokens | label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 1 | 0 of 1 | 1 (0 unknown) | 0 | 0 of 1 (0 unknown) | 1 of 1 graded (0 ungraded) | 7 (1 of 1 known) | 1701844 (1 of 1 known) | live sample: 1 runs per version, below the 5-run reliability threshold |
| candidate | 1 | 1 of 1 | 0 (0 unknown) | 0 | 1 of 1 (0 unknown) | 1 of 1 graded (0 ungraded) | 9 (1 of 1 known) | 614411 (1 of 1 known) | live sample: 1 runs per version, below the 5-run reliability threshold |

## Per run

### baseline attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence3/baseline-1/project/.quadratus/runs/20260923T205001Z-175dc789`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 7
- reported tokens: 1701844
- input tokens: input 1670806, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 570.5131871670019
- grader: 13 passed, 0 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier
- CLI evidence rows: none

### candidate attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence3/candidate-1/project/.quadratus/runs/20260923T210328Z-53a7a0a0`
- runtime commit: d011a5c62a443bc6aef4eb4e81e0fad96873ecba
- launcher: exit 0
- provenance: live launcher run (series.json)
- completed: True
- provider attempts: 9
- reported tokens: 614411
- input tokens: input 602264, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 294.9324943749816
- grader: 13 passed, 0 failed
- only declared paths changed: yes
- policy plan present: yes
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier > closeout > orchestrator
- CLI evidence rows:
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - lead on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
    - tool failure: `/bin/zsh -lc "git status --short && sed -n '1,120p' app.py && sed -n '1,80p' access.py && sed -n '1,80p' catalog.py && git diff -- presentation.py app.py access.py"` exit 128
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
