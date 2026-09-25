## Goal (verbatim, unchanged)

Complete exactly two tasks, in order, without changing their boundaries. 1. Non-security display task, scope presentation.py: make /caption use the existing display helper in catalog.py. Look up that module's helper name and behavior before editing. Reuse the helper rather than copying its algorithm. Preserve the public adapter signature and endpoint response. 2. Security task, scope app.py and access.py: use the shared visible_to predicate at the records endpoint and repair the predicate so only an authenticated tenant's own records are visible. Missing records and other tenants' records must both return exactly 404 {"detail":"Not found"}. Preserve the Record(id,title) response, existing 401 behavior, and reject tenant spoofing through headers or query parameters. Authentication comes only from auth.current_tenant. Do not edit auth.py, catalog.py, README.md, tests, or policy. There is no feature entitlement. No installs, commits, pushes, external services, or production data. The common per-task integration gate checks preservation invariants only; the operator runs the full grader afterwards. Workers are available but not mandatory. Report actual lookup evidence and actual worker invocations. Run the provided check, preserve evidence and stop.

## Standing rules (always in force)

- Project source is available in the working directory. Use actual files as evidence. Do not commit or publish changes.

## Completed work

### Task t1 (by grok:default)

Task 1 was completed in `presentation.py` only: `display_title` now returns `catalog.compact_caption(value)` with the signature unchanged. Lookup recorded `compact_caption` at `catalog.py` lines 9–12 (whitespace collapse via `" ".join(value.split())`, unchanged if length ≤ 24, else `text[:23] + "…"`). The adapter previously returned `value` unchanged. `app.py` was not edited and still returns `{"caption": display_title(title)}`. No workers were invoked. The preservation gate (`pytest … test_contract.py -k preservation`) passed: 5 passed, 8 deselected (session output 0.10s; earlier recorded run 0.92s). Those tests cover record shape and 401 only, not `display_title`. A separate `-k lookup` run (3 passed) is outside that gate. No project tests exist. Security work was not done: `app.py` and `access.py` were not edited. Whether the outer goal still wanted both tasks is unresolved in the record.

**Why:** The turn required only task 1—import `compact_caption` and return it through the existing adapter (~4 code lines, 0 test lines)—and stop before the security task. Delegation was chosen so the algorithm was not copied. The conflicting opening line that says complete both tasks was noted and not followed for this turn. The harness diff matches that single edit.

**Already tried and rejected:**
- None recorded. No failed approaches.

**Full work:**
[artifact 68ca099c2c4f | task-needs | by grok:default | 1 lines, 31 chars]
{"needs": ["execute", "patch"]}
[...fetch artifact 68ca099c2c4f for the full text]
[artifact 6f964df9da71 | task-scope | by grok:default | 1 lines, 442 chars]
{"permitted_paths": ["presentation.py"], "forbidden_paths": [], "intended_result": "Make `def display_title(value):` delegate to catalog.compact_caption.", "acceptance": ["Preserve `def display_title(value):` exactly.", "Reuse catalog.compact_caption without copying its algorithm.", "Preserve /caption response shape {\"caption\": ...}.", "Run the operator-provided preservation check, retain its actual output, and stop."], "max_lines": 10}
[...fetch artifact 6f964df9da71 for the full text]
[artifact c2b5027c7232 | draft | by grok:default | 31 lines, 2056 chars]
I'll look up `compact_caption` in `catalog.py` and the current adapter in `presentation.py` before changing anything, and I'll stay inside task 1.`compact_caption` collapses whitespace, trims, and truncates past 24 characters. I'll delegate `display_title` to it and leave the signature unchanged.Task 1 is done. `presentation.py` now delegates to `catalog.compact_caption`. I stopped before the security task.

**Helper lookup.** `catalog.py` defines `compact_caption` at lines 9–12. It collapses whitespace with `" ".join(value.split())`, then returns the text unchanged when `len(text) <= 24` and `text[:23] + "…"` otherwise. `presentation.py` previously returned `value` unchanged from `display_title`. `app.py` still returns `{"caption": display_title(title)}` and was not edited.

**Change** (`presentation.py` only, 4 lines, 0 test lines):

```python
from catalog import compact_caption


[...fetch artifact c2b5027c7232 for the full text]
[artifact 78260556a29a | closeout-evidence-index | by grok:default | 4 lines, 263 chars]
Task description (historical, not a fresh instruction): artifact 311f4a635851
Recorded conversation: artifact 3ead0222f923
Source diff captured by the harness: artifact f298d5c524cd
Most recent recorded session check (may predate this task): artifact e2be4a90d62a
[...fetch artifact 78260556a29a for the full text]