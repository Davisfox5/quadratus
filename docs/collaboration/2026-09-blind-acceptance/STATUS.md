# Joint blind-acceptance checkpoint

Shared branch: `codex/blind-worker-acceptance`, [PR #11](https://github.com/Davisfox5/quadratus/pull/11).
Claude control/review baseline: `fdcbd99`. No scored application run has started.

## Verified this round

- **Sol fix passed two small live checks.** `agents.enabled=false` plus both
  disable flags removed the native-agent tools from its reported list; a direct
  spawn challenge returned unavailable, with no native children or collaboration
  events observed. Parent rollouts retained privately and hashed.
- Claude Fable and Grok returned unavailable on their native spawn challenges.
  Their envelopes have weaker observability; these are scoped live results,
  not proof against all hidden vendor activity.
- Restricted Grok retrieved a fresh random file value without changing source.
  Exact-response formatting failed because it added a preface. Initial Grok
  probe used the wrong wire alias; fixed in the probe, with a regression, while
  retaining that failed attempt and its unknown usage.
- **883 Quadratus tests passed in 56.91s**, no skips, on the pulled control fix
  with Docker/installed-Codex checks enabled. Later probe-only tests: 2 passed.
  Ruff and diff checks clean.
- Final application image `sha256:d1f99331a1639f5ff364faf9e4c027943613fadba733d452191296b873a390e2`
  passed **100 Python tests, 18 Node tests, 9 mutation checks and all 8 browser
  scenarios**, offline, with unchanged source. Includes openpyxl and both
  Python/Node Playwright. The image is Linux arm64-specific.
- Claude resent its private archive. Codex downloaded it from the Claude Code
  attachment, verified its original SHA-256, and saved it outside both repos
  with owner-only permissions. Contents remain unopened.

This round used **75,135 known reported tokens across seven returned probes**,
plus one failed attempt with unknown usage. All were unscored. No automatic
retry, forced coverage run or application implementation by either evaluator.

Evidence: [Sol checks](evidence/agents-enabled-probes.json),
[vendor checks and archive commitment](evidence/vendor-control-probes.json),
[application baseline](evidence/application-preflight.json),
[Codex log](CODEX_LOG.md), [Claude log](CLAUDE_LOG.md).

## Active handoff — Codex owns the frozen scored attempt

Claude supplied dba749d (vendor-specific delimiter, canonical Grok spawner and
scheduler denial). Merged runtime **a001c1b** passed **891 tests in 52.84s**, no
skips, with Docker/installed-Codex checks; Ruff/diff clean. The single corrected
Grok tool-list check passed: targeted names absent, read/write/exec available,
8,722 reported tokens. These are scoped model reports, not a universal vendor
billing guarantee. No additional Claude/Sol probe was needed.

Final task/source/runtime/image/config and private examiner commitment are
frozen in evidence/scored-attempt-1-freeze.json (SHA-256
f22398d1d16f37d101d3d76ea7a6216c1d270574783892a372e11d89916e4abb).
The private archive remains unopened and outside both mounts.

- **Codex now:** execute the one frozen uncoached attempt: 15 minutes, 24
  provider attempts, two helpers, 500,000 reported tokens as a post-return
  threshold. Save partial work and telemetry; no automatic continuation.
- **Claude next, only after saved output:** independently score/review the
  preserved application result using the precommitted private cases. Keep
  runtime, application source and examiner cases unchanged during the attempt.

No open-ended mutual waiting. Codex posts the exact saved-output handoff on
PR11; Claude acknowledges receipt before Codex calls that handoff complete.
