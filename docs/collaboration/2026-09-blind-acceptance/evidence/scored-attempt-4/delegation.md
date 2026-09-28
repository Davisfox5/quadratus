# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.5s | usage unknown | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | selected, never invoked | RunBudgetExceeded | 0.0s | usage unknown | Run stopped: unknown_usage

## Totals
- Quadratus-dispatched: 0 tokens
- 1 invocation(s) consumed a window and reported no usage. These remain **unknown**, not zero:
    - run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.5s | usage unknown | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.

## Selected but never invoked
These models were chosen and never reached. This is not coverage:
- openai:gpt-6-astra
