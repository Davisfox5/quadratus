import http.client
import json
import sqlite3
import threading

import pytest
from stockroom.api import make_server
from stockroom.store import Store

KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}


@pytest.fixture
def srv(tmp_path):
    path = tmp_path / "s.db"
    store = Store(path)
    store.add_item("A-1", "Anvil", 10)
    store.add_item("B-2", "Bolt", 4)
    server = make_server(path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address, store
    server.shutdown()
    server.server_close()


def raw(addr, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    reply = conn.getresponse()
    data = reply.read()
    conn.close()
    return reply.status, reply.getheader("Content-Type"), data


def bare(addr, method, path):
    """A request with no Content-Length header and no body."""
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.putrequest(method, path)
    conn.endheaders()
    reply = conn.getresponse()
    data = reply.read()
    conn.close()
    return reply.status, reply.getheader("Content-Type"), data


def call(addr, method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    status, ctype, data = raw(addr, method, path, body)
    return status, json.loads(data)


def checked(reply):
    status, ctype, data = reply
    payload = json.loads(data)
    assert isinstance(payload, dict) and set(payload) == {"error", "message"}, payload
    assert isinstance(payload["message"], str) and payload["message"].strip()
    assert ctype and ctype.split(";")[0].strip() == "application/json"
    return status, payload["error"]


def err(addr, method, path, body=None, headers=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    return checked(raw(addr, method, path, body, headers))


def snapshot(store):
    with sqlite3.connect(store.path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: db.execute(f"SELECT * FROM {n} ORDER BY 1").fetchall() for n in names}


def reserve(addr, quantity=3, holder="ann", ttl=600, sku="A-1"):
    status, r = call(addr, "POST", "/reservations",
                     {"sku": sku, "quantity": quantity, "holder": holder, "ttl_seconds": ttl})
    assert status == 201
    return r


# -- bug reports ------------------------------------------------------------

def test_bug_commit_of_released_is_409(srv):
    addr, _ = srv
    r = reserve(addr)
    assert call(addr, "DELETE", f"/reservations/{r['id']}")[0] == 200
    assert err(addr, "POST", f"/reservations/{r['id']}/commit") == (409, "reservation_released")


def test_bug_holder_of_64_over_http(srv):
    addr, _ = srv
    assert reserve(addr, holder="h" * 64)["holder"] == "h" * 64
    assert err(addr, "POST", "/reservations",
               {"sku": "A-1", "quantity": 1, "holder": "h" * 65}) == (400, "invalid_holder")


def test_bug_delete_of_expired_returns_it(srv):
    addr, store = srv
    r = store.reserve("A-1", 3, "ann", 1, now=100)
    status, out = call(addr, "DELETE", f"/reservations/{r['id']}")
    assert (status, out["state"]) == (200, "expired")
    assert call(addr, "GET", "/items/A-1/availability")[1]["reserved"] == 0


def test_bug_availability_over_http_ignores_unnoticed_expiry(srv):
    addr, store = srv
    store.reserve("A-1", 3, "ann", 1, now=100)
    assert call(addr, "GET", "/items/A-1/availability") == (
        200, {"sku": "A-1", "on_hand": 10, "reserved": 0, "available": 10})


# -- extend -----------------------------------------------------------------

def test_extend(srv):
    addr, store = srv
    r = reserve(addr, ttl=60)
    status, out = call(addr, "POST", f"/reservations/{r['id']}/extend", {"ttl_seconds": 5000})
    assert status == 200 and set(out) == KEYS and out["state"] == "active"
    assert 4900 < out["expires_at"] - r["expires_at"] < 5000
    assert call(addr, "GET", f"/reservations/{r['id']}") == (200, out)


@pytest.mark.parametrize("body,expected", [
    (b'{"ttl_seconds": 0}', (400, "invalid_ttl")),
    (b'{"ttl_seconds": 86401}', (400, "invalid_ttl")),
    (b'{"ttl_seconds": 60.0}', (400, "invalid_ttl")),
    (b'{"ttl_seconds": 6e1}', (400, "invalid_ttl")),
    (b'{"ttl_seconds": "60"}', (400, "invalid_ttl")),
    (b'{"ttl_seconds": true}', (400, "invalid_ttl")),
    (b'{}', (400, "missing_field")),
    (b'{"ttl_seconds": 60, "quantity": 1}', (400, "unknown_field")),
    (b'[60]', (400, "invalid_body")),
    (b'60', (400, "invalid_body")),
    (b'{"ttl_seconds": 60', (400, "invalid_json")),
    (b'{"ttl_seconds": NaN}', (400, "invalid_json")),
    (b'\xff\xfe', (400, "invalid_json")),
])
def test_extend_bad_bodies(srv, body, expected):
    addr, store = srv
    r = reserve(addr)
    before = snapshot(store)
    assert err(addr, "POST", f"/reservations/{r['id']}/extend", body) == expected
    assert snapshot(store) == before


def test_extend_needs_a_content_length(srv):
    addr, store = srv
    r = reserve(addr)
    before = snapshot(store)
    assert checked(bare(addr, "POST", f"/reservations/{r['id']}/extend")) == (400, "invalid_length")
    assert err(addr, "POST", f"/reservations/{r['id']}/extend", b"",
               {"Content-Length": "abc"}) == (400, "invalid_length")
    assert snapshot(store) == before


def test_extend_body_too_large(srv):
    addr, _ = srv
    r = reserve(addr)
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.putrequest("POST", f"/reservations/{r['id']}/extend")
    conn.putheader("Content-Length", "65537")
    conn.endheaders()
    reply = conn.getresponse()
    assert checked((reply.status, reply.getheader("Content-Type"), reply.read())) == (413, "body_too_large")
    conn.close()


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "1.5", "1" * 19, "+1"])
def test_extend_invalid_id(srv, bad):
    addr, _ = srv
    assert err(addr, "POST", f"/reservations/{bad}/extend", {"ttl_seconds": 60}) == (400, "invalid_id")


def test_extend_unknown_reservation(srv):
    addr, _ = srv
    assert err(addr, "POST", "/reservations/999/extend", {"ttl_seconds": 60}) == (404, "unknown_reservation")


def test_extend_refused_by_state(srv):
    addr, store = srv
    a, b = reserve(addr), reserve(addr)
    c = store.reserve("A-1", 1, "ann", 1, now=100)
    call(addr, "DELETE", f"/reservations/{a['id']}")
    call(addr, "POST", f"/reservations/{b['id']}/commit")
    body = {"ttl_seconds": 60}
    assert err(addr, "POST", f"/reservations/{a['id']}/extend", body) == (409, "reservation_released")
    assert err(addr, "POST", f"/reservations/{b['id']}/extend", body) == (409, "already_committed")
    assert err(addr, "POST", f"/reservations/{c['id']}/extend", body) == (409, "reservation_expired")


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_extend_other_methods(srv, method):
    addr, _ = srv
    r = reserve(addr)
    assert err(addr, method, f"/reservations/{r['id']}/extend") == (405, "method_not_allowed")


# -- commit with a body -----------------------------------------------------

def test_commit_without_content_length_commits_in_full(srv):
    addr, _ = srv
    r = reserve(addr, quantity=3)
    status, ctype, data = bare(addr, "POST", f"/reservations/{r['id']}/commit")
    assert status == 200 and json.loads(data)["state"] == "committed"
    assert call(addr, "GET", "/items/A-1/availability")[1]["on_hand"] == 7


def test_commit_with_zero_content_length_commits_in_full(srv):
    addr, _ = srv
    r = reserve(addr, quantity=3)
    status, ctype, data = raw(addr, "POST", f"/reservations/{r['id']}/commit", b"", {"Content-Length": "0"})
    assert status == 200 and json.loads(data)["state"] == "committed"


def test_partial_commit(srv):
    addr, _ = srv
    r = reserve(addr, quantity=5)
    status, out = call(addr, "POST", f"/reservations/{r['id']}/commit", {"quantity": 2})
    assert status == 200 and (out["state"], out["quantity"], out["expires_at"]) == ("active", 3, r["expires_at"])
    assert call(addr, "GET", "/items/A-1/availability") == (
        200, {"sku": "A-1", "on_hand": 8, "reserved": 3, "available": 5})
    status, out = call(addr, "POST", f"/reservations/{r['id']}/commit", {"quantity": 3})
    assert status == 200 and out["state"] == "committed"
    assert call(addr, "GET", "/items/A-1/availability")[1]["on_hand"] == 5


@pytest.mark.parametrize("body,expected", [
    (b'{"quantity": 0}', (400, "invalid_quantity")),
    (b'{"quantity": 6}', (400, "invalid_quantity")),
    (b'{"quantity": 2.0}', (400, "invalid_quantity")),
    (b'{"quantity": 2e0}', (400, "invalid_quantity")),
    (b'{"quantity": "2"}', (400, "invalid_quantity")),
    (b'{"quantity": true}', (400, "invalid_quantity")),
    (b'{}', (400, "missing_field")),
    (b'{"quantity": 2, "holder": "ann"}', (400, "unknown_field")),
    (b'[2]', (400, "invalid_body")),
    (b'{"quantity": 2', (400, "invalid_json")),
    (b'{"quantity": Infinity}', (400, "invalid_json")),
])
def test_commit_bad_bodies(srv, body, expected):
    addr, store = srv
    r = reserve(addr, quantity=5)
    before = snapshot(store)
    assert err(addr, "POST", f"/reservations/{r['id']}/commit", body) == expected
    assert snapshot(store) == before


def test_commit_bad_content_length(srv):
    addr, store = srv
    r = reserve(addr)
    before = snapshot(store)
    assert err(addr, "POST", f"/reservations/{r['id']}/commit", b"",
               {"Content-Length": "abc"}) == (400, "invalid_length")
    assert snapshot(store) == before


def test_partial_commit_refused_by_state(srv):
    addr, _ = srv
    r = reserve(addr)
    call(addr, "POST", f"/reservations/{r['id']}/commit")
    assert err(addr, "POST", f"/reservations/{r['id']}/commit", {"quantity": 1}) == (409, "already_committed")


# -- adjust -----------------------------------------------------------------

def test_adjust(srv):
    addr, _ = srv
    assert call(addr, "POST", "/items/A-1/adjust", {"delta": 5}) == (
        200, {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15})
    assert call(addr, "POST", "/items/A-1/adjust", {"delta": -15}) == (
        200, {"sku": "A-1", "on_hand": 0, "reserved": 0, "available": 0})


@pytest.mark.parametrize("body,expected", [
    (b'{"delta": 0}', (400, "invalid_quantity")),
    (b'{"delta": 1.0}', (400, "invalid_quantity")),
    (b'{"delta": -1e0}', (400, "invalid_quantity")),
    (b'{"delta": "1"}', (400, "invalid_quantity")),
    (b'{"delta": null}', (400, "invalid_quantity")),
    (b'{"delta": -8}', (409, "insufficient_stock")),
    (b'{}', (400, "missing_field")),
    (b'{"delta": 1, "sku": "A-1"}', (400, "unknown_field")),
    (b'"1"', (400, "invalid_body")),
    (b'{"delta": 1,}', (400, "invalid_json")),
    (b'{"delta": -Infinity}', (400, "invalid_json")),
])
def test_adjust_bad_bodies(srv, body, expected):
    addr, store = srv
    reserve(addr, quantity=3)
    before = snapshot(store)
    assert err(addr, "POST", "/items/A-1/adjust", body) == expected
    assert snapshot(store) == before


def test_adjust_needs_a_content_length(srv):
    addr, store = srv
    before = snapshot(store)
    assert checked(bare(addr, "POST", "/items/A-1/adjust")) == (400, "invalid_length")
    assert snapshot(store) == before


def test_adjust_unknown_sku(srv):
    addr, _ = srv
    assert err(addr, "POST", "/items/nope/adjust", {"delta": 1}) == (404, "unknown_sku")


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_adjust_other_methods(srv, method):
    addr, _ = srv
    assert err(addr, method, "/items/A-1/adjust") == (405, "method_not_allowed")


# -- listing ----------------------------------------------------------------

def test_list(srv):
    addr, store = srv
    a = reserve(addr, 1, "ann")
    b = reserve(addr, 1, "bob")
    c = reserve(addr, 1, "ann b")
    reserve(addr, 1, "ann", sku="B-2")
    call(addr, "DELETE", f"/reservations/{b['id']}")
    status, out = call(addr, "GET", "/items/A-1/reservations")
    assert status == 200 and set(out) == {"reservations"}
    assert [r["id"] for r in out["reservations"]] == [a["id"], b["id"], c["id"]]
    assert [r["state"] for r in out["reservations"]] == ["active", "released", "active"]
    assert all(set(r) == KEYS for r in out["reservations"])
    assert call(addr, "GET", "/items/B-2/reservations")[1]["reservations"][0]["sku"] == "B-2"


def test_list_filters(srv):
    addr, store = srv
    a = reserve(addr, 1, "ann")
    b = reserve(addr, 1, "bob")
    c = reserve(addr, 1, "ann b")
    call(addr, "DELETE", f"/reservations/{a['id']}")

    def ids(query):
        return [r["id"] for r in call(addr, "GET", "/items/A-1/reservations?" + query)[1]["reservations"]]

    assert ids("state=active") == [b["id"], c["id"]]
    assert ids("state=released") == [a["id"]]
    assert ids("holder=ann") == [a["id"]]
    assert ids("holder=ann%20b") == [c["id"]]
    assert ids("holder=ann&state=active") == []
    assert ids("state=active&holder=bob") == [b["id"]]
    assert ids("state=committed") == []


def test_list_sees_expiry(srv):
    addr, store = srv
    store.reserve("A-1", 1, "ann", 1, now=100)
    out = call(addr, "GET", "/items/A-1/reservations?state=expired")[1]["reservations"]
    assert len(out) == 1 and out[0]["state"] == "expired"


@pytest.mark.parametrize("query,expected", [
    ("state=gone", (400, "invalid_state")),
    ("state=ACTIVE", (400, "invalid_state")),
    ("sku=A-1", (400, "unknown_field")),
    ("limit=5", (400, "unknown_field")),
    ("state=active&state=released", (400, "unknown_field")),
    ("holder=ann&holder=ann", (400, "unknown_field")),
    ("holder=" + "h" * 65, (400, "invalid_holder")),
])
def test_list_bad_queries(srv, query, expected):
    addr, _ = srv
    assert err(addr, "GET", "/items/A-1/reservations?" + query) == expected


def test_list_unknown_sku(srv):
    addr, _ = srv
    assert err(addr, "GET", "/items/nope/reservations") == (404, "unknown_sku")


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_list_other_methods(srv, method):
    addr, _ = srv
    assert err(addr, method, "/items/A-1/reservations") == (405, "method_not_allowed")


# -- what must not change ---------------------------------------------------

def test_old_routes_keep_their_answers(srv):
    addr, _ = srv
    assert err(addr, "GET", "/reservations") == (405, "method_not_allowed")
    assert err(addr, "GET", "/items/A-1/other") == (404, "not_found")
    assert err(addr, "GET", "/reservations/1/other") == (404, "not_found")
    assert checked(bare(addr, "POST", "/reservations")) == (400, "invalid_length")
    r = reserve(addr)
    assert call(addr, "GET", f"/reservations/{r['id']}") == (200, r)
    status, items = call(addr, "GET", "/items")
    assert status == 200 and items["items"][0] == {"sku": "A-1", "name": "Anvil", "quantity": 10}
