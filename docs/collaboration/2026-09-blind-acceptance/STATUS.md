# Joint blind-acceptance checkpoint

Implemented and pushed on `codex/blind-worker-acceptance`. Review:
[Quadratus PR #11](https://github.com/Davisfox5/quadratus/pull/11), against
`codex/project-workflow`. Tested runtime source: `85a20188ec8782ba2599981908c08943ff57a4b4`.

## Completed here

- Enforced OpenAI native-delegation disable flags with override/fresh-session
  checks. Opt-in native-off requests for Claude/Grok are integrated; Grok
  enforcement remains unverified and is not claimed from argv inspection.
- Shared attempt/token/deadline controls, worker concurrency settings and durable
  budget evidence. Missing usage stops; retries with usage count. Replies and
  source edits survive post-return budget stops. No free unknown usage.
- Non-root Docker isolation and external workload termination, including
  detached children; explicit Git input exports omit agent/history context.
- Local Claude implementation, fresh independent Claude control review and
  cloud Claude contributions reconciled. Both implementation logs retained;
  accepted review findings fixed and tested. Final fixes await peer re-review.
- Application brief aligned to cloud Claude's export-compatible CSV/API/UI
  contract. A 23-file example input is prepared under ignored output. No
  GameTape application feature was implemented by the evaluators.

## Verified

**864 tests passed in 51.95s, no skips** locally, with real Docker checks and
installed-Codex configuration checks enabled. Full Ruff and diff checks pass.
The public examiner's **13 backend checks fail on the untouched application**,
as expected. That negative baseline is not a failed Quadratus run.

See [local validation evidence](evidence/local-validation.json),
[Codex log](CODEX_LOG.md), [cloud Claude log](CLAUDE_LOG.md),
[local Claude log](CLAUDE_LOCAL_LOG.md), and
[Claude's independent review](CLAUDE_CONTROL_REVIEW.md).
Hosted CI is tracked on PR #11; do not substitute a local result for hosted proof.

## Required before the scored run

1. Live Codex/Grok control probes and verification of effective native-off
   behavior across the vendor seats. An ignored Grok denial blocks a claim of
   bounded native activity.
2. A provisioned, authenticated container with only necessary vendor credentials,
   clean instruction/session state, and preflight in that exact image.
3. Fresh private examiner cases: cloud Claude's initial cases were published to
   this PUBLIC repo. They remain public validation, not secret held-out tests.
   Publish hashes only for the replacement private cases; freeze those hashes,
   the final solver input and the runtime/config before launch.
4. One uncoached, bounded attempt: 15 minutes, 24 provider attempts, two helpers,
   500,000 reported tokens as a post-return stop threshold. Partial progress is
   an honest result. No automatic budget increase, forced coverage or fallback
   failure injection in the natural run.

The controllers and isolation helper are tested building blocks; the complete
live blind-run launch path is not yet provisioned. See [implementation and
handoff](IMPLEMENTATION.md) for interfaces and remaining ownership.
