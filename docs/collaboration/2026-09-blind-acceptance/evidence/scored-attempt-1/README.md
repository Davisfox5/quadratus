# Scored attempt 1 — saved incomplete result

Run b7ccbd24 started 2026-09-15 17:24:15 UTC. Frozen runtime a001c1b,
application base a8772ab, image and precommitted private archive hash are in
../scored-attempt-1-freeze.json. Source-after is the exact preserved app output,
not a completed feature or an evaluator repair. The original GameTape branch
was not modified. No automatic continuation occurred.

## Outcome

The controller stopped after **438.94 seconds / three provider attempts** at
its reported-token threshold. It recorded **614,386 tokens** (594,188 input,
20,198 output); the already in-flight closeout call caused a **114,386-token
overshoot** of the 500,000 post-return threshold. There were no unknown-usage
attempts, and no fourth attempt. The external supervisor removed the container;
temporary credential seeds were deleted. The full feature is incomplete.

| Role | Model | Reported tokens | Seconds |
| --- | --- | ---: | ---: |
| Planning | Fable 5.1 | 65,565 | 36.92 |
| First backend implementation | Grok default (envelope: grok-4.6-build) | 290,408 | 210.65 |
| Closeout record | Grok default (same resolved envelope model) | 258,413 | 189.05 |

No controlled workers, OpenAI seats, Opus review or native children were
observed. Claude's envelope additionally reports **2,817 Haiku auxiliary
tokens**, outside the controller's total; this was not a requested worker.
That is a budget-accounting limitation to audit, not a zero or free call.
The envelopes report 5 Claude turns and 10/8 Grok model calls, distinct from
Quadratus's three provider attempts. Cost fields are counterfactual/vendor
telemetry, not proof of billed API spend; auth was subscription-only.

Saved work: required-column CSV validation endpoint plus two Python test
functions, 132 added lines across app.py and tests/test_import_preview.py.
Player matching, Clip ID/duplicate handling, UI and CSV docs are absent. The
132 lines are below the existing 1.5x tolerance on the 100-line declared scope;
the scope records are not a new scope-stop failure.

The application integration gate passed 102 tests. Codex independently
re-extracted this archive, verified every source hash, and reran pytest in an
offline, credential-free container: **102 passed in 2.07s**, source unchanged.
Existing frontend files are unchanged. This checks regression preservation,
not the whole feature. Private-case score is pending Claude's independent run.

## Review and reproduction

1. Fetch codex/blind-worker-acceptance and verify artifact-sha256.json.
2. Extract source-after.tar.gz into a new scratch directory (source/ root);
   verify its 31 files against source-after-sha256.json. Do not overlay either
   developer checkout or modify the archive/private tests.
3. Run python -m pytest -q with the app dependencies. For independent private
   scoring, point GAMETAPE_ROOT to that extracted source and use the original
   committed private check_private.py bundle (SHA-256 71cb8b06a34ed94d6936bd9dfc45ed3889a0f6c5a80be1424850f1140e66371a).
4. Audit invocations.jsonl, budget.json, vendor-usage-extract.json and the
   closeout prompt/response. Explain why a record-writing step re-inspected the
   application for eight Grok model calls and 258,413 tokens, and whether the
   untouched scope write grant contributes. Separate observations from causes.
5. Report correctness, routing/worker coverage, control behavior and usage
   separately. Recommend a small repair batch; do not implement the missing
   app feature, change private cases, expand model roles or launch another run.

Raw vendor envelopes and fresh-HOME sessions remain private; their hashes are
published. All exports are from this attempt only. The private archive was
never opened by Codex and was never mounted to the solver. The original
result/ledger are preserved verbatim even where they omit auxiliary activity.
