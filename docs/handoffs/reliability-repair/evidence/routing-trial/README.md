# Unmodified routing trial

Quadratus source baseline: 9ac8bac, branch codex/project-workflow. No routing, prompt, retry, permission, or accounting implementation changes are included in this trial. `run.py` adds external observation wrappers that pass through original arguments, return values, and exceptions.

Target: sports-video-tagger-routing-trial, branch quadratus/bulk-edit-routing-trial, baseline f93d8b1. Baseline tests: 79 passed. Existing saved-filter trial and original checkout remain separate.

Goal: safe bulk clip tagging with preview, stale-edit rejection, deterministic concurrency regressions, compact UI, and a final code review. The goal contains no model names or routing labels. Normal default adversarial mode and subscription CLI transports are used. Maximum 12 tasks; provider timeouts and retry settings unchanged.

Expected core coverage: orchestrator decomposes; complex design can seat Opus; standard work and test tasks can seat Sol; simple implementation can seat Grok; an explicit review should seat the existing Sol/Opus pair. These are expectations, not claimed results. Helper commissioning (Haiku/Luna/Grok worker and escalations) is optional and must be observed separately. Fallback and security excursion are conditional paths, not expected in ordinary feature work. No natural feature can guarantee every conditional or fallback seat without intervention.

Evidence: events.jsonl records actual model invocations, parsed task metadata, selected leads/collaborators, CLI starts/ends, outcomes, elapsed time, and available usage. transport-*.json preserves returned vendor envelopes, including envelopes whose extraction raises. A terminated call can still have unknown usage. console.log preserves existing progress messages. The target .quadratus/runs directory holds the application's own status, report, artifacts, diff, and ledger.

Do not treat all core models being called as full worker-tree coverage, or tests passing as autonomous completion. Preserve observed failures for the combined repair batch; do not repair the target or engine while the trial runs.

## First run observed result

Run 20260913T181033Z-443eee0d stopped incomplete after about 6.5 minutes, before any task closed or integration gate ran. Fable 5.1 selected architect/complex, with Opus lead and Sol plus Grok collaborators. Opus delegated two format/edit errands to restricted Luna. The first patch applied (87-line design document). The second response had prose before PATCH and an invalid unified diff. The exact response and a non-mutating git apply --check reproduction are saved. Removing prose alone would not make this patch usable.

The single-worker exception propagated out of WorkerPool.commission and Session._draft_with_channels rather than becoming evidence the lead could use to revise or reroute the errand. Sol/Grok collaborators never executed in this run because drafting did not finish. No model roles or runtime code were modified.

Known usage for the first run: Fable 1 call, 97,546 input / 2,877 output; Opus 2 calls, 519,769 input / 20,524 output; Luna 2 calls, 64,675 input / 4,636 output. Input includes cache. This is 710,027 tokens including repeated context. Unlike a transport timeout, this response-format failure occurs after the meter records returned usage, so these five calls do appear in the application's ledger. The raw Claude envelopes also contain auxiliary Haiku usage; that is vendor-internal activity, not a Quadratus-assigned Haiku worker.

A separate read-only review goal in review/ was then started to cover the peer-review path independently of the blocked editing path. It contains no model names or routing labels. Do not describe this as successful continuation of the interrupted feature build.

## Additional review-run observation

Fable selected review/standard: Sol lead, Opus collaborator. Sol completed its draft and Opus was actually invoked. Sol also used the vendor CLI's native spawn_agent to start a second Sol, outside Quadratus WorkerPool. The parent session recorded 203,199 input and 6,350 output; its child recorded another 127,405 input and 7,700 output. The CLI return reported exactly the parent count, omitting 135,105 child tokens from Quadratus's meter. See review/native-delegation-evidence.json. Parent/child usage here is cumulative per session and is not added to intermediate snapshots.
