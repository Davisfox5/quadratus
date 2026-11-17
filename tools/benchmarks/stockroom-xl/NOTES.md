# Notes on the extended stockroom graders

Goal: `xl/goal.txt` (39 items). Graders: `xl/graders/`. Reference: `xl/reference/`.

## Counts and results

| Set | Cases |
| --- | --- |
| Old graders (items 1 to 18), `test_grade_*.py` | 116 |
| New graders (items 19 to 38), `test_gradex_store.py` | 78 |
| New graders, `test_gradex_api.py` | 62 |
| New graders, `test_gradex_cli.py` | 35 |
| New total | 175 |
| All | 291 |

- Reference (`grade.sh xl/reference xl/grade-reference.json`): 291 of 291 pass.
  The reference's own suite has 156 tests.
- `B/solo-sol` (`grade.sh B/solo-sol xl/grade-solo-sol-old.json`): 114 of 291 pass. All 175 new
  cases fail or error. Of the old 116, 114 pass; the two that fail are the two docs graders that
  were extended (`test_errors_table_lists_every_code_with_its_status`,
  `test_readme_documents_the_new_surface`). The suite-size grader still passes there because
  solo-sol has 148 tests of its own, above the new threshold of 90.

## Changes to the copied graders

`conftest.py`, `grade.sh`, `test_grade_store.py` and `test_grade_api.py` are byte-identical copies.
`test_grade_cli.py` changed in three places only:

1. `CODES` gained the eleven codes of item 38 with their statuses: 400 for `invalid_kind`,
   `invalid_cursor`, `invalid_limit`, `invalid_delta`, `invalid_reason`, `invalid_lines`,
   `invalid_idempotency_key`, `unknown_parameter`, `duplicate_parameter`; 409 for `stock_reserved`
   and `idempotency_conflict`.
2. The README grader also looks for `events`, `adjust`, `sweep`, `reserve_many`, `/events`,
   `/adjustments`, `/sweep`, `/bulk-reservations`, `reserve-many`, `--key`, `Idempotency-Key`, and
   for `idempotency` in any letter case.
3. The suite-size threshold is 90 passed (was 40).

## Lint

The installed `ruff` is 0.16.9. Its default rule set is wider than the old E and F rules (it
includes import sorting, bugbear and others). The three new files pass `ruff check --isolated`.
The old files, kept unchanged as instructed, carry seven findings under this ruff: `I001` in each
of the three files, `RUF059` in `test_grade_api.py`, `PLW1510` in `test_grade_cli.py`, `B023` and
`BLE001` in `test_grade_store.py`. So `ruff check xl/graders` as a whole reports those seven. None
is a defect; clearing them means editing files the brief said to leave alone.

## Readings the graders rely on (tested)

Each of these is a place where a second reading exists. The graders take the first one named.

- **Item 20, "and for nothing else".** Opening an old items-only database records no event
  (reading A) versus backfilling one `item_added` per existing item (reading B). Graders assume A.
- **Item 21, "at most `limit` of them".** The lowest ids above `after_id` come back, so paging
  forward with `after_id` walks the whole log (A), versus any or the most recent `limit` (B).
  Graders assume A: one paging test and the default-100 test.
- **Item 21, kind "is given".** Only `None` means not given, so `kind=""` is `invalid_kind` (A),
  versus an empty string counting as not given (B). Graders assume A at the store, and for
  `GET /events?kind=` (see item 31 below).
- **Item 24, what a thread may see.** Under threads a `+1` adjustment is never refused, and
  exactly as many `-1` adjustments succeed as there is stock, the rest getting
  `insufficient_stock` (A), versus some other refusal under contention (B). Graders assume A, as
  the existing item 9 grader does for reservations. The mixed race with reservations also assumes
  the ten units are all taken while callers are being refused.
- **Item 27, "for each line in order its sku and its quantity".** Line 1's sku, line 1's quantity,
  line 2's sku, line 2's quantity (A), versus every sku first and then every quantity (B).
  Graders assume A: `[["A-1", 0], ["nope", 1]]` is `invalid_quantity`.
- **Item 27, "not a list".** A tuple of pairs as `lines` is `invalid_lines` (A), versus accepted
  like a list because a line itself may be a tuple (B). Graders assume A, in a case of its own
  (`test_reserve_many_invalid_lines[a_tuple_of_pairs]`).
- **Item 29, "returns the remembered value".** A replay returns the dict the first call returned,
  so a reservation since released or expired still comes back with state "active" (A), versus the
  reservation as it is now (B). Graders assume A, in the store and over HTTP, where item 34 says
  "the same body as the first one". For adjust: the first availability object, not the current.
- **Items 8 and 35, refused call at a time when a reservation has just lapsed.** The refusal
  writes nothing, not even the "expired" state (A), versus persisting the expiry and then refusing
  (B). Graders assume A in one store case only
  (`test_a_refused_commit_of_an_expired_reservation_changes_nothing`). Every other no-change check
  is taken at a time when nothing has newly lapsed, so it holds under both readings.
- **Item 31, a parameter with an empty value** (`?kind=`, `?after=`, `?limit=`). The key is given
  and its empty value is invalid (A), versus an empty value counting as absent (B, what
  `urllib.parse.parse_qs` does by default). Graders assume A, in three separate cases
  (`test_events_route_refuses_an_empty_value`). These three are the least certain graders in the
  set; drop them if B is acceptable.
- **Items 11 and 33, "the body is then not read".** The new graders send a `Content-Length` over
  the limit and no body at all, and expect the 413 at once (A). A server that waits for the body
  before answering (B) times out. The old grader sends the whole body.
- **Item 33, `ttl_seconds` with a fraction on `/bulk-reservations`.** Item 33 names only `delta`
  and a line quantity; the graders take "the rules of items 11 to 13 apply" to cover it and expect
  `invalid_ttl` (one case).
- **Item 37, numeric values in range of the syntax.** `adjust A-1 0 restock`, `events --limit 0`,
  `events --limit 501`, `reserve-many ann A-1=0`, `--ttl 0`, a repeated sku and 21 lines are valid
  on the command line and refused by the store with status 1 (A), versus rejected by the command
  line as `usage` (B). Graders assume A, as the existing graders do for `reserve A-1 0 ann` and
  `--ttl 0`.

## Left open by the goal (not tested)

- **Item 20, `at` of an "adjusted" event.** The goal gives `at` for every kind but this one: the
  call's `now` or the clock. Graders check only that it is a float.
- **Items 20 and 26, `at` of the events of `reserve_many`.** Item 20 defines it for "a successful
  reserve". Graders check kind, sku, quantity, reservation id, holder and order, not `at`.
- **Item 37, REASON, KIND and KEY.** The command line may reject an unknown reason, kind or a
  malformed key itself (status 2, `usage`), or pass it on and let the store refuse (status 1 with
  the store's code). Graders accept either, and check the single JSON line and that nothing
  changed.
- **Item 21, `after_id=None` and `limit=None`.** Not an int by the letter; an implementation may
  treat `None` as the default. Not tested. A non-str `sku` is not tested either.
- **Item 25, going back in time.** Whether a reservation that a read at a later time showed as
  expired counts as "expired at now" for a sweep at an earlier `now`. Graders never go back in
  time, except for the stated rule that a swept reservation reads "expired" at any `now` (checked
  through `get_reservation` only, not through `availability`).
- **Items 2 and 25, reservation ids.** Nothing says they increase. Graders only sort them.
- **Item 19, "never reused".** Graders check ids are positive and strictly increasing.
- **Item 27, odd lines.** A non-str or unhashable sku inside a line, and combinations of two
  `invalid_lines` faults, are not tested.
- **Item 28, an empty key from outside the store.** `Idempotency-Key:` with an empty value and
  `--key ""`: invalid by the store's rule, or not given. Not tested. `idempotency_key=""` at the
  store is tested (`invalid_idempotency_key`).
- **Item 29, argument comparison across types.** `2` against `2.0`, `True` against `1`, a non-str
  holder on a replay. Not tested.
- **Item 31, order of the query checks.** No order is stated, so every refusal case carries one
  fault. Also untested: `?sku=`, percent-encoded values, leading zeros (`limit=007`), an empty
  query (`/events?`), query strings on other routes.
- **Items 32 and 33, order between body errors and path errors.** For example invalid JSON sent
  to an unknown sku, or an unknown field together with a missing one. Every case carries one
  fault. `POST /sweep` with an empty body (`Content-Length: 0`) is not tested: `invalid_json` by
  the letter, `{}` by the seed's habit for `POST /items`.
- **Item 34, key against body errors.** An invalid key is tested only with a well-formed body
  (and an unknown sku, which only the store can refuse). A replay that omits `ttl_seconds` the
  second time is not tested.
- **Item 36, default `--ttl` of `reserve-many`.** Not stated (the reference uses 900). Graders
  check only that the command works without `--ttl`. Options placed between positionals are not
  tested.
- **Item 37, signs and separators.** `+3` as DELTA, `-2` as QTY, a SKU that contains `=`, and
  non-ASCII digits are not tested.
- **Item 38.** The README grader checks that the words are present, nothing more.

## Checks beyond the two runs

Scripts and the log are in `xl/checks/`.

- `mutate.py`: 25 deliberately broken copies of the reference, each run against the new graders
  (`mutants.log`). Examples: a sweep that skips a reservation a read already showed as expired, a
  replay that returns the current state, `stock_reserved` checked before `insufficient_stock`, a
  key pattern that lets a trailing newline through, the header ignored, adjust without a write
  lock. 24 were caught on the first run. The one that got through parsed DELTA with `int()`,
  which accepts `1_0`; three usage cases with `1_0` were added (DELTA, `--limit`, QTY) and it is
  caught now.
- `variants.py`: 6 legitimate variants of the reference, each run against every grader. The
  variants: the clock for the two untested `at` values; the command line rejecting reason, kind
  and key itself; expiry computed on read and stored only by a sweep; another order of query
  checks; reservation ids that go down; write-ahead logging with `+3` accepted as DELTA. Every
  grader passes on all six (two variants break the reference's own inherited tests, which is why
  the suite-size grader fails there; no grader of behaviour does).
- The four thread graders were run 15 times in a row on the reference with no failure.

The new set is 175 cases, above the 120 to 160 the brief asked for.
