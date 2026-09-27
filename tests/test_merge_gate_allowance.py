"""The merge gate has its own gate-fix allowance (phase 3, map G5).

It used to run with the counter the last serial parent task left, so after
a task that spent its fix the merge gate had none. Whole Session on the
parallel harness: a serial task that spends its one fix, then a batch whose
merged tree fails the gate."""

from pathlib import Path

from quadratus.integration import GateResult
from tests.gate_facts import ASSERTION_FAILURE
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session

SERIAL = _block("c.py")


class MergeGate:
    """Fails while the serial task's c.py is unfixed, and while the merged
    tree's a.py is unfixed; children see neither and pass."""

    def __init__(self, project, *, attributable=True):
        self.project, self.attributable, self.runs = Path(project), attributable, []
        self.cwd = self.project

    def run(self):
        c, a, b = (self.project / n for n in ("c.py", "a.py", "b.py"))
        phase = "serial" if c.exists() and "fixed" not in c.read_text() else (
            "merge" if a.exists() and b.exists() and "fixed" not in a.read_text() else None)
        self.runs.append(phase or "pass")
        if phase is None:
            return GateResult(True, "check", 0, "1 passed")
        return GateResult(False, "check", 1, f"{phase}: 1 failed",
                          report=ASSERTION_FAILURE if self.attributable else None)


def _run(tmp_path, *, merge_fix_works=True, attributable=True):
    script = Orchestrated([SERIAL, BATCH, "DONE"], lead_delay=0)
    fixes = []
    project = tmp_path / "project"
    project.mkdir()
    gate = MergeGate(project, attributable=attributable)
    session, _ = parallel_session(tmp_path, script, integration_gate=gate, max_gate_fixes=1)
    parent = script.invoke_for(project)

    def invoke(model, prompt, system=None, allow_writes=False):
        if "integration check failed" in prompt:
            if (project / "a.py").exists():
                fixes.append("merge")
                (project / "a.py").write_text("# a.py fixed\n" if merge_fix_works else "# a.py again\n")
                return 'Fixed the merge.\nCHANGED: ["a.py"]'
            fixes.append("serial")
            (project / "c.py").write_text("# c.py fixed\n")
            return 'Fixed c.\nCHANGED: ["c.py"]'
        if "You are leading" in prompt and " in c.py" in prompt:
            (project / "c.py").write_text("# c.py\n")
            return 'Wrote it\nCHANGED: ["c.py"]'
        return parent(model, prompt, system, allow_writes)
    session.invoke = invoke
    session.run(max_tasks=6)
    return session, gate, fixes


def test_the_merge_gate_gets_its_own_repair_after_a_serial_task_spent_its_fix(tmp_path):
    session, gate, fixes = _run(tmp_path)
    assert fixes == ["serial", "merge"], "the merge gate had its own one repair"
    assert gate.runs[-1] == "pass" and session.completed, session.stop_reason
    (batch,) = session.parallel_batches
    assert batch["merge_gate"]["gate_fixes"] == 1 and batch["merge_gate"]["passed"] is True
    serial = next(o for o in session.task_outcomes if o.task_id == "t1")
    assert serial.attempts["gate_fix"] == 1, "the serial task's own history is unchanged"


def test_the_merge_allowance_is_the_configured_one_and_exhausts(tmp_path):
    session, gate, fixes = _run(tmp_path, merge_fix_works=False)
    assert fixes == ["serial", "merge"], "exactly max_gate_fixes (1) merge repair, no more"
    assert not session.completed and session.parallel_batches[0]["merge_gate"]["passed"] is False
    assert "the merge gate still fails" in session.stop_reason


def test_an_unattributable_merge_failure_gets_no_repair(tmp_path):
    import pytest

    from quadratus.integration import CheckUnattributable
    with pytest.raises(CheckUnattributable, match="structured report undeclared"):
        _run(tmp_path, attributable=False)


def test_an_unattributable_failure_at_the_serial_gate_stops_before_any_batch(tmp_path):
    """Renamed coverage (Codex 5859838257): with no declared report at any
    gate the run stops at the serial task's gate; the merge is never reached."""
    import pytest

    from quadratus.integration import CheckUnattributable
    with pytest.raises(CheckUnattributable, match="structured report undeclared"):
        _run(tmp_path, attributable=False)


class SerialAttributableMergeNot(MergeGate):
    def run(self):
        result = super().run()
        if self.runs[-1] == "merge":
            return GateResult(False, "check", 1, "merge: runner crashed", report=None)
        return result


def test_an_unattributable_merged_tree_failure_gets_no_merge_repair(tmp_path):
    import pytest

    from quadratus.integration import CheckUnattributable
    script = Orchestrated([SERIAL, BATCH, "DONE"], lead_delay=0)
    project = tmp_path / "project"
    project.mkdir()
    gate = SerialAttributableMergeNot(project)
    session, _ = parallel_session(tmp_path, script, integration_gate=gate, max_gate_fixes=1)
    parent, fixes = script.invoke_for(project), []

    def invoke(model, prompt, system=None, allow_writes=False):
        if "integration check failed" in prompt:
            fixes.append("merge" if (project / "a.py").exists() else "serial")
            (project / "c.py").write_text("# c.py fixed\n")
            return 'Fixed c.\nCHANGED: ["c.py"]'
        if "You are leading" in prompt and " in c.py" in prompt:
            (project / "c.py").write_text("# c.py\n")
            return 'Wrote it\nCHANGED: ["c.py"]'
        return parent(model, prompt, system, allow_writes)
    session.invoke = invoke
    with pytest.raises(CheckUnattributable, match="merge|structured report undeclared"):
        session.run(max_tasks=6)
    assert fixes == ["serial"], "the serial failure was repaired; the merged tree got no repair"
    assert gate.runs[-1] == "merge"
    assert session.parallel_batches[0]["merge_gate"] == dict(task="t3-merge", gate_fixes=0, passed=False)


# -- G6: a sent-back batch spends one slot and counts as a correction --------------------

BAD_BATCH = "PARALLEL\n" + _block("a.py") + "\nRESOLVES: F1\n---\n" + _block("b.py")


def test_a_sent_back_batch_spends_one_slot_like_a_serial_send_back(tmp_path):
    """Old: a sent-back batch of two spent two slots, so with two slots the
    task named next never ran."""
    script = Orchestrated([BAD_BATCH, SERIAL, "DONE"], lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    parent = script.invoke_for(project)

    def invoke(model, prompt, system=None, allow_writes=False):
        if "You are leading" in prompt and " in c.py" in prompt:
            (project / "c.py").write_text("# c.py\n")
            return 'Wrote it\nCHANGED: ["c.py"]'
        return parent(model, prompt, system, allow_writes)
    session.invoke = invoke
    session.run(max_tasks=2)
    assert len(session.history) == 1, "the task named next ran in the second slot"
    assert (project / "c.py").read_text() == "# c.py\n" and session._covers_corrections == 1


def test_repeated_sent_back_batches_exhaust_the_correction_allowance(tmp_path):
    import pytest

    from quadratus.session import RunStalled
    script = Orchestrated([BAD_BATCH] * 6, lead_delay=0)
    session, _ = parallel_session(tmp_path, script, max_requirement_reopens=2)
    with pytest.raises(RunStalled, match="parallel batches that were sent back"):
        session.run(max_tasks=12)
    assert session.history == [] and session._covers_corrections == 3
