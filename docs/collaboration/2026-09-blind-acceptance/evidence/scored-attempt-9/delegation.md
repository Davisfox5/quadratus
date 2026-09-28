# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.5s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 35.8s | 73,642 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 228.5s | 241,700 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 8.1s | 7,337 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 27.6s | 57,953 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/collaborator | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | RunBudgetExceeded | 99.9s | 152,163 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 532,795 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.

## Selected but never invoked
These models were chosen and never reached. This is not coverage:
- claude:opus
