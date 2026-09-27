"""Operator-declared capability readiness probes (phase 2, #25).

Run 19: the browser suite failed because the operator's environment had no
HOME, and the run spent a gate-fix on the application instead of saying so.
A readiness probe is the operator's own declaration of what must work before
any model is asked to do anything: "node can launch Chromium", "python can
import the frozen test deps". It runs once at run start, under the real
execution identity and environment (the same ``os.environ`` the checks run
with), before the first model call. A failing probe is an operator handoff:
the run stops with the probe's own bounded output and no model call is made.

What a probe is not:

- Not acceptance. Passing proves readiness only and never counts toward a
  task's checks or the goal.
- Not retried, replayed or given an allowance. One execution each.
- Not a place for the application to be repaired. Nothing a model writes can
  change a probe's declaration, which is fixed before the run starts.

Bounds: at most ``MAX_PROBES`` probes, each at most ``MAX_TIMEOUT`` seconds,
output kept as its last ``TAIL_CHARS`` characters. Each probe gets a
harness-owned scratch directory (``QUADRATUS_PROBE_DIR``) and a private
bytecode prefix, both removed afterwards; the probe runs in its own process
group, which is killed on timeout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

MAX_PROBES = 8
MAX_TIMEOUT = 120
TAIL_CHARS = 2000
_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_SHELL = re.compile(r"[;&|`$<>]")


class CapabilityProbeFailed(RuntimeError):
    """A declared readiness probe failed before any model call: an operator
    handoff, never an application repair."""


@dataclass(frozen=True)
class Probe:
    id: str
    argv: Tuple[str, ...]
    timeout: int = 30


@dataclass(frozen=True)
class ProbeReceipt:
    id: str
    passed: bool
    returncode: Optional[int]
    reason: str
    seconds: float
    output: str


def probes_from(data, root) -> Tuple[Probe, ...]:
    """Validate an operator declaration: a JSON list of
    ``{"id", "argv", "timeout"}``. Raises ValueError naming the problem."""
    if isinstance(data, (str, Path)):
        data = json.loads(Path(data).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("readiness probes must be a non-empty JSON list")
    if len(data) > MAX_PROBES:
        raise ValueError(f"at most {MAX_PROBES} readiness probes")
    probes, seen = [], set()
    for entry in data:
        if not isinstance(entry, dict) or set(entry) - {"id", "argv", "timeout"}:
            raise ValueError("each readiness probe is an object with id, argv and optional timeout")
        pid, argv, timeout = entry.get("id"), entry.get("argv"), entry.get("timeout", 30)
        if not isinstance(pid, str) or not _ID.fullmatch(pid) or pid in seen:
            raise ValueError(f"readiness probe id must be unique, lowercase and short: {pid!r}")
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(a, str) or not a or _SHELL.search(a) for a in argv)):
            raise ValueError(f"readiness probe {pid} argv must be a list with no shell syntax")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
            raise ValueError(f"readiness probe {pid} timeout must be 1 to {MAX_TIMEOUT} seconds")
        seen.add(pid)
        probes.append(Probe(pid, tuple(argv), timeout))
    return tuple(probes)


def run_probe(probe: Probe, root) -> ProbeReceipt:
    owned = tempfile.mkdtemp(prefix=f"quadratus-probe-{probe.id}-")
    env = dict(os.environ, QUADRATUS_PROBE_DIR=owned, PYTHONPYCACHEPREFIX=os.path.join(owned, "pycache"),
               PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    try:
        try:
            proc = subprocess.Popen(list(probe.argv), cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return ProbeReceipt(probe.id, False, None, f"could not start: {exc}", 0.0, "")
        try:
            out, _ = proc.communicate(timeout=probe.timeout)
            code, reason = proc.returncode, "exit 0" if proc.returncode == 0 else f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            _kill(proc)
            out, _ = proc.communicate()
            code, reason = None, f"timed out after {probe.timeout}s"
        text = out.decode("utf-8", "replace")[-TAIL_CHARS:]
        return ProbeReceipt(probe.id, code == 0, code, reason, round(time.monotonic() - started, 2), text)
    finally:
        shutil.rmtree(owned, ignore_errors=True)


def _kill(proc) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            continue


def run_probes(probes, root) -> List[dict]:
    """Every probe once, in order; stops at the first failure (a later probe
    has nothing to prove once readiness is refused)."""
    receipts = []
    for probe in probes:
        receipt = run_probe(probe, root)
        receipts.append(asdict(receipt))
        if not receipt.passed:
            break
    return receipts
