# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.7s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 32.0s | 63,225 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 256.6s | 201,764 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 7.6s | 6,900 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 33.9s | 56,818 tokens
- t2/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | grok:default | invoked | ok | 134.6s | 157,172 tokens
- t2/closeout | [seat] | grok:default | invoked | ok | 8.1s | 6,968 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | RunBudgetExceeded | 22.7s | 40,213 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 533,060 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
