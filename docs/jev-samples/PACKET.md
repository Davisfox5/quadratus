# Jev sample packet (T-grok-samples)

Author: Grok. Reviewer: Codex. Not frozen. No Jev call.

Machine copy: `tests/fixtures/jev-samples/samples.json`. Schema is `tools/jev_experiment/packet.py` `validate` on #49 at the projects-map revision. `held_out` uses an underscore. Answer sets are flat `acceptable_kinds` and `acceptable_difficulties`. `acceptable_pairs` is omitted where the cross product is the set.

`projects.engine` is `f96e0c88e7a18156a9582e3aa19de4c9d70ff89f`. `projects.gametape` is `1cd9264edb4429f00cde43a04a1944d0dca37f11`. No absolute path.

Unlabelled `route()` on that engine, for every row, is kind general, difficulty simple, lead `grok:default`. Applying an accepted pair then does this:

| id | split | accepted pair | lead |
|---|---|---|---|
| dev-favicon | development | frontend + simple | grok:default |
| dev-node-status | development | docs + rote | grok:default (execute skips the worker) |
| dev-trial-sentence | development | docs + rote | grok:worker |
| hold-heading | held-out | frontend + rote | grok:default (execute and a capture stay) |
| hold-import-note | held-out | docs + simple | grok:default |
| hold-dotenv-guard | held-out | security + simple | openai:gpt-5.6-sol |
| hold-one-assertion | held-out | test + rote | openai:gpt-5.6-sol |
| hold-budget-threads | held-out | concurrency + complex | claude:opus |
| hold-import-readonly | held-out | review + simple | openai:gpt-5.6-sol |
| hold-import-readonly | held-out | comprehend + simple | grok:default |
| hold-list-timing | held-out | perf + simple | grok:default |

dev-favicon is not a scoring item. The favicon prose has no KIND, NEEDS, SCOPE, TIER, or LEAD line; the tool injects those. Kind is frontend only under the kind-v1 tie-break.

dev-node-status replaces the missing mutation script with `tests/ui/import_preview.test.js` on the GameTape baseline. hold-budget-threads replaces the in-memory Ledger story with `RunBudget`. hold-dotenv-guard requires 403 and `error=denied`, because 404 already makes "not 200" green. hold-list-timing records three durations and has no millisecond cutoff. hold-import-readonly is `edits: none` with a named path and a positive line ceiling.
