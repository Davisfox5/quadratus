"""The merge gate is run-level work, attributed to itself (Codex, 5866389948;
O-NEXT-15 F2 follow-up).

After a parallel batch the merge gate ran with the last serial task's
outcome, contract and bound gate still in place, and a failed merge gate
always added a run-level *product* fact, even for a failure the check could
not attribute to an assertion. The merge context now clears those three and
restores them afterwards, a failed merge attempt is classed by the same
attribution as a task's (product only for an attributable assertion,
otherwise operator), and each merge attempt is kept in the batch's merge
record under the merge's synthetic id. Whole Session on the parallel
harness: a serial task, then a two-task batch, then the merge gate.
"""

import pytest

from quadratus.integration import CheckUnattributable, IntegrationGate
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _run(tmp_path, gate_argv, raises=None):
    project = tmp_path / "project"
    project.mkdir()
    session, _ = parallel_session(tmp_path, Orchestrated([_block("a.py"), BATCH, "DONE"], lead_delay=0),
                                  integration_gate=IntegrationGate(gate_argv, cwd=project),
                                  requirements_ledger=False)
    if raises is None:
        session.run(max_tasks=4)
    else:
        with pytest.raises(raises, match="without an attributable assertion failure"):
            session.run(max_tasks=4)
    return session


#: Passes for the serial task (only a.py exists), fails once the batch has
#: written b.py, with no structured report: not an attributable assertion.
FAILS_AT_MERGE = ["sh", "-c", "test ! -f b.py"]


def test_a_non_attributable_merge_failure_is_the_operators_not_the_products(tmp_path):
    session = _run(tmp_path, FAILS_AT_MERGE, raises=CheckUnattributable)
    t1 = next(o for o in session.task_outcomes if o.task_id == "t1")
    assert len(t1.checks) == 1 and t1.checks[0]["passed"], "the closed serial task keeps its one check"
    assert t1.mismatches == [], "and nothing is attributed to it afterwards"
    kinds = [f.kind for f in session.run_outcome.facts]
    assert "product" not in kinds, kinds
    assert "operator" in kinds
    assert not session.completed
    merge = session.parallel_batches[-1]["merge_gate"]
    assert merge["gate_fixes"] == 0 and merge["passed"] is False, "no repair call for a setup failure"
    (attempt,) = merge["attempts"]
    assert attempt["task"] == merge["task"] and merge["task"].endswith("-merge")
    assert attempt["attribution"]["product"] is False


def test_a_passing_merge_gate_keeps_its_attempt_and_adds_no_fact(tmp_path):
    session = _run(tmp_path, ["true"])
    merge = session.parallel_batches[-1]["merge_gate"]
    assert merge["passed"] is True and [a["task"] for a in merge["attempts"]] == [merge["task"]]
    t1 = next(o for o in session.task_outcomes if o.task_id == "t1")
    assert len(t1.checks) == 1 and t1.mismatches == []
    assert not [f for f in session.run_outcome.facts if "merge gate" in f.detail]
    assert session.completed
