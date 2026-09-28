# J30 worker failure journey review

Base: `9eabf69c0627bd605f72ff1bcd8d9c700d216583`. This review covers only offline, scripted provider responses. The replay uses `tests.lifecycle.harness`, which replaces the vendor CLI launch while retaining `run_project`, `Session`, worker dispatch, the integration gate, the typed task outcome and `DONE`.

## What the new journey proves

`tests/test_workflow_worker_failure_journey.py` dispatches a `check` errand from a lead's `WORKER` reply. The fake Claude worker returns its normal error envelope. The lead's next prompt contains the error-as-result (`failed and produced nothing`, `worker unavailable`); the lead edits the project itself. The real integration check passes, the task's fixed contract requires `checks`, its `checks` edge is true, no mandatory edges are unsatisfied, and the run completes after the orchestrator says `DONE`. This adds the final edge and terminal-outcome assertions to the existing failure recovery coverage.

Existing narrower coverage is preserved: `tests/test_reliability_repair.py::test_a_rejected_patch_reaches_the_lead_instead_of_ending_the_run` tests recovery within `_draft_with_channels`; `tests/lifecycle/test_unknown_failures.py::test_a_declared_worker_failure_returns_to_the_lead` follows a whole run but stops at the task cap; `tests/test_worker_loops.py::test_lead_retries_failure_with_two_siblings_and_receives_both` tests the sibling retry path. The new case does not repeat their refusal, budget, or unknown-exception checks.

## Required errand gap

There is no required-worker declaration to exercise. `quadratus.contract.Required` lists checks, design evidence and review, security verification, and settlement. It has no worker field. `TaskOutcome.unsatisfied()` and the completion guard derive mandatory edges from those fields. `Session._serve_worker` reports a declared worker failure to the lead and permits the lead to proceed. The workflow map itself says every errand is currently optional (`docs/workflow-map.md`, J30 note). Marking a worker required in a test by patching `TaskOutcome.edge` or inventing a field would claim behavior the source cannot produce.

To complete the required half of J30 later, the engine owner would need to define how a task declares a required errand, bind that obligation at dispatch, record its result as an edge, and make completion reject the unsatisfied edge. Only then can a whole journey assert a required worker failure reaches a terminal incomplete outcome. This review does not request or implement that product change.

Verification: `docker run --rm --network none -v /private/tmp/quadratus-j30-worker-journey:/pkg:ro -w /pkg -e PYTHONPATH=/pkg -e PYTHONDONTWRITEBYTECODE=1 sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9 python -m pytest -q -p no:cacheprovider tests/test_workflow_worker_failure_journey.py` passed (1 test). No vendor CLI or network was available to this run.
