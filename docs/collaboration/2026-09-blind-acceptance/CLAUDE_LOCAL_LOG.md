# Claude work log

Read `README.md` first. Codex writes `CODEX_LOG.md`; this file is Claude's only.

## 2026-09-15 — native-delegation control lane

Claimed and edited: `quadratus/cli_providers.py`, the introductory paragraph
of `quadratus/delegation.py`, new `tests/test_native_control.py`, this log.
Nothing else touched. No commits, pushes, branch changes, subagents or model
calls. Two working windows; the second finished tests and this log.

### Intent

Every Quadratus call on the OpenAI transport must tell codex not to spawn
its own agents, so helpers pass only through `WorkerPool` (Luna/Terra, no Sol
children), and an operator override must not be able to undo that silently.
Sandbox, effort, `--skip-git-repo-check` and native telemetry stay as they
were. No permission grant is widened.

### What was probed (configuration reads only, `codex features list`; no model call)

- `--disable multi_agent` wins over `--enable multi_agent`, over every `-c`
  spelling tried (`-c`, `-cKEY=`, `--config`, `--config=`, inline
  `features={...}` table, spaces around `=`), and over `[features]
  multi_agent = true` in a `CODEX_HOME` config file, regardless of order.
- `-c features.multi_agent=false` alone is weaker: a later `-c ...=true`
  wins and `--enable` beats it from any position. So the control uses
  `--disable`, not `-c`.
- `multi_agent_v2` is a separate switch (present, off) untouched by disabling
  the first; both are disabled.
- `--disable <unknown>` errors ("Unknown feature flag"); `-c
  features.<unknown>=false` is ignored silently. The loud form is used on
  purpose: a renamed switch fails every call and says why.
- `agents.enabled` is an accepted config key (`agents.bogus` is rejected;
  `agents.max_threads=0` fails to load). Any `agents.*` override is refused.
- A bare `--` after `codex exec` makes everything after it positional, so a
  control placed after it would become prompt text. `--` is refused.
- `--profile`/`-p` could not be tested via `features list` (runtime-only
  flag). Assumed to behave as the config-file layer, which `--disable` beats.

### Changes

- `CLISpec.control_args` and `CLISpec.override_conflicts` (new fields).
- `CODEX_SPEC.control_args = ["--disable","multi_agent","--disable","multi_agent_v2"]`,
  `override_conflicts = codex_override_conflicts`.
- `_build_argv`: the operator's extra args are scanned first; a conflict
  raises `ProviderError` before launch (no retry, no reroute, no reservation
  consumed by a launch). Control args are placed after the permission axis and
  before extra args, so existing tests that pin overrides last still hold; the
  scanner is what covers the spellings that could out-order a weaker form.
- Refusal message names the offending key/flag only, never the value.
- `_observe_output`: unchanged extraction; when a native child is observed on
  a transport whose spec carries a control, the child is annotated
  `CONTROL FAILURE: ...` and a warning is logged. `native_delegation_disabled`
  property added. Telemetry and `codex_children` session reading retained.
- `delegation.py` intro: already reworded in the tree when the second window
  opened (control described, other vendors' gap and codex control failure
  named). Left as found; note it says a helper "exists only if WorkerPool
  commissioned it", which should read as configuration proof until the live
  probe runs.

### Validation

```
/tmp/quadratus-review-env/bin/python -m pytest tests/test_native_control.py tests/test_cli_providers.py -q
127 passed, 1 skipped
/tmp/quadratus-review-env/bin/ruff check quadratus/cli_providers.py quadratus/delegation.py tests/test_native_control.py
All checks passed!
```

The skipped test is opt-in (`QUADRATUS_LIVE_CODEX_FEATURES=1`): it runs the
installed binary with the control plus `--enable` for both switches and asserts
`features list` reports both off. Configuration read, no model call.

Tests cover: control present on read-only, writable, granted-directory,
restricted worker (Luna) and senior/escalation seats (Sol, Astra, Terra);
sandbox/effort/mandatory flag retained; benign overrides still last; agreeing
overrides accepted; 18 conflicting spellings refused on every permission mode
before launch; value redaction; telemetry with a fake spawn stream marked as
control failure; claude/grok gaps pinned.

### Not claimed

- No live proof that a codex `exec` turn cannot spawn a child. This is
  configuration proof only; the bounded probe is still owed.
- Profile-layer precedence assumed, not measured.

### Remaining concerns and gaps (documented, not implemented here)

- **Claude senior seats** (Opus reviewer, Fable/Astra orchestrator) keep the
  `Task` tool; only restricted seats deny it. A `--disallowed-tools Task` on
  senior claude seats would be the analogue, but it changes what a reviewer
  can do and needs its own decision.
- **Grok senior seat**: `--always-approve` overrides `--disallowed-tools`
  (verified 2026-09-12), so `Agent` cannot be denied there. No switch exists;
  containment is the scratch directory only.
- A rejected override still passes through `_observed_call`, so the run
  budget reserves an attempt and finishes with unknown usage. Codex may want
  pre-launch `ProviderError`s to release the reservation instead of latching.
- A codex release that renames `multi_agent_v2` will fail every OpenAI call
  loudly until Python is edited. Deliberate; there is no env escape hatch,
  because one would be the silent bypass the gate forbids.
- `codex exec resume/fork` subcommands in extra args are not scanned.
