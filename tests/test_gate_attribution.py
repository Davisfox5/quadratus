"""Gate attribution from the harness-owned pytest producer (phase 3, #25).

Real pytest runs. Only a test-body ``AssertionError`` in this invocation's
report, with exit status 1 and a consistent record, is a product failure;
every other failure is unattributable (Codex review 5858008514)."""

import json
import shutil
import sys

import pytest

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
                                              exc_type="builtins.AssertionError", assertion=True,
                                              body=True)]
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


EXPECTED = dict(nonce="n1", module_file="/owned/quadratus_gate_report_ab.py", module_sha256="d" * 64)


def _report(**changes):
    good = dict(producer="quadratus-pytest/4", nonce="n1", module_file=EXPECTED["module_file"],
                module_sha256=EXPECTED["module_sha256"], exitstatus=1, collected=1, collect_errors=0,
                counts=dict(passed=0, failed=1, errors=0, skipped=0),
                failures=[dict(nodeid="t", when="call", exc_type="builtins.AssertionError", assertion=True)],
                truncated=False)
    good.update(changes)
    return good


def _written(tmp_path, data, name="r.json"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return str(path)


def test_a_sound_report_parses_and_foreign_identities_are_refused(tmp_path):
    assert read_report(_written(tmp_path, _report()), EXPECTED)["state"] == "parsed"
    for change in (dict(nonce="other"), dict(producer="x"), dict(module_sha256="e" * 64),
                   dict(module_file="/project/quadratus_gate_report.py")):
        assert read_report(_written(tmp_path, _report(**change)), EXPECTED)["state"] == "foreign", change
    assert read_report(str(tmp_path / "absent.json"), EXPECTED) == {"state": "missing"}
    assert read_report(None, None) == {"state": "undeclared"}


def test_impossible_or_mistyped_reports_are_malformed_never_product(tmp_path):
    """Codex controls: collected=0, negative counts, booleans for integers."""
    bad = [dict(collected=0), dict(counts=dict(passed=-99, failed=1, errors=0, skipped=0)),
           dict(collected=True), dict(counts=dict(passed=0, failed=True, errors=0, skipped=0)),
           dict(exitstatus=True), dict(exitstatus=9), dict(collect_errors=-1), dict(truncated=1),
           dict(counts=dict(passed=0, failed=1, errors=0)),
           dict(counts=dict(passed=5, failed=1, errors=0, skipped=0)),
           dict(failures=[dict(nodeid="t", when="call", exc_type="builtins.AssertionError", assertion=1)]),
           dict(failures=[dict(nodeid="t", when="later", exc_type="x", assertion=True)]),
           dict(failures=[]), dict(truncated=True)]
    for change in bad:
        state = read_report(_written(tmp_path, _report(**change)), EXPECTED)
        assert state["state"] == "unparsable", (change, state)


def test_unreadable_report_files_are_refused_within_a_deadline(tmp_path):
    """Codex controls: a FIFO, a symlink, a file replaced or grown after
    stat, and deep nesting; each under an external deadline."""
    import os
    import threading

    def bounded(path):
        box = {}
        worker = threading.Thread(target=lambda: box.update(r=read_report(path, EXPECTED)), daemon=True)
        worker.start()
        worker.join(5)
        assert "r" in box, f"read_report blocked on {path}"
        return box["r"]

    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    assert bounded(str(fifo)) == {"state": "unparsable", "detail": "not a regular file"}
    target = _written(tmp_path, _report(), "target.json")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert bounded(str(link))["state"] == "unparsable"
    assert bounded(_written(tmp_path, "[" * 200000 + "]" * 200000, "deep.json"))["state"] == "unparsable"
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1024 * 1024 + 1))
    assert bounded(str(big)) == {"state": "unparsable", "detail": f"more than {1024 * 1024} bytes"}


def test_a_file_replaced_or_grown_between_stat_and_read_is_bounded(tmp_path, monkeypatch):
    import os

    from quadratus import integration
    path = _written(tmp_path, _report(), "swap.json")
    real_open = os.open

    def swap_then_open(p, flags, *a):
        os.replace(_written(tmp_path, _report(), "other.json"), p)
        return real_open(p, flags, *a)
    monkeypatch.setattr(integration.os, "open", swap_then_open)
    assert read_report(path, EXPECTED) == {"state": "unparsable", "detail": "replaced while being opened"}
    monkeypatch.setattr(integration.os, "open", real_open)

    def grow_then_open(p, flags, *a):
        with open(p, "ab") as out:
            out.write(b" " * (1024 * 1024 + 1))
        return real_open(p, flags, *a)
    grown = _written(tmp_path, _report(), "grow.json")
    monkeypatch.setattr(integration.os, "open", grow_then_open)
    assert read_report(grown, EXPECTED)["detail"] == f"more than {1024 * 1024} bytes"


def test_an_inconsistent_record_is_not_product(tmp_path):
    from quadratus.integration import GateResult
    report = dict(state="parsed", exitstatus=1, collected=2, collect_errors=0,
                  counts=dict(passed=0, failed=2, errors=0, skipped=0),
                  failures=[dict(nodeid="t", when="call", exc_type="builtins.AssertionError", assertion=True)],
                  truncated=False)
    assert "inconsistent" in _reasons(GateResult(False, "check", 1, "", report=report))
    assert "inconsistent" in _reasons(GateResult(False, "check", 1, "", report={**report, "collected": 0}))
    truncated = {**report, "truncated": True, "counts": dict(report["counts"], failed=1)}
    assert "inconsistent" in _reasons(GateResult(False, "check", 1, "", report=truncated))


# -- shadowing and partial runs (Codex review 5858490122) --------------------------------

SHADOW = """import json, os
import pytest


def pytest_addoption(parser):
    parser.addoption("--quadratus-report", action="store", default=None)


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--quadratus-report")
    json.dump(dict(producer="quadratus-pytest/4", nonce=os.environ.get("QUADRATUS_GATE_NONCE", ""),
                   module_file=os.path.realpath(__file__), module_sha256="0" * 64, exitstatus=1,
                   collected=1, collect_errors=0, counts=dict(passed=0, failed=1, errors=0, skipped=0),
                   failures=[dict(nodeid="x", when="call", exc_type="builtins.AssertionError",
                                  assertion=True)], truncated=False), open(path, "w"))
"""


def test_a_project_local_producer_cannot_shadow_the_harness_one(tmp_path):
    """Codex control: a project module named like the producer, writing an
    assertion report with the nonce from its environment, while the real
    test raises RuntimeError. It is never loaded as the producer, and a
    report claiming another module is foreign."""
    (tmp_path / "quadratus_gate_report.py").write_text(SHADOW)
    result = _gate(tmp_path, "def test_a():\n    raise RuntimeError('browser runner could not launch')\n")
    assert result.report["state"] == "parsed", "the harness producer, not the shadow, wrote the report"
    assert "failures that are not assertions: builtins.RuntimeError" in _reasons(result)


def test_a_conftest_that_registers_a_shadow_producer_is_foreign(tmp_path):
    (tmp_path / "conftest.py").write_text("pytest_plugins = ['shadow_producer']\n")
    (tmp_path / "shadow_producer.py").write_text(SHADOW.replace(
        'def pytest_addoption(parser):\n    parser.addoption("--quadratus-report", action="store", default=None)\n',
        "").replace("def pytest_sessionfinish(", "@pytest.hookimpl(trylast=True)\ndef pytest_sessionfinish("))
    result = _gate(tmp_path, "def test_a():\n    raise RuntimeError('browser runner could not launch')\n")
    assert result.report["state"] == "foreign" and "structured report foreign" in _reasons(result)


def test_partial_runs_under_maxfail_stay_accurate(tmp_path):
    body = "".join(f"def test_{n}():\n    assert {n} == 0\n\n" for n in range(1, 4))
    stop_first = _gate(tmp_path, body, DECLARED + ["-x"])
    assert stop_first.report["collected"] == 3 and stop_first.report["counts"]["failed"] == 1
    assert attribute(stop_first)["product"] is True
    two = _gate(tmp_path, body, DECLARED + ["--maxfail=2"])
    assert two.report["counts"]["failed"] == 2 and attribute(two)["product"] is True
    mixed = _gate(tmp_path, body.replace("assert 2 == 0", "raise OSError('display')"), DECLARED + ["--maxfail=2"])
    assert "builtins.OSError" in _reasons(mixed)


# --- node:test producer (series rule-58a4625 f3) ---------------------------

_NODE = shutil.which("node")
NODE_DECLARED = ["node", "--test", "a.test.mjs", "--test-reporter-destination={report}"]


def _node_gate(tmp_path, body, argv=NODE_DECLARED, name="a.test.mjs"):
    (tmp_path / name).write_text(body)
    return IntegrationGate(argv, cwd=tmp_path).run()


@pytest.mark.skipif(not _NODE, reason="needs node")
def test_a_node_assertion_failure_is_product_and_the_tap_count_survives(tmp_path):
    body = ("import test from 'node:test'; import assert from 'node:assert';\n"
            "test('ok', () => {});\ntest('bad', () => { assert.strictEqual(1, 2); });\n")
    result = _node_gate(tmp_path, body)
    assert result.returncode == 1 and result.report["state"] == "parsed", result.report
    assert result.report["failures"] == [dict(nodeid=result.report["failures"][0]["nodeid"], when="call",
                                              exc_type="AssertionError", assertion=True, body=None)]
    assert result.report["counts"] == dict(passed=1, failed=1, errors=0, skipped=0)
    assert attribute(result) == dict(product=True, reasons=[])
    assert "# pass 1" in result.output and "# fail 1" in result.output, "TAP stays on stdout for the count"
    assert not list(tmp_path.glob("*.json")), "the report lives outside the project"


@pytest.mark.skipif(not _NODE, reason="needs node")
def test_a_node_runtime_error_is_not_an_assertion(tmp_path):
    body = "import test from 'node:test';\ntest('bad', () => { null.querySelector('x'); });\n"
    result = _node_gate(tmp_path, body)
    assert "TypeError" in _reasons(result) and "not assertions" in _reasons(result)


@pytest.mark.skipif(not _NODE, reason="needs node")
def test_a_node_hook_failure_and_a_cancelled_subtest_are_errors_not_product(tmp_path):
    body = ("import { describe, it, before } from 'node:test';\n"
            "describe('s', () => { before(() => { throw new Error('boom'); }); it('inner', () => {}); });\n")
    result = _node_gate(tmp_path, body)
    assert result.report["state"] == "parsed" and result.report["counts"]["errors"] == 2
    assert "setup or teardown error" in _reasons(result)


@pytest.mark.skipif(not _NODE, reason="needs node")
def test_a_node_file_that_fails_to_load_is_a_collection_error(tmp_path):
    result = _node_gate(tmp_path, "throw new Error('load boom');\n")
    assert result.report["state"] == "parsed" and result.report["collect_errors"] == 1
    assert "collection error" in _reasons(result)


@pytest.mark.skipif(not _NODE, reason="needs node")
def test_an_undeclared_node_check_is_still_unattributable(tmp_path):
    body = "import test from 'node:test'; import assert from 'node:assert';\ntest('bad', () => { assert.ok(false); });\n"
    result = _node_gate(tmp_path, body, argv=["node", "--test", "a.test.mjs"])
    assert _reasons(result) == "check: structured report undeclared"


def test_the_model_facing_check_carries_no_report_declaration():
    from quadratus.integration import model_facing
    assert model_facing(DECLARED) == DECLARED[:-1]
    assert model_facing(NODE_DECLARED) == NODE_DECLARED[:-1]
    assert model_facing(["python", "-m", "pytest", "-q"]) == ["python", "-m", "pytest", "-q"]


@pytest.mark.skipif(not _NODE, reason="needs node")
@pytest.mark.parametrize("body", [
    "import { describe, it } from 'node:test'; import assert from 'node:assert';\n"
    "describe('outer', () => { it('leaf', () => { assert.equal(1, 2); }); });\n",
    "import test from 'node:test'; import assert from 'node:assert';\n"
    "test('parent', async (t) => { await t.test('child', () => { assert.equal(1, 2); }); });\n",
])
def test_a_nested_node_assertion_is_product_and_the_parent_aggregate_is_not_an_error(tmp_path, body):
    """Codex review of f8d8c03: the parent's subtestsFailed aggregate read as
    a setup error and denied the repair an ordinary nested suite earns."""
    result = _node_gate(tmp_path, body)
    assert result.report["state"] == "parsed", result.report
    assert result.report["counts"] == dict(passed=0, failed=1, errors=0, skipped=0)
    assert [f["assertion"] for f in result.report["failures"]] == [True]
    assert attribute(result) == dict(product=True, reasons=[])


@pytest.mark.skipif(not _NODE, reason="needs node")
def test_a_nested_node_runtime_error_is_still_not_product(tmp_path):
    body = ("import test from 'node:test';\n"
            "test('parent', async (t) => { await t.test('child', () => { null.x(); }); });\n")
    result = _node_gate(tmp_path, body)
    assert result.report["counts"]["errors"] == 0 and "TypeError" in _reasons(result)


# -- provenance: only an assertion raised through the test function's frame counts --------

def test_a_call_hook_assertion_with_the_body_unrun_is_not_product(tmp_path):
    """Codex attribution assessment on 4a273a3: a tryfirst pytest_runtest_call
    hook raised AssertionError before the body ran; pytest reports it in the
    call phase, and it was admitted as a product failure."""
    (tmp_path / "conftest.py").write_text(
        "import pytest\n\n@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_runtest_call(item):\n    assert False, 'runner environment not ready'\n")
    result = _gate(tmp_path, "def test_a():\n    pass\n")
    verdict = attribute(result)
    assert verdict["product"] is False
    assert "builtins.AssertionError raised outside the test body" in _reasons(result)


def test_a_pyfunc_hook_assertion_is_not_product(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "import pytest\n\n@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_pyfunc_call(pyfuncitem):\n    assert False, 'plugin refused'\n")
    assert attribute(_gate(tmp_path, "def test_a():\n    pass\n"))["product"] is False


@pytest.mark.parametrize("body", [
    # A helper the body calls: the body's frame is on the traceback.
    "def check(x):\n    assert x == 2\n\ndef test_a():\n    check(1)\n",
    # pytest.raises(match=) fails with an AssertionError inside the body.
    "import pytest\n\ndef test_a():\n    with pytest.raises(ValueError, match='right'):\n"
    "        raise ValueError('wrong')\n",
    # A method in a test class.
    "class TestThing:\n    def test_a(self):\n        assert 1 == 2\n",
    # A decorated test: unwrapped to the function the body is.
    "import functools\n\ndef deco(f):\n    @functools.wraps(f)\n    def wrap(*a, **k):\n"
    "        return f(*a, **k)\n    return wrap\n\n@deco\ndef test_a():\n    assert 1 == 2\n",
    # A parametrized test.
    "import pytest\n\n@pytest.mark.parametrize('n', [1])\ndef test_a(n):\n    assert n == 2\n",
])
def test_assertions_raised_through_the_body_stay_product(tmp_path, body):
    result = _gate(tmp_path, body)
    assert attribute(result)["product"] is True, _reasons(result)


def test_a_report_without_the_body_field_still_parses(tmp_path):
    """The body field is optional in the schema; an assertion still decides."""
    from quadratus.integration import _malformed
    record = dict(producer="quadratus-pytest/4", exitstatus=1, collected=1, collect_errors=0,
                  counts=dict(passed=0, failed=1, errors=0, skipped=0), truncated=False,
                  failures=[dict(nodeid="t::a", when="call", exc_type="builtins.AssertionError", assertion=True)])
    assert _malformed(record) == ""
    record["failures"][0]["body"] = "yes"
    assert _malformed(record) == "a failure record"
