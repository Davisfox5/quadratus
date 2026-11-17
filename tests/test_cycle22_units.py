"""Cycle22 units: when a bound case counts as gone (A), the NOT RUN binder's
ranges and mixed claims (B), the one fold decision (C), and the prior-task
scenario route's resume and packet bound (D). The lifecycle controls are in
tests/lifecycle/test_cycle22_corrections.py."""
import json
from types import SimpleNamespace

import pytest

from quadratus import checkpoint
from quadratus import session as S
from quadratus.memory import TaskSummary
from quadratus.outcome import TaskOutcome
from quadratus.scope import NOT_RUN_RULE
from tests.test_done_declarations_units import _session

OLD = "tests/test_api.py::API::test_handler_cleanup_error"
NEW = "tests/test_api.py::API::test_handler_cleanup_error_keeps_committed_result"


def _project(tmp_path, *, old=False):
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    body = "import unittest\nclass API(unittest.TestCase):\n    def test_handler_cleanup_error_keeps_committed_result(self):\n        pass\n"
    if old:
        body += "    def test_handler_cleanup_error(self):\n        pass\n"
    (tmp_path / "tests/test_api.py").write_text(body)
    return tmp_path


def _gone_session(tmp_path, monkeypatch, *, passed=(NEW,), check_passed=True, complete=True, old=False):
    item = dict(task="t3", oid="O37", item=OLD, why="socket denied", role="lead", cases=[OLD], kind="not_run",
                binding=dict(kind="case", cases=[OLD]), requirements=["R12"], after_check=0, executable=True)
    session, _ = _session(_project(tmp_path, old=old), entries=[item], passed=check_passed)
    monkeypatch.setattr(S, "_check_runs", lambda check, root=None: [dict(id="c", report_cases=list(passed))])
    monkeypatch.setattr(S, "_complete_run", lambda run: complete)
    monkeypatch.setattr(S, "_run_passed_cases", lambda run: set(run.get("report_cases") or ()))
    return session, session.unexecuted_acceptance[0]


# -- A ------------------------------------------------------------------------------------------

def test_a_renamed_case_is_gone_only_with_a_current_complete_passing_run_that_lacks_it(tmp_path, monkeypatch):
    session, entry = _gone_session(tmp_path, monkeypatch)
    assert session._bound_cases_gone(entry) == [OLD] and not session._not_run_runnable(entry)
    assert entry["executable"] is True, "the recorded decision is kept; the current reading is computed"


@pytest.mark.parametrize("name, kw", [
    ("the latest check failed", dict(check_passed=False)),
    ("no complete run (skips or a filter)", dict(complete=False)),
    ("the case still executes", dict(passed=(NEW, OLD))),
    ("the case is still defined", dict(old=True)),
    ("no run names a case", dict(passed=())),
])
def test_anything_short_of_that_keeps_the_item_runnable(tmp_path, monkeypatch, name, kw):
    session, entry = _gone_session(tmp_path, monkeypatch, **kw)
    assert session._bound_cases_gone(entry) == [] and session._not_run_runnable(entry), name


def test_a_moved_source_or_another_binding_kind_is_not_gone(tmp_path, monkeypatch):
    session, entry = _gone_session(tmp_path, monkeypatch)
    session._source_fingerprint = lambda: "moved"
    assert session._bound_cases_gone(entry) == [], "stale: the latest check ran on another source"
    session, entry = _gone_session(tmp_path / "s", monkeypatch)
    for kind in ("scenario", "class", "command", "mixed"):
        entry["binding"] = dict(kind=kind, cases=[OLD])
        assert session._bound_cases_gone(entry) == [], kind
    entry.update(binding=dict(kind="case", cases=[OLD]), kind="skipped")
    assert session._bound_cases_gone(entry) == [], "a skip fact settles only by execution"


def test_a_gone_item_is_declaration_debt_not_editing_and_reads_the_same_after_a_resume(tmp_path, monkeypatch):
    session, entry = _gone_session(tmp_path, monkeypatch)
    session.config.requirements_ledger = True
    session.memory.ledger.requirements.update({"R12": "the endpoint behaves"})
    session._refresh_obligations = lambda: None
    blocked = session._deterministic_blockers()
    assert list(blocked) == ["R12"] and session._blockers_need_editing is False
    assert "no longer defined on the current source" in session._row_route_note(dict(kind="not_run", oid="O37"))
    state = json.loads(json.dumps(checkpoint.capture_state(session), default=str))
    resumed, _ = _session(_project(tmp_path / "r"), entries=[])
    resumed._source_fingerprint = lambda: "S"
    checkpoint.restore_state(resumed, state)
    assert resumed._bound_cases_gone(resumed.unexecuted_acceptance[0]) == [OLD]


def test_a_runnable_failed_case_still_needs_an_edit(tmp_path, monkeypatch):
    session, entry = _gone_session(tmp_path, monkeypatch, check_passed=False, old=True)
    session.config.requirements_ledger = True
    session.memory.ledger.requirements.update({"R12": "x"})
    session._refresh_obligations = lambda: None
    session._deterministic_blockers()
    assert session._bound_cases_gone(entry) == []


# -- B ------------------------------------------------------------------------------------------

def _binder(tmp_path, declared=("P1", "P2", "P3", "P4")):
    session, _ = _session(_project(tmp_path), entries=[])
    session.evidence_obligations = [dict(oid=f"O{i}", task="t8", kind="acceptance", proof_id=pid, cases=[NEW])
                                    for i, pid in enumerate(declared, 1)]
    return session


@pytest.mark.parametrize("item", ["P1-P4 HTTP acceptance", "P1–P4 HTTP acceptance", "P1 - P4 acceptance",
                                  "P1, P2, P3, P4"])
def test_both_dashes_read_the_full_inclusive_range(tmp_path, item):
    entry = dict(task="t8", item=item)
    _binder(tmp_path)._bind_not_run(entry)
    assert entry["binding"]["scenario"] == "P1, P2, P3, P4" and entry["cases"] == [NEW]


@pytest.mark.parametrize("item, why", [
    ("P1-P5", "names P5, which this task does not declare"),
    ("P3-P1", "is not an increasing range"),
    ("P2-P2", "is not an increasing range"),
    ("P1-P2-P4", "chains more than one range"),
    ("mutation detection and P1–P4 HTTP acceptance", "a claim a passing run cannot establish (mutation)"),
    ("P1-P4 mutants killed", "a claim a passing run cannot establish (mutants)"),
])
def test_an_unusable_range_or_a_mutation_claim_binds_nothing_and_says_why(tmp_path, item, why):
    entry = dict(task="t8", item=item)
    _binder(tmp_path)._bind_not_run(entry)
    assert entry["cases"] == [] and why in entry["binding"]["problem"]


def test_a_range_never_binds_another_tasks_scenarios(tmp_path):
    entry = dict(task="t9", item="P1-P2")
    _binder(tmp_path)._bind_not_run(entry)
    assert entry["cases"] == [] and "which this task does not declare" in entry["binding"]["problem"]


def test_the_editing_seat_and_the_planner_are_told_the_same_forms():
    assert "an inclusive range 'P<a>-P<b>' binds only when every scenario in it is declared" in NOT_RUN_RULE
    assert "such as mutation detection, stays held" in NOT_RUN_RULE


# -- C ------------------------------------------------------------------------------------------

def test_the_fold_is_one_decision_for_admission_and_serving():
    assert S._folds_binding(dict(binding=[1]), True) is True
    assert S._folds_binding(dict(binding=[1]), False) is False
    assert S._folds_binding(dict(binding=[]), True) is False


# -- D ------------------------------------------------------------------------------------------

def _prior_session(tmp_path):
    session, _ = _session(_project(tmp_path), entries=[])
    proof = dict(id="P1", requirements=["R1"], operation="add is called", state="two tickets exist",
                 expect="the sum is returned", location="tests/test_api.py")
    scope = dict(permitted_paths=["app.py"], forbidden_paths=[], intended_result="add works",
                 acceptance=["add(1, 2) == 3"], max_lines=20, proofs=[proof])
    session.task_outcomes = [TaskOutcome(task_id="t7", intent="implementation", lead="claude:opus",
                                         contract=dict(scope=scope))]
    session.history = [TaskSummary(task_id="t7", author="claude:opus", summary="t7 closed", reasoning="")]
    session.evidence_obligations = [dict(oid="O61", task="t7", kind="acceptance", proof_id="P1", requirements=[["R1", 1]],
                                         location="tests/test_api.py",
                                         scenario=sorted(dict(proof="P1", operation="add is called",
                                                              state="two tickets exist", expect="the sum is returned",
                                                              location="tests/test_api.py", evidence="test").items()))]
    return session


def test_a_qualified_prior_correction_survives_a_resume_as_spent(tmp_path):
    session = _prior_session(tmp_path)
    spec = SimpleNamespace(task_id="t8")
    assert session._prior_scenario_problem(spec, "t7", "P1") == ""
    session._scenario_corrected.add("t7:P1")
    state = json.loads(json.dumps(checkpoint.capture_state(session), default=str))
    resumed = _prior_session(tmp_path / "r")
    checkpoint.restore_state(resumed, state)
    assert "already corrected once" in resumed._prior_scenario_problem(spec, "t7", "P1")
    assert "not an earlier closed task" in resumed._prior_scenario_problem(spec, "t8", "P1")


def test_a_prior_correction_is_held_to_that_tasks_own_packet_allowance_and_recorded_on_its_grant(tmp_path):
    from quadratus.policy import load_policy
    session = _prior_session(tmp_path)
    session.config.repository_policy = load_policy(tmp_path)
    assert session._prior_packet_problem("t7", "P1", {"state": "nothing is stored"}) == ""
    assert "over its allowance" in session._prior_packet_problem("t7", "P1", {"state": "x" * 13_000})
    session.packet_requests.append(dict(id="PX-t7-1", task="t7", state="granted", granted=30_000, revisions=[]))
    assert session._prior_packet_problem("t7", "P1", {"state": "x" * 13_000}) == "", "the grant is t7's allowance"
    session._refresh_obligations = lambda: None
    session._sync_obligation = lambda row: None
    session._apply_prior_scenario_correction("t7", "P1", {"state": "nothing is stored"}, findings=["RFT8-2"],
                                             by="orch", raised_in="t8")
    (row,) = session.evidence_obligations
    assert dict(row["scenario"])["state"] == "nothing is stored" and row["execution"] == "missing"
    assert row["scenario_lineage"][-1]["raised_in"] == "t8" and row["requirements"] == [["R1", 1]]
    (revision,) = session.packet_requests[0]["revisions"]
    assert revision["by"] == "prior-task scenario correction" and revision["raised_in"] == "t8"
    assert session.task_outcomes[0].contract["scope"]["proofs"][0]["state"] == "two tickets exist"


def test_the_qualified_claim_types_with_its_task_and_the_unqualified_one_is_unchanged():
    claim = ("SCENARIO t7:P1 - 400 for valid trailing whitespace contradicts R14 | proposal: expect: the request "
             "is accepted")
    assert S._typed_finding(claim) == ("scenario", dict(proof_id="P1", task="t7", reason="400 for valid trailing "
                                                        "whitespace contradicts R14", field="expect",
                                                        proposal="the request is accepted"))
    plain = claim.replace("t7:P1", "P1")
    assert "task" not in S._typed_finding(plain)[1]
    assert S._typed_near_miss("SCENARIO t7:P1 - no proposal") .startswith("a SCENARIO claim without")
