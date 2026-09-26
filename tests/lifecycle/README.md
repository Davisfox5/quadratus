# Lifecycle replay matrix

Label: **controller determinism, not live reliability.**

`harness.py` replaces `quadratus.cli_providers._launch`, the one place
Quadratus starts a vendor CLI, and nothing above it. A scripted responder
answers each launch in the vendor's own envelope (claude JSON, codex JSONL,
grok JSON, each with known usage) and may edit the working directory as an
editing call would. So argv building, the extractors, the usage meters, Fleet's
per-call CHANGED check, the session lifecycle, workers, the integration gate
(real `pytest` on a two-function project), the design check and the close-out
all run for real. No vendor call, network, credential or limit change.

Each case is its own test, so one failure never hides another.

```bash
pytest -q tests/lifecycle
```

## Coverage

| Boundary | Case | Would have caught |
| --- | --- | --- |
| worker → FETCH → edits → review BLOCKING → revision → recheck → gate → close-out → next task | `test_the_whole_task_lifecycle_in_every_request_layout` ×4 | Run 9 (newline JSON), Run 11 (request glued to a sentence) |
| quoted request example inside a delivery | `test_a_quoted_request_example_in_a_delivery_is_a_draft` | review of 03be7e3 |
| inherited task edits vs this call's edits | `test_a_revision_that_repeats_earlier_files_is_rejected`, `..._with_no_edits_and_an_empty_declaration_proceeds` | source-truth check stays exact |
| stale renders re-captured with no source edits | `test_stale_renders_are_recaptured_without_source_edits` | Run 12 (design-fix CHANGED mismatch) |
| design-fix repeating earlier files | `test_a_design_fix_that_repeats_the_tasks_files_is_rejected` | still rejected |
| render of an unrelated page | `test_renders_of_an_unrelated_page_are_an_open_finding` | Run 12 grade (scripted verdict, see below) |
| render freshness boundary | `test_render_freshness_is_decided_by_timestamp_not_write_order` | frozen-image timing flake |
| Claude cap envelope | `test_a_claude_lead_at_its_cap_hands_back_partial_work` | capped continuation |
| Claude error with `num_turns` above the cap | `test_a_claude_failure_past_the_turn_count_is_not_read_as_a_cap` | 818ecd0; and the empty error message |
| Grok cancelled at the cap | `test_a_grok_lead_cancelled_at_the_cap_is_the_cap` | Run 3 |
| Grok cancelled before the cap | `test_a_grok_cancel_before_the_cap_is_a_failure_recovered_once_on_an_unchanged_tree` | lead recovery, once |
| Grok error field at the cap's count | `test_a_grok_error_at_the_cap_count_is_a_failure_not_a_continuation` | Grok review of #33 (error read as the cap, message dropped) |
| close-out safeguard refusal | `test_a_refused_closeout_keeps_a_harness_record_and_the_run_continues` | Run 10 |
| lead safeguard refusal | `test_a_refused_lead_is_not_retried_or_rerouted` | refusal preserved |
| planner and continuation told to count test setup | `test_the_planner_is_told_to_count_test_setup_and_split_heavy_setup` | Run 13 (prompt only) |
| continuation within its estimate, tests and setup counted | `test_a_continuation_sized_for_its_tests_and_setup_proceeds` | |
| continuation whose tests overrun | `test_a_continuation_whose_tests_overrun_still_stops_with_work_preserved` | ceiling unchanged, tests not trimmed |
| interactive capture with a failed step | `test_a_capture_whose_interaction_step_failed_leaves_the_design_unverified` | a failed step reaching the check (real-browser cases in `tests/test_design_interaction.py`) |
| clean render with no steps | `test_a_clean_render_without_steps_still_verifies` | not a defect: static design tasks stay legal |

On fdee0d8 (Run 9's base) eight cases fail: the three non-single-line
request layouts, all three design cases, the Claude error case and the
refused close-out. The single-line layout and the refusal, cap and recovery
cases pass there too, as they should.

**Run 13 is not closed by the harness.** Run 13 captured no steps, and a
clean no-step render still verifies, on purpose: the check does not require
steps. The failed-step case proves a failed step is caught once a lead
captures one. Whether a lead captures the changed state is asked by the
prompt and judged by the final review and the independent grader.

## What the scripted cases can and cannot prove

- The gate is real: `pytest` runs with this test's own interpreter
  (`harness.GATE`), and each passing case asserts every recorded check
  PASSED. Cases whose task does not implement `add` start from a passing
  fixture, so their gate result is about the case.
- Render timestamps are set explicitly (`harness.STALE` / `harness.FRESH`),
  never left to the write clock; the production freshness check is unchanged.
- The unrelated-page case scripts the reviewer's BLOCKING verdict. It proves
  that verdict becomes an open finding and that the prompts ask for the
  feature's state. It does not prove a model recognises an unrelated image;
  live browser and feature-state acceptance still decides that.

## Not covered yet

- A lead reply that is itself a quoted CONSULT or WORKER example at line
  start (the line parser's own boundary; unit-tested in `test_lead_request.py`).
- Parallel batches (`max_parallel_tasks > 1`) and forked Fleets.
- Security excursions and consults.
- Budget stops (`RunBudgetExceeded`) mid-lifecycle; unit-tested elsewhere.
- A Grok cancel after a write (by inspection `PartialWorkStopped`).
- A launch that exits nonzero: `fake_launch` always exits 0.
- In-session MCP worker tool calls: the harness answers at the CLI boundary,
  so it cannot run the tool loop inside a vendor turn.
