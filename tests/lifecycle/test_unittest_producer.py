"""R21 on 319385322b: the final check failed on a plain test-body assertion
and the run stopped CheckUnattributable with budget left, for two reasons.
pytest 9 reports each subtest through the same hook as its item, so the
pytest producer counted seven passing subtests as seven more passed items
than were collected and the strict validator refused the report; and the
required ``python -m unittest discover`` check had no report of its own.

The pytest producer now counts a subtest as part of its item. A unittest
check that declares ``--quadratus-report={report}`` runs the same arguments
under the same interpreter through the shipped runner module and writes its
own nonce-bound report. Undeclared, it stays unattributable; no sibling
check ever stands in for another. Real stdlib unittest and pytest 9 run
here, no model.
"""

import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from quadratus.integration import (
    PRODUCER,
    PRODUCER_UNITTEST,
    GateCommand,
    GateSuite,
    _is_unittest,
    _report_slot,
    attribute,
    model_facing,
    read_report,
)
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

PY = sys.executable
TOKEN = "--quadratus-report={report}"
UNITTEST = (PY, "-m", "unittest", "discover", "-s", "tests")
PYTEST = (PY, "-m", "pytest", "-q", "tests")


def _project(tmp_path, **files):
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    for name, text in files.items():
        (root / "tests" / f"{name}.py").write_text(textwrap.dedent(text))
    return root


def _run(root, *commands):
    return GateSuite([GateCommand(f"c{i}", argv=tuple(argv)) for i, argv in enumerate(commands)], cwd=root).run()


def _times(text):
    return re.sub(r"in \d+\.\d+s", "in Ns", text)


MIXED = """
import unittest

class Mix(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)
    def test_body_assert(self):
        self.assertEqual(1, 2)
    def test_body_error(self):
        raise RuntimeError("boom")
    def test_subtests_pass(self):
        for v in (1, 2):
            with self.subTest(v=v):
                self.assertTrue(v)
    def test_subtest_fail(self):
        for v in (0, 1):
            with self.subTest(v=v):
                self.assertTrue(v)
    def test_subtest_skip(self):
        with self.subTest(v=1):
            self.skipTest("no")
        self.assertTrue(True)
    @unittest.skip("x")
    def test_skipped(self):
        pass
    @unittest.expectedFailure
    def test_xfail(self):
        self.assertEqual(1, 2)
    @unittest.expectedFailure
    def test_unexpected(self):
        self.assertTrue(True)

class Fixture(unittest.TestCase):
    def setUp(self):
        assert False, "setup"
    def test_x(self):
        pass

class ClassFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise ValueError("cls")
    def test_y(self):
        pass
"""


def test_the_unittest_producer_keeps_unittests_own_output_exit_and_records_every_case(tmp_path):
    root = _project(tmp_path, test_mix=MIXED, test_broken="import nonexistent_module_xyz\n")
    plain = subprocess.run(list(UNITTEST), cwd=root, capture_output=True, text=True)
    result = _run(root, UNITTEST + (TOKEN,))
    (receipt,) = result.receipts
    assert receipt.returncode == plain.returncode == 1
    tail = _times((plain.stdout + plain.stderr).strip())[-1500:]
    assert _times(receipt.output.strip()).endswith(tail), "unittest's own output"
    report = receipt.report
    assert report["state"] == "parsed", report
    assert (report["exitstatus"], report["collected"], report["collect_errors"]) == (1, 11, 1)
    assert report["counts"] == dict(passed=2, failed=5, errors=1, skipped=3), "a skipped subcase skips its test"
    by = {f["nodeid"]: (f["when"], f["exc_type"], f["assertion"], f["body"]) for f in report["failures"]}
    assert by == {
        "setUpClass (test_mix.ClassFixture)": ("setup", "builtins.ValueError", False, False),
        "test_mix.Fixture.test_x": ("call", "builtins.AssertionError", False, False),
        "test_mix.Mix.test_body_assert": ("call", "builtins.AssertionError", True, True),
        "test_mix.Mix.test_body_error": ("call", "builtins.RuntimeError", False, True),
        "test_mix.Mix.test_subtest_fail": ("call", "builtins.AssertionError", True, True),
        "test_mix.Mix.test_unexpected": ("call", "", False, False),
    }
    verdict = attribute(result)
    assert not verdict["product"], "collection, fixture, setUp, error and unexpected success never repair"


@pytest.mark.parametrize("body,product", [
    ("self.assertEqual(1, 2)", True),
    ("with self.subTest(v=0):\n                    self.assertTrue(0)", True),
    ("raise RuntimeError('x')", False),
    ("with self.subTest(v=0):\n                    raise RuntimeError('x')", False),
])
def test_only_body_assertions_reach_a_repair_through_unittests_own_report(tmp_path, body, product):
    root = _project(tmp_path, test_one=f"""
        import unittest
        class One(unittest.TestCase):
            def test_ok(self):
                self.assertTrue(True)
            def test_case(self):
                {body}
        """)
    verdict = attribute(_run(root, UNITTEST + (TOKEN,)))
    assert verdict["product"] is product, verdict


def test_an_undeclared_unittest_check_stays_unattributable(tmp_path):
    root = _project(tmp_path, test_one="""
        import unittest
        class One(unittest.TestCase):
            def test_case(self):
                self.assertEqual(1, 2)
        """)
    verdict = attribute(_run(root, UNITTEST, PYTEST + (TOKEN,)))
    assert verdict == dict(product=False, reasons=["c0: structured report undeclared"])
    assert attribute(_run(root, UNITTEST + (TOKEN,), PYTEST + (TOKEN,)))["product"] is True


def test_reports_fail_closed_on_missing_foreign_stale_or_other_runners(tmp_path):
    forge = """
        import os, unittest
        class One(unittest.TestCase):
            def test_case(self):
                os.environ["QUADRATUS_GATE_NONCE"] = "stale"
                self.assertEqual(1, 2)
        """
    root = _project(tmp_path, test_one=forge)
    (receipt,) = _run(root, UNITTEST + (TOKEN,)).receipts
    assert receipt.report["state"] == "foreign", "a report under another nonce is refused"
    usage = _run(root, (PY, "-m", "unittest", "discover", "-s", "missing_dir", TOKEN)).receipts[0]
    assert usage.report["state"] == "missing", "nothing ran: no report"
    for argv in ((PY, "tests/test_one.py", TOKEN), ("coverage", "run", "-m", "unittest", TOKEN)):
        assert not _is_unittest(list(argv)), argv
    assert _is_unittest([PY, "-m", "unittest", "discover"]) and _is_unittest(["python3.9", "-m", "unittest"])


def test_the_slot_binds_the_unittest_producer_and_the_model_sees_the_original_command(tmp_path):
    argv = list(UNITTEST + (TOKEN,))
    assert model_facing(argv) == list(UNITTEST)
    with _report_slot(argv, {"PYTHONPATH": "/project/lib"}) as (run_argv, env, path, expected):
        assert run_argv[0] == PY and run_argv[1] == "-m" and run_argv[2].startswith("quadratus_unittest_report_")
        assert run_argv[3:] == list(UNITTEST[3:]), "the original unittest arguments, unchanged"
        assert expected["producer"] == PRODUCER_UNITTEST and env["QUADRATUS_GATE_REPORT"] == path
        assert env["PYTHONPATH"].endswith("/project/lib"), "the project's own path is kept"
        Path(path).write_text(json.dumps(dict(producer=PRODUCER, nonce=expected["nonce"],
                                              module_file=expected["module_file"],
                                              module_sha256=expected["module_sha256"])))
        assert read_report(path, expected)["state"] == "foreign", "another producer's report is refused"


_PY312 = sys.executable if sys.version_info >= (3, 12) else shutil.which("python3.12")


@pytest.mark.skipif(_PY312 is None, reason="unittest's no-tests exit 5 is 3.12+")
def test_no_tests_keeps_unittests_own_exit(tmp_path):
    root = _project(tmp_path)
    (receipt,) = _run(root, (_PY312,) + UNITTEST[1:] + (TOKEN,)).receipts
    assert receipt.returncode == 5 and receipt.report["exitstatus"] == 5


# -- pytest 9 subtests ---------------------------------------------------------------------

INERT = """
import unittest
class Inert(unittest.TestCase):
    def test_subtests(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertTrue(value)
    def test_direct_assertion(self):
        self.assertEqual(1, 2)
"""


def test_roots_inert_subtest_file_now_parses_with_one_attributable_failure(tmp_path):
    """Root's R21 control: on 319385322b this report was refused as more
    executed than collected and the direct assertion went unattributed."""
    root = _project(tmp_path, test_inert=INERT)
    result = _run(root, PYTEST + (TOKEN,))
    report = result.receipts[0].report
    assert report["state"] == "parsed", report
    assert (report["collected"], report["counts"]) == (2, dict(passed=1, failed=1, errors=0, skipped=0))
    assert attribute(result)["product"] is True


@pytest.mark.parametrize("body,counts,product", [
    ("with self.subTest(v=0):\n                    self.assertTrue(0)", dict(passed=1, failed=1, errors=0, skipped=0), True),
    ("with self.subTest(v=0):\n                    raise RuntimeError('x')", dict(passed=1, failed=1, errors=0, skipped=0),
     False),
    ("with self.subTest(v=0):\n                    self.skipTest('no')", dict(passed=1, failed=0, errors=0, skipped=1), None),
])
def test_a_pytest_subtest_is_part_of_its_item(tmp_path, body, counts, product):
    root = _project(tmp_path, test_sub=f"""
        import unittest
        class Sub(unittest.TestCase):
            def test_ok(self):
                for v in (1, 2):
                    with self.subTest(v=v):
                        self.assertTrue(v)
            def test_case(self):
                {body}
        """)
    result = _run(root, PYTEST + (TOKEN,))
    report = result.receipts[0].report
    assert report["state"] == "parsed" and report["counts"] == counts, report
    if product is not None:
        assert attribute(result)["product"] is product


def test_the_pytest_subtests_fixture_is_counted_the_same_way(tmp_path):
    root = _project(tmp_path, test_fix="""
        def test_fixture(subtests):
            for v in (1, 0):
                with subtests.test(v=v):
                    assert v
        """)
    result = _run(root, PYTEST + (TOKEN,))
    report = result.receipts[0].report
    assert report["state"] == "parsed" and report["counts"]["failed"] == 1 and report["collected"] == 1, report
    assert attribute(result)["product"] is True


# -- whole Session: the actual dual gate gets exactly one repair -----------------------------

_BUGGY = """
import unittest
from app import add

class AddTest(unittest.TestCase):
    def test_sums(self):
        for a, b in ((1, 2), (3, 4)):
            with self.subTest(a=a, b=b):
                self.assertEqual(add(a, b), a + b)
    def test_written_wrong(self):
        self.assertEqual(add(2, 2), 5)
"""
_REPAIRED = _BUGGY.replace("add(2, 2), 5", "add(2, 2), 4")


@pytest.mark.parametrize("unittest_gate", ["declared", "undeclared", "error"])
def test_a_dual_gate_body_assertion_gets_exactly_one_repair(tmp_path, monkeypatch, unittest_gate):
    """R21's shape: a required unittest check and a required pytest check,
    a passing subtest test beside a test the run wrote wrong. Declared, both
    reports attribute the failure to a body assertion and the run gets one
    gate-fix call and completes. Undeclared unittest, or a non-assertion
    failure, still hands off with no repair call."""
    body = _BUGGY if unittest_gate != "error" else _BUGGY.replace(
        "self.assertEqual(add(2, 2), 5)", "raise RuntimeError('not a product assertion')")

    def lead(call, replay):
        if call.task != "t1":
            return Script()._lead(call, replay)
        H.write(call, {"app.py": FIXED, "tests/test_app.py": body})
        return 'Implemented add with tests.\nCHANGED: ["app.py", "tests/test_app.py"]'

    def gate_fix(call, replay):
        H.write(call, {"tests/test_app.py": _REPAIRED})
        return 'Fixed the test expectation.\nCHANGED: ["tests/test_app.py"]'

    unittest_check = " ".join(UNITTEST + ((TOKEN,) if unittest_gate != "undeclared" else ()))
    original = ("import unittest\nfrom app import add\n\n\nclass Original(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n")
    replay = H.run(tmp_path, monkeypatch, Script(lead=lead, **{"gate-fix": gate_fix}),
                   files={**FILES, "app.py": FIXED, "tests/test_original.py": original,
                          "tests/test_app.py": original.replace("Original", "App")},
                   check=" ".join((PY, "-m", "pytest", "-q", TOKEN)), extra_checks=[unittest_check], max_tasks=2,
                   record_complete=False)
    fixes = replay.of("gate-fix")
    if unittest_gate == "declared":
        assert len(fixes) == 1, (replay.result.error, [c.role for c in replay.calls])
        assert "CheckUnattributable" not in (replay.result.error or ""), replay.result.error
        assert (replay.project / "tests" / "test_app.py").read_text() == _REPAIRED, (
            replay.result.error, [(c.role, c.cwd) for c in replay.calls])
    else:
        assert not fixes and "CheckUnattributable" in (replay.result.error or ""), replay.result.error


# -- root review of f0b4827: skipped subcases and the reporter's cache identity -------------

_SUBCASES = {
    "pass-and-skip": ("for v in (1, 0):\n    with self.subTest(v=v):\n        if not v:\n"
                      "            self.skipTest('no')\n        self.assertTrue(v)",
                      dict(passed=0, failed=0, errors=0, skipped=1), None),
    "skip-only": ("with self.subTest(v=0):\n    self.skipTest('no')",
                  dict(passed=0, failed=0, errors=0, skipped=1), None),
    "fail-and-skip": ("for v in (0, 2):\n    with self.subTest(v=v):\n        if v == 2:\n"
                      "            self.skipTest('no')\n        self.assertTrue(v)",
                      dict(passed=0, failed=1, errors=0, skipped=0), dict(pytest=True, unittest=False)),
}


@pytest.mark.parametrize("runner", ["unittest", "pytest"])
@pytest.mark.parametrize("case", sorted(_SUBCASES))
def test_a_skipped_subcase_is_never_counted_as_its_test_passing(tmp_path, runner, case):
    """Root on f0b4827: a skipped subcase vanished from the canonical report,
    so the receipt showed zero skips and the test as passed. With no
    failure its test now counts skipped; a failure still decides, with its
    provenance. Both producers, real runs."""
    from quadratus.integration import skipped_count
    body, counts, product = _SUBCASES[case]
    module = "import unittest\n\n\nclass Sub(unittest.TestCase):\n    def test_case(self):\n"
    root = _project(tmp_path, test_sub=module + textwrap.indent(body, " " * 8) + "\n")
    result = _run(root, (UNITTEST if runner == "unittest" else PYTEST) + (TOKEN,))
    (receipt,) = result.receipts
    assert receipt.report["state"] == "parsed" and receipt.report["counts"] == counts, receipt.report
    assert skipped_count(receipt) == counts["skipped"]
    if product is not None:
        # unittest's own summary for fail-and-skip is "Ran 1 test ... (failures=1,
        # skipped=1)", which the unchanged runner count reads as zero executed:
        # the receipt fails closed as unattributable, as it did before this fix.
        assert attribute(result)["product"] is product[runner], attribute(result)


@pytest.mark.parametrize("runner", ["unittest", "pytest"])
def test_the_selected_reporters_bytes_are_part_of_the_cache_identity(tmp_path, monkeypatch, runner):
    """Root on f0b4827: a changed copy of the unittest producer still hit the
    gate cache with the same runner hash. The shipped producer a declared
    report selects is now hashed with the runner: unchanged stays cached,
    changed bytes miss."""
    import quadratus.integration as integration
    root = _project(tmp_path, test_one="""
        import unittest
        class One(unittest.TestCase):
            def test_ok(self):
                self.assertTrue(True)
        """)
    name = "_UNITTEST_PRODUCER_FILE" if runner == "unittest" else "_PRODUCER_FILE"
    copy = tmp_path / Path(getattr(integration, name)).name
    copy.write_bytes(Path(getattr(integration, name)).read_bytes())
    monkeypatch.setattr(integration, name, copy)
    argv = (UNITTEST if runner == "unittest" else PYTEST) + (TOKEN,)
    suite = GateSuite([GateCommand("c", argv=argv, cacheable=True)], cwd=root)
    first = suite.run().receipts[0]
    again = suite.run().receipts[0]
    assert (first.cached, again.cached, again.runner_hash) == (False, True, first.runner_hash)
    copy.write_bytes(copy.read_bytes() + b"\n# changed\n")
    changed = suite.run().receipts[0]
    assert changed.cached is False and changed.runner_hash != first.runner_hash
