# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 41.1s | 105,531 tokens
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t1/collaborator | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 28.9s | 105,424 tokens
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | RunBudgetExceeded | 0.0s | usage unknown | Run stopped: reported_token_threshold

## worker
- t1/worker:code-1 | [worker] | claude:haiku -> claude-haiku-4-5-20251001 | invoked | RunBudgetExceeded | 324.1s | 312,518 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 523,473 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.

## Selected but never invoked
These models were chosen and never reached. This is not coverage:
- claude:opus
