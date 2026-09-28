"""A design recheck records its facts under the design stage (map P3.4;
Codex, 5863678556).

``_check_design``'s fix paths run the integration gate, which sets the
task's stage to ``checks``; the recheck that followed recorded its
``invalid_proof`` finding there too, so a reader keyed on the design stage
(the findings projection) missed it. The session re-enters ``design`` after
the gate. Kinds, stop text, authority and recovery counts are unchanged.
Whole-controller journeys, run as their own tests are.
"""

from quadratus.finding_state import from_session, legacy_parity
from quadratus.session import Session
from tests.lifecycle import test_lifecycle_matrix as matrix
from tests.lifecycle import test_named_stops as named


def _sessions(monkeypatch):
    got = []
    run = Session.run

    def recording(self, *args, **kw):
        got.append(self)
        return run(self, *args, **kw)
    monkeypatch.setattr(Session, "run", recording)
    return got


def _facts(session, kind):
    return [(o.task_id, f.stage) for o in session.task_outcomes for f in o.facts if f.kind == kind]


def test_a_failing_design_recheck_records_its_finding_under_design(tmp_path, monkeypatch):
    got = _sessions(monkeypatch)
    matrix.test_a_capture_whose_interaction_step_failed_leaves_the_design_unverified(tmp_path, monkeypatch)
    session = got[-1]
    assert session.stop_reason.startswith("DesignUnverified: task t1"), session.stop_reason
    assert _facts(session, "invalid_proof") == [("t1", "design")]
    state = from_session(session)
    assert [i.task for i in state.design_debt()] == ["t1"], "the design debt reader sees it"
    parity = legacy_parity(state, open_findings=session.open_findings, stop_reason=session.stop_reason,
                           design_unverified=session._design_unverified)
    assert parity["agree"], parity["problems"]


# -- negatives -----------------------------------------------------------------------

def test_a_successful_recheck_leaves_no_design_finding(tmp_path, monkeypatch):
    got = _sessions(monkeypatch)
    matrix.test_stale_renders_are_recaptured_without_source_edits(tmp_path, monkeypatch)
    session = got[-1]
    assert _facts(session, "invalid_proof") == []
    assert from_session(session).design_debt() == []


def test_ordinary_check_facts_keep_the_checks_stage(tmp_path, monkeypatch):
    got = _sessions(monkeypatch)
    named.test_a_task_closing_with_a_failed_attributed_check_is_named(tmp_path, monkeypatch)
    session = got[-1]
    product = _facts(session, "product")
    assert product and all(stage == "checks" for _, stage in product), product
