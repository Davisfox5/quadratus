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
| inherited task edits vs this call's edits | `test_a_revision_that_repeats_earlier_files_is_recorded_and_the_run_continues`, `test_an_undeclared_edit_is_recorded_and_still_measured`, `..._with_no_edits_and_an_empty_declaration_proceeds` | the harness diff decides; the declaration is a recorded fact (phase-4 run on ea464cc) |
| stale renders re-captured with no source edits | `test_stale_renders_are_recaptured_without_source_edits` | Run 12 (design-fix CHANGED mismatch) |
| design-fix repeating earlier files | `test_a_design_fix_that_repeats_the_tasks_files_is_recorded_and_reviewed` | recorded, review still runs |
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
| cap with no detected change (reading, denied write, checks only, edit then revert), then continuation | `test_a_cap_with_no_detected_change_hands_over_the_files_and_claims_nothing_more` ×4 | Run 14 t2 (repeated discovery); Codex review of 895cf67 (activity was inferred) |
| design evidence left unverified | `test_a_capture_whose_interaction_step_failed_leaves_the_design_unverified` | Run 15 (blank `result.error`; now `DesignUnverified: ...`) |
| declared Node suite beside the operator's pytest: passes / fails / runner missing / not declared | `test_a_declared_node_suite_runs_beside_the_operator_check`, `test_a_failing_declared_node_suite_fails_the_gate`, `test_a_missing_node_runner_blocks_the_gate`, `test_a_project_without_a_declared_script_keeps_the_single_check` | Run 15 (Node UI tests never ran in-run) |
| operator extra check beside pytest: passes / fails / runner missing / pattern refused | `test_an_operator_extra_check_runs_beside_the_check_and_stays_out_of_prompts`, `test_a_failing_extra_check_fails_the_gate_even_with_pytest_passing`, `test_an_extra_check_with_a_missing_runner_is_blocked`, `test_a_pattern_or_empty_extra_check_is_refused_before_any_call` ×4 | Run 15 (a prose-documented Node suite; no package.json) |
| declared review-only UI task (`"edits": "none"`) vs a one-line fix and a task with an edit budget; a mislabelled audit that edits | `test_a_one_line_ui_fix_is_editing_work_not_an_audit`, `test_a_mislabelled_audit_that_edits_is_still_measured` | Codex review of 3d5c3f3 (max_lines alone is not intent) |
| a later non-UI task's reviewers; an incomplete evidence transfer; capture before an edit in the same call | `test_a_later_non_ui_tasks_reviewers_get_no_earlier_renders`, `test_an_incomplete_evidence_transfer_blocks_the_review_instead_of_claiming_it`, `test_a_capture_taken_before_an_edit_in_the_same_call_is_stale` | Codex review of 3d5c3f3 (cross-task leak, silent skips, capture order) |
| capture after the revision's own edit; consecutive design tasks | `test_a_capture_after_the_revisions_edit_is_fresh`, `test_consecutive_design_tasks_each_need_their_own_renders` | Codex review of 3d5c3f3 |
| review-only UI task (max_lines 1) vs a task with an edit budget | `test_a_review_only_ui_task_is_told_to_report_not_repair`, `test_a_ui_task_with_an_edit_budget_is_still_told_to_repair`, `test_a_review_only_task_still_needs_evidence_and_keeps_its_findings` | Run 16 (an audit told to fix what it saw) |
| revision that changes nothing vs one that changes source, after a capture | `test_an_unchanged_revision_keeps_fresh_renders`, `test_a_changed_revision_still_invalidates_earlier_renders` | Run 16 (every write-enabled call reset freshness) |
| capture fixture under .quadratus/capture-fixtures/<task> | `test_a_capture_fixture_is_harness_state_not_a_source_change` | Run 16 (fixture made in tests/, then deleted) |
| design reviewer reads the renders in its own copy | `test_the_design_reviewer_can_read_the_renders_in_its_copy` | Run 16 (reads outside the copy denied) |
| tail edit in a 1,600-line file, then continuation | `test_a_tail_edit_in_a_long_file_reaches_the_continuation_as_a_window` | Codex review of 895cf67 (a prefix hid the edit) |
| capped edits, then continuation | `test_capped_edits_reach_the_continuation_as_the_tree_now_has_them` | handoff shows the tree now, not a transcript |
| revision / gate-fix / design-fix below, at and above the cap, unchanged and changed tree; out-of-scope capped fix; refusal and denial precedence; reviewers, close-out and orchestrator uncapped | `test_dependency_identity.py`: `test_a_capped_revision_takes_the_capped_task_path` ×4, `test_a_capped_gate_fix_*`, `test_a_capped_design_fix_*`, `test_a_refused_gate_fix_keeps_the_refusal`, `test_a_gate_fix_denied_the_harness_command_at_the_cap_is_a_capability_stop`, `test_reviewers_closeout_and_the_orchestrator_are_not_capped` | Run 19 (uncapped gate-fix and design-fix past 14 rounds) |
| capture-steps line for the orchestrator | `test_the_orchestrator_is_told_when_steps_are_empty_and_that_they_never_replace_a_finding` | Run 19 (visible-at-load final wait on a favicon task) |
| dependency tree created, edited, nested, deleted by an editing call; written by a check, by a preview, by an operator-declared cache; a shim the check would resolve; over the bound at run start; changed between calls; changed by a capped call | `test_dependency_identity.py` (D section) | Run 19 (a model-written `node_modules` shim the source checks could not see) |
| continuation with test setup in scope | `test_a_continuation_is_handed_its_existing_test_setup` | Run 14 t2 (re-read test helpers) |
| secret, hidden, symlinked, pattern paths in a scope | `test_secret_hidden_and_linked_files_in_a_scope_never_reach_a_prompt` | content never shown; path and reason only |
| review-only audit measuring overflow: clean / overflow recorded as a finding and repaired / later clean recapture / two findings closed one at a time | `test_a_clean_audit_records_nothing_and_the_run_can_finish`, `test_an_overflow_audit_becomes_debt_and_a_repair_resolves_it`, `test_a_later_clean_recapture_audit_can_resolve_a_finding`, `test_two_findings_on_shared_requirements_close_one_at_a_time` | Run 17 (a real 450px mobile overflow found by an audit, with no way to schedule its repair) |
| audit with any integrity or unclean-page problem; ledger off; audit that edits; refused audit lead | `test_overflow_with_any_integrity_or_page_problem_is_todays_stop` ×3, `test_an_unclean_page_alone_is_todays_stop`, `test_with_the_ledger_off_an_overflow_audit_is_todays_stop`, `test_an_audit_that_edits_source_is_still_a_scope_stop`, `test_a_refused_audit_lead_is_todays_refusal_stop` | only measured overflow becomes debt; every other stop is unchanged |
| RESOLVES unknown, empty, duplicate, or without its requirements in COVERS | `test_an_invalid_resolves_is_sent_back_before_any_lead_call` ×3, `test_a_resolves_without_its_requirements_in_covers_is_sent_back` | sent back before any lead call |
| a finding kept open: non-UI repair, another target, undelivered evidence, overflow left, unrelated covering task, source changed after resolution | `test_a_non_ui_task_cannot_resolve_an_overflow`, `test_clean_renders_of_another_page_do_not_resolve`, `test_approval_without_delivered_evidence_resolves_nothing`, `test_a_repair_that_leaves_the_overflow_stops_as_today`, `test_an_unrelated_covering_task_leaves_an_owed_requirement_unmet`, `test_a_source_change_after_resolution_reopens_the_finding_at_done` | a finding closes only on fresh, delivered, same-target measurement |
| resolution bound to the measured state: same steps resolve / same URL, other state does not; two editing repairs reopen the first until rechecked; capped repair then continuation | `test_a_recapture_in_the_measured_state_resolves_the_finding`, `test_a_recapture_of_the_same_address_in_another_state_does_not_resolve`, `test_two_editing_repairs_reopen_the_first_until_its_state_is_rechecked`, `test_a_capped_repair_leaves_the_finding_open_until_a_continuation_resolves_it` | Codex worker review of 28cfc05 (Run 17's list, dialog and preview share one URL) |
| task cap with an open or stale-resolved finding; a later refused lead; mixed or repeated RESOLVES | `test_the_task_cap_with_an_open_finding_is_a_named_stop`, `test_a_stale_resolution_at_the_final_slot_is_a_named_stop`, `test_findings_are_kept_when_a_later_lead_is_refused`, `test_an_invalid_resolves_is_sent_back_before_any_lead_call` ×5 | Codex worker review of 28cfc05 (blank error at the cap, a second RESOLVES line dropped) |
| resolving renders replaced after review (other state / changed bytes); failed gate in a resolving task; RESOLVES on a non-UI task; wrong-state resolution stops with no second repair | `test_resolving_renders_replaced_after_review_reopen_at_done` ×2, `test_a_failed_gate_keeps_the_finding_open_and_stays_the_primary_stop`, `test_a_non_ui_task_naming_resolves_is_sent_back_before_its_lead`, `test_clean_renders_of_another_page_do_not_resolve`, `test_a_recapture_of_the_same_address_in_another_state_does_not_resolve` | Codex review of e47c7ed (false completion; a failed gate closed debt; a second repair ran) |
| audit evidence not deliverable; screenshot width inconsistent with the measured width vs a genuine full-page overflow; scope stop in a resolving task; parallel COVERS while a finding is open | `test_an_audit_whose_evidence_cannot_be_delivered_creates_no_debt`, `test_screenshot_and_measured_width_must_agree_for_debt` ×2, `test_a_scope_stop_in_a_resolving_task_is_written_into_the_finding`, `tests/test_parallel_tasks.py::test_a_parallel_batch_does_not_cover_a_requirement_owed_to_an_open_finding` | Codex review of e47c7ed |
| renders, source or state changed between approval and settlement; exception after a later source change vs an exception with source unchanged | `test_renders_changed_between_approval_and_settlement_do_not_resolve` ×3, `test_an_exception_after_a_source_change_reopens_an_earlier_resolution`, `test_an_exception_with_source_unchanged_keeps_a_valid_resolution` | Codex review of dd17a1d (settled hash differed from the approved one; a stale resolution persisted on an exception exit) |
| renders changed while the review is in flight (screenshot / summary); a file changed between snapshot and copy; a failed re-check on an exception exit | `test_renders_changed_while_the_review_is_in_flight_do_not_resolve` ×2, `test_a_file_changed_between_snapshot_and_copy_is_not_delivered`, `test_a_failed_recheck_on_an_exception_exit_distrusts_resolutions` | Codex review of ee7e62b (post-return snapshot; failed bookkeeping kept a resolution) |
| harness-owned capture from an operator profile (real preview, real browser): audit finds a real overflow, a repair resolves it; a clean page; a design-fix recaptured | `test_the_harness_captures_the_audit_and_the_repair_and_resolves_the_debt`, `test_a_clean_page_needs_no_repair`, `test_a_design_fix_that_edits_is_recaptured_by_the_harness` (`test_harness_capture.py`) | Run 18 (both repair leads capped on denied capture commands) |
| capability stops: no profile and a lead that cannot capture (before any call); a denied harness command (after the one call); an incidental denial (recorded); refusal outranks denial; UI task without a capture; a repair declaring another state; an unusable profile; a preview that writes source; a failed gate starts no preview | `test_without_a_profile_a_lead_that_cannot_capture_is_never_invoked`, `test_a_denied_harness_capture_stops_the_task_after_its_one_call`, `test_an_incidental_denial_is_recorded_and_the_run_goes_on`, `test_a_refusal_outranks_a_denial`, `test_with_a_profile_a_ui_task_without_a_capture_is_sent_back`, `test_a_repair_declaring_another_state_is_sent_back_before_its_lead`, `test_an_unusable_profile_stops_before_any_model_call`, `test_a_preview_that_writes_source_leaves_the_design_unverified`, `test_a_failed_gate_means_no_capture_and_the_finding_stays_open` | Codex amendments 5852185725 |
| an upload-and-wait state across preview restarts from audit to repair (task-owned fixtures); another task's fixture; a preview that writes source while shutting down | `test_an_interaction_state_survives_preview_restarts_from_audit_to_repair`, `test_a_harness_capture_may_upload_only_its_own_fixture`, `test_a_preview_that_writes_source_as_it_shuts_down_is_caught` | Codex clarifications 5852188936, 5852254888 |
| a capped call denied the harness command (capability stop, no continuation); a malformed denial field (ignored, usage kept) | `test_a_capped_call_denied_the_harness_command_is_a_capability_stop`, `test_a_malformed_denial_field_changes_nothing` | Codex review of 3a55d82 (unit cases for redirects, fixture syntax, argument links, bounded log, capture descendants and module shadowing are in `tests/test_preview.py`) |

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
earlier or finish inside its cap; only a live run shows that. A file too long
to show whole is shown as a marked prefix, or, where a capped predecessor
changed it, as windows around those changes; code elsewhere in a long file
still has to be read by the lead.

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
- The replayed leads write renders themselves, which models a lead that can run
  the capture; `harness.run(lead_runs_commands=True)` says so. The capability
  cases set it False (the real claude fact) and pin a claude lead.
- In-session MCP worker tool calls: the harness answers at the CLI boundary,
  so it cannot run the tool loop inside a vendor turn.
