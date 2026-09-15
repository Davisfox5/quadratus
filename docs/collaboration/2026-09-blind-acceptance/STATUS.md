# Joint blind-acceptance checkpoint

Shared branch: `codex/blind-worker-acceptance`, [PR #11](https://github.com/Davisfox5/quadratus/pull/11).
Frozen runtime: **a001c1b**. Scored attempt **b7ccbd24** is saved and stopped.

## Current owner and handoff

**The two-fix repair batch is active.** User authorized implementation; no new
live trial or budget change is part of this batch.

- **Codex:** session/runtime closeout caller and integration regressions. Same
  model, compact inline evidence, empty scratch directory, low effort, one
  attempt and at most 60 seconds. Scope grants and source-discovery instructions
  omitted for closeout only. Normal review/edit calls retain their behavior.
- **Claude:** provider summary_only controls and robust auxiliary usage parsing.
  Initial accounting patch 32356f1/8bdc49a is pulled. Codex found that malformed
  auxiliary metadata must stop the budget rather than return a partial total;
  Claude acknowledged the correction and exact caller interface in
  [comment5685567221](https://github.com/Davisfox5/quadratus/pull/11#issuecomment-5685567221).
- **Next trigger:** both lanes pushed, then independent cross-review and the
  combined offline suite. Codex owns this integration step; Claude reviews the
  caller while Codex reviews provider accounting/control changes.

Provider limits are explicit: Claude can request no tools and one turn; Grok
retains read-only tools with a one-turn request; Codex retains read-only/native-
off controls but no known generic tools-off or turn cap. The latter is bounded
by time/attempt count. CLI output-token ceilings are not hard-enforced by
max_tokens. Empty CWD is not a filesystem isolation boundary. No new live
savings or complete native-tool suppression claim is being made.

Scoring/reconciliation from the prior run is complete. All-worker coverage
remains unvalidated; the saved original output and private cases are unchanged.

## Saved result

- **Incomplete**, stopped at the reported-token threshold after **438.94s**.
- **3 provider attempts, 614,386 controller-reported tokens**, including
  **114,386 overshoot** from the last in-flight call; no fourth attempt/retry.
- Fable planning: 65,565; Grok implementation: 290,408; Grok closeout: 258,413.
  No controlled workers/OpenAI seats/Opus review/native children observed.
- Vendor envelope additionally reports **2,817 Haiku auxiliary tokens** outside
  that controller total. Preserve the distinction; they were not a worker.
- Required-column backend preview and two tests saved; players/duplicates/UI/
  docs incomplete. Independent archive reconstruction: **102 Python tests pass**,
  source hashes unchanged. Claude subsequently reports 7/9 private cases passing.
- Container removed, auth seeds deleted, no automatic continuation. The private
  archive remains unopened by Codex and was never mounted to the solver.

## Completed readiness evidence

Claude's delimiter and spawner/scheduler fixes were merged before freezing.
The corrected Grok tool list omitted targeted names and retained read/write/
exec. Claude/Sol scoped probes also passed. These are scoped reports, not a
universal vendor billing guarantee. Full local engine suite: **891 passed**,
no skips, Docker/installed-Codex checks enabled; Ruff/diff clean.

Freeze: [source/runtime/config/image/fixture commitment](evidence/scored-attempt-1-freeze.json).
Logs: [Codex](CODEX_LOG.md), [Claude](CLAUDE_LOG.md).
