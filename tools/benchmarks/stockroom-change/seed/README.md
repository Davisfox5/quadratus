# stockroom

A small inventory service: a SQLite store, a JSON HTTP API and a command line.
Standard library only (pytest for the tests).

## Store

`stockroom.store.Store(path)` opens (and creates) the database file.

- `add_item(sku, name, quantity)` adds an item and returns it.
- `get_item(sku)` returns the item or `None`.
- `list_items()` returns every item, ordered by sku.
- `schema_version()` returns 2. New databases use version 2; existing item-only
  databases upgrade in place, preserving every item. Reopening changes nothing.
- `reserve(sku, quantity, holder, ttl_seconds, now=None)` holds stock and returns
  an active reservation.
- `release(reservation_id, now=None)` releases a reservation and returns it.
- `commit(reservation_id, now=None)` commits a reservation and returns it.
- `get_reservation(reservation_id, now=None)` returns its current state.
- `availability(sku, now=None)` returns `{"sku", "on_hand", "reserved",
  "available"}`. Reserved stock is the total quantity of active reservations;
  available stock is on-hand quantity minus reserved quantity.

Reservation dictionaries contain exactly `id`, `sku`, `quantity`, `holder`,
`expires_at` and `state`. IDs are positive integers, unique and never reused.
The holder is stored with surrounding whitespace stripped. `expires_at` is a
float equal to `now + ttl_seconds`. Optional `now` is an int or float in seconds;
when omitted, it uses `time.time()`.

Reservations have four states: `active`, `released`, `committed` and `expired`.
Reserve creates `active`. Release changes `active` to `released`, returns
`released` unchanged, returns `expired` as expired, and refuses `committed` with
`already_committed`. Commit changes `active` to `committed` and reduces the item's
quantity by the reserved quantity. It refuses `committed` with
`already_committed`, `released` with `reservation_released`, and `expired` with
`reservation_expired`.

An active reservation expires when `expires_at` is at or before `now`. It reads
as `expired`, is not counted as reserved, and can never become active again.

Reservation validation checks sku, quantity, holder, ttl, then available stock.
Quantity must be an int other than bool and at least 1. Holder must be a str
with 1 to 64 characters after stripping. TTL must be an int other than bool
from 1 to 86400 seconds. Concurrent reservations cannot oversell stock.

A refused request raises `StoreError` with a stable `code` and a `message`.
Refused calls, requests and commands leave the database unchanged; recording
that a reservation has expired is permitted. See [error codes](docs/errors.md).

## HTTP API

`stockroom.api.make_server(db_path, host="127.0.0.1", port=0)` returns a server.

- `GET /items` returns `{"items": [...]}`.
- `POST /items` with `{"sku", "name", "quantity"}` returns the item, status 201.
- `POST /reservations` accepts an object with `sku`, `quantity`, `holder` and
  optional `ttl_seconds` (default 900), returning the reservation, status 201.
- `GET /reservations/<id>` returns the reservation, status 200.
- `DELETE /reservations/<id>` releases it and returns the reservation, status 200.
- `POST /reservations/<id>/commit` commits it and returns the reservation, status 200.
- `GET /items/<sku>/availability` returns the availability object, status 200.

Errors are `{"error": code, "message": text}`.
Every error from the new routes has a non-empty message and
`Content-Type: application/json`; [error codes](docs/errors.md) lists statuses.
Server parsing rejections also send full JSON error responses with Content-Length
and close the connection, including for versionless requests, using `bad_request`
for statuses 400, 414, 431 and 505.

Reservation bodies require valid UTF-8 and strict JSON: NaN, Infinity and
-Infinity are invalid. Fractional or exponent JSON numbers, including `2.0` and
`2e0`, are invalid for quantity and ttl. Only the four documented body fields
are accepted. Content-Length must be present and a non-negative decimal integer;
values above 65536 receive status 413 without reading the body. Reservation path
IDs must be decimal integers of 1 to 18 digits, without a sign, and nonzero.

## Command line

    python -m stockroom.cli --db PATH list
    python -m stockroom.cli --db PATH add SKU NAME QUANTITY
    python -m stockroom.cli --db PATH reserve SKU QUANTITY HOLDER [--ttl SECONDS]
    python -m stockroom.cli --db PATH release ID
    python -m stockroom.cli --db PATH commit ID
    python -m stockroom.cli --db PATH show ID
    python -m stockroom.cli --db PATH availability SKU

Success prints one JSON object on stdout. A store refusal prints
`{"error", "message"}` on stderr and exits 1.

For the five reservation commands, `reserve` defaults to a ttl of 900 seconds,
`show` returns the reservation, and `availability` returns the availability
object. Successful commands print exactly one JSON object line on stdout,
nothing on stderr, and `main(argv)` returns 0.

Every failure of these five commands prints exactly one JSON object line
`{"error": code, "message": text}` with a non-empty message on stderr and nothing
on stdout. `main(argv)` returns 1 for a store refusal, using the store's code,
or 2 with code `usage` for command-line rejection, without raising `SystemExit`.
An unusable `--db` value also produces this JSON error with code `usage` and
return status 2, including a value that cannot be used as a database path at all
or a database that raises a SQLite error during opening or a store call.
Command-line rejection includes non-decimal-integer QUANTITY, ID or --ttl values,
missing arguments and extra arguments. The existing `list` and `add` behavior,
including argument rejection, is unchanged.

## Tests

    python -m pytest -q
