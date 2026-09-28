# O-NEXT-10: live readers left after P3.4

Audit of `quadratus/session.py` at 7403511 for per-task decisions made after
`_build_contract` that still read live `self.config` without going through
`_required` or the contract. Read-only on the engine. Reproductions are strict
xfails in `tests/lifecycle/test_live_reader_audit.py` (one passing control).
Excluded as asked: `design_self_verify`, `capture_profile`,
`design_cross_check` (O-NEXT-08), and run-level budgets.

## TL;DR

- Five live readers can make a task be told, checked or closed differently
  from its contract after a mid-task config change.
- The biggest is the write grant: `allow_writes` is read live at every
  editing call and prompt. The contract records `authority.write_grant`, but
  nothing reads it, so a drift on widens a read-only task to writes.
- The cheap gate subset reads the live gate instead of `_bound_gate()`, so a
  gate that appears mid-task can close the task before review.
- The legacy readers list in docs/workflow-map.md still matches the code.

## Findings

| # | Where (session.py) | Field | Decision it drives | Contract has an equivalent? |
|---|---|---|---|---|
| A | 1096, 1154, 1215, 1241, 1556, 1622, 2536, 4666, 4740, 4820, 4833 | `allow_writes` | whether an editing call gets writes, the dependency bracket, lead and revision instructions, worker write requests, turn-limit close handling | Yes, `authority.write_grant`, recorded only |
| B | 2390-2391 (label at 4922) | `integration_gate` | whether cheap gates run after the draft and can close the task before review | Yes, `_task_gate` / `_bound_gate()` and `required_checks` |
| C | 4526 | `requirements_ledger` | a review-only design failure closes as audit debt or stops | No |
| D | 2653 | `security_verdict_json` | verdict protocol (prose or JSON) used to check the security task | Partly: `security_verification` fixes that it runs, not how |
| E | 1134-1135, 1895-1896 | `default_scope` | operator limits shown to the lead and applied in every scope check | No: `scope` holds the task scope only; the outer limit is live |

## Reproduction outputs (`pytest --runxfail -rA`)

```
A invoke allow_writes: [True]            # contract write_grant 'none'
A lead prompt says: edit method
A revision delivery: Update the project using the edit method in your role instru
A' invoke allow_writes: [False]          # contract write_grant 'operator'
A' lead prompt says: no edit grant
B contract required_checks: ['check']
B t1 collaborator calls: []
B t1 stages: ['dispatch', 'draft', 'checks']
B gate results: ['FAILED']
B control t1 collaborator calls: ['collaborator', 'collaborator']
C audit debt at dispatch: True after drift: False
D verifier asked for JSON: [True]
D verification edge: False unsatisfied: ['verification']
D completed: False                        # same journey without drift completes
E contract authority: {'write_grant': 'operator', 'edits': 'scoped:40'}
E out_of_scope at dispatch: [] after drift: ['app.py']
```

Default run: `1 passed, 6 xfailed`.

## Legacy readers retained (workflow-map.md)

All five entries match the code at 7403511:

- `open_findings`, `_partial_tasks`, `_design_unverified`: typed ∪ legacy
  with recorded disagreement at `_unresolved_partial`, the open-findings
  check near 3232, `_closed_with_findings` near 4411 and `_design_debt`.
- `legacy_open_findings` feeds `completion_decision` (line 494 there).
- Design-debt naming returns `typed or legacy`.
- `stop_reason` is set with the typed stop in `_stop_with` and becomes
  `result.error` in `project_run.py`.
- Items 22 and 23: `collaborators_for`, `_brief_design_reviewers`, the review
  lens and `_lead_prompt` read the contract through `_required`.

One adjacent note, not a mismatch: `_open_work` names open findings from the
legacy `open_findings` list alone (line 4616), while partial work is named
from the union since item 21. Not reproduced here.
