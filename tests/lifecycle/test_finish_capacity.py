"""The finish within the job's own limits (Davis, 2026-10-16).

R22 closed three tasks and spent its 20 calls before the cap's goal
question; the audit after it never ran. Two changes, no limit moved:

- the job's last dispatched wave closes with a factual record the harness
  writes (its reviews as they ended, its last check and source, its open
  debt, evidence pointers), not a model summary no later planning call
  reads;
- the goal question and the audit are held one call and one full per-call
  reserve each from ordinary work. An ordinary call that would spend them
  is refused before it starts and recorded; what it would have done stays
  undone and every current debt gate still decides, so a refused review
  leaves its finding open and the run stops on it rather than finishing.

Whole-run replays below the vendor CLI (tests.lifecycle.harness).
"""

import json
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script, judging
from tests.lifecycle.test_step2_recovery import _episode, _interrupt_after, _simple

PLAN_1 = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
          + _simple("app.py", "Implement add in app.py.", "R1"))
PLAN_2 = _simple("README.md", "Document add in README.md.", "R2")
BATCH = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\nPARALLEL\n"
         + _simple("app.py", "Implement add in app.py.", "R1") + "\n---\n"
         + _simple("README.md", "Document add in README.md.", "R2"))
README = "# app\n\n`add(a, b)` returns the sum.\n"


def _limits(calls):
    return RunLimits(max_calls=calls, max_reported_tokens=6_000_000, wall_seconds=600, max_concurrent_workers=2)


#: This file's own explicit judgment of t2's documentation scenario (a
#: reading row): README.md names add(a, b) and its result, which is all R2 asks.
DOCS_JUDGED = "SUFFICIENT README.md names add(a, b) and states that it returns the sum, which is all R2 asks"


def _script(plan, *, t2_review="BLOCKING: README lacks an example\nR2: MET - README.md",
            recheck="RESOLVED", closeout=None):
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plan[n - 1] if n <= len(plan) else "DONE"

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collaborator(call, replay):
        return t2_review if call.task == "t2" else "NO FINDINGS\nR1: MET - app.py"

    extra = {"closeout": closeout} if closeout else {}
    return Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                  recheck=lambda call, replay: recheck,
                  auditor=judging(lambda call, replay: "R1: MET - app.py\nR2: MET - README.md", reading=DOCS_JUDGED),
                  **{"requirements-review": lambda call, replay: "COMPLETE"}, **extra)


def _budget(replay):
    return json.loads((Path(replay.result.run_dir) / "budget.json").read_text())


def _roles(replay):
    return [(c.task, c.role) for c in replay.calls]


@pytest.mark.requirements_ledger
def test_serial_the_last_wave_closes_factually_and_the_finish_runs_inside_the_limit(tmp_path, monkeypatch):
    """12 calls. On 12f8 this script spent call 11 on t2's model close-out,
    asked the goal question at 12 and stopped at call_limit before the
    audit. Now t2 closes factually, the goal question is call 11 and the
    audit call 12, and the run completes. t1's close-out, which the next
    planning call reads, is still the lead's own."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=2,
                   limits=_limits(12), record_complete=False)
    assert _roles(replay)[-2:] == [("run", "orchestrator"), ("audit", "auditor")]
    assert [c.task for c in replay.of("closeout")] == ["t1"]
    assert replay.result.completed, replay.result.error
    budget = _budget(replay)
    assert budget["reserved_attempts"] == 12 and budget["finish_protection"]["started"] == ["goal", "audit"]
    assert budget["finish_protection"]["refusals"] == []
    record = json.loads(replay.artifact_texts("factual-closeout")[0])
    assert record["task"] == "t2" and "last dispatched wave" in record["why"]
    assert record["reviews"] == [["openai:gpt-5.6-sol", "reviewed: blocking findings"],
                                 ["openai:gpt-5.6-sol", "recheck: RFT2-1 resolved"]]
    assert record["check"] == "last check PASSED on the current source"
    assert record["changed"] == ["README.md"] and len(record["evidence"]) == 2
    summary = H.result_json(replay)
    t2 = next(t for t in summary["workflow"]["tasks"] if t["task_id"] == "t2")
    assert t2["edges"].get("review") is True, "the factual record changes no review edge"
    ledger = (Path(replay.result.run_dir) / "ledger.md").read_text()
    assert "Recorded by the harness, not a model" in ledger and "No collaborator review" not in ledger
    assert "codebase map was not amended" in ledger


@pytest.mark.requirements_ledger
def test_serial_a_route_the_finish_cannot_afford_whole_runs_terminal_and_a_blocking_review_stays_open(tmp_path,
                                                                                                        monkeypatch):
    """10 calls over three slots: after t1 (lead, review, close-out) and
    the planning call for t2, two calls are left after the held goal
    question and audit. t2's ordinary route is three (its lead, its review
    and a model close-out before the last slot) and cannot be paid; its
    terminal route (admission/3) is the same task, reviewer and gates
    closed by the harness's factual record, two calls, and is admitted.
    The job commits to end after t2 before any call of t2's: the third
    slot is relinquished, recorded and named. t2's review blocks; the
    correction it selects (revision, recheck) cannot be paid and is
    refused whole, so the finding stays open as written, t2 closes
    factually with it, and the run ends FindingsOpen with the goal
    question and the audit never asked: a terminal route buys the task,
    never a completion."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=3,
                   limits=_limits(10), record_complete=False)
    assert _roles(replay)[-2:] == [("t2", "lead"), ("t2", "collaborator")]
    assert not replay.of("auditor") and not replay.result.completed
    result = H.result_json(replay)
    rows = [(a["scope"], a["feasible"], a["short"]) for a in result["plan_admissions"] if a["task"] == "t2"]
    assert rows == [("mandatory", True, {}), ("full", False, {"calls": 1}), ("terminal", True, {}),
                    ("fix-cycle", False, {"calls": 2})]
    terminal = result["terminal_route"]
    assert (terminal["task"], terminal["slots_used"], terminal["relinquished"], terminal["max_tasks"]) == ("t2", 2, 1, 3)
    assert "short by calls 1" in terminal["reason"] and "close-out" not in terminal["selected"]
    assert json.loads(replay.artifact_texts("terminal-route")[0]) == terminal
    assert result["not_started"] == [] and [c.task for c in replay.of("closeout")] == ["t1"]
    assert [json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")] == ["t2"]
    assert "terminal route" in json.loads(replay.artifact_texts("factual-closeout")[0])["why"]
    assert result["finish_refusals"][-1]["what"] == "fix round"
    assert replay.result.error.startswith("FindingsOpen"), replay.result.error
    assert (replay.project / "README.md").read_text() != FILES["README.md"], "t2's lead wrote"
    budget = _budget(replay)
    assert budget["finish_protection"]["pending"] == ["goal", "audit"] and budget["finish_protection"]["refusals"] == []
    assert budget["stop_reason"] == ""


@pytest.mark.requirements_ledger
def test_serial_a_mandatory_deficit_is_refused_before_any_write_with_no_terminal_candidate(tmp_path, monkeypatch):
    """9 calls over three slots: one call is left after the held finish
    when t2 is planned, and t2's known stages are two. No terminal route
    is tried (it drops only the close-out): the refusal is the mandatory
    one, nothing of t2 runs or is written, t2 is NOT RUN, the remaining
    slots are forfeited and the stop says so."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=3,
                   limits=_limits(9), record_complete=False)
    assert not [c for c in replay.calls if c.task == "t2"], "nothing of t2 ran"
    result = H.result_json(replay)
    assert [n["task"] for n in result["not_started"]] == ["t2"] and result["terminal_route"] is None
    assert [a["scope"] for a in result["plan_admissions"] if a["task"] == "t2"] == ["mandatory"]
    assert result["finish_refusals"][-1]["what"].startswith("dispatch: the calls left cannot pay")
    assert "mandatory stage(s)" in result["finish_refusals"][-1]["detail"]
    assert (replay.project / "README.md").read_text() == FILES["README.md"], "nothing was written"
    assert "t2's route could not be paid" in replay.result.error and "forfeited" in replay.result.error
    assert not replay.artifact_texts("terminal-route")


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("calls", [14, 7])
def test_parallel_last_wave_closes_factually_and_its_route_is_admitted_whole_or_not_started(tmp_path, monkeypatch,
                                                                                            calls):
    """Both children are the last wave and close factually (no close-out
    on either route). The batch's selected route is its known stages, four
    calls: each child's lead and review; the revision, the rechecks and a
    post-merge recheck exist only once a finding does and are admitted
    then (admission/2). With the two orchestrator calls made and the two
    finishing judgments held, eight calls admit it exactly; fourteen pay
    the whole job. t2's finding is left open in its copy (UNRESOLVED there,
    after its two fix rounds, each admitted on its own before it starts)
    and rechecked once on the merged source, which resolves it; the goal
    question and audit follow. With seven calls the wave is one short and
    is not started before any child's call: both tasks are NOT RUN with
    the route's shortfall named, nothing is written, and the run ends on
    the uncovered requirements."""
    def recheck(call, replay):
        return "RESOLVED" if "now merged" in call.prompt else "UNRESOLVED: no example yet"

    script = _script([BATCH])
    script.overrides["recheck"] = recheck
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_limits(calls),
                   record_complete=False)
    assert not replay.of("closeout"), "no model close-out in the last wave"
    result = H.result_json(replay)
    admission = [a for a in result["plan_admissions"] if a["task"] == "batch:t1,t2"][-1]
    assert "close-out(reserve)" not in admission["stages"]
    assert not any("recheck" in s or "revision" in s for s in admission["stages"]), admission["stages"]
    assert len(admission["stages"]) == 4
    if calls == 14:
        assert admission["scope"] == "full" and admission["feasible"]
        assert sorted(json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")) == ["t1", "t2"]
        batch = result["parallel_batches"][0]
        assert batch["post_merge_rechecks"][0]["rechecked"][0]["verdict"] == "resolved"
        assert [a["scope"] for a in result["plan_admissions"] if a["task"] == "t2"] == ["fix-cycle", "fix-cycle"]
        assert replay.result.completed, replay.result.error
        assert _roles(replay)[-2:] == [("run", "orchestrator"), ("audit", "auditor")]
        assert _budget(replay)["reserved_attempts"] == 13
    else:
        # In the last wave the batch's route is its known stages, so the
        # shortfall is found at the mandatory admission and no further
        # admission is asked.
        assert admission["scope"] == "mandatory"
        assert not admission["feasible"] and admission["short"] == {"calls": 1}
        assert not [c for c in replay.calls if c.task in ("t1", "t2")], "no child call was made"
        assert [n["task"] for n in result["not_started"]] == ["t1", "t2"]
        assert result["finish_refusals"][0]["what"].startswith("batch dispatch:")
        assert "short by calls 1" in result["finish_refusals"][0]["detail"], result["finish_refusals"][0]
        assert (replay.project / "app.py").read_text() == FILES["app.py"], "nothing was written"
        assert not replay.result.completed and not replay.of("recheck")


@pytest.mark.requirements_ledger
def test_a_resumed_episode_holds_the_same_finish_from_the_carried_spend(tmp_path, monkeypatch):
    """Interrupted after t1 at a quiescent point, then resumed: the second
    episode carries the job's spend and holds the goal question and the
    audit again, once, from the same allowance. Twelve calls in all, as
    the uninterrupted job: ordinary work never takes the finish's two."""
    _interrupt_after(monkeypatch, 1)
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=2,
                   limits=_limits(12), record_complete=False)
    first = Path(replay.result.run_dir)
    assert "KeyboardInterrupt" in replay.result.error
    held = json.loads((first / "budget.json").read_text())["finish_protection"]
    assert held["pending"] == ["goal", "audit"] and held["started"] == []
    second = Path(_episode(replay, resume_from=first.name, limits=_limits(12)).run_dir)
    budget = json.loads((second / "budget.json").read_text())
    assert budget["finish_protection"]["started"] == ["goal", "audit"] and budget["finish_protection"]["pending"] == []
    assert budget["reserved_attempts"] <= 12 and budget["stop_reason"] == ""
    assert json.loads((second / "result.json").read_text())["completed"]


# -- a refused first editor is NOT RUN, and the finish the gates allow follows ----
# (root reconciliation of R23, 6208197375). Every replayed call reports 110
# tokens. With a 1,000 reserve and a 3,000 per-call ceiling, an editing call
# needs 3,000 + 2,000 held, so it starts only while 5,000 tokens remain; a
# review, or the goal question beside the audit's hold, needs 3,000 or 2,000.

ONE = "REQUIREMENTS:\nR1: add(a, b) returns the sum\n" + _simple("app.py", "Implement add in app.py.", "R1")
MORE = _simple("app.py", "Add a docstring to add in app.py.", "R1")


@pytest.mark.requirements_ledger
def test_a_selected_repair_carries_the_known_nonfinal_closeout(tmp_path, monkeypatch):
    """Root's control on fef1e27, the default bound (20 calls, 2,000,000
    tokens, 3 slots, 200,000 reserve, 500,000 ceiling): a planner of
    86,663 and a requirements review of 39,473 (126,136 intake), a lead
    and a reviewer of 350,000 each leave 773,864 after the 400,000 finish
    hold. The review's correction (revision 490,000, recheck 190,000:
    700,000 in exposure classes) was admitted without t1's known 200,000
    model close-out, ran and resolved the finding, and the job stopped
    FinishReserved at the close-out: goal and audit never asked. Every
    selected repair now carries the task's known remainder, so the
    correction (900,000, the measured gate wall counted) is refused before
    its editor writes, no revision runs, the finding stays open, the
    close-out, the audit and the goal question all run, and the run ends
    incomplete on the uncovered requirement, never on the finish."""
    monkeypatch.setattr(H, "USAGE", dict(input_tokens=100, output_tokens=10))

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
    plan = "REQUIREMENTS:\nR1: add(a, b) returns the sum\n" + _simple("app.py", "Implement add in app.py.", "R1")
    script = Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                    collaborator=lambda c, r: ("BLOCKING: negative-number behavior needs an explicit regression "
                                               "test.\nR1: MET - app.py"),
                    auditor=lambda c, r: "R1: MET - app.py", **{"requirements-review": lambda c, r: "COMPLETE"})

    def respond(call, replay):
        role = call.role
        total = (86_663 if len(replay.calls) == 1 else 39_473 if role.startswith("requirements-review")
                 else 350_000 if role.startswith(("lead", "collaborator")) else 490_000 if role.startswith("revision")
                 else 190_000 if role.startswith("recheck") else 110)
        H.USAGE = dict(input_tokens=total - 10, output_tokens=10)
        return script(call, replay)
    replay = H.run(tmp_path, monkeypatch, respond, files=FILES, max_tasks=3, record_complete=False,
                   limits=RunLimits(max_calls=20, max_reported_tokens=2_000_000, wall_seconds=1800,
                                    max_concurrent_workers=2, reserve_tokens_per_call=200_000,
                                    max_tokens_per_call=500_000))
    result = H.result_json(replay)
    roles = [(c.task, c.role) for c in replay.calls]
    assert not replay.of("revision") and not replay.of("recheck"), roles
    cycle = [a for a in result["plan_admissions"] if a["scope"] == "fix-cycle"]
    assert [a["feasible"] for a in cycle] == [False]
    assert cycle[0]["stages"] == ["fix revision(editing/write)", "recheck by claude:opus(reserve)",
                                  "close-out(reserve)"]
    assert cycle[0]["short"] == {"tokens": 900_000 - 773_864}, cycle[0]
    refusal = [r for r in result["finish_refusals"] if r["what"] == "fix round"]
    assert refusal and "the correction the review selected cannot be paid whole" in refusal[0]["detail"]
    assert replay.of("closeout") and replay.of("auditor"), roles
    audit = replay.of("auditor")[-1].prompt
    assert "so it was not started" not in audit, "a refused correction is not a task not started"
    assert ("Plan admission for t1: a correction its review selected could not be paid" in audit
            and "the task had started" in audit and "stay open as written" in audit)
    assert "task slots usable 2 of 3 (1 spent)" in audit
    assert roles[:6] == [("run", "orchestrator"), ("plan", "requirements-review"), ("t1", "lead"),
                         ("t1", "collaborator"), ("t1", "closeout"), ("run", "orchestrator")], roles
    assert not replay.result.completed, "the finding is open and R1 uncovered: not complete"
    assert "FinishReserved" not in (replay.result.error or ""), replay.result.error
    assert "R1" in replay.result.error and "Work preserved" in replay.result.error, replay.result.error
    budget = _budget(replay)
    assert sorted(budget["finish_protection"]["started"]) == ["audit", "goal"], "both finishing judgments ran"
    assert budget["finish_protection"]["refusals"] == [], "nothing was refused at the budget"
    assert budget["stop_reason"] == ""


def _tight(total):
    return RunLimits(max_calls=40, max_reported_tokens=total, wall_seconds=600, max_concurrent_workers=2,
                     reserve_tokens_per_call=1_000, max_tokens_per_call=3_000)


@pytest.mark.requirements_ledger
def test_a_route_the_finish_cannot_afford_whole_runs_on_its_terminal_route_and_the_job_ends_after_it(tmp_path,
                                                                                                    monkeypatch):
    """7,400 tokens over three slots. t1's ordinary route (its lead at the
    3,000 ceiling, its review and its close-out at the 1,000 reserve:
    5,000) fits the 5,180 left after the two 1,000 holds and the 220 of
    intake. After t1 and the planning call for t2, 4,740 remain after the
    holds: t2's mandatory stages (4,000) fit, its ordinary route (5,000,
    not the last slot) does not, and its terminal route (admission/3: the
    same lead, reviewer and gates, closed factually) does. The job
    commits to end after t2 before t2's first call: recorded with the
    actual slots used and the one relinquished, the planner was shown the
    allowance the admission charges, t2 edits within its scope, is
    reviewed, closes factually, nothing is planned after it, the goal
    question and the audit run, and the run completes. The stop never
    names the cap. Before admission/3 this allowance left t2 NOT RUN."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": FIXED.replace("def add(a, b):", 'def add(a, b):\n    """Return a + b."""')})
        return 'Added the docstring.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
    script = _script([ONE, MORE], t2_review="NO FINDINGS\nR1: MET - app.py")
    script.overrides["lead"] = lead
    script.overrides["auditor"] = lambda call, replay: "R1: MET - app.py"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_tight(7_400),
                   record_complete=False)
    assert [(c.task, c.role) for c in replay.calls if c.task == "t2"] == [("t2", "lead"), ("t2", "collaborator")]
    assert len(replay.of("orchestrator")) == 3, "plan, plan t2, goal question: no planning after the commitment"
    assert replay.of("auditor"), "the audit runs after the committed task"
    result = H.result_json(replay)
    rows = [(a["scope"], a["feasible"], a["short"]) for a in result["plan_admissions"] if a["task"] == "t2"]
    assert rows == [("mandatory", True, {}), ("full", False, {"tokens": 5_000 - 4_740}), ("terminal", True, {})]
    terminal = result["terminal_route"]
    assert (terminal["task"], terminal["slots_used"], terminal["relinquished"], terminal["max_tasks"]) == ("t2", 2, 1, 3)
    assert "short by tokens 260" in terminal["reason"] and "2 call(s), 4,000 tokens" in terminal["selected"]
    assert json.loads(replay.artifact_texts("terminal-route")[0]) == terminal
    assert result["not_started"] == [] and [c.task for c in replay.of("closeout")] == ["t1"]
    assert [json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")] == ["t2"]
    assert '"""Return a + b."""' in (replay.project / "app.py").read_text(), "t2 wrote within its scope"
    planning = replay.of("orchestrator")[1].prompt
    assert ("Allowance after the held finish (2 stage(s) held): 33 ordinary call(s), 4,850 tokens" in planning
            and "an editing call 3,000 tokens, any other call 1,000" in planning)
    assert "A requirement is delivered only with its executed proof in the same task" in planning
    assert "Size the implementation and its executed proof together" in planning
    assert replay.result.completed, replay.result.error
    assert "the task cap (3) was reached" not in (replay.result.error or "")
    assert result["requirements"]["audits"][-1]["verdicts"]["R1"]["met"] is True
    # The audit is told what was chosen, not that the task was not started
    # (Astra A1 on 0ae5bb6): the ordinary refusal, the admitted terminal
    # route, the spent and relinquished slots, and no usable slot.
    audit = replay.of("auditor")[-1].prompt
    assert "so it was not started" not in audit
    assert ("Plan admission for t2: its ordinary route could not be paid" in audit
            and "its terminal route (the same stages, closed factually) was admitted" in audit
            and "2 of 3 slot(s) spent, 1 relinquished" in audit and "Admission is not execution" in audit)
    assert "task slots usable 0 of 3 (2 spent, 1 relinquished by the terminal commitment)" in audit
    assert result["obligations"]["capacity"]["slots_left"] == 0
    assert (result["obligations"]["capacity"]["slots_spent"], result["obligations"]["capacity"]["slots_relinquished"]) == (2, 1)
    budget = _budget(replay)
    assert budget["finish_protection"]["refusals"] == [] and budget["stop_reason"] == ""
    assert budget["finish_protection"]["started"] == ["goal", "audit"]


@pytest.mark.requirements_ledger
def test_a_mandatory_deficit_has_no_terminal_candidate_and_nothing_is_written(tmp_path, monkeypatch):
    """6,100 tokens over three slots: 3,880 are left after the two holds
    and the intake, and t1's mandatory stages (its lead at the 3,000
    ceiling and its review at the 1,000 reserve) need 4,000. The terminal
    route drops only the close-out, so none is tried: t1 is refused at
    its mandatory admission, nothing is written, t1 is NOT RUN, the slots
    are forfeited and the stop says so."""
    script = _script([ONE, MORE], t2_review="NO FINDINGS\nR1: MET - app.py")
    script.overrides["auditor"] = lambda call, replay: "R1: MET - app.py"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_tight(6_100),
                   record_complete=False)
    assert not [c for c in replay.calls if c.task in ("t1", "t2")], "no task call was made"
    result = H.result_json(replay)
    assert [n["task"] for n in result["not_started"]] == ["t1"] and result["terminal_route"] is None
    admissions = [a for a in result["plan_admissions"] if a["task"] == "t1"]
    assert [a["scope"] for a in admissions] == ["mandatory"] and admissions[0]["short"] == {"tokens": 4_000 - 3_880}
    assert (replay.project / "app.py").read_text() == FILES["app.py"], "nothing was written"
    assert not replay.result.completed and "t1's route could not be paid" in replay.result.error
    assert "forfeited" in replay.result.error and not replay.artifact_texts("terminal-route")


@pytest.mark.requirements_ledger
def test_a_terminal_tasks_capped_checked_source_meets_the_same_bounded_review_as_the_last_slot(tmp_path, monkeypatch):
    """Astra A2 on 0ae5bb6: 7,000 tokens over three slots, t1's ordinary
    route (5,000) misses the 4,780 left by 220 and its terminal route is
    admitted, so the job commits to end after t1. Its lead writes the
    correct source inside its scope and stops at its turn cap. No later
    planning call can continue it, exactly as in the numeric last slot,
    so the capped source takes the same bounded handoff: the harness's
    check passes on that exact source, one cross-vendor reviewer reads
    it, the goal question and the audit run, and the run completes with
    the cap recorded as a non-blocking fact. Before the shared predicate
    the terminal task was left capped with no reviewer, goal or audit."""
    from quadratus.config import Settings

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.grok_ok("Wrote add; no final answer.\nPROVES: P1 - tests/test_app.py::test_add",
                         stop="cancelled", num_turns=14)
    script = _script([ONE], t2_review="NO FINDINGS\nR1: MET - app.py")
    script.overrides["lead"] = lead
    script.overrides["auditor"] = lambda call, replay: "R1: MET - app.py"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_tight(7_000),
                   record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    result = H.result_json(replay)
    assert result["terminal_route"]["task"] == "t1" and result["terminal_route"]["relinquished"] == 2
    assert [(c.task, c.role) for c in replay.calls][2:] == [("t1", "lead"), ("t1", "collaborator"),
                                                            ("run", "orchestrator"), ("audit", "auditor")]
    assert replay.result.completed, replay.result.error
    t1 = next(t for t in result["workflow"]["tasks"] if t["task_id"] == "t1")
    assert t1["edges"].get("review") is True
    assert any(f["kind"] == "cap" and (f["recovered"] or not f["terminal"]) for f in t1["facts"]), t1["facts"]
    assert not replay.of("closeout") and [json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")] == ["t1"]

    # The same commitment with a capped source whose check fails: no
    # handoff, no reviewer, the task stays capped and the run incomplete.
    (tmp_path / "broken").mkdir()

    def broken(call, replay):
        H.write(call, {"app.py": "def add(a, b):\n    return a - b\n"})
        return H.grok_ok("Wrote add; no final answer.\nPROVES: P1 - tests/test_app.py::test_add",
                         stop="cancelled", num_turns=14)
    script = _script([ONE], t2_review="NO FINDINGS\nR1: MET - app.py")
    script.overrides["lead"] = broken
    held = H.run(tmp_path / "broken", monkeypatch, script, files=FILES, max_tasks=3, limits=_tight(7_000),
                 record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    assert H.result_json(held)["terminal_route"]["task"] == "t1"
    assert [(c.task, c.role) for c in held.calls][2:] == [("t1", "lead")], "no reviewer for a failing capped source"
    assert not held.result.completed and held.result.error.startswith("GoalUnconfirmedAtCap")
    assert "t1 not continued" in held.result.error and "committed to end after t1" in held.result.error


@pytest.mark.requirements_ledger
def test_a_terminal_route_after_a_failed_predecessor_still_ends_incomplete(tmp_path, monkeypatch):
    """7,400 tokens over three slots: t1's ordinary route fits (5,180 left
    after the holds for its 5,000), its lead stops at its turn cap, then
    t2's ordinary route (5,000 against 4,960) does not fit and its
    terminal route does, so the job commits to end after t2. t2 runs
    whole (lead, blocking review, the correction it selects, recheck
    resolved) and closes factually; t1's capped work is still debt, so
    the cap's own decision stops before the goal question, no audit runs,
    and the stop names the commitment and the uncontinued task. A
    terminal route never clears a predecessor's debt."""
    from quadratus.config import Settings

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lead
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, limits=_tight(7_400),
                   record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    result = H.result_json(replay)
    assert result["not_started"] == [] and result["terminal_route"]["task"] == "t2"
    assert [c.role for c in replay.calls if c.task == "t2"] == ["lead", "collaborator", "revision", "recheck"]
    assert [a["scope"] for a in result["plan_admissions"] if a["task"] == "t2"] == ["mandatory", "full", "terminal",
                                                                                     "fix-cycle"]
    assert len(replay.of("orchestrator")) == 2 and not replay.of("auditor"), "no goal question, no audit"
    assert replay.result.error.startswith("GoalUnconfirmedAtCap") and "t1 not continued" in replay.result.error
    assert "committed to end after t2" in replay.result.error and "relinquishing 1 of 3" in replay.result.error
    assert not replay.result.completed
    assert _budget(replay)["finish_protection"]["pending"] == ["goal", "audit"]


@pytest.mark.requirements_ledger
def test_a_refusal_after_the_task_has_edited_still_stops_the_run(tmp_path, monkeypatch):
    """11,400 tokens: t2's selected route (8,000: its lead, review,
    revision and recheck; the last slot has no model close-out) fits the
    8,740 left at dispatch after the holds. Its lead writes README.md and
    asks to FETCH twice, each turn reporting 2,900 tokens (under the 3,000
    ceiling): the admitted route pays the second turn (5,840 left), and the
    third, exposed at 3,000 against the 2,940 now left, is refused. Work
    was written, so this is not a task that never started: the run stops on
    the refusal as before, with the edit kept and nothing recorded NOT RUN.
    An exactly admitted route cannot be short after one turn, which is what
    the admission is for; it is short once the turns have spent it."""
    import json as _json
    import uuid as _uuid

    heavy_usage = {"input_tokens": 2_890, "output_tokens": 10}

    def heavy(call, text):
        """The seat's own envelope, reporting 2,900 tokens for this turn."""
        if call.vendor == "claude":
            return _json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                                "result": text, "stop_reason": "end_turn", "session_id": str(_uuid.uuid4()),
                                "usage": heavy_usage})
        thread = str(_uuid.uuid4())
        events = [{"type": "thread.started", "thread_id": thread}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": heavy_usage}]
        return "\n".join(_json.dumps(e) for e in events)

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README})
        return heavy(call, "FETCH: no-such-artifact")
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lead
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_tight(11_400),
                   record_complete=False)
    assert len([c for c in replay.calls if c.task == "t2"]) == 2
    assert replay.result.error.startswith("FinishReserved") and H.result_json(replay)["not_started"] == []
    assert (replay.project / "README.md").read_text() == README
    assert not replay.of("auditor")


# -- a serial task the run is certain to stop after closes factually ---------------
# (R24 on 54d729: t1 closed with open review findings, its model close-out was
# written, and the run stopped FindingsOpen before any planning call read it.)

_BLOCKING = "BLOCKING: add is not shown for negative numbers; example: add(-1, -2) should be -3\nR1: MET - app.py"
#: A recheck whose verdict is glued to narration: the strict whole-reply
#: verdict reads it as no verdict, exactly as before this change.
_GLUED = "I'll re-check only the blocking finding against the revised source.RESOLVED"


def test_a_task_closing_into_a_findings_stop_gets_no_model_close_out(tmp_path, monkeypatch):
    """No ledger, so open review findings cannot be held as debt: t1 closes
    with its finding open and the run stops FindingsOpen. Old: a model
    close-out ran first that nothing read. Now the harness records it; the
    review, the unaccepted recheck and the stop are unchanged."""
    script = _script([PLAN_1, PLAN_2], recheck=_GLUED)
    script.overrides["collaborator"] = lambda call, replay: _BLOCKING
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.of("recheck"), "the review and its recheck still run"
    assert not replay.of("closeout"), "no model close-out before a certain stop"
    assert len(replay.of("orchestrator")) == 1 and not [c for c in replay.calls if c.task == "t2"]
    assert replay.result.error.startswith("FindingsOpen") and not replay.result.completed
    record = json.loads(replay.artifact_texts("factual-closeout")[0])
    assert record["task"] == "t1" and "stops on its open findings" in record["why"]
    assert record["unresolved"] or record["open"], "the open finding is on the record"


def test_a_task_closing_into_a_failed_check_stop_gets_no_model_close_out(tmp_path, monkeypatch):
    """No ledger, so a failed check cannot become debt: its finding stops
    the run after t1. The harness records the close-out."""
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lambda call, replay: (H.write(call, {"app.py": "def add(a, b):\n    return a - b\n"})
                                                     or 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]')
    script.overrides["collaborator"] = lambda call, replay: "NO FINDINGS\nR1: MET - app.py"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert not H.result_json(replay)["checks"][-1]["passed"]
    assert not replay.of("closeout") and len(replay.of("orchestrator")) == 1
    record = json.loads(replay.artifact_texts("factual-closeout")[0])
    assert "stops on its open findings" in record["why"] and "last check FAILED" in record["check"]


@pytest.mark.requirements_ledger
def test_a_finding_the_run_holds_as_debt_keeps_the_model_close_out(tmp_path, monkeypatch):
    """Ledger on, COVERS, a configured check and a slot left: t1's open
    review finding is held as debt and the orchestrator plans again, so it
    reads t1's model close-out. That close-out still runs."""
    script = _script([PLAN_1, PLAN_2], recheck="UNRESOLVED: negative sums are not shown")
    script.overrides["collaborator"] = lambda call, replay: (
        _BLOCKING if call.task == "t1" else "NO FINDINGS\nR2: MET - README.md")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert [c.task for c in replay.of("closeout")] == ["t1"]
    assert len(replay.of("orchestrator")) >= 2, "the run planned again after t1"
    assert not [json.loads(t) for t in replay.artifact_texts("factual-closeout") if json.loads(t)["task"] == "t1"]
