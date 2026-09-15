# Joint blind-acceptance checkpoint

Implemented and pushed on `codex/blind-worker-acceptance`. Review:
[Quadratus PR #11](https://github.com/Davisfox5/quadratus/pull/11), against
`codex/project-workflow`. Latest pulled peer checkpoint: `7179c9016570561e40df470f9cc74087724dcec6`.
Codex runner/brief follow-up is recorded in CODEX_LOG.

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

**880 tests passed in 55.24s, no skips** locally, with real Docker checks and
installed-Codex configuration checks enabled. Full Ruff and diff checks pass.
The public examiner's **13 backend checks fail on the untouched application**,
as expected. That negative baseline is not a failed Quadratus run.

See [local validation evidence](evidence/local-validation.json),
[Codex log](CODEX_LOG.md), [cloud Claude log](CLAUDE_LOG.md),
[local Claude log](CLAUDE_LOCAL_LOG.md), and
[Claude's independent review](CLAUDE_CONTROL_REVIEW.md).
Hosted [CI at 85a2018](https://github.com/Davisfox5/quadratus/actions/runs/34972145866)
passed on Python 3.11 and 3.12: **861 passed, 3 skipped** on each. The skips
are the two real-Docker checks and installed-Codex config check, which passed
locally. That hosted run predates the current follow-up; the 880-test result above is local.

## Required before the scored run

1. **Live Sol probe FAILED native-off enforcement:** both flags report false,
   but a linked Sol child was observed. RunBudget stopped after one invocation.
   Resolve this before the scored run; Grok/Claude live probes are still pending.
   See evidence/sol-native-probe.json. Config checks are insufficient.
2. The provisioned image passed isolated Codex/Claude subscription-auth checks,
   effective Codex flags and Chromium smoke. Live Grok auth and native behavior
   remain to be checked. See evidence/container-preflight.json.
3. Locate and hash-check Claude's replacement private archive (nine cases,
   commitment posted on PR #11). Its initial public cases remain public
   validation. Freeze the final solver input and runtime/config with the
   private commitment before launch. The user has been asked for the archive path.
4. One uncoached, bounded attempt: 15 minutes, 24 provider attempts, two helpers,
   500,000 reported tokens as a post-return stop threshold. Partial progress is
   an honest result. No automatic budget increase, forced coverage or fallback
   failure injection in the natural run.

The isolated image and credential-only HOME passed preflight. A scored run
has not started; live control evidence and private archive verification remain. See [implementation and
handoff](IMPLEMENTATION.md) for interfaces and remaining ownership.
