"""Graders for goal items 36 and 37: adjust, sweep, events, reserve-many and --key."""

import contextlib
import json
import sqlite3
import time

import pytest
from stockroom.cli import main
from stockroom.store import Store

KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "s.db")
    store = Store(path)
    store.add_item("A-1", "Anvil", 10)
    store.add_item("B-2", "Bucket", 5)
    return path


def ok(capsys, db, *argv):
    capsys.readouterr()
    assert main(["--db", db, *argv]) == 0
    out = capsys.readouterr()
    assert out.err == "" and out.out.endswith("\n") and out.out.count("\n") == 1
    return json.loads(out.out)


def bad(capsys, db, *argv):
    capsys.readouterr()
    status = main(["--db", db, *argv])
    out = capsys.readouterr()
    assert out.out == "" and out.err.endswith("\n") and out.err.count("\n") == 1, out
    payload = json.loads(out.err)
    assert set(payload) == {"error", "message"} and payload["message"].strip()
    return status, payload["error"]


def snapshot(db):
    """Every row of every table, whatever the tables are."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: sorted(conn.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr) for n in names}


def refused(result, code):
    """The store's refusal, or the command line's own: the goal allows a command to reject it first."""
    return result in ((1, code), (2, "usage"))


# ---- adjust -------------------------------------------------------------------------------------

def test_adjust_prints_the_availability_object(capsys, db):
    assert ok(capsys, db, "adjust", "A-1", "5", "restock") == {
        "sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15}
    Store(db).reserve("A-1", 4, "ann", 600)
    assert ok(capsys, db, "adjust", "A-1", "-3", "damage") == {
        "sku": "A-1", "on_hand": 12, "reserved": 4, "available": 8}
    assert Store(db).get_item("A-1")["quantity"] == 12
    assert [e["quantity"] for e in Store(db).events(kind="adjusted")] == [5, -3]


def test_adjust_store_refusals_exit_1(capsys, db):
    Store(db).reserve("A-1", 4, "ann", 600)
    rows = snapshot(db)
    assert bad(capsys, db, "adjust", "nope", "1", "restock") == (1, "unknown_sku")
    assert bad(capsys, db, "adjust", "A-1", "0", "restock") == (1, "invalid_delta")
    assert bad(capsys, db, "adjust", "A-1", "-11", "damage") == (1, "insufficient_stock")
    assert bad(capsys, db, "adjust", "A-1", "-7", "damage") == (1, "stock_reserved")
    assert snapshot(db) == rows


def test_adjust_with_an_unknown_reason_fails(capsys, db):
    rows = snapshot(db)
    assert refused(bad(capsys, db, "adjust", "A-1", "1", "theft"), "invalid_reason")
    assert snapshot(db) == rows


def test_adjust_key_replays_and_conflicts(capsys, db):
    first = ok(capsys, db, "adjust", "A-1", "5", "restock", "--key", "k1")
    assert first == {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15}
    assert ok(capsys, db, "adjust", "A-1", "5", "restock", "--key", "k1") == first
    assert Store(db).get_item("A-1")["quantity"] == 15
    assert bad(capsys, db, "adjust", "A-1", "6", "restock", "--key", "k1") == (1, "idempotency_conflict")
    assert bad(capsys, db, "reserve", "A-1", "1", "ann", "--key", "k1") == (1, "idempotency_conflict")
    assert ok(capsys, db, "adjust", "A-1", "5", "restock", "--key", "k2")["on_hand"] == 20


# ---- reserve --key ------------------------------------------------------------------------------

def test_reserve_key_replays_and_conflicts(capsys, db):
    first = ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60", "--key", "k1")
    assert set(first) == KEYS and first["state"] == "active"
    assert ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60", "--key", "k1") == first
    assert Store(db).availability("A-1")["reserved"] == 2
    assert bad(capsys, db, "reserve", "A-1", "3", "ann", "--ttl", "60", "--key", "k1") == (
        1, "idempotency_conflict")
    assert ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60", "--key", "k2")["id"] != first["id"]
    assert ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60")["id"] != first["id"]
    assert Store(db).availability("A-1")["reserved"] == 6


def test_an_invalid_key_fails(capsys, db):
    rows = snapshot(db)
    assert refused(bad(capsys, db, "reserve", "A-1", "2", "ann", "--key", "not a key"), "invalid_idempotency_key")
    assert refused(bad(capsys, db, "adjust", "A-1", "2", "restock", "--key", "k!"), "invalid_idempotency_key")
    assert snapshot(db) == rows


# ---- sweep --------------------------------------------------------------------------------------

def test_sweep_prints_how_many_expired(capsys, db):
    assert ok(capsys, db, "sweep") == {"expired": 0}
    store = Store(db)
    old = [store.reserve("A-1", 2, "ann", 60, now=1000), store.reserve("B-2", 1, "bob", 60, now=1000)]
    live = store.reserve("A-1", 1, "cy", 600)
    assert ok(capsys, db, "sweep") == {"expired": 2}
    assert ok(capsys, db, "sweep") == {"expired": 0}
    assert ok(capsys, db, "show", str(old[0]["id"]))["state"] == "expired"
    assert ok(capsys, db, "show", str(live["id"]))["state"] == "active"
    assert [e["reservation_id"] for e in Store(db).events(kind="expired")] == sorted(r["id"] for r in old)


# ---- events -------------------------------------------------------------------------------------

@pytest.fixture
def logged(db):
    store = Store(db)
    r = store.reserve("B-2", 1, "bob", 60, now=1000)
    store.release(r["id"], now=1001)
    store.adjust("A-1", 1, "restock", now=1002)
    return db, store.events()


def test_events_prints_the_log(capsys, logged):
    db, everything = logged
    assert len(everything) == 5
    assert ok(capsys, db, "events") == {"events": everything}


def test_events_options_filter_the_log(capsys, logged):
    db, everything = logged
    assert ok(capsys, db, "events", "--sku", "B-2") == {"events": [e for e in everything if e["sku"] == "B-2"]}
    assert ok(capsys, db, "events", "--kind", "released") == {
        "events": [e for e in everything if e["kind"] == "released"]}
    assert ok(capsys, db, "events", "--after", str(everything[1]["id"])) == {"events": everything[2:]}
    assert ok(capsys, db, "events", "--limit", "2") == {"events": everything[:2]}
    assert ok(capsys, db, "events", "--sku", "B-2", "--kind", "reserved", "--after", "0", "--limit", "5") == {
        "events": [everything[2]]}
    assert ok(capsys, db, "events", "--sku", "nope") == {"events": []}


def test_events_store_refusals_exit_1(capsys, logged):
    db, _ = logged
    rows = snapshot(db)
    assert bad(capsys, db, "events", "--limit", "0") == (1, "invalid_limit")
    assert bad(capsys, db, "events", "--limit", "501") == (1, "invalid_limit")
    assert refused(bad(capsys, db, "events", "--kind", "bogus"), "invalid_kind")
    assert snapshot(db) == rows


# ---- reserve-many -------------------------------------------------------------------------------

def test_reserve_many_prints_the_reservations(capsys, db):
    body = ok(capsys, db, "reserve-many", "ann", "B-2=1", "A-1=2", "--ttl", "60")
    assert set(body) == {"reservations"}
    made = body["reservations"]
    assert [(r["sku"], r["quantity"], r["holder"], r["state"]) for r in made] == [
        ("B-2", 1, "ann", "active"), ("A-1", 2, "ann", "active")]
    assert all(set(r) == KEYS for r in made)
    assert made[0]["expires_at"] == made[1]["expires_at"]
    assert 0 < made[0]["expires_at"] - time.time() <= 60
    for r in made:
        assert ok(capsys, db, "show", str(r["id"])) == r
    assert ok(capsys, db, "availability", "A-1") == {"sku": "A-1", "on_hand": 10, "reserved": 2, "available": 8}


def test_reserve_many_works_without_ttl(capsys, db):
    made = ok(capsys, db, "reserve-many", "bob", "A-1=1")["reservations"]
    assert [(r["sku"], r["quantity"], r["holder"], r["state"]) for r in made] == [("A-1", 1, "bob", "active")]


def test_reserve_many_store_refusals_exit_1(capsys, db):
    rows = snapshot(db)
    assert bad(capsys, db, "reserve-many", "ann", "A-1=1", "nope=1") == (1, "unknown_sku")
    assert bad(capsys, db, "reserve-many", "ann", "A-1=0") == (1, "invalid_quantity")
    assert bad(capsys, db, "reserve-many", "ann", "A-1=11") == (1, "insufficient_stock")
    assert bad(capsys, db, "reserve-many", "ann", "A-1=1", "B-2=6") == (1, "insufficient_stock")
    assert bad(capsys, db, "reserve-many", "  ", "A-1=1") == (1, "invalid_holder")
    assert bad(capsys, db, "reserve-many", "ann", "A-1=1", "--ttl", "0") == (1, "invalid_ttl")
    assert bad(capsys, db, "reserve-many", "ann", "A-1=1", "A-1=2") == (1, "invalid_lines")
    assert bad(capsys, db, "reserve-many", "ann", *[f"S{i}=1" for i in range(21)]) == (1, "invalid_lines")
    assert snapshot(db) == rows
    assert Store(db).availability("A-1")["reserved"] == 0


# ---- item 37: what the command line itself rejects ----------------------------------------------

@pytest.mark.parametrize("argv", [
    ["adjust", "A-1", "abc", "restock"],
    ["adjust", "A-1", "--3", "damage"],
    ["adjust", "A-1", "1_0", "restock"],
    ["adjust", "A-1", "3"],
    ["adjust", "A-1", "3", "restock", "extra"],
    ["adjust", "A-1", "3", "restock", "--key"],
    ["sweep", "extra"],
    ["events", "--after", "x"],
    ["events", "--limit", "2.0"],
    ["events", "--limit", "1_0"],
    ["events", "--sku"],
    ["events", "extra"],
    ["reserve-many"],
    ["reserve-many", "ann"],
    ["reserve-many", "ann", "A-1"],
    ["reserve-many", "ann", "=2"],
    ["reserve-many", "ann", "A-1="],
    ["reserve-many", "ann", "A-1=2.0"],
    ["reserve-many", "ann", "A-1=1_0"],
    ["reserve-many", "ann", "A-1=2", "B-2"],
    ["reserve-many", "ann", "A-1=2", "--ttl", "soon"],
])
def test_usage_errors_are_json_exit_2(capsys, db, argv):
    Store(db).reserve("A-1", 1, "old", 60, now=1000)  # something a wrongly accepted sweep would record
    rows = snapshot(db)
    assert bad(capsys, db, *argv) == (2, "usage")
    assert snapshot(db) == rows


# ---- item 20 through the command line -----------------------------------------------------------

def test_commands_record_the_events_of_what_they_did(capsys, db):
    def kinds():
        return [(e["kind"], e["sku"], e["quantity"]) for e in Store(db).events()[2:]]

    ok(capsys, db, "add", "C-3", "Crate", "4")
    a = ok(capsys, db, "reserve", "A-1", "2", "ann", "--ttl", "60")
    b = ok(capsys, db, "reserve", "C-3", "3", "bob", "--ttl", "60")
    ok(capsys, db, "release", str(a["id"]))
    ok(capsys, db, "release", str(a["id"]))
    ok(capsys, db, "commit", str(b["id"]))
    ok(capsys, db, "show", str(b["id"]))
    ok(capsys, db, "availability", "C-3")
    ok(capsys, db, "list")
    assert bad(capsys, db, "commit", str(b["id"])) == (1, "already_committed")
    ok(capsys, db, "adjust", "C-3", "-1", "damage")
    assert kinds() == [("item_added", "C-3", 4), ("reserved", "A-1", 2), ("reserved", "C-3", 3),
                       ("released", "A-1", 2), ("committed", "C-3", 3), ("adjusted", "C-3", -1)]
    assert ok(capsys, db, "events", "--kind", "committed")["events"][0]["reservation_id"] == b["id"]
