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

## Active handoff — concrete Grok failure, owners assigned

Claude review e51f811/fdcbd99 is pulled. The two wider-denial tool-list checks
are complete: **Claude passed; Grok failed**. Grok still reports spawn_subagent,
workflow, search_tool, use_tool and scheduler_create. No scored run started.
The shared fold joins names with spaces; installed Grok help requires commas.
These are reported tool names, not evidence that a child actually ran.

- **Claude now:** repair vendor-specific denial folding and its native-control
  tests, check scheduler_create scope, then push the patch. Explicit assignment:
  https://github.com/Davisfox5/quadratus/pull/11#issuecomment-5684742091
- **Codex concurrently:** preserve the evidence and prepare the frozen launcher
  offline. Pull and independently verify the patch; run one Grok tool-list
  check on the changed control. No duplicate Claude/Sol probes.
- **Codex after control passes:** freeze task/source/runtime/image/config and
  private archive commitment, then execute one uncoached attempt within 15
  minutes, 24 provider attempts, two helpers and 500,000 reported tokens as a
  post-return threshold. Preserve partial work; no extension.
- **Claude after saved scored output is published:** independently review and
  score using its precommitted private cases. Do not modify application source
  or examiner cases during preparation or the run.

Neither agent should infer a handoff from an old waiting note. A failed check
must name the evidence, repair owner and verification owner explicitly.
