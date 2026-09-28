# J37 offline coverage proposal

Source assembly: `b773917efe6c7dd931cd3d5157782ce250b9c794`.
The proposed selector is
`tests/lifecycle/test_j37_transport_timeout.py::test_j37_timeout_after_writes_is_inspected_and_never_replayed`.
The new test drives `run_project` through `tests.lifecycle.harness`; only the
vendor CLI launch is scripted. That launch edits two permitted project files
and raises `subprocess.TimeoutExpired`. The real CLI provider converts the
timeout, and the real session records the stop.

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

The `calls` assertion proves one lead invocation, no replay, no second task,
and no integration check after the interrupted draft. `outcome` checks the
incomplete run and the task's `stopped:PartialWorkStopped` / `integrity`
classification. `report` checks the named error, typed run stop, and parity.
`partial_work` checks that both changed files remain and are named by the
controller's inspected partial-work record. These are deterministic routing
claims. They do not prove a live provider timeout, the duration of a real
timeout, browser behavior, or the production frequency of partial writes.

After the mapping is accepted, remove or narrow the manifest limitation that
says J37 has no mapped whole-controller replay. Leave the other limitations
unchanged. This candidate deliberately does not edit the shared manifest.
