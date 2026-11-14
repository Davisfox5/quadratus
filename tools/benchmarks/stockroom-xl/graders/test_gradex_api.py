"""Graders for goal items 31 to 35: /events, adjustments, /sweep, /bulk-reservations, Idempotency-Key."""

import contextlib
import http.client
import json
import sqlite3
import threading
import time

import pytest
from stockroom.api import make_server
from stockroom.store import Store

KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}
EVENT_KEYS = {"id", "at", "kind", "sku", "quantity", "reservation_id", "holder"}
ADJUST = "/items/A-1/adjustments"
SWEEP = "/sweep"
BULK = "/bulk-reservations"


@pytest.fixture
def srv(tmp_path):
    path = tmp_path / "s.db"
    store = Store(path)
    store.add_item("A-1", "Anvil", 10)
    store.add_item("B-2", "Bucket", 5)
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


def call(addr, method, path, payload=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode()
    status, _ctype, data = raw(addr, method, path, body, headers)
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


def announce(addr, path, length):
    """POST with the given Content-Length header (or none) and no body at all."""
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.putrequest("POST", path)
    if length is not None:
        conn.putheader("Content-Length", length)
    conn.endheaders()
    reply = conn.getresponse()
    payload = json.loads(reply.read())
    conn.close()
    return reply.status, payload["error"]


def frozen(path):
    """Every row of every table, whatever the tables are, and the event log."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = {n: sorted(db.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr) for n in names}
    return rows, Store(path).events(limit=500)


# ---- item 31: GET /events -----------------------------------------------------------------------

def test_events_route_returns_the_log(srv):
    addr, path = srv
    r = Store(path).reserve("A-1", 2, "ann", 60, now=1000)
    status, body = call(addr, "GET", "/events")
    assert status == 200 and set(body) == {"events"}
    events = body["events"]
    assert events == Store(path).events()
    assert [e["kind"] for e in events] == ["item_added", "item_added", "reserved"]
    assert all(set(e) == EVENT_KEYS for e in events)
    assert (events[0]["sku"], events[0]["quantity"], events[0]["reservation_id"], events[0]["holder"]) == (
        "A-1", 10, None, None)
    assert (events[2]["sku"], events[2]["quantity"], events[2]["reservation_id"], events[2]["holder"]) == (
        "A-1", 2, r["id"], "ann")


def test_events_route_filters(srv):
    addr, path = srv
    store = Store(path)
    r = store.reserve("B-2", 1, "bob", 60, now=1000)
    store.release(r["id"], now=1001)
    store.adjust("A-1", 1, "restock", now=1002)
    everything = store.events()
    assert len(everything) == 5

    def get(query):
        status, body = call(addr, "GET", "/events?" + query)
        assert status == 200 and set(body) == {"events"}
        return body["events"]

    assert get("sku=B-2") == [e for e in everything if e["sku"] == "B-2"]
    assert get("kind=released") == [e for e in everything if e["kind"] == "released"]
    assert get(f"after={everything[1]['id']}") == everything[2:]
    assert get("after=0") == everything
    assert get("limit=2") == everything[:2]
    assert get("limit=500") == everything
    assert get(f"sku=B-2&kind=reserved&after={everything[0]['id']}&limit=5") == [everything[2]]
    assert get("sku=nope") == []
    assert get("after=" + "9" * 18) == []


EVENT_QUERIES = {
    "invalid_kind": ["kind=bogus", "kind=Reserved"],
    "invalid_cursor": ["after=abc", "after=-1", "after=1.5", "after=%2B1", "after=" + "1" * 19],
    "invalid_limit": ["limit=abc", "limit=0", "limit=501", "limit=-5", "limit=2.0", "limit=" + "1" * 19],
    "unknown_parameter": ["colour=red", "after_id=1", "sku=A-1&offset=2"],
    "duplicate_parameter": ["limit=1&limit=2", "sku=A-1&sku=B-2", "after=1&kind=reserved&after=1"],
}


@pytest.mark.parametrize("expected", list(EVENT_QUERIES))
def test_events_route_refusals(srv, expected):
    addr, path = srv
    before = frozen(path)
    for query in EVENT_QUERIES[expected]:
        assert err(addr, "GET", "/events?" + query) == (400, expected), query
    assert frozen(path) == before


@pytest.mark.parametrize("query, expected", [
    ("kind=", "invalid_kind"), ("after=", "invalid_cursor"), ("limit=", "invalid_limit")])
def test_events_route_refuses_an_empty_value(srv, query, expected):
    addr, _ = srv
    assert err(addr, "GET", "/events?" + query) == (400, expected)


# ---- item 32: the three new POST routes ---------------------------------------------------------

def test_adjustment_route(srv):
    addr, path = srv
    assert call(addr, "POST", ADJUST, {"delta": 5, "reason": "restock"}) == (
        200, {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15})
    Store(path).reserve("A-1", 4, "ann", 600)
    assert call(addr, "POST", ADJUST, {"reason": "damage", "delta": -3}) == (
        200, {"sku": "A-1", "on_hand": 12, "reserved": 4, "available": 8})
    assert Store(path).get_item("A-1")["quantity"] == 12
    assert [e["quantity"] for e in Store(path).events(kind="adjusted")] == [5, -3]
    assert call(addr, "POST", "/items/B-2/adjustments", {"delta": -5, "reason": "correction"}) == (
        200, {"sku": "B-2", "on_hand": 0, "reserved": 0, "available": 0})
    # A body of exactly 65536 bytes is still read.
    body = json.dumps({"delta": 1, "reason": "restock"}).encode()
    assert raw(addr, "POST", ADJUST, body + b" " * (65536 - len(body)))[0] == 200
    assert Store(path).get_item("A-1")["quantity"] == 13


@pytest.mark.parametrize("body, expected", [
    ({"delta": 0, "reason": "restock"}, (400, "invalid_delta")),
    ({"delta": "2", "reason": "restock"}, (400, "invalid_delta")),
    ({"delta": 2, "reason": "theft"}, (400, "invalid_reason")),
    ({"delta": -11, "reason": "damage"}, (409, "insufficient_stock")),
    ({"delta": 2, "reason": "restock", "note": "x"}, (400, "unknown_field")),
    ({"reason": "restock"}, (400, "missing_field")),
    ({"delta": 2}, (400, "missing_field")),
])
def test_adjustment_refusals(srv, body, expected):
    addr, path = srv
    before = frozen(path)
    assert err(addr, "POST", ADJUST, body) == expected
    assert frozen(path) == before


def test_adjustment_unknown_sku_and_stock_reserved(srv):
    addr, path = srv
    Store(path).reserve("A-1", 4, "ann", 600)
    before = frozen(path)
    assert err(addr, "POST", "/items/nope/adjustments", {"delta": 1, "reason": "restock"}) == (404, "unknown_sku")
    assert err(addr, "POST", ADJUST, {"delta": -7, "reason": "damage"}) == (409, "stock_reserved")
    assert frozen(path) == before
    assert call(addr, "POST", ADJUST, {"delta": -6, "reason": "damage"}) == (
        200, {"sku": "A-1", "on_hand": 4, "reserved": 4, "available": 0})


def test_sweep_route(srv):
    addr, path = srv
    store = Store(path)
    old = [store.reserve("A-1", 2, "ann", 60, now=1000), store.reserve("B-2", 1, "bob", 60, now=1000)]
    live = store.reserve("A-1", 1, "cy", 600)
    assert call(addr, "POST", SWEEP, {}) == (200, {"expired": 2})
    assert call(addr, "POST", SWEEP, {}) == (200, {"expired": 0})
    assert [e["reservation_id"] for e in Store(path).events(kind="expired")] == sorted(r["id"] for r in old)
    assert call(addr, "GET", f"/reservations/{old[0]['id']}")[1]["state"] == "expired"
    assert call(addr, "GET", f"/reservations/{live['id']}")[1]["state"] == "active"


def test_bulk_reservation_route(srv):
    addr, path = srv
    payload = {"lines": [["B-2", 1], ["A-1", 2]], "holder": " ann ", "ttl_seconds": 60}
    status, body = call(addr, "POST", BULK, payload)
    assert status == 201 and set(body) == {"reservations"}
    made = body["reservations"]
    assert [(r["sku"], r["quantity"], r["holder"], r["state"]) for r in made] == [
        ("B-2", 1, "ann", "active"), ("A-1", 2, "ann", "active")]
    assert all(set(r) == KEYS for r in made)
    assert made[0]["expires_at"] == made[1]["expires_at"]
    assert 0 < made[0]["expires_at"] - time.time() <= 60
    for r in made:
        assert call(addr, "GET", f"/reservations/{r['id']}") == (200, r)
    assert call(addr, "GET", "/items/A-1/availability")[1]["reserved"] == 2
    reserved = Store(path).events(kind="reserved")
    assert [(e["sku"], e["reservation_id"]) for e in reserved] == [(r["sku"], r["id"]) for r in made]
    # ttl_seconds omitted is 900.
    status, body = call(addr, "POST", BULK, {"lines": [["A-1", 1]], "holder": "ann"})
    assert status == 201
    assert 800 < body["reservations"][0]["expires_at"] - time.time() <= 900


GOOD_BULK = {"lines": [["A-1", 2], ["B-2", 1]], "holder": "ann", "ttl_seconds": 60}


@pytest.mark.parametrize("change, expected", [
    ({"lines": []}, (400, "invalid_lines")),
    ({"lines": [["A-1"]]}, (400, "invalid_lines")),
    ({"lines": [["A-1", 1], ["A-1", 1]]}, (400, "invalid_lines")),
    ({"lines": [[f"S{i}", 1] for i in range(21)]}, (400, "invalid_lines")),
    ({"lines": [["A-1", 1], ["nope", 1]]}, (404, "unknown_sku")),
    ({"lines": [["A-1", 0]]}, (400, "invalid_quantity")),
    ({"holder": "  "}, (400, "invalid_holder")),
    ({"ttl_seconds": 0}, (400, "invalid_ttl")),
    ({"lines": [["A-1", 2], ["B-2", 6]]}, (409, "insufficient_stock")),
    ({"extra": 1}, (400, "unknown_field")),
    ({"lines": ...}, (400, "missing_field")),
    ({"holder": ...}, (400, "missing_field")),
])
def test_bulk_reservation_refusals(srv, change, expected):
    addr, path = srv
    before = frozen(path)
    body = {k: v for k, v in dict(GOOD_BULK, **change).items() if v is not ...}  # ... drops the field
    assert err(addr, "POST", BULK, body) == expected
    assert frozen(path) == before
    assert Store(path).availability("A-1")["reserved"] == 0


# ---- item 33: the request-body rules on the three routes ----------------------------------------

@pytest.mark.parametrize("path, body, expected", [
    (ADJUST, b"{not json", "invalid_json"),
    (ADJUST, b'{"delta": NaN, "reason": "restock"}', "invalid_json"),
    (ADJUST, b'[2, "restock"]', "invalid_body"),
    (ADJUST, b'{"delta": 2.0, "reason": "restock"}', "invalid_delta"),
    (SWEEP, b"\xff\xfe{}", "invalid_json"),
    (SWEEP, b"-Infinity", "invalid_json"),
    (SWEEP, b"[]", "invalid_body"),
    (SWEEP, b"null", "invalid_body"),
    (SWEEP, b'{"now": 5}', "unknown_field"),
    (BULK, b'{"lines": [["A-1", Infinity]], "holder": "ann"}', "invalid_json"),
    (BULK, b'[["A-1", 1]]', "invalid_body"),
    (BULK, b'{"lines": [["A-1", 2e0]], "holder": "ann"}', "invalid_quantity"),
    (BULK, b'{"lines": [["A-1", 2]], "holder": "ann", "ttl_seconds": 60.0}', "invalid_ttl"),
])
def test_strict_bodies(srv, path, body, expected):
    addr, db = srv
    if path == SWEEP:  # something a wrongly accepted sweep would record
        Store(db).reserve("A-1", 1, "old", 60, now=1000)
    before = frozen(db)
    assert err(addr, "POST", path, body) == (400, expected)
    assert frozen(db) == before


@pytest.mark.parametrize("path", [ADJUST, SWEEP, BULK])
def test_content_length_rules(srv, path):
    addr, db = srv
    if path == SWEEP:  # something a wrongly accepted sweep would record
        Store(db).reserve("A-1", 1, "old", 60, now=1000)
    before = frozen(db)
    for length in (None, "abc", "-1", "1.5"):
        assert announce(addr, path, length) == (400, "invalid_length"), length
    # Too large: answered from the header alone, without waiting for a body.
    assert announce(addr, path, "65537") == (413, "body_too_large")
    assert frozen(db) == before


@pytest.mark.parametrize("path", [ADJUST, SWEEP, BULK, "/events"])
def test_other_methods_are_not_allowed(srv, path):
    addr, db = srv
    before = frozen(db)
    for method in ("GET", "PUT", "DELETE", "POST"):
        if method == ("GET" if path == "/events" else "POST"):
            continue
        body = b"{}" if method in ("POST", "PUT") else None
        assert err(addr, method, path, body) == (405, "method_not_allowed"), method
    assert frozen(db) == before


# ---- item 34: the Idempotency-Key header --------------------------------------------------------

RESERVE = {"sku": "A-1", "quantity": 2, "holder": "ann", "ttl_seconds": 60}


def test_replayed_reservation_gets_the_same_status_and_body(srv):
    addr, path = srv
    first = call(addr, "POST", "/reservations", RESERVE, {"Idempotency-Key": "key-1"})
    assert first[0] == 201 and first[1]["state"] == "active"
    before = frozen(path)
    assert call(addr, "POST", "/reservations", RESERVE, {"Idempotency-Key": "key-1"}) == first
    assert frozen(path) == before
    assert Store(path).availability("A-1")["reserved"] == 2
    other = call(addr, "POST", "/reservations", RESERVE, {"Idempotency-Key": "key-2"})
    assert other[0] == 201 and other[1]["id"] != first[1]["id"]


def test_replay_after_release_still_gets_the_first_body(srv):
    addr, path = srv
    first = call(addr, "POST", "/reservations", RESERVE, {"Idempotency-Key": "key-1"})
    assert call(addr, "DELETE", f"/reservations/{first[1]['id']}")[1]["state"] == "released"
    assert call(addr, "POST", "/reservations", RESERVE, {"Idempotency-Key": "key-1"}) == first
    assert Store(path).availability("A-1")["reserved"] == 0


def test_key_reused_for_another_request_conflicts(srv):
    addr, path = srv
    call(addr, "POST", "/reservations", RESERVE, {"Idempotency-Key": "key-1"})
    before = frozen(path)
    key = {"Idempotency-Key": "key-1"}
    assert err(addr, "POST", "/reservations", dict(RESERVE, quantity=3), key) == (409, "idempotency_conflict")
    assert err(addr, "POST", "/reservations", dict(RESERVE, sku="nope"), key) == (409, "idempotency_conflict")
    assert err(addr, "POST", ADJUST, {"delta": 2, "reason": "restock"}, key) == (409, "idempotency_conflict")
    assert frozen(path) == before


@pytest.mark.parametrize("key", ["a b", "x" * 65, "k!"])
def test_invalid_key_header(srv, key):
    addr, path = srv
    before = frozen(path)
    header = {"Idempotency-Key": key}
    assert err(addr, "POST", "/reservations", RESERVE, header) == (400, "invalid_idempotency_key")
    assert err(addr, "POST", "/reservations", dict(RESERVE, sku="nope"), header) == (400, "invalid_idempotency_key")
    assert err(addr, "POST", ADJUST, {"delta": 2, "reason": "restock"}, header) == (400, "invalid_idempotency_key")
    assert frozen(path) == before


def test_replayed_adjustment_is_applied_once(srv):
    addr, path = srv
    key = {"Idempotency-Key": "x" * 64}
    first = call(addr, "POST", ADJUST, {"delta": 5, "reason": "restock"}, key)
    assert first == (200, {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15})
    before = frozen(path)
    assert call(addr, "POST", ADJUST, {"delta": 5, "reason": "restock"}, key) == first
    assert frozen(path) == before
    assert call(addr, "POST", ADJUST, {"delta": -4, "reason": "damage"})[1]["on_hand"] == 11
    assert call(addr, "POST", ADJUST, {"delta": 5, "reason": "restock"}, key) == first
    assert Store(path).get_item("A-1")["quantity"] == 11
    assert err(addr, "POST", ADJUST, {"delta": 6, "reason": "restock"}, key) == (409, "idempotency_conflict")
    assert err(addr, "POST", "/reservations", RESERVE, key) == (409, "idempotency_conflict")


def test_key_header_is_ignored_on_other_routes(srv):
    addr, path = srv
    key = {"Idempotency-Key": "key-1"}
    bulk = {"lines": [["A-1", 1]], "holder": "ann", "ttl_seconds": 60}
    one = call(addr, "POST", BULK, bulk, key)
    two = call(addr, "POST", BULK, bulk, key)
    assert one[0] == two[0] == 201
    assert one[1]["reservations"][0]["id"] != two[1]["reservations"][0]["id"]
    assert Store(path).availability("A-1")["reserved"] == 2
    bad = {"Idempotency-Key": "not a key!"}
    assert call(addr, "POST", BULK, bulk, bad)[0] == 201
    assert call(addr, "POST", SWEEP, {}, bad) == (200, {"expired": 0})
    assert call(addr, "GET", "/events", None, bad)[0] == 200
    assert call(addr, "GET", "/items/A-1/availability", None, bad)[1]["reserved"] == 3
    rid = one[1]["reservations"][0]["id"]
    assert call(addr, "GET", f"/reservations/{rid}", None, bad)[0] == 200
    assert call(addr, "DELETE", f"/reservations/{rid}", None, bad)[1]["state"] == "released"
    assert call(addr, "POST", f"/reservations/{two[1]['reservations'][0]['id']}/commit", None, bad)[1][
        "state"] == "committed"
    # The same key was never remembered by the routes that ignore it.
    assert call(addr, "POST", "/reservations", RESERVE, key)[0] == 201


# ---- items 20 and 35 over HTTP ------------------------------------------------------------------

def test_requests_record_the_events_of_what_they_did(srv):
    addr, path = srv

    def kinds():
        return [(e["kind"], e["sku"]) for e in Store(path).events()[2:]]

    assert call(addr, "POST", "/items", {"sku": "C-3", "name": "Crate", "quantity": 4})[0] == 201
    a = call(addr, "POST", "/reservations", RESERVE)[1]
    b = call(addr, "POST", "/reservations", dict(RESERVE, sku="C-3"))[1]
    call(addr, "DELETE", f"/reservations/{a['id']}")
    call(addr, "DELETE", f"/reservations/{a['id']}")
    call(addr, "POST", f"/reservations/{b['id']}/commit")
    call(addr, "GET", f"/reservations/{b['id']}")
    call(addr, "GET", "/items/C-3/availability")
    assert call(addr, "POST", "/items", {"sku": "C-3", "name": "Crate", "quantity": 4})[0] == 409
    assert err(addr, "POST", f"/reservations/{b['id']}/commit") == (409, "already_committed")
    assert kinds() == [("item_added", "C-3"), ("reserved", "A-1"), ("reserved", "C-3"), ("released", "A-1"),
                       ("committed", "C-3")]
    last = Store(path).events()[-1]
    assert (last["quantity"], last["reservation_id"], last["holder"]) == (2, b["id"], "ann")
