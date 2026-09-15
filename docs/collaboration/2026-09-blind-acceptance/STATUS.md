# Joint blind-acceptance checkpoint

Shared branch: `codex/blind-worker-acceptance`, [PR #11](https://github.com/Davisfox5/quadratus/pull/11).
Claude control/review baseline: `b1a6ff9`. No scored application run has started.

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

## Before the scored attempt

1. Claude reviews the published probe evidence and the scope of remaining
   workflow/messaging tools (whether they can reach other sessions despite the
   native spawn denials). Tool names alone do not establish an escape.
2. Freeze the final task, application source, runtime, image/config and private
   archive commitment together. The v3 input has 30 files including all existing
   application tests; the earlier 23-file draft omitted tests/ui. The baseline
   ran on a separate disposable copy, not on the solver's input.
3. Run one uncoached attempt within 15 minutes, 24 provider attempts, two helpers
   and 500,000 reported tokens as a post-return threshold. Record partial
   progress and missing model coverage honestly; no automatic budget extension.
