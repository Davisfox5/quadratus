# WIRED offline assembly on the cleared engine

This branch starts at independently cleared engine commit
`f804da4848a5301d47868083253a3238f33b622b`. It imports the reviewed
offline workflow artifacts from assembly
`5aaba2756154861e8fdc1a11a3cf9db39ec3915f` without changing any file
already present on the engine base. The engine's newer wiring, stage, serial,
batch, settlement, guard, tests, fixtures, and workflow map stay on the base.

## Exact composition

These 19 paths have the same Git blobs as the prior reviewed assembly:

| Area | Paths |
| --- | --- |
| Workflow acceptance | `tests/test_workflow_acceptance.py`, `tools/workflow_acceptance.py` |
| Wheel validation | `docs/workflow-wheel-validation.md`, `tests/test_workflow_wheel.py`, `tools/verify_workflow_wheel.py` |
| J30 optional worker failure | `docs/review/worker-failure-journey.md`, `tests/test_workflow_worker_failure_journey.py` |
| J31 design fix role | `docs/review/design-fix-role-journey.md`, `tests/test_workflow_design_fix_role_journey.py` |
| Evidence boundary | `docs/workflow-evidence-boundary.md`, `tests/test_workflow_evidence_boundary.py` |
| Finding-state projection | `docs/finding-state-contract.md`, `quadratus/finding_state.py`, `tests/test_finding_state.py` |
| Scorecard | `docs/workflow-scorecard.md`, `tests/test_workflow_scorecard.py`, `tools/workflow_scorecard.py` |
| Acceptance map | `docs/workflow-acceptance-manifest.json`, `docs/review/acceptance-map-refresh.md` |

The finding-state projection's three blobs were already present at the engine
base, so the candidate adds the other 16 paths only. Every imported path was
compared with the base first; no differing existing file was overwritten.
The acceptance map still leaves the J30 required route unproven, maps the J31
role packet without claiming report coverage, and places E2's non-completion
checks under J10.

## Verification and limits

The focused combined verification uses frozen Docker image
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`
by image ID, with network disabled, a read-only source mount and container
filesystem, and a writable `/tmp` tmpfs. The tests exercise scripted
controllers and offline data. They do not prove live provider judgment or a
live continuation. The text package is not included until reviewed. No
vendor, profile, or activation change is part of this branch.

Focused check results and any remaining limits are recorded in the PR 25
coordination handoff. Independent review is required before integration.
