# J37 offline coverage proposal

Source assembly: `b773917efe6c7dd931cd3d5157782ce250b9c794`.
The proposed selector is
`tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed`.
The new test drives `run_project` through `tests.lifecycle.harness`. The
harness scripts the vendor CLI launch, provider availability, executable
discovery (`shutil.which`), and lead command capability (`lead_can_run`).
The scripted launch edits two permitted project files and raises
`subprocess.TimeoutExpired`. The real CLI provider converts the timeout,
and the real session records the stop.

Proposed `docs/workflow-acceptance-manifest.json` J37 mapping, for the assembly
owner to apply after independent review:

```json
{
  "id": "J37",
  "tests": ["tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed"],
  "required_obligations": ["calls", "outcome", "report", "partial_work"],
  "coverage": {
    "calls": ["tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed"],
    "outcome": ["tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed"],
    "report": ["tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed"],
    "partial_work": ["tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed"]
  }
}
```

The `calls` assertion observes one lead invocation and no second task. In
this route, the budget's `unknown_usage` latch can stop a broken provider
retry or continued task before those counts change; the independent review
found the counts turn red when that latch is also removed. The check-result
assertion observes no integration check, but has no independent mutation
control because the stop prevents the gate from running. `outcome` checks the
incomplete run and the task's `stopped:PartialWorkStopped` / `integrity`
classification. `report` checks the named error, typed run stop, parity, and
the actual human report's preserved-work section naming both files; it also
checks that the saved `report.md` equals `replay.result.report`. A scratch-only
control that removed the report's per-file bullets made this assertion fail;
the candidate engine was not changed.
`partial_work` checks that both changed files remain and are named by the
controller's inspected partial-work record. These are deterministic routing
claims. They do not prove a live provider timeout, the duration of a real
timeout, browser behavior, or the production frequency of partial writes.

After the mapping is accepted, remove or narrow the manifest limitation that
says J37 has no mapped whole-controller replay. Leave the other limitations
unchanged. This candidate deliberately does not edit the shared manifest.
