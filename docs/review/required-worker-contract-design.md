# J30 required-worker contract: design proposal

Base: `31aa595463d45c6071501653104498cf2e236649`. Proposal and tests only. Nothing under `quadratus/` changes, and no required-worker semantics are active.

## TL;DR

- Today every worker errand is optional. A failed errand goes back to the lead as text, and the task can still complete. That stays true for every errand that isn't declared.
- Proposal: the orchestrator may add one line to a task, `REQUIRES-WORKER: check`. It is parsed next to `COVERS` and `CONTINUES`, frozen into the task contract at dispatch, and becomes a mandatory edge `worker:check`.
- The edge is true only when an errand of that kind returned a result. A failure, a refusal, a closed channel, or the lead doing the work itself leaves it unsatisfied.
- Completion refuses an unsatisfied edge through the guard that already exists. The stop is `CompletionUnproven`, kind `unverified`. No new exception, no new stop kind.
- No budget, retry or failure allowance widens. `RepeatedFailure`, `max_worker_failures` (4) and `WorkerBudget.max_per_task` (12) apply exactly as they do to optional errands.
- Tests: `tests/test_required_worker_contract.py`, 4 passing controls that pin today's optional behaviour and 6 strict xfails that encode the proposal through whole-run replays.
- Open questions for the operator are at the end. The biggest two: whether a CONTINUES task may discharge a failed required errand, and whether adding a contract field (which changes every contract's digest) is acceptable.

## Where things stand

- `quadratus/contract.py:31-46` `Required` has six fields (`checks`, `design_evidence`, `design_review`, `security_verification`, `settlement`, `design_collaboration_applicable`). None is about workers.
- `quadratus/outcome.py:195-212` `TaskOutcome.unsatisfied()` and `quadratus/outcome.py:357-370` `_required_edges()` build the mandatory edge list from those fields only. The two lists are written out twice and must stay in step.
- `quadratus/session.py:1588-1717` `_serve_worker` turns every declared worker failure into text for the lead: refused (`FanOutExceeded`, `RepeatedFailure`, `ErrandToolMismatch`) at 1667-1682, failed at 1683-1699, and a per-result error at 1707-1716. Only an unclassified exception is re-raised (1690, 1700-1706, map J27). No edge is written anywhere on that path.
- `quadratus/session.py:1486-1546` `_tool_worker` (the in-session tool) calls the same `_serve_worker` with `answer_only=True`, so one change point covers both channels.
- `docs/workflow-map.md:298` lists J30 as "optional recorded; required fails its edge", and `docs/workflow-map.md:475` records that nothing declares an errand required today.
- The optional half is proven on `codex/j30-worker-journey` (`225f46f`, `tests/test_workflow_worker_failure_journey.py`), which is not on this base. `test_control_optional_failure_completes_with_no_worker_edge` reproduces it here so this file stands alone.

## 1. Declaration

Syntax: one line in the orchestrator's task block.

```
REQUIRES-WORKER: check
REQUIRES-WORKER: check, read
```

Tokens are errand names, split on commas and whitespace, de-duplicated, order kept. Each must be a key of `WORKER_TREE` (`quadratus/workers.py:150`). One line per task, like `RESOLVES`.

Where it is parsed: a new `_read_required_errands(spec)` next to `_read_continues` (`quadratus/session.py:725-733`) and `_read_covers` (`quadratus/session.py:669-677`), called in the serial loop right after them (`quadratus/session.py:3106-3107`) and in the parallel loop at the same point (`quadratus/session.py:3624-3625`). It strips the line from the description, the same way `_read_continues` does, so the lead never sees the raw directive.

A bad line (unknown errand, empty, repeated on two lines) is sent back before any lead call, on the same allowance as `COVERS`/`RESOLVES` (`quadratus/session.py:3108-3129`, `max_requirement_reopens`). It never falls back to optional: a declaration the harness cannot honour is the orchestrator's to fix.

The lead is told in its prompt which errands are required and that doing the work itself will not satisfy them. That is prompt text only; the edge is decided from facts, never from the lead's reply.

## 2. Binding at dispatch

- New field: `Required.worker_errands: Tuple[str, ...] = ()` in `quadratus/contract.py:31-46`.
- Filled in `_build_contract` (`quadratus/session.py:2061-2115`, the `Required(...)` call at 2074-2080) from the value the parser stored on the session for the current task, the same way `outcome.resolves` feeds `settlement` at 2079.
- Frozen by `_bind_contract` (`quadratus/session.py:2117-2125`) with the owner, before any model call. A changed list is a new task, as the module docstring (`quadratus/contract.py:7-8`) already says for scope and intent.
- `stages_for` (`quadratus/contract.py:94-107`) is unchanged. A required errand is served inside the draft stage, not a stage of its own.

Side effect to accept or refuse (open question 4): `TaskContract.digest` (`quadratus/contract.py:76-78`) hashes `asdict(self)`, so a new field with a default still changes every contract's digest, including tasks that declare nothing.

## 3. Recording the result as a typed edge

One edge per declared errand, named `worker:<errand>` (for example `worker:check`), written on the task's `TaskOutcome` with the existing `edge()` (`quadratus/outcome.py:168-169`).

Written in `_serve_worker` only when `request['errand']` is in the current contract's `worker_errands`:

| Path in `_serve_worker` | Line | Edge |
|---|---|---|
| Result with no `error` and no `needs_tool` | 1707-1714 | `True` |
| Result with `error` or `needs_tool` | 1707-1712 | `False`, unless already `True` |
| Refused: budget, repeat, tool mismatch | 1667-1682 | `False`, unless already `True` |
| Failed and produced nothing | 1683-1699 | `False`, unless already `True` |
| Channel already closed | 1597-1598 | `False`, unless already `True` |
| Unclassified exception | 1690, 1700-1706 | not written; the run stops as J27 today |
| Never requested | none | absent, which `unsatisfied()` already treats as unmet |

`True` is sticky for the task: a later failed attempt of the same errand does not undo a success. For a helper pair (`commission_many`, 1651-1664), each result counts for its own errand name.

Nothing is written for an errand that was not declared. That is what keeps the optional path byte-identical in the typed record (control: `test_control_optional_failure_completes_with_no_worker_edge`).

`unsatisfied()` and `_required_edges()` each gain `wanted += [f"worker:{e}" for e in required.get("worker_errands") or ()]`. Given the duplication, the implementer may want to make `unsatisfied()` call `_required_edges()`; that is a refactor, not part of the contract.

## 4. Completion rejects the unsatisfied edge

No new decision code. `completion_blockers` (`quadratus/outcome.py:373-449`) already iterates `task.unsatisfied()` at 443 and appends `f"{tid}.{edge} unsatisfied"` at 448. `_guard` (`quadratus/completion_decision.py:427-445`) turns any blocker into:

- stop name: `CompletionUnproven`
- stop kind: `unverified`
- reason: `CompletionUnproven: DONE was accepted but the record does not support it: t1.worker:check unsatisfied. Work preserved.`

The lead's own edits are kept (the stop says "Work preserved", and the xfail checks `app.py`).

Discharge rules, proposed:

- Audit debt never discharges a worker edge. `_AUDIT_EDGES` (`quadratus/outcome.py:283`) stays design-only.
- CONTINUES may discharge it under the existing rule at `quadratus/outcome.py:419-433`: a later task in the chain, closed clean, whose own contract also requires `worker:check` and whose edge is `True`. `worker:*` is not added to `_STATE_EDGES` (285). This is open question 1.

Why no earlier stop: failing the task at the moment the errand fails would cut off the lead's recovery options in `_WORKER_RECOVERY` (`quadratus/session.py:5324-5332`) and the orchestrator's option to name a CONTINUES task. The edge holds the obligation until DONE, which is how `checks` already works.

## 5. Retry and budget: nothing widens

- `RepeatedFailure` (`quadratus/workers.py:134-144`, raised at 529-536): the fingerprint is task, model, write flag and prompt hash. Unchanged. An identical re-send of a required errand is refused like any other (control: `test_control_identical_retry_is_still_refused`).
- `max_worker_failures = 4` (`quadratus/session.py:383`) and `_close_workers` (1564-1577): unchanged. A required errand that fails four times closes the channel, and the edge stays `False` (xfail: `test_required_errand_does_not_widen_the_failure_allowance`; today's code already stops at exactly 4 worker calls in that script).
- `_request_after_close` (1579-1586) still stalls the run on the second request after close. No exemption for a required errand.
- `WorkerBudget.max_per_task = 12`, `max_concurrent = 4` (`quadratus/workers.py:380-381`): unchanged.
- `lead_max_turns` and the run limits: unchanged. A required errand buys no extra lead rounds.
- What satisfies the edge: any successful result of the declared errand in the task. A rewritten instruction or `demanding: true` (in-family bump) both keep the errand name and can satisfy it. Rerouting to a different errand name cannot (open question 2).

## 6. What stays the same for optional errands

- `_serve_worker` output text, `_WORKER_RECOVERY`, ledger rows, failure counting and channel closing: unchanged.
- No `worker:*` edge, no `unsatisfied` entry, no completion blocker.
- The unclassified-exception stop (J27) is unchanged for both kinds.
- A task with an empty `worker_errands` has the same `edges`, `unsatisfied` and completion as today. Only its contract digest changes (section 2).

## Tests in `tests/test_required_worker_contract.py`

Controls (pass today):

- `test_control_required_has_no_worker_field_today`: pins the six `Required` fields.
- `test_control_optional_failure_completes_with_no_worker_edge`: whole run, errand fails, lead fixes, DONE completes, no `worker*` edge.
- `test_control_declaration_line_is_inert_today`: the `REQUIRES-WORKER` line is ignored today and the run completes. This is the pin the xfails reverse; it gets deleted or inverted with the implementation.
- `test_control_identical_retry_is_still_refused`: one provider call for two identical requests; the second is refused.

Strict xfails (`raises=AssertionError`), each confirmed with `--runxfail` to fail at its own assertion on 31aa595:

- `test_required_errand_is_bound_into_the_contract_at_dispatch`: `required.worker_errands == ["check"]`, line stripped from the lead prompt.
- `test_required_errand_success_satisfies_its_edge`: `worker:check is True`, run completes.
- `test_required_errand_failure_records_a_false_edge`: `worker:check is False`, `unsatisfied == ["worker:check"]`, lead's work kept.
- `test_completion_refuses_an_unsatisfied_required_errand`: `CompletionUnproven`, blocker text, stop kind `unverified`.
- `test_required_errand_never_attempted_is_unsatisfied`: lead ignores the declaration, run incomplete.
- `test_required_errand_does_not_widen_the_failure_allowance`: exactly 4 worker calls, edge `False`, incomplete.

## Open questions for the operator

1. **CONTINUES discharge.** Should a later CONTINUES task that runs the same errand successfully discharge the failed one (the proposal), or must the original task satisfy it?
2. **Rerouting.** `_WORKER_RECOVERY` lets the lead send the instruction to a different worker. The worker is picked by errand name, so rerouting means a different errand. Should a declaration accept a set of equivalent errands (for example `check|read`), or is the name exact?
3. **Write errands.** A required write errand only runs after the lead's reply ends (`quadratus/session.py:1516-1519`). Allow `REQUIRES-WORKER` on write errands, or read-only only?
4. **Digest change.** Adding `worker_errands` changes every contract digest even when empty. Accept, or exclude an empty tuple from `asdict` for the digest?
5. **Security excursions.** `_run_security_task` serves WORKER through `_draft_with_channels(consults=False)` (`quadratus/session.py:1719-1757`). Allow required errands there, or refuse the line on a security task?
6. **Who may declare.** Only the orchestrator's task block (the proposal), or also the operator from `--session` configuration?
7. **Parallel batches.** Each child has its own contract and edge. Should a merged child's failed required errand block the batch's merge gate, or only DONE as proposed?
8. **What "returned a result" means.** The proposal counts any result without `error` or `needs_tool`. Should a required `check` errand also need a parseable pass/fail verdict in its answer? That would be a content check, which nothing does today.
