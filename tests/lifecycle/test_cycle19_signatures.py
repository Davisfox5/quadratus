"""Cycle19 lifecycle: a valid definition beside an invocation example reaches
the leads, serial and parallel, with no scope correction spent; a genuinely
contradictory declaration still gets exactly one correction and then stops,
and a contradiction fixed on the correction dispatches.

Run 20261109T155552Z (cycle18 terminal): the parallel CLI+README batch was
refused because ``main(argv)`` in test guidance read as a second signature
of ``def main(argv=None)``; block 1 alone spent the one correction on the
same false refusal and the run stalled with 40 calls and 5 slots left.
"""
import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

pytestmark = pytest.mark.requirements_ledger

REQS = ("REQUIREMENTS:\nR1: add(a, b) returns the sum of two numbers\n"
        "R2: README documents add and states what it returns\n")
VALID = ("Implement `def add(a, b=0):` in app.py. Tests invoke `add(2)` and `add(1, 2)`, and assert calling "
         "`add(1, 2, 3)` raises TypeError.")
CONFLICT = "Implement `def add(a, b=0):` in app.py. Define `def add(a, b):` instead."
CLEAN = "NO FINDINGS\nR1: MET - app.py\nR2: MET - README.md\nREVIEW: COMPLETE"


def _block(kind, path, text, covers, *, signature="def add(a, b=0):", example=True):
    acceptance = [f"{path} has `{signature}`"] + (["`add(1)` returns 1"] if example else [])
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"`{signature}` in {path}",
                        "acceptance": acceptance, "max_lines": 20,
                        "proofs": H.proofs_for(covers, "tests/test_app.py" if path == "app.py" else path,
                                               evidence="reading" if path == "README.md" else None)})
    return f"KIND: {kind}\nSCOPE: {scope}\n{text}\nCOVERS: {covers}"


APP = REQS.split("R2:")[0] + _block("backend simple", "app.py", VALID, "R1")
README = _block("docs rote", "README.md", "Document `def add(a, b=0):` in README.md; show a call `add(2, 3)`.", "R2")
BATCH = REQS + "PARALLEL\n" + _block("backend simple", "app.py", VALID, "R1") + "\n---\n" + README


def _script(*plans):
    replies = iter(plans)

    def lead(call, replay):
        if "README.md" in call.prompt and "Document" in call.prompt:
            H.write(call, {"README.md": FILES["README.md"] + "\nadd(a, b=0) returns a + b.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'
    return Script(orchestrator=lambda c, r: next(replies, "DONE"), lead=lead, collaborator=lambda c, r: CLEAN,
                  auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                  **{"requirements-review": lambda c, r: "COMPLETE"})


def _corrections(replay):
    return [c for c in replay.of("orchestrator") if "CORRECTION REQUIRED" in c.prompt]


def test_a_serial_definition_with_invocation_examples_reaches_its_lead_with_no_correction(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(APP, APP), files=FILES, max_tasks=2, record_complete=False)
    assert _corrections(replay) == [], "no scope correction was spent"
    assert [c.task for c in replay.of("lead")][:1] == ["t1"], [(c.role, c.task) for c in replay.calls]
    assert "more than one signature" not in (replay.result.error or "")


def test_a_parallel_batch_with_invocation_examples_runs_both_leads(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(BATCH, BATCH), files=FILES, max_tasks=3, record_complete=False)
    assert _corrections(replay) == []
    roles = [(c.role.split(":")[0], c.task) for c in replay.calls]
    second_plan = [i for i, (role, _) in enumerate(roles) if role == "orchestrator"][1:2]
    before = roles[:second_plan[0]] if second_plan else roles
    leads = {task for role, task in before if role == "lead"}
    assert len(leads) == 2, roles
    assert "parallel batch not run" not in json.dumps(H.result_json(replay))
    assert "more than one signature" not in (replay.result.error or "")


def test_a_contradiction_declared_twice_spends_exactly_one_correction_and_stops(tmp_path, monkeypatch):
    """Retained negative control: passes on 4f961c4 too."""
    plan = REQS.split("R2:")[0] + _block("backend simple", "app.py", CONFLICT, "R1", example=False)
    replay = H.run(tmp_path, monkeypatch, _script(plan, plan), files=FILES, max_tasks=2, record_complete=False)
    assert len(_corrections(replay)) == 1
    assert "more than one signature" in _corrections(replay)[0].prompt
    assert replay.of("lead") == [], "nothing dispatched"
    assert "more than one signature" in (replay.result.error or ""), replay.result.error


def test_a_contradiction_fixed_on_the_correction_dispatches(tmp_path, monkeypatch):
    """Retained control: passes on 4f961c4 too (no call examples here)."""
    bad = REQS.split("R2:")[0] + _block("backend simple", "app.py", CONFLICT, "R1", example=False)
    fixed = REQS.split("R2:")[0] + _block("backend simple", "app.py", "Implement `def add(a, b=0):` in app.py.",
                                          "R1", example=False)
    replay = H.run(tmp_path, monkeypatch, _script(bad, fixed), files=FILES, max_tasks=2, record_complete=False)
    assert len(_corrections(replay)) == 1
    assert [c.task for c in replay.of("lead")][:1] == ["t1"]
