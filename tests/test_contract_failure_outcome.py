"""A task that fails while its contract is built still has a typed outcome
(map P3.4, the unmerged-child gap).

``run_task`` used to build the contract before it put the task's outcome on
the record, so an exception there left no outcome. For a parallel child that
meant the batch's "not merged" open finding had no typed fact beside it. The
stop itself is unchanged: the exception is still fatal to the batch and is
re-raised. Whole Session on the parallel harness; the contract failure is
injected, a controller invariant rather than observed behaviour.
"""

import pytest

from quadratus.policy import PolicyError
from quadratus.session import Session
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _failing_contract(monkeypatch, path):
    build = Session._build_contract

    def broken(self, spec, outcome):
        if f" in {path}" in spec.description:
            raise PolicyError(f"contract for {path} could not be built")
        return build(self, spec, outcome)
    monkeypatch.setattr(Session, "_build_contract", broken)


def _stopped_before_dispatch(outcome):
    assert outcome.closed_as == "stopped:PolicyError"
    assert outcome.dispatch["state"] == "not_dispatched"
    assert "contract for" in outcome.dispatch["reason"]
    assert any(f.kind == "operator" and "PolicyError" in f.detail for f in outcome.facts)


def test_an_unmerged_child_that_failed_building_its_contract_has_a_typed_outcome(tmp_path, monkeypatch):
    _failing_contract(monkeypatch, "b.py")
    script = Orchestrated([BATCH, "DONE"], lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    with pytest.raises(PolicyError):
        session.run(max_tasks=4)
    (child,) = [o for o in session.task_outcomes if o.closed_as.startswith("stopped:")]
    _stopped_before_dispatch(child)
    assert any("not merged: PolicyError" in f.detail for f in child.facts), "the batch's finding is typed"
    unmerged = [f for f in session.open_findings if "was not merged (PolicyError" in f]
    assert len(unmerged) == 1, session.open_findings
    # Unchanged: still fatal to the batch, re-raised, and the run's stop is typed.
    assert not (project / "b.py").exists() and (project / "a.py").exists(), "the sibling still merged"
    assert session.run_outcome.facts[-1].kind == "operator"
    assert session.run_outcome.facts[-1].legacy == "PolicyError"


def test_a_serial_task_that_failed_building_its_contract_has_a_typed_outcome(tmp_path, monkeypatch):
    _failing_contract(monkeypatch, "c.py")
    script = Orchestrated([_block("c.py"), "DONE"], lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    with pytest.raises(PolicyError):
        session.run(max_tasks=4)
    (outcome,) = session.task_outcomes
    _stopped_before_dispatch(outcome)
    assert outcome.contract is None, "no contract was bound"
    assert not (project / "c.py").exists(), "no model call was made for it"
    assert session.run_outcome.facts[-1].legacy == "PolicyError"


def test_a_task_whose_contract_builds_is_unchanged(tmp_path):
    """Negative control: the ordinary batch still merges both children with contracts."""
    script = Orchestrated([BATCH, "DONE"], lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    children = [o for o in session.task_outcomes if o.closed_as == "closed"]
    assert len(children) == 2 and all(o.contract for o in children)
    assert (project / "a.py").exists() and (project / "b.py").exists()
