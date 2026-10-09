"""Every incomplete run names why (phase 3, map G9).

One journey per stop family. Each ended incomplete with a blank error on
ca69c13; each now ends with its exact named stop, the typed stop fact it
names, and the run still incomplete. Only public result fields and the
typed record are read, so the old commit fails these on behaviour."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, Script

CAPPED_SETTINGS = None


def _stop(replay):
    return replay.workflow["run"]["facts"][-1]


def _capped_lead(call, replay):
    H.write(call, {"app.py": FIXED})
    return H.claude_cap("Wrote add; tests not run yet.", num_turns=14)


def _settings():
    from quadratus.config import Settings
    return Settings(backend="cli", lead_max_turns=14)


def test_a_declined_plan_is_named(tmp_path):
    calls = []

    def invoke(model, prompt, system=None, allow_writes=False):
        calls.append(prompt)
        return "KIND: backend simple\nBuild it."
    session = Session("Build it", ArtifactStore(tmp_path / "a"), invoke,
                      config=SessionConfig(plan_gate=lambda plan: False))
    assert session.run(max_tasks=3) == [] and not session.completed
    assert session.stop_reason == ("PlanDeclined: the operator's plan gate declined the expected task list; "
                                   "no task ran and nothing was changed.")
    stop = session.run_outcome.facts[-1]
    assert (stop.kind, stop.legacy) == ("operator", "PlanDeclined") and len(calls) == 1


@pytest.mark.requirements_ledger
def test_requirements_still_unmet_after_the_reopen_allowance_are_named(tmp_path):
    from tests.test_requirements_ledger import PLAN
    from tests.test_requirements_ledger import Script as LedgerScript
    plan = PLAN.replace("COVERS: R1", "COVERS: R1, R2, R3")
    script = LedgerScript([plan, "DONE", "DONE", "DONE", "DONE"],
                          audits=["R1: MET - app.py\nR2: NOT MET - no button\nR3: MET - app.py"] * 4)
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script,
                      config=SessionConfig(max_requirement_reopens=2))
    session.run(max_tasks=6)
    assert not session.completed
    assert session.stop_reason.startswith("RequirementsUnmet: DONE was sent back 2 time(s) and the requirements "
                                          "are still not met: "), session.stop_reason
    assert "R2" in session.stop_reason and session.stop_reason.endswith("Work preserved.")
    stop = session.run_outcome.facts[-1]
    assert (stop.kind, stop.legacy) == ("unverified", "RequirementsUnmet")


def test_done_with_a_capped_task_never_continued_is_named(tmp_path, monkeypatch):
    def orchestrator(call, replay):
        return DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=_capped_lead), files=FILES,
                   max_tasks=4, settings=_settings())
    assert not replay.result.completed
    assert replay.result.error == ("DoneWithOpenWork: the orchestrator reported DONE, but capped or failed task(s) t1 not "
                                   "continued to completion. Work preserved.")
    assert _stop(replay)["legacy"] == "DoneWithOpenWork"
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"] and (replay.project / "app.py").read_text() == FIXED


def test_the_cap_without_a_confirmed_goal_is_named(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(), files=FILES, max_tasks=1)
    assert not replay.result.completed
    assert replay.result.error == ("GoalUnconfirmedAtCap: the task cap (1) was reached and the orchestrator did "
                                   "not confirm the goal met. Work preserved.")
    stop = _stop(replay)
    assert (stop["kind"], stop["legacy"]) == ("cap", "GoalUnconfirmedAtCap")
    assert len(replay.of("orchestrator")) == 2, "one task, then the one terminal question"


def test_the_cap_with_a_capped_task_never_continued_is_named(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(lead=_capped_lead), files=FILES, max_tasks=1,
                   settings=_settings())
    assert not replay.result.completed
    assert replay.result.error == ("GoalUnconfirmedAtCap: the task cap (1) was reached, but capped or failed task(s) t1 not "
                                   "continued to completion. Work preserved.")
    assert len(replay.of("orchestrator")) == 1, "the goal question is not asked over capped debt"


def test_a_task_closing_with_a_failed_attributed_check_is_named(tmp_path, monkeypatch):
    def looked(call, replay):
        return "Looked; left it.\nCHANGED: []"
    replay = H.run(tmp_path, monkeypatch, Script(lead=looked), files=FILES, max_tasks=3)
    assert not replay.result.completed
    assert replay.result.error.startswith("CheckFailing: a task closed with open work, but task t1's last check "
                                          "still fails (attempt 2, output artifact ")
    assert (_stop(replay)["kind"], _stop(replay)["legacy"]) == ("product", "CheckFailing")
    assert len(replay.of("gate-fix")) == 1
