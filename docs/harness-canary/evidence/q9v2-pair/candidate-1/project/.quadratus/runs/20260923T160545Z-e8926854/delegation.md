# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.5s | 2,136 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 30.4s | 63,702 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 149.3s | 85,721 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 17.4s | 8,474 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 35.4s | 68,962 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 66.9s | 100,459 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 46.5s | 285,886 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 21.2s | 19,396 tokens

## Totals
- Quadratus-dispatched: 634,736 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
