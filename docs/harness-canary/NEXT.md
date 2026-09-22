# After Q9: what changed, and how the next live test runs

For Codex. Written by Claude after reviewing the Q9 pair (RESULT.md), the raw
run records, the fixture, the launcher and the engine's security path.

## TL;DR

- Q9 failed for one environment reason: the container never asserted
  `QUADRATUS_CONTAINED=1`, so the Codex CLI tried to start bubblewrap inside a
  container that denies user namespaces. The engine already carries the
  measured answer to that (`cli_providers.contained`, 2026-09-17); the launcher
  did not turn it on, and the pair's preflight never asked the question.
- Two harness defects the pair exposed are fixed on this branch: the security
  excursion now serves the WORKER channel its prompt offers, and a blocked
  report has to carry the failing command, exit status and verbatim error,
  which the ledger now also captures from the CLI itself.
- The fixture was too small to tell the candidate from the baseline. The next
  test needs a two-task fixture, one of which is not security work, and a
  five-run series after the first clean pair.

## What is fixed here (offline, no model calls)

1. **Security excursion honours WORKER** (`session._run_security_task` now
   drafts through `_draft_with_channels(consults=False)`). Baseline Q9: Sol
   replied with a worker request exactly as its prompt told it to, the harness
   filed the request as the draft, Opus rejected "a dispatch, not a result".
   CONSULT inside an excursion is refused in words and the lead is re-asked;
   FETCH and WORKER work as on any lead. Pinned by
   `tests/test_canary_evidence.py`.
2. **Blocked reports need evidence.** The lead prompt now carries
   `_BLOCKED_REPORT_RULE`: quote the command, its exit status and the verbatim
   error, or the report is rejected; never describe test output you did not
   see. The security verifier prompt says the same from its side. Candidate
   Q9: Opus's verdict was exactly this complaint.
3. **The CLI's own evidence survives.** `InvocationEvent` gains `stderr_tail`
   (last 2000 chars of the CLI's stderr) and `tool_failures` (failed
   `command_execution` items from the codex stream: command, exit code, output
   tail, at most eight). Both bounded again at the ledger boundary. A future
   "bwrap cannot create a namespace" is in `invocations.jsonl`, not only in
   the model's prose.
4. **Preflight asks the real question.** `run_fixture.py --preflight` now runs
   `tools/acceptance/preflight.py` (sign-in per vendor, work-tree read, and
   each vendor's inner sandbox exercised in the exact mode a seat would draw)
   and exits nonzero on a blocker. The tool takes `--host` when the run itself
   will execute natively. Q9's preflight checked binaries and imports only.
5. **Console log under the state directory.** The launcher writes progress to
   `.quadratus/live-console.txt`, which the project excludes, so
   `source_changed` reports only what a model changed.

Not changed: the frozen Q9 records, the replay pack, any gate or budget.

## How to run the next pair

Pick one execution environment and state it in the allowance record.

**Option A, native on the Mac (recommended).** The vendor CLIs run the way they
are built to run and their own sandboxes stay up. Fixture in a scratch
directory, runtime checked out read-only elsewhere, `CANARY_PROJECT` pointing
at the scratch copy for the grader. Run
`python docs/harness-canary/run_fixture.py --preflight --project <scratch>`
first; it must exit 0. Read the report, not only the exit code: the sandbox
check is `ok: true` only for a vendor with an inner sandbox to exercise
(codex); claude and grok report `applicable: false`, which is correct, not a
pass. The sign-in check is positive for codex (`login status`), positive for
claude (`auth status` prints `loggedIn: true` and the subscription type;
declared in `CLAUDE_SPEC` as of 7b9ffd8, so a preflight before that commit
reported claude's sign-in as unchecked), and by known failure wording for
grok (`models` says "You are not authenticated"). Codex's native run on
2026-09-22 showed the earlier shape: codex sandbox ok, grok not signed in,
claude sign-in not checked. Grok has since been signed in and the fixture-v2
preflight passes. A signed-in grok is required before any pair.

**Option B, the prepared container with the documented substitution.** Set
`QUADRATUS_CONTAINED=1` in the container environment. The engine then hands
codex `--sandbox danger-full-access` because the container is the boundary;
the reasoning and the measurements are in `cli_providers.contained`. Do not
add `--security-opt seccomp=unconfined` or capabilities; that was measured and
rejected on 2026-09-17. The preflight runs the sandbox check in the
substituted mode, so it proves the substitution took effect.

Either way: no run starts on a preflight blocker, and the preflight report is
archived with the evidence.

## The fixture for the next pair

The one-line tenant fix routed straight into the security excursion, so both
versions used the same seats and nothing Q1 through Q11 changed could show.
Build `fixture-v2/` with two tasks in one project:

1. **A non-security task first.** Something the ladder routes to Grok or Sol
   and that needs one lookup before the edit, so a worker errand is natural:
   for example, a helper in a second module that the endpoint must call, with
   the helper's name only discoverable by reading that module. This exercises
   the family packet, worker-fit rejection (Q1), the sibling loop (Q7) and the
   scope guard on the candidate, and their absence on the baseline.
2. **The security task second**, unchanged in spirit from Q9 but with the fix
   spanning two files (endpoint plus the shared predicate), so a scope trap
   exists: a naive fix widens into the auth module and the candidate's
   `scoped-endpoint` packet should stop it.

Keep `max_tasks=2`, the frozen external grader, and a reference repair held in
the grader-control copy only. Add a grader case per task so "3 failed, 6
passed" cannot be produced by fixing one task alone.

## Allowance and series

- First: one baseline, one candidate, same limits as Q9 (24 calls, 500k
  reported tokens, 840s internal, 900s wall, two workers, two tasks). A fresh
  allowance record from Davis; the Q9 one is closed.
- If both complete (either outcome), run five per version on the same fixture
  and report a pass rate per version with the per-run records. That is what
  earns removal of "live reliability not measured". One canary never does.
- Report per run: attempts, tokens split cached versus fresh input, the
  `tool_failures` and `stderr_tail` rows if any, policy plan (candidate),
  packets present per role, grader result, only-declared-paths-changed.

## Review split

Codex builds fixture-v2 and the launcher changes for the chosen environment,
runs the preflight and the pair, and posts the records. Claude reviews the
fixture and grader before any run (they are the measurement instrument), and
reviews the records after (Q10 posture). Sensitive paths in this repo for
this work: `quadratus/session.py`, `quadratus/cli_providers.py`,
`quadratus/delegation.py`, `quadratus/runtime.py`; changes there come to
Claude before merge.
