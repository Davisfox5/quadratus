"""The gate's heading names what failed (contract v2 B on #25).

Run 19: "Integration gate FAILED: check, extra1, extra2" sent a fix call
after checks that had passed. A mixed result names the failed checks and
lists the passed ones apart; all-failed, all-passed and a single command are
unchanged.
"""

from quadratus.integration import GateReceipt, GateResult


def _result(*statuses, passed=None):
    receipts = tuple(GateReceipt(id=name, status=status, reason=status, required=True)
                     for name, status in statuses)
    ok = all(s == "passed" for _, s in statuses) if passed is None else passed
    return GateResult(ok, "gate suite", 0 if ok else 1, "tail", receipts)


def _heading(result):
    return result.for_models().splitlines()[0]


def test_one_of_three_failing_names_only_it():
    result = _result(("check", "passed"), ("extra1", "passed"), ("extra2", "failed"))
    assert _heading(result) == "Integration gate FAILED: extra2 (passed: check, extra1)"


def test_blocked_and_errored_count_as_failed_and_skipped_is_neither():
    result = _result(("check", "passed"), ("extra1", "error"), ("extra2", "blocked"), ("extra3", "skipped"))
    assert _heading(result) == "Integration gate FAILED: extra1, extra2 (passed: check)"


def test_all_failing_is_unchanged():
    result = _result(("check", "failed"), ("extra1", "failed"))
    assert _heading(result) == "Integration gate FAILED: check, extra1"


def test_all_passing_is_unchanged():
    assert _heading(_result(("check", "passed"), ("extra1", "passed"))) == "Integration gate PASSED: check, extra1"


def test_a_single_command_gate_is_unchanged():
    assert _heading(_result(("check", "failed"))) == "Integration gate FAILED: check"
    assert _heading(GateResult(False, "pytest -q", 1, "tail")) == "Integration gate FAILED: project check"


def test_the_per_check_lines_and_the_tail_are_unchanged():
    lines = _result(("check", "passed"), ("extra2", "failed")).for_models().splitlines()
    assert lines[1:] == ["check: passed: passed", "extra2: failed: failed", "tail"]
