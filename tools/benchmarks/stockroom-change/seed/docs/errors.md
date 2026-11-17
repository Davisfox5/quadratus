# Reservation error codes

HTTP errors are JSON objects `{"error": code, "message": text}` with a non-empty
message and `Content-Type: application/json`. Command-line failures use the same
object on stderr. A dash means the code never reaches HTTP.

| Code | HTTP status | Meaning |
| --- | --- | --- |
| `unknown_sku` | 404 | No item exists with that sku. |
| `invalid_quantity` | 400 | Reservation quantity is not an int, is a bool, or is less than 1; HTTP also rejects fractional or exponent JSON numbers. |
| `insufficient_stock` | 409 | Requested quantity exceeds available stock. |
| `invalid_holder` | 400 | Holder is not a str, is empty after stripping, or exceeds 64 characters after stripping. |
| `invalid_ttl` | 400 | TTL is not an int, is a bool, or is outside 1 to 86400 seconds; HTTP also rejects fractional or exponent JSON numbers. |
| `unknown_reservation` | 404 | No reservation exists with that id; store IDs that are not positive ints also receive this code. |
| `already_committed` | 409 | Release or commit was requested for a committed reservation. |
| `reservation_released` | 409 | Commit was requested for a released reservation. |
| `reservation_expired` | 409 | Commit was requested for an expired reservation. |
| `bad_request` | 400, 414, 431, 505 | Server rejected malformed request syntax, an invalid or unsupported HTTP version, an over-long request or header line, or too many headers. |
| `invalid_json` | 400 | Body is invalid JSON or invalid UTF-8, including the forbidden JSON constants NaN, Infinity and -Infinity. |
| `invalid_body` | 400 | Valid JSON body is not an object. |
| `missing_field` | 400 | Reservation body lacks sku, quantity or holder. |
| `unknown_field` | 400 | Reservation body has a key other than sku, quantity, holder or ttl_seconds. |
| `invalid_length` | 400 | Content-Length is missing or is not a non-negative decimal integer. |
| `invalid_id` | 400 | Path ID is not a decimal integer of 1 to 18 digits without a sign, or is zero. |
| `not_found` | 404 | Path does not identify a known resource. |
| `method_not_allowed` | 405 | A known path was requested with another method. |
| `body_too_large` | 413 | Content-Length exceeds 65536; the body is not read. |
| `usage` | - | A new command rejects its arguments, including non-decimal integers, missing arguments, extra arguments or an unusable --db value (an invalid database path or a database that raises a SQLite error during opening or a store call); main returns 2. |
