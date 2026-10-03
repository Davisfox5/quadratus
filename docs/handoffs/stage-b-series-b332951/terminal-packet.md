# Stage B terminal packet, v2 (series held; access/capability-confounded)

Series `/Users/davisfox/Documents/GitHub/stage-b/series-b332951`; engine `b3329518b009d2ede6e4acf2d9f90ce5a3a63748`; GameTape `1cd9264edb4429f00cde43a04a1944d0dca37f11`; packet digest `006894d640356b14f3b1bac18605e255bb98e68c666156acc7507125e0358e40`; frozen-inputs digest `61c9f0cd60f53b4a7ef49156091239c9579b5db2acc128162d0980a6ba5a95a5` (graders unchanged at packet time: True); prepared 2026-10-03T11:36:17Z; launched 2026-10-03T11:49:17Z with CLI versions {'claude': '2.1.269 (Claude Code)', 'codex': 'codex-cli 0.154.0', 'grok': 'grok 1.0.30 (04b7ffed98c6)'}; stopped 2026-10-03T13:15:34Z: integrity-failure: before grading: RuntimeError: the engine checkout /Users/davisfox/Documents/GitHub/quadratus has uncommitted changes; a cell must run on one SHA.

Bounds: {'limits': {'max_calls': 90, 'max_reported_tokens': 2500000, 'wall_seconds': 3600, 'max_concurrent_workers': 2}, 'max_tasks': 10, 'survey_recovery': 4, 'repeats': 1, 'jev_model': 'jev-1.13.0'}

Counts: executed 10, failed 0, interrupted 0, integrity-failed 0, unlaunched 4 of 14. Executed cells: 88 attempts, 31,169,606 reported tokens, summed cell duration 9,095.5 s, wall span launch-to-stop 1:26:17.

## Costs

- subscription: every vendor CLI call ran on Claude Max, ChatGPT and SuperGrok subscriptions; no per-call invoice exists
- counterfactual: $159.77 is the engine's API list-price counterfactual for the executed cells; it is not a charge
- budget_api_cost: the run budget's api_cost_usd is 0.0 on every cell because the CLI transport is excluded from that boundary (cost_boundary field); it is not evidence of zero cost
- jev: about $0.001058 estimated at the published $0.042/M input rate on 25,179 input tokens; invoice unverified
- verified_invoice: none

Jev: 14 decisions (kind {'backend': 3, 'frontend': 3, 'test': 1}; difficulty {'standard': 4, 'simple': 3}), 26,698 tokens.

## Cells

| cell | state | grade | per requirement | engine | tasks closed | attempts | tokens | s | $ counterfactual | leads (canonical → resolved) |
|---|---|---|---|---|---:|---:|---:|---:|---:|---|
| f1-preview-errors/jev-r0 | ran | 2/4 | R1:p R2:f R3:f R4:p | reported_token_threshold | 0 | 6 | 4020087 | 849.1 | 21.6786 | t1 openai:gpt-5.6-sol→? |
| f1-preview-errors/rule-r0 | ran | 2/4 | R1:p R2:f R3:f R4:p | reported_token_threshold | 1 | 8 | 4213187 | 1133.1 | 23.4656 | t1 grok:default→?, t2 claude:opus→claude-opus-5, t2 claude:opus→claude-opus-5 |
| f2-project-search/rule-r0 | ran | 4/5 | R1:p R2:p R3:f R4:p R5:p | reported_token_threshold | 0 | 6 | 2548169 | 902.6 | 11.0592 | t1 grok:default→? |
| f2-project-search/jev-r0 | ran | 4/5 | R1:p R2:p R3:f R4:p R5:p | reported_token_threshold | 0 | 8 | 2790804 | 1050.0 | 11.4691 | t1 grok:default→? |
| f3-project-rename/jev-r0 | ran | 3/5 | R1:f R2:p R3:f R4:p R5:p | reported_token_threshold | 1 | 14 | 2645558 | 1105.7 | 12.9287 | t1 grok:default→?, t2 openai:gpt-5.6-sol→? |
| f3-project-rename/rule-r0 | ran | 2/5 | R1:f R2:p R3:f R4:p R5:f | reported_token_threshold | 1 | 12 | 2825410 | 868.3 | 15.5488 | t1 grok:default→?, t2 claude:opus→claude-opus-5, t2 claude:opus→claude-opus-5, t2 claude:opus→claude-opus-5, t2 claude:opus→claude-opus-5 |
| f4-tags-export/rule-r0 | ran | 1/6 | R1:f R2:f R3:f R4:f R5:f R6:p | reported_token_threshold | 0 | 8 | 2651907 | 775.7 | 16.5344 | t1 claude:opus→claude-opus-5, t1 claude:opus→claude-opus-5, t1 claude:opus→claude-opus-5, t1 claude:opus→claude-opus-5 |
| f4-tags-export/jev-r0 | ran | 5/6 | R1:p R2:p R3:p R4:f R5:p R6:p | reported_token_threshold | 1 | 16 | 2678813 | 841.2 | 14.8196 | t1 openai:gpt-5.6-sol→?, t2 openai:gpt-5.6-sol→? |
| f5-empty-state/jev-r0 | ran | withheld |  | reported_token_threshold | 0 | 6 | 4248009 | 1007.5 | 19.9612 | t1 grok:default→? |
| f5-empty-state/rule-r0 | ran | 4/4 | R1:p R2:p R3:p R4:p | reported_token_threshold | 0 | 4 | 2547662 | 562.3 | 12.3023 | t1 grok:default→? |
| f6-download-guard/rule-r0 | prepared |  |  |  |  |  |  | None |  |  |
| f6-download-guard/jev-r0 | prepared |  |  |  |  |  |  | None |  |  |
| f7-projects-migration/jev-r0 | prepared |  |  |  |  |  |  | None |  |  |
| f7-projects-migration/rule-r0 | prepared |  |  |  |  |  |  | None |  |  |

## Jev decisions (raw): answer, default, confidence, selected-label probability, full distribution, usage

| cell | task | decision | answer | default | confidence | P(answer) | probabilities | model/host | s | in/out |
|---|---|---|---|---|---:|---:|---|---|---:|---|
| f1-preview-errors | t1 | task.kind | backend | general | 0.98 | 0.99 | {"architect": 0.0, "backend": 0.99, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.0, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 0.0, "general": 0.01, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.0, "test": 0.0} | jev-1.13.0/typesafe | 0.379 | 2092/168 |
| f1-preview-errors | t1 | task.difficulty | standard | simple | 0.54 | 0.66 | {"complex": 0.01, "rote": 0.0, "simple": 0.33, "standard": 0.66} | jev-1.13.0/typesafe | 0.201 | 1445/49 |
| f2-project-search | t1 | task.kind | frontend | general | 1.0 | 1.0 | {"architect": 0.0, "backend": 0.0, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.0, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 1.0, "general": 0.0, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.0, "test": 0.0} | jev-1.13.0/typesafe | 0.535 | 2106/168 |
| f2-project-search | t1 | task.difficulty | simple | simple | 0.54 | 0.65 | {"complex": 0.0, "rote": 0.02, "simple": 0.65, "standard": 0.33} | jev-1.13.0/typesafe | 0.272 | 1459/49 |
| f3-project-rename | t1 | task.kind | backend | general | 0.97 | 0.98 | {"architect": 0.0, "backend": 0.98, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.01, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 0.0, "general": 0.0, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.01, "test": 0.0} | jev-1.13.0/typesafe | 0.365 | 2114/168 |
| f3-project-rename | t1 | task.difficulty | simple | simple | 0.6 | 0.71 | {"complex": 0.0, "rote": 0.0, "simple": 0.71, "standard": 0.29} | jev-1.13.0/typesafe | 0.198 | 1467/49 |
| f3-project-rename | t2 | task.kind | frontend | general | 1.0 | 1.0 | {"architect": 0.0, "backend": 0.0, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.0, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 1.0, "general": 0.0, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.0, "test": 0.0} | jev-1.13.0/typesafe | 0.322 | 2366/168 |
| f3-project-rename | t2 | task.difficulty | standard | simple | 0.88 | 0.91 | {"complex": 0.01, "rote": 0.0, "simple": 0.08, "standard": 0.91} | jev-1.13.0/typesafe | 0.206 | 1719/49 |
| f4-tags-export | t1 | task.kind | backend | general | 1.0 | 1.0 | {"architect": 0.0, "backend": 1.0, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.0, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 0.0, "general": 0.0, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.0, "test": 0.0} | jev-1.13.0/typesafe | 0.388 | 2358/168 |
| f4-tags-export | t1 | task.difficulty | standard | simple | 0.66 | 0.75 | {"complex": 0.0, "rote": 0.0, "simple": 0.25, "standard": 0.75} | jev-1.13.0/typesafe | 0.269 | 1711/49 |
| f4-tags-export | t2 | task.kind | test | general | 1.0 | 1.0 | {"architect": 0.0, "backend": 0.0, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.0, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 0.0, "general": 0.0, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.0, "test": 1.0} | jev-1.13.0/typesafe | 0.361 | 1797/168 |
| f4-tags-export | t2 | task.difficulty | standard | simple | 0.53 | 0.65 | {"complex": 0.0, "rote": 0.01, "simple": 0.34, "standard": 0.65} | jev-1.13.0/typesafe | 0.203 | 1150/49 |
| f5-empty-state | t1 | task.kind | frontend | general | 1.0 | 1.0 | {"architect": 0.0, "backend": 0.0, "bulk": 0.0, "comprehend": 0.0, "concurrency": 0.0, "data": 0.0, "debug": 0.0, "decompose": 0.0, "docs": 0.0, "frontend": 1.0, "general": 0.0, "glue": 0.0, "iac": 0.0, "mobile": 0.0, "perf": 0.0, "refactor": 0.0, "review": 0.0, "scope": 0.0, "security": 0.0, "test": 0.0} | jev-1.13.0/typesafe | 0.293 | 2021/168 |
| f5-empty-state | t1 | task.difficulty | simple | simple | 0.34 | 0.51 | {"complex": 0.0, "rote": 0.0, "simple": 0.51, "standard": 0.49} | jev-1.13.0/typesafe | 0.182 | 1374/49 |

## Jev tasks: labels, ladder default for the label, lead invoked, and the rule arm's lead on the same task index

| feature | task | kind (conf / P) | difficulty (conf / P) | ladder for the label | lead invoked | rule arm's lead, same task |
|---|---|---|---|---|---|---|
| f1-preview-errors | t1 | backend (0.98 / 0.99) | standard (0.54 / 0.66) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | grok:default |
| f2-project-search | t1 | frontend (1.0 / 1.0) | simple (0.54 / 0.65) | grok:default | grok:default | grok:default |
| f3-project-rename | t1 | backend (0.97 / 0.98) | simple (0.6 / 0.71) | grok:default | grok:default | grok:default |
| f3-project-rename | t2 | frontend (1.0 / 1.0) | standard (0.88 / 0.91) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | claude:opus |
| f4-tags-export | t1 | backend (1.0 / 1.0) | standard (0.66 / 0.75) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | claude:opus |
| f4-tags-export | t2 | test (1.0 / 1.0) | standard (0.53 / 0.65) | openai:gpt-5.6-sol | openai:gpt-5.6-sol | (no such task) |
| f5-empty-state | t1 | frontend (1.0 / 1.0) | simple (0.34 / 0.51) | grok:default | grok:default | grok:default |

Ladder and pins as in `task_kinds.py` at b332951 (reading of the frozen table, not a runner output). The rule arm's leads are the orchestrator's own labels plus vendor load spreading, which put Opus on f1 t2, f3 t2 and f4 t1.

## Comparability (evidence-based; the runner's raw flags are preserved in cells/comparison.json)

| pair | comparable as Jev-vs-rule evidence | grade jev / rule | tasks closed jev / rule | reasons / note |
|---|---|---|---|---|
| f1-preview-errors | False | 2 / 2 of 4 | 0 / 1 | an assigned lead (claude:opus) was denied every project Edit/Write: rule t2 (3 denials), rule t2 (4 denials) |
| f2-project-search | True | 4 / 4 of 5 | 0 / 0 | no Opus-lead confound found in this narrow review; not certified unaffected: same lead seat (grok:default), tied 4/5, differing planner input (decider_labels=all omits the KIND instruction) and decomposition, one repeat |
| f3-project-rename | False | 3 / 2 of 5 | 1 / 1 | an assigned lead (claude:opus) was denied every project Edit/Write: rule t2 (3 denials), rule t2 (4 denials), rule t2 (4 denials), rule t2 (3 denials) |
| f4-tags-export | False | 5 / 1 of 6 | 1 / 0 | an assigned lead (claude:opus) was denied every project Edit/Write: rule t1 (3 denials), rule t1 (3 denials), rule t1 (4 denials), rule t1 (3 denials) |
| f5-empty-state | False | None / 4 of 4 | 0 / 0 | a grade was withheld by the hold's integrity check |
| f6-download-guard | False | None / None of None | None / None | not both executed |
| f7-projects-migration | False | None / None of None | None / None | not both executed |

## Usage by canonical model (invoked calls, executed cells)

| model | resolved identities | calls | input tokens | output tokens | seconds |
|---|---|---:|---:|---:|---:|
| claude:fable | claude-fable-5-1 | 15 | 3,690,545 | 63,241 | 1009.4 |
| openai:gpt-5.6-sol |  | 18 | 1,782,496 | 18,647 | 731.0 |
| claude:opus | claude-opus-5 | 24 | 18,917,413 | 383,447 | 4657.8 |
| grok:default |  | 12 | 5,815,637 | 170,522 | 2493.8 |
| openai:gpt-5.6-luna |  | 3 | 103,595 | 4,156 | 100.1 |
| claude:sonnet | claude-sonnet-5 | 2 | 188,693 | 4,516 | 41.0 |

## Claude lead calls and project write denials

| cell | task | invocation | session | denied Edit/Write | files written | commands errored | denied paths |
|---|---|---|---|---:|---:|---|---|
| f1-preview-errors/rule-r0 | t2 | 3cab41762c0b | b20ce7f3 | 3 | 0 | 2/4 | .quadratus/capture-fixtures/t2/malformed.csv, templates/index.html |
| f1-preview-errors/rule-r0 | t2 | c39e29271e1b | 0bd9e13d | 4 | 1 | 4/9 | .quadratus/capture-fixtures/t2/malformed.csv, static/js/app.js, templates/index.html |
| f3-project-rename/rule-r0 | t2 | bbffbeafb92f | 1dd2e8bb | 3 | 0 | 1/2 | static/css/style.css, static/js/app.js |
| f3-project-rename/rule-r0 | t2 | 72bc43138d66 | 5a2c09b4 | 4 | 0 | 2/2 | static/css/style.css, static/js/app.js |
| f3-project-rename/rule-r0 | t2 | 862843c90e61 | 52550eec | 4 | 0 | 1/1 | static/css/style.css, static/js/app.js |
| f3-project-rename/rule-r0 | t2 | 9ad05bde71bb | fac025d6 | 3 | 1 | 2/3 | static/css/style.css, static/js/app.js |
| f4-tags-export/rule-r0 | t1 | 20bc172df9e6 | 2c1d3b52 | 3 | 0 | 2/2 | app.py, tests/test_export_manifest.py |
| f4-tags-export/rule-r0 | t1 | 5bf5df2f3f58 | 879b2093 | 3 | 0 | 3/4 | app.py, tests/test_export_manifest.py |
| f4-tags-export/rule-r0 | t1 | 9f20f3bc97a9 | 52f37e20 | 4 | 0 | 3/3 | app.py, tests/test_export_manifest.py |
| f4-tags-export/rule-r0 | t1 | 3b78d76688ed | fa6f345b | 3 | 0 | 1/2 | app.py, tests/test_export_manifest.py |

## Capture, review and audit evidence

No executed cell reached a harness capture, a design-review verdict on a closed task, or an end-of-run audit; `design_checks` is empty and `audits` is empty on all ten. Reviews that did run are in each cell's calls (Sol requirements reviews on all ten; Opus design-review calls on f2 jev and f3 jev before their budget stops). Survey sections are empty on all ten: no run reached a re-plan.

## Corrections in v2 (Codex readback, #35)

- Jev decision counts: 14 = 7 kind + 7 difficulty; kind backend 3, frontend 3, test 1; difficulty standard 4, simple 3 (the #35 terminal comment said backend 4 and simple 4).
- Confidence and selected-label probability are distinct fields and both are reported per decision; the lowest difficulty confidence is f5 t1 simple at 0.34 (probability 0.51), not 0.54.
- 9,095.5 s is the sum of cell durations; the series' wall span from launch to stop is 1:26:17.
- hold.json's 'mechanism' promised a post-hoc grade for the withheld f5 jev cell; that grade was not run (preservation, Codex 5969458776) and hold.correction.json records the change; hold.json is unchanged as raw evidence.
- f2: no Opus-lead confound found in this narrow review, not certified unaffected.
- Costs: $159.77 is counterfactual only; Jev about $0.001 is estimated; no invoice is verified; budget api_cost_usd=0 reflects the CLI exclusion, not zero cost.

## Caveats

- Every executed cell ended on reported_token_threshold (2,500,000, applied after a response returns); no cell completed its feature by the engine's own judgement.
- claude:opus leads ran in Claude CLI permissionMode default and were denied every project Edit/Write (CLAUDE_SPEC has no write_args); pairs where the rule arm assigned an Opus lead are not comparable as Jev-versus-rule evidence. Grok and Sol leads wrote normally. Underlying reason for the hold.
- The hold was applied by appending an uncommitted marker line to docs/jev-stage-b-plan.md in the engine checkout, which the runner's integrity check reads as a changed engine and refuses to launch past; that is the technical stop mechanism, and the Opus editing-access fault is the reason.
- f3 R1 failed on both arms on a grader substring assertion stricter than the goal's wording; both frozen totals stand, the one-point gap is R5 (Codex, #35 5969338718).
- f5 jev grade withheld by the hold's integrity check; not graded post hoc.
- f6 and f7 (4 cells) were prepared and never launched.
- No claim of Jev quality benefit or savings is supported by these pairs.

## Hold

`hold.json` (raw, unchanged) and `hold.correction.json`: The post-hoc grade of the second f5 cell promised there was not run: Codex's preservation guidance (#35 comment 5969458776) excludes new grading during this step. The f5 jev grade stays withheld; grading it later is a separate decision. hold.json is left unchanged as raw evidence.

## Pre-launch liveness calls (apart from the series)

`preflight-liveness-ledger.json`: 4 calls on subscriptions, no invoice.
