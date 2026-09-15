# Examiner bundle: CSV clip-manifest import preview (DRAFT, not frozen)

Held-out checks for the blind acceptance task in `../BLIND_ACCEPTANCE.md`.
Written by Claude before any solver output exists, against the interface
contract below. The solver never sees this directory. Codex freezes the brief;
if the frozen brief changes the contract, these checks change with it and the
change is recorded in `CLAUDE_LOG.md` before launch, never after.

## Why a contract is part of the brief

Held-out checks cannot be written blind against an unspecified interface. The
brief therefore pins the endpoint, the CSV columns, the response shape and the
UI hooks. That is a legitimate application requirement, the same way a client
would specify an import format, not a hint about scoring.

## Contract to include verbatim in the solver brief

Endpoint: `POST /api/projects/<project_id>/clips/import_preview`, multipart
form with one field `file` holding a CSV. It never writes the project store.

CSV: header row required; column names matched case-insensitively after
trimming; UTF-8 with optional BOM; LF or CRLF; RFC 4180 quoting. Columns:

| Column | Required | Rule |
| --- | --- | --- |
| `Tag Type` | yes | must name an existing project tag type (exact, after trim) |
| `Start (s)` | yes | finite number, `>= 0` |
| `End (s)` | yes | finite number, `> Start (s)` |
| `Label` | no | free text, displayed escaped |
| `Notes` | no | free text |
| `Players` | no | `;`-separated names or `#number`; each must match one project player |
| `Clip ID` | no | if present, must not equal an existing clip id or an earlier row's id |
| `Duration (s)` | no | ignored (the app's own export writes it) |

Row statuses: `valid`, `malformed` (any rule above fails, wrong column count,
undecodable cell), `duplicate` (same `Tag Type`, `Start (s)`, `End (s)` and
`Label` as an earlier row, or a `Clip ID` already used). A row gets one status;
`malformed` wins over `duplicate`. Row numbering counts the header as line 1.

Response `200`:

```json
{"preview_only": true,
 "summary": {"total": 3, "valid": 1, "malformed": 1, "duplicate": 1},
 "rows": [
   {"line": 2, "status": "valid", "reasons": [],
    "clip": {"tag_type": "Pass", "start": 1.5, "end": 4.0, "label": "x",
             "notes": "", "players": ["<player id>"]}},
   {"line": 3, "status": "malformed", "reasons": ["end must be greater than start"], "clip": null},
   {"line": 4, "status": "duplicate", "reasons": ["same as line 2"], "clip": null}]}
```

Errors: `404 {"error": "Project not found"}`; `400 {"error": "...", "code":
"bad_request"}` for a missing `file`, an undecodable file, or a missing
required header; `400 {"code": "too_many_rows"}` above 5000 data rows.

UI (tagging screen): a button `#btn-import-preview` with accessible name
"Import preview", a file input `#import-preview-file`, a results region with a
table `#import-preview-table` (one `tr[data-status]` per row, escaped text), a
`#import-preview-summary` element with the counts, and a `role="status"`
element for loading/empty/error messages. Everything reachable and operable by
keyboard; Escape dismisses the results if they are shown in a dialog.

Docs: `docs/CSV_IMPORT.md` stating the column table, BOM/CRLF handling and
quoting, citing RFC 4180.

## Running the checks

Against the solver's output tree (never the examiner's own copy):

```
GAMETAPE_ROOT=/path/to/solver/output python -m pytest -q examiner/test_manifest_preview.py
GAMETAPE_ROOT=/path/to/solver/output node examiner/run_browser.js
```

The pytest file needs the solver tree's own `requirements.txt` installed; the
browser check needs Playwright and reuses the solver tree's
`tests/browser/server.py` reset route. A missing endpoint or element is a
failing check, not a skip.

## What these checks do not measure

Routing quality, seat coverage and spend are read from the run's ledger, not
from here. A passing examiner proves application correctness only.
