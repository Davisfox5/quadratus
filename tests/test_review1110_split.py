"""quadratus/session.py was split on 2026-11-11 by moving code, text unchanged.

What the split promised, held here so a later move cannot break it: every
name quadratus.session had at 855c8d0 is still there, each moved name is one
object in both places, no module the code moved to loads quadratus.session,
and no moved code reads a name a test replaces on the session module. Three
older tests scan session.py's own text for a banned pattern; the same scans
run here over the modules the code moved to, so they still cover it.
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
         "session_design", "session_audit", "session_seat_prompts", "session_security")

#: Every name quadratus.session had at 855c8d0, private and imported ones
#: included, written out by a script from that commit's module.
NAMES_AT_START = """
AmbiguousMetadata ArtifactStore CAP CHECK_REQUIREMENTS CONFIRM_GOAL Callable
CapabilityUnavailable CodebaseMap Complexity DONE_REPLY DelegationLedger DependencyGuard
DependencyIdentityUnavailable DependencyTreeChanged DependencyWatch Dict ErrandToolMismatch
FanOutExceeded InfrastructureUnavailable InvocationEvent KNOWN_NEEDS List MAX_CAPTURE_STEPS
MAX_PREFACE_LINES MAX_REQUEST_PREFACE_LINES MAX_SELECTOR_CHARS MAX_TASK_LINES NoCapableSeat
OperatorInputNeeded Optional Origin PRECEDENCE PartialWorkStopped PartialWorkSuspected Path
PersistentMemory PreviewUnavailable ProviderError ProviderRefusal PurePosixPath ROUTING
RepeatedFailure Required RunOutcome RunStalled SEND_BACK STEP ScopeReport Seat Sequence Session
SessionConfig StructuredError SurveyConfig TASK_FAILURE_CAUSES TaskContract TaskFailed TaskKind
TaskListInvalid TaskMemory TaskMetadata TaskOutcome TaskScope TaskSpec TaskSummary
TurnLimitReached UsageMeter WORKER_TREE WorkClass WorkerBudget WorkerPool _ASK_SPARINGLY
_AUDIT_LINE_RULE _BLOCKED_REPORT_RULE _CAPTURE_LINE _CAPTURE_MARKER _CAPTURE_SCOPE_REQUEST
_CAPTURE_SHAPE _CLOSEOUT_HEADER _CLOSEOUT_STYLING _COMMAND_WRAPPERS _CONSULT_NAMES_RULE
_CONTINUES _COVERS _COVERS_REQUEST _DECIDE _DECIDER_LABELS_REQUEST _DESIGN_RENDER_SHOWS
_DESIGN_REVIEW_LENS _DESIGN_REVIEW_ONLY _DESIGN_SELF_VERIFY _DIFF_NONE _DIFF_NONE_DESIGN
_DIFF_NONE_READ_ONLY _DIFF_NONE_REVIEW_ONLY _DIFF_USE_DESIGN _DIFF_USE_JUDGE
_DIFF_USE_OTHER_ATTEMPT _DIFF_USE_REVIEW _EMPTY_BLOCKING_RULE _ENVIRONMENT_TEXT _EXPLORING_COSTS
_FETCH_CHAR_CAP _FIXTURE_FENCE _FIXTURE_HEAD _FIXTURE_HEADER _FRONTEND_WITHOUT_RENDER
_HARNESS_BLOCKERS _HARNESS_CAPTURE _HYPOTHESIS _HYPOTHESIS_REQUEST _Intake _KIND_REQUEST
_KIND_VOCABULARY _MAX_ASKS_PER_DECISION _MAX_ORIENT_NOTES _MAX_ORIENT_NOTE_CHARS
_MEASURED_DIFF_BODY _MEMBER_NAME_CHARS _NEEDS_INDENTED _NEEDS_LINE _NEEDS_REQUEST _NOT_RUN_LINE
_NOT_RUN_REMEDY_LINES _NO_DEAD_ENDS _NO_REQUESTS_IN_FIX _ONE_CAPTURE_MARKER _ONE_CONTINUES_LINE
_ONE_FIXTURE_FENCE _ORIENT_REQUEST _PARALLEL_HEAD _PARALLEL_REQUEST _PLAN_NOTE_CHARS
_PUBLIC_RUNNERS _PYTHON_PROGRAM _QUOTES _READ_BEFORE_YOU_EXPLORE _READ_WHAT_YOU_EDIT
_RENDERED_SUFFIXES _RENDERS_BLIND _REQUIREMENTS_REQUEST _REQ_BLOCK _REQ_LINE _RESOLVES
_REVIEW_COMPLETE _REVIEW_DIFF_BYTES _REVIEW_DIFF_INDEX_PATHS _REVIEW_DIFF_MIN_CUT _SCOPE_REQUEST
_SIZE_CEILING _TERMINAL_REQUEST _TIER _UI_FILE _UI_PATH _UNDECLARED_RENDER_RULE
_UNMEASURED_HEADING _UNMEASURED_MEANING _UNMEASURED_NAMES _WHOLE_GATE _WORKER_RECOVERY
_WRAPPER_WORD _account _audit_check_facts _audit_lines _bounded_diff _capture_candidates
_capture_declaration _capture_fixture_note _case_block _case_files _check_command_paths
_check_for_models _check_ids _check_runner _check_runs _check_source _check_tally _cited_path
_closeout_excerpt _complete_run _configured_checks _confined_fixture_target _covers_ids
_declaration_only _delivers _describe_tree_change _design_blocking _design_fix_delivery
_design_judgement _diff_chunks _enclosing_call _environment_signature _executed_clean
_fixture_body _frontend_without_rendered_file _gate_identity _handoff_note _has_blocking_finding
_has_prefixed_finding _has_security_finding _invocation_role _is_empty_note _item_paths
_kind_request_text _literal_paths _map_entry _measured_diff_block _member_matches
_mentions_member _missing_fixtures _named_outside_comments _needs_lines _never_replaced
_new_skips _no_findings _operator_limit_note _outside_measured_diff _parallel_blocks
_parallel_request _parse_capture_line _parse_closeout _parse_consults _parse_fetch
_parse_fixture_blocks _parse_kind _program_name _pytest_block _python_starts_at _read_continues
_read_covers _read_hypothesis _read_labels _read_metadata _read_requirements _read_resolves
_read_task_needs _read_task_orientation _read_tier _receipt_names _render_fetches _rendered_file
_renders _renders_blind _repeat_key _resolved_verdict _resolves_tokens _review_complete
_review_reading _review_subject_note _revision_changed_delivery _scope_path
_security_finding_stands _security_verdict_line _selection_record _settle_directives
_skip_lexeme _snapshot_files _step_failure _stops_the_run _tier_request _unserved_consult
_unstyled _verified_by_check _write_fixture_bound annotations canonical capture_invocations
changed_paths check_errand_fit classify close_excursion contextlib count_change_lines
cross_family_verifier dataclass dataclasses decide escalate_from field guidance_for hashlib
invocation invocation_context is_design_task is_review_only json lead_request log logging
needs_from_text normalise_needs open_security_excursion orchestrator_seat os parse_control
parse_metadata parse_security_verdict peers_for policy_for primary re replace resolve route_kind
seat_satisfies snapshot_session split_lead_request stages_for time unclassified uuid worker_menu
wraps
""".split()

#: Every attribute Session resolved at 855c8d0, written out the same way.
SESSION_ATTRS_AT_START = """
__init__ _absorb_orientation _absorb_requirements _absorb_unmeasured _activity
_adopt_child_evidence _adopt_entrant _adopt_original_runs _annotate_open_findings
_apply_completion _ask_judge _ask_seat _ask_verifier _assess_scope _audit_debt_applies
_audit_did_not_run _audit_requirements _auditor _batch_shortfall _batch_specs
_bind_capture_profile _bind_contract _bound_gate _breaker_mixed _breaker_reason
_brief_design_reviewers _build_contract _capped_declaration _capped_fix _capped_review
_capture_guidance _capture_ineligible _capture_problem _capture_receipt _capture_source
_capture_state _case_texts _changed_originals_note _cheap_gate _check _check_design
_check_findings_settled _checks_standing_failed _child_renders_unverified _close_failed
_close_out _close_turn_limited _close_workers _closed_with_findings _collaboration_applicable
_collaborator_prompt _command_keys _completion_decision _configured_checks_note
_confirm_goal_met _consult_prompt _content_fingerprint _contest_child _contest_draft
_contest_entry _contest_gates _contract_agrees _count _covers_problem _current_gate _design_debt
_design_instruction _direct_refusal _discharge_named _dispatch_capture_profile
_distrust_resolutions _draft_with_channels _drafting_loop _edge _edit _evidence_files
_evidence_files_for _evidence_refusals _evidence_set_problem _explicit_list_spent
_final_design_review _finalise_findings _findings_block_done _findings_prompt _findings_stop_due
_finish_run _fix_prompt _fix_reviewed_design _form_contest _gate_cwd _gate_for _gate_selection
_hand_off_preview _handoff_unattributable _harness_capture _harness_captures _harness_facts
_harness_record _identity_check _identity_problem _inspect_partial_edits _interim_edits_note
_invoke_model _invoke_model_call _invoke_with_fetches _issue_task_id _join_original_tests
_keep_reply _kind_request _lead_context _lead_prompt _lead_tokens _ledger_at_settlement
_limits_identity _live_capture_page _live_collaboration _live_design_instruction
_live_security_verdict _live_tier _lower_unexecuted _map_block _mark_covered _measure_entry
_measure_scope _mechanical_close_out _merge_files _missing_own_fixtures _name_findings_stop
_named_by_a_run_test _next_explicit_task _next_task_id _no_contest_reason _no_worker_reason
_not_run_block _not_run_remedy _note _note_exception _note_replaced_evidence _note_skips
_note_unrun_tests _observe_checks _open_finding _open_findings_for _open_refs _open_work
_operator_limits_note _original_tests_at_done _other_attempt_note _outer_limits _packet_roles
_parallel_enabled _partial_from_outcomes _pick_challenger _pick_lead _plan_note _policy_denies
_prepare_dispatch _profile_digest _qualify_debt _queue_stands _read_directives _read_listed
_recapture_declared _recheck_after_fix _recheck_blocking _recheck_diff _recheck_prompt
_recheck_resolved_findings _record_abandoned_workers _record_audit_findings
_record_changed_report _record_check_attempt _record_check_debt _record_delivery
_record_edit_reply _record_not_run _record_recheck _record_repair_originals _record_review
_record_selection _record_work _recover_continued _refuse_listed _refuse_mismatched
_refuse_settlement_mismatch _refused_close_out _renders_after_merge_gate _repair_check _repaired
_request_after_close _required _requirements_satisfied _resolve_check_findings
_resolve_citations _resolve_consultant _resolve_findings _resolves_problem _review_diff
_review_requirements _review_turn_budget_note _revision_delivery _revision_prompt _role_packet
_role_packet_with _route_with_decider _routed_result _run_batch _run_integration_gate
_run_original_tests_at_done _run_readiness _run_security_task _run_task _run_task_recorded
_run_tasks _scope_appendix _scope_headroom _security_snapshot _selection_changed _serve_worker
_settle_resolution _shown _skip_baseline _skip_contest _snapshot_original_tests _snapshot_outer
_source_drift _source_fingerprint _source_identity _split_requirements _spread_lead _stage
_standing_not_run _stop_open_work _stop_repeated_environment_failure _stop_with _supply_fixtures
_survey_after_failure _survey_repeat _survey_request _task_list_problems _task_number
_test_functions _test_migrations _tests_changed_in_run _tier _tool_worker _trusted_source
_turn_budget_note _undeclared_design_work _undeclared_predecessor_changes _unmeasured_block
_unmeasured_note _unnamed_standing _unresolved_partial _unverified_once _verification_handoff
_verification_note _verification_problem _verifier_prompt _verify_dependencies
_verify_security_json _with_original_tests _worker_fields_problem _worker_limits_refusal
_write_ceiling _writes brain_trust collaborators_for next_task plan run run_task seat
""".split()

#: Names a test replaces on the session module, and the test that does it.
#: Code that reads one must be in session.py, or it reads the original.
PATCHED_ON_SESSION = {
    "escalate_from": "tests/lifecycle/test_capped_drafts.py",
    "_FETCH_CHAR_CAP": "tests/test_human_coherence_context_review.py",
    "_member_matches": "tests/test_review1110_parsers.py",
    "_measured_diff_block": "tests/test_review1110_prompts.py",
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
    assert len(NAMES_AT_START) == 313
    missing = [name for name in NAMES_AT_START if not hasattr(S, name)]
    assert not missing, f"no longer importable from quadratus.session: {missing}"
    assert S.__all__ == ["Complexity", "PartialWorkStopped", "TaskSpec", "Session", "SessionConfig",
                         "RunStalled", "OperatorInputNeeded", "TaskFailed"]


def test_every_attribute_session_had_still_resolves_on_it():
    assert len(SESSION_ATTRS_AT_START) == 273
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
    done = _fresh("import quadratus.session, quadratus.cli, quadratus.gui, quadratus.project_run")
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
    assert {klass.__name__ for klass in S.Session.__mro__[1:-1]} == {
        "ChecksMixin", "DesignMixin", "AuditMixin", "SeatPromptsMixin", "SecurityMixin"}


def test_the_engine_logs_under_one_name_wherever_its_code_lives():
    assert S.log.name == "quadratus.session"
    for module in MOVED:
        home = importlib.import_module(f"quadratus.{module}")
        if hasattr(home, "log"):
            assert home.log is S.log, f"quadratus.{module} logs under {home.log.name}"


@pytest.mark.parametrize("module", MOVED)
def test_the_scans_older_tests_make_of_session_py_hold_for_the_code_that_moved(module):
    """tests/test_review1110_loop_b.py, test_review1110_glue_engine.py and
    test_review1110_fix_a.py each read session.py's text. Code that moved
    out is no longer in that text, so the same three rules are checked here."""
    source = _source(module)
    # No task id is derived from the history length.
    assert not re.search(r't\{len\(self\.history\)', source)
    assert not re.search(r'task \{len\(self\.history\)', source)
    # The map is amended in two places, both in session.py.
    assert "codebase_map.amend(" not in source
    # No path is stripped of "." and "/" as characters.
    found = [node.lineno for node in ast.walk(ast.parse(source))
             if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in ("strip", "lstrip") and len(node.args) == 1
                 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                 and {".", "/"} <= set(node.args[0].value))]
    assert not found, f"{module}.py strips './' as characters on line(s) {found}; use _scope_path"
