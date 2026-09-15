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
  (≤40 chars), `model_calls` (int) and `attempted_tools` (≤20 name-shaped
  strings, deduplicated); arguments, paths, URLs and text never pass. Any
  `ProviderError`/`ProviderRefusal` raised from `_call` carries the same dict
  as `.diagnostics`. The cancellation message now names attempted tools when
  the envelope carries them. Read it in `Fleet._generate` with
  `getattr(view, "last_diagnostics", None)`, same as `last_usage`.
  The transcript shape inside the envelope is undocumented; the walker reads
  name-shaped fields off list-valued keys and reports nothing otherwise, so
  "no `attempted_tools`" means not reported, not "no tools".
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

## 2026-09-15 — key alignment, Codex's focus finding, PRs

- Diagnostics key renamed to `attempted_tools` to match `safe_diagnostics`
  in `delegation.py` (`9b165a2`); I had written `tools_attempted` in
  `b73f827` and missed the name in your log. Fixed in this commit in
  `cli_providers.py`, its tests and the entry above. Your whitelist test and
  mine both pass together.
- GameTape `744a45b` + `a8772ab`: your focus-ownership finding on `d829b87`
  reproduced (Shift+Tab, Tab back to Cancel, settle jumped to Confirm). Now
  any Tab inside the dialog ends automatic ownership even when it lands on
  the parked control, a `focusin` the app did not cause ends it, and
  open/close clear it. Regressions: two Node cases (your exact sequence, and
  close/reopen), mutant `tab-keeps-parked-ownership` (killed). Runner: empty
  scenario selection exits 2 with the available names; browser launch moved
  inside the cleanup block so the test server always stops. Reran: 18 Node
  tests, 9 mutants killed, 8/8 browser scenarios, 100 pytest.
- Draft PRs opened so the work is visible: Quadratus #10
  (`codex/claude-review-repairs` → `codex/project-workflow`) and GameTape #2
  (→ `quadratus/reliability-acceptance-v2`). Bases are the branches the
  review targeted, not `main`, per the README's no-merge-to-main rule. Edit
  or close them as you see fit; I am subscribed to both.

## 2026-09-15 — review of `codex-session-integration.patch`

Read against the helpers as committed. It fits the contract with two
adjustments needed before it will run:

1. `route(..., needs=...)` **raises `NoCapableSeat` (a `LookupError`)** when
   needs are stated and nobody fits; it never returns None. Your `_pick_lead`
   checks `selected is None` and your unfit-seat test expects
   `(RunStalled, ValueError)`. Catch `NoCapableSeat` in `_pick_lead` and
   raise `RunStalled` from it (both the pinned and the rotated branch); the
   None check can go. `NoCapableSeat.needs` / `.tried` carry the detail for
   the message.
2. `TaskSpec.__post_init__` and `_read_task_needs` keep their own
   `_TASK_NEEDS` set. Prefer `task_kinds.KNOWN_NEEDS` and
   `task_kinds.normalise_needs()` so the vocabulary has one owner; the
   `ValueError` you already convert to `RunStalled` is what `normalise_needs`
   raises on an unknown label.

Observations, no change requested:

- Pinned lead: routing with `available=lambda k: k == spec.lead` and then
  requiring `selected == spec.lead` is a sound way to check the pin without
  substituting; with the catch above it stops loudly for an unfit pin.
- Recovery guard set (refusal, partial-work, exhausted window, explicit pin,
  role/model mismatch, `inspected and not changed`) matches the agreed
  boundary. `TimeoutError` is not a `ProviderError`, so a timed-out lead is
  not recovered; correct, since a timeout may have written.
- `needs_from_text` runs inside every `TaskSpec` construction. Any existing
  test that builds a rote TaskSpec whose description mentions running
  something will now route one seat up; expect a few fixture edits, not
  helper changes.
- `excluded = policy_for(spec.kind).exclude` passed through `available` into
  `escalate_from` is the right place; `escalate_from` itself does not read
  the policy.
- Recovery records `recovery_attempt: 1` and a `lead-recovery` artifact and
  re-records selections; the second `_draft_with_channels` sits outside the
  try, so there is no second recovery. Good.

- Checks run after the rename: focused 45 passed (`test_grok_diagnostics`,
  `test_joint_diagnostic_ledger`, `test_capability_matching`); ruff clean;
  full `python -m pytest -q`: 708 passed, 3 skipped in 43.64s.

## 2026-09-15 — your two helper findings fixed; integration `15ade71` reviewed

- **Diagnostic provenance** (reproduced, fixed): names are read only from
  tool-call containers (`toolCalls`, `tool_calls`, `toolUses`, `tool_uses`)
  or from mixed transcript entries (`messages`, `events`, `steps`, `items`,
  `turns`, `content`) that carry a tool-call `type` or a tool-specific field
  (`toolName`, `tool_name`, `tool`, `function.name`). A bare `name` on a
  message or event is never recorded. Your exact case now yields
  `{'stop_reason': 'cancelled'}`. Negative fixture with a person's name, an
  assistant name, an event label and a typed non-tool entry added; the
  walk now follows envelope order.
- **Direct-write over-inference** (reproduced, fixed): DIRECT_WRITE needs a
  binary/lock/generated *target* and an edit or generation verb together;
  `.svg` moved to the source-path list. `Explain icon.svg` → nothing;
  `Change the fill color in icon.svg` → `{patch}`; `Replace ... logo.png`
  → `{direct-write}`. Also tightened the edit-verb regex to explicit
  inflections (`fix\w*` had matched "fixtures"). Fixtures added for all of
  these.
- The helper now emits `attempted_tools` (`c4c7d5f`), so the
  `tools_attempted` fallback in `safe_diagnostics` (`delegation.py:228`) is
  dead code from this commit on; drop it or keep it, your call.
- GameTape follow-ups you listed as open were already addressed in
  `744a45b` and `a8772ab` (entry above): Tab back onto the parked control,
  close/reopen, launch inside cleanup, empty selection exits 2 (verified
  `run.js nomatch` → exit 2 here). Please re-check on your side.
- Review of `15ade71`: matches the contract and my two review points
  (`NoCapableSeat` → `RunStalled`; pins checked with `seat_satisfies`).
  Remaining nits only: `_TASK_NEEDS` duplicates `task_kinds.KNOWN_NEEDS`
  and `_read_task_needs` re-implements `normalise_needs`; the
  `selected is None` branch in `_pick_lead` cannot fire. Recovery guard set
  and the single-recovery structure are as agreed; the reviewer prompt now
  says line-start `BLOCKING:` and forbids `BLOCKING: none`, which closes
  my items 2 and 3 on `3a4f7ad`.
- Checks run on the merged head (your `15ade71` + this commit): `ruff check
  quadratus tests` clean; focused 70 passed (capability, diagnostics,
  ledger whitelist, recovery integration); full `python -m pytest -q`
  **733 passed, 3 skipped** (playwright, gradio absent here).
- Draft PRs: Quadratus #10 and GameTape #2, both against the branches the
  review targeted. Nothing else open in my lanes.

## 2026-09-15 — review of `a5adac4` (accounting labels); status

- Read the diff and both regressions. The human report now says "Combined
  reported sum (conditional)" with the overlap caveat, `reconcile` exposes
  `combined_reported_tokens`, `parent_child_overlap` and
  `auxiliary_tokens_scope`, and `known_minimum_tokens` is kept as a
  qualified compatibility key. That is the wording I asked for in the
  review; nothing further from me. One note: `parent_child_overlap` reads
  `not_applicable` whenever the native sum is zero, including when a child
  was deduplicated against a controlled row. Accurate, since nothing was
  added, but a reader may want "deduplicated" spelled out; optional.
- `KNOWN_NEEDS` / `normalise_needs` adoption in `session.py` seen; thanks.
- Your remaining two helper findings are closed in `6c3cb14` (entry above).
- Final state on the merged head (`6c3cb14` on top of `a5adac4`):
  `python -m pytest -q` **735 passed, 3 skipped**; ruff clean. GameTape at
  `a8772ab`, accepted by you. Both lanes complete from my side; I remain
  subscribed to PRs #10 and #2 and will act on review comments there.

## 2026-09-15 — CI red on PR #10: lint on the evidence bundle

- Every run on this branch failed at `ruff check .` with one E402 in
  `docs/handoffs/reliability-repair/gametape-completion/browser/server.py`
  (sys.path insert before `import app`). The base branch
  `codex/project-workflow` has failed the same way since `e564491`
  published that bundle (run 54 at `39fc15e` shows the identical error), so
  the suite never ran on any of our commits.
- Fix: `extend-exclude = ["docs/handoffs"]` under `[tool.ruff]` in
  `pyproject.toml`. Evidence scripts stay byte-identical; only the linter
  stops reading records as project code. `ruff check .` is clean locally.
  The pytest step will now run in CI with playwright installed; I will
  watch the result and act on anything it turns up.
- With lint passing, CI ran the suite for the first time on this branch:
  752 passed, 1 failed. `test_real_cli_write_grant_prevents_timeout_replay`
  (`tests/test_acceptance_repairs.py`) resolves the Claude binary from PATH
  when building argv; the runner has no Claude CLI, so `argv[0]` is None and
  the debug log join raises before `_launch`. It passes on your Mac and my
  checkout because both have the CLI. Reproduced here with
  `PATH=/usr/bin:/bin`. Fix: pin `shutil.which` in that one test, the same
  way `tests/test_cli_providers.py` does. **Handoff note:** that file is in
  your lane; this is a one-line, test-only, environment-independence change
  made to get the PR green, recorded here for you to accept or revise.
- Hosted CI on PR #10 is green at `1690059`: `test (3.11)` and `test (3.12)`
  passed on both the push and pull-request runs (753 tests, playwright
  installed, no skips). First green run on this branch; every earlier head
  stopped at lint. `STATUS.md` can drop its "hosted checks pending" line.
