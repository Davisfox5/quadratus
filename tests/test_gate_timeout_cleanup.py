"""A check that times out keeps its output and leaves none of its own
processes behind (run 20261010T225947Z-fec6da52: a timed-out original-test
check left a Node test process and two Python preview servers running, and
the saved output was only "timed out after 600s").

Only the command's own process group is touched, pid by pid with its start
time re-read before SIGKILL; a process that detached into its own session is
outside what the check owns and is left alone. Bounded: each case runs for
a few seconds.
"""

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from quadratus.integration import GateCommand, GateSuite, IntegrationGate

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie still answers signal 0
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]
    except OSError:
        return True


def _wait_gone(pid, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and _alive(pid):
        time.sleep(0.05)
    return not _alive(pid)


def _script(tmp_path, body):
    """The check lives in ``tmp_path/project``; the pid record is written
    outside it, so the check does not change the source it checks."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "check.py").write_text("import os, subprocess, sys, time, signal\n"
                                      + body.replace("'child.pid'", repr(str(tmp_path / "child.pid"))))
    return project


SERVER = ("child = subprocess.Popen([sys.executable, '-c', {code!r}])\n"
          "open('child.pid', 'w').write(str(child.pid))\n")


def test_a_timed_out_check_keeps_its_output_and_stops_its_grandchild(tmp_path):
    code = "import time\nwhile True: time.sleep(1)"
    project = _script(tmp_path, SERVER.format(code=code) + "print('ok 1 - started the preview', flush=True)\ntime.sleep(60)\n")
    suite = GateSuite([GateCommand(id="extra-1", argv=(sys.executable, "check.py"), timeout=3)], cwd=project)
    started = time.monotonic()
    result = suite.run()
    [receipt] = result.receipts
    assert receipt.status == "error" and receipt.reason == "timed out after 3s"
    assert "ok 1 - started the preview" in receipt.output, "what printed before the deadline is kept"
    assert "terminated" in receipt.output
    assert _wait_gone(int((tmp_path / "child.pid").read_text()))
    assert time.monotonic() - started < 15


def test_a_grandchild_that_ignores_sigterm_is_killed(tmp_path):
    code = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(1)"
    project = _script(tmp_path, SERVER.format(code=code) + "time.sleep(60)\n")
    gate = IntegrationGate([sys.executable, "check.py"], cwd=project, timeout=3)
    result = gate.run()
    assert not result.passed and result.returncode is None
    assert "timed out after 3s" in result.output and "killed" in result.output
    assert _wait_gone(int((tmp_path / "child.pid").read_text()))


def test_a_background_process_left_after_a_clean_exit_is_stopped(tmp_path):
    code = "import time\nwhile True: time.sleep(1)"
    project = _script(tmp_path, "child = subprocess.Popen([sys.executable, '-c', " + repr(code) + "], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "open('child.pid', 'w').write(str(child.pid))\nprint('1 passed')\n")
    suite = GateSuite([GateCommand(id="check", argv=(sys.executable, "check.py"), timeout=20)], cwd=project)
    result = suite.run()
    [receipt] = result.receipts
    assert receipt.status == "passed", "the check's own verdict is unchanged"
    assert "owned processes left by the command" in receipt.output
    assert _wait_gone(int((tmp_path / "child.pid").read_text()))


def test_a_process_that_left_the_group_is_not_signalled(tmp_path):
    code = "import time\nwhile True: time.sleep(1)"
    project = _script(tmp_path, "child = subprocess.Popen([sys.executable, '-c', " + repr(code) + "], start_new_session=True, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "open('child.pid', 'w').write(str(child.pid))\ntime.sleep(60)\n")
    suite = GateSuite([GateCommand(id="check", argv=(sys.executable, "check.py"), timeout=2)], cwd=project)
    suite.run()
    pid = int((tmp_path / "child.pid").read_text())
    try:
        assert _alive(pid), "a detached process is outside what the check owns"
    finally:
        os.kill(pid, signal.SIGKILL)
