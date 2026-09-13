# GameTape trial — 2026-09-13

The saved coaching review filter feature is implemented and independently
verified in a separate GameTape worktree. Quadratus generated the backend,
interface and ten new backend tests. Codex corrected one browser race during
acceptance and completed the handoff. The automated Quadratus run did not
reach a successful terminal result.

## Delivered result

- Source: `/Users/davisfox/Documents/GitHub/sports-video-tagger-quadratus-trial`
- Branch: `quadratus/saved-review-filters`; commit: `f93d8b1`.
- Baseline: `49dd28b`; original GameTape checkout remains on its original branch.
- Save, restore, rename and delete named tag/player/search combinations per project.
- Persistent JSON storage, legacy-project compatibility, validation and explicit stale references.
- Restored filters drive the existing export functions.
- Final verification: 79 pytest tests pass (69 baseline); JavaScript syntax and diff checks pass.
- Chromium: save/reload/restore, matching CSV/JSON clip IDs, rename/delete, keyboard,
  empty/error/stale states, and delayed load/save responses across project switches pass.
- Synthetic demo: `http://127.0.0.1:5056`; its data lives outside the trial source tree.
- Local screenshots and reproducible browser checks: `output/gametape-trial/`
  and `output/playwright/gametape-saved-filter.png`.

## What the trial exposed

1. **Senior Grok edits cancelled. Fixed in `022de34`.** Two runs read source
   but stopped at the first edit. Vendor traces and tiny reproductions proved
   `--permission-mode acceptEdits` conflicts with `--always-approve`. Removing
   the conflicting extra flag permits the existing authorized edit. The same
   provider then saved a real file. All 572 Quadratus tests and Ruff passed.
   Model roles, bounded tools and per-call project/snapshot selection remain intact.
2. **Existing target tests wrote into the project. Fixed in the feature.**
   GameTape's fixture did not redirect RECORDINGS_DIR. Tests passed but created
   recordings, invalidating the integration fingerprint. The first model repair
   blamed concurrent source edits. Independent before/after filenames identified
   the real cause; adding recording isolation yields passing tests with zero
   project file changes.
3. **Task metadata can silently default. Still open.** Fable prefaced a
   `KIND: test rote` instruction with explanatory prose. The parser treated it
   as general/simple, so it did not follow the intended test classification.
4. **Task scope and size are not reliably bounded. Still open.** A worker given
   the two-line fixture task went on to implement the remaining interface and
   tests from the overall goal. The first backend task alone added 131 lines;
   the later invocation made hundreds of lines of changes.
5. **Timeout recovery can replay work already saved. Still open.** The expanded
   invocation hit 900 seconds and the provider started retrying the same prompt.
   Codex stopped the replay, retained the source and independently verified it.
   The run record correctly says incomplete; it is not a successful autonomous run.
6. **Failure diagnostics and metering need improvement. Still open.** Gate
   failures omit changed paths; cancelled/timed-out invocations are absent from
   the usage ledger even when they consumed a model window. Per-call progress
   is sparse, making long inspection and close-out periods difficult to distinguish.

## Next application work

Enforce task scope and validate task metadata before dispatch. Preserve and
inspect partial edits before retrying a timed-out editing call. Show changed
paths for gate failures, and record failed-call usage when a vendor reports it.
Then rerun a similarly bounded feature to test unattended completion.

Run records remain under the trial's `.quadratus/runs/`. Original failed
reports, the vendor permission trace, reproduction results, input goals,
acceptance scripts and the external demo launcher remain in
`output/gametape-trial/` in this Quadratus checkout. They contain synthetic data;
no production application data or credentials were copied into the trial.
