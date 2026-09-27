"""The completion guard (phase 3 of the shared workflow plan, #25).

The legacy inputs decide DONE; the typed record must agree before a run
counts as complete. Each case breaks exactly one fact behind an otherwise
clean J1 run and asserts the run is refused with a named stop, zero extra
model calls, and the work kept. On 9c6024b each of these runs completed.
"""

import pytest

from quadratus import session as session_module
from quadratus.outcome import TaskOutcome
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script


def _then_done(first):
    def orchestrator(call, replay):
        return first if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


def _clean(tmp_path, monkeypatch, **kw):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES,
                 max_tasks=3, **kw)


def _refused(replay, *needles):
    result = replay.result
    assert not result.completed, "the record did not support completion"
    assert result.error.startswith("CompletionUnproven: DONE was accepted"), result.error
    for needle in needles:
        assert needle in result.error, (needle, result.error)
    stop = replay.workflow["run"]["facts"][-1]
    assert stop["kind"] == "unverified" and stop["legacy"] == "CompletionUnproven"
    assert (replay.project / "app.py").read_text() != FILES["app.py"], "work preserved"


def test_the_clean_run_the_controls_break_is_complete(tmp_path, monkeypatch):
    replay = _clean(tmp_path, monkeypatch)
    assert replay.result.completed and replay.result.error == ""


def test_an_unsatisfied_mandatory_edge_cannot_count_as_complete(tmp_path, monkeypatch):
    baseline = len(_clean(tmp_path / "a", monkeypatch).calls)
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge",
                        lambda self, name, ok: None if name == "checks" else edge(self, name, ok))
    replay = _clean(tmp_path / "b", monkeypatch)
    _refused(replay, "t1.checks unsatisfied")
    assert len(replay.calls) == baseline, "no call is made to repair a record"


def test_an_incomplete_record_cannot_count_as_complete(tmp_path, monkeypatch):
    record = session_module.Session._record_work

    def losing_source_after(self, outcome, partial):
        record(self, outcome, partial)
        outcome.source_after = None
    monkeypatch.setattr(session_module.Session, "_record_work", losing_source_after)
    replay = _clean(tmp_path, monkeypatch, record_complete=False)
    _refused(replay, "record t1.source_after")


def test_an_active_terminal_task_fact_cannot_count_as_complete(tmp_path, monkeypatch):
    record = session_module.Session._record_work

    def with_a_live_fact(self, outcome, partial):
        record(self, outcome, partial)
        outcome.note("product", "a failure nobody recovered", stage="checks")
    monkeypatch.setattr(session_module.Session, "_record_work", with_a_live_fact)
    replay = _clean(tmp_path, monkeypatch)
    _refused(replay, "t1.product: a failure nobody recovered")


def test_the_cap_confirmation_is_guarded_the_same_way(tmp_path, monkeypatch):
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge",
                        lambda self, name, ok: None if name == "checks" else edge(self, name, ok))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_then_done(DECL_T1)), files=FILES, max_tasks=1)
    assert not replay.result.completed
    assert replay.result.error.startswith("CompletionUnproven: the cap's goal confirmation was accepted")
    assert "t1.checks unsatisfied" in replay.result.error


# -- negative boundaries (Codex review 5858008514) ---------------------------------------

def _dropping(monkeypatch, edge_name):
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge",
                        lambda self, name, ok: None if name == edge_name else edge(self, name, ok))


def _design_done(tmp_path, monkeypatch):
    from tests.lifecycle.test_lifecycle_matrix import _design_files, _design_script
    monkeypatch.setattr(session_module.Session, "_pick_lead", lambda self, spec: "claude:opus")
    script = _design_script("Renders refreshed.\nCHANGED: []")
    first = script.overrides["orchestrator"]
    script.overrides["orchestrator"] = lambda call, replay: (first(call, replay)
                                                            if len(replay.of("orchestrator")) == 1 else "DONE")
    return H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3)


def test_the_design_baseline_completes(tmp_path, monkeypatch):
    replay = _design_done(tmp_path, monkeypatch)
    assert replay.result.completed and replay.result.error == ""


def test_a_missing_delivery_edge_cannot_count_as_complete(tmp_path, monkeypatch):
    _dropping(monkeypatch, "delivered")
    replay = _design_done(tmp_path, monkeypatch)
    assert not replay.result.completed and "t1.delivered unsatisfied" in replay.result.error
    assert replay.result.error.startswith("CompletionUnproven: DONE was accepted")


def test_a_missing_reviewer_edge_cannot_count_as_complete(tmp_path, monkeypatch):
    _dropping(monkeypatch, "reviewer")
    replay = _design_done(tmp_path, monkeypatch)
    assert not replay.result.completed and "t1.reviewer unsatisfied" in replay.result.error


def test_a_missing_security_verification_edge_cannot_count_as_complete(tmp_path, monkeypatch):
    from tests.lifecycle.test_workflow_contract import _security_run
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    baseline = _security_run(tmp_path / "a", monkeypatch, "Accepted: correct, and it cites the function.")
    assert baseline.result.completed
    _dropping(monkeypatch, "verification")
    replay = _security_run(tmp_path / "b", monkeypatch, "Accepted: correct, and it cites the function.")
    assert not replay.result.completed and "t1.verification unsatisfied" in replay.result.error


def test_an_invalid_owner_chain_cannot_count_as_complete(tmp_path, monkeypatch):
    record = session_module.Session._record_work

    def forged(self, outcome, partial):
        record(self, outcome, partial)
        outcome.owner_changes.append({"from": "xai:grok", "to": outcome.lead, "reason": "lead_recovery"})
    monkeypatch.setattr(session_module.Session, "_record_work", forged)
    replay = _clean(tmp_path, monkeypatch, record_complete=False)
    _refused(replay, "record t1.owner_changes from 'xai:grok'")


def _capped_then(tmp_path, monkeypatch, **kw):
    from tests.lifecycle.test_lifecycle_matrix import FIXED, _continuing, _finish
    from tests.lifecycle.test_workflow_contract import CAPPED

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
        return _finish(call, replay)
    continuing = _continuing(DECL_T1)

    def orchestrator(call, replay):
        return continuing(call, replay) if len(replay.of("orchestrator")) <= 2 else "DONE"
    return H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES,
                 max_tasks=4, settings=CAPPED, **kw)


def test_a_recovered_cap_continued_by_a_clean_successor_completes(tmp_path, monkeypatch):
    replay = _capped_then(tmp_path, monkeypatch)
    assert replay.result.completed and replay.result.error == ""


def test_a_successor_with_an_unmet_edge_cannot_hide_the_capped_predecessor(tmp_path, monkeypatch):
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge", lambda self, name, ok: (
        None if (self.task_id, name) == ("t2", "checks") else edge(self, name, ok)))
    replay = _capped_then(tmp_path, monkeypatch)
    assert not replay.result.completed
    assert "t2.checks unsatisfied" in replay.result.error
    assert "t1.checks unsatisfied" in replay.result.error, "the predecessor's debt is not waived"


def test_a_non_recoverable_predecessor_stop_is_not_waived_by_a_continuation(tmp_path, monkeypatch):
    record = session_module.Session._record_work

    def with_integrity(self, outcome, partial):
        record(self, outcome, partial)
        if outcome.task_id == "t1":
            outcome.note("integrity", "evidence identity mismatch")
    monkeypatch.setattr(session_module.Session, "_record_work", with_integrity)
    replay = _capped_then(tmp_path, monkeypatch)
    assert not replay.result.completed and "t1.integrity: evidence identity mismatch" in replay.result.error


# -- discharge by fact only (Codex review 5858204394) ------------------------------------

def _audit_then_repair(tmp_path, monkeypatch, **kw):
    from tests.lifecycle.test_audit_findings import AUDIT, REPAIR, REQS, WIDE, _capture, _repair
    from tests.lifecycle.test_audit_findings import _run as audit_run
    return audit_run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                     {"t1": _capture(measured=WIDE), "t2": _repair()}, **kw)


@pytest.mark.requirements_ledger
def test_settled_audit_debt_does_not_discharge_the_audits_checks(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _audit_then_repair(tmp_path / "a", monkeypatch).result.completed, "the valid audit baseline"
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge", lambda self, name, ok: (
        None if (self.task_id, name) == ("t1", "checks") else edge(self, name, ok)))
    replay = _audit_then_repair(tmp_path / "b", monkeypatch)
    assert not replay.result.completed and "t1.checks unsatisfied" in replay.result.error
    assert replay.result.error.startswith("CompletionUnproven: DONE was accepted")


@pytest.mark.requirements_ledger
def test_settled_audit_debt_does_not_discharge_the_audits_checks_at_the_cap(tmp_path, monkeypatch):
    edge = TaskOutcome.edge
    monkeypatch.setattr(TaskOutcome, "edge", lambda self, name, ok: (
        None if (self.task_id, name) == ("t1", "checks") else edge(self, name, ok)))
    replay = _audit_then_repair(tmp_path, monkeypatch, max_tasks=2)
    assert not replay.result.completed
    assert replay.result.error.startswith("CompletionUnproven: the cap's goal confirmation was accepted")
    assert "t1.checks unsatisfied" in replay.result.error


def test_a_docs_continuation_does_not_discharge_a_capped_design_tasks_verification(tmp_path, monkeypatch):
    """Codex control: t1 edits the page and caps with no renders or review; t2
    is a docs task that CONTINUES t1 and passes its own checks. Nobody
    captured or reviewed the UI, so t1's design edges stay unmet."""
    from tests.lifecycle.test_lifecycle_matrix import (
        DECL_DESIGN,
        _continuing,
        _design_files,
        _finish,
    )
    from tests.lifecycle.test_workflow_contract import CAPPED
    monkeypatch.setattr(session_module.Session, "_pick_lead", lambda self, spec: "claude:opus")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
            return H.claude_cap("Added the button; not captured yet.", num_turns=14)
        return _finish(call, replay)
    continuing = _continuing(DECL_DESIGN)

    def orchestrator(call, replay):
        return continuing(call, replay) if len(replay.of("orchestrator")) <= 2 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=_design_files(),
                   max_tasks=4, settings=CAPPED)
    t1 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    assert t1["closed_as"] == "turn_limited" and not replay.of("design-review")
    assert not replay.result.completed
    for edge in ("evidence", "delivered", "reviewer"):
        assert f"t1.{edge} unsatisfied" in replay.result.error, edge


@pytest.mark.requirements_ledger
def test_a_same_state_continuation_that_captures_and_resolves_discharges_the_capped_repair(tmp_path, monkeypatch):
    """The positive boundary: t2 caps a RESOLVES repair; t3 CONTINUES it under
    the same measured state, captures, is reviewed and resolves F1."""
    from quadratus.config import Settings
    from tests.lifecycle.test_audit_findings import AUDIT, REPAIR, REQS, WIDE, _capture
    from tests.lifecycle.test_audit_findings import _run as audit_run

    def capped(call, replay):
        H.write(call, {"static/style.css": ".toolbar { flex-wrap: wrap; }\n"})
        return H.claude_cap("Wrapped the toolbar; not captured yet.", num_turns=14)
    finish = REPAIR.replace("Fix the mobile overflow.", "Finish the capped repair.") + "\nCONTINUES: t2\nRESOLVES: F1"
    replay = audit_run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", finish],
                       {"t1": _capture(measured=WIDE), "t2": capped, "t3": _capture()},
                       settings=Settings(backend="cli", lead_max_turns=14))
    t2, t3 = (next(t for t in replay.workflow["tasks"] if t["task_id"] == tid) for tid in ("t2", "t3"))
    assert t2["unsatisfied"] and t2["contract"]["intended_state"] == t3["contract"]["intended_state"]
    assert replay.result.completed, replay.result.error
