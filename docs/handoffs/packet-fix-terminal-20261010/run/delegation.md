# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 87.1s | 176,723 tokens
- plan/requirements-review | [seat] | openai:gpt-5.6-sol | invoked | ok | 18.4s | 42,443 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/collaborator | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 96.7s | 115,903 tokens
- t1/collaborator | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 152.6s | 312,888 tokens
- t1/revision | [seat] | grok:default | invoked | ok | 173.5s | 171,073 tokens
- t1/design-review | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 37.4s | 177,456 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 24.2s | 13,759 tokens
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 53.4s | 168,101 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 66.8s | 183,025 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 13.9s | 22,705 tokens
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 58.0s | 144,162 tokens
- t3/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t3/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 51.4s | 246,849 tokens

## Totals
- Quadratus-dispatched: 1,775,087 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
