# WIRED offline assembly

This branch starts at published `3698ed0ce9add5449c23014057f18bb4f6c2da64`
(parent `6c9b149b6f0159e792f122063b2aaacaf36d04d3`). It combines the
reviewed completion wiring already on that base with offline workflow
acceptance artifacts. It does not activate the separate finding-state
projection or call a live provider.

## Exact composition

The following files are copied byte for byte from offline candidate
`392fec1049d094092f497b27ef2a985707c4adcc`:

| Area | Files |
| --- | --- |
| Workflow acceptance | `tests/test_workflow_acceptance.py`, `tools/workflow_acceptance.py` |
| Wheel validation | `docs/workflow-wheel-validation.md`, `tests/test_workflow_wheel.py`, `tools/verify_workflow_wheel.py` |
| J30 optional worker failure | `docs/review/worker-failure-journey.md`, `tests/test_workflow_worker_failure_journey.py` |
| J31 design fix role | `docs/review/design-fix-role-journey.md`, `tests/test_workflow_design_fix_role_journey.py` |
| Evidence boundary | `docs/workflow-evidence-boundary.md`, `tests/test_workflow_evidence_boundary.py` |
| Finding-state projection | `docs/finding-state-contract.md`, `quadratus/finding_state.py`, `tests/test_finding_state.py` |
| Scorecard | `docs/workflow-scorecard.md`, `tests/test_workflow_scorecard.py`, `tools/workflow_scorecard.py` |

`docs/workflow-acceptance-manifest.json` and
`docs/review/acceptance-map-refresh.md` are copied byte for byte from the
reviewed map commit `6d61486`. The manifest conservatively leaves the J30
required route unproven, maps the J31 role packet without claiming report
coverage, and places E2's non-completion checks under J10.

The completion decision files (`quadratus/completion_decision.py`,
`tests/test_completion_decision.py`,
`tests/test_completion_decision_session_seams.py`,
`docs/completion-decision-contract.md`, and
`docs/review/completion-session-seams.md`) were already present at the base.
Each file's Git blob is identical to the offline candidate's blob. This
assembly preserves the newer base versions of `quadratus/session.py`,
`quadratus/outcome.py`, `quadratus/preview.py`, `docs/workflow-map.md`, and
all lifecycle fixtures and tests. No existing file is changed by this
assembly.

## Verification and limits

Validation uses the frozen image
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`
with the checkout mounted read only, network disabled, and a read-only
container filesystem. The tests are scripted controller and offline data
checks. They do not prove live model judgment or a live continuation. The
projection remains offline: `Session` does not import `finding_state.py`.

Test results for this exact assembly are recorded in the PR handoff.
