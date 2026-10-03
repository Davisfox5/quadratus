# Stage B terminal packet (series held, access/capability-confounded)

Series `/Users/davisfox/Documents/GitHub/stage-b/series-b332951`; engine `b3329518b009d2ede6e4acf2d9f90ce5a3a63748`; GameTape `1cd9264edb4429f00cde43a04a1944d0dca37f11`; packet digest `006894d640356b14f3b1bac18605e255bb98e68c666156acc7507125e0358e40`; frozen-inputs digest `61c9f0cd60f53b4a7ef49156091239c9579b5db2acc128162d0980a6ba5a95a5` (graders unchanged at packet time: True); prepared 2026-10-03T11:36:17Z; launched 2026-10-03T11:49:17Z with CLI versions {'claude': '2.1.269 (Claude Code)', 'codex': 'codex-cli 0.154.0', 'grok': 'grok 1.0.30 (04b7ffed98c6)'}; stopped 2026-10-03T13:15:34Z: integrity-failure: before grading: RuntimeError: the engine checkout /Users/davisfox/Documents/GitHub/quadratus has uncommitted changes; a cell must run on one .

Bounds: {'limits': {'max_calls': 90, 'max_reported_tokens': 2500000, 'wall_seconds': 3600, 'max_concurrent_workers': 2}, 'max_tasks': 10, 'survey_recovery': 4, 'repeats': 1, 'jev_model': 'jev-1.13.0'}

Counts: executed 10, failed 0, interrupted 0, integrity-failed 0, unlaunched 4 of 14. Executed cells: 88 attempts, 31,169,606 reported tokens, 9,095.5 s, API-price counterfactual $159.77 (subscriptions; not a bill). Jev: 14 decisions, 26,698 tokens (the only metered spend).

## Cells

| cell | state | grade | per requirement | engine | tasks closed | attempts | tokens | s | $ counterfactual | leads (model) | Jev labels (P) |
|---|---|---|---|---|---:|---:|---:|---:|---:|---|---|
| f1-preview-errors/jev-r0 | ran | 2/4 | R1:p R2:f R3:f R4:p | reported_token_threshold | 0 | 6 | 4020087 | 849.1 | 21.6786 | t1 openai:gpt-5.6-sol | t1 backend (0.98); t1 standard (0.54) |
| f1-preview-errors/rule-r0 | ran | 2/4 | R1:p R2:f R3:f R4:p | reported_token_threshold | 1 | 8 | 4213187 | 1133.1 | 23.4656 | t1 grok:default, t2 claude:opus, t2 claude:opus |  |
| f2-project-search/rule-r0 | ran | 4/5 | R1:p R2:p R3:f R4:p R5:p | reported_token_threshold | 0 | 6 | 2548169 | 902.6 | 11.0592 | t1 grok:default |  |
| f2-project-search/jev-r0 | ran | 4/5 | R1:p R2:p R3:f R4:p R5:p | reported_token_threshold | 0 | 8 | 2790804 | 1050.0 | 11.4691 | t1 grok:default | t1 frontend (1.00); t1 simple (0.54) |
| f3-project-rename/jev-r0 | ran | 3/5 | R1:f R2:p R3:f R4:p R5:p | reported_token_threshold | 1 | 14 | 2645558 | 1105.7 | 12.9287 | t1 grok:default, t2 openai:gpt-5.6-sol | t1 backend (0.97); t1 simple (0.60); t2 frontend (1.00); t2 standard (0.88) |
| f3-project-rename/rule-r0 | ran | 2/5 | R1:f R2:p R3:f R4:p R5:f | reported_token_threshold | 1 | 12 | 2825410 | 868.3 | 15.5488 | t1 grok:default, t2 claude:opus, t2 claude:opus, t2 claude:opus, t2 claude:opus |  |
| f4-tags-export/rule-r0 | ran | 1/6 | R1:f R2:f R3:f R4:f R5:f R6:p | reported_token_threshold | 0 | 8 | 2651907 | 775.7 | 16.5344 | t1 claude:opus, t1 claude:opus, t1 claude:opus, t1 claude:opus |  |
| f4-tags-export/jev-r0 | ran | 5/6 | R1:p R2:p R3:p R4:f R5:p R6:p | reported_token_threshold | 1 | 16 | 2678813 | 841.2 | 14.8196 | t1 openai:gpt-5.6-sol, t2 openai:gpt-5.6-sol | t1 backend (1.00); t1 standard (0.66); t2 test (1.00); t2 standard (0.53) |
| f5-empty-state/jev-r0 | ran | withheld |  | reported_token_threshold | 0 | 6 | 4248009 | 1007.5 | 19.9612 | t1 grok:default | t1 frontend (1.00); t1 simple (0.34) |
| f5-empty-state/rule-r0 | ran | 4/4 | R1:p R2:p R3:p R4:p | reported_token_threshold | 0 | 4 | 2547662 | 562.3 | 12.3023 | t1 grok:default |  |
| f6-download-guard/rule-r0 | prepared |  |  |  |  |  |  | None |  |  |  |
| f6-download-guard/jev-r0 | prepared |  |  |  |  |  |  | None |  |  |  |
| f7-projects-migration/jev-r0 | prepared |  |  |  |  |  |  | None |  |  |  |
| f7-projects-migration/rule-r0 | prepared |  |  |  |  |  |  | None |  |  |  |


## Jev tasks: labels, selected lead, ladder default for that label, and the rule arm's lead on the same task index

| feature | task | Jev kind (P) | Jev difficulty (P) | ladder for the label | lead actually invoked | rule arm's lead, same task |
|---|---|---|---|---|---|---|
| f1-preview-errors | t1 | backend (0.98) | standard (0.54) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | grok:default |
| f2-project-search | t1 | frontend (1.00) | simple (0.54) | grok:default | grok:default | grok:default |
| f3-project-rename | t1 | backend (0.97) | simple (0.60) | grok:default | grok:default | grok:default |
| f3-project-rename | t2 | frontend (1.00) | standard (0.88) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | claude:opus |
| f4-tags-export | t1 | backend (1.00) | standard (0.66) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | claude:opus |
| f4-tags-export | t2 | test (1.00) | standard (0.53) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | (no such task) |
| f5-empty-state | t1 | frontend (1.00) | simple (0.34) | grok:default | grok:default | grok:default |

Ladder and pins as in `task_kinds.py` at b332951: rote → grok:worker, simple → grok:default, standard → gpt-5.6-sol, complex → opus; security/test → Sol. The rule arm's leads are the orchestrator's own labels plus the engine's vendor load spreading (which is what put Opus on f1 t2, f3 t2 and f4 t1). The ladder column is a reading of the frozen table, not a runner output.

## Comparability (evidence-based; runner's raw flags preserved in cells/comparison.json)

| pair | comparable as Jev-vs-rule evidence | grade jev / rule | tasks closed jev / rule | reasons |
|---|---|---|---|---|
| f1-preview-errors | False | 2 / 2 of 4 | 0 / 1 | an assigned lead (claude:opus) was denied every project Edit/Write: rule t2 (3 denials), rule t2 (4 denials) |
| f2-project-search | True | 4 / 4 of 5 | 0 / 0 |  |
| f3-project-rename | False | 3 / 2 of 5 | 1 / 1 | an assigned lead (claude:opus) was denied every project Edit/Write: rule t2 (3 denials), rule t2 (4 denials), rule t2 (4 denials), rule t2 (3 denials) |
| f4-tags-export | False | 5 / 1 of 6 | 1 / 0 | an assigned lead (claude:opus) was denied every project Edit/Write: rule t1 (3 denials), rule t1 (3 denials), rule t1 (4 denials), rule t1 (3 denials) |
| f5-empty-state | False | None / 4 of 4 | 0 / 0 | a grade was withheld by the hold's integrity check |
| f6-download-guard | False | None / None of None | None / None | not both executed |
| f7-projects-migration | False | None / None of None | None / None | not both executed |

## Usage by model (executed cells)

| model | calls | input tokens | output tokens | seconds |
|---|---:|---:|---:|---:|
| claude:fable | 15 | 3,690,545 | 63,241 | 1009.4 |
| openai:gpt-5.6-sol | 18 | 1,782,496 | 18,647 | 731.0 |
| claude:opus | 24 | 18,917,413 | 383,447 | 4657.8 |
| grok:default | 12 | 5,815,637 | 170,522 | 2493.8 |
| openai:gpt-5.6-luna | 3 | 103,595 | 4,156 | 100.1 |
| claude:sonnet | 2 | 188,693 | 4,516 | 41.0 |

## Claude lead calls and project write denials

| cell | task | invocation | denied Edit/Write | files written | denied paths |
|---|---|---|---:|---:|---|
| f1-preview-errors/rule-r0 | t2 | 3cab41762c0b | 3 | 0 | .quadratus/capture-fixtures/t2/malformed.csv, templates/index.html |
| f1-preview-errors/rule-r0 | t2 | c39e29271e1b | 4 | 1 | .quadratus/capture-fixtures/t2/malformed.csv, static/js/app.js, templates/index.html |
| f3-project-rename/rule-r0 | t2 | bbffbeafb92f | 3 | 0 | static/css/style.css, static/js/app.js |
| f3-project-rename/rule-r0 | t2 | 72bc43138d66 | 4 | 0 | static/css/style.css, static/js/app.js |
| f3-project-rename/rule-r0 | t2 | 862843c90e61 | 4 | 0 | static/css/style.css, static/js/app.js |
| f3-project-rename/rule-r0 | t2 | 9ad05bde71bb | 3 | 1 | static/css/style.css, static/js/app.js |
| f4-tags-export/rule-r0 | t1 | 20bc172df9e6 | 3 | 0 | app.py, tests/test_export_manifest.py |
| f4-tags-export/rule-r0 | t1 | 5bf5df2f3f58 | 3 | 0 | app.py, tests/test_export_manifest.py |
| f4-tags-export/rule-r0 | t1 | 9f20f3bc97a9 | 4 | 0 | app.py, tests/test_export_manifest.py |
| f4-tags-export/rule-r0 | t1 | 3b78d76688ed | 3 | 0 | app.py, tests/test_export_manifest.py |

## Caveats

- Every executed cell ended on reported_token_threshold (2,500,000, applied after a response returns); no cell completed its feature by the engine's own judgement.
- claude:opus leads ran in Claude CLI permissionMode default and were denied every project Edit/Write (CLAUDE_SPEC has no write_args); pairs where the rule arm assigned an Opus lead are not comparable as Jev-versus-rule evidence. Grok and Sol leads wrote normally.
- f3 R1 failed on both arms on a grader substring assertion stricter than the goal's wording; both frozen totals stand, the one-point gap is R5 (Codex, #35 5969338718).
- f5 jev grade withheld by the hold's integrity check (engine checkout dirtied on purpose after the rule cell was graded); not graded post hoc during preservation.
- f6 and f7 (4 cells) were prepared and never launched; the hold stopped the series before their batch.
- API-price figures are the engine's counterfactual at list prices; every call ran on subscriptions; Jev calls are the only metered spend.

## Hold

`hold.json`: {"requested_by": "Codex via Davis (chat relay), citing Davis's existing conditional authorization and stop requirements", "requested_at": "2026-10-03T13:06:35Z", "reason": "The claude:opus lead seat cannot perform its granted editing work: project Edit/Write repeatedly denied by the Claude CLI in permissionMode default (reported #35 comment 5969425308). A seat/capability-access blocker under the frozen settings.", "instruction": "hold all subsequent cell launches; preserve active f5 outcomes at a safe boundary; publish terminal evidence; no override, relaunch, replacement, permission change, e

## Pre-launch liveness calls (apart from the series)

`preflight-liveness-ledger.json`: 4 calls on subscriptions, no invoice; see file.
