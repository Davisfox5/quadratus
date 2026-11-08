# Fast feedback, with explicit coverage

## Default policy

| Change or event | Validation |
| --- | --- |
| Ordinary pull request | One Python 3.12 run of affected test files, plus contract smoke tests and lint. Later commits compare against the same PR's last successful validated source. |
| New commit on the same PR | Cancel obsolete CI for that PR. No duplicate branch-push run. |
| Unreferenced prose or delivery metadata | Lint; no pytest invocation. The plan records omitted tests. |
| Shared test configuration, dependency/CI changes, deleted or unmapped code | Full suite, split across at most eight isolated CI machines. |
| Default-branch integration, nightly or manual compatibility run | Full suite on Python 3.11 and 3.12, using the same isolated shards. |
| Local correction | Affected checks on the local interpreter; reuse still-applicable completed evidence. No automatic full suite or second interpreter per edit. |

Python 3.11 is retained because the package declares `requires-python >=3.11`.
It checks the oldest supported interpreter. Python 3.12 is the ordinary feedback
lane. Compatibility work no longer doubles every pull request's test workload.

The required aggregate is `test (3.12)`. It requires planning, lint, and **every**
selected shard to succeed. A documentation-only plan is visible as such; it is
not a claim that tests ran. Repositories with older required-status settings
must replace a required `test (3.11)` PR status with this aggregate.

## Local use

From the repository, with development dependencies installed:

```sh
python tools/ci/check.py --base LAST_VALIDATED_COMMIT --output /tmp/quadratus-check-UNIQUE
```

This includes staged, unstaged and new Python files, prints the selection, and
runs it once. The output directory must be new. `--full` explicitly requests a
full local regression. Lint remains `python -m ruff check .`.

Use the last applicable validated commit, not an arbitrary recent commit, as the
base. Inspect the plan's changed paths and selected/omitted files. If a known
runtime relationship is computed dynamically, include its tests explicitly or
use `--full`; do not pretend the static selector proves that relationship.

## How selection and parallel execution work

A separate job saves the source baseline only after the required aggregate has
succeeded. Failed validation cannot advance it. A later commit compares full
source fingerprints, including added/deleted files and base-branch changes.
Missing baseline cache falls back to the PR base; malformed baseline receipts
force a full run. This avoids revalidating a long-lived PR's entire accumulated
patch after every small correction. It reuses source-validation history rather
than caching or replaying a test result; the new selected checks still execute.

The planner parses source without importing it. It follows imports, relative
imports, test helpers and literal module/file references backwards to tests.
Segmented literal paths are conservatively matched by basename. Each changed
path must reach tests; one mapped edit cannot mask another unmapped edit.
Changed tests and the run-budget/prompt-parity smoke files are retained.
Non-Python contract inputs, unknown paths and shared configuration use the full
fallback. Documentation read by code or tests is treated as a contract input.

Files are indivisible shards: tests sharing module fixtures stay together.
Separate GitHub machines isolate cwd, monkeypatches, registries, sockets and
owned subprocesses. There are at most eight concurrent machines; two-version
compatibility runs may therefore execute in two waves. Local selection stays
serial to avoid introducing unverified shared-machine process interactions.

Longest-first packing uses previously measured file durations when available;
initial estimates use test counts and a lifecycle weight. Every successful CI
run saves fresh durations for subsequent packing. Dependency and browser
installation downloads are cached. **Test outcomes and application bytecode
are not cached.** Fresh-source gate checks and real OS/process/browser tests
remain intact.

## Timing and failure visibility

Each shard retains `intent.json`, `current.json`, `events.jsonl`, JUnit,
`durations.json` and `receipt.json`. The current record names the active test;
phase events distinguish setup, body and teardown. The final receipt records
interpreter, exact plan, source hashes, counts, skips/failures and elapsed time.
A source change before a run rejects the stale plan; a change during the run
makes the result fail. Abrupt termination leaves no successful final receipt.
Inherited `PYTEST_ADDOPTS` is rejected and configuration addopts cannot silently
filter the planned outer run. There are no automatic test retries and no empty-shard full-discovery fallback.

Timing artifacts are uploaded at job end, including on failure; they are not
live-streamed from GitHub. Local `current.json` is readable while running, and
GitHub's live job log shows progress and its current step.

## Existing evidence and limits

A saved 564-test correction regression took 263.71 seconds. Its phase timings
showed about 261.93 seconds in test bodies and only 0.48 seconds in fixture setup
and teardown. Optimizing shared fixture setup would miss the main expense.
The previous exact-head CI also ran both complete serial interpreter suites;
one Python 3.12 lane took 47 minutes 46 seconds.

The small test-harness changes disable unrelated plugin auto-discovery inside
synthetic project gates, and replace retry sleeps with assertions on recorded
backoff delays. Explicit attribution plugins and project conftest hooks still
load. Ten focused cases passed in 18.61 seconds. A historical comparison of the
seven lifecycle cases was 18.10 vs 21.47 seconds; this is not a controlled speed
benchmark or a claim about full-suite throughput.

Static impact selection cannot prove computed imports, generated paths, shell
consumers or all data dependencies. Full integration/compatibility runs preserve
broad coverage; a narrow green result is labelled focused. Large shared-engine
changes can still select most tests. Parallelism reduces elapsed time, not the
number of full-suite cases, and runner capacity/install time/file imbalance can
limit gains. Actual CI durations, rather than a promised speedup, are the measure.

## Applying this to the live-run loop

The current frozen live attempt keeps its existing inputs. Subsequent corrections
use this policy: focused local checks during edits, one settled affected regression
and the new CI aggregate. Add local Mac controls where Linux cannot cover the
changed process/privacy behavior. Reuse unchanged validation; do not wait for an
extra full local suite and both compatibility interpreters merely because an old
collector hard-coded those job names. Update each new attempt's collector to the
actual selected plan and retain its coverage label. Independent original-goal
assessment of each live attempt remains separate from engine test validation.
