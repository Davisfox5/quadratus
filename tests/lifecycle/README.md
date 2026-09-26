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
| Grok `refusal` / `content_filter` stop, below / at / above the cap, unchanged / changed tree | `test_a_grok_decline_is_one_lead_at_any_count_on_any_tree` ×12 | Codex review of f7548a2 (a decline handed to another lead) |
| planner and continuation told to count test setup | `test_the_planner_is_told_to_count_test_setup_and_split_heavy_setup` | Run 13 (prompt only) |
| continuation within its estimate, tests and setup counted | `test_a_continuation_sized_for_its_tests_and_setup_proceeds` | |
| continuation whose tests overrun | `test_a_continuation_whose_tests_overrun_still_stops_with_work_preserved` | ceiling unchanged, tests not trimmed |
| interactive capture with a failed step | `test_a_capture_whose_interaction_step_failed_leaves_the_design_unverified` | a failed step reaching the check (real-browser cases in `tests/test_design_interaction.py`) |
| clean render with no steps | `test_a_clean_render_without_steps_still_verifies` | not a defect: static design tasks stay legal |
| two caps in a row | `test_two_caps_in_a_row_stop_with_a_named_breaker_and_the_work_kept` | Run 14 (blank `result.error` on a breaker stop) |
| round budget stated / not stated | `test_a_capped_lead_is_told_its_round_budget`, `test_no_round_budget_is_stated_without_a_cap` | Run 14 (no lead knew a cap existed) |
| first lead's scoped files | `test_the_first_lead_is_handed_its_scoped_files_as_they_are` | Run 14 t1 (27 discovery calls, no write) |
| zero-write discovery cap, then continuation | `test_a_zero_write_cap_hands_its_continuation_the_files_and_says_so` | Run 14 t2 (repeated discovery) |
| capped edits, then continuation | `test_capped_edits_reach_the_continuation_as_the_tree_now_has_them` | handoff shows the tree now, not a transcript |
| continuation with test setup in scope | `test_a_continuation_is_handed_its_existing_test_setup` | Run 14 t2 (re-read test helpers) |
| secret, hidden, symlinked, pattern paths in a scope | `test_secret_hidden_and_linked_files_in_a_scope_never_reach_a_prompt` | content never shown; path and reason only |

On fdee0d8 (Run 9's base) eight cases fail: the three non-single-line
request layouts, all three design cases, the Claude error case and the
refused close-out. The single-line layout and the refusal, cap and recovery
cases pass there too, as they should.

**Run 13 is not closed by the harness.** Run 13 captured no steps, and a
clean no-step render still verifies, on purpose: the check does not require
steps. The failed-step case proves a failed step is caught once a lead
captures one. Whether a lead captures the changed state is asked by the
prompt and judged by the final review and the independent grader.

**Run 14's discovery cases prove the handoff, not the behaviour.** They pin
what the harness puts in a lead's prompt (the round budget, the capped
predecessor's record, the scoped files read fresh with a hash) and how a
breaker stop is reported. They do not prove a model will read less, write
earlier or finish inside its cap; only a live run shows that.

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
