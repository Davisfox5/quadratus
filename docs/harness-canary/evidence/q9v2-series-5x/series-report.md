# Canary series

**live reliability: 5 runs per version**

Each row indexes a run directory; the raw records there are the evidence.

## Per version

Three outcomes per version, kept apart: launch and instrument (launcher exit 0,
grader not refused), controller completion (result.json), and what the grader
measured. None of them is an overall success on its own.

| version | runs | launch sound | launcher failed | instrument refused | controller completed | grader passed | median attempts | median tokens | label |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline | 5 | 0 of 5 | 5 (0 unknown) | 0 | 0 of 5 (0 unknown) | 3 of 5 graded (0 ungraded) | 8 (5 of 5 known) | 954911 (5 of 5 known) | live reliability: 5 runs per version |
| candidate | 5 | 3 of 5 | 2 (0 unknown) | 0 | 3 of 5 (0 unknown) | 5 of 5 graded (0 ungraded) | 9 (5 of 5 known) | 594858 (5 of 5 known) | live reliability: 5 runs per version |

## Per run

### baseline attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/baseline-1/project/.quadratus/runs/20260924T123359Z-aada6926`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 7
- reported tokens: 1022772 (stop threshold 1000000, checked after each call returns, not a ceiling; 22772 over it)
- input tokens: input 1005516, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 355.46223441697657
- grader: 13 passed, 0 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier
- CLI evidence rows: none

### baseline attempt 2

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/baseline-2/project/.quadratus/runs/20260924T124357Z-07a23cdf`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 8
- reported tokens: 825482 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 808165, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 344.86832208401756
- grader: 12 passed, 1 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier > closeout
- CLI evidence rows: none

### baseline attempt 3

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/baseline-3/project/.quadratus/runs/20260924T125416Z-cfbf929c`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 8
- reported tokens: 819308 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 805087, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 312.1435317500145
- grader: 12 passed, 1 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier > closeout
- CLI evidence rows: none

### baseline attempt 4

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/baseline-4/project/.quadratus/runs/20260924T130503Z-4d499557`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 8
- reported tokens: 954911 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 937355, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 381.7630114160129
- grader: 13 passed, 0 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier > closeout
- CLI evidence rows: none

### baseline attempt 5

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/baseline-5/project/.quadratus/runs/20260924T131601Z-5c9dce6c`
- runtime commit: 5d70d312868a3452c6aec9fd8461fd12fcdc31cb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 7
- reported tokens: 1121358 (stop threshold 1000000, checked after each call returns, not a ceiling; 121358 over it)
- input tokens: input 1104591, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 342.6172024169937
- grader: 13 passed, 0 failed
- only declared paths changed: no policy plan
- policy plan present: no
- roles invoked: orchestrator > orchestrator > lead > closeout > orchestrator > lead > verifier
- CLI evidence rows: none

### candidate attempt 1

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/candidate-1/project/.quadratus/runs/20260924T123956Z-c93f631c`
- runtime commit: ce0ceefe93423cd7d96bad42f39aff4667874bfb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 8
- reported tokens: 497050 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 486562, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 238.13382858404657
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
    - tool failure: `/bin/zsh -lc "git status --short && git diff -- presentation.py app.py access.py && sed -n '1,120p' app.py && sed -n '1,80p' access.py && sed -n '1,80p' auth.py && sed -n '1,80p' catalog.py"` exit 128
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None

### candidate attempt 2

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/candidate-2/project/.quadratus/runs/20260924T124944Z-d2f91e0c`
- runtime commit: ce0ceefe93423cd7d96bad42f39aff4667874bfb
- launcher: exit 1
- provenance: live launcher run (series.json)
- completed: False
- provider attempts: 8
- reported tokens: 558212 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 545979, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 270.4922615829855
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
    - tool failure: `/bin/zsh -lc 'git status --short && nl -ba app.py && nl -ba access.py && nl -ba auth.py && nl -ba catalog.py && git diff -- presentation.py app.py access.py'` exit 128
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None

### candidate attempt 3

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/candidate-3/project/.quadratus/runs/20260924T125930Z-2a7add83`
- runtime commit: ce0ceefe93423cd7d96bad42f39aff4667874bfb
- launcher: exit 0
- provenance: live launcher run (series.json)
- completed: True
- provider attempts: 9
- reported tokens: 733210 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 721287, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 330.46285558398813
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
    - tool failure: `/bin/zsh -lc "sed -n '1,120p' access.py; sed -n '1,160p' app.py; git status --short; git diff -- presentation.py app.py access.py"` exit 129
    - tool failure: `/bin/zsh -lc "/private/tmp/q9v2.nxS6sx/venv/bin/python -c 'from fastapi.testclient import TestClient; from app import app; c=TestClient(app); alpha={\"Authorization\":\"Bearer alpha-token\"}; ok=c.get(\"/records/alpha-1\",headers=alpha); cross=c.get(\"/records/beta-1?tenant=beta\",headers={**alpha,\"X-Tenant\":\"beta\",\"X-Tenant-Id\":\"beta\"}); missing=c.get(\"/records/nope\",headers=alpha); unauth=c.get(\"/records/alpha-1\"); caption=c.get(\"/caption\",params={\"title\":\"  A   deliberately l` exit 1
    - tool failure: `/bin/zsh -lc 'nl -ba access.py app.py presentation.py'` exit 1
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None

### candidate attempt 4

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/candidate-4/project/.quadratus/runs/20260924T131126Z-73e19575`
- runtime commit: ce0ceefe93423cd7d96bad42f39aff4667874bfb
- launcher: exit 0
- provenance: live launcher run (series.json)
- completed: True
- provider attempts: 9
- reported tokens: 594858 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 583106, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 272.9552365419804
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
    - tool failure: `/bin/zsh -lc "pwd && git status --short && sed -n '1,120p' app.py && sed -n '1,80p' access.py && git diff -- presentation.py app.py access.py"` exit 128
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None

### candidate attempt 5

- run directory: `/private/tmp/q9v2.nxS6sx/evidence-series/candidate-5/project/.quadratus/runs/20260924T132146Z-8eeac2b5`
- runtime commit: ce0ceefe93423cd7d96bad42f39aff4667874bfb
- launcher: exit 0
- provenance: live launcher run (series.json)
- completed: True
- provider attempts: 9
- reported tokens: 628120 (stop threshold 1000000, checked after each call returns, not a ceiling)
- input tokens: input 616863, cached missing (unknown), fresh unknown (1 rows without a cached figure)
- unknown-usage attempts: 0
- wall seconds: 273.71915441699093
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
    - tool failure: `/bin/zsh -lc "git status --short && git diff -- presentation.py app.py access.py && sed -n '1,160p' app.py && sed -n '1,120p' access.py && sed -n '1,120p' auth.py && sed -n '1,120p' catalog.py"` exit 128
  - closeout on openai:gpt-5.6-sol: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
  - orchestrator on openai:gpt-6-astra: stderr `Reading prompt from stdin...
`
    - tool failure: `None` exit None
