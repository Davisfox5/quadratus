# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 5.0s | 2,137 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 29.1s | 59,501 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 135.6s | 402,540 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 12.0s | 8,934 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 33.8s | 68,980 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 72.1s | 125,421 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 42.5s | 137,253 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 14.3s | 20,716 tokens

## Totals
- Quadratus-dispatched: 825,482 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
