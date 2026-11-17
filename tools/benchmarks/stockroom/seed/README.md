# stockroom

A small inventory service: a SQLite store, a JSON HTTP API and a command line.
Standard library only (pytest for the tests).

## Store

`stockroom.store.Store(path)` opens (and creates) the database file.

- `add_item(sku, name, quantity)` adds an item and returns it.
- `get_item(sku)` returns the item or `None`.
- `list_items()` returns every item, ordered by sku.

A refused request raises `StoreError` with a stable `code` and a `message`.

## HTTP API

`stockroom.api.make_server(db_path, host="127.0.0.1", port=0)` returns a server.

- `GET /items` returns `{"items": [...]}`.
- `POST /items` with `{"sku", "name", "quantity"}` returns the item, status 201.

Errors are `{"error": code, "message": text}`.

## Command line

    python -m stockroom.cli --db PATH list
    python -m stockroom.cli --db PATH add SKU NAME QUANTITY

Success prints one JSON object on stdout. A store refusal prints
`{"error", "message"}` on stderr and exits 1.

## Tests

    python -m pytest -q
