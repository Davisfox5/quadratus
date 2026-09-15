# Joint repair checkpoint — September 15, 2026

The agreed offline repair batch is implemented, cross-reviewed and tested.
Work remains on `codex/claude-review-repairs` in both repositories. Neither base
branch was merged or deployment changed.

| Repository | Source checkpoint validated | Review |
| --- | --- | --- |
| Quadratus | `4c0cea51923fe687476c6d1b78490cda0abf8c69` | [Draft PR #10](https://github.com/Davisfox5/quadratus/pull/10), base `codex/project-workflow` |
| GameTape | `a8772ab32f28dd96d06fda8152504b0b9f15e262` | [Draft PR #2](https://github.com/Davisfox5/sports-video-tagger/pull/2), base `quadratus/reliability-acceptance-v2` |

Subsequent documentation, lint-configuration and portable test-fixture commits
do not change these tested runtime source files.

## What changed

- Scope stops no longer say the scope passed. The invocation retains its
  provider outcome and usage, while a post-return scope failure is recorded on
  that same event. Partial work is preserved and tokens are not counted twice.
- Tasks carry explicit/inferred execution, patch and direct-write requirements.
  Routing checks those needs rather than treating every rote task as suitable
  for a restricted worker. Text patches remain available to restricted seats.
- A failed lead can move upward once only after source inspection proves no
  selected source content changed. Refusals, interruption, scope failures,
  unknown/partial work, explicit pins and non-lead failures retain stop behavior.
- Grok failure diagnostics record bounded tool names with tool-call provenance,
  stop reason and model-call count. The ledger independently filters this data.
  Ordinary message names and tool arguments do not enter those diagnostics.
- Standard review rechecks use explicit finding markers and a single standalone
  boundary verdict, rejecting contradictory or ambiguous markers. The security
  excursion's separate verifier policy is unchanged.
- Accounting reports qualify parent/child sums as conditional, identify the
  auxiliary counter's scope, and avoid re-adding a child already counted as a
  controlled session. The old numeric `known_minimum_tokens` key remains a
  deprecated compatibility field; it is not proof of non-overlapping usage.
- GameTape settles keyboard focus intentionally, respects subsequent user focus
  choices, exposes the preview scroller to the Tab cycle, and releases another
  project's waiting status. Portable browser tests now live in GameTape, reset
  their own fixtures, reject empty selection and clean up after launch failure.

## Validation and peer review

Codex's final local Quadratus run: **753 passed in 48.15s, no skips**. Full
`ruff check quadratus tests` and diff checks passed. Provider calls in these
tests are scripted; this is offline regression proof, not a new live worker run.
Claude's CI repair `99271c5` excludes captured historical handoffs from lint
discovery; `ruff check .` also passes locally. After making the scripted timeout
fixture independent of an installed vendor CLI, hosted Python 3.11 and 3.12
checks both passed **753 tests, no skips** at `1690059`
([verified CI run](https://github.com/Davisfox5/quadratus/actions/runs/34929335343)).
The final merge retains those exact runtime and test files. The fixture also
passes locally with vendor CLIs removed from PATH.

GameTape: **100 Python tests, 18 Node tests, nine detected application mutations,
eight real-browser scenarios**. Browser checks used Node 24.15.0, Chromium
headless shell 1234 and an ffmpeg-generated synthetic video. No page errors;
only the deliberate HTTP 409. Backend tests ran before the final UI follow-up;
backend source and Python tests are byte-identical afterward. UI, mutation and
browser checks reran on `a8772ab`. An unmatched browser scenario exits 2.

Claude reviewed Codex's scope/telemetry/verdict slice and session recovery
integration. Codex independently reviewed and ran Claude's GameTape and
capability/diagnostic changes. That review found a focus ownership edge case,
two runner defects, message-name leakage into diagnostic tool names, and excess
direct-write inference for SVG. Claude fixed them; Codex verified the corrections.
Claude subsequently reviewed the accounting-label correction at `a5adac4` and
accepted it in `811ddd8`; its optional deduplication-label note remains in the log.

Reproducible commands/results, evolving findings, ownership and intermediate
failures are in [CODEX_LOG.md](CODEX_LOG.md) and [CLAUDE_LOG.md](CLAUDE_LOG.md).
Sanitized local validation records are under [evidence/](evidence/).

## Deliberately not claimed complete

- No new subscription/vendor run was started for this repair batch. A bounded
  restricted-seat live probe is still needed to identify Grok's precise
  cancellation trigger and verify currently supported permission behavior.
- This is not all-worker coverage. Native vendor delegation and Quadratus
  worker-pool commissioning remain distinct; the worker pool still needs a
  suitable live acceptance task within the approved roles and budgets.
- Automatic Claude aggregate collection and proof of Sol parent/child counter
  non-overlap remain follow-up work. The old historical totals are qualified,
  not rewritten as a verified bill.
- Video-lock redesign is deferred. Exports and video edits share mutable media;
  removing the lock needs source snapshots/version checks and race tests.
  GameTape's supported deployment remains one Flask process.
- Requirement inference is heuristic; explicit NEEDS labels and validated
  source/grants remain important. Labels never create a write permission.
- Invocation rows flush after acceptance checks; a hard process kill in that
  brief interval can lose a pending row. This is not a write-ahead journal.
