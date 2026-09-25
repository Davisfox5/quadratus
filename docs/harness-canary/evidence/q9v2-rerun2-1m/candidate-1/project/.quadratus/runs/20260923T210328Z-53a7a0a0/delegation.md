# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.7s | 2,133 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 27.5s | 59,360 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 63.1s | 94,902 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 20.2s | 9,561 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 30.8s | 46,238 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 66.1s | 126,395 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 46.1s | 189,988 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 19.8s | 20,718 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 17.1s | 65,116 tokens

## Totals
- Quadratus-dispatched: 614,411 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
