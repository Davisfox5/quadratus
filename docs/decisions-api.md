# OpenAI Decisions API: what it is, what we know, what waits

TL;DR: real, announced at DevDay on 2026-09-29, exactly the shape of three
routing decisions this engine makes. As of 2026-09-30 there is no published
endpoint, schema, model id or pricing, and the openai package (3.22.1) has no
decisions resource. The inventory below and `quadratus/decisions.py` are the
prep; the transport is written the day the contract is public. Any call to it
is a billed API call, opt-in, metered, and never a silent replacement for the
ladder.

## What OpenAI said

A specialized GPT-6 Luna picks one of a developer's predefined answers from
text or image context in about 150 ms (against about 1.6 s for a normal Luna
call). Stated uses: classify content, route requests, choose an agent's next
action. Limited preview for selected API customers; broad release "in the
coming days". No documentation, endpoint, schema or pricing was published with
the announcement, and none had appeared a day later. Luna's ordinary API price
is $0.10 per million input tokens and $0.50 per million output; the Decisions
endpoint's price is not stated.

Whether a ChatGPT subscription or the Codex CLI reaches it is unknown. It was
announced as an API endpoint, so the working assumption is API-key billing.

## Decisions in this engine with a finite answer set

| id | answers | today's rule | delegable |
|---|---|---|---|
| task.difficulty | rote, simple, standard, complex | KIND line; unlabelled tasks default with confidence "defaulted" | yes |
| task.kind | the `task_kinds.KINDS` set | KIND line; unknown kinds degrade to general | yes |
| worker.escalate | base, escalate | the lead's "demanding" flag | yes |
| task.tier | direct, normal | TIER label plus deterministic admission on facts the harness holds | no |
| reply.control | task, DONE, ASK, FETCH, CONSULT, WORKER, delivery | bounded preface parser the prompt states in words | no |
| security.route | general, security | WorkClass on the task | no |

The three delegable rows are where a fast, cheap, constrained classifier
earns its place: an unlabelled or mislabelled task today rides a default rung
silently, and a wrong difficulty is how a bounded worker gets handed work it
cannot run (task_kinds, 2026-09-14). The three non-delegable rows are parsers
and admissions whose rules the prompts state and the tests pin; a
probabilistic reading would break prompt parity or spend a refusal.

## Integration rules, decided now

- The deterministic rule stays the default and the fallback. An external
  decider is opt-in per run (`--decider openai` or similar), recorded on the
  contract like the tier is, and its verdict is one input the rule may
  override, never the other way round.
- Every call is metered as a billed API call with measured tokens where
  reported. Metering stays observational.
- A decider that cannot be reached refuses loudly (`DecisionsUnavailable`);
  the run continues on the rule. No silent fallback either direction.
- The verdict and its source are recorded on the task (`Verdict.source`), so
  a routing outcome can be traced to the rule or to the decider.
- Verification of the contract is a probe, not a table: the first call the
  transport makes records what the endpoint accepted and returned.

## What unblocks the transport

1. The published endpoint, request and response schema, and model id.
2. Pricing, so the meter's seed sheet has a row.
3. An openai SDK release with the resource, or a documented raw HTTP shape.
4. Preview access on the account, if broad release has not landed.
