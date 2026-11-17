import http.client
import json
import threading

import pytest
from stockroom.api import make_server
from stockroom.store import Store


@pytest.fixture
def srv(tmp_path):
    path = tmp_path / "s.db"
    Store(path).add_item("A-1", "Anvil", 10)
    server = make_server(path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address, path
    server.shutdown()
    server.server_close()


def raw(addr, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    reply = conn.getresponse()
    data = reply.read()
    conn.close()
    return reply.status, reply.getheader("Content-Type"), data


def call(addr, method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    status, ctype, data = raw(addr, method, path, body)
    return status, json.loads(data)


def err(addr, method, path, body=None, headers=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    status, ctype, data = raw(addr, method, path, body, headers)
    payload = json.loads(data)
    assert isinstance(payload, dict) and set(payload) == {"error", "message"}, payload
    assert isinstance(payload["message"], str) and payload["message"].strip()
    assert ctype and ctype.split(";")[0].strip() == "application/json"
    return status, payload["error"]


GOOD = {"sku": "A-1", "quantity": 2, "holder": "ann", "ttl_seconds": 60}


def test_reserve_get_release(srv):
    addr, _ = srv
    status, r = call(addr, "POST", "/reservations", GOOD)
    assert status == 201 and r["state"] == "active" and r["quantity"] == 2
    assert set(r) == {"id", "sku", "quantity", "holder", "expires_at", "state"}
    assert call(addr, "GET", f"/reservations/{r['id']}") == (200, r)
    assert call(addr, "GET", "/items/A-1/availability") == (
        200, {"sku": "A-1", "on_hand": 10, "reserved": 2, "available": 8})
    status, gone = call(addr, "DELETE", f"/reservations/{r['id']}")
    assert (status, gone["state"]) == (200, "released")


def test_commit(srv):
    addr, path = srv
    _, r = call(addr, "POST", "/reservations", GOOD)
    status, done = call(addr, "POST", f"/reservations/{r['id']}/commit")
    assert (status, done["state"]) == (200, "committed")
    assert Store(path).get_item("A-1")["quantity"] == 8
    assert err(addr, "POST", f"/reservations/{r['id']}/commit") == (409, "already_committed")
    assert err(addr, "DELETE", f"/reservations/{r['id']}") == (409, "already_committed")


def test_default_ttl_is_900(srv):
    addr, _ = srv
    _, r = call(addr, "POST", "/reservations", {"sku": "A-1", "quantity": 1, "holder": "ann"})
    import time
    assert 800 < r["expires_at"] - time.time() <= 900


def test_released_then_commit_conflicts(srv):
    addr, _ = srv
    _, r = call(addr, "POST", "/reservations", GOOD)
    call(addr, "DELETE", f"/reservations/{r['id']}")
    assert err(addr, "POST", f"/reservations/{r['id']}/commit") == (409, "reservation_released")


def test_expired_commit_conflicts(srv):
    addr, _ = srv
    _, r = call(addr, "POST", "/reservations", dict(GOOD, ttl_seconds=1))
    import time
    time.sleep(1.2)
    assert err(addr, "POST", f"/reservations/{r['id']}/commit") == (409, "reservation_expired")
    assert call(addr, "GET", "/items/A-1/availability")[1]["reserved"] == 0


@pytest.mark.parametrize("change, expected", [
    ({"quantity": 0}, (400, "invalid_quantity")),
    ({"quantity": "2"}, (400, "invalid_quantity")),
    ({"quantity": True}, (400, "invalid_quantity")),
    ({"holder": "  "}, (400, "invalid_holder")),
    ({"holder": 5}, (400, "invalid_holder")),
    ({"ttl_seconds": 0}, (400, "invalid_ttl")),
    ({"ttl_seconds": 86401}, (400, "invalid_ttl")),
    ({"sku": "nope"}, (404, "unknown_sku")),
    ({"quantity": 11}, (409, "insufficient_stock")),
    ({"extra": 1}, (400, "unknown_field")),
])
def test_reserve_refusals(srv, change, expected):
    addr, path = srv
    assert err(addr, "POST", "/reservations", dict(GOOD, **change)) == expected
    assert Store(path).availability("A-1")["reserved"] == 0


@pytest.mark.parametrize("missing", ["sku", "quantity", "holder"])
def test_missing_field(srv, missing):
    addr, _ = srv
    body = {k: v for k, v in GOOD.items() if k != missing}
    assert err(addr, "POST", "/reservations", body) == (400, "missing_field")


@pytest.mark.parametrize("body, expected", [
    (b"{not json", "invalid_json"),
    (b"\xff\xfe{}", "invalid_json"),
    (b'{"sku": "A-1", "quantity": NaN, "holder": "ann"}', "invalid_json"),
    (b'{"sku": "A-1", "quantity": Infinity, "holder": "ann"}', "invalid_json"),
    (b"[1, 2]", "invalid_body"),
    (b'"text"', "invalid_body"),
    (b'{"sku": "A-1", "quantity": 2.0, "holder": "ann"}', "invalid_quantity"),
    (b'{"sku": "A-1", "quantity": 2e0, "holder": "ann"}', "invalid_quantity"),
    (b'{"sku": "A-1", "quantity": 2, "holder": "ann", "ttl_seconds": 60.0}', "invalid_ttl"),
])
def test_strict_json(srv, body, expected):
    addr, path = srv
    assert err(addr, "POST", "/reservations", body) == (400, expected)
    assert Store(path).availability("A-1")["reserved"] == 0


def test_body_too_large(srv):
    addr, path = srv
    big = json.dumps(dict(GOOD, holder="x" * 70000)).encode()
    assert err(addr, "POST", "/reservations", big) == (413, "body_too_large")
    assert Store(path).availability("A-1")["reserved"] == 0


def test_length_of_exactly_the_limit_is_read(srv):
    addr, _ = srv
    body = json.dumps(GOOD).encode()
    body = body + b" " * (65536 - len(body))
    assert raw(addr, "POST", "/reservations", body)[0] == 201


@pytest.mark.parametrize("length", [None, "abc", "-1", "1.5", ""])
def test_invalid_length(srv, length):
    addr, _ = srv
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.putrequest("POST", "/reservations")
    if length is not None:
        conn.putheader("Content-Length", length)
    conn.endheaders()
    reply = conn.getresponse()
    payload = json.loads(reply.read())
    conn.close()
    assert (reply.status, payload["error"]) == (400, "invalid_length")


@pytest.mark.parametrize("rid", ["abc", "0", "-1", "+1", "1.0", "1" * 19, "%20"])
def test_invalid_id(srv, rid):
    addr, _ = srv
    assert err(addr, "GET", f"/reservations/{rid}") == (400, "invalid_id")
    assert err(addr, "DELETE", f"/reservations/{rid}") == (400, "invalid_id")
    assert err(addr, "POST", f"/reservations/{rid}/commit") == (400, "invalid_id")


def test_unknown_reservation_and_large_valid_id(srv):
    addr, _ = srv
    assert err(addr, "GET", "/reservations/999") == (404, "unknown_reservation")
    assert err(addr, "DELETE", "/reservations/999") == (404, "unknown_reservation")
    assert err(addr, "POST", "/reservations/999/commit") == (404, "unknown_reservation")
    assert err(addr, "GET", "/reservations/" + "9" * 18) == (404, "unknown_reservation")


def test_unknown_sku_availability_and_paths(srv):
    addr, _ = srv
    assert err(addr, "GET", "/items/nope/availability") == (404, "unknown_sku")
    assert err(addr, "GET", "/reservations/1/commit/extra") == (404, "not_found")
    assert err(addr, "GET", "/nothing") == (404, "not_found")


@pytest.mark.parametrize("method, path", [
    ("GET", "/reservations"), ("PUT", "/reservations"), ("DELETE", "/reservations"),
    ("PUT", "/reservations/1"), ("POST", "/reservations/1"),
    ("GET", "/reservations/1/commit"), ("DELETE", "/reservations/1/commit"),
    ("POST", "/items/A-1/availability"), ("DELETE", "/items/A-1/availability"),
])
def test_method_not_allowed(srv, method, path):
    addr, _ = srv
    body = b"{}" if method in ("POST", "PUT") else None
    assert err(addr, method, path, body) == (405, "method_not_allowed")


def test_seed_routes_unchanged(srv):
    addr, _ = srv
    assert call(addr, "GET", "/items") == (200, {"items": [{"sku": "A-1", "name": "Anvil", "quantity": 10}]})
    status, item = call(addr, "POST", "/items", {"sku": "B-2", "name": "Bucket", "quantity": 1})
    assert (status, item) == (201, {"sku": "B-2", "name": "Bucket", "quantity": 1})
    assert call(addr, "POST", "/items", {"sku": "B-2", "name": "Bucket", "quantity": 1})[0] == 409
