"""Readiness probes run under the real environment with harness-owned,
cleaned-up scratch space (quadratus.readiness). Real subprocesses."""

import os
import sys
import threading
import time

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


# -- bounded cleanup and output (Codex review of c8c7d86) --------------------------------
#
# Each control runs the probe in a daemon thread under an outer deadline, so
# an unbounded implementation fails the assertion instead of hanging pytest.


OUTER = 25


def _bounded(probe, root):
    box = {}
    thread = threading.Thread(target=lambda: box.update(receipt=run_probe(probe, root)), daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(OUTER)
    return box.get("receipt"), time.monotonic() - started


def _dead(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        with open(f"/proc/{pid}/status") as status:
            return any(line.startswith("State:") and "Z" in line for line in status)
    except OSError:
        return True


_TERM_IGNORING_CHILD = (
    "import os, signal, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', "
    "'import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\ntime.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(0.3)\n"
)


def test_a_term_ignoring_descendant_holding_the_pipe_is_stopped_within_bounds(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = _TERM_IGNORING_CHILD + "time.sleep(120)\n"
    receipt, elapsed = _bounded(Probe("hold", (sys.executable, "-c", code, str(pidfile)), timeout=1), tmp_path)
    assert receipt is not None, f"run_probe did not return within {OUTER}s"
    assert receipt.reason == "timed out after 1s" and not receipt.passed
    assert _dead(int(pidfile.read_text())), "the descendant was stopped with the group"


def test_a_descendant_left_behind_by_a_clean_exit_is_stopped_and_recorded(tmp_path):
    pidfile = tmp_path / "child.pid"
    receipt, elapsed = _bounded(Probe("leak", (sys.executable, "-c", _TERM_IGNORING_CHILD, str(pidfile)),
                                      timeout=10), tmp_path)
    assert receipt is not None, f"run_probe did not return within {OUTER}s"
    assert receipt.passed and receipt.left_processes is True
    assert _dead(int(pidfile.read_text()))


def test_continuous_output_does_not_stretch_the_deadline_and_is_kept_bounded(tmp_path):
    code = "import sys\nwhile True:\n    sys.stdout.write('y' * 4096)\n"
    receipt, elapsed = _bounded(Probe("chatty", (sys.executable, "-c", code), timeout=1), tmp_path)
    assert receipt is not None, f"run_probe did not return within {OUTER}s"
    assert receipt.reason == "timed out after 1s" and elapsed < OUTER
    assert receipt.truncated and receipt.output_bytes > 8192 and len(receipt.output) <= 2000


def test_high_volume_output_is_drained_not_buffered_whole(tmp_path):
    size = 20 * 1024 * 1024
    code = f"import sys\nsys.stdout.write('z' * {size})\n"
    receipt, _ = _bounded(Probe("loud", (sys.executable, "-c", code), timeout=30), tmp_path)
    assert receipt is not None and receipt.passed
    assert receipt.output_bytes == size and receipt.truncated and len(receipt.output) == 2000
