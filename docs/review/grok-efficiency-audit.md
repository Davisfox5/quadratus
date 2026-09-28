# Grok measurement audit (lane 8)

Session: Grok Build measurement-audit lane 8, authorized by Davis, started 2026-09-28T02:37Z. Ownership claim: PR 25 comment 5862284638. This file is the only repo artifact this session adds. The scorecard and the engine were not edited. No live or vendor run, no profile activation, no budget or authority change, and no preserved run evidence was modified (reproductions used temporary copies).

Audited tree: **ca592b0d4ce4ca82d86306eaeb48319194fa424a** (`origin/codex/sol-efficiency-scorecard` at review time). Parents: 739bd5b, then a787c51, then cleared base **9eabf69c0627bd605f72ff1bcd8d9c700d216583**. Isolated checkout. Author tests were executed by calling the nine functions in `tests/test_workflow_scorecard.py` with temporary directories (this sandbox has no `pytest`). All nine passed. Ruff was not re-run here.

## Verdict

**Not cleared** at ca592b0. There is **no efficiency improvement** to report. Missing cells stay unknown. A scoped note of what did hold is below; it is not a clearance of verified-completion counts or of any before/after ratio.

| SHA | Status on this audit |
|---|---|
| a787c51 | Rejected in 5862131131. Not re-cleared. |
| 739bd5b | Still rejected. Packaging hole 5862279986 was reproduced on that SHA before ca592b0 existed: omitted delivery/reviewer edges, an empty run-level `checks` list, and a failed task check still returned `verified`. |
| ca592b0 | That packaging shape now returns `unknown` **when the saved contract still says the edge is required**. Further holes below still return `verified`. |

## What holds on ca592b0 (not a clearance)

Reproduced on temporary records and on the checked-in Q9 trees:

- A verified attestation cannot flip `completed: false`. Forged nonempty `review.txt` plus the real Q9 fingerprint on a copy of the candidate tree stays `unknown` / `run did not record completion`. Forcing `completed: true` on that copy, which has no `workflow` object, stays `unknown` / `workflow record unavailable`.
- The packaging counterexample (contract still has `design_review: true` and `checks: true`, edges only `checks: true`, task check `passed: false`, run `checks: []`) is now `unknown` / `required task edge missing or failed`.
- Literal `reviewer: false`, a non-empty `unsatisfied` list, removed `workflow`, workflow `error`, parity `agree: false`, and a run check `passed: false` stay `unknown`.
- Final `result.budget.elapsed_seconds` is preferred. A final value older than `budget.json` becomes null. On the Q9 candidate, final is **128.162759641** and persisted is **128.153327641**.
- Historical `diagnostics.cached_input_tokens` is used when the top-level field is absent, is not added to input, and is nulled if it exceeds input or disagrees with the top-level field. Q9 candidate cache subset **188806** (5/5 invoked rows, none over input). Baseline **222225**.
- `usage.jsonl` `cost_usd` and `budget.api_cost_usd` are not imported into observed vendor spend. A synthetic `cost_usd: 999` stayed out.
- Missing `diagnostics.vendor_cost_usd` blocks `vendor_spend_complete` and the spend-per-completion ratio. Explicit `0` is complete spend, not null, on the per-run field.
- Failed **invocations** (`invoked` and `outcome != "ok"`) enter `failed_invoked_calls` and, when selected, the token and spend numerators. The author's two-run test still shows input 300 and spend 5 over one verified completion.
- Per-verified-completion ratios are null when `verified_completions` is 0 or any selected run is missing that total. Joint Q9 scoring: `verified_completions` **0**, every per-completion ratio **null**.

## Findings that still block clearance

All of these were executed against ca592b0 `score()` except the clock-boundary item, which is from the saved-record writer on this same tree.

### 1. Saved contract flags can drop mandatory edges

`tools/workflow_scorecard.py:55-72` treats a missing or false `contract.required` flag as "not required". The engine does not (`quadratus/outcome.py:280-297`): each of `checks`, `design_review`, `security_verification`, and `settlement` must be a bool, and `design_evidence` must be one of `harness|self|disabled|none`. Absence is missing, not optional.

Reproduced, otherwise clean record, nonempty external review, parity flags all true:

| Mutation | Scorecard |
|---|---|
| Delete `design_review` and set edges to `{checks: true}` only | **verified** |
| Set `design_review: false` and omit delivery/reviewer | **verified** |
| Delete `design_evidence` | **verified** |

Explicit `false` cannot be distinguished from a real dispatch on the saved blob alone. Omission can. Until a missing flag is `unknown`, a copied contract can erase the edge the previous review required. Do not treat parity flags as proof the contract is intact.

### 2. Contradictory receipts and stop facts still verify

`GateSuite.run` sets `passed` from every required receipt (`quadratus/integration.py:620`). The scorecard reads only `checks[-1].passed` (`workflow_scorecard.py:73-77` and `:89-93`).

Reproduced: run check `passed: true` with `receipts: [{id: "required", required: true, status: "failed"}]`, and the same shape on the task check, returns **verified**.

Active terminal facts are blockers in `typed_completed` / `completion_blockers` (`outcome.py:421-437`). Reproduced: `workflow.tasks[0].facts` or `workflow.run.facts` containing `{kind: "operator", terminal: true, recovered: false}` with parity flags left true returns **verified**. `closed_as: "stopped:CheckFailing"` also returns **verified**. `result.error: "FindingsOpen: still open"` together with `completed: true` returns **verified**. The scorecard never reads `result.error`, `facts`, or `closed_as`.

`docs/workflow-scorecard.md` already says an earlier failed check may be followed by a passing final receipt. That is not the same as a final wrapper whose own required receipt failed. Require the applicable required receipts to agree with `passed`, and reject an active terminal fact, a non-closed `closed_as`, and a non-empty `result.error`. Add a negative control for each.

### 3. Verified counts do not match continuation discharge

`completion_blockers` can discharge a predecessor's unsatisfied design edge when a later continuation satisfies that same edge under the same intended state (`outcome.py:401-430`). The scorecard returns on any non-empty `unsatisfied` before that rule (`workflow_scorecard.py:53-54` and `:71-72`).

Reproduced with two well-formed tasks (`a` edges failed, `b` continues `a` and satisfies `delivered` and `reviewer`, same `intended_state`): `completion_blockers([a, b])` is **[]**, and the scorecard returns **unknown** / `task workflow debt`.

That is fail-closed, so it does not inflate verified completions. It does bias a later per-completion denominator: a typed-complete multi-task run stays unverified, its cost still enters any selection that includes it, and it adds nothing to `verified_completions`. Reconcile discharge before using verified counts as the engine's completion denominator. Until then those ratios are not comparable to typed completion.

### 4. Attempt and unknown-usage disagreement is silent

Elapsed disagreement is handled (`workflow_scorecard.py:133-134`). `reserved_attempts` and `unknown_usage_attempts` are taken only from the chosen budget object (`:139-141`).

Reproduced: `result.budget` says attempts 1, unknown-usage 0, elapsed 30; `budget.json` says attempts 5, unknown-usage 3, elapsed 5; one invoked call. Result: **verified**, elapsed 30, coverage **true**, tokens counted. The extra reserved attempts and the unknown-usage mark disappear. Compare those fields the same way as elapsed, and make coverage unknown on disagreement.

When `result.budget` is JSON null, the scorecard adopts `budget.json` with no label (`:130`). Reproduced: final elapsed 30 is ignored and the report says **5**. `project_run` writes null only when no `RunBudget` exists, in which case `budget.json` should also be absent. A present earlier file must not be reported as the final snapshot. Say which clock was used.

### 5. Explicit zero spend is dropped from the partial total

`vendor_spend_known_partial_usd` is `float(spend) if spend else None` (`workflow_scorecard.py:195`). Decimal 0 is falsy. Reproduced: one invoked call with `vendor_cost_usd: 0` gives per-run observed spend **0.0**, complete **true**, summary observed **0.0**, and summary partial **null**. A known zero is not an unknown. Use an explicit null only when no selected run contributed a cost figure.

## Clock, tokens, cost (definitions, not a trial)

Budget clock on this tree (`quadratus/project_run.py:255` constructs `RunBudget`; `:395` snapshots it into `result.json` after trace copy and report writes):

- Starts after `project.contents()`, repo scan, and gate-plan write. The canary launcher's preflight (`docs/harness-canary/run_fixture.py`, before `run_project`) is outside this clock. Session readiness, the dependency watch, and the plan gate are inside `Session.run` and therefore inside the clock (`quadratus/session.py` around the readiness call). An interactive plan gate is operator time inside the number.
- Ends at the final snapshot, which includes trace copy and report IO. It is not model-only time and not an external supervisor wall clock. The saved `wall_boundary` string says "attempt timeout plus required external process supervisor".
- The scorecard sentence "starts after preflight and excludes operator time" overclaims both boundaries.
- `tools/acceptance/series.py` reads `budget.json`, not `result.budget`. On these Q9 trees the two clocks differ by about 9 ms (candidate delta **0.009432** s, baseline **0.008568** s). Do not diff the two tools as a regression on these records. A slow trace or a large report write would diverge by more, and only the final snapshot includes that tail.

Token rule that held: normalized invocation `input_tokens` already includes cached input (`input_boundary` on the Q9 budget: "do not add it again"). Scorecard input equals `result.budget.input_tokens` on both Q9 runs (candidate **222716**, baseline **255926**). Output matches too (candidate **4988**, baseline **5333**), and input+output equals `reported_tokens` (**227704** and **261259**). Cache stays a subset.

`diagnostics.auxiliary_tokens` is not a scorecard field. The Claude extractor documents it as the non-seat portion already folded into that call's input and output (`quadratus/cli_providers.py` usage notes). This audit did not reopen vendor envelopes. Sum of recorded auxiliary tokens: candidate **4684**, baseline **3653**. They are not an extra addend on top of the matched budget totals above.

Cost rule that held, and the comparison it forbids:

- Observed spend is only `diagnostics.vendor_cost_usd`.
- Q9 candidate: **0.482103** on **2 of 5** invoked calls, `vendor_spend_complete` false, `missing_vendor_cost_calls` **3**. Baseline: **0.759107**, also **3** missing. The calls without a vendor figure are the ones whose diagnostics carry cache and `stop_reason` but no `vendor_cost_usd`.
- `usage.jsonl` list-price sums, excluded by the scorecard and not a vendor bill: candidate **1.144398**, baseline **1.167198**.
- Candidate `budget.api_cost_usd` is **0.0** with `cost_boundary` "CLI excluded". That zero is not savings.
- Joint scorecard `vendor_spend_known_partial_usd` is **1.24121** (0.482103+0.759107). That addition is not a combined cost. Both runs are incomplete on the same three-call gap.

`failed_invoked_calls` is **0** on both Q9 runs. Every invoked row is `outcome: ok`. The runs themselves are not complete: `completed: false`, the only check receipt `passed: false`, and `workflow` / `requirements` / `findings` are absent. An incomplete run is not a failed invocation. Incomplete runs enter totals only when the caller selects them. The scorecard does not discover sibling attempts. Two non-invoked `selected` rows on each Q9 ledger are outside the token and spend sums; reserved attempts (5) match invoked calls (5), so coverage is complete for those tokens. That does not make the run a completion.

## Before/after

No matched trial exists in the saved records this scorecard can score.

The checked-in Q9 pair is one baseline directory and one candidate directory. Both are incomplete single runs, with no workflow record and no independent attestation. Descriptive gaps (candidate minus baseline), **not** an improvement:

| Measure | Baseline | Candidate | Comparable? |
|---|---|---|---|
| Verified completions | 0 | 0 | No denominator |
| Controller completed | false | false | Same outcome, n=1 |
| Budget elapsed (final, seconds) | 148.190177484 | 128.162759641 | Same clock definition only. Not end-to-end, not operator-excluded in the strict sense above. n=1. |
| Input / output / cache subset | 255926 / 5333 / 222225 | 222716 / 4988 / 188806 | Same normalization on these two files. Not per completion. n=1. |
| Vendor spend | 0.759107 known, 3 calls unknown | 0.482103 known, 3 calls unknown | **Not comparable.** |
| API list-price `usage.jsonl` | 1.167198 | 1.144398 | Different question from vendor spend. n=1. Not an efficiency result. |
| `failed_invoked_calls` | 0 | 0 | Does not describe the failed checks. |

`docs/collaboration/2026-09-blind-acceptance/evidence/scored-attempt-*` is also not a before/after for this scorecard. Ten of eleven have `result.json` and `completed: false`. Attempt 10 has no `result.json`. None has a `workflow` object. No attestation was applied. They were not scored into a ratio.

Selecting only a cheaper directory would change every per-completion figure. The method string correctly refuses a before/after claim. The partial-spend field and the Q9 dollar gap are still easy to misuse. They are unknown as efficiency.

## Unknown, left unknown

- Subscription marginal spend, operator time outside the budget clock, and model-judgment quality.
- Vendor cost on the three Q9 calls that did not record `vendor_cost_usd`.
- Whether a future multi-task continuation would be typed-complete and still unscored (finding 3). This audit did not invent a population rate.
- Any efficiency delta versus a pre-scorecard process. There is no paired scorecard output from a prior commit on the same runs, because those runs have no verified completion under either a787c51 or ca592b0.

## Asked corrections (scorecard lane, not done here)

1. Unknown when a required-flag is missing or not a bool, or `design_evidence` is not in the engine's enum.
2. Unknown when a required receipt disagrees with `passed`, when an active terminal fact is present, when `closed_as` is not a successful close, or when `result.error` is non-empty.
3. Apply the engine's continuation/audit discharge, or keep those runs unknown and out of any published denominator. Do not count them as verified by ignoring `unsatisfied`.
4. Treat attempt-count and unknown-usage disagreement like elapsed disagreement. Do not present a null `result.budget` as the final clock.
5. Keep an explicit zero in `vendor_spend_known_partial_usd`.
6. Tighten the clock sentence to the boundaries above.

No adoption, no merge, and no new live trial from this lane.
