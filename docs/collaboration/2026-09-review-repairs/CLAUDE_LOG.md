# Claude work log

Read `README.md` first. Codex writes `CODEX_LOG.md`; this file is Claude's only.

## 2026-09-15 — acknowledgment and claims

- Read `README.md`, `CODEX_LOG.md`, both `CLAUDE.md` files and the review context.
  Ownership map accepted as written. Reservations respected: I will not edit
  `quadratus/scope.py`, `quadratus/session.py`, `quadratus/runtime.py`,
  `quadratus/delegation.py`, or GameTape `app.py` in this batch.
- Environment note: I work from a cloud worktree, not the Mac paths in the
  README. Branch `codex/claude-review-repairs` in both repos, fetched from the
  remote; I pull before every push and never force.
- Claimed now (Claude, active):
  - GameTape: `static/js/app.js`, `static/css/style.css`,
    `templates/index.html`, `tests/ui/*`, new `tests/browser/*`,
    `docs/BULK_EDIT.md`. Work: intentional focus after preview/apply/refresh,
    keyboard-reachable preview scroller, cross-project status reset, the
    three surviving mutants turned into tests, a portable browser runner with
    self-established fixtures.
  - Quadratus: `quadratus/task_kinds.py`, `quadratus/cli_providers.py`, new
    test files `tests/test_capability_matching.py` and
    `tests/test_grok_diagnostics.py`. Work: seat capability model
    (execute / patch / direct-write) and need-aware routing; bounded Grok
    diagnostic capture on any non-`end_turn` stop.
- Session integration proposal for Codex (I will not touch `session.py`):
  1. `TaskSpec` gains `needs: frozenset[str]` populated from
     `task_kinds.needs_from_text(description, acceptance)` plus any explicit
     `NEEDS:` label the orchestrator emits; `_pick_lead` passes
     `needs=spec.needs` to `task_kinds.route`. Routing then skips a rung whose
     seat cannot satisfy the need and escalates upward, same rule as
     unavailability. A restricted seat still satisfies `patch`.
  2. Lead `ProviderError` recovery, bounded: only when the failed call's
     source fingerprint equals the pre-call fingerprint (no partial writes)
     and the failure is not `RepeatedFailure`, re-route once via
     `task_kinds.escalate_from(failed_key, needs=..., available=...)`.
     Anything else keeps today's stop-and-preserve behavior.
  3. `runtime.Fleet._generate` copies `provider.last_diagnostics` (new, from
     `cli_providers`) onto the failure event so the ledger keeps stop reason,
     model call count and attempted tool names without publishing transcripts.
- Checks run at this checkpoint: none yet beyond the review's baselines
  (Quadratus 638 passed / 3 skipped without playwright and gradio; GameTape
  100 pytest, 8 node tests, 4 mutants, 8 browser scenarios on Chromium).

## 2026-09-15 — GameTape lane delivered (GameTape `d829b87`)

- Changed: `static/js/app.js`, `static/css/style.css`, `templates/index.html`,
  `tests/ui/bulk_serialization.test.js`, `tests/ui/bulk_coverage.test.js` (new),
  `tests/ui/mutation_check.js`, `tests/browser/{server.py,run.js,scenarios/*}`
  (new), `docs/BULK_EDIT.md`, `.gitignore`. `app.py` untouched.
- Focus: `keepBulkFocusInside()` now records where it parked focus
  (`bulkAutoFocused`); `settleBulkFocus(target)` moves on from that spot only
  if focus is still there. Targets: preview ok → Confirm; preview failed/no-op
  → Preview; commit → Preview; conflict/failure → Refresh; refresh done →
  Preview. A user who moved during the request keeps their place. Enter twice
  on Preview no longer closes the dialog.
- Scroller: `#bulk-preview-wrap` has `tabindex="0"`/`role="region"` and joins
  the Tab cycle only while the table has rows.
- Cross-project: when a pending batch settles while another project's dialog
  still shows `BULK_APPLY_STATUS`, that dialog gets `N clip(s) will change`
  (or empty) back and Preview enabled. `bulkPreview` now carries `count`.
  The old serialization test asserted the stale message; updated.
- Tests: 8 new Node cases; the three mutants that survived in the review
  (`busy-ignores-project`, `stale-preview-restored`,
  `apply-ignores-preview-project`) plus `settle-focus-dropped` added to the
  mutation harness. Browser runner resets fixtures via a test-only
  `POST /__test/reset` before every scenario; each scenario sets its own
  clip state; placeholder video is reported as such and is not media
  acceptance.
- Checks run: `python -m pytest -q` 100 passed; `node --check` ok;
  `node --test tests/ui/*.test.js` 16 passed; `node tests/ui/mutation_check.js`
  baseline 15/0, all 8 mutants killed, source hash unchanged;
  `node tests/browser/run.js` 8/8 scenarios on Chromium 1194 (placeholder
  media; console: only the deliberate 409). Node 22.22.2.
- Limitation: no ffmpeg here, so media playback was not exercised; the runner
  says so on stdout when that is the case.

## 2026-09-15 — Quadratus helpers delivered (this commit)

- Changed: `quadratus/task_kinds.py`, `quadratus/cli_providers.py`,
  `tests/test_capability_matching.py` (new, 33 cases),
  `tests/test_grok_diagnostics.py` (new, 11 cases). No reserved file touched.
- Actual signatures, confirming the accepted contract:
  - `Need.EXECUTE = "execute"`, `Need.PATCH = "patch"`,
    `Need.DIRECT_WRITE = "direct-write"`; `KNOWN_NEEDS` frozenset.
  - `normalise_needs(values) -> frozenset` raises `ValueError` on any unknown
    label (never drops it). Use it on an explicit `NEEDS:` line.
  - `seat_capabilities(key) -> frozenset`: restricted roster row → `{patch}`;
    anything else, including keys the roster does not describe → all three.
  - `seat_satisfies(key, needs, capabilities=seat_capabilities) -> bool`.
  - `needs_from_text(description, acceptance=()) -> frozenset`, conservative:
    EXECUTE on a run verb + known runner, a backticked command literal, or a
    run-only outcome (exit code, tests passed); PATCH on an edit verb and a
    source path together; DIRECT_WRITE on binary/generated/lock files. The
    recorded t6 task text yields `{execute, patch}`.
  - `route(kind, *, default, difficulty=None, candidates=None, available=...,
    needs=(), capabilities=seat_capabilities)`: a seat that cannot satisfy
    every need is skipped at pin, rung, default and candidates alike.
    Unchanged behaviour when `needs` is empty (never raises). With needs
    stated and nobody fit, raises `NoCapableSeat(LookupError)` carrying
    `.needs` and `.tried`. Codex: catch it in `_pick_lead`'s caller and stop
    the task loudly; do not fall back to the rotation seat.
  - `escalate_from(key, *, needs=(), available=..., capabilities=...) ->
    Optional[str]`: strictly higher ladder seat that is available and fits,
    else None (also None for keys off the ladder).
- Diagnostics: `CLISpec.extract_diagnostics` (grok only) →
  `provider.last_diagnostics`, reset to None at the top of every `_call`
  attempt, populated by `_observe_output`. Content is exactly `stop_reason`
  (≤40 chars), `model_calls` (int) and `tools_attempted` (≤20 name-shaped
  strings, deduplicated); arguments, paths, URLs and text never pass. Any
  `ProviderError`/`ProviderRefusal` raised from `_call` carries the same dict
  as `.diagnostics`. The cancellation message now names attempted tools when
  the envelope carries them. Read it in `Fleet._generate` with
  `getattr(view, "last_diagnostics", None)`, same as `last_usage`.
  The transcript shape inside the envelope is undocumented; the walker reads
  name-shaped fields off list-valued keys and reports nothing otherwise, so
  "no `tools_attempted`" means not reported, not "no tools".
- Checks run: `ruff check` clean on the four files; focused 44 passed; full
  `python -m pytest -q` **707 passed, 3 skipped** (skips are the playwright
  and gradio modules absent here; Codex's 681 had them installed, so 681 + 26
  new = 707 matches).

## 2026-09-15 — review of Codex `3a4f7ad` (scope telemetry, verdict parsing)

Read the diff and probed both parsers against the real functions. The slice
does what its log says: oversized no longer renders as passed, a scope stop
marks the last pending event `post_return_failure` with `provider_outcome`
kept, and nested capture joins the session boundary once. Findings, none
blocking:

1. `_resolved_verdict`: `RESOLVED.`, `**RESOLVED**` and `RESOLVED - see notes`
   all read as unresolved and buy a fix cycle. Trailing punctuation is the
   common case. Suggest normalising the standalone line (strip `*`, backticks,
   trailing `.`/`!`) before the exact match; keep everything else as strict as
   it is.
2. `_has_blocking_finding`: `BLOCKING: none` counts as a blocking finding.
   Either treat `BLOCKING:` followed by none / no findings / n/a as empty, or
   have the review prompt say to write `NO FINDINGS` instead of an empty
   BLOCKING line.
3. The prompt at `session.py:1377` says "prefix ... with 'BLOCKING:'" while the
   regex anchors at line start after an optional bullet. `[BLOCKING]` and bold
   without a colon are now silently non-blocking. Consistent with the prompt,
   but the prompt should say "start the line with BLOCKING:" so a reviewer
   cannot comply and still be ignored.
4. `session.py:1038` (security excursion verdict) still uses the substring
   test. Codex's log says the excursion is unchanged on purpose; recording
   that here so it is a decision, not an oversight.
5. `capture_invocations`: with the session boundary, events persist only when
   the outer block exits, i.e. after scope assessment. A hard kill between
   provider return and assessment loses the row that Fleet alone used to
   write. Small durability trade for the correct outcome; acceptable, but
   the ledger docstring should say so. Worker threads do not inherit the
   ContextVar, so pool workers still record directly (correct).
6. `except BaseException` marks the last `ok` event with whatever escaped the
   block, including an exception thrown by `_assess_scope` itself. That is
   probably right (the call did not become accepted work) but the detail will
   name a git error rather than a scope result; fine if intended.

## Handoff to Codex

- Integration points for you, in your files: `TaskSpec.needs`;
  `needs_from_text(spec.description, spec.scope.acceptance if spec.scope else ())`
  unioned with `normalise_needs(<NEEDS: label>)`; `_pick_lead` →
  `route_kind(..., needs=spec.needs)` with `NoCapableSeat` handled as a loud
  task stop; bounded recovery via `escalate_from(failed_key, needs=spec.needs,
  available=self._available)` only after the source fingerprint check you
  described; `diagnostics=getattr(view, "last_diagnostics", None)` on the
  failure event in `Fleet._generate`.
- I have not edited `README.md`; update the ownership rows as you see fit.
- Remaining in my lanes: nothing open. Video-lock work stays deferred as agreed.
