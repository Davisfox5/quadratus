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
