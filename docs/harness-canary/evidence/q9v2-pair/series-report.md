# Canary series

**live sample: 1 runs per version, below the 5-run reliability threshold**

Each row indexes a run directory; the raw records there are the evidence.

## Per version

Three outcomes per version, kept apart: launch and instrument (launcher exit 0,
grader not refused), controller completion (result.json), and what the grader
measured. None of them is an overall success on its own.

| version | runs | launch sound | launcher failed | instrument refused | controller completed | grader passed | median attempts | median tokens | label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 1 | 0 of 1 | 1 (0 unknown) | 0 | 0 of 1 (0 unknown) | 0 of 1 graded (0 ungraded) | 6 (1 of 1 known) | 556518 (1 of 1 known) | live sample: 1 runs per version, below the 5-run reliability threshold |
| candidate | 1 | 0 of 1 | 1 (0 unknown) | 0 | 0 of 1 (0 unknown) | 0 of 1 graded (0 ungraded) | 8 (1 of 1 known) | 634736 (1 of 1 known) | live sample: 1 runs per version, below the 5-run reliability threshold |

## Per run

### baseline attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence/baseline-1/project/.quadratus/runs/20260923T154932Z-68cb919c`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 6
- reported tokens: 556518
- input tokens: input 545745, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 326.38619091699366
- grader: 13 passed, 0 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead
- CLI evidence rows: none

### candidate attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence/candidate-1/project/.quadratus/runs/20260923T160545Z-e8926854`
- runtime commit: 986c934804dcfb16869562400f3655019c41c66c
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 8
- reported tokens: 634736
- input tokens: input 623178, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 371.035053708998
- grader: 13 passed, 0 failed
- only declared paths changed: yes
- policy plan present: yes
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier > closeout
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
    - tool failure: `/bin/zsh -lc "sed -n '1,120p' app.py; sed -n '1,80p' access.py; git status --short; git diff -- presentation.py app.py access.py"` exit 129
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
