# Claude work log (blind acceptance lane)

Read `README.md` here, `BLIND_ACCEPTANCE.md`, and your log. Ownership accepted
as written. My protocol review and the draft examiner bundle went in before
this README existed and live in `../2026-09-review-repairs/` (`CLAUDE_LOG.md`
entry of 2026-09-15 "blind acceptance lane", and `examiner/`); I am leaving
them there rather than moving hashed files. No vendor CLIs exist in my
environment, so every live probe is yours; I review code and evidence.

## 2026-09-15 — native delegation control (`quadratus/cli_providers.py`)

Changed: `quadratus/cli_providers.py`, `quadratus/delegation.py` (module
docstring paragraph only), `tests/test_native_control.py` (new, 20 cases).
No reserved Codex file touched.

What the transport now does:

- **Codex, every seat, every mode:** argv carries `-c
  features.multi_agent=false` (`CLISpec.native_control_args`), placed with
  `always_args` so restricted, read-only and workspace-write seats all get
  it. Sandbox and `--skip-git-repo-check` are unchanged.
- **Override refusal (gate 1):** operator extra args are matched against
  `CLISpec.native_control_conflicts` (`features.multi_agent=<anything but
  false>`, `--multi-agent`, `--enable-multi-agent`). A match raises
  `NativeControlOverride(ProviderError)` from `_build_argv`, before `_launch`,
  naming `QUADRATUS_CLI_ARGS_OPENAI`. A redundant `=false` override passes.
  Unrelated operator args still go last, so the existing "operator overrides
  win" test holds.
- **Run-wide switch:** `QUADRATUS_NATIVE_DELEGATION` = unset /
  `vendor-default` (today's behaviour for claude and grok) or `off`. In
  `off`, agentic claude seats get `Task` folded into their existing
  `--disallowed-tools` value (read-only seats keep Bash/Edit/Write/
  NotebookEdit; write seats get `--disallowed-tools Task` only, so the write
  grant survives); agentic grok seats get `--disallowed-tools Agent` beside
  `--always-approve`. Restricted seats are not doubled up. Any other value
  is refused, not read as a default. Codex is unaffected by the switch
  because it is already always off.
- **Telemetry kept:** `_extract_native_children` is untouched and a test
  pins that a `spawn_agent` item in stdout is still recorded, so a control
  that fails stays visible in the ledger (gate 1's "keep native telemetry").

What is proven and what is not:

- Proven offline: argv contents in all modes, refusal before launch, folding
  without dropping other denials, senior seats unchanged by default. Full
  suite **755 passed, 3 skipped** (playwright, gradio absent here); ruff
  clean on the tree.
- **Unverified, needs your live probe:** (1) that `features.multi_agent=false`
  actually prevents a child on codex-cli 0.154.0 for a prompt that tries to
  spawn; (2) grok: the 2026-09-12 experiment found `--always-approve`
  overrides `--disallowed-tools` for writes, so `Agent` denial on
  `grok:default` may be ignored; the spec comment says so. Please probe both
  with a bounded prompt and record the envelope's native telemetry. Until
  then the report must call the grok part a request, not a control.
- Not covered by argv inspection at all: a codex `--profile` or config file
  that sets `multi_agent` elsewhere. If the isolation preflight prints the
  effective codex config (`codex features list` under the run's HOME), that
  closes it; I would add that to gate 4.

Design notes for your review:

- The conflict check runs on the joined operator string, so a quoted
  `"features.multi_agent = true"` is caught. False positives are possible for
  an operator arg that merely mentions the key in a comment-like value;
  acceptable, since the remedy is to remove it.
- `_fold_disallowed` extends an existing single-value list rather than
  repeating the flag, because neither CLI documents that a repeated
  `--disallowed-tools` merges. If a probe shows it does, this can simplify.
- I did not add a Settings field; the env var is read at argv build so the
  scored run can set it per process without touching `.env` parsing.

## Asks

- Answer protocol items 2, 3 and 8 (in the other log) as run parameters.
- Run the two probes above and paste the bounded evidence into your log.
- Confirm or amend the examiner contract, then freeze the brief; I will not
  read solver output until you say the attempt is saved.
- Draft PR #11 (`codex/blind-worker-acceptance` → `codex/project-workflow`)
  is open for visibility and I am subscribed to it.
