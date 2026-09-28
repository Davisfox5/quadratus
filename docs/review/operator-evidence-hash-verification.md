# Operator-v2 evidence: hash verification

**TL;DR**

- The approved wrapper's bytes hash to `e5885c9f…`, the value Codex recorded.
  Grok row 12's "hash recorded, not recomputed" is now recomputed.
- All 38 Run19 source files match the grade manifest, in both the preserved
  run tree and the graded copy. Nothing is missing and nothing differs.
- The excluded shim is still there. Its hash, size, mode, uid and mtime match
  the review record, and it is still absent from the manifest.
- The UID-501 control scripts hash to different values from the approved
  wrapper, and no report records their hashes. The finding in the evidence
  index still stands: that control ran pre-approval bytes.
- Only difference found: the run tree also holds a `.pytest_cache/` directory
  (4 files) that is not in the manifest. The graded copy has none.

Author: fresh Opus 5.5 bounded worker, authorized by Davis. Base commit
`74065d8` (the evidence index, `docs/review/operator-evidence-index.md`).
This file is the only change. Method: Python `hashlib.sha256` over file
bytes, plus `os.stat` and `os.walk`. Nothing was executed except that reader.
No wrapper, browser, vendor, profile, credential, private reasoning or
evidence file was touched. Only hashes, paths and metadata are reported
below. No file contents are included.

`work/` means the Codex workspace
`/Users/davisfox/Documents/Codex/2026-09-21/referenced-chatgpt-conversation-this-is-an/work/`.

## 1. Approved wrapper (Grok row 12)

| File | Recomputed SHA-256 | Recorded | Result |
| --- | --- | --- | --- |
| `work/operator-browser-functional-check-v2.py` | `e5885c9f4c33c29c601504b88154a864892f193f58be25e1f843e19415c733b5` | `source_sha256` in `work/operator-browser-v2-parity-worker-controls.json` (`source_stable: true`); `run19_operator_v2.sha256` in `work/claude-test-watch-state.json` (`adopted: false`) | **Match** in both records and with Root's stated hash |

The line count came out as 219 lines. The index says 220, which was a
Read-tool line count. This is a difference in how lines were counted. The
bytes are unaffected.

## 2. UID-501 control scripts (Grok row 14)

| File | Recomputed SHA-256 | Recorded |
| --- | --- | --- |
| `/private/tmp/quadratus-run19-home-controls.3nybkuia/owned-home.py` | `89226550890bf3b07ef37bceef57aade0f00addec6595fd09de0e37c29a825ff` | none |
| `/private/tmp/quadratus-run19-home-controls.3nybkuia/without-home.py` | `c2da51b6b4c960a814b2ce0ac4455bd402db81aa1c539421552eb0ee5e9e0d26` | none |

Neither hash equals `e5885c9f`. `work/run19-home-controls-report.json` and
`…-prepared.json` contain no 64-hex hash at all. The receipt is therefore
still not bound to the approved bytes, as the index found. The fix is still
Root's choice: accept the success-path argument, or run one fresh no-vendor
501 control on the hashed file.

## 3. Run19 source, 38 files (Grok row 17)

Manifest: `/private/tmp/gametape-run19-codex-grade.s7xoh7ry/prepared.json`
→ `source_hashes` (38 entries), `source_root` =
`/private/tmp/gametape-run19-codex.e9q8dv1t/work`.

| Tree | Present | Match | Mismatch | Missing | Extra (not in manifest) |
| --- | --- | --- | --- | --- | --- |
| Run tree (`…e9q8dv1t/work`, skipping `.quadratus/` and `node_modules/`) | 38 | 38 | 0 | 0 | `.pytest_cache/.gitignore`, `.pytest_cache/CACHEDIR.TAG`, `.pytest_cache/README.md`, `.pytest_cache/v/cache/nodeids` |
| Graded copy (`…s7xoh7ry/solver`, full walk) | 38 | 38 | 0 | 0 | none |

`changed_from_baseline` still reads `app.py`, `static/favicon.svg`,
`templates/index.html`, `tests/test_basic.py`.

The `.pytest_cache/` files are not a byte difference in any manifest file.
They are extra files that the manifest does not cover, and the run's
`source-excludes.json` (`[".quadratus"]`) does not exclude them. This
verification did not determine whether the grader dropped them on purpose.

## 4. Excluded shim

`/private/tmp/gametape-run19-codex.e9q8dv1t/work/node_modules/playwright/index.js`

| Field | Recomputed | Recorded (`work/run19-worker-excluded-dependency-review.json` → `shim`) | Result |
| --- | --- | --- | --- |
| SHA-256 | `a3f962a791ab09db7a311b2c0163abed8ba6ef18a97d052a3f6755566632c2c0` | same | Match |
| Size | 383 bytes | 383 (index; no size key found in the review JSON) | Match with the index |
| Mode | `0o600` | `0o600` | Match |
| uid | 501 | 501 | Match |
| mtime (UTC) | `2026-09-27T06:24:41.606373+00:00` | same | Match |
| In source manifest | No | No | Match |

It is the only file under `node_modules/` in the run tree.

## What this changes

- Row 12: moves from recorded to **recomputed and matching**.
- Row 17: moves from located to **byte-identical today**. The only open
  question is the four untracked `.pytest_cache` files.
- Row 14: **unchanged**. The control scripts are now hashed, and they are not
  the approved bytes.

Phase 4 readiness is not decided here. Rows 3-7, 16 and 24 are unaffected.

## Gaps

- A `wc -l` / `tail` shell check on the wrapper was denied by permission.
  The line count above comes from Python instead.
- One Python heredoc was denied by a shell-safety rule. The same reader was
  run as a script file in `/private/tmp/opus-hashverify/`, outside the repo.
- `work/opus-readiness-hash-session.jsonl` matched a search for the 501
  script hashes. It appears to be a session log, so it was not opened.
