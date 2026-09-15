# Run incomplete

Project: /work

Edits: enabled

Run files: /work/.quadratus/runs/20260915T193400Z-d9df9b6c

Error: PartialWorkStopped: Task exceeded its declared scope; work preserved. Scope check:
  223 changed lines against a stated bound of ~100. This is the shape of a task expanding into the whole feature; say what grew and why.
  Changed: app.py, tests/test_import_preview.py

## In-flight work when the run stopped

These changes were already on disk when the call stopped and have been preserved. Re-sending the same prompt would apply a second pass on top of them, not repeat the first.

Already written and preserved (2 file(s), 223 line(s)):

- app.py
- tests/test_import_preview.py

Source changes are saved in the project folder.

No integration check ran; this result has not been test-verified.

# Usage report (API-price counterfactual)

- claude:fable: 1 calls, 87,027 in / 2,936 out tokens, $1.0171
- grok:default: 1 calls, 367,432 in / 11,945 out tokens, $0.8065

**Total: $1.8236**
_Prices are the seed sheet in usage.py; verify before deciding._

# Delegation and invocation record

## seat
- run/orchestrator | [seat] | claude:fable -> claude-fable-5-1 | invoked | ok | 51.3s | 89,963 tokens
- t1/lead | [seat] | grok:default | selected, never invoked | selected | duration unknown | usage unknown
- t1/lead | [seat] | grok:default | invoked | PartialWorkStopped | 284.4s | 379,377 tokens | failed after return | provider: ok | Task exceeded its declared scope; work preserved. Scope check:
  223 changed lines against a stated bound of ~100. This 

## Totals
- Quadratus-dispatched: 469,340 tokens
- Subscription usage. Not an API charge; see usage.py for the separate API-price counterfactual.


## Task ledger

## Goal (verbatim, unchanged)

# Read-only CSV clip-manifest preview

Add a CSV clip-manifest preview to GameTape so a coach can validate exported
clip descriptions before importing them elsewhere. This feature must never
save clips, change project data, or read/process video files. Use the following
application interface contract and preserve existing behavior. Include focused
automated checks. Do not add dependencies or implement a saving/import action.

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

Columns not listed in the table are ignored.
Blank lines (no fields, or all fields empty) are ignored and are not counted
in `summary.total`; line numbers still count physical lines.

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

## Application design constraints

Stack: Flask with one Jinja template, vanilla CSS and JS. No framework, no build
step. Screens are `div.screen` elements toggled by an `.active` class rather than
routed.

### Tokens (from static/css/style.css)
- Background `#0f1117`, foreground `#e0e0e0`, headings `#fff`, secondary `#888`
- System font stack, 0.9rem on controls, 2.4rem on the project-list title with
  2px letter-spacing
- Dark only. There is no light theme.

### Layout
- `.screen` with `.screen.active` is the only navigation primitive
- Project list is centered, `max-width: 700px`, 60px top padding
- `.project-controls` is a 10px-gap flex row

### Rules
- Tagging is a keyboard task. Every action a user repeats during a match needs a
  key binding, and the binding is visible on screen.
- The video is the primary surface. Chrome shrinks, video does not.
- Timeline and clip state survive a reload. Losing tags mid-match is the worst
  possible failure.
- Every screen ships loading, empty, and error states.
- No new styling dependencies.

## Standing rules (always in force)

- Project source is available in the working directory. Use actual files as evidence. Do not commit or publish changes.

## Completed work

_Nothing completed yet._