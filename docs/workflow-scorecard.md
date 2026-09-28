# Offline workflow scorecard

`python3 tools/workflow_scorecard.py --attestations review.json /saved/run1 /saved/run2` prints JSON. It reads saved `result.json`, `budget.json`, and `invocations.jsonl`; it does not execute the project or call a model. Run directory basenames must be unique in one scorecard.

An optional external `review.json` is keyed by run directory basename. A verified entry looks like:

```json
{"run1": {"status": "verified", "reviewer": "reviewer name", "evidence": "independent-review.txt", "source_fingerprint": "exact result.json source_fingerprint"}}
```

The reviewer must create the evidence file in the run directory (or supply an absolute path). It should explain the original intended state, required checks, source-bound capture, actual review, settled requirements, and unresolved findings. The tool confirms that the evidence file exists, the source fingerprint matches, and the saved workflow, requirements, findings and checks do not contradict completion. Missing workflow evidence leaves the verdict **unknown**, even if `result.json` says `completed: true`. This still cannot independently judge the review's quality; the attestation remains a human judgment. A verified attestation cannot promote a run whose own completed claim is false or absent.

The summary includes all selected runs and failed invocations. It reports the final budget interval, reserved attempts, invoked calls, failed calls, input and output tokens, and cached input as a subset of input. The budget clock starts after preflight and excludes operator time. It prefers the final `result.json` budget snapshot; a final elapsed value older than the separately persisted budget is unknown. Totals and their per-verified-completion ratios are null when unavailable or when no completion is verified. If reserved attempts and invocation events disagree, full token and spend totals stay unknown. An explicit unknown-usage count also makes token totals unknown. Historical cache counts in `diagnostics` are accepted when consistent with top-level values and no larger than input. `usage.jsonl` dollar values are API list-price counterfactuals and are excluded from observed spend. Only invocation `diagnostics.vendor_cost_usd` enters observed vendor spend; a missing cost on any invoked call prevents a complete spend or spend-per-verified-completion figure. Subscription marginal spend, operator time, token usage outside saved invocations, and model judgment quality are not measured. The result is an observational scorecard, not a before/after efficiency claim.
