"""The SQLite store. One Store per database file; safe to open many."""

import contextlib
import sqlite3
import time

SCHEMA_VERSION = 2
MAX_HOLDER_LENGTH = 64
MAX_TTL_SECONDS = 86400
#: Seconds SQLite waits for a lock before the store tries again. Reservation
#: calls keep trying, so a busy database delays them but never fails them.
BUSY_TIMEOUT = 10
#: The widest id an SQLite INTEGER can hold; larger ids can never exist.
_MAX_ID = 2**63 - 1
#: SQLITE_BUSY and SQLITE_LOCKED: the database is in use, try again.
_BUSY_CODES = (5, 6)

_COLUMNS = "id, sku, quantity, holder, expires_at, state"


class StoreError(Exception):
    """A request the store refuses. ``code`` is a stable machine name."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _encodable(text):
    """True when SQLite can store ``text`` (it holds no lone surrogate)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _storable(text):
    """``text`` as SQLite can hold it: itself, or its surrogate-preserving bytes."""
    return text if _encodable(text) else text.encode("utf-8", "surrogatepass")


def _text(value):
    """The inverse of ``_storable``."""
    return value.decode("utf-8", "surrogatepass") if isinstance(value, bytes) else value


def _is_busy(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in _BUSY_CODES
    return "locked" in str(exc) or "busy" in str(exc)


def _patiently(step):
    """Run ``step`` until it gets past any lock held by another connection."""
    while True:
        try:
            return step()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise


def _clock(now):
    # Kept exact: an int time is never rounded to a float before use.
    return time.time() if now is None else now


def _check_quantity(quantity):
    if not _is_int(quantity) or quantity < 1:
        raise StoreError("invalid_quantity", "quantity must be a whole number, 1 or more")


def _check_holder(holder):
    if isinstance(holder, str):
        holder = holder.strip()
        if holder and len(holder) < MAX_HOLDER_LENGTH:
            return holder
    raise StoreError("invalid_holder", "holder must be text of 1 to 64 characters")


def _check_ttl(ttl_seconds):
    if not _is_int(ttl_seconds) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise StoreError("invalid_ttl", "ttl_seconds must be a whole number from 1 to 86400")


def _check_id(reservation_id):
    if not _is_int(reservation_id) or not 1 <= reservation_id <= _MAX_ID:
        raise StoreError("unknown_reservation", "no reservation with that id")
    return int(reservation_id)


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._upgrade()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # -- schema ---------------------------------------------------------

    @staticmethod
    def _version(db):
        return db.execute("PRAGMA user_version").fetchone()[0]

    def _upgrade(self):
        """Bring the file to SCHEMA_VERSION, once, even with many openers."""
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT, isolation_level=None)
        try:
            if _patiently(lambda: self._version(db)) >= SCHEMA_VERSION:
                return
            _patiently(lambda: db.execute("BEGIN IMMEDIATE"))
            try:
                if self._version(db) < SCHEMA_VERSION:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS items ("
                        "sku TEXT PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL)"
                    )
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS reservations ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "sku TEXT NOT NULL REFERENCES items(sku), "
                        "quantity INTEGER NOT NULL, "
                        "holder TEXT NOT NULL, "
                        "expires_at REAL NOT NULL, "
                        "state TEXT NOT NULL)"
                    )
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                _patiently(lambda: db.execute("COMMIT"))
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def schema_version(self):
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT)
        try:
            return _patiently(lambda: self._version(db))
        finally:
            db.close()

    # -- items (unchanged) ----------------------------------------------

    def add_item(self, sku, name, quantity):
        if not isinstance(sku, str) or not sku.strip() or len(sku) > 32:
            raise StoreError("invalid_sku", "sku must be 1 to 32 characters")
        if not isinstance(name, str) or not name.strip():
            raise StoreError("invalid_name", "name must not be empty")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise StoreError("invalid_quantity", "quantity must be a whole number, 0 or more")
        try:
            with self._connect() as db:
                db.execute("INSERT INTO items VALUES (?, ?, ?)", (sku, name, quantity))
        except sqlite3.IntegrityError:
            raise StoreError("duplicate_sku", f"sku {sku!r} already exists") from None
        return self.get_item(sku)

    def get_item(self, sku):
        with self._connect() as db:
            row = db.execute("SELECT sku, name, quantity FROM items WHERE sku = ?", (sku,)).fetchone()
        return dict(row) if row else None

    def list_items(self):
        with self._connect() as db:
            rows = db.execute("SELECT sku, name, quantity FROM items ORDER BY sku").fetchall()
        return [dict(row) for row in rows]

    # -- reservations ---------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self):
        """One write transaction, taken before anything is read.

        BEGIN IMMEDIATE serialises writers across connections; it and COMMIT
        are retried for as long as another connection holds a lock, so a
        busy database never fails a call. A StoreError is raised before any
        change except recorded expiries, which may stand, so it commits;
        anything else rolls back.
        """
        db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            _patiently(lambda: db.execute("BEGIN IMMEDIATE"))
            try:
                yield db
            except StoreError:
                try:
                    _patiently(lambda: db.execute("COMMIT"))
                except sqlite3.Error:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                raise
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            _patiently(lambda: db.execute("COMMIT"))
        finally:
            db.close()

    @staticmethod
    def _expire(db, now):
        """Record every active reservation whose time is up at ``now``.

        Compared in Python, where an int and a float compare exactly, so an
        int ``now`` is never rounded and any size of int works.
        """
        rows = db.execute("SELECT id, expires_at FROM reservations WHERE state = 'active'")
        expired = [(row[0],) for row in rows.fetchall() if row[1] < now]
        db.executemany("UPDATE reservations SET state = 'expired' WHERE id = ?", expired)

    @staticmethod
    def _item(db, sku):
        if isinstance(sku, str) and _encodable(sku):
            row = db.execute("SELECT sku, quantity FROM items WHERE sku = ?", (sku,)).fetchone()
            if row is not None:
                return row
        raise StoreError("unknown_sku", "no item with that sku")

    @staticmethod
    def _reserved(db, sku):
        return db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM reservations WHERE sku = ? AND state = 'active'",
            (sku,),
        ).fetchone()[0]

    @staticmethod
    def _fetch(db, reservation_id):
        row = db.execute(
            f"SELECT {_COLUMNS} FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise StoreError("unknown_reservation", f"no reservation with id {reservation_id}")
        return {
            "id": row["id"],
            "sku": row["sku"],
            "quantity": row["quantity"],
            "holder": _text(row["holder"]),
            "expires_at": float(row["expires_at"]),
            "state": row["state"],
        }

    def reserve(self, sku, quantity, holder, ttl_seconds, now=None):
        now = _clock(now)
        with self._transaction() as db:
            item = self._item(db, sku)
            _check_quantity(quantity)
            holder = _check_holder(holder)
            _check_ttl(ttl_seconds)
            self._expire(db, now)
            available = item["quantity"] - self._reserved(db, item["sku"])
            if quantity > available:
                raise StoreError(
                    "insufficient_stock", f"only {available} of sku {item['sku']!r} available"
                )
            expires_at = float(now + ttl_seconds)
            cursor = db.execute(
                "INSERT INTO reservations (sku, quantity, holder, expires_at, state) "
                "VALUES (?, ?, ?, ?, 'active')",
                (item["sku"], int(quantity), _storable(holder), expires_at),
            )
            return self._fetch(db, cursor.lastrowid)

    def release(self, reservation_id, now=None):
        now = _clock(now)
        reservation_id = _check_id(reservation_id)
        with self._transaction() as db:
            self._expire(db, now)
            reservation = self._fetch(db, reservation_id)
            if reservation["state"] == "committed":
                raise StoreError("already_committed", "the reservation is already committed")
            if reservation["state"] == "expired":
                raise StoreError("reservation_expired", "the reservation has expired")
            if reservation["state"] == "active":
                db.execute(
                    "UPDATE reservations SET state = 'released' WHERE id = ?", (reservation_id,)
                )
                reservation = self._fetch(db, reservation_id)
            return reservation

    def commit(self, reservation_id, now=None):
        now = _clock(now)
        reservation_id = _check_id(reservation_id)
        with self._transaction() as db:
            self._expire(db, now)
            reservation = self._fetch(db, reservation_id)
            state = reservation["state"]
            if state == "committed":
                raise StoreError("already_committed", "the reservation is already committed")
            if state == "released":
                raise StoreError("reservation_released", "the reservation was released")
            if state == "expired":
                raise StoreError("reservation_expired", "the reservation has expired")
            db.execute(
                "UPDATE reservations SET state = 'committed' WHERE id = ?", (reservation_id,)
            )
            db.execute(
                "UPDATE items SET quantity = quantity - ? WHERE sku = ?",
                (reservation["quantity"], reservation["sku"]),
            )
            return self._fetch(db, reservation_id)

    def get_reservation(self, reservation_id, now=None):
        now = _clock(now)
        reservation_id = _check_id(reservation_id)
        with self._transaction() as db:
            self._expire(db, now)
            return self._fetch(db, reservation_id)

    def availability(self, sku, now=None):
        now = _clock(now)
        with self._transaction() as db:
            item = self._item(db, sku)
            reserved = self._reserved(db, item["sku"])
        return {
            "sku": item["sku"],
            "on_hand": item["quantity"],
            "reserved": reserved,
            "available": item["quantity"] - reserved,
        }
