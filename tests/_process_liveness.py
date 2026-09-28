"""Test-only liveness check for a process whose identity was recorded at launch."""

import subprocess
import sys
from pathlib import Path


def _linux_stat(pid: int) -> tuple[str, str]:
    # /proc/<pid>/stat's comm field is parenthesized and may itself contain
    # spaces or parentheses. Fields after the final ')' start with state.
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    if len(fields) < 20:
        raise AssertionError(f"malformed /proc/{pid}/stat")
    return fields[0], fields[19]  # state (field 3), starttime (field 22)


def process_starttime(pid: int) -> str | None:
    """Capture Linux PID identity; retain the existing ps route elsewhere."""
    if sys.platform.startswith("linux"):
        assert Path("/proc/self/stat").is_file(), "Linux /proc is required for process checks"
        return _linux_stat(pid)[1]
    return None


def process_stopped(pid: int, starttime: str | None) -> bool:
    """True only if this recorded process is gone or a zombie."""
    if sys.platform.startswith("linux"):
        assert Path("/proc/self/stat").is_file(), "Linux /proc is required for process checks"
        assert starttime is not None, "Linux process identity was not recorded"
        try:
            state, current_starttime = _linux_stat(pid)
        except FileNotFoundError:
            return True
        return current_starttime != starttime or state == "Z"
    proc = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True, check=False)
    return not proc.stdout.strip() or proc.stdout.strip().startswith("Z")
