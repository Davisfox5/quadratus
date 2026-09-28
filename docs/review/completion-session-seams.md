# Completion decision: whole-Session seams (P3.4)

## TL;DR

- No reachable incorrect completion was found, so there is no strict xfail.
- On today's paths, an **active run-level fact cannot reach either DONE
  site**. The guard does not read run-level facts, and that gap is real, but
  only an injected state reaches it.
- **Typed-only partial debt is also unreachable at DONE.** The stop text that
  names no reason is pinned as a synthetic characterisation.
- New file: `tests/test_completion_decision_session_seams.py`, 9 tests. It
  adds per-round replay, reachability proofs and labelled synthetic cases.
  No engine, candidate or runtime change.

Base 064f930. Candidate `quadratus/completion_decision.py` is unchanged and
unwired.

## What is new relative to `tests/test_completion_decision.py`

The existing shadow cases replay the candidate once, from the end state. This
file adds a `rounds` spy that snapshots `snapshot_session` at **every** DONE
reply, after `_findings_block_done` (so after the findings re-check). Each
round is replayed with the answers the session got in that round:

- Every earlier round must be `send_back`. Its `refusal` must equal the
  session's own `_done_refusal` for a findings send-back, and must be empty
  for a requirements send-back.
- Its `reopen_fact` must equal the session's non-terminal note, in order.
- The last round must match `completed`, `done_accepted`, `stop_reason`, the
  stop kind and name, and the exact `partial:` mismatch notes the session
  wrote on its tasks.
- The steps must match what the session did in that round: the requirements
  check and the dependency check at DONE.

## Reachable (real paths, no injection)

| Test | Site | Shows |
| --- | --- | --- |
| `test_requirements_sent_back_once_then_met_complete_round_by_round` | DONE | send_back then complete. `reopens` goes 0 then 1 and is read, not reset. The non-terminal note neither blocks nor is reported |
| `test_a_reopened_finding_is_sent_back_each_round_before_any_requirements_check` | DONE | 3 send-backs with the session's refusal text, then `FindingsUnresolved`. No round reaches the requirements check |
| `test_a_repaired_merge_gate_reaches_done_only_as_a_recovered_run_level_fact` | DONE | The run-level product fact is present but recovered. The run completes with no divergence |
| `test_a_standing_merge_gate_failure_ends_the_run_before_the_done_reply` | — | The active run-level fact stops the run (`FindingsOpen`) and the DONE reply is never read |
| `test_a_repaired_merge_gate_on_the_last_slots_completes_at_the_cap` | cap | Slots exactly spent (1 serial + batch of 2). The goal is asked and the run completes with a recovered run-level fact |
| `test_a_standing_merge_gate_failure_on_the_last_slots_never_reaches_the_cap` | — | The batch check `break`s, so the cap's `else` never runs and the goal is not asked |

### Why run-level facts cannot reach DONE (session.py at 064f930)

Every writer of an active `run_outcome` fact falls into one of these cases:

- **`_stop_with`.** Every call site is followed by `break` or `return`.
- **`_open_finding` outside a task.** It also appends to `open_findings`.
  The loop stops right after the task (`if self.open_findings ...`) or the
  batch.
- **The merge gate (`_record_check_attempt`, target None).** A standing
  failure leaves `checks[-1]` failed, so the batch check stops the run. A
  repair marks the fact recovered (`attempt_facts`).
- **After the decision.** `run()`'s exception note and the end-of-run
  dependency check are both written after the decision has been made.
- **Send-back notes.** These are written with `terminal=False`.

The tests above pin the merge-gate case. The others are a code reading, cited
by line in the handoff, not separately tested.

## Synthetic (injected, labelled; not reachable today)

Each test injects its state when `next_task` returns DONE, before the DONE
site. Each pins today's engine answer beside the candidate's.

| Test | Engine today | Candidate |
| --- | --- | --- |
| `test_synthetic_an_active_run_level_fact_at_done_completes_today` | **completes**; `typed_completed` is False | complete, and names the fact in `divergences` |
| `test_synthetic_typed_only_partial_blocks_without_being_named` | `DoneWithOpenWork`, "the record shows no single open item"; mismatch note on t1 | same text, same mismatch, divergence names t1 |
| `test_synthetic_legacy_open_findings_short_circuit_partiality_at_done` | partiality never evaluated; no mismatch recorded | `partial_mismatches == ()` |

If a future writer makes a run-level fact reachable at DONE, the first
synthetic case becomes a real incorrect completion. That is the test to turn
into a strict xfail.

Why typed-only partial is unreachable: a cap fact is noted exactly where
`_partial_tasks.add` runs. A CONTINUES does both `discard` and `recover("cap")`.
A merge-stage fact always comes with an `open_findings` entry, which stops
the run at the batch check.

## Evidence

Frozen image `sha256:707363c1…dc0d9`, `--network none`, source read-only at
`/pkg`:

```bash
docker run --rm --network none -v "$PWD":/pkg:ro -w /pkg sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9 \
  python -m pytest -q -p no:cacheprovider tests/test_completion_decision_session_seams.py tests/test_completion_decision.py
# 74 passed (9 new + 65 existing)
```

The module is unchanged, so an old-base red is not applicable. These
mutation controls were applied in-process with `python -c`, which
monkeypatches the candidate and leaves no file edited:

- Forcing `reopens=0` in `_done_reply` reds
  `test_a_reopened_finding_is_sent_back_each_round_before_any_requirements_check`.
- `_open_work` returning `[]` reds
  `test_synthetic_legacy_open_findings_short_circuit_partiality_at_done`.

## Limitations

- `ruff` is not in the frozen image, so the new file was not linted there.
- No full suite was run; only these two files.
- The requirements send-back journey needs a new task to cover the missing
  requirement. An audit marking a requirement NOT MET un-covers it, and the
  next DONE is refused before any audit. That is observed behaviour, not
  judged here.
- The mutation controls cover two branches only.
