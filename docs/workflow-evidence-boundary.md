# Evidence and operator boundaries: candidate controls (#25, "Opus evidence")

Originally prepared on `9eabf69`; aligned in the isolated offline assembly
based on `7d294ce`. Tests only: `tests/test_workflow_evidence_boundary.py`.
No engine change, runtime wiring, live run or vendor call.

## TL;DR

- **19 whole-controller controls.** The historical E1/E2 expectations were
  aligned with ruling #25/5862699144 and the independently reviewed E2 fix
  `bbb4956`; the combined assembly result is recorded in its composition
  manifest.
- **Covered and holding:**
  - A render set the reviewer didn't fully receive is never reviewed and
    never approved. That holds whether a file was dropped, its bytes were
    swapped after the snapshot, or it changed while being prepared.
  - Only an exact `APPROVED` satisfies the reviewer. Eight near-misses stay
    unverified.
  - The recorded delivery digests are the bytes the reviewer read and the
    bytes the project keeps.
  - When the runner's environment breaks after a design-fix, gate-fix or
    security-fix, there's no repair call. The run hands off to the operator.
- **E1 attribution limit:** a preview exiting 1 without structured proof of
  its origin remains `invalid_proof` and incomplete. It does not authorize an
  application repair. Proven launch or harness failures can be operator
  failures; this fixture alone does not establish that provenance.
- **E2 corrected route:** a later task can overwrite an approved
  non-RESOLVES task's renders; `bbb4956` makes the run incomplete. The
  historical base completed in this case. The test asserts non-completion
  without imposing an integrity classification.
- **Validation:** The combined offline assembly suite passed; the wheel test
  skipped because no offline builder interpreter was configured. Ruff is not
  installed in the frozen image.

## Method

Every case runs `run_project` through `tests/lifecycle/harness.py`. Only
`cli_providers._launch` is faked, and the harness asserts typed-outcome
parity and completeness on every replay. Faults are injected in the test's
environment or by wrapping a copy or snapshot step:

- files in the project or evidence folder;
- `PYTEST_ADDOPTS`, which the check process inherits;
- a preview argv;
- the `_furnish_evidence` and `_capture_state` wrappers.

No decision is patched. Scripted replies prove routing, not model judgement.

## Obligation → control → expected on the isolated assembly

| Obligation | Control | Observed |
| --- | --- | --- |
| Delivery: a partial copy is not a delivery (typed) | `test_a_copy_that_drops_a_file_leaves_delivery_and_reviewer_unsatisfied` | pass: no review call; `delivered` False, `reviewer` not True, `delivery` None, both in `unsatisfied`; active `unverified`; `FindingsOpen` |
| Identity: bytes swapped between the snapshot and the Fleet copy are not delivered | `test_renders_rewritten_between_the_snapshot_and_the_copy_are_not_delivered` | pass: `EvidenceNotDelivered` names the file, no review call, the changed render is left as found |
| Identity: renders that change between hashing and snapshot | `test_renders_changed_while_being_prepared_for_review_are_not_delivered` | pass: "changed while they were being prepared for review", no copy, no call |
| Acknowledgment: only an exact `APPROVED` | `test_anything_but_an_exact_approval_...` (8 replies) | pass: one review call, never re-asked; `delivered` True, `reviewer` False, incomplete |
| Acknowledgment: positive boundary | `test_an_exact_approval_with_surrounding_whitespace_is_an_approval` | pass: `reviewer` True, `unsatisfied` empty, completed |
| Identity: approved = delivered = kept | `test_the_recorded_delivery_is_the_bytes_the_reviewer_read_and_the_bytes_kept` | pass: `delivery.files` sha256 equals the reviewer copy and the project, for all 3 mandatory files; the reviewer is another vendor |
| Replaced after approval (E2) | `test_an_approved_tasks_renders_changed_by_a_later_task_do_not_complete` | pass: the digests diverge from the record and the run remains incomplete; historical `9eabf69` completed incorrectly |
| Operator: runner lost after the design-fix | `test_a_runner_lost_after_the_design_fix_gets_no_gate_fix_and_no_review` | pass: `CheckUnattributable` (operator), 0 gate-fix, 1 design-fix, no design review, work kept |
| Operator: runner lost after an attributable failure's gate-fix | `test_a_runner_lost_after_a_gate_fix_is_the_operators_not_a_product_failure` | pass: 1 gate-fix; first attempt `product` True, last False; primary `operator`; app unchanged |
| Operator: runner lost after a security-fix | `test_a_runner_lost_after_a_security_fix_gets_no_further_repair_or_verdict` | pass: 1 security-fix, 1 verifier call, 0 gate-fix, `CheckUnattributable` |
| Operator: preview can't start, no repair | `test_a_preview_that_never_starts_gets_no_repair_call` | pass: no design-fix, gate-fix or review; lead's edit kept; `DesignUnverified: task t1 ... exited with 1 before it was ready` |
| Preview exits 1 without proven attribution (E1) | `test_a_preview_exit_without_proven_origin_remains_invalid_proof` | pass: active `invalid_proof`, incomplete, no repair call; exit status alone does not establish operator provenance |

## Deliberately not duplicated (existing coverage)

- J9b symlink, fixture changed after capture, harness capture on another
  source, stale self-capture, and missing renders:
  `tests/lifecycle/test_evidence_identity.py`.
- J10 approved and BLOCKING edges, and never-verified evidence with no
  delivery: `tests/lifecycle/test_workflow_contract.py`.
- Preflight refusal, aggregate budget, verdict text on a dropped copy, and
  cross-task evidence: `tests/lifecycle/test_lifecycle_matrix.py`.
- J4, J5, J6 and J33 on the first gate:
  `tests/lifecycle/test_workflow_outcomes.py`.
- J7 readiness: `test_workflow_contract.py`.
- Denied or unusable capture and a preview that writes source:
  `tests/lifecycle/test_harness_capture.py`.
- Settlement mismatch on RESOLVES tasks: `test_audit_findings.py` and
  related tests.

## Classification limits and the incumbent's E2 correction

- **E1.** `_check_design` → `_harness_capture` can return a
  `PreviewFailed` string and stop as `DesignUnverified` with `invalid_proof`.
  A process exit/timeout or prose detail alone cannot prove an operator
  fault. Structured launch or harness provenance is required before using
  the operator route. The present ambiguous fixture is a negative control;
  it preserves the existing no-repair boundary.
- **E2.** The historical `9eabf69` path settled approved evidence only for
  RESOLVES tasks. The base now includes reviewed `bbb4956`, which checks
  non-RESOLVES evidence at final settlement too.
  - Contract (Codex, 5862294492): final required evidence that differs from
    the reviewer-approved delivery must not complete the run.
  - A later mismatch is unverified pending fresh qualified evidence and
    review, consistent with the later-audit behaviour. It is not assumed to
    be integrity; the settlement-time J9b integrity boundary is unchanged.
  - No new retry authority and no automatic edits.
  - The passing control asserts non-completion only.

## Validation boundary

- **Run:** see the isolated assembly composition manifest for the combined
  frozen-image, network-disabled validation: 351 passed, 11 skipped. The
  historical 18 passed/2 xfailed result applies only to the old base and
  old expectations. The installed wheel check was among the skips because
  `QUADRATUS_WHEEL_BUILDER` was unset. Ruff is unavailable in this image.
