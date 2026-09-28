"""The finding/stop projection candidate (map P3.4: open_findings, stop_reason).

Unit cases build the typed record by hand; replay cases run the real Session
offline (scripted replies, no vendor call) and compare the projection with
the legacy inputs the run actually held. Nothing here wires the module into
the engine."""

import copy
import json
from dataclasses import asdict

import pytest

from quadratus import finding_state as FS
from quadratus.outcome import RunOutcome, TaskOutcome, classify


def _named(name):
    """An exception whose class name is ``name``: outcome.classify reads names."""
    return type(name, (Exception,), {})("stopped")


def _task(tid, *facts, closed_as="closed", open_at_close=None):
    outcome = TaskOutcome(tid, "implementation")
    outcome.closed_as = closed_as
    for kind, detail, stage in facts:
        outcome.note(kind, detail, stage=stage)
    if open_at_close is not None:
        outcome.open_at_close = open_at_close
    return outcome


def _ledger(fid, status="open", task="t1", **extra):
    return dict(id=fid, task=task, requirements=["R1"], kind="product.overflow", status=status,
                message=f"{fid} overflows", **extra)


DESIGN_T2 = ("invalid_proof", "Task t2 is design work without clean rendered evidence: stale.", "design")


# -- identity -----------------------------------------------------------------

def test_identity_survives_a_later_record_and_a_recovery():
    t1 = _task("t1", ("cap", "stopped at the lead turn limit (14 turns)", "draft"),
               ("unverified", "BLOCKING: x", "review"), closed_as="turn_limited")
    run = RunOutcome()
    before = FS.project([t1], run, [_ledger("F1")])
    ids = {i.id for i in before.active}
    assert ids == {"ledger:F1", "task:t1:draft:cap:1", "task:t1:review:unverified:1"}

    # More facts, a new task, a ledger row, and t1's cap recovered by a CONTINUES.
    t1.note("unverified", "BLOCKING: y", stage="review")
    t2 = _task("t2", DESIGN_T2)
    t1.recover("cap")
    after = FS.project([t1, t2], run, [_ledger("F1"), _ledger("F2")])
    by_id = {i.id: i for i in after.active + after.history}
    assert ids <= set(by_id), "no earlier item was renumbered"
    assert by_id["task:t1:draft:cap:1"].state == "recovered"
    assert by_id["task:t1:review:unverified:1"].detail == "BLOCKING: x"
    assert by_id["task:t1:review:unverified:2"].detail == "BLOCKING: y"


def test_a_repeated_task_id_gets_its_own_identity():
    a, b = _task("t1", DESIGN_T2), _task("t1", DESIGN_T2)
    state = FS.project([a, b], RunOutcome())
    assert [i.id for i in state.active] == ["task:t1:design:invalid_proof:1",
                                            "task:t1#2:design:invalid_proof:1"]


# -- recovered versus active --------------------------------------------------

def test_recovered_and_non_terminal_facts_are_history_never_dropped():
    t1 = _task("t1")
    t1.note("transport", "ProviderError: timeout", stage="draft").recovered = True
    t1.note("cap", "stopped at the lead turn limit (14 turns)", stage="draft")
    run = RunOutcome()
    run.note("unverified", "DONE sent back: audit findings open", terminal=False)
    state = FS.project([t1], run)
    assert [i.category for i in state.active] == ["cap"]
    assert {(i.category, i.state) for i in state.history} == {("transport", "recovered"),
                                                              ("sent_back", "non_terminal")}
    assert state.stop is None and state.findings() == []


# -- audit debt versus design debt -------------------------------------------

def test_audit_debt_comes_from_the_ledger_and_design_debt_from_its_task():
    audit = _task("t1", closed_as="closed")
    design = _task("t2", DESIGN_T2, ("unverified", "Task t2 design: BLOCKING: cramped", "design"))
    state = FS.project([audit, design], RunOutcome(),
                       [_ledger("F1", reopened="its resolving evidence no longer holds"),
                        _ledger("F2", status="resolved", resolved_by="t3")])
    assert state.audit_debt() == ["F1"]
    f1 = next(i for i in state.active if i.id == "ledger:F1")
    assert f1.debt == "audit" and f1.refs == ("F1", "R1") and f1.notes[0].startswith("reopened: ")
    assert [(i.category, i.debt) for i in state.design_debt()] == [("design_evidence", "design"),
                                                                    ("design_review", "design")]
    assert state.design_debt({"t1"}) == [], "another task's design debt is never this task's (G8)"
    assert next(i for i in state.history if i.id == "ledger:F2").state == "resolved"
    assert "ledger:F1" not in [i.id for i in state.findings()], "audit debt is not a legacy open-finding text"


def test_the_ledger_outranks_a_stale_task_snapshot_and_missing_fails_closed():
    t1 = _task("t1", open_at_close=dict(findings=["F1", "F9"], requirements=[]))
    state = FS.project([t1], RunOutcome(), [_ledger("F1", status="resolved"), _ledger("F2", status="waived")])
    f1 = next(i for i in state.history if i.id == "ledger:F1")
    assert f1.state == "resolved" and "resolved in the ledger since" in f1.notes[-1]
    f9 = next(i for i in state.active if i.id == "ledger:F9")
    assert f9.category == "ledger_missing" and f9.debt == "audit"
    assert "ledger:F2" in {i.id for i in state.active}, "an unknown status stays open"
    assert any("F9" in p for p in state.problems) and any("waived" in p for p in state.problems)


# -- parallel merged and unmerged children -----------------------------------

def test_a_merged_childs_finding_and_an_unmerged_childs_are_both_typed():
    merged = _task("t1", ("unverified", "BLOCKING: naming", "review"))
    unmerged = _task("t2", ("operator", "KeyError: 'x'", "draft"), closed_as="stopped:KeyError")
    unmerged.note("operator", "not merged: KeyError: 'x'", stage="merge")
    state = FS.project([merged, unmerged], RunOutcome())
    legacy = ["BLOCKING: naming",
              "Parallel task t2 was not merged (KeyError: 'x'); its files are kept in artifact a1b2."]
    assert FS.legacy_parity(state, open_findings=legacy)["agree"]
    assert {i.category for i in state.for_task("t2")} == {"stopped", "unmerged"}


def test_an_unmerged_child_with_no_typed_outcome_is_a_disagreement():
    legacy = ["Parallel task t2 was not merged (changed files outside its scope: x.py); "
              "its files are kept in artifact a1."]
    report = FS.legacy_parity(FS.project([], RunOutcome()), open_findings=legacy)
    assert not report["agree"]
    assert report["problems"] == ["legacy open finding without a typed fact (x1): not merged: changed files "
                                  "outside its scope: x.py"]


# -- disagreement handling ---------------------------------------------------

def test_a_typed_finding_missing_from_the_legacy_list_is_reported_not_raised():
    state = FS.project([_task("t1", ("security", "Security verification reject: raw", "verification"))],
                       RunOutcome())
    report = FS.legacy_parity(state, open_findings=[])
    assert not report["agree"] and report["problems"][0].startswith("typed finding absent from the legacy list")


def test_design_debt_the_legacy_list_cleared_but_the_fact_keeps_is_reported():
    # G8 clearance (session.py:3339) drops the legacy entry; the fact stays active.
    state = FS.project([_task("t2", DESIGN_T2)], RunOutcome())
    report = FS.legacy_parity(state, open_findings=[DESIGN_T2[1]], design_unverified=[])
    assert report["problems"] == ["design debt: typed ['t2'], legacy []"]


def test_an_unknown_fact_class_stays_active_and_is_reported():
    state = FS.project([_task("t1", ("mystery", "?", "draft"))], RunOutcome())
    assert state.active[0].kind == "mystery" and state.problems


# -- the stop ---------------------------------------------------------------

@pytest.mark.parametrize("name, kind", [("ProviderRefusal", "refusal"), ("OperatorInputNeeded", "operator"),
                                        ("RunBudgetExceeded", "budget"), ("KeyError", "operator")])
def test_an_exception_stop_is_kept_whatever_debt_is_open(name, kind):
    exc = _named(name)
    run = RunOutcome()
    run.note(classify(exc), f"{name}: stopped", legacy=name)
    state = FS.project([_task("t2", DESIGN_T2)], run, [_ledger("F1")])
    assert (state.stop.name, state.stop.kind) == (name, kind)
    assert {i.debt for i in state.active} == {"audit", "design"}, "debt is listed beside the stop"
    assert state.outranked_by == ()
    assert FS.legacy_parity(state, open_findings=[DESIGN_T2[1]], error=f"{name}: stopped")["agree"]


def test_a_named_stop_ranked_below_open_work_is_kept_and_the_outranking_reported():
    run = RunOutcome()
    run.note("unverified", "DesignUnverified: task t2 ... Work preserved.", legacy="DesignUnverified")
    state = FS.project([_task("t2", DESIGN_T2, ("security", "Security verification reject: r", "verification"))],
                       run)
    assert state.stop.name == "DesignUnverified"
    # invalid_proof ranks above unverified, so the stop's own cause outranks
    # the class DesignUnverified is recorded under (session.py:4411).
    assert state.outranked_by == ("task:t2:verification:security:1", "task:t2:design:invalid_proof:1")


@pytest.mark.parametrize("prior", ["security", "refusal"])
def test_an_end_of_run_dependency_change_under_a_higher_stop_is_secondary(prior):
    run = RunOutcome()
    run.note(prior, "FindingsOpen: a task closed with open work, but x. Work preserved.", legacy="FindingsOpen")
    run.note("integrity", "DependencyTreeChanged: node_modules changed")  # session.py:2954
    state = FS.project([], run)
    assert state.stop.name == "FindingsOpen"
    assert [(i.category, i.kind) for i in state.active] == [("dependency", "integrity")]


def test_a_later_named_stop_replaces_an_earlier_one_which_stays_a_fact():
    run = RunOutcome()
    run.note("cap", "GoalUnconfirmedAtCap: cap. Work preserved.", legacy="GoalUnconfirmedAtCap")
    run.note("integrity", "DependencyTreeChanged: changed", legacy="DependencyTreeChanged")
    state = FS.project([], run)
    assert state.stop.name == "DependencyTreeChanged"
    assert [i.category for i in state.active] == ["stop"]
    assert FS.legacy_parity(state, error="DependencyTreeChanged: changed")["agree"]


def test_a_truncated_stop_detail_is_a_gap_not_a_disagreement():
    reason = "FindingsOpen: a task closed with open work, but " + "x" * 600 + ". Work preserved."
    run = RunOutcome()
    run.note("unverified", reason, legacy="FindingsOpen")
    report = FS.legacy_parity(FS.project([], run), stop_reason=reason, error=reason)
    assert report["agree"] and report["gaps"] == [
        f"stop FindingsOpen: typed detail is a 400-character prefix of the {len(reason)}-character legacy text"]


def test_an_untyped_exception_after_the_loop_is_a_stop_disagreement():
    # run() lines 2957-2960: _recheck_resolved_findings raising after a named stop.
    run = RunOutcome()
    run.note("cap", "GoalUnconfirmedAtCap: cap. Work preserved.", legacy="GoalUnconfirmedAtCap")
    report = FS.legacy_parity(FS.project([], run), error="OSError: disk", stop_reason="GoalUnconfirmedAtCap: cap.")
    assert report["problems"] == ["stop: typed 'GoalUnconfirmedAtCap', legacy 'OSError'"]


def test_no_stop_and_no_error_agree():
    assert FS.legacy_parity(FS.project([], RunOutcome()))["agree"]


# -- purity and the result.json form ------------------------------------------

def test_projection_reads_and_never_writes_and_the_stored_form_projects_the_same():
    t1 = _task("t1", ("cap", "stopped at the lead turn limit (14 turns)", "draft"), DESIGN_T2,
               open_at_close=dict(findings=["F1"], requirements=[]))
    t1.recover("cap")
    run = RunOutcome()
    run.note("unverified", "FindingsUnresolved: F1 open. Work preserved.", legacy="FindingsUnresolved")
    ledger = [_ledger("F1")]
    before = (copy.deepcopy(t1.to_dict()), copy.deepcopy(asdict(run)), copy.deepcopy(ledger))
    live = FS.project([t1], run, ledger)
    assert (t1.to_dict(), asdict(run), ledger) == before
    stored = json.loads(json.dumps({"workflow": {"tasks": [t1.to_dict()], "run": asdict(run)}, "findings": ledger}))
    assert FS.from_result(stored) == live


# -- offline replays through the real Session ---------------------------------

@pytest.fixture
def captured(monkeypatch):
    """The Session each replay ran, read after it ended; nothing is changed."""
    from quadratus.session import Session
    seen = []
    original = Session.run

    def run(self, *args, **kwargs):
        seen.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Session, "run", run)
    return seen


def _parity(session, error):
    return FS.legacy_parity(FS.from_session(session), open_findings=list(session.open_findings), error=error,
                            stop_reason=session.stop_reason, design_unverified=list(session._design_unverified))


def _capped_lead(call, replay):
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import FIXED
    H.write(call, {"app.py": FIXED})
    return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)


def _looked(call, replay):
    return "Looked; left it.\nCHANGED: []"


def _orchestrate_capped_then_done(call, replay):
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1
    return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"


REPLAYS = {
    # tests/lifecycle/test_named_stops.py journeys, projected instead of string-matched.
    "CheckFailing": (dict(lead=_looked), dict(max_tasks=3), "product"),
    "GoalUnconfirmedAtCap": ({}, dict(max_tasks=1), "cap"),
    "DoneWithOpenWork": (dict(orchestrator=_orchestrate_capped_then_done, lead=_capped_lead),
                         dict(max_tasks=4, capped=True), "cap"),
}


@pytest.mark.parametrize("name", sorted(REPLAYS))
def test_named_stop_replays_agree_with_the_legacy_inputs(name, tmp_path, monkeypatch, captured):
    from quadratus.config import Settings
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import FILES, Script
    overrides, run_kw, kind = REPLAYS[name]
    run_kw = dict(run_kw)
    if run_kw.pop("capped", False):
        run_kw["settings"] = Settings(backend="cli", lead_max_turns=14)
    replay = H.run(tmp_path, monkeypatch, Script(**overrides), files=FILES, **run_kw)
    session = captured[-1]
    report = _parity(session, replay.result.error)
    assert report["agree"], report
    state = FS.from_session(session)
    assert (state.stop.name, state.stop.kind) == (name, kind)
    assert FS.from_result(H.result_json(replay)) == state, "the stored record projects the same"


def test_a_clean_completion_projects_no_stop_and_no_findings(tmp_path, monkeypatch, captured):
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import FILES, Script
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_orchestrate_capped_then_done), files=FILES,
                   max_tasks=3)
    assert replay.result.completed and replay.result.error == ""
    state = FS.from_session(captured[-1])
    assert state.stop is None and state.findings() == [] and state.audit_debt() == []
    assert _parity(captured[-1], replay.result.error)["agree"]


def test_a_stalled_orchestrator_replay_keeps_its_operator_stop(tmp_path, monkeypatch, captured):
    # The default script names the same task twice: RunStalled is raised, and
    # project_run reports the exception, not any open work behind it.
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import FILES, Script
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, max_tasks=3)
    assert replay.result.error.startswith("RunStalled: ")
    state = FS.from_session(captured[-1])
    assert (state.stop.name, state.stop.kind) == ("RunStalled", "operator")
    report = _parity(captured[-1], replay.result.error)
    assert report["agree"], report


def test_a_declined_plan_is_an_operator_stop(tmp_path):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("Build it", ArtifactStore(tmp_path / "a"), lambda *a, **k: "KIND: backend simple\nBuild it.",
                      config=SessionConfig(plan_gate=lambda plan: False))
    session.run(max_tasks=3)
    state = FS.from_session(session)
    assert (state.stop.name, state.stop.kind) == ("PlanDeclined", "operator")
    assert _parity(session, session.stop_reason)["agree"]


def test_a_parallel_childs_design_debt_replay_agrees(tmp_path):
    from tests.test_design_debt_binding import DesignOrchestrated, _design_block
    from tests.test_parallel_tasks import _session as parallel_session
    batch = "PARALLEL\n" + _design_block("templates/a.html") + "\n---\n" + _design_block("templates/b.html")
    session, _ = parallel_session(tmp_path, DesignOrchestrated([batch, "DONE"], lead_delay=0))
    session.run(max_tasks=4)
    state = FS.from_session(session)
    report = _parity(session, session.stop_reason)
    assert report["agree"], report
    assert state.stop.name == "DesignUnverified"
    assert {i.task for i in state.design_debt()} == {d[0] for d in session._design_unverified}


def test_a_parallel_unmerged_child_replay_agrees(tmp_path):
    from tests.test_parallel_tasks import BATCH, Orchestrated
    from tests.test_parallel_tasks import _session as parallel_session
    session, _ = parallel_session(tmp_path, Orchestrated([BATCH, "DONE"], lead_delay=0, rogue="b.py"))
    session.run(max_tasks=4)
    state = FS.from_session(session)
    report = _parity(session, session.stop_reason)
    assert report["agree"], report
    unmerged = [i for i in state.findings() if i.category == "unmerged"]
    assert [(i.task, i.kind) for i in unmerged] == [("t2", "integrity")]
