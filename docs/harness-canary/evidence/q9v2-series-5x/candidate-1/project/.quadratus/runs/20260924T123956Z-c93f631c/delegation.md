# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.3s | 2,135 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 29.6s | 64,578 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 42.7s | 90,015 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 10.8s | 8,637 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 32.8s | 68,298 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 44.2s | 119,208 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 54.4s | 122,688 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 19.7s | 21,491 tokens

## Totals
- Quadratus-dispatched: 497,050 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
