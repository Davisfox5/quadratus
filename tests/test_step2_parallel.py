"""Step 2 (2026-10-13, #35): recoverable conflicts between parallel tasks.

A child whose edits left its scope is a failed, unmerged task a scoped
CONTINUES may finish; its out-of-scope bytes never reach the project. Merged
work that fails the project check after the merge fix round is check debt a
RESOLVES task repairs, not the end of the run. Integrity stops stay stops.
"""

import json
import re
from pathlib import Path

from quadratus.integration import GateResult
from tests.gate_facts import ASSERTION_FAILURE
from tests.lifecycle import harness as H
from tests.test_parallel_tasks import BATCH, Orchestrated, _block
from tests.test_parallel_tasks import _session as parallel_session


def _continuation(paths, text="work in b.py", extra=""):
    covers = next((m.group(1) for m in [re.search(r"COVERS:\s*(.+)", extra)] if m), None)
    scope = {"permitted_paths": paths, "intended_result": "b.py written", "acceptance": ["b.py exists"],
             "max_lines": 20}
    if covers:
        scope["proofs"] = H.proofs_for(covers, paths[0])
    scope = json.dumps(scope)
    return f"KIND: backend simple\nSCOPE: {scope}\n{text}\nCONTINUES: t2{extra}"


def test_an_out_of_scope_child_is_continued_with_a_wider_scope_and_the_run_completes(tmp_path):
    script = Orchestrated([BATCH, _continuation(["b.py", "shared.py"]), "DONE"], rogue="b.py", lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert session.failed == ["t2"], "the child failed; it did not stop the run"
    record = session.failed_records["t2"]
    assert record["cause"] == "scope" and record["unmerged_artifact"]
    # The continuation wrote both files under its own scope; t2's bytes
    # were never applied (the project held the base until t3).
    assert (project / "b.py").exists() and (project / "shared.py").exists()
    t2 = next(o for o in session.task_outcomes if o.task_id == "t2")
    assert t2.closed_as == "failed" and not t2.active, "t3's continuation recovered t2's fact"
    assert session.completed, session.stop_reason


def test_the_same_out_of_scope_task_resent_unchanged_is_refused(tmp_path):
    same = _block("b.py") + "\nCONTINUES: t2"
    script = Orchestrated([BATCH, same, "DONE"], rogue="b.py", lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    prompts, invoke = [], session.invoke

    def recording(model, prompt, system=None, allow_writes=False):
        prompts.append(prompt)
        return invoke(model, prompt, system, allow_writes)
    session.invoke = recording
    session.run(max_tasks=4)
    assert any("with the same task text and scope that did not finish" in p for p in prompts)
    assert not any("You are leading" in p and "CONTINUES" in p for p in prompts), "no lead call was spent"
    assert not (project / "b.py").exists() and not (project / "shared.py").exists()
    assert not session.completed


def test_an_integrity_stop_in_a_child_still_blocks_the_run(tmp_path):
    script = Orchestrated([BATCH, "DONE"], crash="b.py", lead_delay=0)
    session, project = parallel_session(tmp_path, script)
    session.run(max_tasks=4)
    assert script.plan == ["DONE"], "the batch stopped the run"
    assert session.stop_reason.startswith("FindingsOpen") and not session.completed
    t2 = next(o for o in session.task_outcomes if o.task_id == "t2")
    assert any(f.kind == "integrity" and f.active for f in t2.facts)


# -- an actual integration conflict -------------------------------------------------

class SumGate:
    """The project check: a.py and b.py together must agree. Each child's
    copy passes alone; the merged tree fails until a.py is repaired."""

    def __init__(self, project):
        self.project = Path(project)
        self.cwd = self.project
        self.runs = []

    def run(self):
        a, b = self.project / "a.py", self.project / "b.py"
        broken = a.exists() and b.exists() and "repaired" not in a.read_text()
        self.runs.append("fail" if broken else "pass")
        if broken:
            return GateResult(False, "check", 1, "a and b disagree: 1 failed", report=ASSERTION_FAILURE)
        return GateResult(True, "check", 0, "1 passed")


REQS = "REQUIREMENTS:\nR1: a.py works\nR2: b.py works\n"


def _covered(path, rid):
    return _block(path, covers=rid)


def test_merged_work_that_fails_the_check_becomes_repairable_debt(tmp_path):
    batch = REQS + "PARALLEL\n" + _covered("a.py", "R1") + "\n---\n" + _covered("b.py", "R2")
    project = tmp_path / "project"
    project.mkdir()
    gate = SumGate(project)
    script = Orchestrated([batch, "DONE"], lead_delay=0)
    session, _ = parallel_session(tmp_path, script, integration_gate=gate, max_gate_fixes=1,
                                  requirements_ledger=True)
    session.run(max_tasks=4)
    assert "fail" in gate.runs, "the merged tree failed the check"
    debt = [f for f in session.findings if f["task"] == "t2-merge"]
    assert debt and debt[0]["kind"] == "check.failed" and debt[0]["requirements"] == ["R1", "R2"]
    assert script.plan == [], "the planner was asked again after the conflict, not stopped"
    assert not any(f.active and f.kind == "product" for f in session.run_outcome.facts
                   if "merge gate" in f.detail), "the finding holds the debt"
    assert not session.completed, "open check debt still blocks completion"


# -- recovery goes through the serial intake (medium job on 3e59970) ---------------

def test_a_continuation_inside_a_batch_is_sent_back_and_then_charged_as_a_recovery(tmp_path):
    """t2 continued t1 inside a batch: the recovery allowance, the
    hypothesis record, the unchanged-retry check and the contract's
    predecessor were all skipped. A batch carrying CONTINUES is sent back
    before any lead call; the continuation named alone is a recorded recovery."""
    from quadratus.session import SurveyConfig
    first = _block("a.py") + "\nwrites too much"
    batch = ("PARALLEL\n" + _continuation(["a.py"], text="work in a.py") .replace("CONTINUES: t2", "CONTINUES: t1")
             + "\n---\n" + _block("b.py"))
    alone = _continuation(["a.py"], text="finish a.py, smaller", extra="\nHYPOTHESIS: split the helper out")
    alone = alone.replace("CONTINUES: t2", "CONTINUES: t1")
    script = Orchestrated([first, batch, alone, "DONE"], lead_delay=0, rogue="a.py")
    session, project = parallel_session(tmp_path, script, survey=SurveyConfig(recovery_tasks=1))
    prompts, invoke = [], session.invoke

    def recording(model, prompt, system=None, allow_writes=False):
        prompts.append(prompt)
        return invoke(model, prompt, system, allow_writes)
    session.invoke = recording
    session.run(max_tasks=5)
    assert session.failed[:1] == ["t1"], "t1 left its scope and failed"
    assert any("CONTINUES is not supported in a parallel batch" in p for p in prompts)
    assert not session.parallel_batches, "the batch never ran"
    assert session.survey["recovery_used"] == 1, "the continuation named alone was charged"
    assert [h["text"] for h in session.survey["hypotheses"] if h["text"]] == ["split the helper out"]
    contracts = [o.contract or {} for o in session.task_outcomes]
    assert any(c.get("continues") == "t1" for c in contracts), "the continuation's contract names t1"
