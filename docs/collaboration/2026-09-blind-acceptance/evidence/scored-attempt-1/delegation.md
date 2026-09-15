# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 36.9s | 65,565 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | ok | 210.6s | 290,408 tokens
- t1/closeout | [seat] | grok:default | invoked | RunBudgetExceeded | 189.0s | 258,413 tokens | failed after return | provider: ok | Run stopped: reported_token_threshold

## Totals
- Quadratus-dispatched: 614,386 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.
