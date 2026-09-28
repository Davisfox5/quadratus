# Lane 9 — operator-v2 readiness and phase-4 prerequisites

Session: `grok-lane9-readiness-20260928T0237Z` (Grok Build / xAI). Authorized by Davis. Coordination: [PR 25](https://github.com/Davisfox5/quadratus/pull/25), assignment [comment 5862056172](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5862056172).

Review base: `9eabf69c0627bd605f72ff1bcd8d9c700d216583` (detached). This file is the only repo change. No profile edit or activation, no browser or vendor run, no deployment, no merge, no authority or budget change, and no edit to preserved Run19 source or its excluded dependency shim.

Comment watermark when this note was written: `5862323208`, plus the lane-9 claim `5862286599` and progress `5862306715`. Nothing after the assignment changed this lane's file ownership or base.

## What "approved operator-v2" refers to

The shared plan in [5856716643](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5856716643) calls the prospective operator-v2 wrapper an approved, preserved, unactivated input to phase 4. That approval is Claude's "no further objection" in [5853773429](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853773429) to the exit-0 diagnostic delta in [5853551848](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853551848), which Claude labels `e5885c9f…`.

`e5885c9f` is not an object in this clone or on `origin`. [5853551848](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853551848) says the full wrapper changed only by extracting `failure_diagnostic` and wrapping `validate` in `TypeError`/`ValueError`. The only full source on the PR is the earlier paste in [5853493201](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853493201). Findings below that depend on the paste are marked as applying to that paste, not to hashed approved bytes.

The plan text in [5856716643](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5856716643) matches the phase-4 paragraph Codex said was written to `work/quadratus-shared-workflow-plan.md`. That file is not in `9eabf69`. Phase-4 rules used here are the PR paste plus `docs/workflow-map.md` at this SHA (section 12, "P4 entry criteria").

## Run19 cause is not established

Do not read any row below as proof of why Run19 failed.

[5853894131](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853894131) and `quadratus/readiness.py` lines 1-8: Run19's runner stderr was discarded. A later same-UID run with no `HOME` failing `uv_os_homedir`, and the same check with an owned `HOME` passing, is prospective environment evidence. It does not prove the original failure's cause and does not prove the shim was necessary. Claude accepted that correction in [5853897759](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853897759). The UID-501 receipts named in [5853493201](https://github.com/Davisfox5/quadratus/pull/25#issuecomment-5853493201) (`work/run19-home-controls-report.json`, `operator-browser-v2-worker-controls.json`) are not in git. They were not re-executed.

## Offline checks actually run

Interpreter for the unit and J7 runs: CPython 3.10.21. Whole-controller imports need `tomllib` (3.11+). Those four tests ran with an **out-of-tree** adapter, `/tmp/py310-tomllib/tomllib.py`, which re-exports `tomli`. Nothing under the checkout was patched. `tests/test_readiness.py` does not need that adapter.

```text
python3 -m pytest -q tests/test_readiness.py -p no:cacheprovider
7 passed in 2.95s

PYTHONPATH=/tmp/py310-tomllib python3 -m pytest -q \
  tests/lifecycle/test_workflow_contract.py::test_j7_a_failing_probe_stops_the_run_before_any_model_call \
  tests/lifecycle/test_workflow_contract.py::test_j7_the_first_failing_probe_ends_probing \
  tests/lifecycle/test_workflow_contract.py::test_passing_probes_prove_readiness_only \
  tests/lifecycle/test_workflow_contract.py::test_a_probe_that_writes_project_source_is_an_integrity_stop \
  tests/lifecycle/test_workflow_contract.py::test_a_malformed_declaration_is_refused_before_the_run \
  -p no:cacheprovider
12 passed in 1.33s
```

A separate process, `HOME` removed, showed a readiness probe does not invent `HOME`:

```text
probe_env {"home": null, "marker": "LANE9_AMBIENT", "probe": "/tmp/quadratus-probe-env-…", "uid": 0}
scratch_removed True
tap_count 9          # wrapper-shaped "# tests 9 / # pass 9 / # fail 0 / # skipped 0"
pass_product {'product': False, 'reasons': []}
bare_fail {'product': False, 'reasons': ['check: structured report undeclared']}
default_limits 24 500000 900.0 2
```

`uid 0` is this sandbox, not the phase-4 Mac user. `attribute()` returning `product: false` on a **pass** means "do not authorize repair", not "this was not a check". A passed gate still satisfies the checks edge.

## Prerequisite checklist

Status is only `verified`, `missing`, or `blocked`.

| # | Prerequisite | Status | Source | What was checked |
| --- | --- | --- | --- | --- |
| 1 | Lane base is the assigned immutable SHA, not the incumbent PR head | verified | `git rev-parse HEAD` → `9eabf69c0627bd605f72ff1bcd8d9c700d216583`. PR 25 head remains `fdee0d8` | Checkout only |
| 2 | Phase 4 stays Codex-owned, one bounded GameTape continuation, no profile activation before independent integration and this profile review | verified as a rule, not as permission to launch | `docs/workflow-map.md` lines 614-626; plan in 5856716643 | Read. This session did not launch |
| 3 | Every P3 package cleared and legacy inputs retired, which the map lists as P4 criterion 1 | missing | Same map, lines 560-628: seven legacy inputs still decide; "Overall P3 is not complete" | 5862056172 calls `9eabf69` cleared for workers. The map at that SHA still withholds P4. This lane does not re-grade P3 |
| 4 | Installed-wheel validation of **this** head, outside the source tree (criterion 2) | missing | Map lines 617-618. 5862303286 clears `ea8dcd5` as layout and producer boundary only, and says that is not a live or acceptance check. That commit is not `9eabf69` | Not claimed from the packaging lane |
| 5 | Root integration review of the workflow plus this profile review, then Codex-owned execution (criterion 3) | missing | Map line 619; assignment: Root Codex owns bounded execution | This document is only the profile/readiness review |
| 6 | Non-pytest failures stay operator handoffs unless a producer exists, or that limit is explicitly accepted before launch (criterion 4) | missing | Map lines 532-534 and 611-612. `integration.attribute` (line 229) authorizes repair only for the harness pytest report | Reproduced: a nonzero result with `report.state=undeclared` is `product: false` |
| 7 | Live bounds stay 120 calls, 6e6 reported tokens, 7200 s internal, 7500 s outer, 2 workers, 20 tasks, existing lead cap. Library defaults must not be used by accident | missing | Plan in 5856716643. `RunLimits` defaults in `quadratus/run_budget.py` lines 48-52 are 24 calls, 500_000 tokens, 900 s, 2 workers | Defaults printed. No launch profile was available to show the larger bounds are what would actually be passed |
| 8 | No activation and no adoption of the profile into the `90cc5d9` execution tree | verified as still true in git | Map lines 625-626. `git grep EXPECTED_SCENARIOS` on `9eabf69` and `90cc5d951a63b3d0645215cd5a9aa4f26d1ddce0` finds nothing | Profile text is not on those commits |
| 9 | Readiness probes run as the execution uid, with that process's environment, before any model call | verified for the engine mechanism; blocked for the phase-4 host | `readiness.run_probe` lines 144-148 copies `os.environ` and does not change user. `session._run_tasks` lines 2975-2980 runs probes after the dependency baseline and before the plan gate | Probe with `HOME` unset kept `HOME` null, inherited a marker, removed its scratch dir, and reported uid 0 here |
| 10 | The same probe does **not** create the owned `HOME`/`TMPDIR` the v2 paste uses | verified | `run_probe` only adds `QUADRATUS_PROBE_DIR`, `PYTHONPYCACHEPREFIX`, and `PYTHONDONTWRITEBYTECODE` | Command in the section above |
| 11 | Pasted v2 wrapper runs node with an owned temp `HOME` and `TMPDIR`, and a replaced environment (`PATH`, `NODE_PATH`, `PLAYWRIGHT_BROWSERS_PATH`, chromium path), not the parent's environment | verified for the paste only | 5853493201 `check_run`. 5853551848 says this part was unchanged | Not re-run. No browser, no `/opt/playwright` |
| 12 | Approved wrapper bytes match that paste plus the exit-0 diagnostic | blocked | 5853773429 cites `e5885c9f`, which is not on origin. 5853551848 is an excerpt | Need the path and content hash. Do not treat the paste as the installed file |
| 13 | Exit 0 with a failed `validate` still emits the bounded diagnostic | missing on the full paste; claimed fixed; blocked on approved bytes | Paste diagnoses only `code != 0` (`check_run`). Excerpt in 5853551848 adds the `try/except` around `validate` | Excerpt was not applied or executed |
| 14 | UID-501 no-shim control: missing `HOME` fails `uv_os_homedir`; owned `HOME` passes 9 scenarios; source unchanged | blocked | Claimed in 5853493201. Receipt paths are not in the repo | Prospective even if the receipts appear. Not Run19's cause |
| 15 | Dependency identity stops a project-local `node_modules` / `.venv` / `venv` / `env` tree that appears or changes after run start, including the Run19 shim shape | verified in engine tests' source, not re-graded as a live tree | `quadratus/deptree.py` lines 1-36 and 51-58; introduced by ancestor `cac2b5f77ea82f7b68cf450bb35f0d0091fac409` | Not run against preserved Run19 files |
| 16 | External toolchain identity (`/opt/browser-tools`, `/opt/playwright`, `/usr/local/bin/node`, the chromium binary) is part of what the check proves | missing | `deptree.py` lines 32-36: outside-project `NODE_PATH` is not guarded. The paste pins those paths and does not hash them | A local shim is still the deptree case. A swapped system Chromium would not be |
| 17 | Phase-4 application tree is the exact 38 preserved Run19 source files, shim not copied | blocked | Plan in 5856716643; Run19 stop note 5853467525. No manifest or shim path in git | Requested below |
| 18 | Readiness success is not a product check and not acceptance | verified | `tests/lifecycle/test_workflow_contract.py` `test_passing_probes_prove_readiness_only` (lines 68-75). Executed: 12 passed, including this test. Contract `capabilities` recorded `["python"]` while check receipts stayed `[]` | `missing_facts` does not treat `capabilities` or run-level readiness as a satisfied edge |
| 19 | The v2 wrapper is a nine-scenario product counter, so it must be a declared check if it is used at all, never a `--readiness` probe | verified for how the engine would count it; blocked for how the profile wires it | Paste docstring and `EXPECTED_SCENARIOS`. `_test_count` (`integration.py` lines 488-506) returns 9 for the paste's TAP lines, including the placeholder-media note | Wiring JSON was not in git. Putting this command in `--readiness` would gate on product scenarios and then omit them from checks |
| 20 | A nine-scenario pass is not evidence delivery, reviewer approval, or media acceptance | verified | Paste prints `# limitation: placeholder video` and still emits `# fail 0`. Delivery/reviewer are separate mandatory edges (`outcome.py` `_AUDIT_EDGES`, map lines 461-465) | Count and edges read. No media run |
| 21 | Browser-check failure must not become an application repair unless a declared structured producer says it is an assertion failure | verified for this engine | `attribute` as in row 6. The paste's node runner does not pass `--quadratus-report` | Matches the map: operator-profile checks are P4 work. Accept the handoff limit or add a producer before expecting UI repair |
| 22 | Readiness scratch and descendants are cleaned up; cleanup errors do not replace the probe result | verified, with one silent path | `tests/test_readiness.py`: 7 passed, including a SIGTERM-ignoring descendant and a child left after a clean exit. `run_probe` records `_stop` errors. `shutil.rmtree(..., ignore_errors=True)` at line 177 does not record a scratch-removal failure | Scratch in the HOME-unset probe was gone |
| 23 | Pasted wrapper cleans the owned HOME and does not delete a pre-existing browser output directory | verified for the paste only | `TemporaryDirectory` in `check_run`. `main` refuses if `tests/browser/output` exists, then renames the directory it created into `.quadratus/operator-browser-receipts/<uuid>` if the inode is unchanged | Not executed. A failed rename after success leaves `tests/browser/output` in source |
| 24 | Wrapper receipts are harness evidence with an author and a delivery edge | missing | Paste writes under the project `.quadratus`, which `deptree.SKIP_TOP` does not walk and which `run_project` excludes from source when that directory is the state dir. They are not `ArtifactStore` objects and not the reviewer edge | Real in-run evidence for phase 4 still has to be the engine's capture, delivery, and reviewer record |
| 25 | A probe that edits project source is an integrity stop with no model call | verified | `test_a_probe_that_writes_project_source_is_an_integrity_stop`, included in the 12 passed | Executed with the tomllib adapter disclosed above |
| 26 | Preserved Run19 shim and the 38 source files were not modified by this lane | verified for this checkout | Checkout contains neither the shim nor those application files. This commit adds only this document | Host trees were not mounted |

## Phase 4 is not ready to launch

Rows 3, 4, 5, 6, 7, 16, and 24 are missing on the record this lane could read. Rows 12, 14, and 17 are blocked on host artifacts. Root Codex should not treat this note, a green readiness probe, or a 9/9 TAP line as product acceptance or as Run19's diagnosis.

The engine at `9eabf69` does the thing the plan asked of readiness: a declared probe runs once under the current process identity, a failure is `CapabilityProbeFailed` with no model call, and a pass is not stored as a check. The pasted operator-v2 wrapper is a different tool. It manufactures a private `HOME` and then counts nine product scenarios. Those two must stay separate in the phase-4 command. The wrapper's owned `HOME` result, even if the missing receipts confirm it, still would not identify the original Run19 failure.

## Paths requested

These were not guessed:

1. Filesystem path and content hash of the approved unactivated operator-v2 wrapper (`e5885c9f` is not on origin).
2. Path of `work/quadratus-shared-workflow-plan.md` if it differs from the plan pasted in 5856716643.
3. Paths of `work/run19-home-controls-report.json` and `operator-browser-v2-worker-controls.json`, or the paths those names were shorthand for.
4. Path of the preserved 38-file Run19 source tree and the excluded shim, plus any manifest already hashed.

## Reproducible checks

From a clean `9eabf69` checkout, without network and without the host profile:

```text
python3 - << 'PY'
import os, sys
sys.path.insert(0, ".")
os.environ.pop("HOME", None)
os.environ["QUADRATUS_LANE9_MARKER"] = "LANE9_AMBIENT"
from quadratus.integration import _test_count, attribute, GateResult
from quadratus.readiness import Probe, run_probe
from quadratus.run_budget import RunLimits
sample = "# tests 9\n# pass 9\n# fail 0\n# skipped 0\n"
assert _test_count(sample) == 9
assert attribute(GateResult(True, "cmd", 0, sample))["product"] is False
assert attribute(GateResult(False, "cmd", 1, "x", report={"state": "undeclared"}))["reasons"]
receipt = run_probe(Probe("env", (sys.executable, "-c",
    "import os,json;print(json.dumps({'home':os.environ.get('HOME'),"
    "'marker':os.environ.get('QUADRATUS_LANE9_MARKER')}))")), "/tmp")
assert receipt.passed and '"home": null' in receipt.output
limits = RunLimits()
assert (limits.max_calls, limits.max_reported_tokens) == (24, 500_000)
print("ok")
PY

python3 -m pytest -q tests/test_readiness.py -p no:cacheprovider
```

On Python 3.10 the whole-controller J7 module also needs an out-of-tree `tomllib` (3.11 has it). Do not copy that adapter into the tree. The J7 test that must stay green is `test_passing_probes_prove_readiness_only`: capabilities may list the probe id, and `result.json` check receipts stay empty.
