# Operator-v2 readiness evidence index

**TL;DR**

- This answers the four path requests in Grok's lane-9 audit (`b3443ba`,
  `docs/review/grok-operator-readiness.md`, delivered in PR 25 comment
  5862360172). It says where the evidence is. It does not certify readiness.
- All four items exist, but only on Davis's Mac. None of them are in git.
- The approved wrapper file is present and its text matches the corrected
  version Claude approved. Its hash `e5885c9f…` comes from Codex's own receipt.
  This session could not recompute it.
- **The real UID-501 browser control did not run the approved bytes.** It ran
  an earlier draft that has no exit-0 diagnostic path. The success path is the
  same text in both, but the receipt is not bound to `e5885c9f`.
- The Run19 source tree (38 files) and the excluded shim are both preserved,
  with per-file hashes recorded by the grader.

Author: fresh Opus 5.5 bounded worker, authorized by Davis. Base commit
`7aacdd6`. This file is the only change. No wrapper, browser, vendor call,
profile activation or evidence edit was made.

## Path conventions

- `work/…` means relative to the Codex workspace root
  `/Users/davisfox/Documents/Codex/2026-09-21/referenced-chatgpt-conversation-this-is-an/`.
- `/private/tmp/…` paths are local scratch trees on the same host.
- **Everything below is local-only.** A reviewer without host access cannot
  open any of it. Content hashes are given so the host can confirm them.

## 1. Approved operator-v2 wrapper (Grok row 12, request 1)

| Item | Value |
| --- | --- |
| File | `work/operator-browser-functional-check-v2.py` (220 lines) |
| Claimed SHA-256 | `e5885c9f4c33c29c601504b88154a864892f193f58be25e1f843e19415c733b5` |
| Where that hash is recorded | `work/operator-browser-v2-parity-worker-controls.json` field `source_sha256`, with `source_stable: true`; also `run19_operator_v2.sha256` in `work/claude-test-watch-state.json` |
| Controls against those bytes | 14 synthetic controls, zero subprocesses, zero vendors (`work/operator-browser-v2-parity-worker-controls.json`; summary in `work/run19-operator-v2-parity-to-claude.md`) |
| Approval | Claude, PR 25 comment 5853773429, on delta 5853551848 |
| Adopted / activated | No (`run19_operator_v2.adopted: false`) |

What was checked here, by reading only: the on-disk file has the
`failure_diagnostic` helper (lines 160-178), and `check_run` calls it for a
nonzero exit (line 192) and for a `TypeError`/`ValueError` from `validate` on
exit 0 (lines 194-198). That matches the delta described in 5853551848. It
closes Grok row 13's "claimed fixed" at the level of text. It was not executed.

**Not verified:** the SHA-256 itself. Shell access outside this worktree was
denied in this session, so the hash is the recorded value, not a recomputed
one. One command on the host settles it:
`shasum -a 256 work/operator-browser-functional-check-v2.py`.

## 2. Workflow plan (request 2)

`work/quadratus-shared-workflow-plan.md` exists at that path. This session did
not compare it with the paste in 5856716643.

## 3. UID-501 receipts (Grok row 14, request 3)

| Item | Path |
| --- | --- |
| Report | `work/run19-home-controls-report.json` |
| Preparation | `work/run19-home-controls-prepared.json` |
| Control root | `/private/tmp/quadratus-run19-home-controls.3nybkuia/` |
| No-HOME copy and script | `…/no-home/` (38 source files), `…/without-home.py` |
| Owned-HOME copy and script | `…/owned-home/` (38 source files), `…/owned-home.py` |
| Browser artifacts from the owned-HOME pass | `…/owned-home/.quadratus/operator-browser-receipts/79b8b480b4a04280bd5d590cb1a43d7f/` (`results.json` plus six PNGs) |
| Logs | `work/run19-home-no-home.log`, `work/run19-home-owned-home.log` |
| Earlier 11-control synthetic set | `work/operator-browser-v2-worker-controls.json` |

What the report records: uid 501, gid 20, `no_dependency_shim: true`, zero
vendors, network none, source unchanged. No HOME → exit 1 before the browser
with `uv_os_homedir` ENOENT. Owned HOME → exit 0, nine counted scenarios. It
states that the original Run19 failure is not proven, because the original
stderr was lost. That agrees with Grok's "Run19 cause is not established".

**Finding: these receipts are not bound to the approved hash.**

- `owned-home.py` is 213 lines. It has no `failure_diagnostic` helper, and
  `validate` is called without a `try/except` (lines 191-194). That is the
  pre-correction draft, the same text as the full paste in
  `work/run19-operator-v2-to-claude.md` (PR 25 5853493201).
- The approved file (section 1) came later.
- `run19-home-controls-report.json` records no wrapper hash at all.
- Codex's position (in `work/run19-operator-v2-validation-to-claude.md` and
  `…-parity-to-claude.md`) is that the real 501 control "remains valid" because
  the success path did not change. By reading, the success path does match:
  environment, HOME/TMPDIR ownership, `run_bounded`, and the TAP output are the
  same text. The difference is only the added failure diagnostic. That is an
  argument, and it is not a receipt. Whether it is enough to count for phase 4
  is Root's call. The alternative is one fresh no-vendor 501 control with the
  hashed bytes.

## 4. Run19 source snapshot and excluded shim (Grok row 17, request 4)

Run `20260927T060417Z-4a802779`, candidate `90cc5d9`.

| Item | Path / identity |
| --- | --- |
| Preserved run project | `/private/tmp/gametape-run19-codex.e9q8dv1t/work/` |
| Engine copy used by the run | `/private/tmp/gametape-run19-codex.e9q8dv1t/runtime/quadratus/` |
| Per-file manifest (38 files, SHA-256 each) | `/private/tmp/gametape-run19-codex-grade.s7xoh7ry/prepared.json` → `source_hashes`, with `source_root` naming the run project above |
| Graded copy (38 files, shim omitted) | `/private/tmp/gametape-run19-codex-grade.s7xoh7ry/solver/` |
| Changed from baseline | `app.py`, `static/favicon.svg`, `templates/index.html`, `tests/test_basic.py` |
| Grade summary | `work/run19-grade-report.json` (`excluded_dependency_shim_copied: false`, `source_changed_during_grade: []`) |
| Run report / stop | `work/run19-report.md`, `work/run19-stop-to-claude.md` |
| In-run gate receipt | `…/e9q8dv1t/work/.quadratus/operator-corrected-gate-report.json` (command `python -P /opt/quadratus/operator_browser_check.py`, `source_hash b20f3aee…`, `runner_hash 1c024038…`, 9 tests) |
| Source excludes in effect | `…/e9q8dv1t/work/.quadratus/source-excludes.json` = `[".quadratus"]` |

Excluded shim:

| Field | Value (from `work/run19-worker-excluded-dependency-review.json`) |
| --- | --- |
| Path | `/private/tmp/gametape-run19-codex.e9q8dv1t/work/node_modules/playwright/index.js` (presence confirmed here) |
| Size / mode / uid | 383 bytes, `0o600`, uid 501 |
| mtime | `2026-09-27T06:24:41.606373Z` |
| SHA-256 | `a3f962a791ab09db7a311b2c0163abed8ba6ef18a97d052a3f6755566632c2c0` |
| In frozen source manifest | No |
| Recorded behaviour | Sets `HOME=/tmp/solver-home` when absent, then requires the global playwright |

This session did not open or hash the shim or the 38 files. The values above
are what the preserved reviews recorded.

Two notes for Root:

- The in-run gate ran `/opt/quadratus/operator_browser_check.py`. That is the
  original Run19 operator check, not v2. Its `runner_hash` does not cover the
  shim; `run19-grade-report.json` already says so.
- The grade manifest hashes the tree as the grader copied it. It does not say
  the run tree is still byte-identical today. Re-hashing against
  `prepared.json` on the host would show that.

## What stays open

This index moves Grok rows 12, 14 and 17 from "path unknown" to "located". It
does not clear them:

- Row 12: hash recorded, not recomputed here.
- Row 14: receipts exist but ran pre-approval bytes (section 3).
- Row 17: manifest and shim located; current byte-identity not re-checked.

Rows 3-7, 16 and 24 are unaffected. Phase 4 is still not ready on this record.

## Not accessible in this session

- PR 25 comments: `gh` was denied, so no claim, progress or handoff comment was
  posted from here. The comment IDs above come from the local state file and
  Grok's audit text, not from reading GitHub.
- Out-of-tree shell (hashing, `stat`): denied. Only file reads were available.
- Private model reasoning: not accessed.
