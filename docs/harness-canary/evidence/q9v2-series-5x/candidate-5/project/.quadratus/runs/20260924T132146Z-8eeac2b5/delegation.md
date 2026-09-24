# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.3s | 2,136 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 29.5s | 63,966 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 48.9s | 93,729 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 10.5s | 8,775 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 33.3s | 64,292 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 57.1s | 147,233 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 55.1s | 160,973 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 16.0s | 20,740 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 19.4s | 66,276 tokens

## Totals
- Quadratus-dispatched: 628,120 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
