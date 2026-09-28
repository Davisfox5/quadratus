# Native GameTape trial, 2026-09-22

The application passes its checks, but Quadratus did not reach controller completion. Twelve user-authorized native attempts preserved useful work across eleven reported-token stops and one scope-estimate stop. A new capability-inference defect refused a read-only review worker; live continuation is paused for Davis to review the proposed fix.

## Result and provenance

- Runtime: `fdd00d58ffeb348f032bf88f306212701e688f62`, including the pending #23 security-verdict correction.
- Application base: `a8772ab`, the repaired GameTape acceptance checkout. The original `main` checkout was left unchanged.
- Application result: local branch `codex/quadratus-full-20260922`, commit `1cd9264edb4429f00cde43a04a1944d0dca37f11`. No application push, merge or deployment occurred.
- Task: the existing read-only CSV clip-manifest preview contract, through `run_project`, adversarial mode, up to 20 tasks.
- Per attempt: 24 transport attempts, 500,000 reported-token stop threshold, 840-second internal deadline, 900-second external supervisor, two concurrent Quadratus workers.
- Transport: existing authenticated subscription CLIs. API keys were removed from the launcher environment, all API settings were None, and API fallback was disabled.
- Native delegation used the vendor-default setting, not the earlier strict-off canary configuration. No native child-model invocations were recorded. This does not prove that every possible vendor-internal action is observable.
- Davis explicitly authorized automatic fixes and continuations for known blockers, and pausing between runs for a new issue needing attention. No role, permission, per-attempt limit or scope-enforcement relaxation was used.

There were **32 transport calls**, **9,049,489 reported primary tokens**, and **62.59 minutes** inside the live supervisors. Token counts include normalized provider input, including cached input. They are not API charges; auxiliary-model diagnostics have uncertain overlap and are not added to this total. In-flight calls can overshoot the threshold. The largest attempt reported 1,694,362 tokens despite the 500,000 stop threshold.

Fable, Grok and Sol were invoked. Opus was selected for collaboration but never invoked before a stop. No Quadratus worker was invoked. The review lead emitted a WORKER request, the guard refused it, and the lead then performed the review itself. Early progress messages calling that active process a worker were corrected after reading the invocation record.

## Application validation

Final frozen application checks:

- 118 Python tests passed.
- 27 JavaScript UI tests passed; JavaScript syntax check passed.
- 21 independent backend contract cases passed, outside the model-editable project.
- Ten real-browser scenarios passed: upload and row counts, escaped HTML, empty and error responses, same-file reselection, clearing stale results during loading, close/newer-file/project-switch response races, and focus restoration.
- M opened the chooser. The file input has an accessible label and tabindex -1. The preview layout preserved the video surface.
- The browser used a loopback Flask server with disposable synthetic data. The final server's app hash matched the tested file; the project store was byte-identical before and after previews, with zero clips created.
- The original test files and helpers remained unchanged. `git diff --check` passed.

Receipts: [application gate](application-checks.txt), [independent backend checks](independent-checks.txt), [browser results](browser-result.json), [store and served-source hashes](browser-immutability.json). Independent check scripts are retained as `.txt` transcripts so repository test discovery and lint do not execute operator evidence.

All application edits were produced by Quadratus. Codex supplied independent checks and specific failure examples between attempts. Thus this is one supervised continuation sequence, not twelve independent reliability trials and not an autonomous full-run success.

## Stops and preserved progress

| Attempt | Run | Calls | Reported tokens | Stop |
| --- | --- | ---: | ---: | --- |
| 1 | 20260922T143146Z-8d050d50 | 2 | 564,004 | token threshold |
| 2 | 20260922T143902Z-46e1191f | 2 | 683,820 | token threshold |
| 3 | 20260922T145004Z-381d2f88 | 2 | 531,024 | token threshold |
| 4 | 20260922T145508Z-41b6fe12 | 4 | 582,604 | token threshold |
| 5 | 20260922T150048Z-0d2257a9 | 2 | 1,694,362 | token threshold |
| 6 | 20260922T151306Z-16ab7bab | 2 | 391,550 | scope estimate |
| 7 | 20260922T151725Z-197dfe21 | 4 | 643,887 | token threshold |
| 8 | 20260922T152214Z-de9f8d06 | 2 | 760,554 | token threshold |
| 9 | 20260922T152855Z-ae22ae53 | 4 | 585,146 | token threshold |
| 10 | 20260922T153242Z-1393bc4b | 2 | 560,997 | token threshold |
| 11 | 20260922T153953Z-4e1e3e30 | 3 | 505,853 | token threshold |
| 12 | 20260922T154605Z-2c275237 | 3 | 1,545,688 | token threshold |

The preserved work progressed through endpoint parsing, players, duplicate history, frontend, CSS, keyboard handling, UI regression tests, documentation and cumulative review. Scope underestimation counted full changed lines, including re-indentation and tests. Attempt 6 requested about 75 lines and produced 127, exceeding its 112-line tolerated bound. The patch stayed within its permitted files and was preserved for inspection. The first attempt also underestimated implementation plus tests, but the token stop happened before normal scope acceptance.

The final lead review found and fixed repeated ignored-header width handling, whitespace-only row handling, the Notes UI documentation claim and the RFC link. It reported no remaining contract defects and passed the gate. Its response was preserved after the token stop; it did not become a completed controller task or an Opus review.

## New harness defect and proposed repair

The exact read-only request included `bulk-edit` as a review topic and ended with `Do not edit files or run commands.` The edit detector matched both uses as positive requests, combined them with source paths elsewhere in the text, and inferred `patch`. The guard rejected `needs: []`, `write: false` and suggested `write:true`. That suggestion was inappropriate for the requested read-only work. No write grant was added and no worker call was spent.

The proposed change is limited to `quadratus/task_kinds.py` and existing tests. It recognizes direct negative edit phrases and the observed use of bulk-edit as a topic. Separate positive edit instructions, imperative `Bulk-edit app.py`, `Re-write app.py`, explicit needs and command-execution requirements retain their existing checks. Worker capabilities, patch application, permission boundaries and model routing are unchanged. Ambiguous language remains conservative; this is not a general natural-language intent parser.

The fix branch starts from `36ab9b6d2694e748f4d831c03eed640ae421124e`, independently of pending #23. No live trial used this proposed fix.

Offline verification:

```text
/tmp/quadratus-harness-env/bin/python -m pytest -q tests/test_capability_matching.py tests/test_worker_tool_fit.py
6 failed, 59 passed in 0.15s  # expected reproduction before the fix

/tmp/quadratus-harness-env/bin/python -m pytest -q tests/test_capability_matching.py tests/test_worker_tool_fit.py tests/test_worker_preflight.py tests/test_worker_loops.py tests/test_task_kinds.py
109 passed in 0.16s

/tmp/quadratus-harness-env/bin/python -m pytest -q
1258 passed, 11 skipped in 53.67s

/tmp/quadratus-harness-env/bin/ruff check .
All checks passed!

git diff --check
exit 0, no output
```

Full receipts: [red reproduction](regression-before.txt), [targeted](regression-targeted.txt), [full suite](regression-full.txt). Existing skips remain skips; no hosted/live reliability test ran on the proposed patch.

## Cleanup and remaining decision

Every supervisor recorded zero remaining owned processes and no forced termination. The operator's named browser and loopback Flask server were closed. No watchers, Docker containers, credential seeds, production database calls, outbound campaigns or deployment were created for this trial.

Davis's next decision is whether to use the tested guard correction in a new native continuation. The remaining controller work is the independent Opus review and a fresh complete close-out. The repeated usage stops also remain an efficiency problem; the parser correction alone does not solve them.
