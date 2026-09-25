# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.4s | 2,137 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 31.7s | 63,704 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 93.0s | 267,116 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 9.0s | 8,621 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 32.7s | 63,956 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 125.7s | 479,324 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | RunBudgetExceeded | 59.4s | 137,914 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 1,022,772 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
