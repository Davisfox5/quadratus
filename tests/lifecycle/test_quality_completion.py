"""Grouped quality/completion batch (Davis on #73, 2026-10-31): whole-controller
replays below the vendor CLIs for the three shared causes read out of the
P12 terminal (evidence-selector-01-ticket-2ac2f85). Scripted replies prove
routing and accounting, never model compliance: a rule a prompt carries is
not proof the seat reads it.

1. Execution debt versus acceptance evidence: a NOT RUN item the task's own
   gate then executed is settled; the auditor's INSUFFICIENT about it is
   recorded and applied to nothing; an unexecuted item still holds, and a
   SUFFICIENT about it credits nothing.
2. One contract rule on every prompt route a seat reads.
3. Slot accounting at dispatch, the carried ledger, the resume, the
   forfeit, the parallel wave, and the capped-revision handoff in a slot
   that is not the last one, with its negatives.
"""
import json
import re
from pathlib import Path

import pytest

from quadratus.config import Settings
from quadratus.session import _CONTRACT_BOUNDARY_RULE
from tests.lifecycle import harness as H
from tests.lifecycle.replies import judged, requested_obligations, scenario_text
from tests.lifecycle.test_finish_capacity import PLAN_1, PLAN_2, _limits
from tests.lifecycle.test_finish_capacity import _script as capacity_script
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_step2_recovery import (
    SIMPLE_SHOWN,
    _episode,
    _interrupt_after,
    _plan,
    _simple,
    simple_proofs,
)

REQS = "REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
T1 = _simple("app.py", "Implement add in app.py.", "R1")
T2 = _simple("README.md", "Document add in README.md.", "R2")
NO_COVERS = T1.rsplit("\nCOVERS:", 1)[0]
README = "# app\n\n`add(a, b)` returns the sum.\n"
DOCS = "SUFFICIENT README.md names add(a, b) and states that it returns the sum, which is all R2 asks"
TYPED = ("def add(a, b):\n"
         "    for name, value in (('a', a), ('b', b)):\n"
         "        if not isinstance(value, (int, float)):\n"
         "            raise TypeError(name)\n"
         "    return a + b\n")
FINDING = "BLOCKING: add('x', 1) returns a string instead of raising TypeError naming the argument."


def _roles(replay):
    return [c.role for c in replay.calls]


def _ledger(replay):
    return [(e["event"], tuple(e["tasks"]), e["slots_used"]) for e in H.result_json(replay)["slot_ledger"]]


def _debt_ids(prompt):
    """The ids the prompt lists under its execution-debt heading."""
    start = prompt.find("## Execution debt")
    if start < 0:
        return []
    end = prompt.find("## Evidence obligations", start)
    block = prompt[start:end if end > 0 else start + 4000]
    return re.findall(r"^- (O\d+) ", block, re.MULTILINE)


def _auditor(verdicts, *, reject_debt=None, approve_debt=None):
    """A scripted auditor: this file's own SUFFICIENT line for each declared
    scenario the prompt lists executed, the reading judgment for README
    rows, and, where the case says so, its own line about every
    execution-debt row the prompt lists (executed or not)."""
    def auditor(call, replay):
        rows = requested_obligations(call.prompt)
        scenarios = {scenario_text(p) for p in simple_proofs("R1") + simple_proofs("R2")}
        approve = {r["behavior"]: SIMPLE_SHOWN for r in rows if r["behavior"] in scenarios and r["execution"] == "ran"}
        reply = judged(call.prompt, verdicts, approve=approve)
        extra = {}
        for row in rows:
            if row["modality"] == "reading":
                extra[row["oid"]] = DOCS
        for oid in _debt_ids(call.prompt):
            if reject_debt:
                extra[oid] = f"INSUFFICIENT {reject_debt}"
            elif approve_debt:
                extra[oid] = f"SUFFICIENT {approve_debt}"
        return reply + "".join(f"\n{oid}: {text}" for oid, text in extra.items())
    return auditor


# -- 1. execution debt is settled by execution and judged by nobody -------------------------

@pytest.mark.requirements_ledger
def test_an_executed_not_run_item_is_settled_and_the_auditors_insufficient_about_it_lowers_nothing(
        tmp_path, monkeypatch):
    """The P12 shape (t6, O49/O50): the lead reported NOT RUN on the case the
    gate then executed on the delivered source, and the auditor wrote MET
    with INSUFFICIENT lines about the executed rows. Old engine: six METs
    lowered on those lines and the run ended RequirementsUnmet. Now the row
    is settled by the execution, the line is recorded and applied to
    nothing, and the requirement is met."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return ('Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\n'
                'NOT RUN: tests/test_app.py::test_add - the sandbox refused to start pytest\nCHANGED: ["app.py"]')

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    auditor=_auditor({"R1": "MET - app.py"}, reject_debt="the executed case does not show the refusal path"),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    result = H.result_json(replay)
    debt = [o for o in result["evidence_obligations"] if o["kind"] == "not_run"]
    assert len(debt) == 1 and debt[0]["purpose"] == "execution" and debt[0]["execution"] == "ran", debt
    assert debt[0]["sufficiency"] == "unjudged" and "not applied" in debt[0]["sufficiency_note"]
    audit = [c for c in replay.of("auditor")][-1]
    assert "## Execution debt" in audit.prompt and "Each executed acceptance obligation" in audit.prompt
    assert result["requirements"]["status"]["R1"].startswith("met"), result["requirements"]["status"]
    assert result["requirements"]["audits"][-1]["lowered"] == []
    assert replay.result.completed, replay.result.error
    assert result["unexecuted_acceptance"][0]["discharged_at"] is not None


@pytest.mark.requirements_ledger
def test_an_unexecuted_not_run_item_holds_its_requirement_and_a_sufficient_about_it_credits_nothing(
        tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return ('Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\n'
                'NOT RUN: tests/test_cli.py::test_refusal - no such file is in the approved commands\nCHANGED: ["app.py"]')

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    auditor=_auditor({"R1": "MET - app.py"}, approve_debt="clearly covered by the unit test"),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    result = H.result_json(replay)
    debt = [o for o in result["evidence_obligations"] if o["kind"] == "not_run"]
    assert len(debt) == 1 and debt[0]["execution"] != "ran" and debt[0]["sufficiency"] == "unjudged"
    assert not replay.result.completed
    assert result["requirements"]["status"]["R1"].startswith("NOT MET"), result["requirements"]["status"]
    assert result["unexecuted_acceptance"][0]["discharged_at"] is None


# -- 2. one contract rule on every prompt route ---------------------------------------------

@pytest.mark.requirements_ledger
def test_the_contract_rule_reaches_every_seat_once_and_a_reviewer_told_it_can_act_on_it(tmp_path, monkeypatch):
    """Route test, not a behaviour claim: the planner, the lead, the review,
    the revision, the recheck's reviewer and the audit each receive the
    rule once. The scripted reviewer raises the documentation-exception
    finding only when its prompt carries the sentence; the run then holds
    the finding open after the lead's one revision instead of completing.
    That the sentence reaches the seat is shown; that a model acts on it is
    not."""
    goal = "Implement add(a, b) in app.py. A non-number argument must raise TypeError naming the argument."
    exemption = "# app\n\n`add` accepts numbers; strings are a documented exception and are concatenated.\n"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "README.md": exemption} if call.task == "t1" else {"README.md": exemption})
        return 'Implemented add; documented the string exception.\nCHANGED: ["app.py", "README.md"]'

    def collaborator(call, replay):
        if "documentation describes the goal's contract and never narrows it" in call.prompt:
            return ("BLOCKING: README.md documents a string exception the goal does not grant; add('x', 1) "
                    "concatenates instead of raising TypeError.\nR1: NOT MET - the refusal is documented away\n"
                    "REVIEW: COMPLETE")
        return "NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE"

    t1 = _simple("app.py", "Implement add in app.py and note it in README.md.", "R1").replace(
        '"permitted_paths": ["app.py"]', '"permitted_paths": ["app.py", "README.md"]')
    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + t1), lead=lead,
                    collaborator=collaborator, recheck=lambda c, r: "UNRESOLVED: the exception is still documented",
                    auditor=_auditor({"R1": "NOT MET - a documented exception narrows the refusal"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, goal=goal, record_complete=False)
    roles = _roles(replay)
    # The recheck is not a route: it carries the reviewer's own finding and
    # the revised source, not the goal block.
    for role in ("orchestrator", "lead", "collaborator", "revision"):
        calls = [c for c in replay.calls if c.role == role]
        assert calls, (role, roles)
        for call in calls:
            assert call.prompt.count(_CONTRACT_BOUNDARY_RULE) == 1, (role, call.prompt.count(_CONTRACT_BOUNDARY_RULE))
    audits = [c for c in replay.calls if c.role == "auditor"]
    assert all(c.prompt.count(_CONTRACT_BOUNDARY_RULE) == 1 for c in audits) if audits else True
    assert not replay.result.completed
    revisions = [c for c in replay.calls if c.role == "revision"]
    assert revisions and "documents a string exception the goal does not grant" in revisions[0].prompt, \
        "the finding the rule-aware reviewer raised reached the writer"


# -- 3. slots are charged at dispatch; the handoff applies in any serial slot -----------------

@pytest.mark.requirements_ledger
def test_a_refused_declaration_and_a_done_sent_back_spend_no_slot_and_the_dispatched_tasks_do(tmp_path, monkeypatch):
    """P12 spent two of eight slots on a declaration the intake refused and a
    DONE that was sent back. With two slots: a task without COVERS (sent
    back), t1, DONE with R2 open (sent back), t2, the cap's own decision.
    Old engine: the refused declaration and t1 spent both slots and the run
    ended RequirementsUnmet with R2 never planned."""
    plan = [REQS + NO_COVERS, T1, "DONE", T2]
    script = capacity_script(plan, t2_review="NO FINDINGS\nR2: MET - README.md")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    planners = replay.of("orchestrator")
    assert "--- TASK SENT BACK ---" in planners[1].prompt and "Task slots: 2 of 2 remain" in planners[1].prompt
    assert "--- DONE SENT BACK ---" in planners[3].prompt and "Task slots: 1 of 2 remain" in planners[3].prompt
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    assert _ledger(replay) == [("sent_back", ("t1",), 0), ("charged", ("t1",), 1), ("done_sent_back", (), 1),
                               ("charged", ("t2",), 2)]
    assert replay.result.completed, replay.result.error
    result = H.result_json(replay)
    assert result["requirements"]["status"]["R1"].startswith("met") and result["requirements"]["status"]["R2"].startswith("met")


@pytest.mark.requirements_ledger
def test_the_uncharged_paths_stay_bounded_by_the_correction_allowance(tmp_path, monkeypatch):
    plan = [REQS + NO_COVERS, NO_COVERS, NO_COVERS, NO_COVERS, NO_COVERS]
    script = capacity_script(plan)
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert "RunStalled" in (replay.result.error or "") and "without valid COVERS" in replay.result.error
    assert not replay.of("lead")
    events = _ledger(replay)
    assert events and all(e[0] == "sent_back" and e[2] == 0 for e in events), events
    assert len(events) == 4, "three corrections allowed, the fourth stalls the run"


@pytest.mark.requirements_ledger
def test_the_slot_ledger_is_carried_across_a_resume(tmp_path, monkeypatch):
    _interrupt_after(monkeypatch, 1)
    plan = [REQS + NO_COVERS, T1, T2]
    first = H.run(tmp_path, monkeypatch, capacity_script(plan, t2_review="NO FINDINGS\nR2: MET - README.md"),
                  files=FILES, max_tasks=2, record_complete=False)
    assert "KeyboardInterrupt" in first.result.error
    assert _ledger(first) == [("sent_back", ("t1",), 0), ("charged", ("t1",), 1)]
    second = _episode(first, resume_from=Path(first.result.run_dir).name, max_tasks=2)
    result = json.loads((Path(second.run_dir) / "result.json").read_text())
    assert [(e["event"], tuple(e["tasks"]), e["slots_used"]) for e in result["slot_ledger"]] == [
        ("sent_back", ("t1",), 0), ("charged", ("t1",), 1), ("charged", ("t2",), 2)]
    assert result["tasks"] == 2


@pytest.mark.requirements_ledger
def test_a_task_not_started_spends_no_slot_and_the_forfeit_counts_its_own_slot_as_unused(tmp_path, monkeypatch):
    """Eight calls over three slots: t2's route cannot be paid at dispatch,
    nothing of it runs. Its slot was never spent, so the forfeit names two
    of three slots unused and the charged count stays at one."""
    replay = H.run(tmp_path, monkeypatch, capacity_script([PLAN_1, PLAN_2]), files=FILES, max_tasks=3,
                   limits=_limits(8), record_complete=False)
    result = H.result_json(replay)
    assert [n["task"] for n in result["not_started"]] == ["t2"]
    assert _ledger(replay) == [("charged", ("t1",), 1), ("not_started", ("t2",), 1)]
    assert "2 of 3 task slot(s) were forfeited" in (replay.result.error or ""), replay.result.error


def test_a_parallel_wave_charges_one_slot_per_task_and_a_sent_back_batch_none(tmp_path):
    from tests.test_merge_gate_allowance import BAD_BATCH
    from tests.test_parallel_tasks import BATCH, Orchestrated
    from tests.test_parallel_tasks import _session as parallel_session
    script = Orchestrated([BAD_BATCH, BATCH, "DONE"], lead_delay=0)
    session, _ = parallel_session(tmp_path, script)
    session.run(max_tasks=3)
    assert [s.task_id for s in session.history] == ["t1", "t2"]
    events = [(e["event"], tuple(e["tasks"]), e["slots_used"], e.get("how")) for e in session.slot_ledger]
    assert events == [("sent_back", ("t1", "t2"), 0, None), ("charged", ("t1", "t2"), 2, "batch")]
    assert session._slots_used == 2 and session._covers_corrections == 1


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["resolved", "unresolved", "failing", "out-of-scope", "source-changed", "recheck-capped"])
def test_a_capped_revision_with_slots_left_meets_its_reviewer_once_and_closes_only_on_resolved(
        tmp_path, monkeypatch, case):
    """P12 t3 and t4: each capped revision left a gated, in-scope source whose
    findings were never rechecked, and each cost a continuation. With a slot
    left, the same bounded one-shot the last slot has: the original reviewer
    checks its own finding once on the checked source. RESOLVED closes the
    task clean with the cap on its record; UNRESOLVED, a changed source, a
    recheck that caps with no verdict (the registry, not the recheck's
    return, decides), a failing check or an out-of-scope edit keep the
    capped or failed close exactly as before, and no second writer or
    recheck is called."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    revised = {"failing": "def add(a, b):\n    return a - b\n"}.get(case, TYPED)

    def revision(call, replay):
        H.write(call, {"app.py": revised})
        if case == "out-of-scope":
            H.write(call, {"README.md": "# app\n\nadd raises TypeError.\n"})
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    rechecks = []

    def recheck(call, replay):
        rechecks.append(call)
        if case == "source-changed":
            (replay.project / "app.py").write_text(TYPED + "# touched during the recheck\n")
        if case == "recheck-capped":
            assert call.vendor == "claude", call.vendor
            return H.claude_cap("Still reading the revised source.", num_turns=8)
        return "RESOLVED" if case in ("resolved", "source-changed") else "UNRESOLVED: add('x', 1) still returns a string."

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    revision=revision, collaborator=lambda c, r: FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                    recheck=recheck, auditor=_auditor({"R1": "MET - app.py"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    writers = [c.role for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix") and c.task == "t1"]
    assert writers == ["lead", "revision"], "the capped writer is never called again"
    assert _ledger(replay)[0] == ("charged", ("t1",), 1)
    if case in ("resolved", "unresolved", "source-changed", "recheck-capped"):
        assert len(rechecks) == 1 and "with task slots left in the job" in rechecks[0].prompt
        assert FINDING in rechecks[0].prompt
        assert "in the harness's own observation" in rechecks[0].prompt
        assert replay.artifacts("capped-checked-revision") and not replay.artifacts("capped-final-revision")
        (handoff,) = result["capped_handoffs"]
        assert (handoff["task"], handoff["stage"], handoff["slot"], handoff["final_slot"]) == ("t1", "revision", 1, False)
        assert handoff["outcome"] == {"resolved": "resolved", "unresolved": "unresolved",
                                      "source-changed": "source changed", "recheck-capped": "unresolved"}[case]
        if case == "recheck-capped":
            assert handoff["unresolved"] == ["RFT1-1"], "the open registry finding, not the recheck's empty return"
            assert any(f["fid"] == "RFT1-1" and not f.get("closed") for f in result["review_findings"])
        assert len(handoff["reviewers"]) == 1 and handoff["reviewers"][0].startswith(rechecks[0].vendor + ":")
        assert handoff["changed"] == ["app.py"]
    else:
        assert not rechecks, "no recheck for work that is not the checked, in-scope source"
        assert result["capped_handoffs"] == []
    assert result["slots_unaccounted"] == 0
    if case == "resolved":
        assert result["turn_limited_tasks"] == [], "the rechecked task is not unfinished debt"
        t1 = [t for t in result["workflow"]["tasks"] if t["task_id"] == "t1"][0]
        assert any("turn limit" in f["detail"] for f in t1["facts"]), "the cap event stays on the record"
        assert replay.result.completed, replay.result.error
        return
    assert not replay.result.completed
    if case == "out-of-scope":
        assert result["failed_tasks"] == ["t1"]
        return
    assert result["turn_limited_tasks"] == ["t1"], "the capped close applies as before"
    planners = replay.of("orchestrator")
    assert len(planners) >= 2 and "still owed: t1" in planners[1].prompt, "the planner continues the work by name"
    if case == "source-changed":
        assert (replay.project / "app.py").read_text().endswith("# touched during the recheck\n")
