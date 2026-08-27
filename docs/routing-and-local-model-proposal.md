# Proposal: task routing and a local participant

**Status: proposed, nothing changed.** Written after reading the repo against
techniques 5 and 8 in the DF AI techniques playbook.

## What the playbook assumed, and what is actually here

The brief said this repo runs "a fixed Claude-then-GPT cascade with no tier
logic" and needs a heuristic router built. Half of that is right, and the half
that is wrong changes the recommendation.

There are two generations in `multi_llm/`:

**The collaboration pipeline** (`orchestrator.py`), which is what the CLI and
GUI actually run, is the fixed cascade. `run()` does:

```python
coordinator = self._coordinator()   # settings.synthesizer, else self.available[0]
lead = self.available[0]
reviewers = self.available[1:]
```

`self.available` is ordered by `PROVIDER_ORDER`, default `claude,openai,gemini`.
So Claude leads and OpenAI reviews because of a string in `config.py`, not
because of anything about the task. Every task runs all four phases with every
available provider, whether it is a rename or a schema migration.

**The session engine** (`session.py`, `registry.py`, `routing.py`,
`task_kinds.py`) already has the router. It has a `Capability` taxonomy that is
explicitly shapes-of-work rather than quality rankings, a `Complexity` ladder
that maps difficulty to both who leads and how many collaborators are drawn
(`ROTE`/`SIMPLE` are lead-alone, `STANDARD` adds one, `COMPLEX` draws the whole
brain trust), `MODE_ROSTERS`, an `ORCHESTRATOR_CHAIN`, and a documented security
work-routing path. It is fully unit-tested and, per the README, not yet wired to
the CLI entry points.

**So the recommendation is not "build a heuristic router". It is "wire the one
that exists".** Building a second router next to this one would be the actual
mistake.

## Recommendation 1: wire the session engine's routing into the pipeline

The smallest version that gets most of the value, in order:

1. **Classify the task before running phases.** `session.py` already parses a
   complexity out of task text (the `Complexity._COLLABORATORS` lookup around
   line 1010). Call that from `Orchestrator.run()` and let the result decide
   whether phases 1 and 2 run at all. A `ROTE` or `SIMPLE` task should take the
   existing single-provider fast path, which is already implemented and today
   only fires when exactly one provider is configured.

2. **Pick the lead from capability, not list order.** `registry.models_for()`
   already answers "who is good at this shape of work". Replace
   `lead = self.available[0]` with a capability lookup against the classified
   task kind. Keep `PROVIDER_ORDER` as the tiebreak so behavior stays
   predictable when the registry expresses no preference.

3. **Scale reviewers by complexity.** `Complexity.collaborator_count()` already
   returns the right number. Today phase 3 loops every reviewer for
   `settings.rounds` rounds regardless.

Do these three and the cascade is gone without a new abstraction.

## Recommendation 2: the Fable question, which needs your call

`routing.py` seats Fable 5 as the orchestrator and `ORCHESTRATOR_CHAIN` leads
with it. Your standing rule is that Fable is a build-time tool in Claude Code and
never a runtime dependency.

Whether this repo violates that depends on what "runtime" means here. This is a
developer tool that runs on your machine against your own subscriptions, which
reads as build-time. But it is a long-running orchestrator making autonomous
model calls, which is the shape the rule exists to control, and `registry.py`
already documents Fable as the model most likely to exhaust the subscription
window first (2x Opus 5 against the same window) and as having been withdrawn for
19 days in June 2026.

**I have not changed it.** Two options:

- Treat this repo as build-time tooling and leave Fable seated. Write the
  exemption into `CLAUDE.md` so `df-model-routing` stops flagging it.
- Move the orchestrator seat to Opus 5 and keep Fable as an explicit opt-in for
  a hard session. `ORCHESTRATOR_CHAIN` already supports reordering.

## Recommendation 3: local model as a third participant (technique 8)

Worth doing, and narrower than it sounds. The playbook's rule is the important
part: **local never produces customer-facing text, only structured labels and
transforms.** In this repo that maps to specific jobs:

- **Task classification.** Recommendation 1 needs something to read a task and
  emit a complexity and a kind. That is a constrained label from a short input,
  running on every task, and it is the highest-volume call in the redesign. It is
  the wrong job for a frontier model and the right one for a local one.
- **Codebase map summarization.** `codebase_map.py` builds context. Bulk file
  summarization is a transform.
- **Ledger compaction.** `ledger.py` is append-only and something has to compress
  it for context. Another transform.
- **A cheap first-pass reviewer** whose findings are confirmed by a frontier
  model before they reach the debate. Not a voice in the brain trust: the four
  independent frontier reads are, per `MODE_ROSTERS`, the whole reason the system
  exists, and adding a weaker fifth voice dilutes that.

The integration is small because `providers.py` already abstracts a provider and
degrades gracefully when one is unavailable. Ollama or LM Studio expose an
OpenAI-compatible endpoint, so a local provider is a `base_url` override on the
existing OpenAI provider shape plus a registry entry with a `LOCAL` tier and a
hard rule that it never holds a seat in `MODE_ROSTERS`.

## What I would not do

- Do not add a local model as a fifth debater. It weakens the one property the
  adversarial mode is built on.
- Do not build a new router. Wire the existing one.
- Do not change `PROVIDER_ORDER` semantics. It should stay the deterministic
  tiebreak once capability routing lands, because a reproducible run matters more
  than an optimal one.

## Order

1. Decide the Fable seat question. It is a one-line config change either way and
   it blocks nothing else.
2. Wire task classification into `Orchestrator.run()` and take the fast path for
   `ROTE` and `SIMPLE`. This is the largest saving for the least code.
3. Add the local provider and move classification onto it.
4. Capability-based lead selection and complexity-scaled reviewers.
