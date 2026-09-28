"""A parsed harness report for a plain test-body assertion failure
(integration.read_report). Stub gates that stand for a real failing test
suite carry it so their failure is attributable; a stub without it is an
operator handoff, as a real undeclared check is (phase 3, #25)."""

ASSERTION_FAILURE = dict(
    state="parsed", exitstatus=1, collected=1, collect_errors=0,
    counts=dict(passed=0, failed=1, errors=0, skipped=0),
    failures=[dict(nodeid="tests/test_app.py::test_add", when="call", exc_type="builtins.AssertionError",
                   assertion=True)],
    truncated=False)
