"""Graders for goal items 19 to 30: event log, adjust, sweep, reserve_many, idempotency keys."""

import contextlib
import itertools
import sqlite3
import threading
import time

import pytest
from stockroom.store import Store, StoreError

KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}
EVENT_KEYS = {"id", "at", "kind", "sku", "quantity", "reservation_id", "holder"}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "s.db"


@pytest.fixture
def store(path):
    s = Store(path)
    s.add_item("A-1", "Anvil", 10)
    s.add_item("B-2", "Bucket", 5)
    return s


def code(call):
    with pytest.raises(StoreError) as refused:
        call()
    return refused.value.code


def snapshot(path):
    """Every row of every table, whatever the tables are."""
    with contextlib.closing(sqlite3.connect(path)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: sorted(db.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr) for n in names}


def log(store):
    return store.events(limit=500)


def added(store, before):
    """The events recorded since ``before`` was read; the earlier ones must not have changed."""
    after = log(store)
    assert after[:len(before)] == before
    return after[len(before):]


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def check(event, kind, sku, quantity, reservation_id=None, holder=None, at=None):
    assert set(event) == EVENT_KEYS
    assert is_int(event["id"]) and event["id"] > 0
    assert isinstance(event["at"], float)
    assert is_int(event["quantity"])
    found = (event["kind"], event["sku"], event["quantity"], event["reservation_id"], event["holder"])
    assert found == (kind, sku, quantity, reservation_id, holder)
    if at is not None:
        assert event["at"] == at


def race(path, jobs):
    """Run every job at once, each in its own thread with its own Store; return how each ended."""
    outcomes, start = [None] * len(jobs), threading.Barrier(len(jobs), timeout=60)

    def worker(index, job):
        try:
            mine = Store(path)
            start.wait()
            outcomes[index] = ("ok", job(mine))
        except StoreError as exc:
            outcomes[index] = (exc.code, None)
        except Exception as exc:  # noqa: BLE001 - any other failure is a defect
            outcomes[index] = (type(exc).__name__, None)

    threads = [threading.Thread(target=worker, args=pair) for pair in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


# ---- items 19 and 20: the event log -------------------------------------------------------------

def test_add_item_records_one_item_added_event(path):
    s = Store(path)
    assert s.events() == []
    before = time.time()
    s.add_item("A-1", "Anvil", 7)
    after = time.time()
    (event,) = s.events()
    check(event, "item_added", "A-1", 7)
    assert before - 1 <= event["at"] <= after + 1


def test_reserve_records_reserved_at_the_calls_now(store):
    before = log(store)
    r = store.reserve("A-1", 3, "  ann  ", 60, now=100)
    (event,) = added(store, before)
    check(event, "reserved", "A-1", 3, r["id"], "ann", at=100.0)


def test_release_records_released_only_when_it_changes_the_reservation(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    before = log(store)
    store.release(r["id"], now=101.5)
    (event,) = added(store, before)
    check(event, "released", "A-1", 3, r["id"], "ann", at=101.5)
    before = log(store)
    assert store.release(r["id"], now=102)["state"] == "released"
    assert added(store, before) == []


def test_commit_records_committed(store):
    r = store.reserve("B-2", 2, "bob", 60, now=100)
    before = log(store)
    store.commit(r["id"], now=101)
    (event,) = added(store, before)
    check(event, "committed", "B-2", 2, r["id"], "bob", at=101.0)


def test_release_of_an_expired_reservation_records_nothing(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    before = log(store)
    assert store.release(r["id"], now=500)["state"] == "expired"
    assert added(store, before) == []


def test_reads_record_nothing(store):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    before = log(store)
    store.get_reservation(r["id"], now=101)
    store.availability("A-1", now=101)
    store.get_item("A-1")
    store.list_items()
    store.schema_version()
    store.events(sku="A-1", kind="reserved")
    assert store.get_reservation(r["id"], now=160)["state"] == "expired"
    assert store.availability("A-1", now=160)["reserved"] == 0
    assert added(store, before) == []


def test_events_are_in_recording_order_with_increasing_ids(store):
    store.add_item("C-3", "Crate", 1)
    a = store.reserve("A-1", 2, "ann", 60, now=10)
    store.release(a["id"], now=11)
    b = store.reserve("B-2", 1, "bob", 60, now=12)
    store.commit(b["id"], now=13)
    store.adjust("A-1", 4, "restock", now=14)
    events = log(store)
    assert [(e["kind"], e["sku"]) for e in events] == [
        ("item_added", "A-1"), ("item_added", "B-2"), ("item_added", "C-3"), ("reserved", "A-1"),
        ("released", "A-1"), ("reserved", "B-2"), ("committed", "B-2"), ("adjusted", "A-1")]
    ids = [e["id"] for e in events]
    assert all(is_int(i) and i > 0 for i in ids)
    assert all(low < high for low, high in itertools.pairwise(ids))


def test_at_is_the_clock_when_now_is_omitted(store):
    before = log(store)
    start = time.time()
    r = store.reserve("A-1", 1, "ann", 60)
    store.commit(r["id"])
    end = time.time()
    events = added(store, before)
    assert [e["kind"] for e in events] == ["reserved", "committed"]
    assert all(start - 1 <= e["at"] <= end + 1 for e in events)


def test_upgrading_an_old_database_records_no_event(path):
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE items (sku TEXT PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL)")
        db.execute("INSERT INTO items VALUES ('A-1', 'Anvil', 7)")
        db.commit()
    s = Store(path)
    assert s.events() == []
    assert Store(path).events() == []
    s.add_item("B-2", "Bucket", 1)
    (event,) = s.events()
    check(event, "item_added", "B-2", 1)


@pytest.fixture
def busy(store):
    """A-1 has 9 on hand after a commit and 2 held until 160; key k-1 is remembered for a reserve."""
    held = store.reserve("A-1", 2, "ann", 60, now=100, idempotency_key="k-1")
    gone = store.reserve("A-1", 1, "bob", 60, now=100)
    done = store.reserve("A-1", 1, "cy", 60, now=100)
    store.release(gone["id"], now=101)
    store.commit(done["id"], now=102)
    return store, {"held": held["id"], "gone": gone["id"], "done": done["id"]}


REFUSED = {
    "add_item-duplicate_sku": lambda s, ids: s.add_item("A-1", "Again", 1),
    "reserve-insufficient_stock": lambda s, ids: s.reserve("A-1", 8, "dee", 60, now=110),
    "release-already_committed": lambda s, ids: s.release(ids["done"], now=110),
    "commit-reservation_released": lambda s, ids: s.commit(ids["gone"], now=110),
    "adjust-insufficient_stock": lambda s, ids: s.adjust("A-1", -10, "damage", now=110),
    "adjust-stock_reserved": lambda s, ids: s.adjust("A-1", -8, "damage", now=110),
    "reserve_many-unknown_sku": lambda s, ids: s.reserve_many([["B-2", 1], ["nope", 1]], "dee", 60, now=110),
    "reserve_many-insufficient_stock": lambda s, ids: s.reserve_many([["B-2", 1], ["A-1", 8]], "dee", 60, now=110),
    "reserve-invalid_idempotency_key": lambda s, ids: s.reserve("A-1", 1, "dee", 60, now=110, idempotency_key="no key"),
    "reserve-idempotency_conflict": lambda s, ids: s.reserve("A-1", 1, "ann", 60, now=110, idempotency_key="k-1"),
    "adjust-idempotency_conflict": lambda s, ids: s.adjust("A-1", 1, "restock", now=110, idempotency_key="k-1"),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_refused_call_records_no_event_and_changes_nothing(busy, path, name):
    store, ids = busy
    before, rows = log(store), snapshot(path)
    assert code(lambda: REFUSED[name](store, ids)) == name.split("-")[1]
    assert added(store, before) == []
    assert snapshot(path) == rows


def test_a_refused_commit_of_an_expired_reservation_changes_nothing(store, path):
    r = store.reserve("A-1", 3, "ann", 60, now=100)
    before, rows = log(store), snapshot(path)
    assert code(lambda: store.commit(r["id"], now=160)) == "reservation_expired"
    assert added(store, before) == []
    assert snapshot(path) == rows


# ---- item 21: reading the log -------------------------------------------------------------------

@pytest.fixture
def full(store):
    """Nine events, at least one of each kind."""
    a = store.reserve("A-1", 2, "ann", 60, now=100)
    b = store.reserve("B-2", 1, "bob", 600, now=100)
    c = store.reserve("A-1", 1, "cy", 600, now=100)
    store.release(b["id"], now=110)
    store.commit(c["id"], now=120)
    store.adjust("B-2", 3, "restock", now=130)
    assert store.sweep(now=200) == 1
    assert store.get_reservation(a["id"], now=200)["state"] == "expired"
    return store


def test_events_filters_by_kind(full):
    everything = full.events()
    counts = {"item_added": 2, "reserved": 3, "released": 1, "committed": 1, "adjusted": 1, "expired": 1}
    assert len(everything) == 9
    for kind, count in counts.items():
        found = full.events(kind=kind)
        assert found == [e for e in everything if e["kind"] == kind], kind
        assert len(found) == count, kind


def test_events_filters_by_sku(full):
    everything = full.events()
    assert full.events(sku="B-2") == [e for e in everything if e["sku"] == "B-2"]
    assert len(full.events(sku="B-2")) == 4
    assert [e["quantity"] for e in full.events(sku="A-1", kind="reserved")] == [2, 1]
    assert full.events(sku="nope") == []
    assert full.events(sku="nope", kind="reserved") == []


def test_events_pages_forward_with_after_id_and_limit(full):
    everything = full.events()
    seen, cursor = [], 0
    for _ in range(10):
        page = full.events(after_id=cursor, limit=2)
        assert len(page) <= 2
        if not page:
            break
        seen += page
        cursor = page[-1]["id"]
    assert seen == everything
    assert full.events(after_id=everything[3]["id"]) == everything[4:]
    assert full.events(after_id=everything[-1]["id"]) == []
    assert full.events(after_id=10**18) == []
    reserved = [e for e in everything if e["kind"] == "reserved"]
    assert full.events(kind="reserved", after_id=reserved[0]["id"], limit=1) == [reserved[1]]


def test_events_returns_100_by_default_and_up_to_500(store):
    for _ in range(100):
        store.adjust("A-1", 1, "restock", now=1)
    everything = store.events(limit=500)
    assert len(everything) == 102
    assert store.events() == everything[:100]
    assert store.events(limit=1) == everything[:1]


def test_events_invalid_kind(full):
    for kind in ("bogus", "", "RESERVED", 5):
        assert code(lambda kind=kind: full.events(kind=kind)) == "invalid_kind", kind


def test_events_invalid_cursor(full):
    for after_id in (-1, 1.0, "0", True):
        assert code(lambda after_id=after_id: full.events(after_id=after_id)) == "invalid_cursor", after_id
    assert len(full.events(after_id=0)) == 9


def test_events_invalid_limit(full):
    for limit in (0, -1, 501, 1.5, "10", True):
        assert code(lambda limit=limit: full.events(limit=limit)) == "invalid_limit", limit
    assert len(full.events(limit=500)) == 9


def test_events_check_order(full):
    assert code(lambda: full.events(kind="bogus", after_id=-1, limit=0)) == "invalid_kind"
    assert code(lambda: full.events(kind="reserved", after_id=-1, limit=0)) == "invalid_cursor"
    assert code(lambda: full.events(sku="nope", after_id=-1, limit=0)) == "invalid_cursor"
    assert code(lambda: full.events(sku="nope", kind="reserved", limit=0)) == "invalid_limit"


# ---- items 22 to 24: adjustments ----------------------------------------------------------------

def test_adjust_returns_availability_after_the_change(store):
    assert store.adjust("A-1", 5, "restock", now=0) == {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15}
    assert store.get_item("A-1")["quantity"] == 15
    store.reserve("A-1", 3, "ann", 60, now=1)
    assert store.adjust("A-1", -2, "damage", now=2) == {"sku": "A-1", "on_hand": 13, "reserved": 3, "available": 10}
    expected = {"sku": "A-1", "on_hand": 3, "reserved": 3, "available": 0}
    assert store.adjust("A-1", -10, "correction", now=3) == expected
    assert store.availability("A-1", now=3) == expected
    assert store.get_item("B-2")["quantity"] == 5


def test_adjust_records_adjusted_with_the_delta(store):
    before = log(store)
    store.adjust("A-1", 4, "restock", now=7)
    store.adjust("B-2", -5, "damage", now=8)
    up, down = added(store, before)
    check(up, "adjusted", "A-1", 4)
    check(down, "adjusted", "B-2", -5)


def test_adjust_invalid_delta(store, path):
    rows = snapshot(path)
    for delta in (0, 1.5, "1", True, None):
        assert code(lambda delta=delta: store.adjust("A-1", delta, "restock", now=0)) == "invalid_delta", delta
    assert snapshot(path) == rows


def test_adjust_invalid_reason(store, path):
    rows = snapshot(path)
    for reason in ("", "RESTOCK", "theft", 5, None):
        assert code(lambda reason=reason: store.adjust("A-1", 1, reason, now=0)) == "invalid_reason", reason
    assert snapshot(path) == rows


def test_adjust_stock_bounds(store, path):
    store.reserve("A-1", 4, "ann", 60, now=0)
    rows = snapshot(path)
    assert code(lambda: store.adjust("A-1", -11, "damage", now=1)) == "insufficient_stock"
    assert code(lambda: store.adjust("A-1", -7, "damage", now=1)) == "stock_reserved"
    assert snapshot(path) == rows
    assert store.adjust("A-1", -6, "damage", now=1) == {"sku": "A-1", "on_hand": 4, "reserved": 4, "available": 0}
    assert store.adjust("B-2", -5, "correction", now=1) == {"sku": "B-2", "on_hand": 0, "reserved": 0, "available": 0}
    assert code(lambda: store.adjust("B-2", -1, "damage", now=1)) == "insufficient_stock"
    assert code(lambda: store.adjust("A-1", -1, "damage", now=1)) == "stock_reserved"


def test_adjust_counts_the_reservations_active_at_now(store):
    store.reserve("A-1", 4, "ann", 60, now=0)
    b = store.reserve("A-1", 3, "bob", 600, now=0)
    c = store.reserve("A-1", 2, "cy", 600, now=0)
    store.release(b["id"], now=1)
    assert code(lambda: store.adjust("A-1", -5, "damage", now=59)) == "stock_reserved"
    assert store.adjust("A-1", -4, "damage", now=59) == {"sku": "A-1", "on_hand": 6, "reserved": 6, "available": 0}
    # At 60 ann's four are expired and no longer held.
    assert store.adjust("A-1", -4, "damage", now=60) == {"sku": "A-1", "on_hand": 2, "reserved": 2, "available": 0}
    store.commit(c["id"], now=61)
    assert store.availability("A-1", now=61) == {"sku": "A-1", "on_hand": 0, "reserved": 0, "available": 0}


def test_adjust_check_order(store):
    assert code(lambda: store.adjust("nope", 0, "theft", now=0)) == "unknown_sku"
    assert code(lambda: store.adjust("A-1", 0, "theft", now=0)) == "invalid_delta"
    assert code(lambda: store.adjust("A-1", -99, "theft", now=0)) == "invalid_reason"
    store.reserve("A-1", 4, "ann", 60, now=0)
    assert code(lambda: store.adjust("A-1", -99, "damage", now=0)) == "insufficient_stock"
    assert code(lambda: store.adjust("A-1", -9, "damage", now=0)) == "stock_reserved"


def test_adjust_now_defaults_to_the_clock(store):
    store.reserve("B-2", 5, "old", 60, now=1000)
    store.reserve("A-1", 4, "ann", 600)
    assert code(lambda: store.adjust("A-1", -7, "damage")) == "stock_reserved"
    assert store.adjust("A-1", -6, "damage") == {"sku": "A-1", "on_hand": 4, "reserved": 4, "available": 0}
    assert store.adjust("B-2", -5, "damage") == {"sku": "B-2", "on_hand": 0, "reserved": 0, "available": 0}


def test_adjustments_under_threads_lose_nothing(path):
    setup = Store(path)
    setup.add_item("UP", "Rising", 0)
    setup.add_item("DOWN", "Falling", 5)
    jobs = [lambda s: s.adjust("UP", 1, "restock", now=0)] * 16
    jobs += [lambda s: s.adjust("DOWN", -1, "damage", now=0)] * 12
    outcomes = [kind for kind, _ in race(path, jobs)]
    assert outcomes[:16] == ["ok"] * 16, outcomes
    assert sorted(set(outcomes[16:])) == ["insufficient_stock", "ok"], outcomes
    assert outcomes[16:].count("ok") == 5, outcomes
    check_store = Store(path)
    assert check_store.get_item("UP")["quantity"] == 16
    assert check_store.get_item("DOWN")["quantity"] == 0
    assert len(check_store.events(sku="UP", kind="adjusted")) == 16
    assert len(check_store.events(sku="DOWN", kind="adjusted")) == 5


def test_adjustments_racing_reservations_never_cut_into_held_stock(path):
    Store(path).add_item("A-1", "Anvil", 10)
    jobs = [lambda s: s.reserve("A-1", 1, "ann", 600, now=0)] * 12
    jobs += [lambda s: s.adjust("A-1", -1, "damage", now=0)] * 12
    outcomes = [kind for kind, _ in race(path, jobs)]
    assert set(outcomes[:12]) <= {"ok", "insufficient_stock"}, outcomes
    assert set(outcomes[12:]) <= {"ok", "insufficient_stock", "stock_reserved"}, outcomes
    reserved, removed = outcomes[:12].count("ok"), outcomes[12:].count("ok")
    assert Store(path).availability("A-1", now=1) == {
        "sku": "A-1", "on_hand": 10 - removed, "reserved": reserved, "available": 10 - removed - reserved}
    assert 10 - removed >= reserved
    # Ten units, each worth one success: none is left over while a caller was refused.
    assert reserved + removed == 10, outcomes


# ---- item 25: the sweep -------------------------------------------------------------------------

def test_sweep_records_one_expired_event_per_expired_reservation(store):
    a = store.reserve("A-1", 2, " ann ", 60, now=100)
    b = store.reserve("B-2", 1, "bob", 60, now=100)
    before = log(store)
    assert store.sweep(now=159) == 0
    assert added(store, before) == []
    count = store.sweep(now=160)
    assert is_int(count) and count == 2
    events = added(store, before)
    expected = {a["id"]: ("A-1", 2, "ann"), b["id"]: ("B-2", 1, "bob")}
    assert [e["reservation_id"] for e in events] == sorted(expected)
    for event in events:
        sku, quantity, holder = expected[event["reservation_id"]]
        check(event, "expired", sku, quantity, event["reservation_id"], holder, at=160.0)


def test_sweep_records_each_reservation_once(store):
    store.reserve("A-1", 2, "ann", 60, now=100)
    assert store.sweep(now=160) == 1
    before = log(store)
    assert store.sweep(now=161) == 0
    assert store.sweep(now=10**6) == 0
    assert added(store, before) == []


def test_sweep_records_a_reservation_that_reads_already_showed_as_expired(store):
    r = store.reserve("A-1", 2, "ann", 60, now=100)
    assert store.get_reservation(r["id"], now=160)["state"] == "expired"
    assert store.availability("A-1", now=161)["reserved"] == 0
    assert store.release(r["id"], now=162)["state"] == "expired"
    assert code(lambda: store.commit(r["id"], now=163)) == "reservation_expired"
    before = log(store)
    assert store.sweep(now=164) == 1
    (event,) = added(store, before)
    check(event, "expired", "A-1", 2, r["id"], "ann", at=164.0)
    assert store.sweep(now=165) == 0


def test_sweep_leaves_released_and_committed_reservations_alone(store):
    a = store.reserve("A-1", 1, "ann", 60, now=100)
    b = store.reserve("A-1", 1, "bob", 60, now=100)
    c = store.reserve("A-1", 1, "cy", 60, now=100)
    store.release(a["id"], now=101)
    store.commit(b["id"], now=102)
    before = log(store)
    assert store.sweep(now=1000) == 1
    (event,) = added(store, before)
    check(event, "expired", "A-1", 1, c["id"], "cy", at=1000.0)
    assert store.get_reservation(a["id"], now=1000)["state"] == "released"
    assert store.get_reservation(b["id"], now=1000)["state"] == "committed"


def test_sweep_takes_what_is_expired_at_now_in_reservation_id_order(store):
    made = [store.reserve("A-1", 1, f"h{i}", ttl, now=0) for i, ttl in enumerate([300, 100, 200, 100])]
    before = log(store)
    assert store.sweep(now=250) == 3
    assert [e["reservation_id"] for e in added(store, before)] == sorted(r["id"] for r in made[1:])
    assert store.get_reservation(made[0]["id"], now=250)["state"] == "active"
    before = log(store)
    assert store.sweep(now=300) == 1
    assert [e["reservation_id"] for e in added(store, before)] == [made[0]["id"]]
    assert store.sweep(now=301) == 0


def test_a_swept_reservation_reads_expired_at_any_now(store):
    r = store.reserve("A-1", 2, "ann", 60, now=100)
    assert store.sweep(now=160) == 1
    for now in (0, 100, 159, 160, 10**6):
        assert store.get_reservation(r["id"], now=now)["state"] == "expired", now


def test_sweep_now_defaults_to_the_clock(store):
    old = store.reserve("A-1", 2, "ann", 60, now=1000)
    live = store.reserve("A-1", 1, "bob", 600)
    before = log(store)
    start = time.time()
    assert store.sweep() == 1
    (event,) = added(store, before)
    check(event, "expired", "A-1", 2, old["id"], "ann")
    assert start - 1 <= event["at"] <= time.time() + 1
    assert store.get_reservation(live["id"])["state"] == "active"


# ---- items 26 and 27: bulk reservations ---------------------------------------------------------

def test_reserve_many_reserves_every_line_in_order(store):
    made = store.reserve_many([["A-1", 2], ("B-2", 1)], "  ann  ", 60, now=100)
    assert isinstance(made, list) and len(made) == 2
    for r, (sku, quantity) in zip(made, [("A-1", 2), ("B-2", 1)], strict=True):
        assert set(r) == KEYS
        assert (r["sku"], r["quantity"], r["holder"], r["state"]) == (sku, quantity, "ann", "active")
        assert isinstance(r["expires_at"], float) and r["expires_at"] == 160.0
        assert store.get_reservation(r["id"], now=100) == r
    assert made[0]["id"] != made[1]["id"]
    assert store.availability("A-1", now=100) == {"sku": "A-1", "on_hand": 10, "reserved": 2, "available": 8}
    assert store.availability("B-2", now=100) == {"sku": "B-2", "on_hand": 5, "reserved": 1, "available": 4}
    assert store.commit(made[1]["id"], now=101)["state"] == "committed"
    assert store.release(made[0]["id"], now=101)["state"] == "released"


def test_reserve_many_records_one_reserved_event_per_line_in_line_order(store):
    before = log(store)
    made = store.reserve_many([["B-2", 1], ["A-1", 2]], "ann", 60, now=100)
    events = added(store, before)
    assert len(events) == 2
    for event, r in zip(events, made, strict=True):
        check(event, "reserved", r["sku"], r["quantity"], r["id"], "ann")
    assert [e["sku"] for e in events] == ["B-2", "A-1"]


def test_reserve_many_reserves_every_line_or_none(store, path):
    before, rows = log(store), snapshot(path)
    assert code(lambda: store.reserve_many([["A-1", 2], ["B-2", 6]], "ann", 60, now=100)) == "insufficient_stock"
    assert added(store, before) == []
    assert snapshot(path) == rows
    assert store.availability("A-1", now=100)["reserved"] == 0


INVALID_LINES = {
    "not_a_list": [None, "A-1", {"A-1": 1}, 5],
    "a_tuple_of_pairs": [(("A-1", 1),), (["A-1", 1], ["B-2", 1])],
    "empty": [[]],
    "not_a_pair": [[["A-1"]], [["A-1", 1, 2]], ["A1"], [None], [{"sku": "A-1", "quantity": 1}],
                   [["A-1", 1], "B2"]],
    "same_sku_twice": [[["A-1", 1], ["A-1", 2]], [["A-1", 1], ["B-2", 1], ("A-1", 1)]],
}


@pytest.mark.parametrize("rule", list(INVALID_LINES))
def test_reserve_many_invalid_lines(store, path, rule):
    rows = snapshot(path)
    for lines in INVALID_LINES[rule]:
        assert code(lambda lines=lines: store.reserve_many(lines, "ann", 60, now=0)) == "invalid_lines", lines
    assert snapshot(path) == rows


def test_reserve_many_takes_1_to_20_lines(store):
    skus = [f"S{i:02d}" for i in range(21)]
    for sku in skus:
        store.add_item(sku, "Thing", 1)
    assert code(lambda: store.reserve_many([[s, 1] for s in skus], "ann", 60, now=0)) == "invalid_lines"
    assert [r["sku"] for r in store.reserve_many([[s, 1] for s in skus[:20]], "ann", 60, now=0)] == skus[:20]
    assert len(store.reserve_many([[skus[20], 1]], "ann", 60, now=0)) == 1


def test_reserve_many_check_order(store):
    def refused(lines, holder="ann", ttl=60):
        return code(lambda: store.reserve_many(lines, holder, ttl, now=0))

    assert refused([["nope", 1], ["nope", 1]]) == "invalid_lines"
    assert refused([], holder="", ttl=0) == "invalid_lines"
    assert refused([["nope", 0], ["A-1", 0]]) == "unknown_sku"
    assert refused([["A-1", 0], ["nope", 1]]) == "invalid_quantity"
    assert refused([["A-1", 1], ["nope", 0]]) == "unknown_sku"
    assert refused([["A-1", 99], ["B-2", 0]], holder="", ttl=0) == "invalid_quantity"
    assert refused([["A-1", 99]], holder="", ttl=0) == "invalid_holder"
    assert refused([["A-1", 99]], ttl=0) == "invalid_ttl"
    assert refused([["A-1", 99]]) == "insufficient_stock"


def test_reserve_many_invalid_quantity_holder_and_ttl(store, path):
    rows = snapshot(path)
    for quantity in (0, -1, 1.5, "2", True, None):
        lines = [["A-1", 1], ["B-2", quantity]]
        assert code(lambda lines=lines: store.reserve_many(lines, "ann", 60, now=0)) == "invalid_quantity", quantity
    for holder in ("", "   ", "x" * 65, 5, None):
        assert code(lambda holder=holder: store.reserve_many([["A-1", 1]], holder, 60, now=0)) == "invalid_holder", holder
    for ttl in (0, 86401, 1.5, "60", True, None):
        assert code(lambda ttl=ttl: store.reserve_many([["A-1", 1]], "ann", ttl, now=0)) == "invalid_ttl", ttl
    assert snapshot(path) == rows
    (r,) = store.reserve_many([["A-1", 1]], " " + "x" * 64 + " ", 86400, now=0)
    assert (r["holder"], r["expires_at"]) == ("x" * 64, 86400.0)


def test_reserve_many_fits_against_what_is_available_at_now(store):
    store.reserve("A-1", 8, "ann", 60, now=0)
    assert code(lambda: store.reserve_many([["B-2", 1], ["A-1", 3]], "bob", 60, now=1)) == "insufficient_stock"
    assert len(store.reserve_many([["B-2", 1], ["A-1", 2]], "bob", 60, now=1)) == 2
    # At 60 ann's eight are expired and free again; bob's two are held until 61.
    assert len(store.reserve_many([["A-1", 8]], "cy", 60, now=60)) == 1
    assert store.availability("A-1", now=60) == {"sku": "A-1", "on_hand": 10, "reserved": 10, "available": 0}


# ---- items 28 to 30: idempotency keys -----------------------------------------------------------

INVALID_KEYS = {
    "empty": [""],
    "65_characters": ["x" * 65],
    "other_characters": ["a b", "a.b", "k!", "a/b"],
    "accented_letter": ["é"],
    "trailing_newline": ["abc\n"],
    "not_a_str": [5, True, b"abc"],
}


@pytest.mark.parametrize("rule", list(INVALID_KEYS))
def test_invalid_idempotency_key(store, path, rule):
    rows = snapshot(path)
    for key in INVALID_KEYS[rule]:
        refused = code(lambda key=key: store.reserve("A-1", 1, "ann", 60, now=0, idempotency_key=key))
        assert refused == "invalid_idempotency_key", key
        refused = code(lambda key=key: store.adjust("A-1", 1, "restock", now=0, idempotency_key=key))
        assert refused == "invalid_idempotency_key", key
    assert snapshot(path) == rows


def test_valid_idempotency_keys_each_make_their_own_reservation(store):
    keys = ["x" * 64, "Az09_-", "a", "-", "_", "0"]
    made = [store.reserve("A-1", 1, "ann", 60, now=0, idempotency_key=key) for key in keys]
    assert len({r["id"] for r in made}) == len(keys)
    assert store.availability("A-1", now=0)["reserved"] == len(keys)


def test_an_invalid_key_is_refused_before_every_other_check(store):
    assert code(lambda: store.reserve("nope", 0, "", 0, now=0, idempotency_key="no key")) == "invalid_idempotency_key"
    assert code(lambda: store.adjust("nope", 0, "theft", now=0, idempotency_key="no key")) == "invalid_idempotency_key"


def test_reserve_with_a_remembered_key_returns_the_remembered_reservation(store, path):
    first = store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1")
    assert set(first) == KEYS and first["state"] == "active" and first["expires_at"] == 160.0
    before, rows = log(store), snapshot(path)
    assert store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1") == first
    assert added(store, before) == []
    assert snapshot(path) == rows
    assert store.availability("A-1", now=100)["reserved"] == 3


def test_a_replay_compares_the_stripped_holder_and_not_now(store):
    first = store.reserve("A-1", 3, "  ann ", 60, now=100, idempotency_key="k1")
    before = log(store)
    assert store.reserve("A-1", 3, "ann", 60, now=130, idempotency_key="k1") == first
    assert store.reserve("A-1", 3, "\tann\n", 60, now=131, idempotency_key="k1") == first
    assert added(store, before) == []
    assert store.availability("A-1", now=131)["reserved"] == 3


def test_a_replay_is_answered_before_any_other_check(store):
    first = store.reserve("A-1", 10, "ann", 60, now=100, idempotency_key="k1")
    assert code(lambda: store.reserve("A-1", 10, "ann", 60, now=101)) == "insufficient_stock"
    assert store.reserve("A-1", 10, "ann", 60, now=101, idempotency_key="k1") == first


def test_a_replay_returns_the_value_the_first_call_returned(store):
    first = store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1")
    store.release(first["id"], now=101)
    before = log(store)
    assert store.reserve("A-1", 3, "ann", 60, now=102, idempotency_key="k1") == first
    assert added(store, before) == []
    assert store.get_reservation(first["id"], now=102)["state"] == "released"
    assert store.availability("A-1", now=102)["reserved"] == 0


def test_a_remembered_key_with_other_arguments_is_a_conflict(store, path):
    store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1")
    before, rows = log(store), snapshot(path)
    for args in (("B-2", 3, "ann", 60), ("A-1", 2, "ann", 60), ("A-1", 3, "bob", 60), ("A-1", 3, "ann", 61)):
        refused = code(lambda args=args: store.reserve(*args, now=100, idempotency_key="k1"))
        assert refused == "idempotency_conflict", args
    assert added(store, before) == []
    assert snapshot(path) == rows


def test_a_conflict_is_decided_before_any_other_check(store):
    store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1")
    for args in (("nope", 3, "ann", 60), ("A-1", 0, "ann", 60), ("A-1", 3, "", 60), ("A-1", 3, "ann", 0),
                 ("A-1", 99, "ann", 60)):
        refused = code(lambda args=args: store.reserve(*args, now=100, idempotency_key="k1"))
        assert refused == "idempotency_conflict", args
    assert code(lambda: store.adjust("nope", 0, "theft", now=100, idempotency_key="k1")) == "idempotency_conflict"


def test_a_key_belongs_to_one_operation(store):
    store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1")
    store.adjust("B-2", 2, "restock", now=100, idempotency_key="k2")
    assert code(lambda: store.adjust("A-1", 3, "restock", now=100, idempotency_key="k1")) == "idempotency_conflict"
    assert code(lambda: store.reserve("B-2", 2, "ann", 60, now=100, idempotency_key="k2")) == "idempotency_conflict"
    assert store.availability("A-1", now=100) == {"sku": "A-1", "on_hand": 10, "reserved": 3, "available": 7}
    assert store.availability("B-2", now=100) == {"sku": "B-2", "on_hand": 7, "reserved": 0, "available": 7}


def test_adjust_with_a_remembered_key_is_applied_once(store, path):
    first = store.adjust("A-1", 5, "restock", now=100, idempotency_key="k1")
    assert first == {"sku": "A-1", "on_hand": 15, "reserved": 0, "available": 15}
    before, rows = log(store), snapshot(path)
    assert store.adjust("A-1", 5, "restock", now=100, idempotency_key="k1") == first
    assert added(store, before) == []
    assert snapshot(path) == rows
    store.adjust("A-1", -4, "damage", now=101)
    assert store.adjust("A-1", 5, "restock", now=102, idempotency_key="k1") == first
    assert store.get_item("A-1")["quantity"] == 11


def test_adjust_with_a_remembered_key_and_other_arguments_is_a_conflict(store, path):
    store.adjust("A-1", 5, "restock", now=100, idempotency_key="k1")
    rows = snapshot(path)
    for args in (("A-1", 4, "restock"), ("A-1", 5, "correction"), ("B-2", 5, "restock"), ("A-1", -5, "restock")):
        assert code(lambda args=args: store.adjust(*args, now=100, idempotency_key="k1")) == "idempotency_conflict", args
    assert snapshot(path) == rows


def test_a_refused_call_remembers_nothing(store):
    assert code(lambda: store.reserve("A-1", 11, "ann", 60, now=100, idempotency_key="k1")) == "insufficient_stock"
    assert store.reserve("A-1", 2, "ann", 60, now=100, idempotency_key="k1")["quantity"] == 2
    assert code(lambda: store.adjust("A-1", -99, "damage", now=100, idempotency_key="k2")) == "insufficient_stock"
    assert store.adjust("A-1", 1, "restock", now=100, idempotency_key="k2")["on_hand"] == 11


def test_keys_are_kept_in_the_database_and_never_expire(store, path):
    first = store.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1")
    other = Store(path)
    assert other.reserve("A-1", 3, "ann", 60, now=100, idempotency_key="k1") == first
    assert code(lambda: other.reserve("A-1", 4, "ann", 60, now=100, idempotency_key="k1")) == "idempotency_conflict"
    assert Store(path).reserve("A-1", 3, "ann", 60, now=10**9, idempotency_key="k1") == first
    assert other.availability("A-1", now=10**9) == {"sku": "A-1", "on_hand": 10, "reserved": 0, "available": 10}


def test_one_reservation_for_one_key_under_threads(path):
    Store(path).add_item("A-1", "Anvil", 10)
    outcomes = race(path, [lambda s: s.reserve("A-1", 1, "ann", 600, now=0, idempotency_key="one-key")] * 24)
    assert {kind for kind, _ in outcomes} == {"ok"}, outcomes
    first = outcomes[0][1]
    assert set(first) == KEYS and first["state"] == "active"
    assert all(value == first for _, value in outcomes)
    check_store = Store(path)
    assert check_store.availability("A-1", now=1) == {"sku": "A-1", "on_hand": 10, "reserved": 1, "available": 9}
    assert len(check_store.events(kind="reserved")) == 1
    assert check_store.get_reservation(first["id"], now=1) == first
