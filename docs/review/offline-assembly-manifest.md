# Isolated offline workflow assembly candidate

This candidate is on `codex/sol-offline-assembly-20260928T033254Z` in
`/private/tmp/quadratus-sol-offline-assembly-20260928T033254Z`. It begins at
`7d294ce91920adcf31bed204ae865fd93e34815c`, which includes the
independently reviewed E2 fix `bbb4956f2f567a3db70f4c1768692698ce44bab6`.
It is for independent integration review, not production adoption.

## Exact added files and source tips

| Source tip | Files brought into this candidate |
| --- | --- |
| `87610b10f9198279426e536807191cf5b4e45057` (acceptance) | `docs/workflow-acceptance-manifest.json`, `tests/test_workflow_acceptance.py`, `tools/workflow_acceptance.py` |
| `ea8dcd5b764849bc1e7ad7ddfb5b0fb6e9e7235f` (wheel) | `docs/workflow-wheel-validation.md`, `tests/test_workflow_wheel.py`, `tools/verify_workflow_wheel.py` |
| `225f46fc62f3fb40db1c9dba695ed3cf41bdd5d3` (J30) | `docs/review/worker-failure-journey.md`, `tests/test_workflow_worker_failure_journey.py` |
| `7aacdd6d32ab1e849879702cc45154326b01f950` (J31) | `docs/review/design-fix-role-journey.md`, `tests/test_workflow_design_fix_role_journey.py` |
| `006bdbede6f78800625c1a799ba0458021e5b1e0` (evidence) | `docs/workflow-evidence-boundary.md`, `tests/test_workflow_evidence_boundary.py` |

The evidence pair is intentionally adjusted after import. E2 is a passing
non-completion control on this base; the old assertion that changed approved
renders still complete is historical only. Under PR #25 ruling 5862699144,
the preview `exit 1` fixture is an unattributed `invalid_proof` and
`unverified` stop with no repair, not an operator fault. The corresponding
document records those limits. No source file other than the twelve named
files was imported; this manifest is the thirteenth added file. `session.py`,
`outcome.py`, completion/projection wiring, and the uncleared scorecard are
outside this candidate.

## Offline validation

The frozen image was
`quadratus-blind-preflight:20260924-candidate` at
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`.
Source was mounted read-only at `/pkg`; the container had `--network none`,
`--read-only`, and a temporary `/tmp` filesystem. The single combined suite
covered the five imported test modules and `tests/lifecycle`:

```text
python -m pytest -q -p no:cacheprovider \
  tests/test_workflow_acceptance.py tests/test_workflow_wheel.py \
  tests/test_workflow_worker_failure_journey.py \
  tests/test_workflow_design_fix_role_journey.py \
  tests/test_workflow_evidence_boundary.py tests/lifecycle
351 passed, 11 skipped in 177.86s
```

The installed-wheel test skipped because `QUADRATUS_WHEEL_BUILDER` was not
configured. No installed wheel behavior is claimed from this run. Ruff is
not installed in this frozen image. Staged patch whitespace validation
passed. Tests use scripted providers and prove controller paths, not model
judgment, a live continuation, or production readiness.

Independent review of this exact assembly is still required before any
integration, live test, activation, or merge.
