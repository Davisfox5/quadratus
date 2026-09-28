"""The task's mandatory gate is its contract's, fixed at dispatch (map P3.4,
``checks``).

``_run_integration_gate`` used to read the live configured gate when the task
reached its checks. It now asks the task's own ``required.checks`` and runs
the gate that contract was built with; the live reading is still compared
and a disagreement recorded. Explicit subset gates (the cheap view) and the
parallel merge gate, which runs with no task contract, keep their routes.
Whole Session on the parallel harness; the drift is a synthetic write during
the lead call, a controller invariant rather than observed behaviour.
"""

from quadratus.integration import GateCommand, GateSuite, IntegrationGate
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _run(tmp_path, plan, *, gate="plain", drift=None):
    project = tmp_path / "project"
    project.mkdir()
    gates = {"plain": lambda: IntegrationGate(["true"], cwd=project),
             "suite": lambda: GateSuite([GateCommand(id="fast", argv=("true",), cheap=True),
                                         GateCommand(id="full", argv=("true",))], cwd=project),
             None: lambda: None}
    script = Orchestrated(plan, lead_delay=0)
    session, _ = parallel_session(tmp_path, script, integration_gate=gates[gate](),
                                  requirements_ledger=False)
    invoke = session.invoke

    def drifting(model, prompt, system=None, allow_writes=False):
        if drift is not None and "You are leading" in prompt:
            session.config.integration_gate = gates[drift]() if drift else None
        return invoke(model, prompt, system, allow_writes)
    session.invoke = drifting
    session.run(max_tasks=len(plan) + 1)
    return session


def _mismatches(session):
    return [m for o in session.task_outcomes for m in o.mismatches]


def test_a_required_gate_still_runs_when_the_setting_drifts_off(tmp_path):
    session = _run(tmp_path, [_block("a.py"), "DONE"], drift=False)
    assert len(session.checks) == 1 and session.checks[0]["passed"], "the gate fixed at dispatch ran"
    assert "checks: contract True, legacy False" in _mismatches(session)
    assert not session.completed, "a recorded mismatch cannot count as complete"


def test_an_unrequired_gate_is_not_added_when_the_setting_drifts_on(tmp_path):
    session = _run(tmp_path, [_block("a.py"), "DONE"], gate=None, drift="plain")
    assert session.checks == [], "no gate the contract did not require"
    assert "checks: contract False, legacy True" in _mismatches(session)
    assert not session.completed


# -- same-journey controls -------------------------------------------------------

def test_the_full_gate_runs_once_as_the_task_s_full_gate(tmp_path):
    session = _run(tmp_path, [_block("a.py"), "DONE"])
    (outcome,) = session.task_outcomes
    assert outcome.contract["required"]["checks"] is True and outcome.edges["checks"] is True
    assert [a["gate"] for a in outcome.checks] == ["full"]
    assert session.completed and not _mismatches(session)


def test_no_gate_configured_means_no_checks(tmp_path):
    session = _run(tmp_path, [_block("a.py"), "DONE"], gate=None)
    (outcome,) = session.task_outcomes
    assert outcome.contract["required"]["checks"] is False and session.checks == []
    assert not _mismatches(session)


def test_the_cheap_view_still_runs_as_a_subset_before_the_full_gate(tmp_path):
    session = _run(tmp_path, [_block("a.py"), "DONE"], gate="suite")
    (outcome,) = session.task_outcomes
    assert [a["gate"] for a in outcome.checks] == ["subset", "full"]
    ran = [[r["id"] for r in check["receipts"]] for check in session.checks]
    assert ran == [["fast"], ["fast", "full"]]


def test_the_merge_gate_still_runs_with_no_task_contract(tmp_path):
    session = _run(tmp_path, [BATCH, "DONE"])
    record = session.parallel_batches[-1]["merge_gate"]
    assert record["passed"] is True and record["gate_fixes"] == 0
    assert session.checks, "the configured gate ran for the merged tree"
    assert not _mismatches(session)
