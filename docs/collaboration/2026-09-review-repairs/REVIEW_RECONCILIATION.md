# Review reconciliation and remaining evidence limits

This note qualifies the September 14 completion report; it does not rewrite the
captured runs. The source under that report remains GameTape `832e50c` and
Quadratus runtime `1bf3b03`. New repairs are on `codex/claude-review-repairs`.

## Usage

The published arithmetic recomputes, but the combined number depends on Sol
parent/child counters not overlapping. The selected published evidence does not
establish that vendor accounting property.

| Component | Tokens | Interpretation |
| --- | ---: | --- |
| Controlled parent calls | 27,446,165 | Sum of reported usage for 90 calls; two have unknown usage |
| Additional Claude aggregate usage | 4,586,562 | Aggregate minus already-counted parent counters; includes 272,570 Haiku tokens |
| Subtotal before adding Sol children | 32,032,727 | Arithmetic subtotal; not a verified bill |
| Separately observed Sol children | 1,381,050 | Do not assume these are incremental without checking parent-counter semantics |
| Combined total if Sol children are incremental | 33,413,777 | Conditional reconciliation, not an established non-overlapping minimum |

The historical `gametape-completion/usage.json` comes from the framework-only
audit. Its `auxiliary_tokens: 0` does not mean no auxiliary vendor activity
occurred; the later Claude aggregate audit supplies that information. Do not
change the zero to 272,570 and then add it to the aggregate adjustment: that
would count the same Haiku activity twice. A future combined schema should name
each counter's scope and overlap status explicitly. Unknown usage stays unknown.

The source reconciler uses per-session maxima; an export with only those final
values cannot independently demonstrate how every intermediate reading was
selected. Recomputing the exported totals verifies arithmetic, not all upstream
vendor semantics. Native activity is not Quadratus worker-pool dispatch.

## Findings and validation boundaries

- Codex reproduced the scope wording and saved invocation defect with new
  regressions before changing code. See `CODEX_LOG.md` for current checks.
- Restricted-worker/task capability mismatch is established by configuration
  and task requirements. The exact Grok cancellation trigger remains unknown.
  Correct conflicting comments after a bounded authorized probe, not inference.
- Review parser defects reproduce in the real session functions. Published
  call counts alone do not prove which wording caused every historical recheck.
- The short Fable interruption is described as deliberate in Codex's activity
  history. The sanitized invocation row proves only a `KeyboardInterrupt`; it
  does not independently prove intent.
- A provider-level `RuntimeError` and an enclosing `ProviderError` can describe
  different layers of the same failed call. Preserve the original records and
  name their layers rather than changing historical exception types to match.
- Grok session IDs are missing in the selected invocation rows. Token matches
  alone are weaker correlation than stable invocation/session identity.
- Four killed application mutants prove those four regressions only. Claude
  reported additional surviving mutants. Portable, independently seeded browser
  tests and missing Node event tests are assigned to Claude's GameTape lane.
- Claude's reported Chromium pass used a placeholder video. It corroborates UI
  behavior, not ffmpeg/media behavior. Codex's earlier browser pass used generated
  media. New media-concurrency claims need separate tests with real media.

## Still open after Codex's first code checkpoint

Worker capability routing and safe recovery; bounded Grok failure diagnostics;
GameTape focus, scroll access, cross-project status and portable regression tests;
explicit runtime accounting scopes and parent/child overlap evidence. Video-lock
redesign is deferred. Neither all-worker acceptance nor a new live provider run
has been performed in this repair batch.
