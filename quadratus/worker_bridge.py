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
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)

SERVER_NAME = "quadratus"
#: Seconds a CLI waits on one tool call. A real worker takes 10 to 60 s and a
#: demanding one longer; grok's own default (60 s) would cut those off.
TOOL_TIMEOUT = 900


def input_schema() -> dict:
    """The typed call, from the live worker tree."""
    from .workers import WORKER_TREE

    # No needs field: every worker is a restricted seat that can neither run
    # a command nor write, so the only need a tool call could declare is
    # refused on arrival (the first draft offered "execute" and nothing else).
    return {
        "type": "object",
        "properties": {
            "errand": {"type": "string", "enum": sorted(WORKER_TREE),
                       "description": "What kind of errand this is; it picks the worker model."},
            "instruction": {"type": "string", "minLength": 1, "maxLength": 4000,
                            "description": "One bounded request, answerable in one step."},
            "demanding": {"type": "boolean",
                          "description": "Bump one tier up the same vendor's line."},
        },
        "required": ["errand", "instruction"],
        "additionalProperties": False,
    }


#: Longest socket path used under the scratch root. AF_UNIX paths are capped
#: near 104 bytes on macOS (108 on Linux), terminator included.
_SOCKET_PATH_BYTES = 100


def _socket_base() -> Optional[str]:
    """The folder the bridge's own private folder is made in.

    The private scratch root when the socket's path fits in a socket address
    there: a vendor sandbox in workspace-write mode may write under /tmp, so
    a steered editing call could remove another lead's socket or put its own
    listener in its place. A home path too long for a socket address, or a
    scratch root that cannot be used, keeps the old short base, /tmp; the
    folder is 0700 and the token still gates every call either way.
    """
    fallback = "/tmp" if os.path.isdir("/tmp") else None
    try:
        from .providers import scratch_root
        root = scratch_root()
    except Exception:  # noqa: BLE001 -- the bridge still works from the short base
        log.debug("no private scratch root for the worker bridge; using the short base", exc_info=True)
        return fallback
    # mkdtemp adds "qw-" and eight characters; the socket is named "s".
    longest = os.path.join(root, "qw-" + "x" * 8, "s")
    return root if len(os.fsencode(longest)) <= _SOCKET_PATH_BYTES else fallback


class WorkerBridge:
    """One lead drafting loop's worker channel. Use as a context manager.

    Leaving the block ends the channel for good. A call that arrives or is
    still queued after that is refused before the handler runs, and a call
    already inside the handler is waited for (``drain_seconds``): its worker
    is a real model call that cannot be interrupted from here, and returning
    while it ran let it spend budget nobody was watching and then write its
    saved state over the session's next call.
    """

    def __init__(self, handler: Callable[[dict], Tuple[str, bool]], *,
                 drain_seconds: float = TOOL_TIMEOUT):
        self._handler = handler
        self._context = contextvars.copy_context()
        self._lock = threading.Lock()
        self._token = secrets.token_hex(16)
        self._dir: Optional[str] = None
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        #: One thread per accepted connection, kept so exit can wait for them.
        self._handlers: List[threading.Thread] = []
        self._handlers_lock = threading.Lock()
        #: How long exit waits for a call already inside the handler. A tool
        #: call is cut off by its CLI after ``TOOL_TIMEOUT`` anyway.
        self._drain_seconds = drain_seconds
        self.calls = 0
        #: Calls still inside the handler when exit stopped waiting. Not
        #: zero means a worker call outlived its lead's channel.
        self.abandoned = 0

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "WorkerBridge":
        self._dir = tempfile.mkdtemp(prefix="qw-", dir=_socket_base())
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
        # An operator stopping the run (KeyboardInterrupt) is not made to
        # wait out a worker call; every other exit is.
        stopping = bool(exc) and exc[0] is not None and not issubclass(exc[0], Exception)
        try:
            self.drain(1.0 if stopping else None)
        finally:
            # Also when the operator interrupts the wait itself: the socket's
            # folder is removed either way.
            if self._dir:
                shutil.rmtree(self._dir, ignore_errors=True)

    def drain(self, seconds: Optional[float] = None) -> int:
        """Wait for calls already being answered; return how many still run.

        Called on exit, after the channel is closed, so nothing new starts
        while it waits. One shared allowance, not one per call. A call that
        outlasts it is counted in ``abandoned`` and logged, since a thread
        cannot be stopped from outside. It may also be called while the
        channel is open (between two lead calls): calls accepted during that
        wait stay listed and are counted in what it returns.
        """
        end = time.monotonic() + (self._drain_seconds if seconds is None else seconds)
        with self._handlers_lock:
            handlers = list(self._handlers)
        for handler in handlers:
            if handler is not threading.current_thread():
                handler.join(max(0.0, end - time.monotonic()))
        with self._handlers_lock:
            # From the list as it is now, not the copy taken above: while the
            # channel is open a call accepted during the wait is listed after
            # that copy, and replacing the list with the copy's survivors
            # dropped it, so the wait on exit never saw it.
            self._handlers = [handler for handler in self._handlers if handler.is_alive()]
            running = len(self._handlers)
        if self._closed.is_set():
            self.abandoned = running
            if running:
                log.warning("%d worker call(s) were still running when their lead's channel closed; "
                            "their spend lands after the lead's call record.", running)
        return running

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
            handler = threading.Thread(target=self._answer, args=(conn,), daemon=True)
            with self._handlers_lock:
                # Started under the lock: a thread listed before it has
                # started cannot be joined, and ``drain`` joins the list.
                handler.start()
                self._handlers = [h for h in self._handlers if h.is_alive()] + [handler]

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
                        # Checked under the lock, so a call that queued
                        # behind another cannot start once the channel is
                        # closed: its lead's call is over.
                        if self._closed.is_set():
                            text, failed = ("The worker channel is closed: the call that opened it "
                                            "has ended."), True
                        else:
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
