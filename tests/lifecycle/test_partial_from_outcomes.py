"""Unresolved partial work is read from the typed record (map P3.4,
``_partial_tasks``).

``_unresolved_partial`` (both DONE sites and the cap's goal-question
short-circuit) used to read the legacy ``_partial_tasks`` set alone. It now
also derives the set from the outcomes (an active cap, or an active
not-merged fact on a parallel child) and treats either as blocking; a
disagreement is recorded on the task, so the run cannot count as complete.
The legacy set stays the report mirror. The ordinary journeys (J20, J22,
J23, DONE and the cap over capped debt, a clean cap) are the existing
lifecycle replays, each of which asserts a complete record.
"""

import json

from quadratus.providers import TurnLimitReached
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
from tests.lifecycle.test_named_stops import _capped_lead, _settings
from tests.test_parallel_tasks import BATCH, Orchestrated
from tests.test_parallel_tasks import _session as parallel_session


def test_capped_work_lost_from_the_legacy_set_still_blocks_done(tmp_path, monkeypatch):
    """Injected: the legacy set loses t1 before DONE; the typed cap still holds."""
    verify = Session._verify_dependencies

    def losing(self, window):
        if window == "at DONE":
            self._partial_tasks.clear()
        return verify(self, window)
    monkeypatch.setattr(Session, "_verify_dependencies", losing)

    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=_capped_lead), files=FILES,
                   max_tasks=4, settings=_settings(), record_complete=False)
    assert not replay.result.completed
    assert replay.result.error.startswith("DoneWithOpenWork: the orchestrator reported DONE"), replay.result.error
    t1 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    assert "partial: typed True, legacy False" in json.dumps(t1)


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches]


def test_an_out_of_scope_parallel_child_is_partial_in_both(tmp_path):
    session, _ = parallel_session(tmp_path, Orchestrated([BATCH, "DONE"], lead_delay=0, rogue="b.py"),
                                  requirements_ledger=False)
    session.run(max_tasks=3)
    assert session._partial_tasks and session._partial_from_outcomes() == set(session._partial_tasks)
    assert session._unresolved_partial and not _mismatches(session)


def test_a_capped_parallel_child_is_partial_in_both(tmp_path, monkeypatch):
    recorded = Session._run_task_recorded

    def capped(self, spec):
        # Inside run_task, where a real turn-limit exception surfaces.
        if " in b.py" in spec.description:
            raise TurnLimitReached("the child stopped at its turn limit", turns=14)
        return recorded(self, spec)
    monkeypatch.setattr(Session, "_run_task_recorded", capped)
    session, _ = parallel_session(tmp_path, Orchestrated([BATCH, "DONE"], lead_delay=0),
                                  requirements_ledger=False)
    session.run(max_tasks=3)
    typed, legacy = session._partial_from_outcomes(), set(session._partial_tasks)
    assert legacy and typed == legacy, (typed, legacy)
    assert not _mismatches(session)
