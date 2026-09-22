# Q9: live canary

State: the authorized pair ran on 2026-09-22 and was not accepted. See RESULT.md
for the failure, complete retained records, limits and cleanup. A later
[native Mac run](native-20260922/README.md) repaired the fixture and passed all nine
tests; its separate controller-status defect and offline fix are recorded there.
The preparation record below describes what was frozen before execution.

Compare baseline Quadratus 5d70d31 with candidate e1e1f90288d83990ca801d761ea6e86eeb78ab5e
(Q1 through Q8 merged). Each receives an identical copy of fixture/app.py,
fixture/README.md and policy.json at .quadratus/policy.json. The baseline predates
the policy loader and ignores the policy; the candidate resolves scoped-endpoint.
Both use the same goal, external acceptance tests, run limits and subscription
transport. One run per version is a canary, not a reliability estimate.

The task fixes a synthetic two-tenant FastAPI lookup. There is no production
repository, real customer data, database server or outbound message in the
fixture. Only app.py may change. The acceptance suite stays in the read-only
runtime mount; run state stays in the disposable project.

## Frozen allowance, subsequently authorized and executed

- One baseline and one candidate run, sequentially. No automatic rerun.
- Subscription CLIs only; no billed API fallback. Existing subscription auth
  staged into a private, auth-only seed after approval; never the host home.
- Per run: 24 provider attempts, 500,000 reported-token stop threshold, two
  concurrent workers maximum and two tasks maximum. In-flight token overshoot
  is possible and must be reported. Across the pair: 48 attempts maximum.
- External hard wall: 900 seconds per run. Internal new-call deadline: 840
  seconds, leaving a finalization window. A watchdog removes the container and
  its processes at the hard wall; partial source and records are preserved.
- No automatic provider retries, model probes, commit, push, publication,
  production database or permission expansion. Routing uses the existing seats.
- Any unknown usage, refused scope, exhausted budget or failed acceptance check
  is reported as incomplete. Do not loosen the test or scope to get a pass.

## Isolation and prepared image

Use existing quadratus.isolated_run.run_isolated, with network enabled only
for subscription provider transport after approval. Runtime is read-only,
project is a unique writable fixture copy, container root is read-only, HOME
is ephemeral, and credentials are mounted separately. No Docker socket or
other repository is mounted. The model code itself is not a security boundary;
the disposable container constrains writes to the prepared work tree.

Locally prepared image:
sha256:ec4033007992a312694c1bd084a78bafd2cbf8e8dec559009892c5128b3f0373

It extends the previously prepared application image
sha256:d1f99331a1639f5ff364faf9e4c027943613fadba733d452191296b873a390e2
with jsonschema==4.26.0. Its three vendor binaries are present. Live login and
model availability have not been probed, because a probe spends capacity.

run_fixture.py refuses execution without an allowance record naming Davis and
a source. That record is an operator record of actual approval, not a way for
an agent to manufacture authorization. --preflight constructs settings and
limits without constructing a Fleet or invoking a model.

## Offline evidence

In the prepared container with --network none and both mounts read-only:

```text
python -m pytest -q -p no:cacheprovider /opt/quadratus/test_contract.py
baseline: 3 failed, 6 passed, 1 warning in 0.12s
candidate: 3 failed, 6 passed, 1 warning in 0.12s
reference repair: 9 passed, 1 warning in 0.10s

python /opt/quadratus/run_fixture.py --preflight
baseline and candidate: exit 0
CLI-only; 24 attempts; 500000 reported-token stop; 840s internal deadline;
external hard wall 900s; scope app.py
```

The reference repair adds `or record["tenant_id"] != tenant` to the missing-row
condition. It is kept only in the offline grader-control copy and is not copied
to either solver project. The frozen checks cover own-tenant access, both
cross-tenant directions, missing and invalid authentication, header/query
spoofing, uniform 404 responses, and an unrelated path. The policy validates.
An initial preparation attempt used the Mac's older Python tar API and failed
before fixtures were mounted; re-preparation with the Python 3.12 environment
produced the evidence above. No model call occurred in either attempt.

## Acceptance and closeout

After each approved run, run the immutable tests again in a network-disabled
container, verify only app.py changed, inspect the diff and every raw trace,
and reconcile provider attempts, invocation ledger, reported tokens, unknown
usage, overshoot, gates and terminal status. Passing model prose is not success.
Archive the immutable inputs, runtime commits, image ID, output records and
examiner verdict. Send the report to Claude for Q10 review. Remove only the
canary containers, credential seeds and watchers created for this batch.
