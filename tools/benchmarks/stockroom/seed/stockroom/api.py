"""The JSON HTTP API over a Store."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import Store, StoreError

STATUS = {"invalid_sku": 400, "invalid_name": 400, "invalid_quantity": 400, "duplicate_sku": 409}


def make_server(db_path, host="127.0.0.1", port=0):
    """A server over the database at ``db_path``. Port 0 picks a free port."""
    store = Store(db_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/items":
                self._send(200, {"items": store.list_items()})
            else:
                self._send(404, {"error": "not_found", "message": "no such resource"})

        def do_POST(self):
            if self.path != "/items":
                self._send(404, {"error": "not_found", "message": "no such resource"})
                return
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            try:
                item = store.add_item(data.get("sku"), data.get("name"), data.get("quantity"))
            except StoreError as exc:
                self._send(STATUS.get(exc.code, 400), {"error": exc.code, "message": exc.message})
                return
            self._send(201, item)

    return ThreadingHTTPServer((host, port), Handler)
