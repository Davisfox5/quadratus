# Codex work log

## 2026-09-15 — implementation opened

- User requested joint implementation with Claude, following the previous shared
  branch and documented peer review workflow.
- Working tree clean at `7b94992`; kept `codex/blind-worker-acceptance`.
- Reserved the paths in README. Preparing one bounded Claude CLI collaboration
  call for the native-control lane; separate from the scored Quadratus run.
- Codex begins shared runtime budget controls and checks isolation prerequisites.
  No application feature edits or large provider run in this checkpoint.

## 2026-09-15 — first implementation and peer checks

- Claude Fable 5.1 worked locally in the reserved native-control files. First
  420-second window stopped after implementation; a bounded finishing window
  added tests/log and ended on its turn limit. No subagents or scored run.
- Budget/controller tests plus real Docker checks: **23 passed in 4.74s**.
  Runtime/provider/project/recovery regression subset: **97 passed in 5.98s**.
  Git input freezer + budget checks: **30 passed in 0.63s**.
- Docker canary check denied examiner/host-home reads and runtime writes;
  watchdog killed a child in a separate process group and preserved its file.
- Codex found the opt-in Claude config test was still using the autouse fake
  binary `/usr/bin/x`: **1 failed, 86 passed**, a fixture bug, not a failed CLI
  control. With Claude's editing windows complete, Codex takes the focused
  fixture correction and rejection of resume/fork overrides for fresh runs.
  Also qualifying pre-probe source comments; Claude's original log is preserved.
- Token controls reserve before attempts, include retries, stop on missing
  usage/native observations and record a provider success followed by a budget
  stop on the same invocation. These controls are opt-in, not yet a fully
  provisioned three-vendor blind runner. Strict unknown usage is intentional;
  an invalid CLI configuration stops the bounded run rather than retrying free.

## 2026-09-15 — Claude independent review received

- Fresh, tools-disabled Claude review returned successfully (one response,
  separate from its implementation session). Saved its exact final review in
  CLAUDE_CONTROL_REVIEW.md; no reasoning transcript or credentials published.
- Accepted: preserve final responses when the budget stops after provider
  success; run containers as non-root; fail closed/roll back reservations when
  durable budget state cannot be written; check overridden observed-attempt
  methods; tighten known instruction-file and image/entrypoint validation.
- Finding 1's proposed zero/estimated usage is rejected: the user-approved
  protocol explicitly stops on unknown usage. Retries with reported usage are
  counted and tested; failed calls with no usage cannot safely be retried.
- Other review qualifications: provider native_children is initialized before
  each call; Fleet copies the shared provider for every budgeted view and a
  second copy retains the same controller; SessionConfig is not frozen.
  Denied retries are not actual calls and are explained by budget.json rather
  than adding a fake invocation. The reviewer had code text, not an execution
  environment; speculative items are distinguished from reproduced defects.

## 2026-09-15 — peer findings resolved and validation

- Corrected Claude's opt-in config-test fixture; real installed CLI configuration
  proof and budget tests: **79 passed in 0.20s**. Added rejection of resume/fork
  extras and qualified pre-probe native-control comments. No live model probe.
- The first full suite found a compatibility regression in my hook: accessing
  timeout even without a budget broke three minimal provider adapters. Fixed
  the optional path, not the old tests. Affected tests: **81 passed in 6.01s**;
  then full suite **845 passed in 51.93s**, real Docker/config checks enabled.
- Implemented the accepted Claude follow-ups: completed final replies retained
  under budget-responses (not published), non-root containers, storage failures
  stop before launch with reservations rolled back, observed-path override
  guards, additional instruction-file exclusions, strict image/entrypoint checks.
- Follow-up tests including Docker: **42 passed in 5.85s**, Ruff/diff clean.
  Final full-suite rerun is being recorded at the next checkpoint.
- Prepared a 13-file example input export from GameTape a8772ab under ignored
  output/blind-acceptance-preparation/v1. It contains application code, ordinary
  docs/tests and TASK.md, no Git history, agent settings or joint repair logs.
  It is a draft; held-out checks/authenticated runtime are not yet frozen.
- Claude's review is independent code review, not a blind scored application
  attempt. No implementation file in GameTape was changed by this batch.

## 2026-09-15 — final local checkpoint

- Full combined suite after peer corrections: **853 passed in 52.66s**, zero
  skips with QUADRATUS_TEST_DOCKER=1 and QUADRATUS_LIVE_CODEX_FEATURES=1.
  `ruff check .` and `git diff --check` passed. No owned container remains.
- Claude's native implementation plus Codex's focused corrections are accepted
  for this offline checkpoint. Codex implemented the accepted independent
  review findings and ran the final regressions; Claude has not re-reviewed
  those final follow-ups. Raw provider transcripts remain under ignored output.
- Current coding lanes are released for review. No GameTape application edits,
  native-delegation live acceptance, restricted Grok live probe, authenticated
  container, or completed held-out scoring suite are claimed.

## 2026-09-15 — cloud Claude reconciliation

- The normal push found remote work daea6d9/96aabb8. Fetched and merged without
  force. Preserved cloud CLAUDE_LOG.md and prior protocol/examiner work; local
  Fable's log is now CLAUDE_LOCAL_LOG.md. Neither is rewritten as the other.
- Kept the locally verified OpenAI `--disable` implementation, both feature
  switches, parsed conflict rejection and control-failure telemetry. The cloud
  `-c ...=false` plus regex alone misses --enable and multi_agent_v2.
- Integrated cloud Claude's optional all-vendor native-delegation mode in the
  same provider module, avoiding duplicate CLISpec control APIs. Default senior
  behavior stays unchanged. Off requests preserve write grants/tool denials;
  Claude Task/Agent are denied; Grok Agent denial is still an unverified request.
  Extra args for Claude/Grok must be empty in off mode to prevent an override
  from silently removing the denial. OpenAI retains its verified conflict checks.
- Protocol answers 2/3/8: keep 15 minutes, 24 attempts and 500,000 reported tokens;
  a stopped, incomplete first attempt measures bounded progress, not completion.
  No expansion to an hour. Unknown usage stops; charging the remaining balance
  also leaves nothing for continuation. All-vendor off remains a proposed scored
  run parameter that needs live proof before claiming effective enforcement.
- Adopted cloud Claude's application API/CSV/UI contract (5000 rows); no solver
  hint about model coverage. The earlier 13-file/500-row input is superseded.
- Verified repository visibility PUBLIC. Cloud examiner cases have been publicly
  published, so they cannot be described as secret held-out tests. Kept all cases,
  added publication qualifications and updated their manifest. Asked cloud Claude
  on PR #11 to prepare fresh private cases outside Git and publish only hashes.
  No private reference implementation was requested/read; no solver run exists.
- Native merge subset: **140 passed, 1 skipped in 1.23s**, Ruff clean. The skip
  was the opt-in local config read; the final combined run enables it explicitly.

## 2026-09-15 — reconciled checkpoint 85a2018

- Full suite after cloud merge: **864 passed in 51.95s**, no skips, with both
  optional local checks enabled. Full Ruff/diff checks clean.
- Public examiner on a disposable unchanged GameTape copy: **13 expected
  failures in 0.26s**. No solver output exists and no reference implementation
  was read. This confirms the feature is absent, not that the hidden scoring
  problem is solved.
- Kept CRLF fixture bytes intact and added a path-specific cr-at-eol Git
  attribute; its content hash did not change. Examiner publication labels and
  SHA256SUMS updated together, without changing the test assertions.
- STATUS.md and evidence/local-validation.json bind this result to source.
  The three-vendor authenticated launcher and fresh private cases remain gates.

- Hosted CI independently verified at 85a2018: Python 3.11 **861 passed,
  3 skipped in 52.52s**; Python 3.12 **861 passed, 3 skipped in 55.57s**.
  Optional Docker/config checks ran locally instead. Later commits are docs only.
