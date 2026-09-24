# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.6s | 2,136 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 31.8s | 59,600 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 41.7s | 90,786 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 8.8s | 8,761 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 31.7s | 64,326 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 47.7s | 145,938 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 86.3s | 165,351 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 18.4s | 21,314 tokens

## Totals
- Quadratus-dispatched: 558,212 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
