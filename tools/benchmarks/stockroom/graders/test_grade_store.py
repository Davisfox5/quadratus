import sqlite3
import threading

import pytest
from stockroom.store import Store, StoreError

KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.add_item("A-1", "Anvil", 10)
    return s


def code(call):
    with pytest.raises(StoreError) as refused:
        call()
    return refused.value.code


def snapshot(store):
    with sqlite3.connect(store.path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: db.execute(f"SELECT * FROM {n} ORDER BY 1").fetchall() for n in names}


def test_schema_version(store):
    assert store.schema_version() == 2


def test_v1_database_is_upgraded_and_keeps_items(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE items (sku TEXT PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL)")
        db.execute("INSERT INTO items VALUES ('A-1', 'Anvil', 7)")
    s = Store(path)
    assert s.schema_version() == 2
    assert s.list_items() == [{"sku": "A-1", "name": "Anvil", "quantity": 7}]
    assert s.reserve("A-1", 2, "ann", 60, now=0)["state"] == "active"
    before = snapshot(s)
    again = Store(path)
    assert again.schema_version() == 2 and snapshot(again) == before


def test_reserve_shape(store):
    r = store.reserve("A-1", 3, "  ann  ", 60, now=100)
    assert set(r) == KEYS
    assert isinstance(r["id"], int) and not isinstance(r["id"], bool) and r["id"] > 0
    assert (r["sku"], r["quantity"], r["holder"], r["state"]) == ("A-1", 3, "ann", "active")
    assert isinstance(r["expires_at"], float) and r["expires_at"] == 160.0


def test_ids_are_unique(store):
    ids = [store.reserve("A-1", 1, "ann", 60, now=0)["id"] for _ in range(5)]
    assert len(set(ids)) == 5


def test_availability(store):
    store.reserve("A-1", 3, "ann", 60, now=100)
    assert store.availability("A-1", now=100) == {"sku": "A-1", "on_hand": 10, "reserved": 3, "available": 7}


def test_expiry_frees_stock_at_the_boundary(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    # Read in time order: once seen expired it may stay expired (goal item 5).
    assert store.get_reservation(r["id"], now=159)["state"] == "active"
    assert store.availability("A-1", now=159.9)["reserved"] == 3
    assert store.availability("A-1", now=160)["reserved"] == 0
    assert store.get_reservation(r["id"], now=160)["state"] == "expired"


def test_release(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    assert store.release(r["id"], now=101)["state"] == "released"
    assert store.release(r["id"], now=102)["state"] == "released"
    assert store.availability("A-1", now=102)["available"] == 10


def test_release_of_expired_reads_expired(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    assert store.release(r["id"], now=500)["state"] == "expired"
    assert store.get_reservation(r["id"], now=500)["state"] == "expired"


def test_commit_takes_stock(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    done = store.commit(r["id"], now=101)
    assert done["state"] == "committed" and set(done) == KEYS
    assert store.get_item("A-1")["quantity"] == 7
    assert store.availability("A-1", now=101) == {"sku": "A-1", "on_hand": 7, "reserved": 0, "available": 7}
    assert store.get_reservation(r["id"], now=10**6)["state"] == "committed"


def test_commit_refusals(store):
    a = store.reserve("A-1", 1, "ann", 60, now=100)
    store.commit(a["id"], now=101)
    assert code(lambda: store.commit(a["id"], now=102)) == "already_committed"
    assert code(lambda: store.release(a["id"], now=102)) == "already_committed"
    b = store.reserve("A-1", 1, "ann", 60, now=100)
    store.release(b["id"], now=101)
    assert code(lambda: store.commit(b["id"], now=102)) == "reservation_released"
    c = store.reserve("A-1", 1, "ann", 60, now=100)
    assert code(lambda: store.commit(c["id"], now=160)) == "reservation_expired"
    assert store.get_item("A-1")["quantity"] == 9


@pytest.mark.parametrize("quantity", [0, -1, 1.0, "2", True, None])
def test_invalid_quantity(store, quantity):
    before = snapshot(store)
    assert code(lambda: store.reserve("A-1", quantity, "ann", 60, now=0)) == "invalid_quantity"
    assert snapshot(store) == before


@pytest.mark.parametrize("holder", ["", "   ", "x" * 65, 5, None])
def test_invalid_holder(store, holder):
    assert code(lambda: store.reserve("A-1", 1, holder, 60, now=0)) == "invalid_holder"


def test_holder_of_64_after_stripping_is_accepted(store):
    assert store.reserve("A-1", 1, " " + "x" * 64 + " ", 60, now=0)["holder"] == "x" * 64


@pytest.mark.parametrize("ttl", [0, -5, 86401, 1.5, "60", True, None])
def test_invalid_ttl(store, ttl):
    assert code(lambda: store.reserve("A-1", 1, "ann", ttl, now=0)) == "invalid_ttl"


def test_ttl_bounds_are_accepted(store):
    assert store.reserve("A-1", 1, "ann", 1, now=0)["expires_at"] == 1.0
    assert store.reserve("A-1", 1, "ann", 86400, now=0)["expires_at"] == 86400.0


def test_unknown_sku_and_stock(store):
    assert code(lambda: store.reserve("nope", 1, "ann", 60, now=0)) == "unknown_sku"
    assert code(lambda: store.availability("nope", now=0)) == "unknown_sku"
    store.reserve("A-1", 8, "ann", 60, now=0)
    before = snapshot(store)
    assert code(lambda: store.reserve("A-1", 3, "bob", 60, now=0)) == "insufficient_stock"
    assert snapshot(store) == before
    assert store.reserve("A-1", 2, "bob", 60, now=0)["state"] == "active"


def test_check_order(store):
    assert code(lambda: store.reserve("nope", 0, "", 0, now=0)) == "unknown_sku"
    assert code(lambda: store.reserve("A-1", 0, "", 0, now=0)) == "invalid_quantity"
    assert code(lambda: store.reserve("A-1", 99, "", 0, now=0)) == "invalid_holder"
    assert code(lambda: store.reserve("A-1", 99, "ann", 0, now=0)) == "invalid_ttl"
    assert code(lambda: store.reserve("A-1", 99, "ann", 60, now=0)) == "insufficient_stock"


@pytest.mark.parametrize("rid", [0, -1, 999, "1", 1.0, None])
def test_unknown_reservation(store, rid):
    store.reserve("A-1", 1, "ann", 60, now=0)
    for call in (store.release, store.commit, store.get_reservation):
        assert code(lambda call=call: call(rid, now=1)) == "unknown_reservation"


def test_now_defaults_to_the_clock(store):
    r = store.reserve("A-1", 1, "ann", 60)
    assert store.get_reservation(r["id"])["state"] == "active"
    assert store.availability("A-1")["reserved"] == 1


def test_no_overselling_under_threads(tmp_path):
    path = tmp_path / "c.db"
    Store(path).add_item("A-1", "Anvil", 10)
    outcomes, start = [], threading.Barrier(24)

    def worker():
        mine = Store(path)
        start.wait()
        try:
            mine.reserve("A-1", 1, "ann", 600, now=0)
            outcomes.append("ok")
        except StoreError as exc:
            outcomes.append(exc.code)
        except Exception as exc:  # any other failure is a defect
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(set(outcomes)) == ["insufficient_stock", "ok"], outcomes
    assert outcomes.count("ok") == 10
    assert Store(path).availability("A-1", now=1) == {"sku": "A-1", "on_hand": 10, "reserved": 10, "available": 0}


def test_seed_behaviour_is_unchanged(store):
    assert store.list_items() == [{"sku": "A-1", "name": "Anvil", "quantity": 10}]
    assert code(lambda: store.add_item("A-1", "Again", 1)) == "duplicate_sku"
    assert store.get_item("zzz") is None
