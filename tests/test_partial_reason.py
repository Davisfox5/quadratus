"""Both open-work readers name partial work from typed and legacy together
(map P3.4; Codex ruling 5864252244).

The blocking predicate already reads typed ∪ legacy partiality; the reason
text read the legacy set alone, so typed-only partial work blocked a run
whose stop said "the record shows no single open item". ``Session._open_work``
and ``completion_decision._open_work`` now name the union, sorted and
deduplicated; the text is unchanged whenever the two sources agree. The
typed-only case is synthetic: no natural journey loses the legacy entry.
"""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.completion_decision import DONE_REPLY, _open_work, assess, snapshot_session
from quadratus.outcome import TaskOutcome
from quadratus.session import Session


def _session(tmp_path, typed, legacy):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE")
    for tid in ("t1", "t2", "t3"):
        outcome = TaskOutcome(tid, "build")
        if tid in typed:
            outcome.note("cap", f"{tid} reached its turn limit")
        session.task_outcomes.append(outcome)
    session._partial_tasks = set(legacy)
    return session


def _readers(session):
    inputs = snapshot_session(session, site=DONE_REPLY)
    return session._open_work(), _open_work(inputs, assess(inputs))


@pytest.mark.parametrize("typed, legacy, named", [
    ({"t2"}, set(), "t2"),                 # typed only (synthetic)
    (set(), {"t3"}, "t3"),                 # legacy only
    ({"t1", "t2"}, {"t2", "t3"}, "t1, t2, t3"),  # overlap: union, sorted, once each
    ({"t2"}, {"t2"}, "t2"),                # agreement: today's text
])
def test_both_readers_name_the_union(tmp_path, typed, legacy, named):
    session_reasons, decision_reasons = _readers(_session(tmp_path, typed, legacy))
    expected = [f"capped or failed task(s) {named} not continued to completion"]
    assert session_reasons == expected
    assert decision_reasons == session_reasons, "the two readers agree"


def test_no_partial_work_names_none(tmp_path):
    session_reasons, decision_reasons = _readers(_session(tmp_path, set(), set()))
    assert session_reasons == [] and decision_reasons == []
