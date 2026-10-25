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
    assert replay.of("closeout") == []
    assert replay.result.completed, replay.result.error
    budget = _budget(replay)
    assert budget["reserved_attempts"] == 11 and budget["finish_protection"]["started"] == ["goal", "audit"]
    assert budget["finish_protection"]["refusals"] == []
    record = json.loads(replay.artifact_texts("factual-closeout")[0])
    assert record["task"] == "t2" and "last dispatched wave" in record["why"]
    assert record["reviews"] == [["openai:sol", "reviewed: blocking findings"],
                                 ["openai:sol", "recheck: RFT2-1 resolved"]]
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
    """9 calls over two slots, no close-out call anywhere (2026-10-21):
    after t1 (lead, review) and the planning call for t2, two calls are
    left after the held goal question and audit. t2's route (its lead and
    its review) is two and is admitted whole; there is no terminal route
    to fall to, since no stage can be dropped. t2's review blocks; the
    correction it selects (revision, recheck) cannot be paid and is
    refused whole, so the finding stays open as written, t2 (the last
    slot) closes on the harness's record with it, and the run ends
    FindingsOpen with the goal question and the audit never asked."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=2,
                   limits=_limits(9), record_complete=False)
    assert _roles(replay)[-2:] == [("t2", "lead"), ("t2", "collaborator")]
    assert not replay.of("auditor") and not replay.result.completed
    result = H.result_json(replay)
    rows = [(a["scope"], a["feasible"], a["short"]) for a in result["plan_admissions"] if a["task"] == "t2"]
    assert rows == [("mandatory", True, {}), ("full", True, {}), ("fix-cycle", False, {"calls": 2})]
    assert result["terminal_route"] is None and not replay.artifact_texts("terminal-route")
    assert result["not_started"] == [] and replay.of("closeout") == []
    records = {json.loads(t)["task"]: json.loads(t) for t in replay.artifact_texts("factual-closeout")}
    assert sorted(records) == ["t1", "t2"] and "last dispatched wave" in records["t2"]["why"]
    assert result["finish_refusals"][-1]["what"] == "fix round"
    assert replay.result.error.startswith("FindingsOpen"), replay.result.error
    assert (replay.project / "README.md").read_text() != FILES["README.md"], "t2's lead wrote"
    budget = _budget(replay)
    assert budget["finish_protection"]["pending"] == ["goal", "audit"] and budget["finish_protection"]["refusals"] == []
    assert budget["stop_reason"] == ""


@pytest.mark.requirements_ledger
def test_serial_a_mandatory_deficit_is_refused_before_any_write_with_no_terminal_candidate(tmp_path, monkeypatch):
    """8 calls over three slots: one call is left after the held finish
    when t2 is planned, and t2's known stages are two. The refusal is the
    mandatory one, nothing of t2 runs or is written, t2 is NOT RUN, the
    remaining slots are forfeited and the stop says so."""
    replay = H.run(tmp_path, monkeypatch, _script([PLAN_1, PLAN_2]), files=FILES, max_tasks=3,
                   limits=_limits(8), record_complete=False)
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
def test_a_selected_repair_carries_no_close_out_and_the_finish_still_runs(tmp_path, monkeypatch):
    """Root's control on fef1e27 (20 calls, 3 slots, 200,000 reserve,
    500,000 ceiling), at 2,200,000 tokens: a planner of 86,663 and a
    requirements review of 39,473 (126,136 intake), a lead and a reviewer
    of 350,000 each leave 973,864 after the 400,000 finish hold. The
    correction the review selects (revision 490,000, recheck 190,000:
    700,000 in exposure classes) carries no close-out any more
    (2026-10-21: the record is the harness's, not a call), so it is
    admitted whole, the revision runs, the recheck resolves the finding,
    t1 closes on the harness's record, the next planning call (200,000
    exposure against the 293,864 then left above the hold) is admitted,
    and the goal question and the audit both run: the finish keeps its
    hold throughout and refuses nothing. (At the old 2,000,000 the same
    route leaves 93,864 above the hold, and the planning call, not the
    finish, is what the hold refuses.)"""
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
                   limits=RunLimits(max_calls=20, max_reported_tokens=2_200_000, wall_seconds=1800,
                                    max_concurrent_workers=2, reserve_tokens_per_call=200_000,
                                    max_tokens_per_call=500_000))
    result = H.result_json(replay)
    roles = [(c.task, c.role) for c in replay.calls]
    assert len(replay.of("revision")) == 1 and len(replay.of("recheck")) == 1, roles
    cycle = [a for a in result["plan_admissions"] if a["scope"] == "fix-cycle"]
    assert [a["feasible"] for a in cycle] == [True]
    assert cycle[0]["stages"] == ["fix revision(editing/write)", "recheck by claude:opus(reserve)"]
    assert "close-out" not in json.dumps(result["plan_admissions"])
    assert [r for r in result["finish_refusals"] if r["what"] == "fix round"] == []
    assert not replay.of("closeout") and replay.of("auditor"), roles
    assert roles[:7] == [("run", "orchestrator"), ("plan", "requirements-review"), ("t1", "lead"),
                         ("t1", "collaborator"), ("t1", "revision"), ("t1", "recheck"), ("run", "orchestrator")], roles
    assert replay.artifact_texts("factual-closeout"), "t1 closed on the harness's record"
    assert "FinishReserved" not in (replay.result.error or ""), replay.result.error
    assert replay.result.completed, replay.result.error
    assert replay.of("auditor"), "the audit ran"
    budget = _budget(replay)
    assert "audit" in budget["finish_protection"]["started"], "the finishing judgment ran"
    assert budget["finish_protection"]["refusals"] == [], "nothing was refused at the budget"
    assert budget["stop_reason"] == ""


def _tight(total):
    return RunLimits(max_calls=40, max_reported_tokens=total, wall_seconds=600, max_concurrent_workers=2,
                     reserve_tokens_per_call=1_000, max_tokens_per_call=3_000)


@pytest.mark.requirements_ledger
def test_a_route_without_a_close_out_is_paid_whole_and_the_job_runs_to_its_finish(tmp_path, monkeypatch):
    """7,400 tokens over three slots, no close-out call anywhere
    (2026-10-21). t1's route (its lead at the 3,000 ceiling and its review
    at the 1,000 reserve: 4,000) fits the 5,180 left after the two 1,000
    holds and the 220 of intake. After t1 and the planning call for t2,
    t2's route (4,000) fits what remains and is admitted whole; nothing is
    relinquished and no terminal route is recorded. t2 edits within its
    scope, is reviewed, closes on the harness's record, the planner stops,
    the goal question and the audit run, and the run completes. Before
    2026-10-21 the same allowance paid t2 only on its terminal route."""
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
    assert replay.of("auditor"), "the audit runs after the last task"
    result = H.result_json(replay)
    rows = [(a["scope"], a["feasible"], a["short"]) for a in result["plan_admissions"] if a["task"] == "t2"]
    assert rows == [("mandatory", True, {}), ("full", True, {})]
    assert result["terminal_route"] is None and not replay.artifact_texts("terminal-route")
    assert "close-out" not in json.dumps(result["plan_admissions"])
    assert result["not_started"] == [] and replay.of("closeout") == []
    assert sorted(json.loads(t)["task"] for t in replay.artifact_texts("factual-closeout")) == ["t1", "t2"]
    assert '"""Return a + b."""' in (replay.project / "app.py").read_text(), "t2 wrote within its scope"
    planning = replay.of("orchestrator")[1].prompt
    assert ("Allowance after the held finish (2 stage(s) held):" in planning
            and "an editing call 3,000 tokens, any other call 1,000" in planning)
    assert "A requirement is delivered only with its executed proof in the same task" in planning
    assert "Size the implementation and its executed proof together" in planning
    assert replay.result.completed, replay.result.error
    assert "the task cap (3) was reached" not in (replay.result.error or "")
    assert result["requirements"]["audits"][-1]["verdicts"]["R1"]["met"] is True
    audit = replay.of("auditor")[-1].prompt
    assert "so it was not started" not in audit and "Plan admission for" not in audit
    assert result["obligations"]["capacity"]["slots_relinquished"] in (None, 0)
    budget = _budget(replay)
    assert budget["finish_protection"]["refusals"] == [] and budget["stop_reason"] == ""
    assert "audit" in budget["finish_protection"]["started"]


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
def test_a_last_slot_tasks_capped_checked_source_meets_the_bounded_review(tmp_path, monkeypatch):
    """Astra A2 on 0ae5bb6, on the last slot (the terminal route it was
    written for cannot arise since 2026-10-21: with no close-out call
    there is no stage to drop, so the same bounded handoff is the last
    slot's). t1's lead writes the correct source inside its scope and
    stops at its turn cap. No later planning call can continue it, so the
    capped source takes the bounded handoff: the harness's check passes on
    that exact source, one cross-vendor reviewer reads it, the goal
    question and the audit run, and the run completes with the cap
    recorded as a non-blocking fact."""
    from quadratus.config import Settings

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return H.grok_ok("Wrote add; no final answer.\nPROVES: P1 - tests/test_app.py::test_add",
                         stop="cancelled", num_turns=14)
    script = _script([ONE], t2_review="NO FINDINGS\nR1: MET - app.py")
    script.overrides["lead"] = lead
    script.overrides["auditor"] = lambda call, replay: "R1: MET - app.py"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, limits=_tight(7_000),
                   record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    result = H.result_json(replay)
    assert result["terminal_route"] is None
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
    held = H.run(tmp_path / "broken", monkeypatch, script, files=FILES, max_tasks=1, limits=_tight(7_000),
                 record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    assert H.result_json(held)["terminal_route"] is None
    assert [(c.task, c.role) for c in held.calls][2:] == [("t1", "lead")], "no reviewer for a failing capped source"
    assert not held.result.completed and held.result.error.startswith("GoalUnconfirmedAtCap")
    assert "t1 not continued" in held.result.error


@pytest.mark.requirements_ledger
def test_a_last_slot_after_a_failed_predecessor_still_ends_incomplete(tmp_path, monkeypatch):
    """7,400 tokens over two slots: t1's route fits, its lead stops at its
    turn cap, then t2 (the last slot) runs whole (lead, blocking review,
    the correction it selects, recheck resolved) and closes on the
    harness's record; t1's capped work is still debt, so the cap's own
    decision stops before the goal question, no audit runs, and the stop
    names the uncontinued task. A later task never clears a predecessor's
    debt. (The terminal route this was written on cannot arise since
    2026-10-21: no close-out call exists to drop.)"""
    from quadratus.config import Settings

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'
    script = _script([PLAN_1, PLAN_2])
    script.overrides["lead"] = lead
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_tight(7_400),
                   record_complete=False, settings=Settings(backend="cli", lead_max_turns=14))
    result = H.result_json(replay)
    assert result["not_started"] == [] and result["terminal_route"] is None
    assert [c.role for c in replay.calls if c.task == "t2"] == ["lead", "collaborator", "revision", "recheck"]
    assert [a["scope"] for a in result["plan_admissions"] if a["task"] == "t2"] == ["mandatory", "full", "fix-cycle"]
    assert len(replay.of("orchestrator")) == 2 and not replay.of("auditor"), "no goal question, no audit"
    assert replay.result.error.startswith("GoalUnconfirmedAtCap") and "t1 not continued" in replay.result.error
    assert not replay.result.completed
    assert _budget(replay)["finish_protection"]["pending"] == ["goal", "audit"]


@pytest.mark.requirements_ledger
def test_a_refusal_after_the_task_has_edited_still_stops_the_run(tmp_path, monkeypatch):
    """10,400 tokens (11,400 before 2026-10-21, when t1 still spent a
    1,000 close-out): t2's selected route (8,000: its lead, review,
    revision and recheck) fits the 8,740 left at dispatch after the holds. Its lead writes README.md and
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
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, limits=_tight(10_400),
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
def test_a_finding_the_run_holds_as_debt_still_reaches_the_planner_through_the_record(tmp_path, monkeypatch):
    """Ledger on, COVERS, a configured check and a slot left: t1's open
    review finding is held as debt and the orchestrator plans again, and
    reads t1's harness record, which carries the unresolved finding (no
    model close-out since 2026-10-21)."""
    script = _script([PLAN_1, PLAN_2], recheck="UNRESOLVED: negative sums are not shown")
    script.overrides["collaborator"] = lambda call, replay: (
        _BLOCKING if call.task == "t1" else "NO FINDINGS\nR2: MET - README.md")
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    assert replay.of("closeout") == []
    assert len(replay.of("orchestrator")) >= 2, "the run planned again after t1"
    (t1,) = [json.loads(t) for t in replay.artifact_texts("factual-closeout") if json.loads(t)["task"] == "t1"]
    assert "negative" in " ".join(t1["unresolved"] + t1["open"]), t1
    assert "Recorded by the harness" in replay.of("orchestrator")[1].prompt
