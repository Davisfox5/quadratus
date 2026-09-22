# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 22.7s | 67,347 tokens
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 43.9s | 108,955 tokens
- t1/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t1/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 39.9s | 104,431 tokens
- t1/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 21.5s | 19,554 tokens

## Totals
- Quadratus-dispatched: 300,287 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
