# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.6s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 32.2s | 77,192 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | RunBudgetExceeded | 263.4s | 450,602 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 527,794 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
