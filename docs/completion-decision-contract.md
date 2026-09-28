# Completion decision: candidate and integration contract

## TL;DR

- `quadratus/completion_decision.py` is one function covering both DONE
  sites: the orchestrator's DONE reply, and the goal question at the task
  cap. It is a **candidate**. Nothing in the runtime imports it, and final
  DONE behaviour is unchanged.
- It is pure. The three effectful moves stay in the session: the goal
  question, the requirements review/audit, and the dependency check. The
  function names the one it needs next, and the session makes it and passes
  the answer back.
- It reproduces today's stop names, kinds and texts byte for byte. It keeps
  the goal-question order and the existing bounds (reopen allowance, task
  cap). Thirteen whole-controller replays check this against the real
  session.
- Where today's answer looks wrong, the decision says so in `divergences`
  and still returns today's answer. Changing behaviour is a separate,
  unauthorised step.
- Decision for the incumbent (Claude, `session.py` owner): wire it at both
  sites with the seam below once reviewed. The one prerequisite is none.
  The inputs it reads exist on 9eabf69.

Map row: P3.4 "DONE computed twice" (docs/workflow-map.md, section 12).

## Inputs (`CompletionInputs`)

| Field | Source today | Notes |
| --- | --- | --- |
| `site` | which branch of `_run_tasks` | `done_reply` or `cap` |
| `tasks` | `task_outcomes` | read only, never mutated |
| `run` | `run_outcome` | read only |
| `ledger` | `LedgerSnapshot.from_parts(config.requirements_ledger, memory.ledger.requirement_status, findings)` | taken **after** `_recheck_resolved_findings()`, and re-taken after a requirements check (the audit writes status; the review may add requirements) |
| `legacy_open_findings` | `open_findings` | legacy mirror; still decides today |
| `legacy_partial` | `_partial_tasks` | legacy mirror; typed partiality is derived from `tasks` |
| `max_tasks` | `run(max_tasks=)` | named in cap stops |
| `reopens`, `max_reopens` | `_requirement_reopens`, `config.max_requirement_reopens` | read, never extended |
| `goal_confirmed` | `_confirm_goal_met()` | cap only |
| `requirements_satisfied`, `done_refusal` | `_requirements_satisfied()`, `_done_refusal` | |
| `dependencies_verified` | `_verify_dependencies("at DONE")` returned | DONE reply only; a failure raises as today |

`snapshot_session(session, site=..., max_tasks=...)` builds these from a live
session without calling or changing anything. It is duck-typed, and the
module imports only `quadratus.outcome`. The findings lane's module is not
imported. `open_findings` stays the legacy list until that lane's typed
projection clears review. After that it is a one-field swap here.

## Output (`Decision`)

| Status | Meaning | Session applies |
| --- | --- | --- |
| `step` | one move is needed: `confirm_goal`, `check_requirements` or `verify_dependencies` | make it, rebuild inputs with the answer |
| `send_back` | DONE refused, allowance left | `_requirement_reopens += 1`; note `reopen_fact` (non-terminal `unverified`); set `_done_refusal = refusal` when non-empty; `continue` |
| `complete` | ready | `completed = True`; `done_accepted` |
| `incomplete` | named stop, repairable in a later run | `_stop_with(stop.kind, stop.reason)`; `completed = False` |
| `operator_stop` | named stop where the stop's kind, or the record's dominant active fact, is refusal/security/integrity/denial/operator/budget | same as `incomplete`; the status is information, and the stop text is unchanged |

The decision also carries these fields:

- `done_accepted`: the value to set, or `None` to leave it alone.
- `annotate` / `annotate_reason`: the `unresolved_reason` of each open finding.
- `partial_mismatches`: `(task_id, note)` pairs to append to
  `outcome.mismatches`, only where today's short-circuit evaluates partiality.
- `blockers`: the guard's blockers.
- `debt`: checks failing, merge gate, partial typed/legacy, ledger and legacy
  findings, unsatisfied edges by edge, owed refs, and ranked active constraints.
- `divergences`.

## Integration seam (for the incumbent; not done here)

At the DONE reply (`spec is None`) and in the cap's `else:` branch, replace
the legacy conjunction with:

```python
self._recheck_resolved_findings()
decision = resolve(
    snapshot_session(self, site=DONE_REPLY or CAP, max_tasks=max_tasks),
    confirm_goal=self._confirm_goal_met,
    check_requirements=lambda: RequirementsAnswer(self._requirements_satisfied(), self._done_refusal,
                                                  LedgerSnapshot.from_parts(...)),
    verify_dependencies=lambda: self._verify_dependencies("at DONE"),
)
# then apply the Decision per the table above; `send_back` -> continue, otherwise break/return
```

Three things to keep when wiring:

- `_findings_block_done` and `_requirements_satisfied` also write progress
  notes and `_done_refusal`. The session keeps those side effects. The
  decision never writes them.
- Today's DONE-reply findings path runs the re-check only when `findings` is
  non-empty. With no findings the re-check is a no-op, so calling it always is
  equivalent.
- At the cap, `_open_findings_for(None)` is read before the goal question,
  as now. The decision returns `FindingsUnresolved` with no step.

## Order and bounds preserved

- **DONE reply:**
  1. Open ledger findings send DONE back while `reopens < max_reopens`, or
     stop `FindingsUnresolved`. Requirements are not checked.
  2. Requirements are checked. A failure sends DONE back or stops
     `RequirementsUnmet`.
  3. Dependencies are verified.
  4. The legacy conjunction runs: legacy open findings, then partial
     (evaluated only when those are empty), ledger findings, standing checks.
  5. The result is `DoneWithOpenWork` or the guard. `done_accepted` is True
     from step 4 on, as today.
- **Cap:**
  1. Open ledger findings stop `FindingsUnresolved`, with no goal question.
  2. Partial work means no goal question.
  3. The goal question is asked **once**.
  4. Requirements are checked only after a confirmed goal.
  5. `done_accepted = completed` before the guard, so a guard refusal at the
     cap leaves it True, as today.
- `decide` raises `ValueError` on answers the order forbids (a goal answer
  over capped or audit debt, requirements before a confirmed goal,
  dependencies before requirements). A wiring that asks early fails loudly.

## Parity matrix

"Shadow" means a real replay through `tests/lifecycle/harness.py` with
read-only spies on the session (`run`, `_confirm_goal_met`,
`_requirements_satisfied`, `_findings_block_done`). The candidate is then
driven from the session's end state with the answers the session got. It
must ask the same questions and match `completed`, `done_accepted`,
`stop_reason`, and the stop's kind and name.

| Journey | Site | Today | Candidate | Evidence |
| --- | --- | --- | --- | --- |
| Clean J1 | DONE | complete | same | shadow `test_shadow_clean_done_reply` |
| Clean at cap | cap | complete after goal confirmed | same | shadow `test_shadow_clean_cap_confirmation` |
| Goal not confirmed | cap | `GoalUnconfirmedAtCap` (cap) | same | shadow + unit |
| Capped, never continued | DONE | `DoneWithOpenWork` (cap) | same | shadow + unit |
| Capped, never continued | cap | `GoalUnconfirmedAtCap`, goal not asked | same | shadow + unit |
| Capped, then CONTINUES | DONE | complete | same | shadow + unit |
| Unsatisfied checks edge | DONE | `CompletionUnproven` | same | shadow |
| Missing evidence / delivered / reviewer | both | `CompletionUnproven` | same | unit ×6 |
| Missing security verification | DONE | `CompletionUnproven` | same | unit |
| Integrity fact on a continued cap | DONE | `CompletionUnproven` (unverified) | same text; status `operator_stop` | shadow + unit |
| Requirements unmet past allowance | DONE | `RequirementsUnmet` | same | shadow (direct Session) + unit |
| Requirements unmet | cap | `RequirementsUnmet` (cap text) | same | shadow + unit |
| Open finding at cap | cap | `FindingsUnresolved`, annotated | same | shadow + unit |
| Finding reopened at DONE, allowance out | DONE | `FindingsUnresolved` | same | shadow + unit |
| Audit then repair, ledger on | DONE | complete | same | shadow |
| Standing failed check / repaired (G12) | DONE | `DoneWithOpenWork` / complete | same | unit |
| Merge gate failing | DONE | `DoneWithOpenWork` (product) | same | unit |
| Refusal / security / integrity with open work | DONE | `DoneWithOpenWork` of that kind | same; `operator_stop` | unit |
| Owed COVERS, unknown CONTINUES, broken record | DONE | `CompletionUnproven` | same | unit |

## Divergences reported, not acted on

1. **A typed-only partial is missing from the stop's reasons.** `_open_work`
   names the legacy `_partial_tasks`. Typed-only partiality still blocks, but
   the stop says "the record shows no single open item".
2. **A guard refusal is typed `unverified` while a higher fact dominates.**
   For example, an active `integrity` fact. The decision's status is
   `operator_stop`; the stop kind is today's.
3. **Run-level active terminal facts are not guard blockers.**
   `completion_blockers` reads tasks only. This is unreachable at DONE on
   today's paths, where every run-level writer also blocks through
   `open_findings` or the merge gate. It is reported if it is ever reached.

Two more behaviours are preserved without being flagged:

- The cap asks the goal question even when a check is failing or legacy open
  findings exist, and ends in `GoalUnconfirmedAtCap`. A model call over
  known-failing work is today's behaviour.
- With the ledger off, `_open_refs` still owes COVERS requirements.

## Evidence

Frozen image `sha256:707363c1…dc0d9`, source mounted read-only at `/pkg`,
`--network none`:

```bash
python -m pytest -q -p no:cacheprovider tests/test_completion_decision.py   # 65 passed
```

The module is new, so a red on the old base would only be a missing import,
and that is not claimed as evidence. The behavioural controls are these
mutations, each applied, observed red and reverted:

- Asking the goal question over capped debt reds
  `test_shadow_cap_with_a_capped_task_never_continued`.
- An off-by-one in the findings reopen bound reds the unit control and
  `test_shadow_a_finding_reopened_at_done_runs_out_the_allowance`.

## Not in this candidate

- No `session.py` / `outcome.py` change, and no runtime wiring.
- No model call, and no change to the budget or the bounds.
- No retirement of `open_findings` or `_partial_tasks`. Each stays an
  explicit legacy input until its own row clears.
