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

**Attempt 4 ran and stopped on the orchestrator's exhausted window**, which
Davis predicted before the launch. Run `bb61698d`, engine `a0f16ec`: one call,
3.5 seconds, nothing written, source byte-identical to the input. Fable
returned a vendor limit envelope; the seat was recomputed to Astra as designed,
and the run then stopped at `unknown_usage` **before Astra was invoked**,
because the limit envelope carries an empty `modelUsage` map. Astra's own
window state is therefore unknown. Per Davis's standing instruction nothing was
changed and no retry was made. [Result and review](evidence/scored-attempt-4/README.md).

**Fixed on Davis's instruction: the run now proceeds to the fallback seat
whether or not the primary has tokens left.** An all-zero token report is read
as a report of zero rather than as missing usage, so a vendor limit envelope no
longer latches the budget before the deputy can be reserved. A missing or
unreadable `usage` still stops the run, which keeps the timeout-after-partial-
work case intact. Suite 976 passed, 2 skipped.

**Attempt 5 reached the fallback seat and could not read the source.** Run
`984ad6ee`, engine `adc774e`: Fable's window still gone, but the budget stayed
open and **Astra was reserved and called** — the attempt-4 fix confirmed live.
Astra then found its own sandbox blocked (`bwrap: No permissions to create a
new namespace`, denied by the container's `--cap-drop ALL`) and correctly asked
the operator instead of guessing; a blind run has no operator channel, so the
run stopped. The same failure appears in attempt 3's Sol lead output: **every
OpenAI seat in every isolated run so far has been blind to the project.**
[Result and review](evidence/scored-attempt-5/README.md).

**Decided and implemented (Davis, 2026-09-17): the container is the boundary.**
Codex's inner sandbox stands down inside `run_isolated` and only there, behind
an explicit `QUADRATUS_CONTAINED` assertion the launcher makes; Claude and Grok
are unchanged, since a tool denial needs no privilege. Measured first: allowing
user namespaces is the only route that keeps both sandboxes, and it opens the
surface behind most container escapes for every process in the container.
`tools/acceptance/preflight.py` now checks, with no model call, that the work
tree is readable and that a seat on each vendor can read a real file out of it.

**Attempt 6 stopped at the same failure, because that fix reached the wrong
half of the fleet.** Run `be04e367`, engine `ad8fc76`: the seat fell to Astra
again, Astra was called, and it hit the same `bwrap` error 18 seconds in with
containment asserted. The substitution was consulted only on the
*restricted-seat* branch — and senior seats are never restricted, so it could
not reach an orchestrator, lead, reviewer or consultant, which is every seat
that has ever been blind. Two calls, 57,183 tokens, nothing written; private 0
of 9, public 0 of 13, baseline 100 passing.
[Result and review](evidence/scored-attempt-6/README.md).

**Repaired, and the mode question is now settled without a model.** The
substitution applies to every rank of Codex seat
(`contained_restricted_args` → `contained_sandbox_args`), and the preflight
exercises each sandbox in the mode the run will really use, reading a real
file, so a failure is always a blocker. The earlier note that no model-free
check could settle this was wrong: `codex sandbox` honours `-c sandbox_mode=`,
and inside the container `read-only` and `workspace-write` both fail with
bwrap's namespace error while `danger-full-access` returns the file. Verified
against the real container both ways. Suite 1011 passed, 1 skipped (gradio
absent); ruff and diff-check clean.

**Attempt 7: an OpenAI seat read the project for the first time.** Run
`7303ff7d`, engine `cd33f7d`. No `bwrap` anywhere; Astra ran three shell
commands against its disposable source copy and wrote a task grounded in
`app.py`'s export column format. Across seven attempts no OpenAI seat had done
grounded work before, so the containment repair is confirmed live.

It then stopped on two things. **Grok's subscription session had expired**
(token dead since 2026-09-14T22:13Z; `grok models` says so on the host too) —
a credential fact, not an engine defect. **Then that refusal ended the run**:
it reported no usage, the budget latched `unknown_usage`, and the Sol recovery
the engine had already selected was refused. Private 0 of 9, public 0 of 13,
baseline 100 passing, empty diff.
[Result and review](evidence/scored-attempt-7/README.md).

**Repaired:** a vendor refusal that never reached a model reports zero spend
rather than unknown — narrow, erring towards unknown, with the
timeout-after-partial-work guard pinned by its own test. And the preflight now
asks whether each CLI is signed in (positive proof for codex; known-failure
wording for grok, which exits 0 on a dead session; not applicable for claude).
Verified in the container: exit 1, naming grok. Suite 1030 passed, 1 skipped.

**Blocked on Davis: `grok login`.** The preflight refuses to launch until the
session is live, which is what it is for. Attempt 8 goes the moment it passes.
The worker tool-fit repair (`a0f16ec`) has now been present and unexercised for
four attempts — attempt 7 selected a lead and wrote a task but never
commissioned an errand.


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
