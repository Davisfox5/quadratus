# Direction: one candidate, one live run

Standing ruling from Davis, recorded 2026-09-28. This file is the ruling.
Comments on any PR are for decisions, verdicts and blockers against it, and
cannot amend it. Only Davis amends it, by editing this file.

## Why

Run 19 is the reason to keep the new design: the application passed an
independent grade while Quadratus closed zero tasks after 6.89 million
reported tokens. Typed outcomes and a task contract fixed at dispatch are the
right correction. They are not yet a result. Scripted journeys cannot show
that a live model will finish GameTape, and another review lane will not show
it either. Nineteen live runs were spent as a debugger. That stops.

## The finish line

One real GameTape completion under the agreed bounds, then five runs for
reliability. Review findings, test counts and closed threads are not progress
toward it.

## Rules in force

1. **No widening.** No new lanes, no new journeys, no new branches. A
   residual that does not block the gate below goes to the backlog
   (section "Backlog"). Grok is read-only.
2. **One integrator.** Claude (cloud) integrates and does nothing else: no
   feature authoring. Codex is root reviewer and owns local execution on the
   Mac. Nobody else has an assignment.
3. **One candidate.** PR #35 against `main`. Composition: the latest cleared
   engine head, the already-reviewed docs, tests and evidence, and the
   scorecard fixture fix Codex specified. One SHA. A change after that SHA is a
   new PR against `main`, after the live run.
4. **PR #25 is not merged as titled.** Its description is stale and its
   branch is not that change. #25, #33, #34, #11, #26, #30 and #32 close as
   superseded when #35 merges.
5. **The gate, before any live call.** All on the exact #35 SHA:
   - integration review clean (Codex, composition only, blocking findings
     only);
   - CI green: ruff plus the full suite on 3.11 and 3.12;
   - the journey suite green on that SHA;
   - the package installs as a wheel outside the source tree, entry points
     and resources present;
   - the Run 19 tree copied from the 38 hash-matched files, the private shim
     absent, hashes read back;
   - a host receipt for the browser check under the intended UID, owned HOME
     and TMPDIR, network none, no vendor call.
   Fix only what fails this list.
6. **One run, then stop.** Bounds unchanged: 120 calls, 6,000,000 reported
   tokens, 7,200 s internal, 7,500 s outer wall, two workers, 20 tasks. The
   token figure is a post-return stop, not a budget; it is not raised.
   Hypothesis: Quadratus reports complete only if the independent grade
   passes, and any failure names its cause instead of a blank error or a
   false complete.
   - Completes inside the bounds: run five for reliability.
   - Fails: fix that one named cause offline, rerun once. No return to
     one-fault-per-run.
7. **Reporting.** One comment on #35 per run: completion, tasks closed,
   calls, reported tokens, wall seconds, stop reason, grader result. Nothing
   else is posted about the run.
8. **Process hygiene.** No cadence posts, no status-only posts, no inline
   clock stamps. A candidate is ready only when its SHA is on `origin`. Ruff
   is installed in the frozen review image. A new blocker names the
   requirement it violates and whether it is reachable in normal execution,
   an invariant, or a future capability; otherwise it is backlog.

## Amendment, 2026-09-30: the survey run

Davis's ruling after the two post-#35 live runs each ended on their first
task-level fault: one run should continue through many errors and collect
them all. Enacted as #39 (task-level failures), #40 (prompt rule parity),
check debt (J40) and the survey profile (J41), with Codex reviewing each and
owning the launch. The survey run is judged by verified completion and
unique causes resolved; its `survey` section is diagnostic data, reported
apart from the rule 6 hypothesis, and never redefines success. The
orchestrator never asks for more allowance: the operator sets it.

## Backlog (not blockers)

- Unverified design evidence on an editing task still stops the run after
  the task closes (`DesignUnverified`); map G8 says only that task's own
  renders discharge it, so routing it to a RESOLVES repair needs that rule
  revisited first. A failed check became requirement debt on 2026-09-30
  (`check.failed` findings).

- Task-scope snapshot proposal `550c58a`.
- F2 stale merge context (O-NEXT-15).
- O-NEXT-13 capture seam proposal.
- O-NEXT-16 residual RESOLVES/RECAPTURE ledger gap.
- G-NEXT-03 process measurement.
- Any Opus or Sol candidate not in the #35 composition.
- Phase-4 profile proposal beyond what the gate in rule 5 needs.
