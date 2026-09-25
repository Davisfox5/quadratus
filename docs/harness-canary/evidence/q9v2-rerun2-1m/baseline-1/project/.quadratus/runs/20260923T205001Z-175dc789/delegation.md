# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.8s | 2,135 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 28.1s | 59,361 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 104.0s | 303,467 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 10.8s | 8,620 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 31.9s | 68,506 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 139.5s | 163,619 tokens
- t2/verifier | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/verifier | [seat] | claude:opus -> claude-opus-5 | invoked | RunBudgetExceeded | 251.8s | 1,096,136 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 1,701,844 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
