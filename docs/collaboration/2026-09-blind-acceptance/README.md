# Joint blind-acceptance implementation

Branch: `codex/blind-worker-acceptance`, based on merged repairs `a9e1b79`.
Protocol: [BLIND_ACCEPTANCE.md](../2026-09-review-repairs/BLIND_ACCEPTANCE.md).
This is implementation and peer review preparation, not a claim of a scored run.

## Ownership

| Owner | Files | Deliverable |
| --- | --- | --- |
| Codex | `quadratus/run_budget.py`, `quadratus/runtime.py`, `quadratus/project_run.py`, `quadratus/providers.py`, `tests/test_run_budget.py`, runner/isolation preparation files, this README, CODEX_LOG | Shared call/token/deadline control, offline proofs, prepared isolation preflight |
| Claude | `quadratus/cli_providers.py`, `quadratus/delegation.py` introductory wording only, `tests/test_native_control.py`, CLAUDE_LOG | Enforce native OpenAI delegation disabled despite CLI overrides; review other vendor gaps; tests |
| Claude next review | Hold-out fixture design and Codex controls | Record review before application solver output exists; do not implement the application feature |
| Codex next review | Claude native control changes | Independent tests and override/bypass checks |

Both agents may read everything in this development checkout; only the later
solver must be blind. Keep separate append-only logs. Record intention, claimed
paths, changes, exact validation and unresolved issues. Use path-specific staging;
never force push/reset/clean or change branches while another agent works here.
If a cloud checkout is used, fetch/merge normally and resolve log ownership
before overlapping edits. Do not claim a second agent ran until it returned.

## Review gates

1. Native control cannot be silently overridden. Preserve read/write and
   restricted-seat permissions and existing senior review/security roles.
2. Global budgets count actual provider attempts including retries/workers,
   reserve concurrent starts atomically, and stop on unknown token usage.
   Report token overshoot from already in-flight calls; never promise a hard
   prepaid token cap. Keep failed/partial work and final budget evidence.
3. Time limits include an external watchdog; a method checking time before a
   call does not bound a running process. No hidden model fallback or API spend.
4. Blindness needs a tested filesystem boundary and clean session/config inputs,
   not a copy with full host read access. No live acceptance until verified.
5. Freeze the application brief/fixtures and held-out checks before launch.
   No model names, routing expectations or prior repairs in the solver bundle.
6. Correctness, routing, model coverage and usage are scored separately.
   Conditional fallback models need not appear in a successful ordinary run.

## Current checkpoint

Implementation started. No native-control live proof, isolated vendor
installation or scored task is claimed. The next logs contain actual results.
