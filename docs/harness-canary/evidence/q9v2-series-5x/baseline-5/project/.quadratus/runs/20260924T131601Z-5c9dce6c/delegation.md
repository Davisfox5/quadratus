# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 1.7s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 34.5s | 59,539 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 132.5s | 588,697 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 9.6s | 8,623 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 36.4s | 64,632 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 79.5s | 216,070 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | RunBudgetExceeded | 47.9s | 183,797 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 1,121,358 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
