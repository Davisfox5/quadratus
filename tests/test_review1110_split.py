"""quadratus/session.py was split on 2026-11-11 by moving code, text unchanged.

The split was redone on 2026-11-12, after the development line was merged
into the file. What it promised, held here so a later move cannot break it:
every name quadratus.session had at 6181300 is still there, each moved name
is one object in both places, no module the code moved to loads
quadratus.session, and no moved code reads a name a test replaces on the
session module. Four older tests scan session.py's own text for a banned
pattern; the same scans run here over the modules the code moved to, so
they still cover it.
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from quadratus import session as S

ROOT = Path(__file__).resolve().parents[1]

#: The modules session.py's code moved to.
MOVED = ("session_types", "reply_parsers", "session_prompts", "check_facts", "session_checks",
         "session_design", "session_audit", "session_seat_prompts", "session_security",
         "session_packets", "session_dispatch", "session_evidence", "session_obligations",
         "session_done", "session_recovery", "session_admission", "session_findings",
         "session_closeout")

#: The classes Session inherits its moved methods from, one per mixin module.
MIXINS = {"ChecksMixin", "DesignMixin", "AuditMixin", "SeatPromptsMixin", "SecurityMixin",
          "PacketsMixin", "DispatchMixin", "EvidenceMixin", "ObligationsMixin", "DoneMixin",
          "RecoveryMixin", "AdmissionMixin", "FindingsMixin", "CloseOutMixin"}

#: Every name quadratus.session had at 6181300, private and imported ones
#: included, written out by a script from that commit's module.
NAMES_AT_START = """
AmbiguousMetadata Any ArtifactStore CAP CHECK_REQUIREMENTS CONFIRM_GOAL Callable
CapabilityUnavailable CodebaseMap Complexity DONE_REPLY DelegationLedger DependencyGuard
DependencyIdentityUnavailable DependencyTreeChanged DependencyWatch Dict ErrandToolMismatch
FanOutExceeded FinishReserved InfrastructureUnavailable InvocationEvent JobStoreUnavailable
KNOWN_NEEDS List MAX_CAPTURE_STEPS MAX_PREFACE_LINES MAX_REQUEST_PREFACE_LINES
MAX_SELECTOR_CHARS NOT_RUN_RULE NoCapableSeat OperatorInputNeeded Optional Origin PRECEDENCE
PROVES_RULE PacketTooLarge PartialWorkStopped PartialWorkSuspected Path PermissionBlocked
PersistentMemory PlanningUnaffordable PreviewUnavailable ProviderError ProviderRefusal
PurePosixPath ROUTING RepeatedFailure Required RunOutcome RunStalled SEND_BACK
STEP ScopeReport Seat Sequence Session SessionConfig SimpleNamespace StructuredError
SurveyConfig TASK_FAILURE_CAUSES TASK_SIZE_GUIDE TEST_SUBSTANCE_RULE TaskContract TaskFailed
TaskKind TaskListInvalid TaskMemory TaskMetadata TaskOutcome TaskScope TaskSpec TaskSummary
Tuple TurnLimitReached UsageMeter WORKER_TREE WorkClass WorkerBudget WorkerPool _ANSWER_ID
_ASK_SPARINGLY _AUDIT_EXAMPLES _AUDIT_LINE _AUDIT_LINE_RULE _AUTHORITY_ORDER
_BLOCKED_REPORT_RULE _BLOCKING_LINE _BOUNDARY_NOTE _BOUNDARY_NOTES_PER_REPLY
_BOUNDARY_NOTE_CHARS _BOUNDARY_NOTE_RULE _CAPTURE_LINE _CAPTURE_MARKER _CAPTURE_SCOPE_REQUEST
_CAPTURE_SHAPE _CAP_NO_VERDICT _CASE_CITATION _CASE_NAME _CLASS_CITATION _CLASS_CLAIM
_CLASS_MEMBER _COMMAND_PROSE_STOPS _COMMAND_SELECTION_REASONS
_COMMAND_WRAPPERS _CONSULT_NAMES_RULE _CONTINUES _CONTRACT_BOUNDARY_RULE _COORDINATOR
_CORRECTION_EXPLANATION _COUNTERPART_FIX _COUNTERPART_LENS _COUNTERPART_RECHECK _COVERS
_COVERS_REQUEST _DECIDE _DECIDER_LABELS_REQUEST _DEFERRED_ROW_LINE _DESIGN_RENDER_SHOWS
_DESIGN_REVIEW_LENS _DESIGN_REVIEW_ONLY _DESIGN_SELF_VERIFY _DIFF_NONE _DIFF_NONE_DESIGN
_DIFF_NONE_READ_ONLY _DIFF_NONE_REVIEW_ONLY _DIFF_USE_DESIGN _DIFF_USE_JUDGE
_DIFF_USE_OTHER_ATTEMPT _DIFF_USE_REVIEW _DISCHARGES _DOC_ACTIVE _DOC_IMPERATIVE _DOC_SUBJECT
_DOC_SUFFIXES _EARLY_REJECTION_SCENARIO_RULE _EDITING_ROLES _EMPTY_BLOCKING_RULE _EMPTY_FINDING
_ENVIRONMENT_TEXT _ERROR_BOUNDARY_LENS _ERROR_CLAUSE _ERROR_PATH_RULE _EVIDENCE_AUTHORITY
_EVIDENCE_BODY _EVIDENCE_CHANNEL _EVIDENCE_CLAIM _EVIDENCE_FILE _EVIDENCE_HEAD
_EXCERPTS_PER_PROMPT _EXCERPT_CHARS _EXECUTED_ERROR_RULE _EXPLORING_COSTS _FAILURE_ID_CHARS
_FAILURE_ROWS _FALLBACK_PACKET_BYTES _FETCH_CHAR_CAP _FETCH_REQUEST _FETCH_SYNTAX _FINDING_ID
_FINDING_SHAPE _FINISH_REASON_CHARS _FIXTURE_FENCE _FIXTURE_HEAD _FIXTURE_HEADER
_FRONTEND_WITHOUT_RENDER _GENERATED_KEEP_BYTES _GENERATED_KEEP_FILES _HARNESS_BLOCKERS
_HARNESS_CAPTURE _HISTORY_BINDING_CHARS _HISTORY_EXAMPLE_CHARS _HISTORY_FOOTER_CHARS
_HISTORY_PER_REQUIREMENT _HISTORY_REASON_CHARS _HISTORY_TOTAL_CHARS _HYPOTHESIS
_HYPOTHESIS_REQUEST _ID_END _IMPACT_RANK _INSPECTION_ITEM _INSPECTION_VERDICT _INTEGER_INPUT
_ITEM_DISPLAY_CHARS _Intake _KIND_REQUEST _KIND_VOCABULARY _MAX_ASKS_PER_DECISION
_MAX_EVIDENCE_TEST_BYTES _MAX_ORIENT_NOTES _MAX_ORIENT_NOTE_CHARS _MAX_SCOPE_LINES
_MAX_SCOPE_PATHS _MAX_SCOPE_PATH_CHARS _MEASURED_DIFF_BODY _MEMBER_NAME_CHARS
_MISSING_SCENARIO_RULE _MODULE_NAME _NEEDS_INDENTED _NEEDS_LINE _NEEDS_REQUEST _NOT_MET_SEGMENT
_NOT_RUN_FOR _NOT_RUN_LINE _NOT_RUN_REMEDY_LINES _NO_REQUESTS_IN_FIX
_NO_VERDICT_CLASSES _OBJECT_CONTINUATION _ONE_CAPTURE_MARKER _ONE_CONTINUES_LINE
_ONE_FIXTURE_FENCE _ORIENT_REQUEST _OUTSIDE_SCOPE _OUTSIDE_SCOPE_RULE _OWED_REVIEW_GRAMMAR
_OWED_SETTLEMENT_ATTEMPTS _PACKET_CARRYING_CALLS _PACKET_DECISION _PACKET_EMPTY
_PACKET_FREE_REQUESTS _PACKET_GRANT_TAIL _PACKET_NARROW _PACKET_NARROW_TEXT _PACKET_NECESSARY
_PACKET_REASON_MIN _PACKET_REFUSE_TAIL _PACKET_REQUEST_LINE _PACKET_ROUND_SHARE
_PACKET_WHY_NOT_SHORTER _PARALLEL_HEAD _PARALLEL_REQUEST _PASS_ONLY_WORDS _PLAN_NOTE_CHARS
_POPULATION_ADDED _POPULATION_HEADS _POPULATION_INTRO _POPULATION_LOCATIVES _PREDICATE
_PROBE_RULE _PROTOCOL_RULE _PROVES_ANY _PROVES_BODY _PROVES_LOOSE _PROVES_PID _PUBLIC_RUNNERS
_PYTHON_HEAD _PYTHON_PROGRAM _QUOTED_SPAN _QUOTES _READING_JOINERS _READ_BEFORE_YOU_EXPLORE
_READ_WHAT_YOU_EDIT _REASK_CLOSED _RENDERED_SUFFIXES _RENDERS_BLIND _REPAIR_OUTCOME_RULE
_REQUIREMENTS_REQUEST _REQUIREMENT_VERDICT _REQ_BLOCK _REQ_LINE _RESOLVES _REVIEW_COMPLETE
_REVIEW_DIFF_BYTES _REVIEW_DIFF_INDEX_PATHS _REVIEW_DIFF_MIN_CUT _ROW_LINE _RUNNER_SPAN
_RUNNER_VALUED _RUNS_ANY _RUNS_BODY _SCENARIO_CLAIM _SCENARIO_RANGE _SCENARIO_RENDER_MAX_CHARS
_SCENARIO_REPLY _SCOPE_REQUEST _SEGMENT_BOUNDARY _SIZE_GUIDE _SOURCE_FILE _STANDING_REASON_CHARS
_SUBORDINATOR _TERMINAL_REQUEST _TEST_COMMAND_RULE _TEST_COMPARE_CHARS _TEST_DIFF_CONTEXT
_TEST_VERDICT_LINE _TIER _TYPED_FINDING_FORMS _TYPED_HEAD _TYPED_SEP _UI_FILE _UI_PATH
_UNDECLARED_RENDER_RULE _UNITTEST_FLAGS _UNMEASURED_HEADING _UNMEASURED_MEANING
_UNMEASURED_NAMES _UNRUNNABLE_CLAIM _WHOLE_GATE _WORKER_RECOVERY _WRAPPER_WORD _account
_approving_review _audit_check_facts _audit_lines _behaviour_text _behavioural_error
_bounded_diff _capture_candidates _capture_declaration _capture_fixture_note _case_block
_case_collision _case_files _check_command_paths _check_for_models _check_ids _check_runner
_check_runs _check_source _check_tally _cited_cases _cited_path _class_declaration
_class_run_cases _clean_goal_review _closeout_excerpt _command_interpreter _command_rest
_command_runner_name _command_words _complete_run _configured_checks _confined_fixture_target
_covers_ids _declaration_only _delivers _describe_tree_change _design_approved _design_blocking
_design_fix_delivery _design_judgement _diagnostic_excerpt _diff_chunks _display_item
_enclosing_call _environment_signature _error_clause _evidence_path_problem _executed_clean
_failure_rows _finding_id _fixture_body _folds_binding _frontend_without_rendered_file
_gate_identity _handoff_note _has_blocking_finding _has_prefixed_finding _has_security_finding
_inspection_attested _int_conversion_limit _invocation_role _is_empty_note _item_paths
_kind_request_text _literal_paths _measured_diff_block _member_matches
_mentions_member _met_lines _missing_fixtures _named_outside_comments _named_skip_run
_needs_lines _never_replaced _new_skips _no_findings _operator_limit_note _outside_measured_diff
_packet_allowance_record _packet_bytes _packet_reason_problem _parallel_blocks _parallel_request
_parse_capture_line _parse_consults _parse_evidence _parse_fetch
_parse_fetch_window _parse_fixture_blocks _parse_kind _parse_packet_request _population_clause
_population_path _program_name _proof_cases _proves_problem _pure_reading _pytest_block
_python_args _python_case_defs _python_starts_at _read_continues _read_covers _read_discharges
_read_hypothesis _read_labels _read_metadata _read_packet_decisions _read_requirements
_read_resolves _read_task_needs _read_task_orientation _read_tier _receipt_names _render_fetches
_rendered_file _renders _renders_blind _repeat_key _replaced_note _report_cases _report_classes
_requirement_verdicts _resolved_interpreter _resolved_verdict _resolves_tokens _review_complete
_review_disposition _review_judgments _review_reading _review_subject_note
_revision_changed_delivery _run_passed_cases _same_python_command _scenario_text_problem
_scope_matches _scope_path _seat_takes_scratch _security_finding_stands _security_verdict_line
_selection_record _settle_directives _skip_lexeme _snapshot_files _split_explanation
_split_item_reason _split_runner_unknown _step_failure _stops_the_run _test_diff
_test_repair_settled _test_repair_verified _test_runner_shape _test_verdict _tier_request
_typed_finding _typed_near_miss _unittest_module_command _unserved_consult _unstyled
_verified_by_check _without_command_label _worst_impact _write_fixture_bound annotations
boundary_classes boundary_note canonical capture_invocations changed_paths check_errand_fit
classify close_excursion contextlib count_change_lines count_change_lines_by_path
cross_family_verifier dataclass dataclasses decide delivery escalate_from field finishing
guidance_for hashlib invocation invocation_context is_design_task is_review_only is_test_path
json judgment lead_request log logging needs_from_text normalise_needs obligations
open_security_excursion orchestrator_seat os parse_control parse_metadata parse_security_verdict
peers_for policy_for primary re recovery replace resolve route_kind seat_satisfies
snapshot_session split_lead_request stages_for threading time unclassified uuid worker_menu
wraps
""".split()

#: Every attribute Session resolved at 6181300, written out the same way.
SESSION_ATTRS_AT_START = """
_AUDIT_FORMS _AUDIT_KINDS _EVIDENCE_ID _INTERRUPTIONS _RECEIPT_CHARS __init__
_absorb_orientation _absorb_requirements _absorb_unmeasured _acceptance_record _actionable
_activity _admission_notes _admit_batch _admit_dispatch _admit_plan _admit_repair
_adopt_child_evidence _adopt_entrant _adopt_original_runs _allowance_note
_annotate_open_findings _answer_id _apply_acceptance _apply_completion
_apply_prior_scenario_correction _apply_review_reading _apply_scenario_correction _ask_judge
_ask_restatement_judgment _ask_seat _ask_verifier _assess_scope _associate_not_run
_attempt_identity _attest_inspections _audit_binding _audit_completion_short _audit_debt_applies
_audit_did_not_run _audit_history _audit_lines_block _audit_omitted _audit_required_ids
_audit_requirements _audit_source _audit_text_artifact _auditor _authority_before
_authority_context _authority_record _basis_label _batch_shortfall _batch_specs
_behavior_is_generated _bind_capture_profile _bind_contract _bind_not_run _bind_unknown_causes
_binding_feedback _binding_matches _binding_requested _bound_cases_gone _bound_gate
_boundary_notes_block _breaker_mixed _breaker_reason _brief_design_reviewers _budget
_build_contract _call_event _call_observations _capped_declaration _capped_final_draft
_capped_fix _capped_review _capped_revision_recheck _capture_context _capture_guidance
_capture_ineligible _capture_measurement _capture_problem _capture_receipt _capture_source
_capture_state _case_declaration_current _case_state _case_texts _changed_originals_note
_cheap_gate _check _check_design _check_failures_complete _check_findings_settled
_check_passed_cases _check_scope _checked_final_source _checked_source _checks_standing_failed
_child_renders_unverified _claim_judgment _close_decision _close_failed _close_owed
_close_policy _close_review_finding _close_review_finding_by_id _close_turn_limited
_close_workers _closed_findings_note _closed_with_findings _closes_factually
_closure_route_problem _collaboration_applicable _collaborator_prompt _collaborator_prompt_text
_command_key _command_keys _command_population _command_receipt_citation _command_requirement
_commit_terminal _compare_repaired_tests _comparison_call _comparison_reviewer _complete_audit
_complete_judgment _completion_decision _configured_checks_note _confirm_goal_met
_consult_prompt _consume_evidence _consumed_identity _content_fingerprint _contest_child
_contest_draft _contest_entry _contest_gates _continuation_admission _continuation_chain
_continues_problem _contract_agrees _correct_scenarios _correct_weakened_tests
_correction_packet_problem _count _count_interrupted_admissions _cover_by_receipt _covered_texts
_covers_problem _current_checks_digest _current_gate _decision_state _declaration_decision
_declaration_facts _declaration_identity _declaration_owner _declare_at_cap _declare_at_done
_declare_deferred_evidence _deferred_rows_block _deferred_rows_for _design_context_drift
_design_debt _design_fix_edit _design_instruction _design_repair_stages _design_review_ahead
_design_review_drift _design_state_for_record _deterministic_blockers _direct_refusal
_discharge_named _discharge_pending_settlements _discharge_record _discharges_block
_discharges_problem _discharging_check _discovering_checks _dispatch_capture_profile _dispose
_disputable _distrust_resolutions _done_declaration_plan _done_declarations _draft_with_channels
_drafting_loop _edge _edit _editing_work _editor_lines _effort_inputs _environment_id
_evidence_bindings _evidence_block _evidence_files _evidence_files_for _evidence_identity
_evidence_note _evidence_receipt _evidence_refusals _evidence_resolves _evidence_route
_evidence_set_problem _executed_cases _execution_identity _explain_unsettled_check
_explicit_list_spent _factual_close_out _failed_attempts _failure_context _file_digests
_final_audit_current _final_challenge _final_design_review _final_work_slot _finalise_findings
_finding_contract _finding_lines _finding_prefix _finding_records _findings_block
_findings_block_done _findings_prompt _findings_stop_due _finish_pending_evidence
_finish_refused _finish_run _fix_cycle_stages _fix_prompt _fix_reviewed_design _fold_note
_form_contest _gate_can_run _gate_cwd _gate_for _gate_repair_stages _gate_repair_wall
_gate_runs_command _gate_selection _gate_wall _gate_wall_of _generated_tests _goal_authority
_goal_block _hand_off_preview _handed_drift _handoff_account _handoff_came_to _handoff_classes
_handoff_holds _handoff_unattributable _harness_capture _harness_captures _harness_facts
_harness_record _hold_open_finding _hold_task_findings _hold_unexercised_errors
_hold_unreviewed_goal_work _hold_weakened _identity_check _identity_problem _independent_judge
_inherit_review_findings _inherited_findings_block _inherited_not_run _inherited_review_findings
_inspect_partial_edits _inspection_note _interim_edits_note _invoke_model _invoke_model_call
_invoke_model_watched _invoke_with_fetches _issue_task_id _join_original_tests
_judge_goal_review _judge_obligations _judge_restated _judge_restatements _judge_test_repairs
_judgment_binding _judgment_gap _keep_reply _kind_request _known_command_paths _known_incomplete
_known_outcome _known_remainder _last_planning_tokens _lead_context _lead_prompt _lead_tokens
_ledger_at_settlement _lift_final_cap _limits_identity _live_capture_page _live_collaboration
_live_design_instruction _live_security_verdict _live_tier _lower_unexecuted
_lower_unexercised_errors _mandatory_stages _map_block _mark_covered _mark_deferred_rows
_mark_not_run_display _mark_not_run_display_safely _mark_stale_bindings _measure_context
_measure_entry _measure_scope _mechanical_close_out _merge_files
_missing_own_fixtures _module_case_ids _name_findings_stop _named_by_a_run_test
_new_case_problems _next_explicit_task _next_run_seq _next_task_decision _next_task_id
_no_contest_reason _no_edit_cap_note _no_worker_reason _normalize_resolves _not_met_basis
_not_met_segments _not_run_block _not_run_executable _not_run_identity _not_run_owned
_not_run_remedy _not_run_runnable _not_run_scenario_ids _note _note_check_wall _note_exception
_note_replaced_evidence _note_skips _note_unrun_tests _obligation_current_gate
_obligation_receipt _obligation_view _obligations _obligations_block _observation_facts
_observation_named _observe_checks _open_finding _open_findings_for _open_findings_prompt
_open_findings_touching _open_inspections _open_refs _open_work _operation _operator_limits_note
_original_tests_at_done _other_attempt_note _outer_limits _owe_review _owed_attempts
_owed_changed _owed_open _owed_record _owed_report _owed_resolves_note _owed_review_prompt
_owed_settlements _owed_tasks _owed_texts _owned_unexecutable _owner_lead _packet
_packet_allowance _packet_budget_problem _packet_ceiling _packet_current_request _packet_decide
_packet_decisions_unused _packet_default _packet_fingerprint _packet_grant_for _packet_justify
_packet_lead _packet_problem _packet_record_dispatch _packet_request_text _packet_roles
_packet_seat_key _parallel_enabled _partial_from_outcomes _path_case_problem _pick_challenger
_pick_lead _plan_findings _plan_note _plan_stages _planner_failure_context _planning_forfeit
_planning_refused _planning_stages _policy_denies _population_problem _prepare_dispatch
_prior_packet_problem _prior_scenario_problem _prior_scope _probe_applies _probe_checks
_profile_digest _proof_passed_cases _proof_problem _proof_rows _protect_finish
_publish_authority _publish_operations _qualify_debt _queue_stands _quiescent
_read_deferred_rows _read_directives _read_done_declaration _read_listed _read_restatement
_read_test_verdicts _reading_only _reask_offer_closed _reask_quote _reask_refusal _reason_source
_reassess _rebind_capture_rows _rebind_not_run _recapture_declared _receipt _receipt_identity
_recheck_after_fix _recheck_after_merge _recheck_blocking _recheck_diff _recheck_instructions
_recheck_open_findings _recheck_prompt _recheck_reading _recheck_resolved_findings
_reconcile_supersession _record_abandoned_workers _record_audit_findings _record_boundary_notes
_record_changed_report _record_check_attempt _record_check_debt _record_deferred
_record_delivery _record_edit_reply _record_merge_debt _record_not_run _record_proves
_record_recheck _record_repair_originals _record_review _record_review_notes _record_runs
_record_scenario_correction _record_scope_declarations _record_scratch_evidence
_record_selection _record_stop_rule _record_task_failure _record_terminal_recovery
_record_unmerged_failure _record_work _recover_continued _recover_judgment _recovery_allowance
_recovery_attempt _recovery_context _recovery_decision _recovery_open _recovery_result
_recovery_tried _refresh_obligations _refresh_recovery_results _refuse_admission_record
_refuse_listed _refuse_mismatched _refuse_settlement_mismatch _refuse_unpayable_dispatch
_register_acceptance_obligations _register_obligation
_register_review_findings _release_renewal _renders_after_merge_gate _renew_audit_hold
_repair_check _repair_context _repair_diagnosis _repaired _request_after_close _request_bindings
_required _requirement_revisions _requirement_texts_now _requirements_satisfied
_resolution_findings_block _resolution_pending _resolution_reviewers _resolution_targets_of
_resolve_case_ids _resolve_check_findings _resolve_citations _resolve_consultant
_resolve_findings _resolves_problem _restated _restated_awaiting _restated_current
_restated_executed_ids _restated_judged _restated_review_block _restated_revisions
_restatement_asked _restatement_decision_key _restatement_judge _restatement_judgment_prompt
_restatement_status _restatement_unread _review_capped_final_child _review_debt_hold
_review_debt_holds _review_diff _review_gaps _review_notes_block _review_notes_grouped
_review_requirements _review_resolution_targets _review_resolves_note _review_turn_budget_note
_revision_delivery _revision_prompt _role_packet _role_packet_with _round_plan _route_gate
_route_wall _route_with_decider _routed_ids _routed_result _routing_facts _row_route_note
_run_batch _run_evidence _run_integration_gate _run_original_tests_at_done _run_readiness
_run_security_task _run_task _run_task_recorded _run_tasks _scenario_allowance _scenario_field
_scenario_line _scenario_rows _scenario_shown _scenario_text _scenario_unbound _scope_appendix
_scope_case_paths _scope_covers _scope_path_problem _scope_request _scope_resolves_problem
_scope_route_text _scope_size_note _scratch_identity _scratch_receipt _security_repair_stages
_security_snapshot _selection_changed _serve_evidence _serve_packet_request _serve_plan_findings
_serve_worker _settle_completion _settle_folded _settle_not_started _settle_owed
_settle_owed_by_judgment _settle_owed_reviews _settle_resolution _settle_review_findings
_settle_test_comparison _shown _shown_reason _sibling_note _skip_baseline _skip_contest
_slot_event _slots_note _snapshot_original_tests _snapshot_outer _snapshot_scratch_bindings
_source_drift _source_fingerprint _source_identity _split_requirements _spread_lead _stage
_standing_not_run _standing_reason _standing_reason_note _stop_follows_close _stop_open_work
_stop_repeated_environment_failure _stop_with _supersede_obligation _superseded_row_line
_superseding_rows _supply_fixtures _survey_after_failure _survey_repeat _survey_request
_sync_obligation _take_over_not_run _task_changed_files _task_close_out _task_failed_cases
_task_list_problems _task_number _task_runs _terminal_committed _terminal_for
_test_command_extra_words _test_command_requirement _test_functions _test_migrations
_tests_changed_in_run _tier _tool_worker _trusted_source _turn_budget_note
_unchanged_continuation _uncovered_requirements _undeclared_design_work
_undeclared_predecessor_changes _unfinished_facts _unfinished_state_note _unmark_not_started
_unmeasured_block _unmeasured_note _unmerged_conflict _unnamed_standing _unresolved_partial
_unverified_once _verification_handoff _verification_note _verification_problem _verifier_prompt
_verify_dependencies _verify_security_json _with_original_tests _work_account
_worker_fields_problem _worker_limits_refusal _write_ceiling _writer_goal
_writer_turn_budget_note _writes brain_trust collaborators_for next_task plan run run_task seat
""".split()

#: Removed on 2026-11-12, after the split, and taken out of the two lists
#: above. No model writes a close-out (owner ruling, 2026-10-21), so the
#: model close-out and its reply parser had no caller; review seats get one
#: measured diff block (Session._review_diff), so the development line's own
#: block and its bound had none either.
REMOVED_NAMES = ["_CLOSEOUT_HEADER", "_CLOSEOUT_STYLING", "_NO_DEAD_ENDS", "_map_entry", "_parse_closeout",
                 "REVIEW_DIFF_CHARS"]
REMOVED_ATTRS = ["_close_out", "_refused_close_out", "_measured_change"]

#: Names a test replaces on the session module, and the test that does it.
#: Code that reads one must be in session.py, or it reads the original.
PATCHED_ON_SESSION = {
    "escalate_from": "tests/lifecycle/test_capped_drafts.py",
    "_FETCH_CHAR_CAP": "tests/test_human_coherence_context_review.py",
    "_member_matches": "tests/test_review1110_parsers.py",
    "_measured_diff_block": "tests/test_review1110_prompts.py",
    "_TEST_COMPARE_CHARS": "tests/lifecycle/test_bounded_repair.py",
    "_gate_identity": "tests/test_quality_completion_units.py",
    "_check_runs": "tests/test_cycle22_units.py",
    "_complete_run": "tests/test_cycle22_units.py",
    "_run_passed_cases": "tests/test_cycle22_units.py",
    "is_review_only": "tests/test_evidence_routing_units.py",
}


def _source(module: str) -> str:
    return (ROOT / "quadratus" / f"{module}.py").read_text(encoding="utf-8")


def _defined(module: str) -> list:
    """The names a module binds at its top level, imports left out."""
    names = []
    for node in ast.parse(_source(module)).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names += [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names.append(node.target.id)
    return sorted(set(names))


def _fresh(code: str):
    """Run ``code`` in a new interpreter that imports this checkout."""
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=180)


def test_every_name_the_session_module_had_is_still_importable_from_it():
    assert len(NAMES_AT_START) == 532 - len(REMOVED_NAMES)
    assert not [name for name in REMOVED_NAMES + REMOVED_ATTRS if hasattr(S, name) or hasattr(S.Session, name)]
    missing = [name for name in NAMES_AT_START if not hasattr(S, name)]
    assert not missing, f"no longer importable from quadratus.session: {missing}"
    assert S.__all__ == ["Complexity", "PartialWorkStopped", "TaskSpec", "Session", "SessionConfig",
                         "RunStalled", "OperatorInputNeeded", "TaskFailed"]


def test_every_attribute_session_had_still_resolves_on_it():
    assert len(SESSION_ATTRS_AT_START) == 683 - len(REMOVED_ATTRS)
    missing = [name for name in SESSION_ATTRS_AT_START if not hasattr(S.Session, name)]
    assert not missing, f"no longer on Session: {missing}"


@pytest.mark.parametrize("module", MOVED)
def test_a_moved_name_is_the_same_object_in_both_places(module):
    home = importlib.import_module(f"quadratus.{module}")
    names = _defined(module)
    assert names, f"{module} defines nothing"
    for name in names:
        if name == "log":
            continue
        if name.endswith("Mixin"):
            assert getattr(home, name) in S.Session.__mro__, f"Session does not inherit {name}"
            continue
        assert getattr(S, name) is getattr(home, name), (
            f"quadratus.session.{name} is not quadratus.{module}.{name}: import it in session.py")


@pytest.mark.parametrize("module", MOVED)
def test_a_moved_module_imports_alone_and_never_loads_the_session_module(module):
    """A mixin that imported quadratus.session would be a cycle the moment
    session.py imports the mixin."""
    done = _fresh(f"import sys, quadratus.{module}; sys.exit('quadratus.session' in sys.modules)")
    assert done.returncode == 0, f"quadratus.{module} loads quadratus.session, or fails alone: {done.stderr[-1500:]}"


def test_the_engine_and_its_entry_points_import_together():
    done = _fresh("import quadratus.session, quadratus.cli, quadratus.gui, quadratus.project_run, "
                  "quadratus.baseline")
    assert done.returncode == 0, done.stderr[-1500:]


def test_no_moved_code_reads_a_name_a_test_replaces_on_the_session_module():
    """monkeypatch.setattr(session, name, ...) changes what session.py's own
    code reads and nothing else. A reader in another module would go on
    calling the original, and the test that patches it would pass or fail
    for the wrong reason."""
    readers = []
    for module in MOVED:
        for node in ast.walk(ast.parse(_source(module))):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in PATCHED_ON_SESSION:
                readers.append((module, node.id, node.lineno))
            elif isinstance(node, ast.Attribute) and node.attr in PATCHED_ON_SESSION:
                readers.append((module, node.attr, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                readers += [(module, alias.name, node.lineno) for alias in node.names
                            if alias.name in PATCHED_ON_SESSION]
    assert not readers, f"read outside session.py, where a test's replacement is not seen: {readers}"
    read_here = {node.id for node in ast.walk(ast.parse(_source("session")))
                 if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    for name, test in PATCHED_ON_SESSION.items():
        assert hasattr(S, name) and name in read_here, f"{name} ({test}) is no longer read in session.py"


def test_no_method_is_defined_on_session_and_on_a_mixin_or_on_two_mixins():
    """Session's bases are places its methods live, not layers: with no name
    defined twice, the order of the bases cannot change what runs."""
    seen = {}
    for klass in S.Session.__mro__[:-1]:
        for name in vars(klass):
            if name.startswith("__") and name.endswith("__") and name != "__init__":
                continue
            assert name not in seen, f"{name} is defined on {seen[name]} and on {klass.__name__}"
            seen[name] = klass.__name__
    assert seen["__init__"] == "Session"
    assert {klass.__name__ for klass in S.Session.__mro__[1:-1]} == MIXINS


def test_the_engine_logs_under_one_name_wherever_its_code_lives():
    assert S.log.name == "quadratus.session"
    for module in MOVED:
        home = importlib.import_module(f"quadratus.{module}")
        if hasattr(home, "log"):
            assert home.log is S.log, f"quadratus.{module} logs under {home.log.name}"


@pytest.mark.parametrize("module", MOVED)
def test_the_scans_older_tests_make_of_session_py_hold_for_the_code_that_moved(module):
    """tests/test_review1110_loop_b.py, test_review1110_glue_engine.py,
    test_review1110_fix_a.py and test_finding_identity_units.py each read
    session.py's text. Code that moved out is no longer in that text, so the
    same four rules are checked here."""
    source = _source(module)
    # No task id is derived from the history length.
    assert not re.search(r't\{len\(self\.history\)', source)
    assert not re.search(r'task \{len\(self\.history\)', source)
    # The map is amended only in session.py, where the older test counts the places.
    assert "codebase_map.amend(" not in source
    # No scenario text bound is quoted by name.
    assert "MAX_SCENARIO_TEXT" not in source
    # No path is stripped of "." and "/" as characters.
    found = [node.lineno for node in ast.walk(ast.parse(source))
             if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in ("strip", "lstrip") and len(node.args) == 1
                 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                 and {".", "/"} <= set(node.args[0].value))]
    assert not found, f"{module}.py strips './' as characters on line(s) {found}; use _scope_path"
