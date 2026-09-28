# Preview failure provenance (E1): where each failure comes from

Read-only review by Opus 5.5, done under root ruling 5862699144 on #25. No
engine edits were made. The incumbent Claude owns the implementation.

**Source reviewed:** `7d294ce91920adcf31bed204ae865fd93e34815c`
(`claude/quadratus-lead-request-parser-riq378`), specifically
`quadratus/preview.py`, `quadratus/session.py` (`_check_design`,
`_harness_capture`), `quadratus/readiness.py`, `quadratus/outcome.py` and
`quadratus/design_evidence.py` (`main`). I also read the E1 test from the
candidate `006bdbe` (`tests/test_workflow_evidence_boundary.py`), which is
not an ancestor of 7d294ce.

## TL;DR

- **Two origins are proven failures before launch, and only those two
  qualify as `operator`.** Either the port was already taken before
  `Popen`, or `Popen` itself raised `OSError`. In both cases the app never
  ran.
- **Everything else stays `invalid_proof`.** That covers every app exit
  (before readiness or as it became ready), the readiness timeout, the
  budget being spent before the capture, the capture timing out, and a
  non-zero capture exit. The structured facts can't separate app from
  environment in those cases, so the route is left alone and attribution
  stays unverified.
- **The E1 strict xfail on 006bdbe has the wrong fixture for an operator
  positive.** `raise SystemExit('Xvfb: cannot open display')` runs Python,
  so the app launches and exits 1. Under the ruling that is an app exit.
  Its operator expectation should become a negative control, with a
  separate positive that uses a real launch failure.
- **Readiness receipts can't attribute a preview failure.** No probe is
  bound to the capture profile, and probes only run at run start.
- **No repair calls are at stake.** Neither harness capture-failure path in
  `_check_design` spends a model call. E1 changes the class and the stop,
  not the repair budget.
- **Limitations:** I couldn't execute the recipes in this session because
  the sandbox needs approval, so the detail texts below come from reading
  the source and aren't observed output. The incumbent's `c868bf2` couldn't
  be fetched (a local fetch found no such object, and the GitHub API
  returned `422 No commit found`), so I haven't compared it. Both are
  listed under "Limitations" below.

## Source table

The line numbers are for `quadratus/preview.py` at 7d294ce unless another
file is named. The "Detail text" column is the format string exactly as the
source builds it. Today, every row reaches `_check_design` as a plain string
and is routed as `invalid_proof`, ending in `DesignUnverified` (see
`session.py:3300-3308` and `3335-3337` → `3381-3384`).

| # | Origin (raise site) | Did the app process run? | Structured fact available at the raise site | Detail text (byte form) | Ruling-compatible class |
|---|---|---|---|---|---|
| L1 | `running` 335-338: `subprocess.Popen` raised `OSError` | No, the process never existed | The `OSError` instance (`errno`, `filename`), plus the fact that the raise came from `Popen` | `the preview could not start: {exc}` | **operator**, if `argv[0]` is a named runner (see caveat L1a) |
| L2 | `running` 326-327: `_listening(host, port)` true before `Popen` | No, it was refused before launch | The harness's own connect probe succeeded before any launch | `something is already listening on {origin}; the preview was not started` | **operator** (see caveat L2a) |
| A1 | `running` 343-345: `proc.poll()` not None before readiness | Yes | `proc.returncode`. The log tail is prose. | `the preview exited with {rc} before it was ready: {log tail or "(no output)"}` | invalid_proof (preserved), attribution unverified |
| A2 | `running` 359-360: exit after the ready answer, before yield | Yes | `proc.returncode` | `the preview exited with {rc} as it became ready` | invalid_proof (preserved), attribution unverified |
| T1 | `running` 355-357: not ready by `ready_by = min(deadline, start + ready_timeout)` | Yes, still alive | Only that time passed. Status and redirect answers aren't kept. | `the preview was not ready at {origin}{ready_path} within {ready_timeout:g}s: {log tail}` | invalid_proof (preserved), attribution unverified |
| T2 | `capture_task` 397-399: `left <= 0` after readiness | Yes, and ready | Only that time passed | `the preview used the whole {total_timeout:g}s budget before the capture` | invalid_proof (preserved), attribution unverified |
| T3 | `capture_task` 407-409: `capture.wait` timed out | Yes, both processes | Only that time passed | `the capture did not finish within {left:.0f}s of the {total_timeout:g}s budget` | invalid_proof (preserved), attribution unverified |
| C1 | `capture_task` 410-411: capture returncode ≠ 0 | Yes, both processes | `capture.returncode`. The output tail is prose. | `the capture exited with {rc}: {output tail[-400:]}` | invalid_proof (preserved), attribution unverified |

These failures happen outside the `PreviewFailed` / string channel, and
they're recorded here so nobody re-routes them by accident:

| # | Site | Behaviour today |
|---|---|---|
| X1 | `running` 329-330: `tempfile.mkdtemp` / `_environment()` raises | This happens before launch and isn't caught by `capture_task`. It propagates to `session.py:2015-2016`, where `classify(exc)` is called. Because `OSError` has no row, it becomes **operator** already. Keep this. |
| X2 | `capture_task` 401: the capture's own `Popen` raises `OSError` | This isn't caught either, so the preview is stopped by `finally` and the error propagates. It's **operator** already, through the same path. Keep this. |
| X3 | `session.py:4220-4222`: capture ineligible | `the harness did not capture because {reason}`. No launch. The reason is the task's own check state, so it's not an operator case. Preserve. |
| X4 | `session.py:4228-4229`: source changed during preview/capture | `the project source changed while the harness previewed and captured it`. Something launched wrote source. Preserve. |
| X5 | `session.py:4224/4227`: `_verify_dependencies` | Raises `DependencyTreeChanged`, which is **integrity** already. Unchanged. |

### Why only L1 and L2

The ruling says a launch failure or a proven harness/environment failure may
go to operator only when structured facts establish that origin. L1 and L2
are the only sites where the harness can show that the application never
executed, because the fact comes from the harness's own call rather than
from anything the app printed. Every other row starts after a successful
`Popen`, and at that point:

- A returncode says the process ended. It doesn't say why. `127` from `npm`
  or a wrapper script looks like "command not found", but a launched process
  reported it.
- The log and output tails are prose. Matching `Xvfb`, `EADDRINUSE`,
  `cannot open display` or `request pinning is unavailable` would be prose
  parsing, which the ruling forbids.
- `design_evidence.main` returns 1 for a failed step (`design_evidence.py:633-634`).
  An uncaught exception in the capture also exits 1. That includes a
  harness-side failure such as `RuntimeError("request pinning is
  unavailable")` from `browser.render_page` (`tests/test_preview.py:686-704`),
  a missing Playwright, or a browser launch failure. Exit 2 means a
  `ValueError` (`design_evidence.py:619-631`), and that includes fixture
  problems a lead could have caused. Exit 1 therefore mixes an app finding
  with a harness failure, and exit 2 mixes a harness failure with a
  lead-caused one. Neither is proof.
- A timeout (T1–T3) fits a slow app, a wrong `ready_path` in the operator's
  own profile, a hung browser, and a busy machine equally well.

### Caveats on the two positives

- **L1a: a project-file `argv[0]`.** `profile_from_dict` (lines 189-195)
  accepts a named runner (`python…`, `node`, `npm`, `npx`, `uv`, `flask`,
  `pnpm`, `yarn`, bare or by absolute path) or an existing project file. If
  `argv[0]` is a project file such as `./serve.py`, then `ENOENT`, `EACCES`
  and `ENOEXEC` can be caused by a lead's edit: a deleted file, a lost
  execute bit, or a broken shebang. That's product origin that still shows
  up at `Popen`. For the operator class to rest on structure, it should
  require either that `argv[0]` matched `_RUNNERS`, or proof that the file
  is unchanged since dispatch. Without either, I'd preserve the route. The
  incumbent reported "conventional runner OSError attribution", which
  matches this narrowing. I haven't verified it (see Limitations).
- **L2a: who owns the listener.** The module docstring (lines 15-18) says a
  grandchild that leaves the process group (`setsid`) is outside what the
  preview controls. If an earlier harness preview in the same run started
  such a grandchild, and it still holds the port, the listener came from the
  app. The harness doesn't record who owns the listener. I'd still class L2
  as operator: the refusal happens before launch, no model edit can free
  the port, and the text already says `the preview was not started`. The
  structured record should keep only what was observed (for example
  `stage=before_launch, cause=port_in_use`) and never claim to know who the
  owner is.

### Readiness receipts don't supply provenance

`session._run_readiness` (2035-2060) runs the operator's probes once, before
any model call, and a failing probe raises `CapabilityProbeFailed`, which
goes to operator. A passing probe only adds its id to
`TaskContract.capabilities` (`session.py:2101, 2109`). No field ties a probe
to the capture profile or the preview's argv or environment. Probes also run
with `os.environ` (`readiness.py:147`), not the preview's `_environment()`
plus the profile env (`preview.py:330`). So:

- A passing probe doesn't prove that a later A1/T1 failure was the app's
  fault.
- A missing probe doesn't prove that it was the environment's fault.

Missing readiness attribution has to be recorded as explicitly unverified,
never as either side, which matches the ruling's no-repair boundary. Binding
a probe to the preview would need a new declaration, which is out of E1's
scope.

### Two call sites, one structured kind

A preview failure reaches `_check_design` at two places:

- `3300-3308`, the first capture. It opens `invalid_proof` directly.
- `3335-3337`, after the design fix. It rewrites the failure into
  `records=[dict(kind="integrity", message=failure)]` and then opens
  `invalid_proof` at 3381-3384.

Whatever structured kind `capture_task` returns has to reach both places.
The second one currently labels its record `integrity`, so if the kind is
only checked at the first site, an L1/L2 failure after a design fix would
keep today's route.

In neither place is a model repair call spent because of the capture
failure itself. The first site returns straight away, and the second is
already past its one design fix. E1 therefore changes the class and the
stop (`DesignUnverified` → operator handoff) for L1/L2 only.

## Test recipes

These are independent of the E1 fixture. They use real processes, patch no
decisions, and each one should assert the exact detail string (a byte-equal
`==`, not `match=`/`in`) as well as the class. `{p}` is a free loopback port
chosen by the test. `{tmp}` is the test's project root. `sys.executable` is
the interpreter running the tests. There is one outcome assertion per
recipe:

- **operator**: task `primary == "operator"`, `invalid_proof` not active,
  stop kind `operator`, and no `design-fix`, `gate-fix` or `design-review`
  call.
- **preserved**: `DesignUnverified: task t1…`, `invalid_proof` active, and
  the same absent calls.

In the run-level form, the detail must appear byte-identical inside the stop
text as well.

### Operator positives

- **P-L1 (runner that doesn't exist).** Profile
  `preview=["{tmp}/missing-bin/python3", "-V"]`, origin `http://127.0.0.1:{p}`.
  The absolute path passes validation because its basename matches
  `_RUNNERS` and existence isn't checked (preview.py:192). Expected detail:
  `the preview could not start: [Errno 2] No such file or directory: '{tmp}/missing-bin/python3'`.
  Expected class: operator.
- **P-L2 (port already taken).** The test binds and `listen()`s a socket on
  `127.0.0.1:{p}` before the run and keeps it open for the whole capture.
  Profile `preview=[sys.executable, "-m", "http.server", "{p}", "--bind", "127.0.0.1"]`.
  Expected detail:
  `something is already listening on http://127.0.0.1:{p}; the preview was not started`.
  Expected class: operator. Also assert that the test's listener still
  accepts a connection afterwards, as in `tests/test_preview.py:200-210`.

### Controls that must be preserved (negative)

- **N-A1a (silent app exit).** `preview=[sys.executable, "-c", "raise SystemExit(3)"]`,
  `ready_timeout=5`. Detail:
  `the preview exited with 3 before it was ready: (no output)`. Preserved.
- **N-A1b (app exit whose prose names the environment).** This is the 006bdbe
  E1 fixture:
  `preview=[sys.executable, "-c", "raise SystemExit('Xvfb: cannot open display')"]`.
  Detail: `the preview exited with 1 before it was ready: Xvfb: cannot open display`.
  Preserved. This is the no-prose-parsing control, and it replaces the
  current operator expectation in
  `test_a_preview_that_never_starts_is_an_operator_handoff`.
- **N-A1c ("command not found" status from a launched process).**
  `preview=[sys.executable, "-c", "raise SystemExit(127)"]`. Detail:
  `the preview exited with 127 before it was ready: (no output)`. Preserved.
- **N-A2 (exit as it became ready).** I haven't found a deterministic
  real-process recipe, because the second `poll()` at line 359 races the
  app's own exit. Test this at unit level only, with a real sleeper child
  whose `poll` is wrapped to report `5` on its second call after a ready
  answer served by the test. Detail: `the preview exited with 5 as it became ready`.
  Preserved.
- **N-T1 (never ready).** `preview=[sys.executable, "-c", "import time; time.sleep(60)"]`,
  `ready_timeout=1`. Detail:
  `the preview was not ready at http://127.0.0.1:{p}/ within 1s: (no output)`.
  Preserved.
  - Note that the text names `ready_timeout` even when `ready_by` was cut
    short by the overall `deadline` (line 341). That's a wording inaccuracy,
    recorded here and not asked to be fixed, because the detail text must
    stay byte-identical.
- **N-T2 (budget spent before the capture).** The project file `late.py` is
  a `BaseHTTPRequestHandler` on `{p}` whose `do_GET` sleeps 1.5 s and then
  answers 200. Profile: `ready_timeout=1, capture_timeout=1, total_timeout=1`.
  The ready request is accepted, blocks for about 1.5 s (its `open` timeout
  is 2 s) and returns 200 after the deadline, so line 398 fires. Detail:
  `the preview used the whole 1s budget before the capture`. Preserved.
- **N-T3 (capture timeout).** Serve with `http.server` and patch
  `preview.capture_argv` to `[sys.executable, "-c", "import time; time.sleep(30)"]`
  (the same mechanism as `tests/test_preview.py:241-249`), with
  `capture_timeout=1` and `total_timeout` left at its default, which is
  `ready_timeout + capture_timeout = 31`. Detail:
  `the capture did not finish within 1s of the 31s budget`. Preserved.
- **N-C1 (capture exit 1 whose prose names the harness).** `capture_argv` →
  `[sys.executable, "-c", "import sys; sys.stderr.write('request pinning is unavailable'); sys.exit(1)"]`.
  Detail: `the capture exited with 1: request pinning is unavailable`.
  Preserved.
- **N-C2 (capture exit 2).** Use the same approach with
  `'error: bad step'` and `sys.exit(2)`. Detail:
  `the capture exited with 2: error: bad step`. Preserved.

### Record-level controls

- **R1 (both call sites).** Run P-L1 through the post-design-fix capture:
  the first capture succeeds and shows a problem, then the design fix
  happens, and then the second preview gets the missing runner, for example
  by swapping the profile file between the two captures in the test's own
  environment. Assert that it gets the same class as P-L1. This covers the
  `kind="integrity"` rewrite at `session.py:3337`.
- **R2 (readiness doesn't upgrade).** Run N-A1b with a passing readiness
  probe declared, such as `[sys.executable, "-c", "pass"]`. Assert that the
  outcome is still preserved and that the preview failure's attribution is
  recorded as unverified. A passing probe must not turn it into either
  operator or product.
- **R3 (ruling proves no repair).** In every recipe above, assert no
  `design-fix`, `gate-fix` or `design-review` call, plus
  `[c.task for c in replay.of("lead")] == ["t1"]`. That shape is already
  used in `_dead_preview_run` on 006bdbe.

## Limitations

- **The recipes weren't executed.** In this session the sandbox needed
  approval to run Python and no one was available to grant it, so it was
  denied. Every detail string above comes from reading the format strings
  at 7d294ce, not from observed output. The OS-specific part is
  `str(FileNotFoundError)` in P-L1, which is `[Errno 2] No such file or
  directory: '<path>'` on CPython for both macOS and Linux. Whoever
  implements this should confirm each string by running it before pinning
  it with `==`. The scratch runner I prepared is
  `/private/tmp/opus-e1-recipes.py`, which is local and not committed.
- **I didn't compare against the incumbent's `c868bf2`.** A local
  `git fetch` of the incumbent branch had no such object, and
  `gh api repos/Davisfox5/quadratus/commits/c868bf2` returned `422 No commit
  found`, so it hadn't been pushed at the time of writing. The report says
  it gives structured attribution for port-listening and for a conventional
  runner's OSError. That would be L2 and L1 with caveat L1a, which is
  consistent with this table, but I haven't verified it. A reviewer of
  c868bf2 should check four things: that A1/A2/T1-T3/C1 are unchanged, that
  both `_check_design` sites carry the kind, that the detail bytes are
  unchanged, and that no field infers blame from a returncode or from tail
  text.
- **Scope.** This review covers `capture_task` and its callers only. I
  didn't read other `design_evidence.capture` internals beyond `main`'s
  exit codes, and I didn't read `browser.py`.
