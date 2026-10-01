# Stage B: paired feature runs, Jev versus the rule (plan, 2026-10-01)

TL;DR: five real GameTape feature tasks, each run twice on identical
checkouts under identical limits, once with `--decider jev` and once
without. The two arms of a pair start together so both see the same vendor
availability. Claude quarterbacks the runs from the IDE on Davis's Mac
(Davis's call, 2026-10-01); Codex is asked for a usage reading and a
light review, not for execution, because its weekly window is near its
end. Nothing here spends until Davis is in the IDE and the packet is bound
to the real tree.

## What the comparison answers

Does routing by Jev's kind and difficulty change which lead takes a
feature, and does that change what gets delivered and what it costs? Stage
A answered only the first half on rote and simple samples (`#35` comment
5924736888). Stage B puts feature-sized tasks in front of the real engine
with the full lifecycle: checks, captures, design review, audit.

It does not answer whether Jev is "right": there is no ground truth for a
feature's kind or difficulty. The paired design makes the arms comparable;
the outcome measures are completion, closed-clean count, failed checks, the
lead actually chosen, calls, reported tokens and wall time. A faster failed
cell is not a win.

## Design

- Five tasks, two arms, one repeat: ten runs. The runner supports
  `repeats` if a second pass is authorized later.
- Arms differ in one flag. The jev arm omits any KIND line so Jev decides
  kind and difficulty; the rule arm omits it too and takes the engine's
  default (general/simple). The packet validator refuses a KIND line.
- Each cell is its own detached worktree at the baseline SHA with its own
  `.quadratus` state, so runs never share source or state.
- Order is counterbalanced: even tasks start jev first, odd tasks rule
  first, and both arms of a pair are launched together. Pairs run one at a
  time by default (`--pairs 1`); `--pairs 2` doubles vendor load and is a
  call to make after the first pair's timings are in.
- Limits per cell, from Codex's Stage B outline (`docs/jev-experiment-plan.md`):
  20 calls, 500,000 reported tokens, 600 s wall, two workers. Ten cells
  bound the series at 200 calls and 5M tokens; sequential pairs take at
  most about 100 minutes, two pairs at a time about 50.
- Same `check`, `extra_checks` and `capture_profile` for every cell, read
  from the packet. The engine's own integration gate, capture and design
  review apply unchanged.

## The five features (draft, bound in the IDE)

The texts in `tools/jev_stage_b/packet.example.json` are drafts written
without the GameTape tree in front of me. Before `prepare`, each SCOPE gets
real `permitted_paths`, `acceptance` lines a check can execute, a line
ceiling within `MAX_TASK_LINES`, and a capture for UI work. Nothing in the
text may hint at a label.

| id | feature | why it is in the set |
|---|---|---|
| f1-preview-errors | per-row error table under the import preview | frontend plus a response shape; a plausible standard |
| f2-project-search | client-side search over the project list | frontend/simple control |
| f3-project-rename | rename endpoint, persistence, card control, test | backend plus frontend plus data; the likeliest to route standard |
| f4-tags-export | CSV export endpoint with a round-trip test | backend/simple with a test pin question |
| f5-empty-state | empty-state panel on the project list | frontend/rote-to-simple control |

Two controls, three where a Jev label could plausibly move the lead to Sol
or Opus. The rule arm's default is general/simple → `grok:default` on all
five, so a lead change in the jev arm is the routing effect under test.

## The OpenAI window is the binding constraint

Every run spends on the ChatGPT subscription whether or not Sol leads: the
requirements review and the final audit are Sol seats (about 28k and 43k
reported tokens on the Stage A live run), and a Sol lead adds the lead's
own context (about 117k on that run). Ten runs are therefore roughly 0.7M
to 1.3M tokens on the OpenAI side, and Davis reports about 10% of the
weekly window left. So:

1. Ask Codex for one number before launch: what the ChatGPT window shows
   remaining. No analysis, no review.
2. Run the first pair alone (`--only f2-project-search`, the cheapest
   control) and read `usage.jsonl` for the OpenAI-side total per cell.
3. Extrapolate to ten cells. If the window will not hold the series, cut
   to the three route-sensitive pairs (f1, f3, f4) and say so in the record
   rather than running until a seat goes dark.
4. If any seat becomes unavailable mid-series the running pair finishes
   or stops on its own limits, the next pair is not launched, and the
   series is reported as partial. An arm that ran with a seat missing is
   not comparable to one that did not.

Jev's own cost is negligible (about 1,500 tokens and a quarter of a cent
per cell) and comes from existing TypeSafe credit.

## Running it (from the IDE on the Mac)

    python3 tools/jev_stage_b/pairs.py prepare --packet /abs/packet.json --out /abs/stage-b
    python3 tools/jev_stage_b/pairs.py run --out /abs/stage-b --only f2-project-search
    python3 tools/jev_stage_b/pairs.py collect --out /abs/stage-b
    python3 tools/jev_stage_b/pairs.py run --out /abs/stage-b          # the rest, one pair at a time
    python3 tools/jev_stage_b/pairs.py collect --out /abs/stage-b

`run` calls `quadratus.project_run.run_project` directly with the packet's
`RunLimits`, because the CLI does not expose limits. A failed cell is a
result with an error, never a reason to stop its sibling. `collect` reads
`result.json`, `budget.json` and `invocations.jsonl` per cell and writes
`comparison.md` and `comparison.json`.

Needed in the IDE before `prepare`: the GameTape repo path and baseline
(1cd9264 or current main), `TYPESAFE_API_KEY` in the project's `.env`, the
capture profile and check command used on run `20261001T001902Z`, Codex's
window reading, and Davis's yes on the five features or edits to them.

## Evidence and reporting

Per cell: `result.json`, `budget.json`, `usage.jsonl`, `invocations.jsonl`,
`changes.diff`, `report.md`, captures and design verdicts under the cell's
`.quadratus/runs/<id>`. Series: `manifest.json` (packet digest, order,
states, timings) and `comparison.md`. The report separates the three
route-sensitive pairs from the two controls, gives both arms' completion
and closed-clean counts, failed checks, leads, calls, tokens and seconds,
and the per-pair deltas. Jev decision tokens are reported apart from the
work. Recorded on `#46` as T-jev-stage-b with the authorization comment
and the terminal receipt.

## Stop rules

Any cell's budget stop is that cell's result, not a series stop. The series
stops before the next pair on: a seat reported unavailable, unknown usage
on any cell, Jev's resolved model changing from `jev-1.13.0`, a credential
failure, or a change to the packet after `prepare`. Nothing is retried,
re-labelled or replaced after seeing a result.
