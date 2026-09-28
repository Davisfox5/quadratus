# Finding-projection seam audit: a7a9cd6 against 8529b2a and a07e7bf

Opus 5.5 bounded worker, read-only audit, 2026-09-27. Coordination log: #25.
Scope: cross-module compatibility only. Terminal-engine correctness is Sol's
review and is not repeated here. Nothing outside this file was changed.

## TL;DR

- **The merge is clean and green.** The candidate (`finding_state.py`,
  its tests, its contract doc) touches no file the incoming commits touch.
  On the merged tree, all 84 tests in the five affected files pass.
- **It is not yet correct together. Three things need fixing, all in the
  candidate:**
  1. **Full text is ignored.** 8529b2a puts the whole stop text on
     `Fact.full`. The candidate never reads it, so it still calls a long
     stop a "truncation gap". It should compare `full or detail` and
     require exact equality. The same fix applies to long open-finding
     texts, which today compare only their first 400 characters. That can
     hide a real difference.
  2. **One gap is already closed.** Since a07e7bf, an exception after the
     loop leaves a typed stop, and parity agrees. The candidate's contract
     doc (gap 2, precedence item 5) and one test name still describe the
     old behaviour. Fix the doc; keep the test as a legacy-record case.
  3. **Recovery G8 is unsettled, and it now touches three readers.** After a
     task's later check verifies, the legacy list drops that task's design
     debt. The incoming engine reader (`Session._design_debt`, which reads
     `evidence`) and the candidate reader (which reads `invalid_proof`
     facts) both keep it. Because the engine lets either side name the stop,
     the typed side can now name `DesignUnverified` for a task whose own
     latest check passed. Both typed readers need one rule: the latest
     outcome for a task id decides.
- **Decision for root:** apply the three candidate-side changes below before
  switching any reader to the projection. Candidate seam step 1 is replaced:
  ea7c9a2 already moved `_name_findings_stop` onto `evidence`, so the
  projection must *agree with* that reader rather than become it. One
  engine-side risk (a post-loop exception skips the ledger re-check) goes
  to Sol/root and is not patched here.

---

## Topology

```
9eabf69 ─┬─ 22eed61 ─ a7a9cd6                     (candidate: finding_state.py, tests, contract doc)
         └─ ea7c9a2 ─ 3075d6c ─ 3dca66d ─ 8529b2a ─ a07e7bf   (incoming: outcome.py, session.py, tests, map)
```

The two sides share no files, so there is no textual conflict. All the
compatibility questions are semantic.

## Offline evidence

The merged overlay is `git archive a07e7bf`, with a7a9cd6's three files laid
over it. It ran in the frozen image
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`,
with `--network none` and the source mounted read-only:

```
python -m pytest -q -p no:cacheprovider tests/test_finding_state.py tests/test_stop_full_text.py \
  tests/test_post_loop_exception.py tests/test_design_debt_typed.py tests/lifecycle/test_stop_fixtures.py
84 passed
```

A scratch probe (not committed) projected live records from the incoming
tests' own setups through the candidate:

| Probe | Record | Candidate result on the merged tree | Should be |
| --- | --- | --- | --- |
| A | `_unmet(40)`, a `RequirementsUnmet` stop of 505 chars | agree, **gap** "400-char prefix" (`Stop` has no `full`; the fact's `full` is 505 chars) | agree, no gap: `full == stop_reason` |
| B | long `LookupError` through `project_run`; the saved fact has a `full` key | agree, **gap** "313-char prefix" | agree, no gap: `full == result.error` |
| C | post-loop `OSError` (a07e7bf) | agree, no gap | same; before a07e7bf this was a stop problem |
| D | post-loop `KeyError` after `GoalUnconfirmedAtCap` | stop `KeyError`; the earlier stop stays an active `stop` item | same (G9 behaviour holds) |
| E | G8: t1 unverified, then t1 rechecked verified; legacy list cleared | engine `_design_debt({"t1"})` = `[("t1", …)]`, with a mismatch recorded on **both** outcomes; candidate `design_debt` = the active item; parity reports 2 problems | nothing: t1's own latest check verified |
| F | design problem > 400 chars | agree (both sides cut at 400) | agree, compared on the whole text |
| G | scope finding of 1018 chars | agree (both sides cut at 400) | agree, compared on the whole text; a difference after char 400 must disagree |

Probes F and G agree today only because both sides are cut to the same 400
characters. If two findings differ only after character 400, they read as
equal.

## 1. Full-or-detail equality (8529b2a)

**What changed upstream.** `Fact.full` is `None` unless `detail` was cut.
`TaskOutcome.note` and `RunOutcome.note` set it with `_cut(detail, 400)`.
`_note_exception` passes `full="<Class>: <exc>"`, and it is kept only when
it differs from the stored detail. `asdict` carries `full` into
`result.json` (probe B: the saved fact has the key).

**What the candidate does.** `project()` never reads `full`: `Stop` and
`Item` have only `detail`. `legacy_parity` accepts a bounded prefix as a
gap. `_legacy_key`, `_item_key` and `_design_detail` cut both sides to
`_FACT_LIMIT`.

**Minimal candidate change (contract, not a patch):**

1. Add `full: Optional[str] = None` to `Stop` and to `Item`. Fill it from
   `_get(fact, "full")`. Also record whether the source carried the field at
   all: `"full" in fact` for a dict, `hasattr` for a live `Fact`. Call that
   `has_full`.
2. Define `whole = full or detail`.
3. Stop parity:
   - If `has_full`: pass only when `text == whole`. Anything else is a
     **problem**, including a prefix at a bound. The bounded-prefix `gap` is
     never emitted for such a record.
   - If `not has_full` (a pre-8529b2a `result.json`): keep today's
     bounded-prefix rule (400, or `len(name) + 2 + 300`) as the only way to
     get a `gap`.
4. Finding keys: `_item_key` uses `whole`, and `_legacy_key` stops cutting
   when the matching item `has_full`. The `unmerged` regex rebuilds
   `not merged: <reason>` uncut. The simplest way is to cut the legacy text
   only when the typed side is a legacy record. A single boolean from
   `from_result` (any fact dict lacking `full`) is enough.
5. Design pairs: `_design_detail` stops cutting for `has_full` records, and
   the typed side uses `whole`.
6. `_EXCEPTION_MESSAGE_LIMIT`'s comment should cite `Session._note_exception`
   (session.py:2952 at a07e7bf), not `:2930`.

**Test recipe (each must fail on a7a9cd6 as merged):**

- `test_a_long_stop_with_full_text_agrees_without_a_gap`: probe A on the
  live session. Assert `gaps == []`, `agree` is true, and
  `state.stop.full == session.stop_reason`.
- `test_a_long_exception_from_result_agrees_without_a_gap`: probe B through
  `from_result`. Assert `gaps == []`.
- `test_full_text_that_diverges_after_the_bound_is_a_disagreement`: a
  synthetic run fact with `detail = X[:400]`, `full = X`, legacy `X[:400] +
  "…different"`. Expect a problem, never a gap. This is the masking case.
- `test_a_legacy_record_without_full_keeps_the_bounded_gap`: a dict fact
  with no `full` key and a 400-char prefix gives a gap, as today. The four
  existing bound tests move here unchanged.
- `test_a_long_finding_text_is_compared_whole`: probe G, with the legacy
  text changed after char 400. Expect a problem.
- `test_a_long_design_problem_is_compared_whole`: probe F, with the legacy
  problem changed after char 400. Expect a design-pair problem.

**Legacy truncation rules to leave alone.** These are the engine's own text
cuts. They are not `Fact` bounds, and the projection must not "correct"
them:

- `DesignUnverified` cuts `problem[:400]` inside `stop_reason`. Its `full`
  equals `stop_reason`, so equality holds.
- `CompletionUnproven` cuts `blockers[:600]`.
- `_annotate_open_findings(stop_reason[:240])` and the in-loop
  `str(exc)[:200]` are ledger annotation text. The candidate reads them
  only as `notes`, never compares them.
- The ledger `message[:300]` (session.py:4379) is ledger data. The
  candidate's `[:_FACT_LIMIT]` on it is a no-op.

## 2. Post-loop exception (a07e7bf)

**Compatible.** Post-loop exceptions now go through the same
`_note_exception`: same kind (`classify`), `legacy = Class`, detail
`<Class>: <msg[:300]>`, `full` when cut. The candidate's "last active run
fact with a legacy name is the stop" rule picks it up. Probes C and D:
parity agrees, and an earlier named stop stays an active `stop` item (G9).
The end-of-run `DependencyTreeChanged` route in `_finish_run` is caught
internally and not re-raised, so it is not double-typed.

**Candidate changes (doc and test naming only):**

- `docs/finding-state-contract.md`: remove gap 2. Rewrite precedence item 5
  as "a post-loop exception is typed like a loop exception (a07e7bf); the
  run is not complete". Unblock seam step 6 from gap 2. It is still blocked
  on gap 1 until §1 lands.
- `test_an_untyped_exception_after_the_loop_is_a_stop_disagreement` still
  passes, because it is a synthetic record. It now describes a record only a
  pre-a07e7bf `result.json` can hold. Rename it to a legacy-record case, and
  add `test_a_post_loop_exception_is_the_typed_stop`: probe C through
  `from_result`, `agree` and `stop == "OSError"`.
- Add probe D as `test_a_post_loop_exception_after_a_named_stop_keeps_both`:
  stop `KeyError`, with `GoalUnconfirmedAtCap`'s fact in `active` under
  category `stop`.

**Engine-side risk, for Sol/root. Not patched; it only affects the
projection's inputs.** The loop-exception path runs
`_recheck_resolved_findings()` and falls back to `_distrust_resolutions()`.
The post-loop path does not. If `_verify_dependencies` raises a
non-dependency exception, or `_recheck_resolved_findings` itself raises,
then `_annotate_open_findings` never runs and nothing is distrusted. The
candidate treats the ledger as the authority and moves `resolved` rows to
history. A resolution that a later task undid can therefore project as
resolved after a post-loop exception. The contract should say this, and the
recipe below pins it once the engine chooses a fix:
`test_a_post_loop_exception_leaves_no_unrechecked_resolution` resolves F1
in t1, has t2 undo it, and raises `OSError` at `"at the end of the run"`.
F1 must not project as resolved without a distrust note.

## 3. Recovery G8: one design-debt rule for three readers

**The readers after merge:**

| Reader | Source | After a verified recheck of the same task id |
| --- | --- | --- |
| legacy `_design_unverified` | appended at 3280 and 3359; cleared at 3355 on `ok`; extended at 3606 | **cleared** |
| engine `Session._design_debt` (ea7c9a2 and 3075d6c; names the stop) | every `TaskOutcome.evidence` with `verified is False` and no `findings` | **kept**: the earlier outcome's evidence still says `False` |
| candidate `FindingState.design_debt` | active `invalid_proof` facts at stage `design` | **kept**: nothing recovers the fact |

`_name_findings_stop` returns `typed or legacy`. So after merge, the typed
side alone names `DesignUnverified` for a task whose own latest check
passed (probe E). The disagreement is also written to every outcome
sharing the task id, which puts it on the record twice. The candidate's
existing test
`test_design_debt_the_legacy_list_cleared_but_the_fact_keeps_is_reported`
pins its half of the same split. The candidate's seam step 1 ("decide the G8
clear at 3339 first") is where this was deferred. The incoming engine
switch made that decision implicitly, in the direction that contradicts the
code comment at 3353 ("Verified renders for this task discharge its own
earlier design debt").

**Contract. The same rule must hold in both typed readers:**

> For each task id, only the **last** `TaskOutcome` with a design
> `evidence` record decides design debt. If that record is
> `verified is False` and carries no `findings`, the task owes design debt,
> with that record's `problem`. Otherwise it owes none, and the earlier
> outcomes' `invalid_proof` facts move to `history` with the note
> "discharged by a verified recheck (G8)". A missing record is never
> verified (fail closed). A `verified is None` (disabled) record discharges
> nothing and adds nothing.

Candidate-side minimal change: in `project()`, compute the last design
`evidence` per task id. Give an `invalid_proof`/`design` item whose task's
latest evidence is verified the state `recovered`, plus that note. Do not
delete it; facts are history. `design_debt()` then needs no change.
`legacy_parity`'s design pairs become `(task, latest problem)` per task.
This matches both the legacy list after a 3355 clear and the
`typed_last`/`legacy_last` comparison 3075d6c uses. The engine's
`_design_debt` needs the same "last evidence per task id" filter. That is
an engine edit, and it is listed for root and Sol, not made here.

**Test recipe:**

- `test_g8_a_verified_recheck_discharges_the_earlier_outcome`: probe E. The
  candidate's `design_debt({"t1"}) == []`, the item is in `history` with the
  G8 note, and parity has no problems.
- `test_g8_an_unverified_recheck_keeps_the_latest_problem`: t1 fails with
  "a", then fails with "b". Exactly one debt, with problem "b". The legacy
  list `[("t1","a"),("t1","b")]` agrees (the 3075d6c rule).
- `test_g8_another_tasks_verified_check_discharges_nothing`: t1 unverified,
  t2 verified. t1's debt stays (G8 binding).
- `test_g8_audit_debt_is_not_discharged_as_design`: t1's evidence carries
  `findings`. The ledger item is unaffected, and there is no design item.
- Cross-reader agreement, run on every design journey in
  `tests/lifecycle/test_stop_fixtures.py` (`DesignUnverified:serial`,
  `DesignUnverified:parallel-child`) and on
  `test_design_debt_typed.test_a_parallel_childs_design_debt_agrees_in_both`.
  Assert `{(i.task, problem_of(i)) for i in state.design_debt(stopping)} ==
  set(session._design_debt(stopping))`. For `problem_of`, prefer the
  evidence record's `problem` over parsing `detail`. That means `Item` needs
  a `refs` or `problem` slot for design items; the fact text is lossy.

## 4. Candidate seam steps, updated for the incoming commits

| Candidate step | Status after merge | Change |
| --- | --- | --- |
| 1 `_name_findings_stop` `own` becomes `state.design_debt` | **Superseded.** ea7c9a2 already reads `evidence` | Replace with "prove `state.design_debt(stopping)` equals `_design_debt(stopping)` on every replay after §3". `not self.stop_reason` becomes `state.stop is None`, unchanged |
| 2 to 5 | Unaffected by 8529b2a and a07e7bf | Only the §1 key change applies to step 4's "first text" |
| 6 `project_run` / `run()` read `state.stop` | Gap 2 closed. Gap 1 closes when §1 lands | The byte-identical control compares `result.error` with `state.stop.full or state.stop.detail` |
| 7 retire `_design_unverified` | Blocked on §3 | Retire it only after both typed readers use the latest-evidence rule |

**Fixture corpus (3dca66d).** `stop_fixtures.json` records `stop_fact` as
only `{kind, legacy, detail}`, and its docstring still says "that gap is
open". Once §1 lands, the fixture writer should add `full`, and the
docstring should drop the sentence. That belongs to the fixture lane
(`QUADRATUS_WRITE_STOP_FIXTURES=1`), not the candidate. For each fixture,
the candidate can then assert: `result_error == (stop_fact.full or
stop_fact.detail)` and `legacy_error_class(result_error) ==
stop_fact.legacy`. It is a stop-only corpus with no task facts, so the full
projection must still be checked on the live journeys (`from_session`).

## Handoff (immutable once pushed)

- Branch `opus/projection-seam-audit`, based on candidate `a7a9cd6`. It adds
  only this file.
- No engine, test, candidate or map file was modified. No vendor call. No
  profile, authority or budget change.
- Blocked in this session, not performed: posting the claim, progress and
  handoff comments on #25 (`gh pr comment` needed approval, and there was no
  approval surface). Root should relay this file's TL;DR to #25.
- Next owner: the candidate author for §1–§3 (the candidate side), and
  Sol/root for the two engine-side items (the §2 post-loop re-check and the
  §3 `Session._design_debt` latest-evidence filter).
