# Decision models: Jev wired, OpenAI Decisions API waiting

TL;DR: two decision-only models fit the same three routing decisions here.
TypeSafe AI's Jev is documented and wired (`--decider jev`,
`quadratus[decisions]`, `TYPESAFE_API_KEY`); OpenAI's Decisions API was
announced on 2026-09-29 with no published contract, so its transport waits.
Either is opt-in, billed and metered, consulted only where the orchestrator
stated no usable label, and never a silent replacement for the ladder.

## Jev (TypeSafe AI), wired 2026-09-30

Contract read from the vendor's own SDK (`typesafe-sdk` 0.7.2 on PyPI, MIT,
"Production/Stable"): `POST https://api.typesafe.ai/v1/systemone` with a
`state` (text or JSON) and named `questions` of type `noul` (yes/no
probability), `choice` (a label from `criteria`, with `confidence` and
per-label `probabilities`) or `score` (an ordered rubric). The response
carries `model` and `usage.input_tokens`/`output_tokens`; the schema says
output tokens are free. Default model alias `jev-latest`; `client.models.list()`
names what the account can call. Text only, no image input. Price from the
OpenRouter listing on 2026-09-30: $0.042 per million input tokens, output
free; it is a seed on the usage sheet, stale by assumption.

Access without a TypeSafe account: Vercel's AI Gateway fronts Jev behind a
TypeSafe-compatible API (base URL `https://ai-gateway.vercel.sh/typesafe`,
model `typesafe-ai/jev`, the gateway key as the bearer, no markup). A
`vck_` key is recognised as a gateway key and the gateway host and model
become its defaults; `TYPESAFE_BASE_URL` and `TYPESAFE_DEFAULT_MODEL` still
override. The host is recorded on every call. The key lives in the
project's gitignored `.env` as `TYPESAFE_API_KEY`.

There is no free path, as of a live probe on 2026-09-30 from Davis's Mac
with a gateway key: the gateway's model list answers (`jev`, alongside
`liquid/d1`), and a decision call on either `typesafe-ai/jev` or `jev`
returns 403 "Free tier users do not have access to this model. Upgrade to
paid credits"; `typesafe-ai/jev-1.13` is 404. Search results claiming the
Vercel free tier covers Jev were wrong. TypeSafe's own console stopped
giving new accounts free credit on 2026-09-27. So the choice is a paid
Vercel top-up on the gateway key (any amount; at $0.042 per million input
tokens a $5 top-up is about 120 million tokens, and one routing decision
is a few hundred) or a direct TypeSafe key, which the decider sends to
`api.typesafe.ai` with model `jev-latest`.

Direct key, live on 2026-09-30 (Davis's console, $5 credit): the key is
spelled `apikey_<id>_<secret>` (not the `ts_` the search results claimed),
the account's models are `jev-latest` and `jev-preview`, and `jev-latest`
resolved to `jev-1.13.0`. Three difficulty decisions on task texts of one
to two sentences cost 323 to 324 input tokens each and answered rote
(0.58), simple (0.68) and complex (0.83) where those were the expected
readings, each with the full probability map. A decision is about a
hundredth of a cent.

What the engine does with it (`JevDecider`, `Session._route_with_decider`):

- Only when the orchestrator's reply carried no usable KIND line (a
  "defaulted" or "degraded" route). A stated label is never overridden.
  With `--decider-labels all` (`SessionConfig.decider_labels`, Stage B,
  2026-10-02) the orchestrator is asked not to write a KIND line at all,
  so the decider routes every planned task; a label it states anyway is
  still kept. The default, `unstated`, is the behaviour above.
- One `choice` question per decision, `task.kind` then `task.difficulty`,
  with the task text as state; the answer must be one of the ladder's own
  labels or it is refused. Every answer is sent with its definition and the
  question carries a tie-break for mixed work (below); the definitions
  version rides on each decision record as `labels`.
- Every verdict or refusal is recorded on the run (`result.json.decisions`:
  task, decision, default, answer, source, confidence, probabilities or
  error), in the task's own record as a routing note, and the metadata
  confidence becomes `"decided"`.
- A refusal (no package, no key, transport error, out-of-set answer) keeps
  the rule's default and says why; nothing raises into the run.
- Each call is metered under `jev:<model>` with the SDK's token counts.
- SDK transport retries are off (`RetryPolicy(max_retries=0)`): the SDK's
  default retried a failed request twice below the one budget reservation,
  so a 503 with `max_calls=1` was three HTTP attempts for one ticket
  (Codex, 2026-10-01). Every HTTP attempt is now one reservation; a
  transport-level 503 or timeout is one attempt and one latched
  unknown-usage stop. A retry, if ever wanted, is a second budgeted call.

Not wired yet: `worker.escalate` (the lead's demanding flag), and any use of
`noul` or `score` questions. Those wait for a live run that shows the two
kind/difficulty decisions earning their keep.

## Label definitions (the words the decider is judged against)

The first live run (`20261001T022457Z-6602cd28`, 2026-10-01) sent bare
label names with no criteria text and classified a favicon task that added
a route, an SVG, a template link and a test as `backend`. The question now
carries one definition per answer and a stated tie-break, from a single
source (`quadratus/decision_labels.py`) that this section is rendered
from; a test holds the two equal. Difficulty is defined by reasoning
burden, context and dependencies, not by line count. No new categories and
no confidence cutoff: this changes what the decider is asked, not what the
engine does with the answer.

<!-- rendered from quadratus.decision_labels.render_markdown(); do not edit by hand -->
Definitions version `kind-v1/difficulty-v1`.

**task.kind** -- Pick the label whose definition the acceptance checks exercise most. Mixed work: when a page a user sees changes and that rendering is part of the acceptance, choose frontend over backend; when the defining risk is a race, data loss or hostile input, choose concurrency, data or security over the layer it lives in; when several kinds share the work equally and none of those apply, choose general.

- `scope`: Turning the operator's request into a stated goal with constraints, before any task exists. Example: write the goal and non-goals for 'add import preview'.
- `decompose`: Breaking a stated goal into bounded tasks with acceptance checks. Example: split the import-preview goal into route, parser and page tasks.
- `architect`: Open-ended structural judgement before code exists: which pieces, which boundaries, which trade-off. Example: decide whether previews are computed on upload or on request.
- `backend`: Server-side logic, routes, services and APIs where no rendered page is part of the acceptance. Example: add a JSON endpoint that lists projects.
- `frontend`: Anything a user sees in a browser: templates, markup, styles, static assets, page behaviour; the acceptance includes how a page renders. Example: add a favicon and its link tag.
- `mobile`: Native or mobile-platform code (Android, iOS) and its build or device constraints. Example: fix a Kotlin screen's rotation state.
- `bulk`: High-volume mechanical edits where the same change repeats across many files and judgement is low. Example: rename a function across forty call sites.
- `glue`: Wiring existing pieces together with little new logic: configuration, adapters, plumbing. Example: register an existing blueprint and pass its settings through.
- `review`: Finding defects nobody has reported yet in code that already exists. Example: audit the upload handler for missing validation.
- `debug`: Root-causing a defect that has already announced itself (a failing test, a traceback, a bug report). Example: find why the import page returns 500 on empty files.
- `concurrency`: Work whose failure mode is a race, a deadlock or a lost update: locks, workers, shared state. Example: make the job queue safe for two workers.
- `security`: Authentication, authorisation, secrets, injection, or hardening against hostile input. Example: stop path traversal in the file download route.
- `refactor`: Restructuring code without changing behaviour, with existing tests as the net. Example: extract the parser from the route into its own module.
- `test`: Adding or repairing tests as the deliverable itself, not as part of another change. Example: cover the parser's edge cases with unit tests.
- `comprehend`: Reading the codebase to answer a question about it, producing an explanation, not code. Example: explain how uploads reach storage.
- `iac`: Infrastructure as code: deployment, containers, CI pipelines, environment definitions. Example: add a CI job that runs the browser tests.
- `docs`: Documentation as the deliverable: README, guides, docstrings, changelogs. Example: document the import-preview endpoint.
- `perf`: Making something measurably faster or lighter, with a measurement as the acceptance. Example: cut the project-list query from 40 round trips to one.
- `data`: Schema, query and migration work on stored data. Example: add a column with a migration and backfill.
- `general`: Work that fits no other label, or spans several with none dominant. Example: a small change touching a route, a template and a test equally.

**task.difficulty** -- Judge the reasoning burden, the context a model must hold and what the task depends on. Line count is not difficulty: a long rote edit is rote, a short change to shared state is complex. Most well-sized tasks are simple; reserve complex for genuinely hard reasoning.

- `rote`: Mechanical with a known recipe: one obvious way to do it, little context beyond the named files, nothing else depends on the choice. Example: add a link tag to a template.
- `simple`: One clear idea with a few steps; a model can hold the whole task and its context at once and verify it locally. Example: add a route plus its test.
- `standard`: Several interacting steps, or dependencies on code outside the named files, where a wrong choice early costs rework. Example: a parser that must agree with an existing validator.
- `complex`: Genuinely hard reasoning: many interacting constraints, unclear specification, or failure modes that are hard to observe (races, data loss). Example: make a migration safe under live traffic.
<!-- end rendered -->

## OpenAI Decisions API, waiting

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
