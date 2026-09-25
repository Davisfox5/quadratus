"""Serve a lead's in-session worker calls from the live session.

Before this, a lead could delegate only by making ``WORKER {...}`` its whole
reply, which ended its session: everything it had read was lost, and the
re-invocation paid for it again. Measured on GameTape (2026-09-24), grok leads
wrote WORKER and FETCH requests into their reasoning mid-loop instead, where
nothing served them, and explored themselves. The session-tool probe showed
all three CLIs will call a real tool correctly, so the channel is now a tool.

The bridge is a Unix socket in a private directory with a per-bridge token.
The CLI starts :mod:`quadratus.worker_mcp` as a stdio MCP server; that server
forwards each ``commission_worker`` call here, and ``handler`` runs it through
the session's own worker path. Each call runs in a copy of the lead's context,
so its ledger rows carry the lead's task, and calls are serialised: a worker
answer is small, and serial calls keep the pool's accounting simple to read.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

log = logging.getLogger(__name__)

SERVER_NAME = "quadratus"
#: Seconds a CLI waits on one tool call. A real worker takes 10 to 60 s and a
#: demanding one longer; grok's own default (60 s) would cut those off.
TOOL_TIMEOUT = 900


def input_schema() -> dict:
    """The typed call, from the live worker tree and needs vocabulary."""
    from .task_kinds import KNOWN_NEEDS
    from .workers import WORKER_TREE

    read_needs = sorted(str(getattr(n, "value", n)) for n in KNOWN_NEEDS)
    read_needs = [n for n in read_needs if n not in ("patch", "direct-write")]
    return {
        "type": "object",
        "properties": {
            "errand": {"type": "string", "enum": sorted(WORKER_TREE),
                       "description": "What kind of errand this is; it picks the worker model."},
            "instruction": {"type": "string", "minLength": 1, "maxLength": 4000,
                            "description": "One bounded request, answerable in one step."},
            "needs": {"type": "array", "items": {"type": "string", "enum": read_needs},
                      "description": "execute if the worker must run a command; omit for an answer from reading."},
            "demanding": {"type": "boolean",
                          "description": "Bump one tier up the same vendor's line."},
        },
        "required": ["errand", "instruction"],
        "additionalProperties": False,
    }


class WorkerBridge:
    """One lead drafting loop's worker channel. Use as a context manager."""

    def __init__(self, handler: Callable[[dict], Tuple[str, bool]]):
        self._handler = handler
        self._context = contextvars.copy_context()
        self._lock = threading.Lock()
        self._token = secrets.token_hex(16)
        self._dir: Optional[str] = None
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self.calls = 0

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "WorkerBridge":
        # Short base directory: AF_UNIX paths are capped near 104 bytes on macOS.
        base = "/tmp" if os.path.isdir("/tmp") else None
        self._dir = tempfile.mkdtemp(prefix="qw-", dir=base)
        os.chmod(self._dir, 0o700)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self._sock.listen(4)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, name="quadratus-worker-bridge", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._closed.set()
        if self._sock is not None:
            # Wake accept() now rather than at its next poll: closing a
            # listening socket does not interrupt accept() on macOS, and every
            # drafting loop opens a bridge.
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                    wake.settimeout(1)
                    wake.connect(self.socket_path)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._sock is not None:
            self._sock.close()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)

    @property
    def socket_path(self) -> str:
        return os.path.join(self._dir or "", "s")

    # -- what a provider attaches --------------------------------------------
    def spec(self) -> dict:
        """How a CLI starts the tool's server for one call."""
        return dict(
            name=SERVER_NAME,
            command=sys.executable,
            args=[str(Path(__file__).with_name("worker_mcp.py"))],
            env={"QUADRATUS_WORKER_SOCKET": self.socket_path,
                 "QUADRATUS_WORKER_TOKEN": self._token,
                 "QUADRATUS_WORKER_SCHEMA": json.dumps(input_schema())},
            timeout=TOOL_TIMEOUT,
        )

    # -- serving -------------------------------------------------------------
    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self._closed.is_set():
                conn.close()
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(10)
                data = b""
                while not data.endswith(b"\n") and len(data) < 1_000_000:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.decode() or "{}")
                if not hmac.compare_digest(str(request.get("token", "")), self._token):
                    text, failed = "Refused: this call does not carry the session's token.", True
                else:
                    arguments = request.get("arguments")
                    with self._lock:
                        self.calls += 1
                        text, failed = self._context.copy().run(self._handler, arguments)
            except Exception as exc:  # noqa: BLE001 -- the model gets the failure, the run decides
                log.debug("worker bridge call failed", exc_info=True)
                text, failed = f"The worker call failed: {type(exc).__name__}: {exc}"[:1000], True
            try:
                conn.settimeout(None)
                conn.sendall(json.dumps({"text": text, "is_error": failed}).encode())
            except OSError:
                pass
