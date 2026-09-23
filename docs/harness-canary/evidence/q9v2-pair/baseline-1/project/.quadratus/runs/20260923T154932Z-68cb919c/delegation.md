# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 3.7s | 2,137 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 26.7s | 66,210 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 144.6s | 226,580 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 10.7s | 8,120 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 32.9s | 67,813 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | RunBudgetExceeded | 107.5s | 185,658 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 556,518 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
