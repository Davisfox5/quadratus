# Stop fixtures after `Fact.full` and post-loop exceptions (P3.4)

Opus 5.5, 2026-09-28. A fixture-compatibility review, not an engine review:
`8529b2a` and `a07e7bf` were cleared on source (Sol 5862595888) and are not
re-reviewed here.

## TL;DR

- **The 20 recorded stops do not change**, byte for byte, at `8529b2a`,
  `a07e7bf` or the incumbent tip `7d294ce`. The fixture still passes, and
  that is the problem: it passes because it cannot see either change.
- **It cannot see `Fact.full`.** `_record` projects the stop fact as
  `kind`, `legacy`, `detail` only. Every recorded stop is under the
  400-character cut anyway (longest detail: 323), so `full` would be `null`
  in all 18 non-null facts even if it were projected.
- **It cannot see a post-loop exception.** None of the 18 journeys raises
  after the task loop, and `_record` keeps only `run_outcome.stop()` (the
  last stop), so the case where the typed stop and `stop_reason` disagree
  can never appear in it.
- **One claim is now false:** the test docstring says the 400-character
  gap "is open". `8529b2a` closed it. The workflow-map entry 10 claim that
  exception stops leave `stop_reason` empty is also incomplete after
  `a07e7bf`.
- **Proposal:** add `full` to the projection, add a `stops` list, add four
  journeys that already exist as tests (two long-text, two post-loop), and
  add four derived assertions so a regenerated fixture cannot quietly bless
  a disagreement. Test-only; no engine change.

## Refs read

| Ref | What it is |
| --- | --- |
| `3dca66daf440bfa66a173b71453f49ee88d6ed48` | Fixtures: `tests/lifecycle/test_stop_fixtures.py`, `stop_fixtures.json` |
| `8529b2a60e5ff21a2b9b2d12f121463445f5f29f` | `Fact.full`; `run()`'s exception fact passes its whole text |
| `a07e7bffcac15307078b71e90f0809bdcff5c085` | `_finish_run` wrapped; `_note_exception` shared; `completed = False` |
| `7d294ce91920adcf31bed204ae865fd93e34815c` | Incumbent tip; map entry 10 corrected (5862594012). Parent `bbb4956` (E2) |

`git diff 3dca66d 7d294ce -- tests/lifecycle/` touches only
`test_replaced_evidence.py`: the fixture file and its test are unchanged
through the tip. The full suite on `bbb4956` (5862593194: 2,359 passed, 3
known environment failures) includes `test_stop_fixtures.py`, so the 20
records hold on a descendant of both seams.

## Why no recorded value moves

**`8529b2a`.** `Fact.full` is set only when the text exceeds its bound
(`_cut(detail, 400)`; an exception passes its whole text and keeps it only
when `detail` differs). Lengths in `stop_fixtures.json` at `3dca66d`:

| Journey | `stop_reason` | `result_error` | `detail` |
| --- | ---: | ---: | ---: |
| CheckFailing | 137 | 137 | 137 |
| CompletionUnproven:cap | 135 | 135 | 135 |
| CompletionUnproven:done [0] | 0 | 0 (`""`) | no fact |
| CompletionUnproven:done [1] | 112 | 112 | 112 |
| DependencyTreeChanged | 0 | 202 | 202 |
| DesignUnverified:parallel-child | 185 | `null` | 185 |
| DesignUnverified:serial | 252 | 252 | 252 |
| DoneWithOpenWork:capped | 116 | 116 | 116 |
| FindingsOpen | 162 | 162 | 162 |
| FindingsUnresolved:cap | 95 | 95 | 95 |
| FindingsUnresolved:resolves | 101 | 101 | 101 |
| GoalUnconfirmedAtCap:capped | 118 | 118 | 118 |
| GoalUnconfirmedAtCap:not-confirmed | 117 | 117 | 117 |
| PlanDeclined | 108 | `null` | 108 |
| RequirementsUnmet:done | 323 | `null` | 323 |
| RunStalled:no-requirements [0] | 0 | `null` | 205 |
| RunStalled:no-requirements [1] | 275 | `null` | 275 |
| TurnLimitBreaker:batch | 208 | `null` | 208 |
| TurnLimitBreaker:serial | 130 | 130 | 130 |
| clean | 0 | 0 (`""`) | no fact |

All under 400, and both exception details (`DependencyTreeChanged`,
`RunStalled`) have messages under 300. So `full` is `None` everywhere.

**`a07e7bf`.** Every journey either finishes the loop normally, where
`_finish_run` runs the same statements it ran inline before, or raises from
the loop, where the path is unchanged apart from calling `_note_exception`
(same kind, legacy, detail, full). No journey injects a failure into
`_verify_dependencies("at the end of the run")` or
`_annotate_open_findings` after the loop.

## Outdated claims to drop

1. **`tests/lifecycle/test_stop_fixtures.py` docstring (3dca66d, lines 9-10):**
   "`stop_fact.detail` is truncated at 400 characters and `stop_reason` is
   not; the fixture keeps both, since that gap is open." Replace with:
   "`stop_fact.detail` is cut at 400 characters (300 for an exception's
   message); `stop_fact.full` holds the whole text only when it was cut."
2. **`docs/workflow-map.md` entry 10 (7d294ce):** "Exception stops
   (`RunStalled`, `DependencyTreeChanged`) leave `stop_reason` empty." True
   for an exception from the loop. After `a07e7bf` an exception after the
   loop can follow a named stop, and then `stop_reason` still names the
   earlier stop while the typed stop is the exception. Suggested addition:
   "An exception after the loop leaves `stop_reason` as it was, which may
   name an earlier stop; the typed stop is the exception
   (`KeyError:post-loop-after-named` in the fixtures)."
3. **Commit 3dca66d message** says 19 journey tests. Already corrected in
   the map by 7d294ce; nothing further, but assembly notes should quote 18
   (22 after this proposal).

## Proposed migration

### 1. Projection (`_record`)

```python
"stop_fact": stop and {"kind": stop.kind, "legacy": stop.legacy,
                       "detail": norm(stop.detail), "full": norm(stop.full)},
"stops": [f.legacy for f in session.run_outcome.facts if f.active and f.legacy is not None],
```

`stops` is the input `RunOutcome.stop()` picks from (the last active fact
with a legacy name). It is what makes a post-loop exception after a named
stop visible: `stop()` alone keeps only the exception.

**Expected change to the 20 existing records:**

- Every non-null `stop_fact` (18 records) gains `"full": null`. Nothing
  else in `stop_fact` changes.
- Every record gains `"stops"`. Expected value: `[]` for `clean` and
  `CompletionUnproven:done [0]`, `[stop_fact.legacy]` for the other 18.
  Derived from source: only `_stop_with` and `_note_exception` write a
  legacy name, and every route below breaks the loop at its first
  `_stop_with`. This is the one value I could not execute (see Checks). If
  regeneration shows a record with two entries, that is a finding to
  explain, not a value to accept.

### 2. Four new journeys (existing tests, run as written)

```python
from tests import test_post_loop_exception as postloop
from tests import test_stop_full_text as fulltext

"RequirementsUnmet:long": pytest.param(
    fulltext.test_a_long_named_stop_keeps_its_whole_text_on_the_fact, marks=LEDGER),
"LookupError:long": fulltext.test_the_whole_controller_error_and_the_fact_agree,
"OSError:post-loop": postloop.test_an_exception_in_the_end_of_run_check_is_a_typed_stop,
"KeyError:post-loop-after-named": postloop.test_an_exception_in_the_final_findings_record_is_a_typed_stop,
```

Each takes only `tmp_path` / `monkeypatch`, which the fixture's caller
already supplies. Each monkeypatches a `Session` method other than `run`,
so it composes with the fixture's `recording_run` wrapper. Two go through
`project_run` (so `result_error` is recorded), two are direct `Session`
(so it is `null`); that mix is deliberate.

Expected records, from the tests' own assertions and source. Values marked
*regen* come from the journey's script and must be read off the
regenerated fixture and checked against the rule in brackets.

**`RequirementsUnmet:long`** (direct `Session`, 40 requirements, 39 unmet)

| Field | Expected |
| --- | --- |
| `completed`, `done_accepted` | `false`, `false` |
| `stop_reason` | *regen* [length > 400, starts `RequirementsUnmet: DONE was sent back 2 time(s)`] |
| `result_error` | `null` |
| `stop_fact` | `unverified`, `RequirementsUnmet`, `detail == stop_reason[:400]`, `full == stop_reason` |
| `stops` | `["RequirementsUnmet"]` |
| `tasks` | *regen* [one task, as in `RequirementsUnmet:done`] |

This is the only record where `full` is not `null` on a named stop.

**`LookupError:long`** (through `project_run`; `_run_tasks` replaced)

| Field | Expected |
| --- | --- |
| `completed`, `done_accepted` | `false`, `false` |
| `stop_reason` | `""` |
| `result_error` | `"LookupError: the orchestrator seat record is gone: " + "y" * 500` |
| `stop_fact` | `operator`, `LookupError`, `detail == result_error[:13 + 300]`, `full == result_error` |
| `stops` | `["LookupError"]` |
| `tasks`, findings lists | `[]` |

`LookupError` has no row in `_EXCEPTION_CLASS`, so `classify` gives
`operator`. The only exception record with `full` set; it pins the 300 cut
distinct from the 400 cut.

**`OSError:post-loop`** (through `project_run`, the `clean` journey plus an
end-of-run failure)

| Field | Expected |
| --- | --- |
| `completed` | `false` (was `true` on 8529b2a: the regression this pins) |
| `done_accepted` | `true` |
| `stop_reason` | `""` |
| `result_error` | `"OSError: the dependency tree could not be read"` |
| `stop_fact` | `operator`, `OSError`, `detail == result_error`, `full: null` |
| `stops` | `["OSError"]` |
| `tasks` | same as `clean`: `[{t1, closed, clean}]` |
| findings lists | `[]` (the exception precedes `_annotate_open_findings`) |

The first record with `done_accepted: true`, `completed: false` and an
empty `stop_reason`. A completion candidate reading only legacy inputs
(`stop_reason` empty, DONE accepted, no open findings) would call this run
complete. That is exactly the shape the fixture exists to catch.

**`KeyError:post-loop-after-named`** (direct `Session`, cap reached,
`_annotate_open_findings` raises)

| Field | Expected |
| --- | --- |
| `completed`, `done_accepted` | `false`, `false` |
| `stop_reason` | *regen* [starts `GoalUnconfirmedAtCap:`; expected to equal the `GoalUnconfirmedAtCap:not-confirmed` text] |
| `result_error` | `null` |
| `stop_fact` | `operator`, `KeyError`, `detail == "KeyError: 'status'"`, `full: null` |
| `stops` | `["GoalUnconfirmedAtCap", "KeyError"]` |
| `tasks` | *regen* [one task, `t1`] |

The only record where the typed stop and `stop_reason` name different
stops. Any assertion of the form "typed whole text == `stop_reason`" must
exempt this shape explicitly, or it will fail here, and it should.

### 3. Derived assertions (in the test, not the JSON)

These run on every record, including regenerated ones, so rewriting
`stop_fixtures.json` with `QUADRATUS_WRITE_STOP_FIXTURES=1` cannot bless an
outdated or contradictory value. With `whole = full or detail`:

```python
def _consistent(r):
    fact, stops = r["stop_fact"], r["stops"]
    # A: the typed stop is the last legacy stop, and only a stop has one.
    assert (fact is None) == (not stops)
    if fact is None:
        assert r["completed"] and r["stop_reason"] == "" and not r["result_error"]
        return
    assert fact["legacy"] == stops[-1] and not r["completed"]
    whole = fact["full"] or fact["detail"]
    # B: full is present only when detail was cut, and extends it.
    if fact["full"] is not None:
        assert fact["full"] != fact["detail"] and fact["full"].startswith(fact["detail"])
    # C: result.error, when project_run wrote one, is the typed whole text.
    if r["result_error"]:
        assert r["result_error"] == whole
    # D: stop_reason is the typed whole text, or it is the stop before a
    #    post-loop exception (the only route that leaves both).
    if r["stop_reason"]:
        named = r["stop_reason"].split(":", 1)[0]
        if named == fact["legacy"]:
            assert r["stop_reason"] == whole
        else:
            assert len(stops) >= 2 and stops[-2] == named
```

Checked by hand against all 20 existing records and the four new ones:

- **A:** holds. `clean` and `CompletionUnproven:done [0]` are the only
  `null` facts and the only `completed: true` records.
- **B:** vacuous on the 20; binds on `RequirementsUnmet:long` and
  `LookupError:long`.
- **C:** every non-empty `result_error` equals `detail` today (lengths in
  the table, text identical). It includes `DependencyTreeChanged`, where
  `stop_reason` is empty. `project_run` builds `error` from the exception's
  whole text or from `stop_reason`. The one rewrite, the TurnLimitBreaker
  branch, writes `in-flight.json` and leaves `error` alone.
- **D:** every non-empty `stop_reason` equals `detail` today;
  `KeyError:post-loop-after-named` takes the `else` branch.

Worth noting: before `8529b2a`, C and D would have failed on any stop over
400 characters. That is the gap the old docstring called open.

### 4. Normaliser

Apply `norm` to `full` as to `detail`. The four new texts contain no
temporary path and no 12-hex-digit run (`"x"`/`"y"` padding,
`R1`…`R40`), so normalisation leaves them unchanged and the fixture
stays readable.

## Not in scope, noted for the owner

- `RunOutcome.stop()` is "last active legacy fact", not precedence-ranked.
  After a post-loop exception the exception is the typed stop whatever came
  before it. In `_finish_run`, if an end-of-run `DependencyTreeChanged` is
  recorded through `_stop_with("integrity", …)` and `_annotate_open_findings`
  then raises, the typed stop becomes an `operator` exception above an
  `integrity` fact. The `stops` field would show that; no journey reaches
  it, and I have not re-reviewed whether it matters. Sol's clearance covers
  the seam.
- A `CompletionUnproven` reason can reach 600 characters of blockers
  (`_guard_completion`), so it is the likeliest live stop to set `full`. No
  existing journey has enough blockers to cross 400; the long-text journey
  above uses `RequirementsUnmet` because a test already builds one.

## Checks

- Read by `git show` at the four exact SHAs, plus PR #25 comments
  5862594012, 5862595888, 5862593194 and 5862699144.
- The length table was computed from `3dca66d:tests/lifecycle/stop_fixtures.json`
  with a JSON load, not by eye.
- **Not executed:** I could not run the fixture file or the two seam test
  files at `a07e7bf`. Running pytest needed a permission that this session
  could not grant. "No recorded value moves" rests on source reading plus
  Claude's `bbb4956` full-suite result, which includes `test_stop_fixtures.py`.
  The `stops` expectations and the *regen* cells are derived, not observed.
  Whoever applies this should regenerate, diff against the tables above, and
  treat any mismatch as a finding.
- No engine or test file edited. No live or vendor call. Doc only.
