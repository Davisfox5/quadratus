"""The SQLite store. One Store per database file; safe to open many."""

import sqlite3


class StoreError(Exception):
    """A request the store refuses. ``code`` is a stable machine name."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS items ("
                "sku TEXT PRIMARY KEY, name TEXT NOT NULL, quantity INTEGER NOT NULL)"
            )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

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
