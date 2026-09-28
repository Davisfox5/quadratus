"""The ``commission_worker`` tool a lead calls without ending its turn.

A stdio MCP server, started by the vendor CLI for one lead call. It holds no
logic: each call is forwarded over a private Unix socket to the live session's
:class:`~quadratus.worker_bridge.WorkerBridge`, which runs the worker through
the same :class:`~quadratus.workers.WorkerPool` path, budgets and ledger as a
``WORKER`` reply. The answer comes back as the tool result.

Standard library only, and runnable as a plain script path, because the CLI
spawns it with whatever interpreter and working directory the call has. The
schema arrives in the environment, built by the bridge from the live
``WORKER_TREE`` and ``KNOWN_NEEDS``, so the enum the model sees is never a
second copy that can drift.

Probed on 2026-09-25 against claude 2.1.269, codex 0.154.0 and grok 1.0.30
with a stub: all three models called the tool correctly first time.
"""

import json
import os
import socket
import sys

TOOL_NAME = "commission_worker"
_DESCRIPTION = (
    "Commission a Quadratus worker for one bounded, read-only errand and wait for "
    "its answer. Use it for knowledge about the code (layout, conventions, what "
    "exists, what a function does) instead of exploring yourself: a worker answers "
    "in one step at a flat cost, while every step of your own session re-sends "
    "everything before it. Ask narrowly. The answer is text: workers cannot edit "
    "files, run commands or delegate."
)


def _reply(rid, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": rid}
    body.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


def _forward(arguments):
    path = os.environ["QUADRATUS_WORKER_SOCKET"]
    request = json.dumps({"token": os.environ["QUADRATUS_WORKER_TOKEN"],
                          "arguments": arguments}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(path)
        conn.sendall(request)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    answer = json.loads(b"".join(chunks).decode() or "{}")
    return str(answer.get("text", "")), bool(answer.get("is_error", True))


def main():
    schema = json.loads(os.environ.get("QUADRATUS_WORKER_SCHEMA") or "{}")
    tool = {"name": TOOL_NAME, "description": _DESCRIPTION, "inputSchema": schema}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method, rid = request.get("method"), request.get("id")
        if rid is None:
            continue  # a notification
        if method == "initialize":
            version = (request.get("params") or {}).get("protocolVersion", "2025-06-18")
            _reply(rid, {"protocolVersion": version, "capabilities": {"tools": {}},
                         "serverInfo": {"name": "quadratus", "version": "1"}})
        elif method == "tools/list":
            _reply(rid, {"tools": [tool]})
        elif method == "tools/call":
            params = request.get("params") or {}
            if params.get("name") != TOOL_NAME:
                _reply(rid, error={"code": -32602, "message": f"unknown tool {params.get('name')!r}"})
                continue
            try:
                text, failed = _forward(params.get("arguments") or {})
            except Exception as exc:  # noqa: BLE001 -- reported to the model, not raised
                text, failed = f"The worker channel is unavailable: {type(exc).__name__}: {exc}", True
            _reply(rid, {"content": [{"type": "text", "text": text}], "isError": failed})
        elif method == "ping":
            _reply(rid, {})
        else:
            _reply(rid, error={"code": -32601, "message": "method not found"})


if __name__ == "__main__":
    main()
