# Project workflow and review resolution

The Claude/Grok review was substantially correct against `8e71b6b`: the
application had no caller-selected source tree. CLIProvider already supported
a working directory and preserved directories it did not own, but the caller
never supplied one. More seriously, the CLI integration gate ran in the launch
directory while agents worked elsewhere. A green result could describe an
unmodified checkout.

The fix binds the whole coding run to a selected project. Model seating alone
could not solve this. The original Grok review document was unavailable locally;
the findings supplied in the conversation were checked against source and the
installed CLI. The remote Claude branch was fetched for comparison. Its older
`multi_llm` app was not merged over the newer roster; its repository scanner
was adapted into the current package.

## Run a project

```bash
quadratus "Implement the next parser change" \
  --project /path/to/repo --allow-writes --check "pytest -q"
quadratus-gui
```

In the GUI, enter a project folder, click **Open or create project**, describe
the change, enable edits if wanted, and run. An existing folder works without
Git. A nonexistent folder is created and initialized with Git. A GitHub URL
can be cloned into a new or empty destination with the optional URL field or
`--clone`; `--branch` creates a new local branch. Git uses existing credentials.
There is no automatic commit, push, remote branch publication or PR creation.

Project execution requires subscription CLI transport. Model aliases and the
approved Fable/Astra orchestrator and brain-trust/security roles are preserved.
The legacy discussion pipeline still supports API transport and remains the
bare CLI default. No paid API fallback is introduced.

## Where everything goes

| Content | Destination |
| --- | --- |
| Generated and edited source | Selected project folder |
| Diff, report, ledger, result JSON | `.quadratus/runs/<unique-run-id>/` |
| Raw transcripts and author metadata | That run's `artifacts/` |
| Usage records | That run's `usage.jsonl` |
| Cross-session repository notes | `.quadratus/codebase-map.jsonl` |
| Reviewer/orchestrator source copies | Temporary directory, removed after each call |

`--state-dir` can relocate records; relative paths are relative to the project.
State inside the tree is excluded from source snapshots/diffs. Each run has a
distinct record directory. A per-project lock rejects overlapping Quadratus
runs. `-o` exports the report while the actual source stays in the project.

## Execution contract

1. The runner inspects the folder and seeds the codebase map before model work.
2. Every read-only call receives a fresh copy of current source. Copies omit
   common dependencies, build output, internal state, `.env` secrets and
   symlinks. They are copied, never hard-linked.
3. Each editing call needs both the operator's run grant and an explicit call
   grant. Unrestricted editors receive the real project. Restricted editors
   use the same restricted tools and return a `PATCH:` unified diff for checked
   application by the harness. Text patches cannot target internal state,
   secrets, outside paths, symlinks, binary data or rename-only operations.
4. The integration gate executes in the selected project after edits. Its
   result records that directory. Source mutation during a check invalidates
   the result. A failed check gets the existing bounded fix allowance; a
   remaining failure stops the run as incomplete.
5. Explicit DONE reports completion only when checks and findings permit it.
   Reaching the task limit is incomplete. No changes and no executed tests are
   stated explicitly. Model errors and keyboard interruptions preserve the
   partial run report and any source edits already made.

Passing checks only prove what the command exercises. A zero-diff run is not
represented as a source implementation. Snapshots isolate relative writes;
they are not an OS security boundary against arbitrary absolute paths or
malicious commands. Local vendor permissions and trusted project/check commands
still matter. The GUI is localhost-only, including on API configurations.

## Other review corrections

- KIND-prefixed ASK and FETCH remain control messages. An unanswered request
  at a fetch/consult budget boundary raises a clear stalled-run error instead
  of becoming a task or draft.
- Leads can commission `WORKER` JSON requests through the existing worker
  pool. Grants reach the runner explicitly. An internal TypeError does not
  rerun work without the grant, and granting a previously denied operation is
  a distinct retry.
- Grok restricted seats use real `-p <prompt>` invocation and the read-only
  filesystem/web tool allowlist. Senior Grok behavior and role assignments
  remain intact. Prompt files are unique, outside source, and cleaned up.
- Model-bound provider copies share their owner's scratch lifetime. API
  OpenAI roster seats retain distinct models; non-Opus Claude API seats require
  explicit `QUADRATUS_API_MODEL_<SEAT>` IDs. Probes honor operator aliases and
  custom binary paths.
- Review artifacts retain actual reviewer authors. Identical content is still
  deduplicated while all contributor metadata survives reopening old stores.
- GUI attachments are consumed once and cleared after send/clear. They remain
  snippet context, separate from project selection.

## Artifact security fix: fixed

The vulnerable path was model-controlled FETCH to `TaskMemory.fetch` /
`ArtifactStore.get`, followed by construction of an unchecked filesystem path.
Both text and metadata paths also followed attacker-placed symlinks during
reads and writes. The invariant is that an artifact ID names only a regular
record under the selected store, and never redirects I/O outside it.

The shared ArtifactStore boundary now validates canonical 12-character hex
IDs and performs directory-relative I/O with no-follow flags and a regular-file
check on the opened descriptor. It preserves `get`'s KeyError and `ref`'s None
semantics, old metadata, Unicode, empty records and content deduplication.
The relevant implementation is `artifacts.py`, with provenance attribution in
`memory.py` and `session.py`.

`tests/test_artifact_boundaries.py` exercises traversal, absolute/encoded and
malformed IDs, forged ArtifactRefs, existing/dangling/cyclic symlinks for reads
and writes, a FIFO, and a model FETCH reaching the session. Outside sentinels
remain unchanged and absent from model prompts. Legitimate reopen/dedup and
old-store-plus-new-reviewer cases pass. One independent read-only investigator
and one independent candidate reviewer were used under the fix-finding skill;
the review's legacy provenance regression was reproduced and corrected.

Ordered checks (all passed in the Python 3.12 review environment):

1. Artifact imports/execution and focused triggers, alternate link states,
   legitimate controls and nearest callers:
   `/tmp/quadratus-review-env/bin/python -m pytest -q tests/test_artifact_boundaries.py tests/test_memory.py tests/test_usage_and_channels.py`
   — 86 passed before the later legacy-provenance assertion, which also passed
   in the complete suite.
2. Complete package suite:
   `/tmp/quadratus-review-env/bin/python -m pytest -q`
   — **572 passed**, including the artifact regression and browser tests.
3. Required lint and patch checks:
   `/tmp/quadratus-review-env/bin/python -m ruff check quadratus tests --output-format concise`
   and `git diff --check` — both passed.

## Verification and remaining limits

`tests/test_project_workflow.py` drives real subprocesses with scripted vendor
envelopes. Agents modify actual temporary source trees, reviewers inspect fresh
copies and deliberately attempt relative writes, and real Python checks run
against the resulting project. It covers success, broken edits with a clean
unrelated launch directory, read-only operation, vendor failure, CLI routing,
restricted patches, Git initialization/branch creation, locking, task limits,
and unsafe patch rejection.

The GUI was exercised in Chromium with the same scripted transports: open a
project, enable edits, run, inspect the passing check and persisted source, and
download the diff. No browser console errors were recorded. Screenshot:
`output/playwright/project-workflow.png` (local QA artifact).

One complete live subscription run also passed on 2026-09-13 in a temporary
synthetic project. Fable reported an exhausted window; the existing seating
policy moved orchestration to Astra. Grok's restricted worker returned a patch
changing `return a - b` to `return a + b`. The harness applied it to the actual
selected project, the real Python check passed for positive and negative
inputs, and the orchestrator reported DONE. The saved `result.json` recorded
one task, source changes, the exact project cwd and a passing check. Source
remained after Fleet cleanup. Evidence is retained locally in
`output/project-proof/live-result.json` and `live-changes.diff`.

This is a small acceptance run, not a large-repository reliability benchmark.
Grok's added web-tool names were checked in the installed binary, but live
lookup behavior remains unverified. GitHub clone argument handling is
tested without cloning a private remote. No remote PR is opened. Check commands
need the project's own installed dependencies; automatic detection is only a
starting point. Source snapshots are capped at 100 MiB. Persistent records do
not yet provide automatic continuation of a stopped run; GUI operator questions
are reported as incomplete, while CLI sessions can answer them interactively.

### GameTape trial: senior Grok edit transport correction

The 2026-09-13 saved-filter trial exposed a separate path from the earlier
bounded-worker patch proof. Two senior Grok editing calls read the selected
project successfully, then cancelled on their first `search_replace` request.
The saved vendor trace records `permission_cancelled`; neither call changed
source, and Quadratus correctly persisted an incomplete result.

A synthetic file-edit reproduction showed that combining `--always-approve`
with `--permission-mode acceptEdits` cancels both prompt-file and argument
invocations. Removing the conflicting `acceptEdits` flag lets the same edit
complete. The corrected `GrokCLIProvider` was then exercised directly with its
normal prompt-file transport: the source changed and survived cleanup.

The fix retains existing model roles, bounded-worker restrictions, and Fleet's
per-call project/snapshot selection. Grok's senior calls already used
`--always-approve`; the extra mode was preventing an authorized edit rather
than providing filesystem isolation. Regression checks cover the cancellation
and verify that an editing view cannot redirect its owner's scratch directory.
Local trial evidence lives under `output/gametape-trial/`.
