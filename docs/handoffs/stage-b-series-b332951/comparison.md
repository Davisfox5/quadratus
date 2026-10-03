| task | arm | r | grade | engine done | tasks | closed | checks failed | recovery/hyp | leads | kind/difficulty | calls | tokens | s | stop |
|---|---|---:|---|---|---:|---:|---:|---|---|---|---:|---:|---:|---|
| f1-preview-errors | jev | 0 | 2/4 | False | 0 | 0 | 0 | 0/0 | openai:gpt-5.6-sol | backend/standard | 6 | 4020087 | 849.1 | reported_token_threshold |
| f1-preview-errors | rule | 0 | 2/4 | False | 1 | 1 | 0 | 0/0 | grok:default+claude:opus | orchestrator | 8 | 4213187 | 1133.1 | reported_token_threshold |
| f2-project-search | jev | 0 | 4/5 | False | 0 | 0 | 0 | 0/0 | grok:default | frontend/simple | 8 | 2790804 | 1050.0 | reported_token_threshold |
| f2-project-search | rule | 0 | 4/5 | False | 0 | 0 | 0 | 0/0 | grok:default | orchestrator | 6 | 2548169 | 902.6 | reported_token_threshold |
| f3-project-rename | jev | 0 | 3/5 | False | 1 | 1 | 0 | 0/0 | grok:default+openai:gpt-5.6-sol | backend,frontend/simple,standard | 14 | 2645558 | 1105.7 | reported_token_threshold |
| f3-project-rename | rule | 0 | 2/5 | False | 1 | 1 | 0 | 0/0 | grok:default+claude:opus | orchestrator | 12 | 2825410 | 868.3 | reported_token_threshold |
| f4-tags-export | jev | 0 | 5/6 | False | 1 | 1 | 0 | 0/0 | openai:gpt-5.6-sol | backend,test/standard | 16 | 2678813 | 841.2 | reported_token_threshold |
| f4-tags-export | rule | 0 | 1/6 | False | 0 | 0 | 0 | 0/0 | claude:opus | orchestrator | 8 | 2651907 | 775.7 | reported_token_threshold |
| f5-empty-state | jev | 0 |  | False | 0 | 0 | 0 | 0/0 | grok:default | frontend/simple | 6 | 4248009 | 1007.5 | reported_token_threshold |
| f5-empty-state | rule | 0 | 4/4 | False | 0 | 0 | 0 | 0/0 | grok:default | orchestrator | 4 | 2547662 | 562.3 | reported_token_threshold |
| f6-download-guard | jev | 0 |  | prepared |  |  |  |  |  | -/- |  |  | None |  |
| f6-download-guard | rule | 0 |  | prepared |  |  |  |  |  | default |  |  | None |  |
| f7-projects-migration | jev | 0 |  | prepared |  |  |  |  |  | -/- |  |  | None |  |
| f7-projects-migration | rule | 0 |  | prepared |  |  |  |  |  | default |  |  | None |  |

| task | arm | engine task | lead | kind | difficulty |
|---|---|---|---|---|---|
| f1-preview-errors | jev | t1 | openai:gpt-5.6-sol | backend | standard |
| f1-preview-errors | rule | t1 | grok:default |  |  |
| f1-preview-errors | rule | t2 | claude:opus |  |  |
| f2-project-search | jev | t1 | grok:default | frontend | simple |
| f2-project-search | rule | t1 | grok:default |  |  |
| f3-project-rename | jev | t1 | grok:default | backend | simple |
| f3-project-rename | jev | t2 | openai:gpt-5.6-sol | frontend | standard |
| f3-project-rename | rule | t1 | grok:default |  |  |
| f3-project-rename | rule | t2 | claude:opus |  |  |
| f4-tags-export | jev | t1 | openai:gpt-5.6-sol | backend | standard |
| f4-tags-export | jev | t2 | openai:gpt-5.6-sol | test | standard |
| f4-tags-export | rule | t1 | claude:opus |  |  |
| f5-empty-state | jev | t1 | grok:default | frontend | simple |
| f5-empty-state | rule | t1 | grok:default |  |  |

| pair | comparable | grade jev | grade rule | lead changed | both engine done | jev done | rule done | tokens (jev - rule) | seconds (jev - rule) | start gap s |
|---|---|---|---|---|---|---|---|---:|---:|---:|
| f1-preview-errors r0 | yes | 2/4 | 2/4 | True | False | False | False | -193100 | -283.9999999999999 | 0.0 |
| f2-project-search r0 | yes | 4/5 | 4/5 | False | False | False | False | 242635 | 147.39999999999998 | 0.0 |
| f3-project-rename r0 | yes | 3/5 | 2/5 | True | False | False | False | -179852 | 237.4000000000001 | 0.0 |
| f4-tags-export r0 | yes | 5/6 | 1/6 | True | False | False | False | 26906 | 65.5 | 0.0 |
| f5-empty-state r0 | no: jev integrity: before grading: RuntimeError: the engine checkout /Users/davisfox/Documents/GitH |  | 4/4 | False | False | False | False | 1700347 | 445.20000000000005 | 0.0 |
| f6-download-guard r0 | no: jev prepared; rule prepared |  |  | None | False | None | None | None | None |  |
| f7-projects-migration r0 | no: jev prepared; rule prepared |  |  | None | False | None | None | None | None |  |

Series stopped: integrity-failure: before grading: RuntimeError: the engine checkout /Users/davisfox/Documents/GitHub/quadratus has uncommitted changes; a cell must run on one SHA (after f5-empty-state/jev-r0).

Cells 14: ran 10, failed 0, interrupted 0, pending 4. Packet 006894d64035. Paired comparison on frozen goals: the grade column is the frozen independent graders' verdict, 'engine done' is the engine's own completion claim and never the grade; no accuracy or savings claim beyond these cells, and a faster failed cell is not a win
