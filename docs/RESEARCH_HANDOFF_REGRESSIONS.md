# Research handoff regressions (offline, saved-record)

## Purpose
`tests/test_research_handoff_pairs.py` runs one fixed acceptance checklist (R1-R3)
unchanged against two states of `tools/research_usage_report.py`: one archived
Git-history source (`early_report`) and the current production source. This is one
actual historical regression pair. It is not two real bugs and not a synthetic
mutant. Inputs are small SYNTHETIC saved run records (`result.json`,
`invocations.jsonl`, `changes.diff`).

## Why a saved-record tool, not the PR73 engine
The report tool reads files only, so the same input bytes can be replayed against
old and new code with no provider, engine, git or network. Touching active PR73
work would add confounds. Runtime, existing tests, truth checker, policy and
budgets are untouched.

## Acceptance (fixed, request-derived)
- R1: valid completed result plus one good row (100/20) plus a malformed JSON line:
  known subtotal 120 kept, no complete total, no total-token-per-verified-requirement,
  nonzero exit, parse problem exposed.
- R2: a task with `closed_as=closed` counts 1, but without an independent audit it
  gives no verified denominator.
- R3: requirement listed/met with audit `[{auditor: null, result: 'no auditor available'}]`
  is incomplete evidence, not corruption: total 7 kept, exit 0, verdict unverified,
  no denominator or ratio.

The rubric was not changed or relaxed. A simple required-ID assertion rejects a
checklist with a missing or substituted ID.

## Provenance
The fixture lives in `tests/fixtures/research_handoff/`, copied from Git history:
- early_report: commit 9dca3b10e21ca96566b7a60cf6431b2a01d1ab0c
  (sha256 `5c5fb6d0...839a2c`)

A second candidate from commit cfb7eb351d315042d58a4b2378886fc96e1ddc42 was
discarded: Git byte verification showed its source is byte-identical to
`early_report`, so it was a metadata-only duplicate and not a distinct predecessor.
Its fixture file and provenance entry were removed.

Selection of the archived predecessor rests on Git byte verification and on the
executed baseline, not on silently relaxing the rubric. Executing the unchanged
R1-R3 checks showed the archived source already includes the R1/R2 fixes and still
has the no-auditor misclassification bug (an audit without an auditor is flagged
as malformed/corrupt instead of incomplete), so it fails exactly R3.

## Integrity check
A small helper, `validate_archive(name, data)`, asserts the pinned sha256 with a
clear message. It is independent of production code and is used when the archive is
loaded, in the fixture hash/provenance test, and in the tamper test. The tamper test
confirms pristine bytes pass and that appended, bit-flipped and truncated bytes (and
an unknown archive name) make it raise under `pytest.raises`.

## Expected failure sets (observed by execution)
- early_report: {R3}
- current: {} (none)

## Negative controls
The expected R3 failure for the archived source is asserted as an exact set, never
skipped or xfailed. Missing-auditor input is a separate case from the clean
positive control, which runs against both sources, so a synthetic "handoff marked
complete" note cannot hide the residual failure. That note is explicitly SYNTHETIC
and says nothing about any real author.

## Run
`python -m pytest tests/test_research_handoff_pairs.py -q`
Each candidate runs via `sys.executable -I` with the current `truth_check.py`
copied beside it, a 60 s timeout, no shell, and bounded stderr.

## What this does and does not establish
It shows that fixed criteria separate an archived implementation state from the
current one on identical saved bytes. Its truth-checker inputs are synthetic
receipts and audits, not actual verified product performance. It does not show
live-agent improvement, engine recovery, or real continuation behavior.
