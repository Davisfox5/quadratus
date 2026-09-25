# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.6s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 31.3s | 59,618 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 80.3s | 232,684 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 11.0s | 8,651 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 31.6s | 46,039 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 149.4s | 404,709 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 57.6s | 183,563 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 18.3s | 19,647 tokens

## Totals
- Quadratus-dispatched: 954,911 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
