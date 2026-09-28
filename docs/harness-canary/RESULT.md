# Q9 live canary: not accepted

Both live runs ended incomplete. Neither changed app.py, and each independent
acceptance run reports three failures and six passes. The synthetic tenant leak
remains. The candidate resolved its family and enforced failed gates, but this
pair does not demonstrate successful coding or improved model reliability.

Davis authorized the reviewed pair directly in the Codex task. The exact
instruction and limits are in evidence/allowance.json and were relayed on
[the coordination thread](https://github.com/Davisfox5/War-Room/pull/4#issuecomment-5770617451).
There was one run per version, no live probe, provider retry, automatic rerun,
API fallback, permission expansion or production data access.

## Results

| | Baseline | Candidate |
| --- | --- | --- |
| Runtime commit | 5d70d31 | e1e1f90288d83990ca801d761ea6e86eeb78ab5e |
| Run ID | 20260922T030524Z-4361b5a8 | 20260922T030752Z-c22907f8 |
| UTC start | 2026-09-22 03:05:22 | 2026-09-22 03:07:52 |
| External elapsed seconds | 149.78 | 128.55 |
| Provider attempts | 5 | 5 |
| Reported input tokens | 255926 | 222716 |
| Reported output tokens | 5333 | 4988 |
| Reported total tokens | 261259 | 227704 |
| Unknown-usage attempts | 0 | 0 |
| Reported-token threshold overshoot | 0 | 0 |
| Independent acceptance | 3 failed, 6 passed | 3 failed, 6 passed |
| Controller completion | false | false |
| Supervisor exit | 1 | 1 |
| app.py, README.md, policy | unchanged | unchanged |

The pair used 488963 controller-reported tokens. Claude also returned auxiliary
model diagnostics of 3653 and 4684 tokens respectively. Those diagnostics are
not explicit invocation rows; their overlap with parent usage is unknown and
is not added to the reported total. Subscription price estimates in raw reports
are not API charges. No billed API transport was used. Actual provider model
resolution is recorded only where supplied; requested seat names are not proof
of the resolved release.

All ten attempts reconcile exactly between invocation rows and budget totals.
Each run used Fable for decomposition, Sol for the security lead and its single
gate-fix round, Opus for verification, then Sol for closeout. No worker was
commissioned. The baseline lead returned a WORKER envelope, but the security
path retained it as a draft rather than dispatching it. This pair therefore
does not exercise Q1's worker-fit rejection or Q7's sibling/inner loop.

## Failure and independent evidence

Sol reported `bwrap: No permissions to create a new namespace` in both runs.
It could not edit the file through its inner sandbox. Opus independently found
the original leaking condition, rejected completion, and pointed out that the
model's sandbox explanation lacked a preserved tool-level transcript.

After the pair, an offline, credential-free container with the same user,
capabilities, read-only root and no-new-privileges reproduced denial of a user
namespace: `unshare --user --map-root-user /bin/true` exited 1 with
`unshare: unshare failed: Operation not permitted`. The exact command and result
are in evidence/offline-namespace-proof.json. This independently establishes
the environment's namespace restriction. It is not a recovered transcript of
the model's particular failed tool call.

The controller's gate executed successfully as a process and caught both
cross-tenant leaks and tenant-hint spoofing. Its failure is independent of the
model's inability to run its own commands. After each run, the frozen external
suite ran again with no network and read-only project/runtime mounts. Both
returned the same three failures. app.py, README.md and policy hashes are
identical to the original fixture, as recorded in independent-verdict.json.

## Policy and prompt evidence

The baseline has no policy-plan record. The candidate persisted
`scoped-endpoint` with `tenant-isolation`, no policy blockers, and plan hash
`263c2282a219a7c999d857bae749edfe189d56587322e8b0a6dfbe29cf4b4d04`.
Its run result includes both required gates; the API gate receipt fails.
Missing adapter values remain honestly marked not_configured.

The candidate's source at the frozen commit sends the role packet through
Session._lead_prompt, Session._verifier_prompt and Session._invoke_model. An
offline reconstruction from the identical policy verifies that both prompt
builders contain the scoped-endpoint and tenant-isolation packet. The two
reconstructed prompt files are explicitly labeled. The targeted role-packet
suite passes all 18 tests.

Full successful-call wire prompts and native CLI tool transcripts are not
retained by this runtime. The reconstruction and source path are evidence of
prompt construction, not captured proof of the exact live wire bytes. Both
runs' complete retained controller records, raw answer artifacts, invocation
ledgers, usage rows, gate output, diffs and supervisor receipts are included.
All eight raw answer/evidence artifacts per run and both closeouts were read.
No additional model calls were made to recover missing evidence.

## Measurement limitations and decisions

1. The launcher redirected its console into live-console.txt at the project
   root. The controller counted that operator-created log in source_changed
   and changes.diff. It was visible to reviewers. Neither model edited source;
   the fixture hashes and task-level scope reports prove this. For a future
   batch, write the console under the excluded .quadratus directory. Keep this
   pair's original records unchanged.
2. Candidate gate receipts report tests=6 and "fewer tests than required"
   because the parser counts passed tests in the `3 failed, 6 passed` summary.
   Nine tests actually ran. The failed exit still rejects the task correctly,
   but the diagnostic should count executed tests and preserve assertion
   failure as the cause. This pair must not be described as zero or missing
   tests.
3. The prepared image passed import/CLI-presence checks, but those checks did
   not exercise a credential-free inner-sandbox operation. Add that offline
   environment check before requesting another live batch. Do not disable a
   sandbox or grant privileges merely to make a run pass.
4. A new run needs a separately bounded allowance after environment repair and
   review. This pair is closed, not eligible for an automatic retry. A single
   fresh run per version is a canary, not pass^k or a reliability estimate.

## Exact checks and cleanup

Inside each network-disabled grader container:

```text
python -m pytest -q -p no:cacheprovider /opt/quadratus/test_contract.py
baseline: 3 failed, 6 passed, 1 warning in 0.11s; exit 1
candidate: 3 failed, 6 passed, 1 warning in 0.11s; exit 1
```

The warning is Starlette's deprecated AnyIO BlockingPortal alias. Complete
outputs are in each evidence directory's grader.txt. The full grader Docker
argv is in checks.json.

```text
/tmp/quadratus-harness-env/bin/python -m pytest -q tests/test_role_packets.py
18 passed in 0.43s
```

The supervisor removed both live containers and their tmpfs homes. The private
auth-only credential seed was removed after the pair. No harness automation or
launch agent was found on this Mac; the unrelated Flex outreach automation was
left intact. Claude's coordination watcher cleanup is recorded on the thread
when Q10 closes. No production migration or deployment is claimed by this Q9
report.
