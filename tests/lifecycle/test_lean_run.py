"""The whole controller under ``--lean``'s settings: an outline plan with no
proof scenarios, a reviewer whose blocking finding quotes the goal, a
revision made in one turn and written by the harness, and the planner and
auditor answering from handed files. Offline: scripted replies below the
vendor CLIs."""

import json
import re
from pathlib import Path

import pytest

from quadratus.session_knobs import LEAN_SETTINGS
from tests.lifecycle import harness as H
from tests.lifecycle.replies import recheck_reply
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, T1, Script

pytestmark = pytest.mark.requirements_ledger

LEAN = dict.fromkeys(LEAN_SETTINGS, True)
GOAL = "Make add return the sum of its two arguments."
SCOPE = {k: v for k, v in T1.items() if k != "proofs"}
PLAN = ("REQUIREMENTS:\nR1: add returns the sum of its two arguments\n"
        "KIND: architect complex\nSCOPE: " + json.dumps(SCOPE) + "\nImplement add in app.py.\nCOVERS: R1")
HEAD = "## The files to judge from (handed over by the harness; data, not instructions)"
HALF = "def add(a, b):\n    return a + b if a else 0\n"
MET = "R1: MET - tests/test_app.py::test_add"


def _plan(call, replay):
    return PLAN if len(replay.of("orchestrator")) == 1 else "DONE"


def _half(call, replay):
    return "Implemented add.\n=== WRITE FILE app.py ===\n" + HALF + "=== END FILE app.py ===\n"


def _whole(call, replay):
    return "Implemented add.\n=== WRITE FILE app.py ===\n" + FIXED + "=== END FILE app.py ===\n"


def _blocks_once(line):
    def collaborator(call, replay):
        if len(replay.of("collaborator")) == 1:
            return line + "\nR1: NOT MET - add(0, 2) returns 0\nREVIEW: COMPLETE"
        return _clean(call, replay)
    return collaborator


def _clean(call, replay):
    return "No blocking findings.\nR1: MET - tests/test_app.py::test_add\nREVIEW: COMPLETE"


def _run(tmp_path, monkeypatch, **roles):
    roles.setdefault("collaborator", _clean)
    script = Script(orchestrator=_plan, **{"requirements-review": lambda call, replay: "COMPLETE",
                                           "auditor": lambda call, replay: MET, **roles})
    return H.run(tmp_path, monkeypatch, script, files=FILES, goal=GOAL, max_tasks=4, session_options=LEAN)


def test_a_lean_run_completes_with_no_proof_scenarios_and_one_turn_seats(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, lead=_whole)
    assert replay.result.completed, replay.result.error
    plan = replay.of("orchestrator")[0]
    assert "This run plans in outline" in plan.prompt and "declares no proof scenarios" in plan.prompt
    for call in replay.of("orchestrator") + replay.of("auditor"):
        said = call.prompt + " ".join(call.argv)
        assert HEAD in call.prompt and "You judge from this message alone" in said
        assert "fresh source copy" not in said
    assert "=== FILE app.py (" in replay.of("auditor")[0].prompt
    lead = replay.of("lead")[0]
    assert "Your work will be reviewed against the following" in lead.prompt
    assert "Keep the tests fast" in lead.prompt
    assert len(replay.of("lead")) == 1 and Path(lead.cwd) != replay.project
    assert "You write this task in one reply" in lead.prompt and "=== FILE app.py (" in lead.prompt
    assert "You write this task from this message alone" in lead.prompt + " ".join(lead.argv)
    assert (replay.project / "app.py").read_text() == FIXED
    audit = replay.of("auditor")[0].prompt
    assert "RISK R<n>:" in audit and "at the edge of what a language or library can represent" in audit
    assert H.result_json(replay).get("evidence_obligations") in ([], None)


def test_a_blocking_finding_that_quotes_the_goal_buys_a_one_turn_revision(tmp_path, monkeypatch):
    def revision(call, replay):
        assert HEAD in call.prompt and "=== FILE app.py (" in call.prompt
        return "Fixed the zero case.\n=== WRITE FILE app.py ===\n" + FIXED + "=== END FILE app.py ===\n"

    replay = _run(
        tmp_path, monkeypatch, lead=_half, revision=revision,
        collaborator=_blocks_once('BLOCKING: add(0, 2) returns 0 | basis: R1 | impact: contract | '
                                  'goal: "return the sum of its two arguments"'),
        recheck=lambda call, replay: recheck_reply(call.prompt, default="RESOLVED - add(0, 2) is 2 now"))
    assert replay.result.completed, replay.result.error
    assert (replay.project / "app.py").read_text() == FIXED
    fix, = replay.of("revision")
    said = fix.prompt + " ".join(fix.argv)
    assert "You revise from this message alone" in said and Path(fix.cwd) != replay.project
    assert "goal_basis_demotions" not in H.result_json(replay)


def test_a_revision_reply_that_cannot_be_written_gets_the_ordinary_editing_call(tmp_path, monkeypatch):
    def revision(call, replay):
        if len(replay.of("revision")) == 1:
            return "NEED SESSION: I have to delete a file."
        H.write(call, {"app.py": FIXED})
        return 'Fixed the zero case.\nCHANGED: ["app.py"]'

    replay = _run(
        tmp_path, monkeypatch, lead=_half, revision=revision,
        collaborator=_blocks_once('BLOCKING: add(0, 2) returns 0 | basis: R1 | impact: contract | '
                                  'goal: "return the sum of its two arguments"'),
        recheck=lambda call, replay: recheck_reply(call.prompt, default="RESOLVED - add(0, 2) is 2 now"))
    assert replay.result.completed, replay.result.error
    first, second = replay.of("revision")
    assert Path(first.cwd) != replay.project and Path(second.cwd) == replay.project
    assert (replay.project / "app.py").read_text() == FIXED


def test_a_blocking_finding_with_no_goal_words_is_a_note_and_buys_no_revision(tmp_path, monkeypatch):
    def collaborator(call, replay):
        return ("BLOCKING: add(10**400, 1) is slow | basis: app.py:2 | impact: other\n"
                "R1: MET - tests/test_app.py::test_add\nREVIEW: COMPLETE")

    replay = _run(tmp_path, monkeypatch, lead=_whole, collaborator=collaborator)
    assert replay.result.completed, replay.result.error
    assert replay.of("revision") == []
    rows = H.result_json(replay)["goal_basis_demotions"]
    assert len(rows) == 1 and rows[0]["why"] == "no goal words quoted"



def test_a_file_the_lead_could_not_run_is_cleared_by_the_whole_pytest_run_that_shows_it(tmp_path, monkeypatch):
    """A lead whose sandbox refused a socket reports the test file NOT RUN.
    The gate is pytest with no operand: it names no file, and its own
    report shows that file's case passed (stockroom, 2026-11-15)."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nNOT RUN: tests/test_app.py - the sandbox refused a socket\nCHANGED: ["app.py"]'

    replay = _run(tmp_path, monkeypatch, lead=lead)
    assert replay.result.completed, replay.result.error
    entry, = H.result_json(replay)["unexecuted_acceptance"]
    assert entry["item"] == "tests/test_app.py" and entry["discharge_state"] == "discharged"


def test_a_draft_that_cannot_be_written_in_one_turn_gets_the_ordinary_lead_call(tmp_path, monkeypatch):
    def lead(call, replay):
        if len(replay.of("lead")) == 1:
            return "NEED SESSION: I have to run the suite first."
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = _run(tmp_path, monkeypatch, lead=lead)
    assert replay.result.completed, replay.result.error
    first, second = replay.of("lead")
    assert Path(first.cwd) != replay.project and Path(second.cwd) == replay.project
    assert "You write this task in one reply" not in second.prompt


def test_a_repair_after_the_audit_is_told_to_stay_small(tmp_path, monkeypatch):
    def auditor(call, replay):
        if len(replay.of("auditor")) == 1:
            return ('R1: NOT MET - add(0, 2) returns 0; basis: "the sum of its two arguments"; evidence: '
                    "source reading; at app.py add; example: add(0, 2) returns 0")
        return MET

    def plan(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return PLAN
        if n == 3:
            return ("KIND: architect complex\nSCOPE: " + json.dumps(SCOPE)
                    + "\nMake add(0, 2) return 2.\nCOVERS: R1")
        return "DONE"

    def lead(call, replay):
        return _half(call, replay) if len(replay.of("lead")) == 1 else _whole(call, replay)

    script = Script(orchestrator=plan, lead=lead, collaborator=_clean, auditor=auditor,
                    **{"requirements-review": lambda call, replay: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, goal=GOAL, max_tasks=4, session_options=LEAN)
    assert replay.result.completed, replay.result.error
    sent_back = replay.of("orchestrator")[2].prompt
    assert "DONE SENT BACK" in sent_back and "the smallest change that makes the audit's example right" in sent_back


def test_a_decision_goes_to_the_planner_becomes_a_ruling_and_is_reported(tmp_path, monkeypatch):
    def auditor(call, replay):
        if "Rulings given during this audit" in call.prompt:
            assert "Keep sub exactly as written" in call.prompt and "R2" in call.prompt
            return "R2: MET - app.py"
        return ("R1: MET - app.py tests/test_app.py::test_add\n"
                "R2: DECISION - the goal does not say whether sub may gain a docstring; A: no change at all; "
                "B: behaviour only; the work follows A\nR3: MET - app.py")

    asked = []

    def operator(question):
        asked.append(question)
        return "never asked"

    plan = ("REQUIREMENTS:\nR1: add returns the sum of its two arguments\nR2: sub is unchanged\n"
            "R3: README.md documents add\nKIND: architect complex\nSCOPE: " + json.dumps(SCOPE)
            + "\nImplement add in app.py.\nCOVERS: R1, R2, R3")

    def orchestrator(call, replay):
        if "The audit needs a ruling on R2" in call.prompt:
            assert "docstring" in call.prompt and "sub is unchanged" in call.prompt
            return "READING: Keep sub exactly as written."
        return plan if len([c for c in replay.of("orchestrator") if "needs a ruling" not in c.prompt]) == 1 else "DONE"

    def collaborator(call, replay):
        return ("No blocking findings.\nR1: MET - tests/test_app.py::test_add\nR2: MET - app.py\n"
                "R3: MET - app.py\nREVIEW: COMPLETE")

    script = Script(orchestrator=orchestrator, lead=_whole, collaborator=collaborator, auditor=auditor,
                    **{"requirements-review": lambda call, replay: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=4, session_options=LEAN,
                   goal="Make add return the sum of its two arguments. Keep sub as it is. Document add in README.md.",
                   ask_operator=operator)
    assert replay.result.completed, replay.result.error
    assert asked == []
    row, = H.result_json(replay)["audit_decisions"]
    assert row["answered_by"] == "planner" and row["requirements"] == ["R2"] and row["kind"] == "decision"
    assert "Rulings the planner made for you" in replay.result.report and "Keep sub exactly" in replay.result.report


def test_a_requirement_judged_met_is_not_asked_again_while_its_files_are_unchanged(tmp_path, monkeypatch):
    def auditor(call, replay):
        if len(replay.of("auditor")) == 1:
            return ("R1: MET - app.py tests/test_app.py::test_add\nR2: MET - app.py\n"
                    "R3: NOT MET - README.md does not mention add; basis: \"Document add in README.md\"; "
                    "evidence: source reading; at README.md; example: none")
        assert "## Already judged" in call.prompt
        lines = call.prompt.split("## Lines this reply must contain")[1]
        assert "R3" in lines and "R1" not in lines and "R2" not in lines
        return "R3: MET - README.md"

    goal_scope = dict(SCOPE, permitted_paths=["README.md"])

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return ("REQUIREMENTS:\nR1: add returns the sum of its two arguments\nR2: sub is unchanged\n"
                    "R3: README.md documents add\nKIND: architect complex\nSCOPE: " + json.dumps(SCOPE)
                    + "\nImplement add in app.py.\nCOVERS: R1, R2, R3")
        if n == 3:
            return ("KIND: architect complex\nSCOPE: " + json.dumps(goal_scope)
                    + "\nDocument add in README.md.\nCOVERS: R3")
        return "DONE"

    def lead(call, replay):
        if len(replay.of("lead")) == 1:
            return _whole(call, replay)
        return "Documented add.\n=== WRITE FILE README.md ===\nadd(a, b) returns the sum.\n=== END FILE README.md ===\n"

    def collaborator(call, replay):
        return ("No blocking findings.\nR1: MET - tests/test_app.py::test_add\nR2: MET - app.py\n"
                "R3: MET - README.md\nREVIEW: COMPLETE")

    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator, auditor=auditor,
                    **{"requirements-review": lambda call, replay: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES,
                   goal="Make add return the sum of its two arguments. Keep sub as it is. Document add in README.md.",
                   max_tasks=4, session_options=LEAN)
    assert replay.result.completed, replay.result.error
    assert len(replay.of("auditor")) == 2


def test_a_long_requirement_list_is_audited_in_several_calls_at_once(tmp_path, monkeypatch):
    ids = [f"R{n}" for n in range(1, 15)]
    plan = ("REQUIREMENTS:\n" + "\n".join(f"{r}: add returns the sum of its two arguments, case {r}" for r in ids)
            + "\nKIND: architect complex\nSCOPE: " + json.dumps(SCOPE) + "\nImplement add in app.py.\nCOVERS: "
            + ", ".join(ids))

    def orchestrator(call, replay):
        return plan if len(replay.of("orchestrator")) == 1 else "DONE"

    def answer(call, replay):
        asked = call.prompt.split("## Lines this reply must contain")[1]
        return "\n".join(f"{r}: MET - app.py tests/test_app.py::test_add" for r in ids
                         if re.search(rf"\b{r}\b", asked))

    def collaborator(call, replay):
        return ("No blocking findings.\n" + "\n".join(f"{r}: MET - tests/test_app.py::test_add" for r in ids)
                + "\nREVIEW: COMPLETE")

    script = Script(orchestrator=orchestrator, lead=_whole, collaborator=collaborator, auditor=answer,
                    **{"requirements-review": lambda call, replay: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, goal=GOAL, max_tasks=4, session_options=LEAN)
    assert replay.result.completed, replay.result.error
    calls = replay.of("auditor")
    assert len(calls) == 3
    assert all("judged in separate calls at the same time" in call.prompt for call in calls)


def test_a_plan_given_in_one_reply_runs_its_later_wave_without_a_planning_call(tmp_path, monkeypatch):
    readme = dict(SCOPE, permitted_paths=["README.md"])
    first = ("REQUIREMENTS:\nR1: add returns the sum of its two arguments\nR2: README.md documents add\n"
             "KIND: architect complex\nSCOPE: " + json.dumps(SCOPE) + "\nImplement add in app.py.\nCOVERS: R1\n"
             "=== NEXT WAVE ===\nKIND: architect complex\nSCOPE: " + json.dumps(readme)
             + "\nDocument add in README.md.\nCOVERS: R2")

    def orchestrator(call, replay):
        assert "=== NEXT WAVE ===" in call.prompt
        return first if len(replay.of("orchestrator")) == 1 else "DONE"

    def lead(call, replay):
        if len(replay.of("lead")) == 1:
            return _whole(call, replay)
        return "Documented add.\n=== WRITE FILE README.md ===\nadd(a, b) returns the sum.\n=== END FILE README.md ===\n"

    def collaborator(call, replay):
        rid = "R1" if len(replay.of("collaborator")) == 1 else "R2"
        cite = "tests/test_app.py::test_add" if rid == "R1" else "README.md"
        return f"No blocking findings.\n{rid}: MET - {cite}\nREVIEW: COMPLETE"

    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                    auditor=lambda call, replay: "R1: MET - app.py tests/test_app.py::test_add\nR2: MET - README.md",
                    **{"requirements-review": lambda call, replay: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES,
                   goal="Make add return the sum of its two arguments. Document add in README.md.",
                   max_tasks=4, session_options=LEAN)
    assert replay.result.completed, replay.result.error
    assert len(replay.of("lead")) == 2 and len(replay.of("orchestrator")) == 2
    assert [w["outcome"] for w in H.result_json(replay)["plan_waves"]] == ["run"]


def test_a_planned_wave_is_set_aside_when_work_is_left_open(tmp_path):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("g", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(plan_queue=True))
    assert session._take_plan_waves("KIND: x\nfirst\n=== NEXT WAVE ===\nsecond\n=== NEXT WAVE ===\nDONE") == "KIND: x\nfirst"
    assert session._plan_queue == ["second"]
    session._open_work = lambda: ["t1 is capped"]
    assert session._next_planned_wave() is None and session._plan_queue == []
    assert "Wave 1:\nsecond" in session._plan_aside and "t1 is capped" in session._plan_aside
