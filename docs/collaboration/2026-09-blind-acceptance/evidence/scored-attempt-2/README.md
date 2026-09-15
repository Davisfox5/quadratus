# Scored attempt 2 — preserved scope stop

Run **d9df9b6c**, frozen engine **ad82a4c**, GameTape **a8772ab**. The 30 input
files and task brief match attempt 1 byte for byte. The private examiner archive
hash is unchanged; Codex did not open its contents and it was not mounted.
See [the pre-launch freeze](../scored-attempt-2-freeze.json).

## Result

**Incomplete**, stopped after **335.78 seconds**, **two provider attempts** and
**469,340 reported tokens**. The token threshold was not crossed. The first
lead's scope assessment stopped the task: **223 changed lines against a declared
100-line bound** (above the existing 1.5x tolerance). Changes stayed within the
two permitted paths. The container and temporary credential seed were removed;
there was no retry, continuation, closeout call or caller intervention.

| Task | Seat | Reported tokens | Seconds |
| --- | --- | ---: | ---: |
| Decomposition | Fable | 89,963, including 2,815 Haiku auxiliary tokens | 51.31 |
| First row-validator implementation | Grok | 379,377 | 284.38 |

Total input 454,459; output 14,881. Cached reads 305,484 and Claude cache creation
60,660 are already included in input. Fable's own row is 87,148; the Haiku row
is 2,801 input + 14 output = 2,815, counted once. Auxiliary activity is not a
controlled worker. Vendor telemetry reports five Claude turns and nine Grok
model calls; these are different vendor measures, not fourteen harness calls.
Subscription-only auth was used; vendor costUSD fields are not evidence of billing.

No controlled workers, OpenAI seats or Opus review were observed. Fable classified
the first task backend/simple and seated Grok; simple tasks have no collaborators
under the existing policy. The stop happened before further task selection.
Thus this run still does not establish full-worker coverage. It did exercise
the repaired scope-stop ledger: Grok has `outcome: PartialWorkStopped`,
`provider_outcome: ok`, `post_return_failure: true`, with its usage retained.
The expensive-closeout repair was never reached in this attempt.

## Preserved application state

The source archive contains 31 files. Only app.py (85 added lines) and
tests/test_import_preview.py (138 added lines) changed. A pure per-row validator
and unit tests exist; the HTTP endpoint, CSV orchestration/duplicate detection,
UI and documentation remain absent. Zero tasks were closed out. The controller
recorded no integration checks before its scope stop.

Codex independently reconstructed the saved archive and verified all source
hashes, then ran pytest in a fresh network-disabled, credential-free container:
**111 passed in 2.07s**, source hashes unchanged. This proves the saved regression
suite passes, not that the requested application feature is complete. Private
application scoring is assigned to Claude against the unchanged original cases.

## Findings to review

1. **Scope estimation/decomposition remains a blocker.** The 100-line allowance
   covered a validator plus eleven tests; actual changes were 223 lines. The
   implementation stayed within the requested row-validator slice. The generic
   stop wording about growing into the whole feature is not proof of that kind
   of semantic expansion. Do not bypass the limit or expand it just to pass.
2. **The generated task contradicts its own acceptance signature.** Its body
   specifies `_validate_clip_row(project, cols, width, row)`, while the scope
   acceptance still says `(project, cols, row)`. These are published in result.json.
3. **Successful ledger rows omit usage provenance.** The parser correctly
   counted 2,815 auxiliary tokens but the Fable invocation's diagnostics are `{}`.
   Runtime retained diagnostics only for failures. Codex reproduced and patched
   this offline after the run; frozen evidence is unchanged. The regression uses
   the observed envelope values and checks both persisted metadata and unchanged
   89,963 total. This is metadata, not another auxiliary charge.

## Reproduce and review

Verify artifact-sha256.json. Extract source-after.tar.gz into a fresh directory
(source/ prefix) and verify source-after-sha256.json. Run `python -m pytest -q`
with the existing app dependencies. For Claude's private scoring use the original
private archive committed by SHA-256
`71cb8b06a34ed94d6936bd9dfc45ed3889a0f6c5a80be1424850f1140e66371a`;
set GAMETAPE_ROOT to that extracted source. Do not modify the app or examiner.

Review result.json/in-flight scope, changes.diff, invocation outcomes, normalized
usage and vendor usage extracts. Raw stdout/session data remains private; vendor
thought fields are omitted from publication. Existing Sol parent/child overlap
assumptions were not exercised because no Sol call occurred. Grok native absence
is limited to available telemetry, not a universal no-hidden-activity claim.

Claude owns independent scoring and review of these findings plus the small
metadata patch. Codex owns disposition. No next live run is queued.
