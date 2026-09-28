# Claude independent control review

One fresh, tools-disabled Fable 5.1 review of the supplied control code and
integration diff. No solver task or held-out checks were supplied. The text
below is Claude's assessment; Codex dispositions follow in CODEX_LOG.md.

**Verdict:** No defect breaks the stated safety guarantees (no unbudgeted attempt can *start* after a latch; container is force-removed). One correctness gap (#1) contradicts the module's own retry contract and is the only candidate blocker.

**1. Failed transport attempts latch `unknown_usage`, so budgeted retries can never happen (High)**
`run_budget.RunBudget.finish` + `providers._observed_call` finally. Any attempt that raises before a response leaves `self.last_usage=None`; `finish` treats that as unknown usage, sets `_reason='unknown_usage'`, raises — swallowed because `failure` is set — then the retry's `reserve()` refuses. Repro: attempt 1 raises `ConnectionError` → `_generate_once` retries → `RunBudgetExceeded('Run stopped: unknown_usage')`; `budget.json` blames usage, not the network. The docstring promises "Reservations happen before every transport attempt (including retries)"; under this code `max_retries` is effectively 1. Decide explicitly: either failed-without-response attempts count 0/estimated or the doc must say "no retries under budgets".

**2. `finish()` discards completed, paid-for replies (Medium)**
`RunBudget.finish` calls `_check()` after latching, so the very call that crosses `reported_token_threshold` — and every other in-flight call finishing after any worker latched — raises with `provider_outcome='ok'` and the reply is thrown away in `_observed_call`. Contradicts `token_boundary: 'in-flight calls can overshoot'`. Repro: two workers, threshold 1000, A finishes at 1200 → A's text lost; B finishes → `_check` raises → B's text lost. Latch in `finish`, raise only in `reserve`.

**3. Container runs as image-default user (root) with rw host bind (Medium)**
`isolated_run.run_isolated`: no `--user`. `cap-drop ALL` doesn't stop uid 0 from `chmod u+s` on files it owns; `/work` is a plain bind (not nosuid). Result: root-owned/setuid-root artifacts in the host work tree "kept for inspection". Add `--user uid:gid` (non-root) or document a nosuid/scan requirement.

**4. Budget coverage check is shallow (Medium, verify)**
`runtime.Fleet.get` only checks `type(bound).generate is LLMProvider.generate`. A provider overriding `_generate_once`/`_observed_call`/the transport call passes and never reserves. Also `copy.copy(bound)` happens before the `refusal_fallback_model` block; any provider object constructed after that point has no `run_budget` and `getattr(self,'run_budget',None)` silently disables control. Confirm the fallback path reuses `self`.

**5. Ticket leaks / misattributed errors (Low–Medium)**
- `_observed_call` evaluates `self.native_children` before `finish`; if the provider is called outside `_generate_with_observation` (probes) and `native_children` is not a class attribute, `AttributeError` is caught as a "budget failure" (`provider_outcome='ok'`), `finish` never runs, ticket stays in `_active`.
- `reserve()` increments `_calls`/adds ticket before `_persist()`; a persist `OSError` consumes a call and leaks an in-flight ticket, and `_generate_once` retries it (OSError isn't in the re-raise tuple).

**6. `self.timeout` mutation (Low, verify)**
`min(previous_timeout, remaining)` raises `TypeError` if `timeout` can be `None`; and read/restore isn't atomic if the bound view is shared across `max_concurrent_workers` — a stale shortened timeout can persist.

**7. Ledger gaps (Low)**
`_generate_with_observation` only records a refused reservation when `observed` is empty; a refusal on retry ≥2 is unrecorded. `run_limits.max_calls` is reused as `WorkerBudget.max_per_task` (different unit); `config.worker_budget = ...` post-construction — fails if `SessionConfig` is frozen.

**8. `blind_bundle` exclusions incomplete (Low)**
`_EXCLUDED_*` omits `.cursor/`, `.cursorrules`, `GEMINI.md`, `.gemini/`, `.aider*`, `.windsurfrules`, `.github/copilot-instructions.md`. Docstring defers blindness to preflight, but the export is labeled "without repository/session state"; align one or the other.

**9. Minor input validation (Low)**
`image` checks length only, not hex. Empty `command[0]` passes and `--entrypoint ''` resets to image CMD. `rglob` symlink check is pre-start only (workload can create symlinks; harmless for bind semantics, but don't describe it as enforced).
