# Cloud reliability repair handoff

This directory and its Git branch replace the earlier Mac-only prompt and ZIP import steps. All required code and selected trial evidence are now tracked in this repository. Read `TASK.md` and implement the combined repair. Missing Mac paths or vendor CLIs are not blockers for cloud implementation and offline regression testing.

## Bring the cloud task branch up to date

From a clean cloud Quadratus checkout, preserve your existing task branch and fast-forward it:

```sh
git status --short
git fetch origin codex/project-workflow
git merge --ff-only origin/codex/project-workflow
git merge-base --is-ancestor 9ac8bac HEAD
REPAIR_BASE=$(git rev-parse HEAD)
```

If the cloud branch has its own edits or commits, preserve them. Do not force-reset; integrate normally or use a separate worktree based on the published branch. The historical code baseline is `9ac8bac`; the published HEAD additionally contains this handoff. Record the starting HEAD so the eventual returned patch contains only your repair commits.

## Inputs

Paths below are relative to this handoff directory:

- `TASK.md`: the coordinated implementation assignment, constraints and completion criteria.
- `evidence/routing-trial/FINDINGS.md`: read through the final outcome; it distinguishes confirmed failures from hypotheses that did not occur.
- `evidence/task-metadata-response.txt`: the actual prefaced KIND response that bypassed the testing pin.
- `evidence/worker-first-valid-response.txt`, `evidence/worker-second-rejected-response.txt`, and `evidence/routing-trial/rejected-worker.patch`: the real worker success/failure sequence.
- `evidence/BULK_TAGGING-first-applied.md`: exact target file state before the corrupt second patch. Copy it into a temporary repository for the regression.
- `evidence/routing-trial/review/native-delegation-evidence.json`: sanitized parent/child model and usage evidence.
- `evidence/routing-trial/usage-summary.json`, invocation event files and both natural-language goals.
- `gametape-f93d8b1.tar.gz`: GameTape's tracked source baseline, including saved filters and its 79-test suite. It contains no local application data or Git history.
- `MANIFEST.json`, `VALIDATION.json`, and `SHA256SUMS`: provenance, validation and integrity information.

The code baseline also includes `CLAUDE.md`, `docs/PROJECT_WORKFLOW.md`, `docs/GAMETAPE_TRIAL.md`, Astra routing and the Grok permission fix `022de34`. Nothing requires an `output/` folder. Historical `$LOCAL_REPOS`, `$LOCAL_CODEX`, `$LOCAL_TMP`, `/Users/...` and `/tmp/...` paths are evidence/provenance, not directories to recreate in Linux.

Verify this directory from the repository root:

```sh
(cd docs/handoffs/reliability-repair && sha256sum -c SHA256SUMS)
```

## Offline cloud checks

Use Python 3.11+ (local verification used 3.12). Create fresh environments:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

The imported code baseline passed 572 tests and Ruff in a fresh reconstruction on the Mac. Record actual Linux results and any platform differences. Transport tests mock the vendor CLIs; do not make live calls to compensate for missing binaries or credentials.

Extract the GameTape archive into a separate temporary directory outside this repo. Its top-level folder is `gametape/`. Create a separate environment there, install its `requirements.txt`, and run pytest. Its baseline passed 79 tests. Node can check `static/js/app.js` with `node --check`. This target is reference/test material, not a request to implement bulk tagging directly during the cloud repair.

The API backend is an explicitly selected supported transport, not an automatic fallback. Current defaults use CLI transport, and project sessions reject API transport. Preserve explicit API support and the no-silent-paid-fallback boundary.

## Completion boundary

Complete implementation, offline verification and local commits in the cloud. Return only the new repair commits and a concise evidence-backed handoff. Do not push, deploy, or invoke billed APIs without user authorization.

Live multi-provider acceptance is explicitly deferred to the Mac after the changes are imported. Return instructions for rerunning the original natural-language goal with normal routing; report actual model invocations separately from selected roles and overall completion. Lack of live CLI access does not justify leaving the cloud repair unimplemented.
