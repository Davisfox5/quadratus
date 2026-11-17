import http.client
import io
import json
import socket
import sqlite3
import threading
from unittest.mock import patch

import pytest

from stockroom.api import make_server
from stockroom.store import Store


NOW = 1000.0
PAYLOAD = {"sku": "A", "quantity": 2, "holder": "  alice  "}
KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    path = tmp_path_factory.mktemp("reservation-api") / "s.db"
    with patch("stockroom.store._clock", side_effect=lambda now: NOW if now is None else now):
        server = make_server(path)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        yield server, path
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.fixture
def api(service):
    server, path = service
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM reservations")
        db.execute("DELETE FROM items")
    store = Store(path)
    store.add_item("A", "Anvil", 10)
    store.add_item("empty", "Bucket", 0)
    return server, store


def snapshot(api):
    with sqlite3.connect(api[1].path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("items", "reservations", "sqlite_sequence")
        }


def call(api, method, path, payload=None, raw=None, lengths=None):
    body = json.dumps(payload).encode() if payload is not None else raw
    connection = http.client.HTTPConnection(*api[0].server_address, timeout=2)
    try:
        connection.putrequest(method, path)
        if lengths is None:
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
        else:
            for length in lengths:
                connection.putheader("Content-Length", length)
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read()
        return response.status, response.getheader("Content-Type"), json.loads(data) if data else None
    finally:
        connection.close()


def refused(api, method, path, code, status, **kwargs):
    before = snapshot(api)
    actual, content_type, body = call(api, method, path, **kwargs)
    assert actual == status
    assert content_type == "application/json"
    assert set(body) == {"error", "message"}
    assert body["error"] == code
    assert isinstance(body["message"], str) and body["message"].strip()
    assert snapshot(api) == before


def reserve(api, **changes):
    status, content_type, reservation = call(
        api, "POST", "/reservations", payload=dict(PAYLOAD, **changes)
    )
    assert status == 201
    assert content_type == "application/json"
    assert set(reservation) == KEYS
    assert type(reservation["id"]) is int and reservation["id"] > 0
    assert reservation == {
        "id": reservation["id"],
        "sku": changes.get("sku", "A"),
        "quantity": changes.get("quantity", 2),
        "holder": changes.get("holder", "  alice  ").strip(),
        "expires_at": NOW + changes.get("ttl_seconds", 900),
        "state": "active",
    }
    assert type(reservation["expires_at"]) is float
    return reservation


def test_success_routes_and_default_ttl(api):
    first = reserve(api)
    path = f"/reservations/{first['id']}"
    assert call(api, "GET", path) == (200, "application/json", first)
    assert call(api, "GET", "/items/A/availability") == (
        200, "application/json",
        {"sku": "A", "on_hand": 10, "reserved": 2, "available": 8},
    )
    released = dict(first, state="released")
    assert call(api, "DELETE", path) == (200, "application/json", released)
    assert call(api, "DELETE", path) == (200, "application/json", released)
    second = reserve(api, ttl_seconds=60)
    committed = dict(second, state="committed")
    assert call(api, "POST", f"/reservations/{second['id']}/commit") == (
        200, "application/json", committed,
    )
    assert call(api, "GET", f"/reservations/{second['id']}")[2] == committed
    assert api[1].get_item("A")["quantity"] == 8




def test_url_encoded_sku(api):
    api[1].add_item("A B", "Space", 1)
    assert call(api, "GET", "/items/A%20B/availability") == (
        200, "application/json",
        {"sku": "A B", "on_hand": 1, "reserved": 0, "available": 1},
    )


@pytest.mark.parametrize(
    "changes,code,status",
    [
        ({"sku": "missing"}, "unknown_sku", 404),
        ({"sku": None}, "unknown_sku", 404),
        ({"sku": []}, "unknown_sku", 404),
        ({"sku": "\udc80"}, "unknown_sku", 404),
        ({"quantity": 0}, "invalid_quantity", 400),
        ({"quantity": -1}, "invalid_quantity", 400),
        ({"quantity": True}, "invalid_quantity", 400),
        ({"quantity": None}, "invalid_quantity", 400),
        ({"quantity": "2"}, "invalid_quantity", 400),
        ({"quantity": 2.0}, "invalid_quantity", 400),
        ({"quantity": 1.5}, "invalid_quantity", 400),
        ({"holder": ""}, "invalid_holder", 400),
        ({"holder": "  "}, "invalid_holder", 400),
        ({"holder": None}, "invalid_holder", 400),
        ({"holder": 1}, "invalid_holder", 400),
        ({"holder": "x" * 65}, "invalid_holder", 400),
        ({"ttl_seconds": 0}, "invalid_ttl", 400),
        ({"ttl_seconds": -1}, "invalid_ttl", 400),
        ({"ttl_seconds": 86401}, "invalid_ttl", 400),
        ({"ttl_seconds": True}, "invalid_ttl", 400),
        ({"ttl_seconds": None}, "invalid_ttl", 400),
        ({"ttl_seconds": "60"}, "invalid_ttl", 400),
        ({"ttl_seconds": 60.0}, "invalid_ttl", 400),
        ({"ttl_seconds": 2**63}, "invalid_ttl", 400),
        ({"quantity": 11}, "insufficient_stock", 409),
        ({"quantity": 2**63}, "insufficient_stock", 409),
        ({"sku": "empty"}, "insufficient_stock", 409),
    ],
)
def test_store_refusals(api, changes, code, status):
    reserve(api, quantity=1)
    refused(
        api, "POST", "/reservations", code, status,
        payload=dict(PAYLOAD, **changes),
    )


@pytest.mark.parametrize("field", ["sku", "quantity", "holder"])
def test_required_fields(api, field):
    payload = dict(PAYLOAD)
    del payload[field]
    refused(api, "POST", "/reservations", "missing_field", 400, payload=payload)


def test_unknown_field(api):
    refused(
        api, "POST", "/reservations", "unknown_field", 400,
        payload=dict(PAYLOAD, extra=1),
    )


@pytest.mark.parametrize("raw", [b"null", b"[]", b"1", b'"text"', b"true"])
def test_non_object(api, raw):
    refused(api, "POST", "/reservations", "invalid_body", 400, raw=raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"", b"{", b"{} trailing", b"\xff",
        b'{"sku":"A","quantity":1,"holder":"\xff"}',
        b'{"sku":"A","quantity":NaN,"holder":"a"}',
        b'{"sku":"A","quantity":Infinity,"holder":"a"}',
        b'{"sku":"A","quantity":-Infinity,"holder":"a"}',
        b'{"sku":"A","quantity":1,"holder":"a","extra":NaN}',
    ],
)
def test_invalid_json(api, raw):
    refused(api, "POST", "/reservations", "invalid_json", 400, raw=raw)


@pytest.mark.parametrize("field,code", [("quantity", "invalid_quantity"), ("ttl_seconds", "invalid_ttl")])
@pytest.mark.parametrize("number", ["2.0", "2e0", "2E+0", "1e9999"])
def test_fraction_and_exponent_tokens(api, field, code, number):
    payload = dict(PAYLOAD)
    payload[field] = "TOKEN"
    raw = json.dumps(payload).replace('"TOKEN"', number).encode()
    refused(api, "POST", "/reservations", code, 400, raw=raw)


@pytest.mark.parametrize("field,code,status", [
    ("quantity", "insufficient_stock", 409),
    ("ttl_seconds", "invalid_ttl", 400),
])
def test_integer_beyond_parser_and_storage_ranges(api, field, code, status):
    payload = dict(PAYLOAD)
    payload[field] = "TOKEN"
    raw = json.dumps(payload).replace('"TOKEN"', "1" + "0" * 4500).encode()
    refused(api, "POST", "/reservations", code, status, raw=raw)


@pytest.mark.parametrize("lengths", [[], [""], ["-1"], ["+1"], ["1.0"], ["1e1"], ["abc"], ["1", "1"]])
def test_invalid_content_length(api, lengths):
    refused(api, "POST", "/reservations", "invalid_length", 400, lengths=lengths)


@pytest.mark.parametrize("length", ["65537", "9" * 5000])
def test_oversize_reply_without_sending_body(api, length):
    refused(api, "POST", "/reservations", "body_too_large", 413, lengths=[length])


def test_body_size_boundary_and_zero_padded_length(api):
    raw = json.dumps(PAYLOAD).encode()
    raw += b" " * (65536 - len(raw))
    assert call(api, "POST", "/reservations", raw=raw, lengths=["00065536"])[0] == 201


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("DELETE", ""), ("POST", "/commit")])
@pytest.mark.parametrize("identifier", ["0", "-1", "+1", "1.0", "1e0", "abc", "1" * 19, "%31", ""])
def test_invalid_ids(api, method, suffix, identifier):
    refused(api, method, "/reservations/" + identifier + suffix, "invalid_id", 400)


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("DELETE", ""), ("POST", "/commit")])
def test_unknown_reservation(api, method, suffix):
    refused(api, method, "/reservations/999999999999999999" + suffix, "unknown_reservation", 404)


def test_unknown_availability_sku(api):
    refused(api, "GET", "/items/missing/availability", "unknown_sku", 404)








@pytest.mark.parametrize("path", [
    "/items", "/reservations", "/reservations/1",
    "/reservations/1/commit", "/items/A/availability",
])
@pytest.mark.parametrize("method", ["PUT", "PATCH", "OPTIONS", "UNIMPLEMENTED"])
def test_methods_without_handlers(api, path, method):
    refused(api, method, path, "method_not_allowed", 405)


@pytest.mark.parametrize("method,path", [
    ("GET", "/reservations"),
    ("DELETE", "/reservations"),
    ("POST", "/reservations/1"),
    ("GET", "/reservations/1/commit"),
    ("DELETE", "/reservations/1/commit"),
    ("POST", "/items/A/availability"),
    ("DELETE", "/items/A/availability"),
])
def test_known_handlers_wrong_route_method(api, method, path):
    refused(api, method, path, "method_not_allowed", 405)


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "PUT", "UNIMPLEMENTED"])
@pytest.mark.parametrize("path", ["/nope", "/reservations/1/other", "/items/A/other"])
def test_unknown_paths(api, method, path):
    refused(api, method, path, "not_found", 404)


def test_head_has_json_headers_and_no_body(api):
    before = snapshot(api)
    assert call(api, "HEAD", "/reservations") == (405, "application/json", None)
    assert call(api, "HEAD", "/nope") == (404, "application/json", None)
    assert snapshot(api) == before


def test_truncated_body_changes_nothing(api):
    before = snapshot(api)
    raw = json.dumps(PAYLOAD).encode()
    with socket.create_connection(api[0].server_address, timeout=2) as connection:
        connection.sendall(
            b"POST /reservations HTTP/1.0\r\nContent-Length: "
            + str(len(raw) + 10).encode() + b"\r\n\r\n" + raw
        )
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = json.loads(response.read())
        assert response.status == 400
        assert response.getheader("Content-Type") == "application/json"
        assert set(body) == {"error", "message"}
        assert body["error"] == "invalid_json" and body["message"].strip()
    assert snapshot(api) == before


def test_413_does_not_call_body_read(api):
    handler_type = api[0].RequestHandlerClass

    class Unreadable:
        def read(self, *args):
            raise AssertionError("oversized body was read")

    handler = object.__new__(handler_type)
    handler.request_version = "HTTP/1.0"
    handler.headers = http.client.HTTPMessage()
    handler.headers["Content-Length"] = "65537"
    handler.rfile = Unreadable()
    replies = []
    handler._send = lambda status, payload: replies.append((status, payload))
    assert handler._read_reservation() is None
    assert replies[0][0] == 413
    assert replies[0][1]["error"] == "body_too_large"


def raw_refused(api, raw_request, status, code):
    before = snapshot(api)
    with socket.create_connection(api[0].server_address, timeout=2) as connection:
        connection.sendall(raw_request)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            try:
                chunk = connection.recv(65536)
            except ConnectionResetError:
                # Unread request bytes can cause a reset on server close.
                # The assertions below still require a complete response.
                break
            if not chunk:
                break
            chunks.append(chunk)
    # EOF or reset proves closure; a timeout still fails the test.
    wire = b"".join(chunks)
    head, separator, body = wire.partition(b"\r\n\r\n")
    assert separator, wire
    status_line, separator, header_lines = head.partition(b"\r\n")
    assert separator
    assert status_line.startswith(b"HTTP/1.")
    assert int(status_line.split()[1]) == status
    headers = http.client.parse_headers(io.BytesIO(header_lines + b"\r\n\r\n"))
    assert headers["Content-Type"] == "application/json"
    assert headers["Content-Length"] is not None
    assert int(headers["Content-Length"]) == len(body)
    payload = json.loads(body)
    assert set(payload) == {"error", "message"}
    assert payload["error"] == code
    assert isinstance(payload["message"], str) and payload["message"].strip()
    assert snapshot(api) == before


@pytest.mark.parametrize("raw_request,status", [
    (b"GET /reservations extra HTTP/1.0\r\n\r\n", 400),
    (b"GET /reservations HTTP/nope\r\n\r\n", 400),
    (b"GET /reservations HTTP/1.x\r\n\r\n", 400),
    (b"GET /reservations HTTP/2.0\r\n\r\n", 505),
    (b"GET /" + b"x" * 65536 + b" HTTP/1.0\r\n\r\n", 414),
    (
        b"GET /reservations HTTP/1.0\r\nX-Long: "
        + b"x" * 65536 + b"\r\n\r\n",
        431,
    ),
    (
        b"GET /reservations HTTP/1.0\r\n"
        + b"X-Header: value\r\n" * 101 + b"\r\n",
        431,
    ),
], ids=[
    "malformed-request-line", "bad-version-prefix", "bad-version-number",
    "unsupported-version", "long-request-line", "long-header", "many-headers",
])
def test_server_parse_errors_are_full_json_responses(api, raw_request, status):
    raw_refused(api, raw_request, status, "bad_request")


@pytest.mark.parametrize("path,status,code", [
    ("/nope", 404, "not_found"),
    ("/reservations", 405, "method_not_allowed"),
    ("/reservations/0", 400, "invalid_id"),
    ("/reservations/999", 404, "unknown_reservation"),
    ("/items/missing/availability", 404, "unknown_sku"),
])
def test_versionless_route_errors_have_full_responses(api, path, status, code):
    raw_refused(api, f"GET {path}\r\n".encode(), status, code)


@pytest.mark.parametrize("chosen,status,code", [
    (404, 404, "not_found"),
    (405, 405, "method_not_allowed"),
    (501, 405, "method_not_allowed"),
    (413, 413, "body_too_large"),
])
def test_send_error_status_mapping_over_socket(api, monkeypatch, chosen, status, code):
    # Dispatch normally handles these statuses itself; exercise the server's
    # send_error hook directly while retaining the actual network response.
    def reject(handler):
        handler.send_error(chosen)

    monkeypatch.setattr(api[0].RequestHandlerClass, "do_GET", reject)
    raw_refused(api, b"GET /reservations HTTP/1.0\r\n\r\n", status, code)


def test_original_items_paths(api):
    payload = {"sku": "B", "name": "Box", "quantity": 3}
    assert call(api, "POST", "/items", payload=payload) == (201, "application/json", payload)
    assert call(api, "GET", "/items") == (
        200, "application/json", {"items": api[1].list_items()},
    )
    refused(api, "POST", "/items", "duplicate_sku", 409, payload=payload)
