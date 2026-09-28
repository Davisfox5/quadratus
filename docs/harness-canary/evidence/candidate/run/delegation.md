# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 23.5s | 57,023 tokens
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 21.6s | 46,296 tokens
- t1/gate-fix | [seat] | openai:gpt-5.6-sol | invoked | ok | 22.4s | 43,534 tokens
- t1/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t1/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 40.4s | 65,789 tokens
- t1/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 19.5s | 15,062 tokens

## Totals
- Quadratus-dispatched: 227,704 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
