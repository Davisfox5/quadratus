# Isolated offline workflow assembly candidate

This followup candidate is on `codex/sol-offline-candidates-20260928T045000Z` in
`/private/tmp/quadratus-sol-offline-candidates-20260928T045000Z`. It begins at
the cleared assembly `a9d0a933eb877cfa5878ffb9343bee12264b67f5`, itself
built from `7d294ce91920adcf31bed204ae865fd93e34815c` and the
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
outside the original assembly commit. The followup imports completion,
projection, and scorecard candidates as described below, without wiring.

## Followup: exact candidate module composition

The eleven files below are copied byte for byte from their named source tips.
They remain offline candidates: neither `Session` nor `outcome.py` imports
or invokes them. The earlier twelve files remain from the base assembly;
this manifest is updated to describe the followup.

| Source tip | Exact imported files |
| --- | --- |
| `064f93066f922836d600dc72d059fe5369ded7e2` | `quadratus/completion_decision.py`, `tests/test_completion_decision.py`, `docs/completion-decision-contract.md` |
| `05840ead7901c64f5125be05508ac0a867826743` | `tests/test_completion_decision_session_seams.py`, `docs/review/completion-session-seams.md` |
| `a66cbb64ec2baccaebdc8ac43a767065d9d2f6d0` | `quadratus/finding_state.py`, `tests/test_finding_state.py`, `docs/finding-state-contract.md` |
| `4297e22d99f40a96ce056f653ffe2c78937b1d7f` | `tools/workflow_scorecard.py`, `tests/test_workflow_scorecard.py`, `docs/workflow-scorecard.md` |

The completion seam test includes the `05840ead` lint correction. Its source
diff only adds the explicit `zip(strict=False)`, formats an import, and marks
two pytest fixture name collisions for lint. The source was read and its
tests rerun in this assembly.

The finding projection's compatibility tests cover old saved facts without
`Fact.full` separately from current facts with full text. A legacy saved
fact can still report a bounded truncation gap; the assembly did not alter
the engine or fixtures to erase that limitation.

The pinned image resolved to
`sha256:707363c1c70ea5918be65840c6a37ac5049716fb94f530264ecdc0ea8f7dc0d9`.
Source was mounted read-only at `/pkg`; the container used `--network none`,
`--read-only`, and a temporary `/tmp` filesystem. One combined focused run:

```text
python -m pytest -q -p no:cacheprovider \
  tests/test_completion_decision.py tests/test_completion_decision_session_seams.py \
  tests/test_finding_state.py tests/test_workflow_scorecard.py
161 passed in 11.02s
```

These tests prove candidate behavior against scripted and saved records. They
do not establish live provider judgment, runtime integration, or production
readiness. Independent review of this exact followup assembly is required
before adoption, live test, activation, or merge.

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
