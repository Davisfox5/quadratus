# Quadratus routing trial — findings before changes

Quadratus engine baseline is commit 9ac8bac. No engine or routing-policy changes were made. The experiment is in an isolated GameTape worktree based on f93d8b1. Both goals omit model names and routing labels. External wrappers record the original calls without changing their arguments, results, permissions, retries, or selection logic.

## Observed in the first feature attempt

Fable 5.1 selected an architect/complex task, seated Opus, and selected Sol plus Grok as collaborators. Opus commissioned restricted Luna twice for a design-document edit. The first patch applied. The second response began with prose and contained a corrupt unified diff. The run stopped with `Bounded editor returned no PATCH or explicit NO CHANGES result`, before any task closed or integration check ran. The first 87-line design survives; no feature code was written.

1. **Worker failures bypass lead recovery.** `WorkerPool.commission` records the failed fingerprint and re-raises. The single-worker path in `Session._draft_with_channels` does not catch that error. A malformed worker answer therefore aborts the whole run before Opus can revise the request, choose a different worker, or report an actionable blocker. The separate `commission_many` path already reports errors as outcomes. Preserve patch validation and restricted permissions; deliver a structured failure to the lead with bounded retry rules. Regression: valid first patch, malformed second patch, unchanged source after rejection, then lead recovery or explicit incomplete closeout.

2. **A successful delegation roundtrip does exist.** Luna's first call used the restricted read-only source copy, returned PATCH, and the harness applied it to the authorized project. Opus was re-invoked with the worker evidence. This is live proof, not just a configured roster. It does not prove all worker families or escalation paths.

3. **Selected collaborators are not executed collaborators.** The feature task selected Sol/Grok, but never invoked them because drafting failed first. Record both states explicitly in diagnostics; do not count the roster as coverage.

4. **The task-size promise remains unenforced.** The first document has 87 lines. Opus then commissioned a rewrite of about 127 lines despite its approximately 80–100-line task. That rewrite was rejected for format/diff validity, not scope. This reinforces the earlier large-code-edit finding; it is not proof that the larger document was saved.

See worker-failure-proof.json and rejected-worker.patch. `git apply --check` rejects the extracted complete patch even after removing the prose. Merely searching for PATCH inside prose would not make the answer safe to apply.

## Observed during the separate review probe

Fable selected review/standard. Sol's lead invocation completed, and Quadratus then actually invoked Opus as a collaborator in a fresh read-only source copy. This exercises the existing reviewer pair without a model directive in the goal.

5. **Native delegation is outside Quadratus's worker accounting.** During its review, Sol used the Codex CLI's native spawn_agent to create another Sol. That child did not pass through Quadratus WorkerPool, its errand selection, or its per-task budget. The parent recorded 203,199 input / 6,350 output; the native child separately recorded 127,405 input / 7,700 output. The CLI return and Quadratus meter included only the parent, omitting 135,105 child tokens. See review/native-delegation-evidence.json. Decide explicitly how native delegation fits the approved authority and budget model; do not silently widen or remove senior roles as a shortcut.

Claude's raw envelopes also list auxiliary Haiku model usage. Treat this as vendor-internal activity, not proof that Quadratus dispatched a Haiku worker. Keep vendor-native and Quadratus-assigned work distinct.

## Carry forward from the saved-filter trial

- Validate kind/difficulty metadata before dispatch; prose before KIND silently changed a testing assignment from the Sol pin to general/simple Grok. Preserve ASK/FETCH/control-command semantics.
- Enforce task scope and detect expansion before accepting completion; an earlier tiny fixture task grew into UI and regression-test implementation.
- Preserve and inspect partial edits after timeout before retrying a writing prompt. Do not blindly replay against an already changed tree.
- Include changed filenames in integration fingerprint failures, without broadly excluding untracked source or disabling same-tree verification.
- Record invocation/retry outcomes, available vendor usage, unknown usage, cache semantics and model identity. The current malformed-output worker calls are metered because their CLI returned; transport cancellations/timeouts in the earlier trial were missing. Native-child omission is an additional distinct case.

## Regression and acceptance target for the combined repair

Keep the current approved Fable/Astra orchestration, Opus/Sol/Grok senior roles, Sol testing pin, Sol+Opus review pair, worker-family rules, restricted editing grants, subscription transports, and Grok permission fix. Regressions should cover metadata errors, bounded patch rejection and recovery, scope expansion, timeout after partial edit, same-tree changed-path diagnostics, native versus framework delegation, and usage without double counting. Then repeat the natural-language feature trial and measure actual invocation coverage and terminal completion separately.

Tests checked during this investigation: 572 Quadratus tests and 79 GameTape tests pass in their respective environments. An initial operator check accidentally used the target environment for Quadratus and failed on its missing OpenAI dependency; rerunning in the established Quadratus environment passed. No dependency or source change was needed.

This is a diagnostic experiment, not a delivered bulk-tagging feature. The review probe's final outcome will be appended after it finishes.

## Review semantics to verify before patching

The review-only goal explicitly permits a completed review with a not-ready design verdict. Both Sol and Opus reported project/design blockers. The harness treats any collaborator text containing BLOCKING as a revision/recheck obligation; `_revision_prompt` additionally says to update the project whenever a project is selected, even when writes are not granted. The actual Fleet grant remains read-only, so this is a conflicting instruction, not evidence of an unauthorized source write. Check the final outcome before concluding that the review cannot close. A regression should distinguish defects in a review artifact from defects that the review correctly reports in its subject.

Review drafts contain absolute links into disposable source copies. Those copies are removed after the call; exported operator-facing findings need stable project-relative provenance or a durable snapshot reference. This is a reporting issue, separate from whether review source access is isolated correctly.

## Final diagnostic outcome

The read-only review completed one full task: Sol draft → Opus critique → Sol revision → Opus RESOLVED → same-tree integration gate (79 passed) → Sol closeout. No source changed during the review. The potential review-subject blocking loop did **not** occur; keep that distinction in regression coverage rather than claiming it failed.

The operator deliberately stopped further review slices after this completed cycle. Fable had already started selecting the next task, so the application records KeyboardInterrupt and completed=false, with tasks=1. This is a completed diagnostic cycle, not successful completion of the whole review goal or bulk-tagging feature. The interrupted next-planning invocation has unknown usage. See review/operator-stop.json and run 20260913T181837Z-37a87a43/result.json.

Across both new probes, actual Quadratus invocations were Fable, Opus, Sol and Luna. Grok was selected in the first task but never reached because worker drafting failed. Haiku/Sonnet/Terra/Grok worker/expert, fallback and security excursion are not covered by this experiment. Grok senior execution was already demonstrated by the earlier saved-filter trial. This is not all-worker end-to-end coverage.

Usage reconciled in usage-summary.json: 1,564,506 reported tokens plus 135,105 separately verified native-child tokens = **at least 1,699,611 tokens**, including cached/repeated input. The interrupted planning call remains unknown. No dollar invoice is inferred from this count.

Quadratus remains clean at 9ac8bac; original GameTape and the delivered saved-filter worktree remain clean. The isolated routing-trial branch retains only the new, uncommitted 87-line design document. No engine fixes, target implementation, pushes, or deployments were performed.
