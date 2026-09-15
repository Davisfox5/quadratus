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

## 2026-09-15 — resumed directly with cloud Claude, 7179c90 reconciliation

Pulled 64a02ef and 7179c90 by fast-forward on the clean shared branch. Sent
coordination and review comments on PR #11 (5683171942 and 5683227424).
Claude retains the native-transport and independent examiner lanes; Codex owns
the task brief, run controls and isolated runner. No private reference or test
contents were requested or read. The archive was not found by filename in
Downloads or Quadratus; asked Davis for its local path while continuing work.
Claude reports a nine-case private set and committed the archive SHA-256 in
PR comment 5680554247:
`71cb8b06a34ed94d6936bd9dfc45ed3889a0f6c5a80be1424850f1140e66371a`.
That is a remote claim/commitment; local hash verification is pending.

Applied N2/N3: copied the app CLAUDE.md Design section into neutral application
constraints, without its runtime model discussion; added unknown-column and
blank-line rules exactly. The previous v2 example is now superseded, not
silently overwritten or represented as the final trial input. Added
JOINT_REPAIR.md to explicit export exclusions. Documented that unknown-usage
failures stop; known-usage retries still count against the shared limits.
Closed low review items: timeout=None now receives the remaining deadline and
is restored after return; documented why the per-task worker ceiling cannot
widen the global provider-attempt cap.

Runner N4: added optional auth-only staging. Only three exact vendor auth-file
paths are accepted, owner-only permissions are required, and symlinks/config/
history/other files fail before launch. Credentials must be a separate tree.
The read-only seed copies into ephemeral HOME; refreshes cannot write back to
source. PIDs explicitly raised to 512. No host home or examiner is mounted.
Added real-container proof of auth copying, clean home, writable temporary
refresh, immutable source and container removal. Synthetic credentials only
in that test. Live credentials were staged privately outside all repositories
for preflight; no contents were printed or committed. Host sign-in checks show
ChatGPT, Claude subscription, and Grok OIDC; this does not prove container auth.

Reviewed Claude's N1/N5 code. Conservative Grok stop is useful, but attempted
Agent evidence does not establish execution. Asked Claude to distinguish
suspected fan-out from confirmed native children in its wording/tests, retaining
the stop. Claude JSON still cannot establish absence of undenied children.

Validation: focused suite **127 passed in 6.28s**. Full local suite with
QUADRATUS_TEST_DOCKER=1 and QUADRATUS_LIVE_CODEX_FEATURES=1: **880 passed in
55.24s**, no skips. Ruff and git diff --check pass. These checks precede any
live vendor probe; the installed-Codex check reads configuration only.
Preparing a clean Linux image with pinned vendor CLI versions, Python/browser
checks and Node 24. No scored run or model call started in this checkpoint.

Container preflight now passed without model calls: image
`sha256:f801ce6551eb6f00271d94773581c803e02dacd0dc3e94fada29e7ccbfb4e013`,
Node 24.15.0, Codex 0.154.0, Claude 2.1.269, Grok 1.0.30. In the isolated HOME,
Codex reports ChatGPT auth, Claude reports claude.ai/firstParty/Max, both Codex
multi-agent switches report false, no API-key environment variables are
present, and Chromium launches successfully. Grok OIDC was staged but its
server authentication still requires a live call. Sanitized evidence and exact
runtime source hashes are in evidence/container-preflight.json. Recipe and
operating boundaries are in tools/acceptance/. A first unscored Sol native
availability probe is now running under one attempt, a 50,000-reported-token
stop threshold and an 80-second outer watchdog (70-second controller). No
retry or scored-task input; raw outputs stay private.

### Live Sol probe: native-off FAILED; scored run paused

One bounded invocation of gpt-5.6-sol at low effort returned in 13.13s. Despite
`--disable multi_agent --disable multi_agent_v2` and the clean-image feature
check showing both false, the runtime found a linked child running
**gpt-5.6-sol** (parent 01a0a5bd-af5e-76a3-bb35-aee0482758b4, child
01a0a5bd-c623-7410-9f41-894c71e0bfd0). RunBudget correctly latched
`uncontrolled_native_delegation`; no retry or further probe started. Parent
reported 43,817 input + 78 output = 43,895; child reported 15,836 input + 5
output = 15,841 separately. Do not assume counters are additive. A separate
unidentified `wait` activity row does not prove another child.

The container was removed successfully. Its tmpfs HOME (including raw rollout
files) was removed too; the summarized linked-child evidence survives in
sol-native-probe.json, with the exact limitation recorded. Private stdout and
the returned reply were preserved. Future probes need narrowly selected raw
rollout evidence copied out before cleanup. This is real evidence against the
flag guarantee, not an application/scored attempt. Asked Claude to investigate
in its native-control lane; do not consume further model calls repeating flags
that already failed, or launch the scored run on their strength.

Reconciled Claude 0f2a657 (attempted Grok fan-out wording and cancelled-envelope
regression) via merge 7fe96f6 and pushed normally after the concurrent remote
push. Full suite on that merged runtime: **881 passed in 54.40s**, no skips,
with both opt-in checks enabled. Temporary credential seed deleted; no owned
containers remain. Host credential files were never mounted directly.

Added tools/acceptance/native_probe.py for the next reviewed probe: same tiny
probe and one-call limits, but matching parent/child rollout files are copied
privately out of tmpfs before cleanup and hashed. Unrelated sessions and symlinks
are excluded; copied files remain mode 600. Regression **1 passed in 0.05s**.
The script refuses host execution and requires evidence under /work. No second
model call was made. Claude has the blocking native-off failure to investigate;
private archive location and final freeze also remain pending.

## 2026-09-15 — agents.enabled live checks and archive received

Pulled Claude c8bafa7/b1a6ff9 by fast-forward. Asked Claude for the actual archive
delivery location on PR #11; it confirmed a cloud attachment and resent it.
Downloaded through the user's Claude desktop Code session to Downloads,
verified the full archive SHA-256 against the original commitment, set mode
600, and left its contents unopened. No manual user transfer remains needed.

Both sequential Sol checks under the full control passed in the same isolated
image as the failed probe: tools reply listed only functions.wait,
functions.request_user_input and functions.exec; spawn-provoking reply was
"Native delegation unavailable". No collaboration events or native children
observed. Each parent rollout now survives privately with its hash. Reported
usage: 12,666 + 12,778 = 25,444; wall time 9.03 + 7.65s. The exact limited
claim and event/usage evidence are in evidence/agents-enabled-probes.json.

Claude Fable and Grok tool-list/spawn checks also returned without an observed
child. These are narrow live checks, not proof that their JSON envelopes expose
all hidden activity. Initial Grok probe failed because my script sent the
literal ledger alias "default". Production Fleet already omits that model flag;
fixed the probe to use Fleet's alias_for and added a regression. The failed
attempt is retained with unknown usage, not erased or counted as free. The
corrected probe is a separately recorded preflight, not an automatic retry.

Restricted Grok read check: the generated random value from probe.txt appears
in the reply and the source stayed unchanged, so read access was demonstrated.
The strict exact-reply check FAILED because Grok prefixed narration. No direct
tool name is present in its envelope, so the record does not invent one.
Seven returned probes reported 75,135 tokens in total; one additional failed
probe has unknown usage. No scored application attempt has started.

Full suite after Claude's control fix and the tool-list mode: **883 passed in
56.91s**, no skips, with Docker/installed-Codex checks enabled. Later probe alias
regressions: **2 passed**. Image recipe now states arm64 and adds the app's
missing openpyxl dependency and Node Playwright matching the Python version.
The feature readout is explicitly not a native-off pass criterion. Final image
and application baseline checks are next; do not infer them from the earlier
image's preflight.

Final image preflight passed offline: image
`sha256:d1f99331a1639f5ff364faf9e4c027943613fadba733d452191296b873a390e2`,
100 Python, 18 Node, 9 mutants killed, 8 Chromium scenarios passed. All 30
exported file hashes match after testing. The baseline used a disposable copy;
v3 input remains untouched. The initial app preflight on the old image skipped
browser checks (exit 2): Node Playwright was missing. Two installer attempts
hit the Python/Node CLI filename collision; installing the matching Node module
under /opt/browser-tools fixed it without overwriting Python's CLI. Final
recipe, image ID, dependency versions and checks are recorded in application-
preflight.json. No model call was used for provisioning or these app checks.

Asked Claude to review the remaining workflow/messaging tool scope before a
blind-ready claim; the observed tool names alone are not proof of cross-session
access. Final freeze and scored attempt remain pending that review. Archive
transfer and hash verification are complete; do not ask Davis to repeat them.
