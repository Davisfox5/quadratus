# Native Mac run: code repair and all nine tests pass

Quadratus can run locally on Davis's Mac. On 2026-09-22, the native run changed
only the intended tenant guard in app.py. Its gate and an independent rerun of
the unchanged acceptance suite both passed all nine tests. The earlier Docker
namespace denial was an environment-specific failure, not a restriction on this
Codex session or proof that Quadratus cannot run on the Mac.

The original controller result remains `completed: false`: its legacy security
verdict parser mistook the reviewer's heading `Not blocking, worth noting` for a
blocking finding. That result is preserved unchanged in run/result.json. The
small parser repair accompanying this evidence fixes the false finding offline;
no second live run or fresh orchestrator completion is claimed.

## Run and controls

- Runtime: 36ab9b6d2694e748f4d831c03eed640ae421124e, before the accompanying fix.
- Run: 20260922T122228Z-f4569fa5, 12:22:28 to 12:24:36 UTC, 128.33 seconds.
- Four subscription CLI calls: Fable decomposition, Sol implementation, Opus
  verification, Sol closeout. No worker or native child was dispatched.
- 300287 reported tokens, zero unknown-usage calls and no threshold overshoot.
  Vendor auxiliary diagnostics report another 5081 tokens with unknown overlap;
  they are not added to the controller total. No billed API fallback was used.
- Limits: 24 calls, 500000 reported-token stop, two concurrent workers, two tasks,
  840-second internal deadline and a 900-second external process supervisor.
- Davis's direct instruction and the bounded interpretation are in allowance.json
  and [the coordination comment](https://github.com/Davisfox5/War-Room/pull/4#issuecomment-5776396007).
- Native macOS execution used existing subscription sessions and the existing
  per-role provider controls. No global permission setting changed. There was
  no Docker container, staged credential copy, production data or message send.
- The task and acceptance tests are unchanged from the earlier Q9 fixture. Only
  Python/test paths in the README, policy and launcher were adapted for the Mac.
  This is a native functional test, not a new baseline/candidate comparison.

## Independent evidence

Native Codex read-only and workspace-write reads matched the fixture, and a
workspace-write file probe succeeded without a model call. All three CLI auth
checks passed. An initial diagnostic invocation used unsupported local sandbox
helper arguments; correcting the command to the installed CLI's syntax resolved
that diagnostic error. The initial and successful receipts are both retained.

Before the run: `3 failed, 6 passed`. After the run: `9 passed, 1 warning in 0.15s`.
The controller gate also reports nine passing tests and checked the same selected
project. The warning is an existing Starlette deprecation.

The sole source diff adds `or record["tenant_id"] != tenant` to the missing-row
condition. The README, policy and external acceptance test hashes are unchanged
from the native preparation. Invocation rows reconcile exactly with the budget.
All seven retained text artifacts were reviewed. Native CLI tool transcripts
are not retained by this runtime; the source diff, immutable-test rerun and
controller receipts independently establish the result.

The recorded verifier response accepts the change and labels its fixture note
nonblocking. Replaying that exact response through the patched security task
produces no open finding, with zero model calls. The regression tests also keep
real BLOCKING and UNRESOLVED findings effective, including after a nonblocking
heading and on the same line. Structured verdict handling is unchanged.

The supervisor exited, recorded zero remaining owned processes, and required no
forced termination. No watcher was created or restarted. The project and
sanitized evidence are retained for inspection. Captured one-off scripts are saved as .py.txt transcripts with their original
bytes. They contain this run's absolute paths and are evidence, not a portable
rerun command.

## Offline checks for the status fix

```text
/tmp/quadratus-harness-env/bin/python -m pytest -q tests/test_security_verdict.py -k prose_heading
before repair: 3 failed, 6 passed, 29 deselected in 0.41s

/tmp/quadratus-harness-env/bin/python -m pytest -q tests/test_security_verdict.py tests/test_session.py tests/test_joint_review_verdicts.py
116 passed in 0.24s

/tmp/quadratus-harness-env/bin/python -m pytest -q tests/harness_pack/test_pack.py
36 passed in 0.26s

/tmp/quadratus-harness-env/bin/python -m pytest -q
1255 passed, 11 skipped in 52.30s

/tmp/quadratus-harness-env/bin/python -m ruff check quadratus/session.py tests/test_security_verdict.py
All checks passed!

git diff --check
exit 0, no output
```

An initial focused invocation named a nonexistent tests/test_harness_pack.py;
that invocation did not run tests. The corrected harness_pack/test_pack.py
command above and the full suite both passed. The post-fix live run was not run.

The first PR CI stopped at lint because these one-off transcripts were named
as Python modules. The .py.txt exports preserve their bytes and identify their
purpose as recorded evidence. Repository-wide `ruff check .` then passed; no
lint rule, gate or runtime source was excluded. CI is rerunning on that head.
