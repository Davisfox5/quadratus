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
   reviews stale and keeps their evidence.
3. A delivery is a commit on origin (`deliver`), nothing local.
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
   stands in the way.

Any direct action taken outside the tool stays visible as unqualified until
it is reconciled into the record. A candidate that moved without
`integrate` (a merge nobody reviewed, a rebase) is reconciled with
`candidate --reason`, which keeps the previous SHA in the candidate's
history and leaves the new SHA with no receipts until they are earned.

## Measurement

Coordination time, duplicate or reopened reviews and blocker turnaround are
tracked beside the runtime measures (calls, tokens, wall time, operator
interventions, independently verified completion). Process adoption is not a
new run allowance: the next real-run milestone is chosen under existing
approvals.
