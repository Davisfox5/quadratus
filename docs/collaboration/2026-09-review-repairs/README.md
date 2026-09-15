# Codex + Claude review repairs

**Current result:** [STATUS.md](STATUS.md) records the completed offline batch,
final checks, draft PRs and deferred live/provider work. The logs preserve the
intermediate coordination and findings.

## Start here

The owner authorized joint implementation on September 15, 2026. Both agents
use `codex/claude-review-repairs`. Codex works in the Mac checkouts below; Claude
confirmed a separate cloud checkout, so coordination uses fetched Git commits
and separate logs rather than assuming edits appear in the same filesystem:

| Repository | Shared local tree | Starting commit |
| --- | --- | --- |
| Quadratus | `/Users/davisfox/Documents/GitHub/quadratus` | `39fc15e` |
| GameTape | `/Users/davisfox/Documents/GitHub/sports-video-tagger-acceptance-v2` | `832e50c` |

GameTape's remote is `Davisfox5/sports-video-tagger`. Do not work in the separate
`sports-video-tagger` main checkout. The existing acceptance branch remains intact.
Read each repo's `CLAUDE.md` and the review context in
`docs/handoffs/reliability-repair/GAMETAPE_COMPLETION.md` before edits.

## Ownership and coordination

These are file reservations, not a claim that Claude has started. Claude should
create `CLAUDE_LOG.md` here with its first action, claimed paths, and test results.
Codex writes `CODEX_LOG.md`. Read the other log before extending scope.

| Owner | Reserved work | Paths |
| --- | --- | --- |
| Codex, peer review/validation | Scope/telemetry/verdict and TaskSpec/routing/recovery integration implemented; accounting labels corrected | `quadratus/scope.py`, `quadratus/session.py`, `quadratus/runtime.py`, `quadratus/delegation.py`, new focused test files |
| Claude, accepted by Codex | GameTape UI and portable test lane, including Codex's focus/runner follow-ups, verified at `a8772ab` | GameTape `static/`, `templates/`, `tests/`, `docs/`; browser support files as needed |
| Claude, accepted by Codex | Restricted-worker capability helpers and Grok diagnostic metadata, including peer follow-ups | Quadratus `quadratus/task_kinds.py`, `quadratus/cli_providers.py`, new tests; integrated into session/runtime |
| Codex, implemented | Accounting labels and evidence qualifications; integration of agreed routing/recovery changes | Current repair documentation; historical evidence remains identifiable |
| Deferred | Video-operation lock redesign | GameTape `app.py`; no lock removal in this batch |

Do not edit another agent's reserved file without recording an explicit handoff
in your own log and seeing the other agent release it. Use path-specific staging;
never `git add .`, `git add -A`, reset, clean, force push, or switch this shared
tree's branch. Commit only your own reviewed changes. Check the index before
committing; if it contains another agent's work, coordinate first. Record commit
IDs and commands/results in your own log. One agent writes each log to avoid
collisions. Push normally after checking the remote; never overwrite it.

## Agreed correction boundaries

- A provider response succeeding and task acceptance failing must both remain
  visible, with one invocation's usage counted once. Preserve partial edits.
- Capability matching must distinguish command execution, returned patches,
  and direct filesystem writes. Restricted workers can return patches without
  acquiring write tools. Do not widen approved roles or permissions.
- Recovery is bounded and cannot replay an editing call blindly. Inspect source
  first; unknown or partial writes require preserved state and deliberate
  continuation. No new expensive vendor run or paid API fallback in this batch.
- Verdict parsing must reject contradictory or ambiguous outcomes. Do not clear
  unresolved findings just because the word RESOLVED appears somewhere.
- Keep busy focus inside the dialog; on completion choose an intentional target
  without stealing focus from someone who moved elsewhere during the request.
- Browser tests belong in GameTape and must establish their own state. Use
  disposable synthetic data; a placeholder video is not media acceptance.
- Four killed mutations prove those specific checks, not comprehensive coverage.
- Recomputed usage totals do not prove Sol parent/child non-overlap. Describe that
  assumption, avoid double-counting Haiku, and preserve unknown usage.
- Video export and editing share mutable media. Do not simply remove the store
  lock; a later batch needs source snapshots/version checks and race tests.

## Completion and review

Each log entry records intent, changed files, result, checks actually run, and
remaining limitations. Mark a task ready for peer review only after its focused
regressions pass. Review the other agent's patch independently. Run the combined
offline suites after integration; distinguish missing dependencies/skips from
passes. This work does not merge into main or deploy either application.

Independent Codex GameTape results and mutation details are saved in `evidence/`.
The session integration patch is a historical review snapshot, superseded by
`15ade71`; do not apply it again. Current status and any remaining peer findings
are at the end of each agent's log.
