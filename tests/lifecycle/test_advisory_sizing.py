"""Size is advisory (operator directive 2026-10-20).

Until this batch a change past 1.5 x the planner's max_lines failed the task
(2026-09-28), bought one trim call first (Step 2 r11, r13, R16), and a plan
sized over 100 lines was refused. Priority is now correctness and complete
requirements, the tests that prove them, then conciseness: the harness
measures every change and shows the size to the lead, the reviewers and the
planner, and stops nothing for size alone. Paths, original tests, budgets,
roles, the independent review, goal confirmation and the final audit are as
they were. Whole-controller replays below the vendor CLI, the real gate
running the project's tests.
"""

import json

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, Script
from tests.lifecycle.test_step2_recovery import _FINDING, _R1, _TYPED, DECL_SIMPLE, _plan, _simple

pytestmark = pytest.mark.requirements_ledger

#: Well past the old stop (1.5 x 40 = 60): ~190 changed lines inside the
#: task's one permitted source path, every one of them in scope.
LARGE = _TYPED + "".join(f"\n\ndef helper_{i}(x):\n    return x + {i}\n" for i in range(60))
DECLARED = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_SIMPLE + "\nCOVERS: R1"
PROVES = 'PROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'


def _writers(replay):
    return [c for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix")]


def _facts(replay, task="t1"):
    return [f for t in replay.workflow["tasks"] if t["task_id"] == task for f in t["facts"]]


def _run(tmp_path, monkeypatch, *, plan=DECLARED, lead=None, revision=None, collaborator=None, recheck=None,
         max_tasks=1, settings=None, auditor=None):
    def default_lead(call, replay):
        H.write(call, {"app.py": LARGE})
        return "Implemented add.\n" + PROVES
    return H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(plan), lead=lead or default_lead,
        revision=revision or (lambda c, r: "Revised.\n" + PROVES),
        collaborator=collaborator or (lambda c, r: "NO FINDINGS\nR1: MET - app.py:5"),
        recheck=recheck or (lambda c, r: "RESOLVED"),
        auditor=auditor or (lambda c, r: "R1: MET - app.py:5"),
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=max_tasks, record_complete=False,
        settings=settings or Settings(backend="cli"))


# -- positive: size past the estimate is measured, shown and reviewed, never stopped ----------

def test_a_draft_well_past_its_estimate_is_checked_reviewed_audited_and_completes(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch)
    assert replay.result.completed, replay.result.error
    result = H.result_json(replay)
    assert result["failed_tasks"] == [] and [c.role for c in _writers(replay)] == ["lead"]
    report = result["scope_reports"][-1]
    assert report["oversized"] and report["within_scope"] and report["changed_lines"] > 150
    assert report["max_lines"] == 40, "the estimate is kept as telemetry"
    assert not any("Bring the change down" in c.prompt for c in replay.calls), "no trim call"
    assert not any(f["kind"] == "failed" for f in _facts(replay)), "a size overrun is not a failure"
    [review] = replay.of("collaborator")
    assert f"this task's change is {report['changed_lines']} lines now" in review.prompt
    assert "estimate of ~40" in review.prompt and "stops nothing for size" in review.prompt
    assert "Unrelated work, duplication or padding is a finding" in review.prompt
    assert replay.of("auditor"), "the final audit still decides"
    assert "helper_59" in (replay.project / "app.py").read_text(), "the work is kept whole"
    lead_prompt = replay.of("lead")[0].prompt
    assert "This is the planner's estimate, not a limit" in lead_prompt
    assert "stops the task past" not in lead_prompt and "say so and stop" not in lead_prompt


def test_a_plan_sized_well_past_the_guide_is_dispatched_as_declared(tmp_path, monkeypatch):
    big = json.loads(DECL_SIMPLE.split("SCOPE: ", 1)[1].split("\n", 1)[0])
    big["max_lines"] = 400
    plan = ("REQUIREMENTS:\nR1: " + _R1 + "\nKIND: backend simple\nSCOPE: " + json.dumps(big)
            + "\nImplement add in app.py.\nCOVERS: R1")
    replay = _run(tmp_path, monkeypatch, plan=plan)
    assert replay.result.completed, replay.result.error
    assert [c.task for c in replay.of("lead")] == ["t1"], "dispatched, never sent back for its size"
    assert not any("max_lines must be" in c.prompt for c in replay.of("orchestrator"))
    first = replay.of("orchestrator")[0].prompt
    assert "a guide, not a limit" in first and "stops nothing for size alone" in first
    assert "no greater than" not in first and "No task may be sized" not in first
    assert "about 400 changed lines" in replay.of("lead")[0].prompt
    assert H.result_json(replay)["scope_reports"][-1]["max_lines"] == 400


def test_a_revision_that_grows_well_past_the_estimate_is_rechecked_not_stopped(tmp_path, monkeypatch):
    """r11's shape: an in-estimate draft, a review finding, a revision that
    lands far past the old stop. The revision is rechecked and the task
    closes; no second writer call, no stop, no BLOCKER channel."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return "Implemented add.\n" + PROVES

    def revision(call, replay):
        H.write(call, {"app.py": LARGE})
        return "Revised add with the TypeError path.\n" + PROVES
    replay = _run(tmp_path, monkeypatch, lead=lead, revision=revision,
                  collaborator=lambda c, r: _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE")
    assert replay.result.completed, replay.result.error
    assert [c.role for c in _writers(replay)] == ["lead", "revision"]
    [revision_call] = replay.of("revision")
    assert "The estimate is advisory, not a limit" in revision_call.prompt
    assert "lines of room for this revision" not in revision_call.prompt
    assert "needs about N more lines" not in revision_call.prompt and "the run stops at" not in revision_call.prompt
    assert replay.of("recheck"), "the grown revision met its recheck"
    assert H.result_json(replay)["failed_tasks"] == []
    assert "helper_59" in (replay.project / "app.py").read_text()


def test_a_capped_draft_past_its_estimate_takes_the_capped_route_not_a_size_failure(tmp_path, monkeypatch):
    """R16's shape: a draft that stops at its round cap with a large in-path
    change. The capped paths decide (here the last slot's checked source
    meets its review); nothing fails the task on size and no trim is bought."""
    def lead(call, replay):
        H.write(call, {"app.py": LARGE})
        return H.grok_ok("Still adding helpers", stop="cancelled", num_turns=8)
    replay = _run(tmp_path, monkeypatch, lead=lead, settings=Settings(backend="cli", lead_max_turns=8))
    result = H.result_json(replay)
    assert result["failed_tasks"] == [] and [c.role for c in _writers(replay)] == ["lead"]
    assert not any(f["kind"] == "failed" and "scope" in f["detail"] for f in _facts(replay))
    assert any(f["kind"] == "cap" for f in _facts(replay)), "the cap is still recorded"
    assert replay.of("collaborator"), "the capped, checked source met its reviewer"
    assert result["scope_reports"][-1]["oversized"]


def test_the_planner_sees_the_measured_size_against_the_estimate(tmp_path, monkeypatch):
    plan2 = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n" + _simple("app.py", "Implement add in app.py.", "R1"))
    docs = _simple("README.md", "Document add in README.md.", "R2")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": LARGE})
            return "Implemented add.\n" + PROVES
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'
    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(plan2, docs), lead=lead,
        collaborator=lambda c, r: "NO FINDINGS\n" + ("R1: MET - app.py:5" if c.task == "t1" else "R2: MET - README.md:3"),
        auditor=lambda c, r: "R1: MET - app.py:5\nR2: MET - README.md:3",
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=2, record_complete=False)
    second = replay.of("orchestrator")[1].prompt
    lines = H.result_json(replay)["scope_reports"][0]["changed_lines"]
    assert f"{lines} line(s) against an estimate of ~" in second, "the task row carries the measurement"
    assert "stopped past" not in second and "exceeded its declared scope" not in second


# -- negative: every other protection holds at this size ----------------------------------------

def test_a_path_breach_inside_a_large_change_still_fails_the_task_with_the_work_preserved(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": LARGE, "README.md": "# app\n\nadd.\n"})
        return "Implemented add.\n" + PROVES
    replay = _run(tmp_path, monkeypatch, lead=lead)
    result = H.result_json(replay)
    assert result["failed_tasks"] == ["t1"] and not replay.result.completed
    fact = next(f for f in _facts(replay) if f["kind"] == "failed")
    assert fact["detail"].startswith("scope: Task exceeded its declared scope") and "README.md" in fact["detail"]
    assert not replay.of("collaborator") and [c.role for c in _writers(replay)] == ["lead"]
    assert (replay.project / "README.md").read_text() == "# app\n\nadd.\n", "the work is preserved"
    assert "helper_59" in (replay.project / "app.py").read_text()


def test_a_reviewer_finding_on_unrelated_bulk_binds_through_the_fix_cycle_not_the_harness(tmp_path, monkeypatch):
    """Length is judged by the reviewer, never by the harness: a BLOCKING
    finding on unrelated helpers goes through the ordinary revision and
    recheck, and an unresolved one keeps the requirement open."""
    def collaborator(call, replay):
        return ("BLOCKING: helper_0 to helper_59 are unrelated to add and duplicate one another | basis: R1 "
                "| impact: contract\nR1: NOT MET - app.py\nREVIEW: COMPLETE")

    def revision(call, replay):
        H.write(call, {"app.py": _TYPED})
        return "Removed the unrelated helpers.\n" + PROVES
    (tmp_path / "resolved").mkdir()
    (tmp_path / "kept").mkdir()
    resolved = _run(tmp_path / "resolved", monkeypatch, collaborator=collaborator, revision=revision)
    assert resolved.result.completed, resolved.result.error
    assert [c.role for c in _writers(resolved)] == ["lead", "revision"] and resolved.of("recheck")
    assert (resolved.project / "app.py").read_text() == _TYPED
    kept = _run(tmp_path / "kept", monkeypatch, collaborator=collaborator,
                recheck=lambda c, r: "UNRESOLVED: the helpers are still there.")
    assert not kept.result.completed and "RFT1-1" in (kept.result.error or "")
    findings = {r["fid"]: r for r in H.result_json(kept)["review_findings"]}
    assert findings["RFT1-1"]["state"] == "unresolved" and not findings["RFT1-1"]["closed"]
    assert "helper_59" in (kept.project / "app.py").read_text(), "the harness removed nothing"


def test_a_large_change_that_rewrites_an_original_test_is_named_to_its_reviewer(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"app.py": LARGE, "tests/test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(2, 2) == 4\n"})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py", "tests/test_app.py"]'
    replay = _run(tmp_path, monkeypatch, lead=lead)
    [review] = replay.of("collaborator")
    assert "existed when the run started and now differ" in review.prompt and "tests/test_app.py" in review.prompt
    assert "a test or helper changed to fit the code is not evidence" in review.prompt
    assert H.result_json(replay)["scope_reports"][-1]["oversized"]


def test_a_size_past_the_direct_bound_escalates_to_the_full_review_without_refusing_the_work(tmp_path, monkeypatch):
    """The direct tier's own bound stays conservative: a task sized over it
    is not admitted to the direct tier and runs on the normal tier, with
    its cross-vendor review, rather than being refused."""
    from tests.lifecycle.test_direct_tier import FAVICON, REQS, _decl, _files, _lead, _task
    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=lambda c, r: (REQS + _decl(dict(FAVICON, max_lines=400))) if len(r.of("orchestrator")) == 1 else "DONE",
        lead=_lead, **{"requirements-review": lambda c, r: "COMPLETE",
                       "auditor": lambda c, r: "R1: MET - static/favicon.svg",
                       "design-review": lambda c, r: "APPROVED",
                       "collaborator": lambda c, r: "NO FINDINGS\nR1: MET - static/favicon.svg"}),
        files=_files(), max_tasks=3, direct_tier=True, record_complete=False)
    task = _task(replay)
    assert task["contract"]["required"]["tier"] == "normal"
    assert "exceeds the direct bound 40" in task["contract"]["required"]["tier_refused"]
    assert [c.task for c in replay.of("lead")] == ["t1"] and replay.of("collaborator"), "full review, not a refusal"
