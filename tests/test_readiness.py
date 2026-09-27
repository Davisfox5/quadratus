"""Readiness probes run under the real environment with harness-owned,
cleaned-up scratch space (quadratus.readiness). Real subprocesses."""

import os
import sys

from quadratus.readiness import Probe, run_probe


def test_a_probe_sees_the_real_environment_and_an_owned_scratch_dir_that_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_TEST_MARKER", "from-the-operator")
    code = ("import os, pathlib\n"
            "d = pathlib.Path(os.environ['QUADRATUS_PROBE_DIR'])\n"
            "(d / 'probe.txt').write_text('x')\n"
            "print(os.environ['QUADRATUS_TEST_MARKER'], d)")
    receipt = run_probe(Probe("env", (sys.executable, "-c", code)), tmp_path)
    marker, owned = receipt.output.split()
    assert receipt.passed and marker == "from-the-operator"
    assert not os.path.exists(owned), "the harness removed its scratch directory"


def test_a_missing_runner_fails_without_raising(tmp_path):
    receipt = run_probe(Probe("gone", ("quadratus-no-such-runner",)), tmp_path)
    assert not receipt.passed and receipt.returncode is None and receipt.reason.startswith("could not start")


def test_output_is_kept_as_a_bounded_tail(tmp_path):
    receipt = run_probe(Probe("loud", (sys.executable, "-c", "print('x' * 50000)")), tmp_path)
    assert receipt.passed and len(receipt.output) == 2000
