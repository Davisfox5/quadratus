# How Quadratus is developed: the shared record and the short rules

Adopted 2026-10-01 from the coordination plan Codex published on #35
(comment 5922888762) with Davis's approval. `docs/DIRECTION.md` remains the
standing ruling; this supplements its process hygiene. Claude is the
incumbent coordinator and integrator; Codex is the root reviewer and owns
local execution and live runs.

## One shared development record

`docs/dev-record.json` is the single machine-readable record, edited only
through `tools/dev_record.py`. It holds the active integration PR, the exact
candidate SHA on origin and the named gate for the current milestone; and
for each task its id, agreed purpose, exact base SHA, owned files or
subsystem, author, independent reviewer, state (queued, claimed, delivered,
reviewed, integrated, blocked, deferred), delivery SHA, reviews bound to the
reviewed SHA and scope, blockers, decisions, and whether the assignee has
acknowledged the work (an assignment is not a start). `python3
tools/dev_record.py render` prints the concise view that goes in the active
PR body.

`docs/workflow-acceptance-manifest.json` stays the journey coverage map and
`tools/workflow_scorecard.py` the measured-result tool. The record references
their evidence; it never copies it and never turns an UNPROVEN row green.

## The rules

1. Before work, read `docs/DIRECTION.md` and the current record, then claim
   a unique scope (`claim`). Overlapping active claims and a base that is not
   the candidate's exact SHA are refused unless the coordinator resolves them
   explicitly (`--resolve`).
2. One author and one independent reviewer own each area. A review is bound
   to the exact reviewed commit and scope (`review --sha`); the author's own
   review is not an independent receipt. A later delivery marks earlier
   reviews stale and keeps their evidence. A review scope names only files
   the task owns; a scope covering part of them is recorded as partial and
   does not clear the task, and a `--scope` given with no paths is refused.
   Extending a reviewed or integrated task's scope returns it to delivered,
   because the review covered the old scope. A cleared review never resolves a
   blocker: only `unblock` records a resolution. `integrate` re-checks the
   record rather than trusting the state: a cleared review of the delivered
   SHA must cover everything the task owns now, and no blocker may be open
   (Codex review of 736e94b, Z5-Z6).
3. A delivery is a commit on origin (`deliver`), nothing local. So is the
   candidate `integrate` moves to; the integrated SHA is the delivery the
   cleared review covered, and the new candidate must contain it (the
   delivery is an ancestor of the candidate), since a candidate on origin
   that never took the work in is not an integration.
4. When a shared setting or handoff is faulty, inventory all its consumers
   and deliver one bounded correction for the shared cause. Review the change
   and its relevant neighbours; repeat a cleared review only for a concrete
   new concern.
5. Every blocker names the violated agreed requirement, the observable
   failure, its reproduction or evidence, and a classification: reachable in
   normal execution, an architectural invariant, or a future capability.
   Synthetic evidence is labelled as such. Unrelated improvements go to the
   backlog.
6. The coordinator maintains the finite milestone gate. New facts can reveal
   a real blocker; a new capability or acceptance requirement needs an
   explicit scope decision. Comments carry decisions, verdicts and blockers,
   not repeated status narratives.
7. Required receipts (CI, acceptance, independent review) are recorded per
   exact SHA (`receipt`) with failed, missing, skipped and unproven kept
   distinct; `ready` says whether the candidate can integrate and names what
   stands in the way, including any task's unresolved blocker whatever its
   state, and any integrated task whose integrated commit has no cleared
   review covering everything it owns now. A task that is claimed, delivered or reviewed but not
   integrated keeps the candidate open.
8. Every mutating command holds a lock on the record for its whole
   load, validate and save, so two agents acting at once cannot both pass
   validation and silently overwrite each other. A command that cannot get
   the lock within `LOCK_TIMEOUT` is refused, not queued.

Any direct action taken outside the tool stays visible as unqualified until
it is reconciled into the record. A candidate that moved without
`integrate` (a merge nobody reviewed, a rebase) is reconciled with
`candidate --reason`, which keeps the previous SHA in the candidate's
history and leaves the new SHA with no receipts until they are earned.

## Spending less per fix (adopted 2026-10-10)

Davis's direction after a week of review rounds that produced no run, with
Codex's adjustments:

1. Review looks first for a false success: anything that lets Quadratus
   report work done, met or passing when it is not. Crashes, stalls and wasted
   calls are still caught, but by cheap offline controls (scripted model
   replies, no provider call), not by waiting for a token bill.
2. Fixes are batched. Findings collected between runs land on one head, are
   reviewed as a diff against the last verified head, and get one full suite
   on that final head. A change that only moves constants or paths gets a
   diff-only check.
3. No acknowledgement comments and no per-CI-tick status. A comment carries a
   verdict, a blocker or a decision.
4. Every run is judged by `tools/truth_check.py`, which reads only the run's
   record and imports nothing from the engine. It answers verified,
   unverified or not met; missing evidence is unverified, never success. It
   checks the final passing check ran test cases on the final source, that no
   required check was skipped, that each requirement has an audit verdict of
   met, that nothing reported NOT RUN still stands, that no finding is open
   and that UI changes carry approved capture evidence (with `--project`, that
   the screenshots exist).
5. Canaries before runs: `tests/lifecycle/test_truth_canaries.py` drives the
   real run with scripted replies through failing tests, a project with no
   tests, an interrupted run and missing screenshots, and each must come out
   unverified or not met while a good run comes out verified. One small,
   visible live canary then checks the real integration.
6. Order: truth check and offline canaries, then the batched fixes, then one
   bounded feature run, then tokens per independently accepted requirement
   is measured before anything expands. Replay stays narrow: the scripted
   harness covers failure paths already observed, and is not grown into a
   general replay system until a run shows the need.

## Measurement

Coordination time, duplicate or reopened reviews and blocker turnaround are
tracked beside the runtime measures (calls, tokens, wall time, operator
interventions, independently verified completion). Process adoption is not a
new run allowance: the next real-run milestone is chosen under existing
approvals.
