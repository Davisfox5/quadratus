# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.8s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 24.8s | 59,497 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 37.3s | 90,155 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 9.0s | 8,614 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 39.6s | 63,876 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 122.6s | 262,786 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 57.9s | 162,524 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 17.7s | 20,970 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 19.2s | 64,788 tokens

## Totals
- Quadratus-dispatched: 733,210 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
