# Canary series

**live sample: 1 runs per version, below the 5-run reliability threshold**

Each row indexes a run directory; the raw records there are the evidence.

## Per version

Three outcomes per version, kept apart: launch and instrument (launcher exit 0,
grader not refused), controller completion (result.json), and what the grader
measured. None of them is an overall success on its own.

| version | runs | launch sound | launcher failed | instrument refused | controller completed | grader passed | median attempts | median tokens | label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 1 | 0 of 1 | 1 (0 unknown) | 0 | 0 of 1 (0 unknown) | 0 of 1 graded (0 ungraded) | 3 (1 of 1 known) | 378070 (1 of 1 known) | live sample: 1 runs per version, below the 5-run reliability threshold |
| candidate | 1 | 0 of 1 | 1 (0 unknown) | 0 | 0 of 1 (0 unknown) | 0 of 1 graded (0 ungraded) | 3 (1 of 1 known) | 65639 (1 of 1 known) | live sample: 1 runs per version, below the 5-run reliability threshold |

## Per run

### baseline attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence2/baseline-1/project/.quadratus/runs/20260923T180322Z-ad60943c`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 3
- reported tokens: 378070
- input tokens: input 372027, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 87.74104954098584
- grader: 8 passed, 5 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead
- CLI evidence rows: none

### candidate attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence2/candidate-1/project/.quadratus/runs/20260923T180452Z-645d4d3a`
- runtime commit: d011a5c62a443bc6aef4eb4e81e0fad96873ecba
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 3
- reported tokens: 65639
- input tokens: input 65118, cached missing (unknown), fresh unknown (2 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 33.80298916599713
- grader: 5 passed, 8 failed
- only declared paths changed: yes
- policy plan present: yes
- roles invoked: orchestrator > orchestrator > lead
- CLI evidence rows:
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - lead on grok:default: stderr `Error: You’ve reached your free Grok Build usage limit for now. Get SuperGrok for much higher limits, or try again later: https://grok.com/supergrok?referrer=grok-build
`
