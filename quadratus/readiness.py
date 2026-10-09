"""Operator-declared capability readiness probes (phase 2, #25).

Run 19: the operator's browser check failed and the run spent a gate-fix on
the application. The original cause is unknown, because that run's stderr
was discarded; a same-UID run without HOME failing, and the same check with
an owned HOME passing, are prospective evidence, not proof of what happened.
Whatever the cause, nothing asked the environment first. A readiness probe is the operator's own declaration of what must work before
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

Bounds: at most ``MAX_PROBES`` probes, each at most ``MAX_TIMEOUT`` seconds.
Output is drained while the probe runs into a fixed-size tail
(``KEEP_BYTES``), never buffered whole, and marked ``truncated`` when more
was written; the deadline holds under continuous output. Each probe gets a
harness-owned scratch directory (``QUADRATUS_PROBE_DIR``) and a private
bytecode prefix, both removed afterwards. It runs in its own process group,
which is always stopped afterwards with the reviewed escalation from
quadratus.preview (SIGINT, SIGTERM, SIGKILL, waiting for the whole group,
not only its leader), on success and on timeout alike, so a descendant that
ignores SIGTERM or outlives a clean exit is stopped too. A cleanup error is
recorded and never replaces the probe's own result.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

MAX_PROBES = 8
MAX_TIMEOUT = 120
TAIL_CHARS = 2000
KEEP_BYTES = 8192
_READER_JOIN_SECONDS = 2
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
    output_bytes: int = 0
    truncated: bool = False
    #: A process the probe started was still in its group after the probe
    #: itself ended; it was stopped.
    left_processes: bool = False
    cleanup_error: Optional[str] = None


class _Tail:
    """Drains a pipe as it is written, keeping only the last KEEP_BYTES."""

    def __init__(self, stream):
        self._stream, self._buffer, self.total = stream, bytearray(), 0
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            for chunk in iter(lambda: self._stream.read1(4096), b""):
                self.total += len(chunk)
                self._buffer += chunk
                if len(self._buffer) > KEEP_BYTES:
                    del self._buffer[:-KEEP_BYTES]
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", "replace")[-TAIL_CHARS:]

    def close(self) -> None:
        """A bounded wait for the reader; the stream is closed only once the
        reader is done, never under a blocked read (see preview._BoundedLog)."""
        self._thread.join(timeout=_READER_JOIN_SECONDS)
        if not self._thread.is_alive():
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass


def probes_from(data, root) -> Tuple[Probe, ...]:
    """Validate an operator declaration: a JSON list of
    ``{"id", "argv", "timeout"}``. Raises ValueError naming the problem."""
    if isinstance(data, (str, Path)):
        path = Path(data)
        # A named problem, not a raw exception (Codex GUI diagnostic on 0a99ee2).
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"readiness probes file could not be read: {path} "
                             f"({exc.strerror or type(exc).__name__})") from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"readiness probes file is not valid JSON: {path} (line {getattr(exc, 'lineno', '?')})") \
                from None
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
    from .preview import _group_alive, _stop
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
        tail = _Tail(proc.stdout)
        timed_out, left, cleanup = False, False, None
        try:
            proc.wait(timeout=probe.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                left = not timed_out and _group_alive(proc.pid)
                _stop(proc)
            except Exception as exc:  # noqa: BLE001 -- recorded; the probe's own result stands
                cleanup = f"{type(exc).__name__}: {str(exc)[:160]}"
            tail.close()
        code = None if timed_out else proc.returncode
        reason = (f"timed out after {probe.timeout}s" if timed_out
                  else "exit 0" if code == 0 else f"exit {code}")
        text = tail.text()
        return ProbeReceipt(probe.id, code == 0, code, reason, round(time.monotonic() - started, 2), text,
                            output_bytes=tail.total, truncated=tail.total > len(text.encode("utf-8")),
                            left_processes=left, cleanup_error=cleanup)
    finally:
        shutil.rmtree(owned, ignore_errors=True)


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
