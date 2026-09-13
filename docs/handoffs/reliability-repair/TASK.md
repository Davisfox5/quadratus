# Coordinated Quadratus reliability repair — cloud implementation

The user authorized one combined repair based on two live GameTape trials. Read this directory's README.md first. The published branch includes the actual 9ac8bac engine baseline plus this handoff. Record your current HEAD as the repair base before implementing. Read applicable repository instructions, CLAUDE.md, docs/PROJECT_WORKFLOW.md, docs/GAMETAPE_TRIAL.md, and evidence/routing-trial/FINDINGS.md through its final outcome. Inspect the included exact response and usage fixtures. Give a short plan, then implement; do not restart a broad audit or stop because live Mac services are unavailable.

## Verified behavior to preserve

- Fable 5.1 naturally selected architect/complex: Opus lead with Sol/Grok collaborators.
- Opus commissioned restricted Luna; its first patch applied through the harness.
- Luna's second response had leading prose AND a corrupt diff. Its exception aborted the run before the lead could recover or the collaborators could execute.
- A separate review completed Sol draft, Opus critique, Sol revision, Opus RESOLVED, same-tree passing gate, and closeout.
- Sol also used native CLI delegation to spawn another Sol. The native child's 135,105 tokens were absent from the parent-only CLI usage reported to Quadratus.
- Not every worker, escalation or fallback was tested. Selected collaborators are not evidence of actual invocations. The suspected review-completion loop did not occur.

## Implement together

1. Validate task kind/difficulty and control-message handling before dispatch. Prose before KIND must not silently turn a testing assignment into general/simple work. Preserve valid ASK/FETCH/CONSULT/WORKER/DONE responses. Use bounded correction or explicit failure where interpretation is ambiguous.

2. Return worker transport, response-format and patch-validation failures to the commissioning lead as actionable outcomes. Let it revise or reroute within approved rules, or close incomplete. Preserve budgets, failed fingerprints and repeated-failure safeguards. Do not accept corrupt patches or widen restricted tools. Reproduce the actual first-success/second-rejection sequence from evidence.

3. Check task scope against actual changed paths and meaningful change bounds. Make intended result and acceptance criteria explicit. Detect an undersized task expanding into a whole feature. Preserve user edits and partial work rather than silently rolling them back. Keep useful project context available to senior agents.

4. Inspect and preserve partial edits after timeout/cancellation before deciding to retry. Never blindly replay a writing prompt against an already changed tree. Bound recovery, clean up child processes, and persist sufficient in-flight task state for an honest resumable handoff.

5. Make native delegation visible and reconcile available child-session usage without double counting cumulative snapshots. Distinguish framework workers, native helpers and auxiliary vendor activity. Expose delegation or budget enforcement the harness cannot observe or control. Do not silently change model authority or remove senior capabilities to make this easier. Fixture-test native-session reconciliation in Linux; do not require access to the user's actual session directory.

   Record every invocation/retry with task, role, requested and resolved model when available, selected versus invoked state, elapsed time, outcome, and usage. Unknown is not zero. Separate subscription usage from hypothetical API cost; preserve cache semantics. Transport failures and post-return response failures need distinct accounting.

6. Include changed filenames in integration fingerprint failures without ignoring unexpected source or weakening same-tree verification. Make revision instructions respect read-only grants. Preserve successful review completion when the subject correctly receives a not-ready verdict. Stabilize exported source references that currently point into deleted review snapshots.

## Settled role and permission constraints

Fable orchestrates; Astra is its only fallback. Opus, Sol and Grok retain their approved senior roles. Testing remains pinned to Sol; explicit review uses Sol+Opus. Preserve worker-family/escalation rules, restricted editing, source-copy isolation, project grants, subscription defaults, and Grok permission fix 022de34. Existing explicitly selected API support is not a defect; do not remove it. Do not introduce automatic paid fallback, invoke billed APIs, or force model participation merely to populate a coverage table.

## Verification and delivery

Add focused regressions for observed failures and deterministic temporary-repository tests for scope, malformed patches, partial-edit recovery, changed-path diagnostics, native-child accounting and cancellation cleanup. Run the full Quadratus suite and Ruff in the new environment. Baseline was 572 tests on the Mac; report actual cloud results and any platform differences. The supplied GameTape tree's baseline is 79 tests. Do not use one project's environment or working directory as evidence for the other.

Commit the coordinated fixes locally. Include a durable implementation report covering changes, evidence, tests, limitations, and exact commit IDs. Do not check in native session logs, raw vendor envelopes, credentials or the whole evidence dump; use minimal sanitized regression fixtures where needed.

Export only your new repair commits relative to the HEAD you recorded at the start, as a Git patch series (`git format-patch --binary --stdout "$REPAIR_BASE"..HEAD`) or a bundle with that prerequisite so they can be imported on the Mac. Do not push or deploy without user authorization.

The live acceptance step is deliberately deferred, not a blocker for cloud completion: after importing your fix on the Mac, repeat evidence/routing-trial/goal.txt in a fresh isolated GameTape worktree based on f93d8b1, through the normal subscription pipeline, without adding model names or forced classification. Return portable instructions for that check and the expected telemetry. Report cloud implementation/offline verification separately from live acceptance pending. Do not claim unattended success or all-worker coverage without that evidence.
