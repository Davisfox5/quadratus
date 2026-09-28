# Evidence and operator boundaries: candidate controls (#25, "Opus evidence")

Base `9eabf69`. Tests only: `tests/test_workflow_evidence_boundary.py`. No
engine change, no runtime wiring, no live or vendor call.

## TL;DR

- **19 whole-controller controls.** 18 pass on the base, and 1 is a strict
  xfail for a real gap.
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
- **Gap E1 (strict xfail):** when the harness preview can't start (the
  operator's environment), the run stops as `DesignUnverified` with
  `invalid_proof`. The plan routes lost capability to an operator handoff.
  No repair call is made today, so this is a classification gap, not a
  repair leak.
- **Gap E2 (question, not an xfail):** a later task can overwrite an
  approved task's renders, and the run still completes. The record keeps the
  approved digest, so the change can be detected offline. Whether DONE must
  refuse it is a contract decision for the incumbent and Codex.
- **Not validated:** Ruff isn't installed on the host or in the frozen
  image. The tests were run in the frozen image with no network access.

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

## Obligation → control → observed on 9eabf69

| Obligation | Control | Observed |
| --- | --- | --- |
| Delivery: a partial copy is not a delivery (typed) | `test_a_copy_that_drops_a_file_leaves_delivery_and_reviewer_unsatisfied` | pass: no review call; `delivered` False, `reviewer` not True, `delivery` None, both in `unsatisfied`; active `unverified`; `FindingsOpen` |
| Identity: bytes swapped between the snapshot and the Fleet copy are not delivered | `test_renders_rewritten_between_the_snapshot_and_the_copy_are_not_delivered` | pass: `EvidenceNotDelivered` names the file, no review call, the changed render is left as found |
| Identity: renders that change between hashing and snapshot | `test_renders_changed_while_being_prepared_for_review_are_not_delivered` | pass: "changed while they were being prepared for review", no copy, no call |
| Acknowledgment: only an exact `APPROVED` | `test_anything_but_an_exact_approval_...` (8 replies) | pass: one review call, never re-asked; `delivered` True, `reviewer` False, incomplete |
| Acknowledgment: positive boundary | `test_an_exact_approval_with_surrounding_whitespace_is_an_approval` | pass: `reviewer` True, `unsatisfied` empty, completed |
| Identity: approved = delivered = kept | `test_the_recorded_delivery_is_the_bytes_the_reviewer_read_and_the_bytes_kept` | pass: `delivery.files` sha256 equals the reviewer copy and the project, for all 3 mandatory files; the reviewer is another vendor |
| Tampered after approval (E2) | `test_an_approved_tasks_renders_changed_by_a_later_task_diverge_from_the_record` | pass, **pins today's route**: the digests diverge from the record, and the run **completes** |
| Operator: runner lost after the design-fix | `test_a_runner_lost_after_the_design_fix_gets_no_gate_fix_and_no_review` | pass: `CheckUnattributable` (operator), 0 gate-fix, 1 design-fix, no design review, work kept |
| Operator: runner lost after an attributable failure's gate-fix | `test_a_runner_lost_after_a_gate_fix_is_the_operators_not_a_product_failure` | pass: 1 gate-fix; first attempt `product` True, last False; primary `operator`; app unchanged |
| Operator: runner lost after a security-fix | `test_a_runner_lost_after_a_security_fix_gets_no_further_repair_or_verdict` | pass: 1 security-fix, 1 verifier call, 0 gate-fix, `CheckUnattributable` |
| Operator: preview can't start, no repair | `test_a_preview_that_never_starts_gets_no_repair_call` | pass: no design-fix, gate-fix or review; lead's edit kept; `DesignUnverified: task t1 ... exited with 1 before it was ready` |
| Operator: preview can't start is an operator handoff (E1) | `test_a_preview_that_never_starts_is_an_operator_handoff` | **strict xfail**, `raises=AssertionError`; observed `primary == 'invalid_proof'`, active `['invalid_proof']` |

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

## Gaps for the incumbent (session.py owner)

- **E1.** Transition: `_check_design` → `_harness_capture` returns a
  `PreviewFailed` string → `_open_finding("invalid_proof", …)` and
  `_design_unverified`, and the run stops as `DesignUnverified`.
  - Expected by the plan ("Failure routing": capability lost mid-run →
    operator handoff; map J8 allows a recapture only when capability is
    fine): a typed `operator` primary and stop, with no repair and no
    recapture.
  - Today's no-repair behaviour is already correct and is pinned by the
    passing control.
  - Separating a preview or capture-tool failure from a render that shows a
    problem needs a structured signal from `preview.capture_task`, which
    today returns free text. That makes it an engine decision, not a test
    change.
- **E2.** `_settle_resolution` re-checks approved digests only for RESOLVES
  tasks. For other design tasks, nothing after the review compares disk
  against `TaskOutcome.delivery.files`, and evidence is excluded from
  CHANGED.
  - Proposed contract, not asserted: at DONE, an approved delivery whose
    digests no longer match disk is an `integrity` fact
    (`EvidenceIdentityMismatch`).
  - The control pins today's completion so a change is deliberate.

## Validation boundary

- **Run:** frozen image `sha256:707363c1…dc0d9`, `--network none`, source
  mounted read-only at `/pkg`:
  `python -m pytest tests/test_workflow_evidence_boundary.py -q -p no:cacheprovider`
  gives 18 passed and 1 xfailed.
- **E1 detail:** the `--runxfail` run shows
  `AssertionError: ('invalid_proof', ['invalid_proof'])`.
- **Blocked:** Ruff isn't installed in the image (`No module named ruff`) or
  on the host (`command not found`).
- **Not run:** the full suite, and old-base reds. These are new controls on
  existing routes, and none claims a changed route.
