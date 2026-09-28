# Acceptance map refresh: J30, J31, E2

## TL;DR

- J30 now cites its one new test, but only for the **optional** worker
  failure. `calls` and `partial_work` are mapped. `outcome` and `report` stay
  UNPROVEN on purpose: the required-worker route has no contract, so nothing
  can prove "required fails its edge". J30 stays UNPROVEN.
- J31 cites its role-packet test for `calls`, `outcome` and `partial_work`.
  `report` stays UNPROVEN. The test reads the design-evidence artifact, not
  `result.json`.
- The E2 tests go under J10, for `outcome` only. The no-rewrite control is
  listed as a guard and cited for nothing.
- No new obligations. No engine, tool or test files changed. The new
  selectors exist only in assembly `a9d0a93`. At this branch's base
  (`87610b1`), `tests/test_workflow_acceptance.py` fails selector validation
  unless the manifest is applied on top of that assembly.

## Sources

| Change | Source | Test read |
| --- | --- | --- |
| J30 optional | `225f46fc62f3fb40db1c9dba695ed3cf41bdd5d3` | `tests/test_workflow_worker_failure_journey.py::test_optional_worker_failure_is_recorded_and_the_task_can_complete` |
| J31 | `7aacdd6d32ab1e849879702cc45154326b01f950` | `tests/test_workflow_design_fix_role_journey.py::test_design_fix_gets_the_lead_packet_in_a_bounded_session_journey` (parametrized `recaptured`) |
| E2 | `bbb4956f2f567a3db70f4c1768692698ce44bab6` | `tests/lifecycle/test_replaced_evidence.py` (three tests) |
| Assembly | `a9d0a933eb877cfa5878ffb9343bee12264b67f5` (source `7d294ce`) | these tests are present unchanged |

The obligations follow `docs/workflow-map.md` §7: calls by role, the final
outcome, the `result.json` fields (`report`), evidence identity and
preserved partial work. The existing J1–J3 mappings set the precedent that
`report` means assertions on `replay.workflow`, which is `result.json`
`workflow`.

## Mapping decisions

**J30.** The test asserts that the worker reached the provider (`claude`) and
that the lead was called exactly twice. The second lead prompt carries the
error-as-result. That supports `calls`. It also asserts that `app.py` keeps
the lead's own edit, which supports `partial_work`. It asserts completion and
that `edges.checks` is true with `unsatisfied == []`, but those describe
the optional half only. The map's Required column is "optional recorded;
required fails its edge". `quadratus.contract.Required` has no worker field,
so the required half cannot be exercised
(`docs/review/worker-failure-journey.md`). Leaving `outcome` and `report`
unmapped keeps the journey UNPROVEN.

**J31.** `calls` is supported: one `design-fix`, `Role: lead` in the prompt
and `Role: reviewer` absent, the same vendor as the lead, the same argv as
the revision, no `gate-fix`, and design-review count 1 or 0. `outcome` is
supported: `ended_at_cap` (typed `cap` stop) when recaptured, and a
`DesignUnverified` stop that is not completed otherwise. `partial_work` is
supported because both edited files are kept in both cases. `report` is not
mapped. `verified` is read from the design-evidence artifact, and the only
`result.json` read is the run stop inside `ended_at_cap`.

**E2 → J10.** The map note (item 13) names no journey. J10 already carries
the completion guard's delivery-edge test for `outcome` only, so the E2
tests follow that precedent. The replaced and cap tests assert the run did
not complete, `CompletionUnproven`, and an active terminal `unverified`
delivery fact that is not integrity. That matches J10's Required column
"`unverified`; not completed". Other obligations are not mapped. E2 is a
reviewer-approved-then-replaced route, not J10's missing-response route, so
its sha assertions are not claimed as J10 `evidence_identity`, and its
role-call and fact assertions are not claimed as J10 `calls` or `report`.
`test_the_same_run_without_the_rewrite_completes` is listed in `tests` only.
If it fails, J10 fails, because the guard would then be over-blocking.

`baseline_commit` stays at `9eabf69`, because the earlier mappings were
reviewed there. A new limitation records that the E2 route needs `bbb4956`.

## Verification

The branch's own worktree copy of `a9d0a93` was read-only-mounted at
`.scratch-assembly` with this manifest overlaid. The assembly checkout and
other owners' files were not touched. The run used frozen image
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`
with `--network none --read-only`:

```text
python -m pytest -q -p no:cacheprovider tests/test_workflow_acceptance.py \
  tests/test_workflow_worker_failure_journey.py \
  tests/test_workflow_design_fix_role_journey.py \
  tests/lifecycle/test_replaced_evidence.py \
  tests/lifecycle/test_completion_guard.py tests/lifecycle/test_workflow_contract.py
57 passed in 26.90s
```

That run includes `test_manifest_matches_map_and_selectors_exist`, so the
refreshed manifest validates against the assembly with 39 journeys.

**Limitations.** The full `tools/workflow_acceptance.py` inventory run was
not completed. The one attempt passed a stray `--` argument and exited at
argparse, and this bounded worker stopped there, so per-journey statuses are
not reported here. Host Python lacks `jsonschema`, so only the container
result counts. The tests are scripted controller replays: they show
deterministic routing, not model behaviour or a live run.
