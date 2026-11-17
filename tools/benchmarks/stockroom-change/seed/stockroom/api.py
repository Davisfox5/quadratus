"""The JSON HTTP API over a Store."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .store import Store, StoreError

STATUS = {"invalid_sku": 400, "invalid_name": 400, "invalid_quantity": 400, "duplicate_sku": 409}


RESERVATION_STATUS = {
    "invalid_quantity": 400,
    "invalid_holder": 400,
    "invalid_ttl": 400,
    "unknown_sku": 404,
    "unknown_reservation": 404,
    "insufficient_stock": 409,
    "already_committed": 409,
    "reservation_expired": 409,
}


def _json_integer(text):
    """Parse JSON integers without the interpreter's decimal digit limit."""
    negative = text.startswith("-")
    digits = text[1:] if negative else text
    value = 0
    for start in range(0, len(digits), 500):
        chunk = digits[start:start + 500]
        value = value * 10 ** len(chunk) + int(chunk)
    return -value if negative else value


def _reject_constant(value):
    raise ValueError("non-JSON constant: " + value)


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
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, status, code, message):
            self.close_connection = True
            # Parsing can fail before the request's version is established.
            # HTTP/0.9 suppresses headers, including for versionless requests.
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.0"
            self._send(status, {"error": code, "message": message})

        def send_error(self, code, message=None, explain=None):
            if code in (405, 501):
                status, error = 405, "method_not_allowed"
            else:
                status = code
                error = {
                    404: "not_found",
                    413: "body_too_large",
                }.get(code, "bad_request")
            message = message or self.responses.get(code, ("Request rejected",))[0]
            self._error(status, error, message)

        def _route(self):
            if self.path == "/items":
                return "items", None, {"GET", "POST"}
            if self.path == "/reservations":
                return "reserve", None, {"POST"}
            match = re.fullmatch(r"/reservations/([^/]*)/commit", self.path)
            if match:
                return "commit", match[1], {"POST"}
            match = re.fullmatch(r"/reservations/([^/]*)", self.path)
            if match:
                return "reservation", match[1], {"GET", "DELETE"}
            match = re.fullmatch(r"/items/([^/]+)/availability", self.path)
            if match:
                return "availability", unquote(match[1]), {"GET"}
            return None, None, set()

        def __getattr__(self, name):
            # BaseHTTPRequestHandler dispatches arbitrary methods through do_*.
            if name.startswith("do_"):
                return self._dispatch
            raise AttributeError(name)

        def _read_reservation(self):
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
                self._error(400, "invalid_length", "Content-Length must be a decimal integer")
                return None
            digits = lengths[0].lstrip("0") or "0"
            if len(digits) > 5 or int(digits) > 65536:
                self._error(413, "body_too_large", "body exceeds 65536 bytes")
                return None
            length = int(digits)
            raw = self.rfile.read(length)
            if len(raw) != length:
                self._error(400, "invalid_json", "incomplete JSON body")
                return None
            try:
                data = json.loads(
                    raw.decode("utf-8"),
                    parse_constant=_reject_constant,
                    parse_int=_json_integer,
                )
            except (ValueError, UnicodeError, RecursionError):
                self._error(400, "invalid_json", "body must be valid UTF-8 JSON")
                return None
            if not isinstance(data, dict):
                self._error(400, "invalid_body", "body must be an object")
                return None
            if not {"sku", "quantity", "holder"} <= data.keys():
                self._error(400, "missing_field", "sku, quantity and holder are required")
                return None
            if data.keys() - {"sku", "quantity", "holder", "ttl_seconds"}:
                self._error(400, "unknown_field", "body contains an unknown field")
                return None
            return data

        def _dispatch(self):
            route, value, methods = self._route()
            if route is None:
                self._error(404, "not_found", "no such resource")
                return
            if self.command not in methods:
                self._error(405, "method_not_allowed", "method not allowed for this resource")
                return
            if route in {"reservation", "commit"}:
                if not re.fullmatch(r"[0-9]{1,18}", value) or int(value) == 0:
                    self._error(400, "invalid_id", "id must be a positive decimal of 1 to 18 digits")
                    return
                value = int(value)
            try:
                status = 200
                if route == "reserve":
                    data = self._read_reservation()
                    if data is None:
                        return
                    result = store.reserve(
                        data["sku"], data["quantity"], data["holder"],
                        data.get("ttl_seconds", 900),
                    )
                    status = 201
                elif route == "availability":
                    result = store.availability(value)
                elif route == "commit":
                    result = store.commit(value)
                elif self.command == "DELETE":
                    result = store.release(value)
                else:
                    result = store.get_reservation(value)
            except StoreError as exc:
                self._error(RESERVATION_STATUS[exc.code], exc.code, exc.message)
                return
            self._send(status, result)

        def do_GET(self):
            if self.path == "/items":
                self._send(200, {"items": store.list_items()})
            else:
                self._dispatch()

        def do_POST(self):
            if self.path != "/items":
                self._dispatch()
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
