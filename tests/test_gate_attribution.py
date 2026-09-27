"""Gate attribution from the harness-owned pytest producer (phase 3, #25).

Real pytest runs. Only a test-body ``AssertionError`` in this invocation's
report, with exit status 1 and a consistent record, is a product failure;
every other failure is unattributable (Codex review 5858008514)."""

import json
import sys

from quadratus.integration import (
    REPORT_TOKEN,
    GateCommand,
    GateSuite,
    IntegrationGate,
    attribute,
    read_report,
)

DECLARED = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--quadratus-report={REPORT_TOKEN}"]


def _gate(tmp_path, body, argv=DECLARED):
    (tmp_path / "test_app.py").write_text(body)
    return IntegrationGate(argv, cwd=tmp_path).run()


def _reasons(result):
    verdict = attribute(result)
    assert verdict["product"] is False
    return " | ".join(verdict["reasons"])


def test_a_plain_assertion_in_a_test_body_is_product(tmp_path):
    result = _gate(tmp_path, "def test_a():\n    assert 1 == 2\n\ndef test_b():\n    pass\n")
    assert result.report["state"] == "parsed" and result.report["exitstatus"] == 1
    assert result.report["failures"] == [dict(nodeid="test_app.py::test_a", when="call",
                                              exc_type="builtins.AssertionError", assertion=True)]
    assert attribute(result) == dict(product=True, reasons=[])
    assert not list(tmp_path.glob("*.json")), "the report lives outside the project"


def test_a_runtime_error_in_a_test_body_is_not_product(tmp_path):
    result = _gate(tmp_path, "def test_a():\n    raise RuntimeError('browser runner could not launch')\n")
    assert "failures that are not assertions: builtins.RuntimeError" in _reasons(result)


def test_mixed_assertion_and_runtime_failures_are_not_product(tmp_path):
    result = _gate(tmp_path, "def test_a():\n    assert 0\n\ndef test_b():\n    raise OSError('no display')\n")
    assert "builtins.OSError" in _reasons(result)


def test_a_setup_error_is_not_product(tmp_path):
    body = ("import pytest\n\n@pytest.fixture\ndef browser():\n    raise RuntimeError('died')\n\n"
            "def test_a(browser):\n    assert browser\n")
    assert "1 setup or teardown error(s)" in _reasons(_gate(tmp_path, body))


def test_an_assertion_raised_in_setup_is_not_product(tmp_path):
    body = ("import pytest\n\n@pytest.fixture\ndef ready():\n    assert False, 'env'\n\n"
            "def test_a(ready):\n    pass\n")
    reasons = _reasons(_gate(tmp_path, body))
    assert "setup or teardown error" in reasons and "not assertions" in reasons


def test_a_collection_error_is_not_product(tmp_path):
    result = _gate(tmp_path, "import no_such_module\n\ndef test_a():\n    assert 0\n")
    assert "collection error" in _reasons(result) or "exit 2" in _reasons(result)


def test_pytest_fail_is_not_an_assertion_though_its_type_claims_builtins(tmp_path):
    result = _gate(tmp_path, "import pytest\n\ndef test_a():\n    pytest.fail('nope')\n")
    assert "builtins.Failed" in _reasons(result)


def test_a_class_named_like_assertion_error_is_not_an_assertion(tmp_path):
    body = ("class AssertionError(Exception):\n    __module__ = 'builtins'\n\n"
            "def test_a():\n    raise AssertionError('spoofed')\n")
    assert "builtins.AssertionError" in _reasons(_gate(tmp_path, body))


def test_an_undeclared_report_is_not_product(tmp_path):
    argv = [a for a in DECLARED if REPORT_TOKEN not in a]
    assert _reasons(_gate(tmp_path, "def test_a():\n    assert 0\n", argv)) == "check: structured report undeclared"


def test_a_runner_killed_before_writing_is_not_product(tmp_path):
    argv = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
            f"--quadratus-report={REPORT_TOKEN}"]
    result = IntegrationGate(argv, cwd=tmp_path).run()
    assert _reasons(result) == "check: structured report missing"


def test_a_non_test_exit_is_not_product(tmp_path):
    argv = [sys.executable, "-c", "import sys; sys.exit(1)", f"--quadratus-report={REPORT_TOKEN}"]
    assert _reasons(IntegrationGate(argv, cwd=tmp_path).run()) == "check: structured report missing"


def test_a_missing_runner_is_not_product(tmp_path):
    result = IntegrationGate(["quadratus-no-such-runner", f"--quadratus-report={REPORT_TOKEN}"], cwd=tmp_path).run()
    assert result.returncode is None and "error" in _reasons(result)


def test_a_suite_is_product_only_when_every_failing_check_is(tmp_path):
    (tmp_path / "test_app.py").write_text("def test_a():\n    assert 1 == 2\n")
    unit = GateCommand("unit", argv=tuple(DECLARED))
    blocked = GateCommand("setup", argv=("quadratus-no-such-runner",))
    mixed = GateSuite([unit, blocked], cwd=tmp_path).run()
    assert _reasons(mixed).startswith("setup: blocked: runner unavailable")
    alone = GateSuite([unit], cwd=tmp_path).run()
    assert attribute(alone)["product"] and alone.receipts[0].report["counts"]["failed"] == 1


def _written(tmp_path, data):
    path = tmp_path / "r.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(path)


def test_malformed_foreign_and_oversized_reports_are_refused(tmp_path):
    good = dict(producer="quadratus-pytest/2", nonce="n1", exitstatus=1, collected=1, collect_errors=0,
                counts=dict(passed=0, failed=1, errors=0, skipped=0),
                failures=[dict(nodeid="t", when="call", exc_type="builtins.AssertionError", assertion=True)],
                truncated=False)
    assert read_report(_written(tmp_path, good), "n1")["state"] == "parsed"
    assert read_report(_written(tmp_path, good), "other")["state"] == "foreign", "another invocation's report"
    assert read_report(_written(tmp_path, {**good, "producer": "x"}), "n1")["state"] == "foreign"
    assert read_report(_written(tmp_path, "{not json"), "n1")["state"] == "unparsable"
    assert read_report(_written(tmp_path, {**good, "counts": {"failed": "1"}}), "n1")["state"] == "unparsable"
    assert read_report(_written(tmp_path, "x" * (1024 * 1024 + 1)), "n1")["state"] == "unparsable"
    assert read_report(str(tmp_path / "absent.json"), "n1") == {"state": "missing"}
    assert read_report(None, None) == {"state": "undeclared"}


def test_an_inconsistent_record_is_not_product(tmp_path):
    from quadratus.integration import GateResult
    report = dict(state="parsed", exitstatus=1, collected=2, collect_errors=0,
                  counts=dict(passed=0, failed=2, errors=0, skipped=0),
                  failures=[dict(nodeid="t", when="call", exc_type="builtins.AssertionError", assertion=True)],
                  truncated=False)
    result = GateResult(False, "check", 1, "", report=report)
    assert "inconsistent or empty failure record" in _reasons(result)
    truncated = GateResult(False, "check", 1, "", report={**report, "truncated": True,
                                                          "counts": dict(report["counts"], failed=1)})
    assert "inconsistent" in _reasons(truncated)
