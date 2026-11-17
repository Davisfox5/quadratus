import sqlite3
import threading
import time

import pytest

import stockroom.store as store_module
from stockroom.store import Store, StoreError

T0 = 1_000_000.0
BIG = 2**53
HUGE = 10**400
KEYS = {"id", "sku", "quantity", "holder", "expires_at", "state"}


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "s.db")
    store.add_item("A-1", "Anvil", 5)
    store.add_item("B-2", "Bucket", 0)
    return store


def snapshot(path):
    """Every table's rows and the file's user version, read straight from SQLite."""
    db = sqlite3.connect(str(path))
    try:
        tables = sorted(
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        rows = {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}
        return db.execute("PRAGMA user_version").fetchone()[0], rows
    finally:
        db.close()


def make_old_database(path):
    db = sqlite3.connect(str(path))
    db.execute(
        "CREATE TABLE items (sku TEXT PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL)"
    )
    db.executemany(
        "INSERT INTO items VALUES (?, ?, ?)", [("A-1", "Anvil", 3), ("B-2", "Bucket", 0)]
    )
    db.commit()
    db.close()


OLD_ITEMS = [
    {"sku": "A-1", "name": "Anvil", "quantity": 3},
    {"sku": "B-2", "name": "Bucket", "quantity": 0},
]


def raw_state(path, reservation_id):
    """The state column as stored, read straight from SQLite."""
    db = sqlite3.connect(str(path))
    try:
        return db.execute(
            "SELECT state FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()[0]
    finally:
        db.close()


# -- item 1: schema version ------------------------------------------------


def test_new_database_is_version_2_and_reopening_changes_nothing(tmp_path):
    path = tmp_path / "s.db"
    assert Store(path).schema_version() == 2
    before = path.read_bytes()
    assert Store(path).schema_version() == 2
    assert path.read_bytes() == before


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "s.db"
    make_old_database(path)
    old_rows = snapshot(path)[1]["items"]
    store = Store(path)
    assert store.schema_version() == 2
    assert store.list_items() == OLD_ITEMS
    assert snapshot(path)[1]["items"] == old_rows
    before = path.read_bytes()
    reopened = Store(path)
    assert path.read_bytes() == before
    assert reopened.schema_version() == 2
    assert reopened.reserve("A-1", 3, "alice", 60, now=T0)["state"] == "active"


def test_concurrent_opens_of_an_old_database_upgrade_once(tmp_path):
    path = tmp_path / "s.db"
    make_old_database(path)
    count = 8
    barrier = threading.Barrier(count)
    failures = []

    def open_store():
        try:
            barrier.wait(timeout=10)
            Store(path)
        except BaseException as exc:  # recorded and asserted below
            failures.append(repr(exc))

    threads = [threading.Thread(target=open_store) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failures == []
    store = Store(path)
    assert store.schema_version() == 2
    assert store.list_items() == OLD_ITEMS


# -- items 2 and 3: the reservation dict -----------------------------------


def test_reserve_returns_the_reservation(store):
    reservation = store.reserve("A-1", 2, "  alice  ", 60, now=T0)
    assert set(reservation) == KEYS
    assert isinstance(reservation["id"], int) and not isinstance(reservation["id"], bool)
    assert reservation["id"] > 0
    assert reservation == {
        "id": reservation["id"],
        "sku": "A-1",
        "quantity": 2,
        "holder": "alice",
        "expires_at": T0 + 60,
        "state": "active",
    }
    assert isinstance(reservation["expires_at"], float)


def test_expires_at_is_a_float_for_int_and_float_now(store):
    from_int = store.reserve("A-1", 1, "a", 60, now=1000)
    assert from_int["expires_at"] == 1060.0 and isinstance(from_int["expires_at"], float)
    assert store.reserve("A-1", 1, "b", 60, now=1000.5)["expires_at"] == 1060.5






def test_get_reservation_returns_the_current_state(store):
    reservation = store.reserve("A-1", 2, "alice", 60, now=T0)
    assert store.get_reservation(reservation["id"], now=T0 + 1) == reservation
    store.release(reservation["id"], now=T0 + 1)
    assert store.get_reservation(reservation["id"], now=T0 + 2) == dict(reservation, state="released")


# -- item 6: transitions ---------------------------------------------------


def test_release_turns_active_into_released(store):
    reservation = store.reserve("A-1", 2, "alice", 60, now=T0)
    released = store.release(reservation["id"], now=T0 + 1)
    assert released == dict(reservation, state="released")
    assert store.release(reservation["id"], now=T0 + 1) == released
    assert store.release(reservation["id"], now=T0 + 100) == released
    assert store.availability("A-1", now=T0 + 1)["reserved"] == 0




def test_commit_turns_active_into_committed_and_takes_stock(store):
    reservation = store.reserve("A-1", 2, "alice", 60, now=T0)
    committed = store.commit(reservation["id"], now=T0 + 1)
    assert committed == dict(reservation, state="committed")
    assert store.get_item("A-1") == {"sku": "A-1", "name": "Anvil", "quantity": 3}
    assert store.availability("A-1", now=T0 + 1) == {
        "sku": "A-1",
        "on_hand": 3,
        "reserved": 0,
        "available": 3,
    }
    assert store.get_reservation(reservation["id"], now=T0 + 100)["state"] == "committed"


# -- item 4: availability --------------------------------------------------


def test_availability_counts_active_reservations(store):
    assert store.availability("A-1", now=T0) == {
        "sku": "A-1",
        "on_hand": 5,
        "reserved": 0,
        "available": 5,
    }
    first = store.reserve("A-1", 2, "a", 60, now=T0)
    second = store.reserve("A-1", 1, "b", 60, now=T0)
    assert store.availability("A-1", now=T0) == {
        "sku": "A-1",
        "on_hand": 5,
        "reserved": 3,
        "available": 2,
    }
    store.release(first["id"], now=T0)
    assert store.availability("A-1", now=T0) == {
        "sku": "A-1",
        "on_hand": 5,
        "reserved": 1,
        "available": 4,
    }
    store.commit(second["id"], now=T0)
    assert store.availability("A-1", now=T0) == {
        "sku": "A-1",
        "on_hand": 4,
        "reserved": 0,
        "available": 4,
    }


# -- item 5: expiry --------------------------------------------------------








def _commit_refusal(store, reservation_id):
    with pytest.raises(StoreError) as refused:
        store.commit(reservation_id, now=HUGE)
    return refused.value.code








def test_now_defaults_to_the_clock(store):
    before = time.time()
    reservation = store.reserve("A-1", 1, "alice", 60)
    after = time.time()
    assert before + 60 <= reservation["expires_at"] <= after + 60
    assert store.get_reservation(reservation["id"])["state"] == "active"
    assert store.availability("A-1")["reserved"] == 1
    old = store.reserve("A-1", 1, "bob", 60, now=time.time() - 120)
    assert store.get_reservation(old["id"])["state"] == "expired"
    assert store.availability("A-1")["reserved"] == 1


# -- item 7: accepted bounds and check order -------------------------------




def test_holder_with_a_lone_surrogate_is_kept(store):
    reservation = store.reserve("A-1", 1, " \udc80 ", 60, now=T0)
    assert reservation["holder"] == "\udc80"
    assert store.get_reservation(reservation["id"], now=T0)["holder"] == "\udc80"
    assert store.release(reservation["id"], now=T0)["holder"] == "\udc80"


@pytest.mark.parametrize(
    "args, code",
    [
        (("Z-9", 0, "", 0), "unknown_sku"),
        (("A-1", 0, "", 0), "invalid_quantity"),
        (("A-1", 99, "", 0), "invalid_holder"),
        (("A-1", 99, "h", 0), "invalid_ttl"),
        (("A-1", 99, "h", 60), "insufficient_stock"),
    ],
)
def test_reserve_checks_arguments_in_order(store, args, code):
    with pytest.raises(StoreError) as refused:
        store.reserve(*args, now=T0)
    assert refused.value.code == code


# -- items 7 and 8: every refusal, and that it changes nothing -------------

NOW = T0 + 10


def populated(tmp_path):
    """A-1 on hand 4, one reservation in each state; at NOW, 3 are available."""
    store = Store(tmp_path / "s.db")
    store.add_item("A-1", "Anvil", 5)
    store.add_item("B-2", "Bucket", 0)
    ids = {"active": store.reserve("A-1", 1, "alice", 600, now=T0)["id"]}
    ids["released"] = store.reserve("A-1", 1, "bob", 600, now=T0)["id"]
    store.release(ids["released"], now=T0)
    ids["committed"] = store.reserve("A-1", 1, "carol", 600, now=T0)["id"]
    store.commit(ids["committed"], now=T0)
    ids["expired"] = store.reserve("A-1", 1, "dave", 10, now=T0)["id"]
    assert store.get_reservation(ids["expired"], now=NOW)["state"] == "expired"
    assert store.availability("A-1", now=NOW)["available"] == 3
    return store, ids


def _reserve(sku="A-1", quantity=1, holder="h", ttl=60):
    return lambda store, ids: store.reserve(sku, quantity, holder, ttl, now=NOW)


REFUSALS = [
    ("unknown-sku", _reserve(sku="Z-9"), "unknown_sku"),
    ("sku-not-text", _reserve(sku=42), "unknown_sku"),
    ("sku-none", _reserve(sku=None), "unknown_sku"),
    ("sku-unstorable", _reserve(sku="\udc80"), "unknown_sku"),
    ("availability-unknown-sku", lambda s, ids: s.availability("Z-9", now=NOW), "unknown_sku"),
    ("quantity-zero", _reserve(quantity=0), "invalid_quantity"),
    ("quantity-negative", _reserve(quantity=-1), "invalid_quantity"),
    ("quantity-float", _reserve(quantity=1.0), "invalid_quantity"),
    ("quantity-fraction", _reserve(quantity=1.5), "invalid_quantity"),
    ("quantity-text", _reserve(quantity="1"), "invalid_quantity"),
    ("quantity-bool", _reserve(quantity=True), "invalid_quantity"),
    ("quantity-none", _reserve(quantity=None), "invalid_quantity"),
    ("holder-empty", _reserve(holder=""), "invalid_holder"),
    ("holder-blank", _reserve(holder="   "), "invalid_holder"),
    ("holder-too-long", _reserve(holder="x" * 65), "invalid_holder"),
    ("holder-too-long-stripped", _reserve(holder=" " + "x" * 65 + " "), "invalid_holder"),
    ("holder-not-text", _reserve(holder=7), "invalid_holder"),
    ("holder-none", _reserve(holder=None), "invalid_holder"),
    ("ttl-zero", _reserve(ttl=0), "invalid_ttl"),
    ("ttl-negative", _reserve(ttl=-5), "invalid_ttl"),
    ("ttl-too-long", _reserve(ttl=86401), "invalid_ttl"),
    ("ttl-huge", _reserve(ttl=2**63), "invalid_ttl"),
    ("ttl-float", _reserve(ttl=60.0), "invalid_ttl"),
    ("ttl-text", _reserve(ttl="60"), "invalid_ttl"),
    ("ttl-bool", _reserve(ttl=True), "invalid_ttl"),
    ("ttl-none", _reserve(ttl=None), "invalid_ttl"),
    ("stock-over-available", _reserve(quantity=4), "insufficient_stock"),
    ("stock-empty-item", _reserve(sku="B-2"), "insufficient_stock"),
    ("stock-huge", _reserve(quantity=2**63), "insufficient_stock"),
    (
        "release-committed",
        lambda s, ids: s.release(ids["committed"], now=NOW),
        "already_committed",
    ),
    (
        "commit-committed",
        lambda s, ids: s.commit(ids["committed"], now=NOW),
        "already_committed",
    ),
    (
        "commit-released",
        lambda s, ids: s.commit(ids["released"], now=NOW),
        "reservation_released",
    ),
    (
        "commit-expired",
        lambda s, ids: s.commit(ids["expired"], now=NOW),
        "reservation_expired",
    ),
] + [
    (
        f"{method}-id-{label}",
        lambda s, ids, method=method, value=value: getattr(s, method)(value, now=NOW),
        "unknown_reservation",
    )
    for method in ("get_reservation", "release", "commit")
    for label, value in [
        ("absent", 999),
        ("zero", 0),
        ("negative", -1),
        ("bool", True),
        ("float", 1.0),
        ("text", "1"),
        ("none", None),
        ("too-wide", 2**63),
        ("too-negative", -(2**63) - 1),
    ]
]




# -- item 9: no overselling under real threads -----------------------------


def test_concurrent_reserves_never_oversell(tmp_path):
    path = tmp_path / "s.db"
    Store(path).add_item("A-1", "Anvil", 5)
    count = 12
    barrier = threading.Barrier(count)
    lock = threading.Lock()
    outcomes, failures = [], []

    def reserve(index):
        try:
            own = Store(path)
            barrier.wait(timeout=10)
            outcome = own.reserve("A-1", 1, f"t{index}", 600, now=T0)
        except StoreError as exc:
            outcome = exc.code
        except BaseException as exc:  # recorded and asserted below
            with lock:
                failures.append(repr(exc))
            return
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failures == []
    granted = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if not isinstance(o, dict)]
    assert len(granted) == 5
    assert refused == ["insufficient_stock"] * (count - 5)
    assert len({o["id"] for o in granted}) == 5
    assert Store(path).availability("A-1", now=T0) == {
        "sku": "A-1",
        "on_hand": 5,
        "reserved": 5,
        "available": 0,
    }


@pytest.mark.parametrize(
    "hold",
    [["BEGIN IMMEDIATE"], ["BEGIN", "SELECT count(*) FROM items"]],
    ids=["writer", "reader"],
)
def test_reserve_outlasts_a_lock_held_past_the_busy_timeout(tmp_path, monkeypatch, hold):
    path = tmp_path / "s.db"
    Store(path).add_item("A-1", "Anvil", 5)
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT", 0.05)
    blocker = sqlite3.connect(str(path), isolation_level=None)
    outcomes = []

    def reserve():
        try:
            outcomes.append(Store(path).reserve("A-1", 1, "h", 60, now=T0))
        except BaseException as exc:  # recorded and asserted below
            outcomes.append(repr(exc))

    thread = threading.Thread(target=reserve)
    try:
        for statement in hold:
            blocker.execute(statement).fetchall()
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive()
        assert outcomes == []
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()
    thread.join(timeout=30)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], dict), outcomes[0]
    assert outcomes[0]["state"] == "active"
    assert Store(path).availability("A-1", now=T0)["reserved"] == 1
