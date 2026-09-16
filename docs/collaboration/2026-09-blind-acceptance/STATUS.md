# Joint blind-acceptance checkpoint

Shared branch: `codex/blind-worker-acceptance`, [PR #11](https://github.com/Davisfox5/quadratus/pull/11).
Latest frozen runtime: **ad82a4c**. Scored attempt **d9df9b6c** is saved and stopped.
Earlier attempt **b7ccbd24** and its engine **a001c1b** remain unchanged.

## Current owner and handoff

**Attempt 3 ran, was scored and is reviewed. Claude launched it on Davis's Mac
at his direction, Codex being out of context.** Run `f19eee8c`, engine
`4795062`, identical `a8772ab` input and brief, private commitment re-verified
unchanged. Stopped at 394.22s on the reported-token threshold, 523,473 against
500,000, three calls of 24, **nothing written**. Private **0 of 9**, public
examiner **0 of 13**, unchanged GameTape baseline 100 tests passing offline.

**Three vendors and a controlled worker ran in one scored attempt for the first
time**, and the worker is the finding: Haiku on the `code` errand took 312,518
tokens in 324 seconds, 60 percent of the run, across eleven turns, producing
101KB of code as text it had no tools to write. Its restricted seat held
exactly what it claims (no write tool, no Task, no subagent) and bounds
permission, not volume. Opus was selected and never invoked; closeout and Opus
review remain unreached. Details: [result and review](evidence/scored-attempt-3/README.md),
[Claude log](CLAUDE_LOG.md).

**Repair landed (Davis's direction, not the turn cap):** worker tool fit is now
checked on both sides. The lead declares what an errand needs and the harness
refuses a mismatch before the budget is charged or any call is made; the worker
is told in its own prompt what it has, that it has no shell, and that it should
reply `NEED TOOL: <what>` in one line rather than working around a missing tool;
and the escalation is capped at one ask plus one reissue. `needs_from_text` also
now sees an interpreter named by path, which the attempt-3 brief used. Suite 972
passed, 2 skipped; ruff and diff-check clean.

**Open for Davis:** whether to authorise attempt 4. Nothing is queued and no
further run has been launched.


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
- **Attempt 2 complete, unsuccessful:** 335.78 seconds, two calls, 469,340
  reported tokens. Scope stop: 223 changed lines against declared 100; no
  workers, no closeout, no retry. Saved app regression suite: 111 passed with
  source hashes unchanged. [Result and review assignment](evidence/scored-attempt-2/README.md).
- **Current owner: Claude independent scoring/review.** Review the unchanged
  private cases against attempt 2 output, decomposition/scope findings and the
  small Codex metadata patch. Codex owns disposition; no next live run is queued.
- **Attempt-2 review scored and dispositioned (Claude, at Davis's direction
  because Codex was out of context):** 0/9 private, 0/13 public, 111 app tests;
  scope estimate error plus a self-contradicting task signature. Repairs
  landed offline: a signature lint in `read_scope` that fails into the
  existing correction round, a code/test split in the scope report and
  `result.json`, a stop message that reports the measurement without a
  cause, and decomposition-prompt text asking for final descriptions and a
  separate code/test estimate. 934 passed, 7 skipped here; Ruff clean. No
  live run queued; policy items (test lines counting fully, Grok per-step
  cost) remain Davis's. See [Claude log](CLAUDE_LOG.md).
- **Post-run patch:** retain whitelisted auxiliary metadata on successful
  invocations. Reproduced missing 2,815-token provenance with observed values;
  totals were already correct. Final full engine suite: 937 passed in 55.00s,
  zero skips with Docker/installed-CLI checks; Ruff clean. Frozen output unchanged.

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
