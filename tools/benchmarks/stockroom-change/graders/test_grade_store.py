import sqlite3

import pytest
from stockroom.store import Store, StoreError

KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}
STATES = ("active", "released", "committed", "expired")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    s.add_item("A-1", "Anvil", 10)
    s.add_item("B-2", "Bolt", 4)
    return s


def code(call):
    with pytest.raises(StoreError) as refused:
        call()
    return refused.value.code


def snapshot(store):
    with sqlite3.connect(store.path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: db.execute(f"SELECT * FROM {n} ORDER BY 1").fetchall() for n in names}


def held(store, state, now=100):
    """A reservation of 3 on A-1 made at ``now`` for 10 seconds, moved to ``state``."""
    r = store.reserve("A-1", 3, "ann", 10, now=now)
    if state == "released":
        store.release(r["id"], now=now)
    elif state == "committed":
        store.commit(r["id"], now=now)
    elif state == "expired":
        assert store.get_reservation(r["id"], now=now + 10)["state"] == "expired"
    return r["id"]


# -- bug reports ------------------------------------------------------------

def test_bug_holder_of_64_is_allowed(store):
    assert store.reserve("A-1", 1, "h" * 64, 10, now=100)["holder"] == "h" * 64
    assert store.reserve("A-1", 1, "  " + "h" * 64 + " ", 10, now=100)["holder"] == "h" * 64


def test_bug_holder_of_65_is_refused(store):
    assert code(lambda: store.reserve("A-1", 1, "h" * 65, 10, now=100)) == "invalid_holder"


def test_bug_expired_at_exactly_expires_at(store):
    r = store.reserve("A-1", 3, "ann", 10, now=100)
    assert store.get_reservation(r["id"], now=109.5)["state"] == "active"
    assert store.get_reservation(r["id"], now=110)["state"] == "expired"


def test_bug_commit_at_exactly_expires_at_is_refused(store):
    r = store.reserve("A-1", 3, "ann", 10, now=100)
    assert code(lambda: store.commit(r["id"], now=110)) == "reservation_expired"
    assert store.get_item("A-1")["quantity"] == 10


def test_bug_availability_is_first_to_see_an_expiry(store):
    store.reserve("A-1", 3, "ann", 10, now=100)
    assert store.availability("A-1", now=200) == {"sku": "A-1", "on_hand": 10, "reserved": 0, "available": 10}


def test_bug_availability_before_expiry_still_counts(store):
    store.reserve("A-1", 3, "ann", 10, now=100)
    assert store.availability("A-1", now=105)["reserved"] == 3


def test_bug_release_of_expired_returns_expired(store):
    r = store.reserve("A-1", 3, "ann", 10, now=100)
    out = store.release(r["id"], now=200)
    assert out["state"] == "expired" and set(out) == KEYS
    assert store.release(r["id"], now=300)["state"] == "expired"


# -- extend -----------------------------------------------------------------

def test_extend_moves_expiry_later(store):
    rid = held(store, "active")
    out = store.extend(rid, 60, now=105)
    assert set(out) == KEYS and out["state"] == "active"
    assert isinstance(out["expires_at"], float) and out["expires_at"] == 165.0
    assert store.get_reservation(rid, now=164)["state"] == "active"
    assert store.get_reservation(rid, now=165)["state"] == "expired"


def test_extend_can_move_expiry_earlier(store):
    rid = held(store, "active")
    assert store.extend(rid, 1, now=101)["expires_at"] == 102.0
    assert store.get_reservation(rid, now=103)["state"] == "expired"


def test_extend_keeps_the_rest(store):
    rid = held(store, "active")
    before = store.get_reservation(rid, now=101)
    after = store.extend(rid, 50, now=101)
    assert {k: v for k, v in after.items() if k != "expires_at"} == {
        k: v for k, v in before.items() if k != "expires_at"}
    assert store.get_reservation(rid, now=102) == after


@pytest.mark.parametrize("bad", [0, -1, 999, "1", True, 1.0, None])
def test_extend_unknown_reservation(store, bad):
    held(store, "active")
    assert code(lambda: store.extend(bad, 60, now=101)) == "unknown_reservation"


@pytest.mark.parametrize("bad", [0, -5, 86401, True, 1.0, "60", None])
def test_extend_invalid_ttl(store, bad):
    rid = held(store, "active")
    before = snapshot(store)
    assert code(lambda: store.extend(rid, bad, now=101)) == "invalid_ttl"
    assert snapshot(store) == before


def test_extend_bounds_accepted(store):
    rid = held(store, "active")
    assert store.extend(rid, 86400, now=101)["expires_at"] == 86501.0
    assert store.extend(rid, 1, now=101)["expires_at"] == 102.0


@pytest.mark.parametrize("state,expected", [
    ("committed", "already_committed"),
    ("released", "reservation_released"),
    ("expired", "reservation_expired"),
])
def test_extend_refused_by_state(store, state, expected):
    rid = held(store, state)
    before = snapshot(store)
    assert code(lambda: store.extend(rid, 60, now=120)) == expected
    assert snapshot(store) == before


def test_extend_at_expiry_is_too_late(store):
    rid = held(store, "active")
    assert code(lambda: store.extend(rid, 60, now=110)) == "reservation_expired"
    assert store.get_reservation(rid, now=111)["state"] == "expired"


def test_extend_checks_id_before_ttl_and_ttl_before_state(store):
    rid = held(store, "committed")
    assert code(lambda: store.extend(999, 0, now=101)) == "unknown_reservation"
    assert code(lambda: store.extend(rid, 0, now=101)) == "invalid_ttl"


def test_extend_uses_the_clock_when_now_is_omitted(store):
    import time
    r = store.reserve("A-1", 1, "ann", 10)
    out = store.extend(r["id"], 500)
    assert abs(out["expires_at"] - (time.time() + 500)) < 5


# -- partial commit ---------------------------------------------------------

def test_partial_commit_keeps_the_rest_active(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    out = store.commit(r["id"], now=101, quantity=2)
    assert set(out) == KEYS
    assert (out["state"], out["quantity"], out["expires_at"]) == ("active", 3, r["expires_at"])
    assert store.get_item("A-1")["quantity"] == 8
    assert store.availability("A-1", now=102) == {"sku": "A-1", "on_hand": 8, "reserved": 3, "available": 5}
    assert store.get_reservation(r["id"], now=102) == out


def test_partial_commit_of_everything_left_commits(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    store.commit(r["id"], now=101, quantity=2)
    out = store.commit(r["id"], now=102, quantity=3)
    assert out["state"] == "committed"
    assert store.get_item("A-1")["quantity"] == 5
    assert store.availability("A-1", now=103)["reserved"] == 0


def test_commit_quantity_equal_to_held_is_full_commit(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    assert store.commit(r["id"], now=101, quantity=5)["state"] == "committed"
    assert store.get_item("A-1")["quantity"] == 5


def test_full_commit_after_partial_takes_only_the_rest(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    store.commit(r["id"], now=101, quantity=4)
    assert store.commit(r["id"], now=102)["state"] == "committed"
    assert store.get_item("A-1")["quantity"] == 5


def test_commit_without_quantity_is_unchanged(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    out = store.commit(r["id"], 101)
    assert (out["state"], out["quantity"]) == ("committed", 5)
    assert store.get_item("A-1")["quantity"] == 5


def test_partial_commit_then_expiry_frees_the_rest(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    store.commit(r["id"], now=101, quantity=2)
    assert store.availability("A-1", now=200) == {"sku": "A-1", "on_hand": 8, "reserved": 0, "available": 8}


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.0, "2"])
def test_partial_commit_invalid_quantity(store, bad):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    before = snapshot(store)
    assert code(lambda: store.commit(r["id"], now=101, quantity=bad)) == "invalid_quantity"
    assert snapshot(store) == before


def test_partial_commit_of_more_than_held(store):
    r = store.reserve("A-1", 5, "ann", 10, now=100)
    before = snapshot(store)
    assert code(lambda: store.commit(r["id"], now=101, quantity=6)) == "invalid_quantity"
    assert snapshot(store) == before


@pytest.mark.parametrize("state,expected", [
    ("committed", "already_committed"),
    ("released", "reservation_released"),
    ("expired", "reservation_expired"),
])
def test_partial_commit_refused_by_state(store, state, expected):
    rid = held(store, state)
    before = snapshot(store)
    assert code(lambda: store.commit(rid, now=120, quantity=1)) == expected
    assert snapshot(store) == before


def test_partial_commit_order_of_checks(store):
    rid = held(store, "released")
    assert code(lambda: store.commit(999, now=101, quantity=0)) == "unknown_reservation"
    assert code(lambda: store.commit(rid, now=101, quantity=0)) == "invalid_quantity"
    assert code(lambda: store.commit(rid, now=101, quantity=99)) == "reservation_released"


# -- adjust -----------------------------------------------------------------

def test_adjust_up_and_down(store):
    assert store.adjust("A-1", 5, now=100) == {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15}
    assert store.adjust("A-1", -12, now=100) == {"sku": "A-1", "on_hand": 3, "reserved": 0, "available": 3}
    assert store.get_item("A-1")["quantity"] == 3
    assert store.get_item("B-2")["quantity"] == 4


def test_adjust_down_to_exactly_reserved(store):
    store.reserve("A-1", 4, "ann", 10, now=100)
    assert store.adjust("A-1", -6, now=101) == {"sku": "A-1", "on_hand": 4, "reserved": 4, "available": 0}


def test_adjust_below_reserved_is_refused(store):
    store.reserve("A-1", 4, "ann", 10, now=100)
    before = snapshot(store)
    assert code(lambda: store.adjust("A-1", -7, now=101)) == "insufficient_stock"
    assert snapshot(store) == before


def test_adjust_ignores_expired_reservations(store):
    store.reserve("A-1", 4, "ann", 10, now=100)
    assert store.adjust("A-1", -10, now=200)["on_hand"] == 0


def test_adjust_below_zero_is_refused(store):
    before = snapshot(store)
    assert code(lambda: store.adjust("A-1", -11, now=100)) == "insufficient_stock"
    assert snapshot(store) == before


@pytest.mark.parametrize("bad", [0, True, False, 1.5, 2.0, "3", None])
def test_adjust_invalid_delta(store, bad):
    before = snapshot(store)
    assert code(lambda: store.adjust("A-1", bad, now=100)) == "invalid_quantity"
    assert snapshot(store) == before


def test_adjust_unknown_sku_comes_first(store):
    assert code(lambda: store.adjust("nope", 1, now=100)) == "unknown_sku"
    assert code(lambda: store.adjust("nope", 0, now=100)) == "unknown_sku"


# -- list_reservations ------------------------------------------------------

def test_list_is_in_id_order_with_current_states(store):
    a = store.reserve("A-1", 1, "ann", 10, now=100)
    b = store.reserve("A-1", 1, "bob", 50, now=100)
    c = store.reserve("A-1", 1, "ann", 50, now=100)
    d = store.reserve("A-1", 1, "cy", 50, now=100)
    store.reserve("B-2", 1, "ann", 50, now=100)
    store.release(c["id"], now=101)
    store.commit(d["id"], now=101)
    rows = store.list_reservations("A-1", now=120)
    assert [r["id"] for r in rows] == [a["id"], b["id"], c["id"], d["id"]]
    assert [r["state"] for r in rows] == ["expired", "active", "released", "committed"]
    assert all(set(r) == KEYS and r["sku"] == "A-1" for r in rows)
    assert rows[1] == store.get_reservation(b["id"], now=120)


def test_list_of_an_item_with_none(store):
    assert store.list_reservations("B-2", now=100) == []


def test_list_filters_by_holder_after_stripping(store):
    a = store.reserve("A-1", 1, "ann", 50, now=100)
    store.reserve("A-1", 1, "bob", 50, now=100)
    c = store.reserve("A-1", 1, " ann ", 50, now=100)
    assert [r["id"] for r in store.list_reservations("A-1", holder="  ann ", now=101)] == [a["id"], c["id"]]
    assert store.list_reservations("A-1", holder="Ann", now=101) == []


@pytest.mark.parametrize("state", STATES)
def test_list_filters_by_state(store, state):
    ids = {s: held(store, s) for s in ("released", "committed")}
    old = store.reserve("A-1", 1, "ann", 10, now=100)["id"]
    live = store.reserve("A-1", 1, "ann", 500, now=100)["id"]
    ids.update(expired=old, active=live)
    rows = store.list_reservations("A-1", state=state, now=150)
    assert [r["id"] for r in rows] == [ids[state]] and rows[0]["state"] == state


def test_list_filters_by_holder_and_state_together(store):
    store.reserve("A-1", 1, "ann", 10, now=100)
    b = store.reserve("A-1", 1, "ann", 500, now=100)
    store.reserve("A-1", 1, "bob", 500, now=100)
    assert [r["id"] for r in store.list_reservations("A-1", "ann", "active", now=150)] == [b["id"]]


def test_list_unknown_sku(store):
    assert code(lambda: store.list_reservations("nope", now=100)) == "unknown_sku"


@pytest.mark.parametrize("bad", ["", "   ", "h" * 65, 5])
def test_list_invalid_holder(store, bad):
    assert code(lambda: store.list_reservations("A-1", holder=bad, now=100)) == "invalid_holder"


@pytest.mark.parametrize("bad", ["gone", "ACTIVE", "", 5])
def test_list_invalid_state(store, bad):
    assert code(lambda: store.list_reservations("A-1", state=bad, now=100)) == "invalid_state"


def test_list_order_of_checks(store):
    assert code(lambda: store.list_reservations("nope", holder="", state="gone", now=100)) == "unknown_sku"
    assert code(lambda: store.list_reservations("A-1", holder="", state="gone", now=100)) == "invalid_holder"


# -- shape of the database --------------------------------------------------

def test_schema_stays_at_2_with_no_new_table(store):
    held(store, "active")
    store.adjust("A-1", 1, now=100)
    store.list_reservations("A-1", now=100)
    assert store.schema_version() == 2
    with sqlite3.connect(store.path) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names - {"sqlite_sequence"} == {"items", "reservations"}


def test_old_calls_still_work(store):
    r = store.reserve("A-1", 2, " ann ", 10, now=100)
    assert set(r) == KEYS and r["holder"] == "ann" and r["expires_at"] == 110.0
    assert store.release(r["id"], now=101)["state"] == "released"
    assert code(lambda: store.reserve("A-1", 11, "ann", 10, now=100)) == "insufficient_stock"
    assert store.list_items()[0] == {"sku": "A-1", "name": "Anvil", "quantity": 10}
