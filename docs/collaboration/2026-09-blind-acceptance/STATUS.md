# Joint blind-acceptance checkpoint

Shared branch: `codex/blind-worker-acceptance`, [PR #11](https://github.com/Davisfox5/quadratus/pull/11).
Frozen runtime: **a001c1b**. Scored attempt **b7ccbd24** is saved and stopped.

## Current owner and handoff

**The two-fix repair batch is complete and verified offline.** No new
live trial, budget change, role change or GameTape feature work was performed.

- **Codex:** bounded session/runtime closeout from supplied evidence, plus
  integration tests (`cebc172`). Independently reviewed Claude's provider
  changes. Reconciled crossed guard implementations by retaining Claude's
  stronger finite-positive timeout/exactly-one-attempt guard from `8c4a39a`.
- **Claude:** summary-only provider controls and complete auxiliary accounting
  (`3ef035a`, `62071a3`). Independently approved the caller in `0c2d172` and
  [its review](https://github.com/Davisfox5/quadratus/pull/11#issuecomment-5685746701).
- **Final verification:** 936 tests passed in 53.95s, zero skips, with Docker
  and installed-CLI checks enabled; Ruff and diff-check clean. No model calls.
  Archived usage-field replay yields 68,382 tokens including 2,817 Haiku
  tokens once; original run records remain unchanged.
- **Live verification completed:** two unscored calls, 11,215 reported tokens.
  Grok returned in 11.23s / 6,112 tokens; Claude refused with vendor safeguard
  label `reasoning_extraction` in 2.25s / 5,103 tokens. No retry or fallback.
  [Report and evidence](CLOSEOUT_LIVE_1.md) preserve the limits and uncertainty.
- **Live review closed:** Claude verified the evidence at `933c787`; Codex
  accepted it and clarified harness provenance. Successful Claude closeout
  remains unverified after the recorded safeguard refusal.
- **Current owner: Codex executing scored attempt 2.** Fresh original input
  a8772ab and identical task/private-case commitment, repaired engine ad82a4c,
  unchanged 24-call/500,000-reported-token/900-second/two-worker limits.
  [Frozen inputs](evidence/scored-attempt-2-freeze.json) precede launch. No
  automatic continuation or solver coaching. Claude scores saved output after
  the explicit result handoff; it has no pending work before then.

Provider limits are explicit: Claude can request no tools and one turn; Grok
retains read-only tools with a one-turn request; Codex retains read-only/native-
off controls but no known generic tools-off or turn cap. The latter is bounded
by time/attempt count. CLI output-token ceilings are not hard-enforced by
max_tokens. Empty CWD is not a filesystem isolation boundary. Without a bound project,
existing API-provider support retains prompt/output/time/attempt bounds, but
CLI-specific tool and turn controls do not apply. The live replay records scoped Grok savings; no general savings or complete
native-tool suppression claim is being made.

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
