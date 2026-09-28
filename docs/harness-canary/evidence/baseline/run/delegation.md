# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 18.5s | 56,810 tokens
- t1/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 28.7s | 42,131 tokens
- t1/gate-fix | [seat] | openai:gpt-5.6-sol | invoked | ok | 36.6s | 86,435 tokens
- t1/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t1/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 44.8s | 60,813 tokens
- t1/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 18.7s | 15,070 tokens

## Totals
- Quadratus-dispatched: 261,259 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
