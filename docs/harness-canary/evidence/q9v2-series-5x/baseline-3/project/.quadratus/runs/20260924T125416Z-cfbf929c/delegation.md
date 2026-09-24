# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.5s | 2,137 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 32.3s | 59,472 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 99.3s | 341,361 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 10.8s | 8,727 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 36.6s | 68,536 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 65.9s | 182,057 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 47.1s | 137,333 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 16.2s | 19,685 tokens

## Totals
- Quadratus-dispatched: 819,308 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
