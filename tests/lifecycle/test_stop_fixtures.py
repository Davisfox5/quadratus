"""Named stops and result fields as they are today, one record per journey
(map P3.4; Codex, 5862193380).

For the findings projection and completion decision candidates: each record
is what an existing journey test leaves behind, captured from the real
``Session`` after that test's own assertions pass. The fields are the legacy
inputs a candidate must reproduce (``stop_reason``, ``result.error``,
``open_findings``, ``_design_unverified``, the audit findings) beside the
typed stop fact, one entry per Session the journey ran, in order.

``stop_fact.detail`` is cut at 400 characters (300 for an exception's
message); ``stop_fact.full`` holds the whole text only when it was cut, and
``stops`` lists every active named stop in order, so a post-loop exception
after an earlier named stop is visible (Opus review 5e2fb31). Derived
checks run on every record, so a regenerated fixture cannot bless a
contradiction.

Nothing here changes a route. ``tests/lifecycle/stop_fixtures.json`` is
compared field by field; ``QUADRATUS_WRITE_STOP_FIXTURES=1`` rewrites it.
Temporary paths and 12-character artifact ids are normalised.
"""

import inspect
import json
import os
import re
from pathlib import Path

import pytest

from quadratus import project_run
from quadratus.session import Session
from tests import test_breaker_names as breaker
from tests import test_design_debt_binding as design
from tests import test_post_loop_exception as postloop
from tests import test_requirements_ledger as ledger
from tests import test_stop_full_text as fulltext
from tests.lifecycle import test_audit_findings as audit
from tests.lifecycle import test_completion_guard as guard
from tests.lifecycle import test_dependency_identity as deptree
from tests.lifecycle import test_lifecycle_matrix as matrix
from tests.lifecycle import test_named_stops as named

FIXTURES = Path(__file__).with_name("stop_fixtures.json")
LEDGER = pytest.mark.requirements_ledger

JOURNEYS = {
    "clean": guard.test_the_clean_run_the_controls_break_is_complete,
    "PlanDeclined": named.test_a_declined_plan_is_named,
    "RequirementsUnmet:done": pytest.param(
        named.test_requirements_still_unmet_after_the_reopen_allowance_are_named, marks=LEDGER),
    "RunStalled:no-requirements": pytest.param(
        ledger.test_without_a_list_no_task_runs_and_the_run_cannot_complete, marks=LEDGER),
    "DoneWithOpenWork:capped": named.test_done_with_a_capped_task_never_continued_is_named,
    "GoalUnconfirmedAtCap:not-confirmed": named.test_the_cap_without_a_confirmed_goal_is_named,
    "GoalUnconfirmedAtCap:capped": named.test_the_cap_with_a_capped_task_never_continued_is_named,
    "CheckFailing": named.test_a_task_closing_with_a_failed_attributed_check_is_named,
    "FindingsOpen": matrix.test_renders_of_an_unrelated_page_are_an_open_finding,
    "DesignUnverified:serial": matrix.test_a_capture_whose_interaction_step_failed_leaves_the_design_unverified,
    "DesignUnverified:parallel-child": design.test_a_parallel_childs_design_debt_names_the_parents_stop,
    "FindingsUnresolved:resolves": pytest.param(
        audit.test_a_recapture_of_the_same_address_in_another_state_does_not_resolve, marks=LEDGER),
    "FindingsUnresolved:cap": pytest.param(audit.test_the_task_cap_with_an_open_finding_is_a_named_stop, marks=LEDGER),
    "CompletionUnproven:done": guard.test_an_unsatisfied_mandatory_edge_cannot_count_as_complete,
    "CompletionUnproven:cap": guard.test_the_cap_confirmation_is_guarded_the_same_way,
    "TurnLimitBreaker:serial": matrix.test_two_caps_in_a_row_stop_with_a_named_breaker_and_the_work_kept,
    "TurnLimitBreaker:batch": breaker.test_a_batch_between_counted_caps_names_what_was_counted,
    "DependencyTreeChanged": deptree.test_a_lead_that_creates_a_node_modules_shim_is_stopped_after_that_call,
    "RequirementsUnmet:long": pytest.param(
        fulltext.test_a_long_named_stop_keeps_its_whole_text_on_the_fact, marks=LEDGER),
    "LookupError:long": fulltext.test_the_whole_controller_error_and_the_fact_agree,
    "OSError:post-loop": postloop.test_an_exception_in_the_end_of_run_check_is_a_typed_stop,
    "KeyError:post-loop-after-named": postloop.test_an_exception_in_the_final_findings_record_is_a_typed_stop,
}

_ARTIFACT = re.compile(r"\b[0-9a-f]{12}\b")


def _normaliser(tmp_path):
    root = str(tmp_path)

    def norm(text):
        if text is None:
            return None
        return _ARTIFACT.sub("<artifact>", str(text).replace(root, "<tmp>"))
    return norm


def _record(session, errors, norm):
    """One Session's fields; ``errors`` maps it to the ``result.error`` project_run wrote."""
    stop = session.run_outcome.stop()
    return {
        "completed": session.completed,
        "done_accepted": session.run_outcome.done_accepted,
        "stop_reason": norm(session.stop_reason),
        "result_error": norm(errors[id(session)]) if id(session) in errors else None,
        "stop_fact": stop and {"kind": stop.kind, "legacy": stop.legacy, "detail": norm(stop.detail),
                               "full": norm(stop.full)},
        "stops": [f.legacy for f in session.run_outcome.facts if f.active and f.legacy is not None],
        "open_findings": [norm(f) for f in session.open_findings],
        "design_unverified": [[t, norm(p)] for t, p in session._design_unverified],
        "audit_findings": [{"id": f.get("id"), "task": f.get("task"), "status": f.get("status")}
                           for f in session.findings],
        "turn_limited": list(session.turn_limited),
        "tasks": [{"task_id": o.task_id, "closed_as": o.closed_as, "primary": o.primary}
                  for o in session.task_outcomes],
    }


def _consistent(record):
    """What must hold on any record, recorded or regenerated."""
    fact, stops = record["stop_fact"], record["stops"]
    assert (fact is None) == (not stops), "a typed stop exactly when a named stop is active"
    if fact is None:
        assert record["completed"] and record["stop_reason"] == "" and not record["result_error"]
        return
    assert fact["legacy"] == stops[-1] and not record["completed"]
    whole = fact["full"] or fact["detail"]
    if fact["full"] is not None:
        assert fact["full"] != fact["detail"] and fact["full"].startswith(fact["detail"])
    if record["result_error"]:
        assert record["result_error"] == whole, "result.error is the typed whole text"
    if record["stop_reason"]:
        named = record["stop_reason"].split(":", 1)[0]
        if named == fact["legacy"]:
            assert record["stop_reason"] == whole
        else:
            assert len(stops) >= 2 and stops[-2] == named, "only a post-loop exception follows a named stop"


@pytest.fixture(scope="module")
def expected():
    return json.loads(FIXTURES.read_text()) if FIXTURES.exists() else {}


@pytest.fixture(scope="module")
def written():
    records = {}
    yield records
    if os.environ.get("QUADRATUS_WRITE_STOP_FIXTURES") == "1" and records:
        FIXTURES.write_text(json.dumps(dict(sorted(records.items())), indent=2) + "\n")


@pytest.mark.parametrize("journey", JOURNEYS.values(), ids=list(JOURNEYS))
def test_a_journey_leaves_its_recorded_stop(journey, request, tmp_path, monkeypatch, expected, written):
    name = request.node.callspec.id
    sessions, errors = [], {}
    run = Session.run

    def recording_run(self, *args, **kw):
        sessions.append(self)
        return run(self, *args, **kw)
    monkeypatch.setattr(Session, "run", recording_run)
    workflow = project_run._workflow_record

    def recording_workflow(session, completed, error):
        errors[id(session)] = error
        return workflow(session, completed, error)
    monkeypatch.setattr(project_run, "_workflow_record", recording_workflow)

    wanted = inspect.signature(journey).parameters
    journey(**{k: v for k, v in (("tmp_path", tmp_path), ("monkeypatch", monkeypatch)) if k in wanted})
    assert sessions, "the journey ran a Session"
    norm = _normaliser(tmp_path)
    record = [_record(session, errors, norm) for session in sessions]
    for one in record:
        _consistent(one)
    written[name] = record
    if os.environ.get("QUADRATUS_WRITE_STOP_FIXTURES") != "1":
        assert name in expected, f"no recorded fixture for {name}"
        assert record == expected[name]


def test_every_recorded_fixture_has_a_journey():
    recorded = json.loads(FIXTURES.read_text())
    assert sorted(recorded) == sorted(JOURNEYS)
