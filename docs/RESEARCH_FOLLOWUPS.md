# Research follow-ups: offline usage report and verifier probe

Two narrowly scoped, user-authorized side tasks. They do not touch the engine,
runtime, evaluation policy, roles or budgets, and they do not change the active
engine milestone. No live experiment, provider call or external call is made by
the tests or by the tools.

Evidence comes in two distinct kinds:

- Synthetic tests: the files under `tests/` build synthetic fixtures in
  temporary directories. They show behavior on constructed inputs only.
- Read-only saved-record analysis: the usage report may also be run against
  historical real saved run directories. That only reads existing
  `invocations.jsonl` and `result.json`; it writes nothing, launches nothing and
  makes no provider call. Its output describes those records, not the tests.

## 1. Offline usage report

    python -I tools/research_usage_report.py RUN_DIR [--json]

Standard library only. It reads `RUN_DIR/invocations.jsonl` and
`RUN_DIR/result.json`, imports nothing from `quadratus` and launches nothing.
Whole-run verification comes from `tools/truth_check.py`, loaded by file path
and run on the existing artifacts, including `changes.diff`. Truth-check and acceptance semantics are not
changed here.

What it reports:

- Usage per task, per role and globally, from events with `invoked: true`.
  Events that were selected but never invoked are counted separately and carry
  no tokens. `selected: false, invoked: false` rows are ignored.
- `invoked` must be a boolean. A row where it is absent or not a boolean is an
  input problem: the row is not used and the ledger is incomplete.
- Each event is a distinct invocation. Retries are never deduplicated.
- Token counts that are missing, negative, bool or not an int stay unknown;
  they are never read as zero or estimated. `known_total_tokens` is a lower
  bound. `total_tokens` is null unless the ledger is complete and every invoked
  event has known input and output.
- Ledger completeness: a missing or unreadable `invocations.jsonl`, or any
  malformed, non-object or schema-invalid line, sets `ledger_complete` to false.
  Then every total (global, per task, per role) is null, `tokens_complete` is
  false, and no total ratio is given. Known subtotals from valid rows are kept
  as lower bounds. `invalid_event_lines` counts the bad lines, and is null when
  the file could not be read at all, so an unknown count is never shown as zero.
- Cached input tokens are a subset of input and are never added again. Text
  output labels the cached figure as a KNOWN cached subtotal and shows
  `cached_unknown_events`.
- Completed tasks (as recorded, `closed_as == closed`) are shown apart from
  requirements. A verified requirement count exists only when the truth check
  verdict is `verified`; otherwise there is no denominator and the reason is
  given. Audited-met claims are shown separately, labelled as not whole-run
  verification.
- Known tokens per verified requirement is a lower bound, shown only when
  verified and known tokens are above zero; it may be given even when the ledger
  is incomplete. The total ratio is null when the ledger is incomplete or any
  token count is unknown.
- A missing or undecodable `result.json` keeps the usage and reports the
  unverified reason. A valid-JSON result with malformed containers (for example
  `requirements.audits` or `workflow.tasks` not a list, `listed` or `status`
  not an object, non-object audit verdicts) is reported as a problem, marked
  unverified, and gives no verified denominator; usage is preserved.

Limits: no prices or seeds, and nothing is a subscription cost. It is not a
full accounting of vendor child processes. Overhead attribution is reported as
`unavailable`, not estimated.

Exit status: 0 when inputs were readable and well formed; 3 when any input is
unreadable or malformed (the report and its problems are still printed).
Invalid token values are warnings, not input failures.

Tests: `tests/test_research_usage_report.py`, synthetic files only, including a
`python -I` run with a planted `quadratus.py` that must never be imported.

## 2. Verifier probe: valid alternatives

`tests/test_verifier_valid_alternatives.py` states a specification of a pure
`add(a, b)` over integers first (zero and negative cases included). The same
unittest assertions run in a subprocess (`python -I`) in a temporary directory
against:

- two structurally distinct valid implementations (direct `a + b`, and a
  step-by-step loop), and
- one deliberately defective variant (`abs(a) + abs(b)`).

Each run is bound to the sha256 of the exact candidate bytes executed. The test
then writes an honest `result.json` and `changes.diff` and asks the standalone
`tools/truth_check.py` to judge them. Both valid candidates must be `verified`;
the defective one must be `not-met`. A stale-source negative (the final source
differs from the bytes the check ran on) must be `unverified`.

Exact scope: this shows narrow acceptance behavior of the existing truth check
on executed, hash-bound evidence. The independent auditor verdict in each
receipt is a SYNTHETIC fixture. It does not measure live reviewer quality, and
no Session, provider or engine is invoked. The existing gate is not weakened.
