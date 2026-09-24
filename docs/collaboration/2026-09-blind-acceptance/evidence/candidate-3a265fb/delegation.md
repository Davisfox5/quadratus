# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable | invoked | ProviderError | 2.1s | 0 tokens | claude reported an error: You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 38.3s | 52,620 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 366.8s | 365,138 tokens
- t1/closeout | [seat] | grok:default | invoked | ok | 9.1s | 8,417 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 29.0s | 59,864 tokens
- t2/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t2/collaborator | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t2/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 53.4s | 101,015 tokens
- t2/collaborator | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 169.2s | 188,721 tokens
- t2/revision | [seat] | openai:gpt-5.6-sol | invoked | ok | 47.7s | 82,216 tokens
- t2/recheck | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 6.8s | 38,414 tokens
- t2/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 12.3s | 16,330 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 23.5s | 41,283 tokens
- t3/lead | [seat] | openai:gpt-5.6-sol | selected, never invoked | selected | duration unknown | usage unknown
- t3/collaborator | [seat] | claude:opus | selected, never invoked | selected | duration unknown | usage unknown
- t3/lead | [seat] | openai:gpt-5.6-sol | invoked | ok | 64.9s | 138,626 tokens
- t3/collaborator | [seat] | claude:opus -> claude-opus-5 | invoked | ok | 151.0s | 100,929 tokens
- t3/revision | [seat] | openai:gpt-5.6-sol | invoked | ok | 55.5s | 122,149 tokens
- t3/closeout | [seat] | openai:gpt-5.6-sol | invoked | ok | 13.2s | 16,408 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 24.1s | 44,457 tokens
- t4/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t4/lead | [seat] | grok:default | invoked | ok | 338.9s | 629,156 tokens
- t4/closeout | [seat] | grok:default | invoked | ok | 9.7s | 8,356 tokens
- run/orchestrator | [seat] | openai:gpt-6-astra | invoked | ok | 30.4s | 70,983 tokens
- t5/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t5/lead | [seat] | grok:default | invoked | RunBudgetExceeded | 551.6s | 1,905,496 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 3,990,578 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
