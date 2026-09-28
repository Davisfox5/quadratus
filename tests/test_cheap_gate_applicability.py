"""The cheap view that runs before review is the task's own gate's, fixed at
dispatch (map P3.4; O-NEXT-10 B at 548da3d, Sol ruling 5865330461).

``_run_task`` took the cheap subset from the live configured gate while the
full check already ran the gate bound with the contract. A cheap command that
appeared after dispatch could fail the task before review; one that vanished
was skipped. It now takes the bound gate's cheap view whenever the task has a
contract, records a live gate whose cheap view differs, and keeps the live
gate for paths with no task contract. Whole Session on the parallel harness;
the drift is a synthetic write during the lead call, a controller invariant.
"""

from quadratus.integration import GateCommand, GateSuite, IntegrationGate
from tests.test_parallel_tasks import Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _gates(project):
    return {"plain": lambda: IntegrationGate(["true"], cwd=project),
            "suite": lambda: GateSuite([GateCommand(id="fast", argv=("true",), cheap=True),
                                        GateCommand(id="full", argv=("true",))], cwd=project),
            "failing_cheap": lambda: GateSuite([GateCommand(id="lint", argv=("false",), cheap=True),
                                                GateCommand(id="full", argv=("true",))], cwd=project)}


def _run(tmp_path, *, gate, drift=None):
    project = tmp_path / "project"
    project.mkdir()
    gates = _gates(project)
    session, _ = parallel_session(tmp_path, Orchestrated([_block("a.py"), "DONE"], lead_delay=0),
                                  integration_gate=gates[gate](), requirements_ledger=False)
    invoke = session.invoke

    def drifting(model, prompt, system=None, allow_writes=False):
        if drift is not None and "You are leading" in prompt:
            session.config.integration_gate = gates[drift]()
        return invoke(model, prompt, system, allow_writes)
    session.invoke = drifting
    session.run(max_tasks=2)
    return session


def _ran(session):
    return [[r["id"] for r in check["receipts"]] for check in session.checks]


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches]


def test_a_cheap_gate_that_appears_after_dispatch_does_not_run(tmp_path):
    session = _run(tmp_path, gate="plain", drift="failing_cheap")
    (outcome,) = session.task_outcomes
    assert "lint" not in [i for ran in _ran(session) for i in ran], "the failing live cheap command never ran"
    assert [a["gate"] for a in outcome.checks] == ["full"]
    assert "cheap_checks: contract (), legacy ('lint',)" in _mismatches(session)
    assert not session.completed, "a recorded mismatch cannot count as complete"


def test_a_cheap_gate_that_vanishes_after_dispatch_still_runs(tmp_path):
    session = _run(tmp_path, gate="suite", drift="plain")
    (outcome,) = session.task_outcomes
    assert [a["gate"] for a in outcome.checks] == ["subset", "full"]
    assert _ran(session) == [["fast"], ["fast", "full"]], "the bound suite ran, cheap view first"
    assert "cheap_checks: contract ('fast',), legacy ()" in _mismatches(session)
    assert not session.completed


# -- controls -----------------------------------------------------------------------

def test_no_drift_runs_the_bound_cheap_view_with_no_mismatch(tmp_path):
    session = _run(tmp_path, gate="suite")
    (outcome,) = session.task_outcomes
    assert [a["gate"] for a in outcome.checks] == ["subset", "full"]
    assert _ran(session) == [["fast"], ["fast", "full"]]
    assert session.completed and not _mismatches(session)


def test_a_plain_gate_has_no_cheap_view(tmp_path):
    session = _run(tmp_path, gate="plain")
    (outcome,) = session.task_outcomes
    assert [a["gate"] for a in outcome.checks] == ["full"]
    assert session.completed and not _mismatches(session)


def test_with_no_task_contract_the_live_gate_decides(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    gates = _gates(project)
    session, _ = parallel_session(tmp_path, Orchestrated(["DONE"], lead_delay=0),
                                  integration_gate=gates["suite"](), requirements_ledger=False)
    assert [c.id for c in session._cheap_gate().commands] == ["fast"]
    session.config.integration_gate = gates["plain"]()
    assert session._cheap_gate() is None
