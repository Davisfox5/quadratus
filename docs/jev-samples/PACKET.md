# Jev sample packet (T-grok-samples)

Author: Grok. Reviewer: Codex. Base: `f96e0c88e7a18156a9582e3aa19de4c9d70ff89f`.
Machine copy: `tests/fixtures/jev-samples/samples.json`.

This packet is a proposal. Answer sets are not frozen until Codex reviews them.
No Jev call, live run, or engine edit was made to produce it. The favicon row
is development-only: Jev already answered it on run `20261001T022457Z-6602cd28`.

## How a lead was assigned

Read from `quadratus/task_kinds.py` `route()` at the base SHA, every ladder
seat assumed available:

- A non-empty `prefer` is tried first. At this SHA those pins are
  scope/decompose → `claude:fable`, review → `openai:gpt-5.6-sol` then
  `claude:opus`, security → Sol, test → Sol.
- Otherwise the difficulty ladder: complex → `claude:opus`, standard →
  `openai:gpt-5.6-sol`, simple → `grok:default`, rote → `grok:worker`.
- `grok:worker` can only return a patch. A task that must run a command or
  write a non-patch file skips it and climbs to `grok:default`. A rote label
  plus `execute` therefore admits the same lead as simple. That is disclosed
  on every row where it happens.
- `tool_first` (perf, frontend render) is an instruction to the lead, not a
  different seat.
- Direct tier, survey recovery, and design-review seats are not varied here.

Unlabelled rule default, from the same code path the live run used when KIND
was omitted: kind `general`, difficulty `simple`, lead `grok:default`.

## What a later experiment may treat as a route change

A label changes the admitted lead only where the `leads` map in the fixture
says the two `route()` results differ. Kind agreement, confidence, and
probability are not scores. The favicon row must not be a scoring item.

## Split

| id | split | why it is in that split |
|---|---|---|
| dev-favicon | development | Jev already answered this text |
| dev-mutation-node | development | the 2026-09-14 incident the ladder comment already encodes |
| dev-trial-sentence | development | control: rote vs simple changes the lead, no command |
| hold-heading | held-out | same shape as the control, unseen text |
| hold-import-note | held-out | review pin vs a ladder kind |
| hold-dotenv-guard | held-out | security pin vs backend on the ladder |
| hold-one-assertion | held-out | test pin: difficulty does not change the lead |
| hold-ledger-append | held-out | test pin vs concurrency on the ladder |
| hold-import-readonly | held-out | review pin vs docs on the ladder |
| hold-list-timing | held-out | perf has no pin; simple vs standard changes the lead |
