"""Where the two merged lines meet in prompts, reviews and the map
(merge of 2026-11-11).

The development line writes a sizing note to the codebase map when a task's
change is first measured. The review batch lists the map around every editing
call and fails a task whose call changed it. Neither line had a test of the
two together: the note has to land between editing calls, never during one.
"""

import json
from pathlib import Path

import pytest

from quadratus import session as S
from quadratus.scope import TaskScope


@pytest.mark.requirements_ledger
def test_the_harness_sizing_note_is_written_between_editing_calls_and_is_no_watch_finding(tmp_path, monkeypatch):
    """A whole run whose SCOPE states the estimate split: the lead edits, the
    harness measures and writes its sizing note, a reviewer blocks, and the
    revision is a second editing call that starts with the note already in
    the map. The watch around both calls records nothing."""
    from quadratus.codebase_map import CodebaseMap
    from tests.lifecycle import harness as H
    from tests.lifecycle.replies import judging_scenarios
    from tests.lifecycle.test_lifecycle_matrix import ADD_PROOF, ADD_SHOWN, FILES, FIXED, T1, Script

    scope = dict(T1, estimate=dict(implementation=20, proof=15, setup=5))
    plan = ["REQUIREMENTS:\nR1: add(1, 2) returns 3\nKIND: architect complex\nSCOPE: " + json.dumps(scope)
            + "\nImplement add in app.py.\nCOVERS: R1"]
    order = []
    amend = CodebaseMap.amend

    def noted(self, **note):
        if note.get("topic") == "sizing":
            order.append("sizing note")
        return amend(self, **note)
    monkeypatch.setattr(CodebaseMap, "amend", noted)

    def lead(call, replay):
        order.append("lead")
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if len(replay.of("collaborator")) == 1:
            return "BLOCKING: nothing tests negative numbers | basis: tests/test_app.py | impact: correctness"
        return "No blocking findings.\nREVIEW: COMPLETE"

    def revision(call, replay):
        order.append("revision")
        old = Path(call.cwd, "tests/test_app.py").read_text()
        H.write(call, {"tests/test_app.py": old + "\n\ndef test_negative():\n    assert add(-1, -2) == -3\n"})
        return 'Added a negative-number test.\nCHANGED: ["tests/test_app.py"]'

    script = Script(orchestrator=lambda call, replay: plan.pop(0) if plan else "DONE", lead=lead,
                    collaborator=collaborator, revision=revision,
                    **{"requirements-review": lambda call, replay: "COMPLETE",
                       "auditor": judging_scenarios(lambda call, replay: "R1: MET - app.py", ADD_PROOF,
                                                    basis=ADD_SHOWN)})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3)
    assert replay.result.completed, replay.result.error
    assert order == ["lead", "sizing note", "revision"], "measured once, after the call that made the change"
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())
    assert record["project_watch"] == dict(findings=[], paths=[])
    notes = [json.loads(line) for line in
             (Path(replay.project) / ".quadratus" / "codebase-map.jsonl").read_text().splitlines()]
    (sizing,) = [n for n in notes if n["topic"] == "sizing"]
    assert sizing["author"] == "harness" and sizing["session"] == "t1"
    assert sizing["note"].startswith("t1 estimated implementation 20, proof 15, setup 5 of 40")


def test_an_operator_size_is_stated_only_to_a_task_that_declared_none():
    """New wording at the merge. The review batch's note stated the lower
    stop; the development line stops nothing for size (owner directive,
    2026-10-20), so the operator's figure is said only where it is the
    estimate the change is measured against, and never as a stop."""
    limit = TaskScope(max_lines=10)
    assert S._operator_limit_note(limit, TaskScope(["app.py"], max_lines=40)) == ""
    assert S._operator_limit_note(limit, TaskScope(["app.py"], max_lines=5)) == ""
    note = S._operator_limit_note(limit, TaskScope(["app.py"]))
    assert note == ("Operator limits (also binding):\nSize: the operator's limits put the estimate at about 10 "
                    "changed lines; it is measured and reported, and stops nothing.")
    assert S._operator_limit_note(TaskScope(), TaskScope(["app.py"])) == ""
