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


# -- root's correction on 0c5c6f1 (6538988187): the seven items, through the real controller ----------

GOAL = "Implement add(a, b) in app.py. A non-number argument must raise TypeError naming the argument."
R1_TEXT = "add(a, b) returns the sum"


def _calls(replay, role, task=None):
    return [c for c in replay.calls if c.role == role and (task is None or c.task == task)]


@pytest.mark.requirements_ledger
def test_both_recheck_routes_carry_the_original_contract_bound_to_the_findings_own_requirement(tmp_path, monkeypatch):
    """Q1. The same-task recheck (t1's reviewer re-checking its own finding
    against the revision) and the held-finding settlement (t1/R1's finding,
    held when t1 closed capped, inherited and rechecked when t2, covering
    R2, continues t1; the engine refuses RESOLVES on a task that does not
    cover the finding's requirement) both carry the verbatim goal, the
    finding's own requirement
    text (R1, never t2's R2), the order of authority and the boundary rule,
    once, without the full review's verdict duty; the recheck grammar and
    its named-id scope stay."""
    from tests.lifecycle.test_recovery_handoff import GROK_CAP

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def revision(call, replay):
        if len([c for c in replay.of("revision") if c.task == "t1"]) == 1:
            H.write(call, {"app.py": FIXED + "# validation pending\n"})
            return 'Started on validation.\nCHANGED: ["app.py"]'
        # The second revision caps on a source the checks pass: the handoff
        # recheck (the original reviewer, once) leaves the finding
        # unresolved, so the capped close holds it for the planner, and the
        # continuation's own gate can pass on this source.
        H.write(call, {"app.py": FIXED + "# still no validation\n"})
        return GROK_CAP

    def collaborator(call, replay):
        if call.task == "t1":
            return FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE"
        return "NO FINDINGS\nR2: MET - README.md\nREVIEW: COMPLETE"

    def recheck(call, replay):
        return "RFT1-1: RESOLVED fixed" if call.task == "t2" else "RFT1-1: UNRESOLVED: still returns a string."

    continuing = T2 + "\nCONTINUES: t1"
    script = Script(orchestrator=_plan(REQS + T1, continuing), lead=lead, revision=revision, collaborator=collaborator,
                    recheck=recheck, auditor=_auditor({"R1": "NOT MET - owed", "R2": "MET - README.md"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, goal=GOAL, record_complete=False)
    own = _calls(replay, "recheck", "t1")
    held = _calls(replay, "recheck", "t2")
    assert own and held, [c.role + ":" + str(c.task) for c in replay.calls]
    for call in own + held:
        prompt = call.prompt
        assert prompt.count("## Original goal (verbatim; it outranks the task text where they differ)") == 1
        assert GOAL in prompt and prompt.count(_CONTRACT_BOUNDARY_RULE) == 1
        assert prompt.count("Authority runs in this order: the original goal and operator rulings") == 1
        block = prompt.split("## Original requirements the findings below were raised under (verbatim)", 1)[1]
        block = block.split("\n\n", 2)[1]
        assert f"R1 (RFT1-1, raised on t1): {R1_TEXT}" in block and "R2" not in block, \
            "bound to the finding's own requirement, labelled with the finding"
        assert "For each covered requirement give one line" not in prompt, "no full-review verdict duty"
        assert "Expected ids: RFT1-1" in prompt and "Check only your BLOCKING findings" in prompt
    assert "Task t2 continues t1" in held[0].prompt, "the inherited settlement, not a RESOLVES"
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"]


@pytest.mark.requirements_ledger
def test_the_serial_lead_prompt_carries_the_order_of_authority_once(tmp_path, monkeypatch):
    """Q2."""
    replay = H.run(tmp_path, monkeypatch, capacity_script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md"),
                   files=FILES, max_tasks=3, record_complete=False)
    for call in _calls(replay, "lead"):
        assert call.prompt.count("Authority runs in this order: the original goal and operator rulings") == 1, call.task
        assert call.prompt.count(_CONTRACT_BOUNDARY_RULE) == 1 and "## Goal (verbatim" in call.prompt


def _repair_script(auditor_after_repair):
    """t1 implements add without validation; the first final audit finds R1
    NOT MET and DONE is sent back; t2 repairs (TYPED) and the second audit
    judges again."""
    def lead(call, replay):
        H.write(call, {"app.py": TYPED if call.task == "t2" else FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def auditor(call, replay):
        first = len(replay.of("auditor")) == 1
        verdict = {"R1": "NOT MET - a non-number is not rejected"} if first else {"R1": auditor_after_repair}
        return _auditor(verdict)(call, replay)

    repair = _simple("app.py", "Validate both arguments of add in app.py.", "R1")
    return Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1, "DONE", repair),
                  lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE",
                  auditor=auditor, **{"requirements-review": lambda c, r: "COMPLETE"})


@pytest.mark.requirements_ledger
def test_a_repair_after_the_final_audit_renews_the_audit_hold_before_its_writer_is_admitted(tmp_path, monkeypatch):
    """C1, affordable: the first audit consumed its hold; before t2's writer
    runs, the audit is held again on the budget record, t2's admission is
    costed against it, and the second audit is paid and answers."""
    replay = H.run(tmp_path, monkeypatch, _repair_script("MET - app.py"), files=FILES, max_tasks=3,
                   limits=_limits(40), record_complete=False)
    result = H.result_json(replay)
    assert [c.task for c in _calls(replay, "lead")] == ["t1", "t2"]
    assert len(_calls(replay, "auditor")) == 2, "the repair bought a second audit"
    (renewal,) = result["finish_renewals"]
    assert renewal["tasks"] == ["t2"] and renewal["renewed"] is True and renewal["reopens"] == 1
    budget = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())
    fin = budget["finish_protection"]
    assert [r["stage"] for r in fin["renewals"]] == ["audit"] and fin["renewals"][0]["previously_started"] is True
    assert fin["started"].count("audit") == 2, "both audits started under a hold"
    assert fin["renewal_refusals"] == []
    t2 = [a for a in result["plan_admissions"] if a["task"] == "t2"]
    assert t2 and t2[0]["at_attempt"] > renewal["at_attempt"] - 1, "admission costed after the renewal"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_repair_the_renewed_audit_leaves_unpayable_is_not_started(tmp_path, monkeypatch):
    """C1, tight: with the audit held again, t2's mandatory route plus the
    held finish cannot be paid from the calls left, so t2 is NOT RUN before
    any call, nothing is written, and the stop names the forfeit. Without
    the renewal the same allowance would have admitted the writer and left
    the second audit unpayable."""
    replay = H.run(tmp_path, monkeypatch, _repair_script("MET - app.py"), files=FILES, max_tasks=3,
                   limits=_limits(10), record_complete=False)
    result = H.result_json(replay)
    assert [c.task for c in _calls(replay, "lead")] == ["t1"], "nothing of t2 ran"
    assert [n["task"] for n in result["not_started"]] == ["t2"]
    assert result["finish_renewals"] and result["finish_renewals"][0]["renewed"] is True
    assert any("audit" in r.get("held", []) for r in json.loads(
        (Path(replay.result.run_dir) / "budget.json").read_text())["finish_protection"]["refusals"]) or \
        any(a["task"] == "t2" and not a["feasible"] for a in result["plan_admissions"])
    assert (replay.project / "app.py").read_text() == FIXED, "the repair never wrote"
    assert not replay.result.completed and "forfeited" in (replay.result.error or ""), replay.result.error


@pytest.mark.requirements_ledger
def test_a_done_repeated_over_nothing_is_sent_back_before_any_second_audit(tmp_path, monkeypatch):
    """C1, unchanged source (addendum 6537675374): DONE after a NOT MET
    audit, said again with no task between, is sent back by the coverage
    check on the verdict already recorded, before any audit is asked; the
    reopens end the run as before, on one auditor call, and the one audit
    on record carries the packet binding it judged."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1, "DONE", "DONE", "DONE"),
                    lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE",
                    auditor=_auditor({"R1": "NOT MET - a non-number is not rejected"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_limits(40), record_complete=False)
    result = H.result_json(replay)
    assert len(_calls(replay, "auditor")) == 1, "nothing changed: no second audit is drawn"
    (audit,) = result["requirements"]["audits"]
    assert audit["binding"] and audit["verdicts"]["R1"]["met"] is False
    fin = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())["finish_protection"]
    assert fin["started"].count("audit") == 1 and fin["renewals"] == []
    assert not replay.result.completed and "DONE was sent back 3 time(s)" in (replay.result.error or ""), \
        replay.result.error
    assert "found not met by the audit and not covered again since" in replay.result.error


@pytest.mark.requirements_ledger
def test_a_repair_that_left_the_source_byte_identical_is_audited_on_its_new_packet(tmp_path, monkeypatch):
    """C1, the packet reading stated: t2 is admitted after the NOT MET audit
    with the hold renewed, writes nothing and closes; its close added a
    check receipt and an obligation row the auditor consumes, so the packet
    is a new one (the engine's own definition of what an audit was about;
    tests/test_boundary_records.py pins that every evidence the audit
    consumes is in it) and the renewed hold pays a second audit of the
    byte-identical source. A source-only cache was tried and refused by
    those controls."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        return 'Already validated; nothing to change.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: []'

    repair = _simple("app.py", "Validate both arguments of add in app.py.", "R1")
    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1, "DONE", repair),
                    lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE",
                    auditor=_auditor({"R1": "NOT MET - a non-number is not rejected"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_limits(40), record_complete=False)
    result = H.result_json(replay)
    assert [c.task for c in _calls(replay, "lead")] == ["t1", "t2"]
    assert len(_calls(replay, "auditor")) == 2, "a new packet over the same bytes is audited under the renewed hold"
    audits = result["requirements"]["audits"]
    assert audits[1]["source"] == audits[0]["source"] and audits[1]["binding"] != audits[0]["binding"]
    assert not audits[1].get("reused")
    assert (replay.project / "app.py").read_text() == FIXED
    fin = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())["finish_protection"]
    assert [r["stage"] for r in fin["renewals"]] == ["audit"] and fin["started"].count("audit") == 2
    assert not replay.result.completed and "RequirementsUnmet" in (replay.result.error or ""), replay.result.error


@pytest.mark.parametrize("calls", [40, 10])
@pytest.mark.requirements_ledger
def test_a_review_only_task_after_a_not_met_audit_renews_the_hold_too(tmp_path, monkeypatch, calls):
    """C1, review-only: a zero-edit task that re-covers the NOT MET
    requirement is a dispatched route whose close makes the next DONE's
    audit necessary, so it is held to the same renewal: affordable, the
    hold is renewed before its lead and pays the second audit; tight, it
    is not started before any call (the refutation of this batch found it
    admitted with no hold, spending its calls with the second audit then
    unpayable)."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        return 'Reviewed add: the validation is present.\nCHANGED: []'

    def auditor(call, replay):
        verdict = ({"R1": "NOT MET - a non-number is not rejected"} if len(replay.of("auditor")) == 1
                   else {"R1": "MET - app.py"})
        return _auditor(verdict)(call, replay)

    review = _simple("app.py", "Review add in app.py against R1.", "R1").replace(
        '"max_lines": 20', '"max_lines": 1, "edits": "none"')
    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1, "DONE", review),
                    lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE",
                    auditor=auditor, **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_limits(calls),
                   record_complete=False)
    result = H.result_json(replay)
    (renewal,) = result["finish_renewals"]
    assert renewal["tasks"] == ["t2"] and renewal["renewed"] is True
    if calls == 40:
        assert [c.task for c in _calls(replay, "lead")] == ["t1", "t2"]
        assert len(_calls(replay, "auditor")) == 2, "the review-only task bought the second audit it made necessary"
        fin = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())["finish_protection"]
        assert [r["stage"] for r in fin["renewals"]] == ["audit"] and fin["started"].count("audit") == 2
        t2 = [a for a in result["plan_admissions"] if a["task"] == "t2"]
        assert t2 and t2[0]["at_attempt"] > renewal["at_attempt"] - 1, "admission costed after the renewal"
    else:
        assert [c.task for c in _calls(replay, "lead")] == ["t1"], "nothing of t2 ran"
        assert [n["task"] for n in result["not_started"]] == ["t2"]
        assert not replay.result.completed and "forfeited" in (replay.result.error or ""), replay.result.error


@pytest.mark.requirements_ledger
def test_every_dispatched_task_has_a_committed_admission_before_its_first_effect(tmp_path, monkeypatch):
    """C2: the admission is on the job store under the committed authority
    before the task's first call, numbered by slot, and the charged ledger
    entry names it."""
    from quadratus import jobstore
    replay = H.run(tmp_path, monkeypatch, capacity_script([PLAN_1, PLAN_2], t2_review="NO FINDINGS\nR2: MET - README.md"),
                   files=FILES, max_tasks=3, record_complete=False)
    result = H.result_json(replay)
    admissions = result["task_admissions"]
    assert [(a["admission"], a["tasks"], a["slot"], a["outcome"]) for a in admissions] == [
        ("A1", ["t1"], 1, "charged"), ("A2", ["t2"], 2, "charged")]
    run_dir = Path(replay.result.run_dir)
    manifest = jobstore.read_manifest(run_dir)
    committed = jobstore.read_record(run_dir, "task-admissions", manifest=manifest)
    assert [a["admission"] for a in committed] == ["A1", "A2"]
    authority = jobstore.read_record(run_dir, "authority", manifest=manifest)
    assert all(a["authority"] == authority["digest"] for a in committed)
    ledger = [json.loads(line) for line in (run_dir / "invocations.jsonl").read_text().splitlines()]
    first_lead = next(i for i, row in enumerate(ledger, 1) if row.get("role") == "lead" and row.get("task") == "t1")
    assert admissions[0]["at_attempt"] < first_lead, "admitted before the first effect"
    assert [e["why"] for e in result["slot_ledger"] if e["event"] == "charged"] == ["A1", "A2"]


@pytest.mark.requirements_ledger
def test_an_admission_the_store_cannot_commit_starts_nothing(tmp_path, monkeypatch):
    """C2: publication failure refuses the task before any call."""
    from quadratus.jobstore import JobStore
    original = JobStore.publish

    def broken(self, updates, **kw):
        if "task-admissions" in updates:
            raise OSError("disk full")
        return original(self, updates, **kw)
    monkeypatch.setattr(JobStore, "publish", broken)
    replay = H.run(tmp_path, monkeypatch, capacity_script([PLAN_1, PLAN_2]), files=FILES, max_tasks=3,
                   record_complete=False)
    assert not _calls(replay, "lead") and not _calls(replay, "collaborator"), "no task call was made"
    assert "JobStoreUnavailable" in (replay.result.error or "") and "could not publish its admission" in replay.result.error
    assert (replay.project / "app.py").read_text() == FILES["app.py"]
    result = H.result_json(replay)
    assert result["task_admissions"] == []
    # Nothing ran, so no slot is spent and the record says not started
    # (workflow refutation of this batch: the charge read dispatched work).
    assert [(e["event"], e["tasks"], e["slots_used"]) for e in result["slot_ledger"]] == [("not_started", ["t1"], 0)]
    assert [n["task"] for n in result["not_started"]] == ["t1"] and result["slots_unaccounted"] == 0
    assert result["workflow"]["tasks"][0]["closed_as"] == "not_started", "the typed outcome says nothing ran"


@pytest.mark.requirements_ledger
def test_a_task_interrupted_after_its_admission_is_refused_on_resume_with_the_admission_retained(tmp_path, monkeypatch):
    """C2, conservative resume through the real controller: t2 is admitted
    (committed to the parent's store) and interrupted before its first
    effect; the quiescent checkpoint predates it. The resume is refused by
    the existing unconsumed-operation guard (the planner's settled call
    that named t2 was never consumed by a checkpoint), and the refusal
    names the committed admission beside the retained receipts: nothing is
    dropped, nothing is re-planned, no slot is refunded, and the parent's
    record is left exactly as the first episode committed it. The count a
    resumed episode would make, were a later cadence to admit one, is the
    unit control in tests/test_quality_completion_units.py."""
    from quadratus import checkpoint, jobstore
    from quadratus.checkpoint import CheckpointRefused
    from quadratus.session import Session
    real = Session._register_acceptance_obligations

    def interrupt(self, spec, covers):
        if spec.task_id == "t2" and not Session._interrupted_once:
            Session._interrupted_once = True
            raise KeyboardInterrupt
        return real(self, spec, covers)
    monkeypatch.setattr(Session, "_register_acceptance_obligations", interrupt)
    monkeypatch.setattr(Session, "_interrupted_once", False, raising=False)
    first = H.run(tmp_path, monkeypatch, capacity_script([PLAN_1, PLAN_2, PLAN_2],
                                                         t2_review="NO FINDINGS\nR2: MET - README.md"),
                  files=FILES, max_tasks=3, record_complete=False)
    assert "KeyboardInterrupt" in first.result.error
    result = H.result_json(first)
    # Interrupted after its admission: charged in the first episode's own
    # record (a failure after admission counts), absent from the checkpoint.
    assert [(a["tasks"], a["outcome"]) for a in result["task_admissions"]] == [(["t1"], "charged"), (["t2"], "charged")]
    assert not _calls(first, "lead", "t2"), "interrupted before t2's first effect"
    run_dir = Path(first.result.run_dir)
    before = jobstore.read_manifest(run_dir)
    body = checkpoint.load(run_dir)
    assert [a["admission"] for a in body["state"]["task_admissions"]] == ["A1"], "the quiescent point predates A2"
    assert [a["admission"] for a in jobstore.read_record(run_dir, "task-admissions", manifest=before)] == ["A1", "A2"]
    with pytest.raises(CheckpointRefused, match="never consumed by the record") as refused:
        _episode(first, resume_from=run_dir.name, max_tasks=3)
    text = str(refused.value)
    assert "retained as receipts" in text and "A2 (t2)" in text and "never dropped or re-planned" in text, text
    assert jobstore.read_manifest(run_dir) == before, "the parent's committed record is untouched by the refusal"
    assert [a["admission"] for a in jobstore.read_record(run_dir, "task-admissions")] == ["A1", "A2"]
    assert len(_calls(first, "orchestrator")) == 2, "no planner call was made over the refused resume"
    assert H.result_json(first) == result, "the parent's own record is not rewritten"


@pytest.mark.requirements_ledger
def test_a_capped_close_after_a_handoff_reports_the_recheck_it_performed(tmp_path, monkeypatch):
    """C3: a capped revision whose checked source met its reviewer once and
    stayed unresolved closes capped, and its record says so: the recheck,
    its outcome and the observation it rested on, never 'before its
    recheck' or 'unreviewed and ungated'. The plain capped close keeps the
    old wording."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def revision(call, replay):
        H.write(call, {"app.py": TYPED})
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    revision=revision, collaborator=lambda c, r: FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                    recheck=lambda c, r: "UNRESOLVED: add('x', 1) still returns a string.",
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == ["t1"]
    finding = next(f for f in result["review_findings"] if f["fid"] == "RFT1-1")
    assert finding["hold"].startswith("UNRESOLVED by claude:opus at its one recheck of t1's capped source"), \
        finding["hold"]
    assert "before its recheck" not in finding["hold"]
    (record,) = replay.artifact_texts("turn-limited")
    assert json.loads(record)["handoff"]["outcome"] == "unresolved"
    planner = replay.of("orchestrator")[1].prompt
    assert "re-checked its finding once on it: UNRESOLVED (RFT1-1)" in planner
    assert "an observation for the reviewers; the task's own canonical gate" in planner
    assert "unreviewed and ungated" not in planner
    # The plain capped close (no gated handoff) keeps its wording.
    plain = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                   revision=lambda c, r: (H.write(c, {"app.py": "def add(a, b):\n    return a - b\n"}) or
                                          H.grok_ok("Still revising", stop="cancelled", num_turns=8)),
                   collaborator=lambda c, r: FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                   auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    (tmp_path / "plain").mkdir()
    other = H.run(tmp_path / "plain", monkeypatch, plain, files=FILES, max_tasks=3,
                  settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    assert "Changed, unreviewed and ungated" in other.of("orchestrator")[1].prompt
    assert next(f for f in H.result_json(other)["review_findings"] if f["fid"] == "RFT1-1")["hold"].endswith("before its recheck")


@pytest.mark.requirements_ledger
def test_a_capped_recheck_is_reported_as_no_verdict_never_as_the_reviewers_unresolved(tmp_path, monkeypatch):
    """C3, no verdict (workflow refutation of this batch): the handoff's
    reviewer stops at its own turn cap, so it gave no verdict; the finding
    stays open and the close says the recheck gave no verdict on it, never
    the recheck grammar's UNRESOLVED, and the hold says the same."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def revision(call, replay):
        H.write(call, {"app.py": TYPED})
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    revision=revision, collaborator=lambda c, r: FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                    recheck=lambda c, r: H.claude_cap("Still reading the revised source.", num_turns=8),
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == ["t1"]
    finding = next(f for f in result["review_findings"] if f["fid"] == "RFT1-1")
    assert not finding.get("closed") and finding.get("history") in (None, [])
    assert finding["hold"].startswith("no verdict at claude:opus's one recheck of t1's capped source"), finding["hold"]
    assert "UNRESOLVED" not in finding["hold"]
    (record,) = replay.artifact_texts("turn-limited")
    handoff = json.loads(record)["handoff"]
    assert handoff["outcome"] == "unresolved" and handoff["findings"]["RFT1-1"].startswith("no verdict")
    planner = replay.of("orchestrator")[1].prompt
    assert "re-checked its finding once on it: no verdict on RFT1-1 (the recheck stopped at its turn cap" in planner
    assert "UNRESOLVED (RFT1-1)" not in planner


@pytest.mark.requirements_ledger
def test_a_capped_close_with_another_reviewers_finding_open_says_what_was_resolved(tmp_path, monkeypatch):
    """C3, other findings open (workflow refutation of this batch): the
    handoff's reviewer resolved its own finding on the capped source while
    another reviewer's finding, outside that recheck, stayed open; the task
    is unfinished and the close says exactly that, never that the recheck
    'ended ... with no verdict'."""
    near_miss = "BLOCKING: SCENARIO P1 - the declared scenario never states that a string argument raises TypeError"
    complex_t1 = T1.replace("KIND: backend simple", "KIND: backend complex")

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def revision(call, replay):
        if len([c for c in replay.of("revision") if c.task == "t1"]) == 1:
            H.write(call, {"app.py": FIXED + "# pass 1\n"})
            return 'Revised.\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": TYPED})
        if call.vendor == "claude":
            return H.claude_cap("Now checking the TypeError path", num_turns=8)
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    def collaborator(call, replay):
        if call.vendor == "grok":
            return near_miss + "\nR1: MET - app.py\nREVIEW: COMPLETE"
        return FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE"

    def recheck(call, replay):
        if call.vendor == "grok":
            return "RFT1-2: UNRESOLVED: the scenario still does not state it."
        mine = [c for c in replay.of("recheck") if c.vendor == call.vendor]
        return "RFT1-1: RESOLVED fixed" if len(mine) >= 2 else "RFT1-1: UNRESOLVED: add('x', 1) still returns a string."

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + complex_t1), lead=lead,
                    revision=revision, collaborator=collaborator, recheck=recheck,
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == ["t1"], replay.result.error
    (handoff,) = [h for h in result["capped_handoffs"] if h["task"] == "t1"]
    assert handoff["outcome"] == "unresolved: other findings open" and handoff["resolved"] == ["RFT1-1"]
    assert [o["finding"] for o in handoff["others_open"]] == ["RFT1-2"]
    by_id = {f["fid"]: f for f in result["review_findings"]}
    assert by_id["RFT1-1"].get("closed") and not by_id["RFT1-2"].get("closed")
    assert by_id["RFT1-2"]["hold"].startswith("not rechecked: outside the one recheck of t1's capped source by "
                                              "openai:sol (its reviewer grok:default was not asked)"), by_id["RFT1-2"]["hold"]
    planner = replay.of("orchestrator")[1].prompt
    assert "re-checked its finding once on it: RESOLVED (RFT1-1); findings of other reviewers were open" in planner
    assert "RFT1-2 by grok:default" in planner and "with no verdict" not in planner


@pytest.mark.requirements_ledger
def test_the_checker_rejects_a_saved_record_whose_clearing_check_ran_under_other_requirement_revisions(
        tmp_path, monkeypatch):
    """E1: the genuine record clears; the same record with only the check's
    R1 revision moved (the typed row still bound to revision 1 and reading
    ran) is contradictory and is refused, as is one whose check carries no
    revision map; the met-over-debt rule names the row."""
    from tools import truth_check as T

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return ('Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\n'
                'NOT RUN: tests/test_app.py::test_add - the sandbox refused to start pytest\nCHANGED: ["app.py"]')

    script = Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=lead,
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.result.completed, replay.result.error
    record = json.loads((Path(replay.result.run_dir) / "result.json").read_text())
    [entry] = [e for e in record["unexecuted_acceptance"] if e["task"] == "t1"]
    assert T._not_run_cleared(entry, record["checks"], record) and T._met_over_debt(record) == []
    assert T.judge(replay.result.run_dir)["verdict"] == T.VERIFIED

    def altered(mutate):
        copy = json.loads(json.dumps(record))
        [e] = [e for e in copy["unexecuted_acceptance"] if e["task"] == "t1"]
        mutate(copy)
        return T._not_run_cleared(e, copy["checks"], copy), T._met_over_debt(copy)

    def moved(copy):
        for check in copy["checks"]:
            check["requirements"]["R1"] = 2

    def no_map(copy):
        for check in copy["checks"]:
            check.pop("requirements", None)
    for mutate in (moved, no_map):
        cleared, lines = altered(mutate)
        assert cleared is False
        assert lines and "does not show its entry cleared under the row's requirement revisions" in lines[0]
    run_dir = tmp_path / "altered"
    run_dir.mkdir()
    copy = json.loads(json.dumps(record))
    moved(copy)
    (run_dir / "result.json").write_text(json.dumps(copy))
    verdict = T.judge(run_dir)
    assert verdict["verdict"] == T.UNVERIFIED
    assert any("requirement revisions" in line for line in verdict["unverified"])


# -- second-round refutation of the follow-up fixes, through the real controller ----------


@pytest.mark.requirements_ledger
def test_a_renewed_audit_with_no_token_reserve_leaves_room_for_the_done_between(tmp_path, monkeypatch):
    """C1, zero reserve: at 11 calls the renewal admitted the repair without
    counting the planner's DONE between it and the second audit, and the
    DONE was refused. The planning call is now counted while a renewed audit
    hold is pending, so the repair runs and both audits are asked."""
    replay = H.run(tmp_path, monkeypatch, _repair_script("MET - app.py"), files=FILES, max_tasks=3,
                   limits=_limits(11), record_complete=False)
    assert [c.task for c in _calls(replay, "lead")] == ["t1", "t2"]
    assert len(_calls(replay, "auditor")) == 2, replay.result.error
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_renewal_for_a_repair_that_is_not_started_is_released(tmp_path, monkeypatch):
    """C1, refused repair: at 8 calls the renewal is made for t2 and t2 is
    refused before any call; the renewed hold is released, so the cap's goal
    question is asked and the stop names the unmet requirement, as before
    the renewal existed. The renewal stays counted against its bound."""
    replay = H.run(tmp_path, monkeypatch, _repair_script("MET - app.py"), files=FILES, max_tasks=3,
                   limits=_limits(8), record_complete=False)
    result = H.result_json(replay)
    assert [c.task for c in _calls(replay, "lead")] == ["t1"], "nothing of t2 ran"
    assert [n["task"] for n in result["not_started"]] == ["t2"]
    (renewal,) = result["finish_renewals"]
    assert renewal["tasks"] == ["t2"] and renewal["renewed"] is True and renewal["released"] == "t2 was not started"
    fin = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())["finish_protection"]
    assert [r["stage"] for r in fin["renewals"]] == ["audit"], "counted against the bound"
    assert any(r["stage"] == "audit" and r["why"] == "t2 was not started" for r in fin["released"])
    assert (replay.result.error or "").startswith("RequirementsUnmet"), replay.result.error


def test_an_explicit_task_whose_admission_is_refused_is_not_recorded_as_ran(tmp_path, monkeypatch):
    """C2, explicit list: a refused admission takes back the 'ran' entry, so
    the list's record, the not-started record and the typed outcome agree,
    and the not-started lead is the one the route chose."""
    from quadratus.jobstore import JobStore
    from tests.lifecycle.test_explicit_tasks import README_TASK
    from tests.lifecycle.test_explicit_tasks import _run as explicit_run
    original = JobStore.publish

    def broken(self, updates, **kw):
        if "task-admissions" in updates:
            raise OSError("disk full")
        return original(self, updates, **kw)
    monkeypatch.setattr(JobStore, "publish", broken)
    replay = explicit_run(tmp_path, monkeypatch, [README_TASK], record_complete=False)
    result = H.result_json(replay)
    assert "could not publish its admission" in (replay.result.error or ""), replay.result.error
    assert replay.calls == [], "no call was made"
    assert result["explicit_tasks"]["ran"] == [] and result["explicit_tasks"]["not_run"] == 1, result["explicit_tasks"]
    (refused,) = result["not_started"]
    assert refused["task"] == "t1" and refused["lead"] == result["workflow"]["tasks"][0]["lead"] != ""
    assert result["workflow"]["tasks"][0]["closed_as"] == "not_started"


@pytest.mark.requirements_ledger
def test_a_task_start_authority_commit_the_store_refuses_charges_nothing(tmp_path, monkeypatch):
    """C2, the sibling commit: the authority committed at a task's start,
    one step before its admission, refused by the store; nothing ran, so no
    slot is charged and the record says not started."""
    from quadratus import session as S
    from quadratus.jobstore import JobStore
    real = S.Session._authority_before
    state = {"armed": False}
    original = JobStore.publish

    def wrapped(self, what):
        state["armed"] = what.startswith("task ")
        try:
            return real(self, what)
        finally:
            state["armed"] = False

    def broken(self, updates, **kw):
        if state["armed"] and "authority" in updates:
            raise OSError("disk full")
        return original(self, updates, **kw)
    monkeypatch.setattr(S.Session, "_authority_before", wrapped)
    monkeypatch.setattr(JobStore, "publish", broken)
    replay = H.run(tmp_path, monkeypatch, capacity_script([PLAN_1, PLAN_2]), files=FILES, max_tasks=3,
                   record_complete=False)
    result = H.result_json(replay)
    assert "JobStoreUnavailable" in (replay.result.error or "") and "approval authority" in replay.result.error
    assert not _calls(replay, "lead"), "no task call was made"
    assert [(e["event"], e["tasks"], e["slots_used"]) for e in result["slot_ledger"]] == [("not_started", ["t1"], 0)]
    assert [n["task"] for n in result["not_started"]] == ["t1"] and result["slots_unaccounted"] == 0
    assert result["workflow"]["tasks"][0]["closed_as"] == "not_started"


def _c3_script(case):
    """The capped-handoff shapes the second-round refutation found worded
    wrongly: a NEW finding at the recheck, an INVALIDATED verdict beside
    another reviewer's open finding, and an UNRESOLVED verdict beside one."""
    near_miss = "BLOCKING: SCENARIO P1 - the declared scenario never states that a string argument raises TypeError"
    if case == "new":
        def revision(call, replay):
            H.write(call, {"app.py": TYPED})
            return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)
        return Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + T1), lead=_c3_lead,
                      revision=revision, collaborator=lambda c, r: FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE",
                      recheck=lambda c, r: ("RFT1-1: RESOLVED the TypeError path is in place\nNEW: add(None, 1) raises "
                                            "AttributeError instead of TypeError | basis: R1 | impact: crash"),
                      auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})

    def revision(call, replay):
        if len([c for c in replay.of("revision") if c.task == "t1"]) == 1:
            H.write(call, {"app.py": FIXED + "# pass 1\n"})
            return 'Revised.\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": TYPED})
        if call.vendor == "claude":
            return H.claude_cap("Now checking the TypeError path", num_turns=8)
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    def collaborator(call, replay):
        if call.vendor == "grok":
            return near_miss + "\nR1: MET - app.py\nREVIEW: COMPLETE"
        return FINDING + "\nR1: MET - app.py\nREVIEW: COMPLETE"

    def recheck(call, replay):
        if call.vendor == "grok":
            return "RFT1-2: UNRESOLVED: the scenario still does not state it."
        if case == "invalidated" and len([c for c in replay.of("recheck") if c.vendor == call.vendor]) >= 2:
            return "RFT1-1: INVALIDATED tests/test_app.py::test_add shows strings are allowed by R1"
        return "RFT1-1: UNRESOLVED: add('x', 1) still returns a string."

    complex_t1 = T1.replace("KIND: backend simple", "KIND: backend complex")
    return Script(orchestrator=_plan(REQS.replace("R2: README documents add\n", "") + complex_t1), lead=_c3_lead,
                  revision=revision, collaborator=collaborator, recheck=recheck,
                  auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})


def _c3_lead(call, replay):
    H.write(call, {"app.py": FIXED})
    return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'


@pytest.mark.parametrize("case", ["new", "invalidated", "unresolved-others"])
@pytest.mark.requirements_ledger
def test_a_capped_close_words_each_finding_by_what_its_recheck_came_to(tmp_path, monkeypatch, case):
    """C3, second round: each finding's hold and the planner's clause say
    what the handoff's recheck came to for that finding, read from the
    registry, with the outcome first so the planner's 120-character view of
    the hold still carries it. A raised finding is never the reviewer's
    UNRESOLVED, an INVALIDATED verdict is never RESOLVED, and another
    reviewer's finding outside the recheck is never credited to it."""
    replay = H.run(tmp_path, monkeypatch, _c3_script(case), files=FILES, max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == ["t1"], replay.result.error
    by_id = {f["fid"]: f for f in result["review_findings"]}
    (handoff,) = [h for h in result["capped_handoffs"] if h["task"] == "t1"]
    planner = replay.of("orchestrator")[1].prompt
    view = {fid: next((line for line in planner.splitlines() if line.startswith(f"- {fid} (")), "")
            for fid in by_id}
    if case == "new":
        assert handoff["findings"] == {"RFT1-2": "raised"} and handoff["resolved"] == ["RFT1-1"]
        assert by_id["RFT1-1"].get("closed") and not by_id["RFT1-2"].get("closed")
        assert by_id["RFT1-2"]["hold"].startswith("raised by claude:opus at the recheck of t1's capped source")
        assert "re-checked its finding once on it: RESOLVED (RFT1-1); raised during the recheck: RFT1-2" in planner
        assert "UNRESOLVED (RFT1-2)" not in planner
        assert "held: raised by claude:opus at the recheck" in view["RFT1-2"]
    elif case == "invalidated":
        assert handoff["invalidated"] == ["RFT1-1"] and handoff["resolved"] == []
        assert handoff["outcome"] == "unresolved: other findings open"
        assert "re-checked its finding once on it: INVALIDATED (RFT1-1); findings of other reviewers were open" in planner
        assert "RESOLVED (RFT1-1)" not in planner
        assert by_id["RFT1-2"]["hold"].startswith("not rechecked: outside the one recheck of t1's capped source")
        assert "held: not rechecked: outside the one recheck" in view["RFT1-2"]
    else:
        assert handoff["outcome"] == "unresolved" and handoff["findings"] == {"RFT1-1": "unresolved"}
        assert [o["finding"] for o in handoff["others_open"]] == ["RFT1-2"]
        assert by_id["RFT1-1"]["hold"].startswith("UNRESOLVED by openai:sol at its one recheck of t1's capped source")
        assert by_id["RFT1-2"]["hold"].startswith("not rechecked: outside the one recheck of t1's capped source by "
                                                  "openai:sol (its reviewer grok:default was not asked)")
        assert ("re-checked its finding once on it: UNRESOLVED (RFT1-1); findings of other reviewers were open and "
                "outside that recheck (RFT1-2 by grok:default)") in planner
        assert "held: UNRESOLVED by openai:sol" in view["RFT1-1"] and "held: not rechecked" in view["RFT1-2"]
