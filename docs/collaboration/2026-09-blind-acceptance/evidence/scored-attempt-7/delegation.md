# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.5s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 39.7s | 82,051 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ProviderError | 0.4s | usage unknown | grok did not complete the turn (stopReason None).; Error: Not signed in. To authenticate without a browser, run:
  grok 
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | RunBudgetExceeded | 0.0s | usage unknown | Run stopped: unknown_usage

## Totals
- Quadratus-dispatched: 82,051 tokens
- 1 invocation(s) consumed a window and reported no usage. These remain **unknown**, not zero:
    - t1/lead | [seat] | grok:default | invoked | ProviderError | 0.4s | usage unknown | grok did not complete the turn (stopReason None).; Error: Not signed in. To authenticate without a browser, run:
  grok 
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.

## Selected but never invoked
These models were chosen and never reached. This is not coverage:
- openai:gpt-5.6-sol
