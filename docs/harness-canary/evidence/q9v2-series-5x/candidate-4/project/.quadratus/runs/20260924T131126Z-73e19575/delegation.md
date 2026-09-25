# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.2s | 2,136 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 29.5s | 59,570 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 49.6s | 93,802 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 9.8s | 8,787 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 33.1s | 64,095 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 58.9s | 119,337 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 54.0s | 161,047 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 15.6s | 20,920 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 18.7s | 65,164 tokens

## Totals
- Quadratus-dispatched: 594,858 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
