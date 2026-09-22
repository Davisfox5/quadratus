# Harness replay pack

Label: **controller determinism, not live reliability.**

Twenty cases replay captured or abridged vendor replies into the session
engine through a scripted `invoke` and assert what the controller did with
them: what it stopped, what it preserved, what it refused, what it carried
into the record. No vendor CLI, API key, network or database is touched.

A pass means the harness reacts to a given reply the same way every time. It
says nothing about how often a live model produces that reply. A family
validated only by this pack carries the label "live reliability not measured"
until fresh runs are funded under an explicit allowance (report,
Reconciliation with the Grok harness playbook).

## Running it

```bash
python -m tests.harness_pack        # per-case report, exit 1 on any failure
pytest -q tests/harness_pack        # the same cases as CI test items
```

## Cases

Each file under `cases/` names its `source`: the report section or incident
it replays (the 2026-09-13 GameTape trials, acceptance attempt 3, the spine
features, Q1 through Q5). Shapes follow the playbook's ten: happy-path,
fake-done, blocked-path, noisy-tool, scope-trap, permission-trap,
compaction, idempotence, resume, cost-cap.

Where a grader is involved (a review, a recheck, a security verdict) the
case carries a `good` and a `bad` variant, and `test_pack.py` proves the
good expectations fail against the bad script. Cases with `grader: false`
may still carry variants when two outcomes share one setup.

## Frozen-pack rule

Do not tune a harness change against these cases. A change that fixes a run
lands with a new case that would have caught that run, added here before the
fix is tuned (report, Reconciliation: frozen-pack rule). Edit an existing
case only when the engine's contract changes on purpose, and say so in the
case's `source`.
