"""Tests for the run loop. No model is called; invoke is faked throughout."""

from __future__ import annotations

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.registry import MODE_ROSTERS
from multi_llm.routing import OrchestratorUnavailable, WorkClass
from multi_llm.session import Complexity, Session, SessionConfig, TaskSpec
from multi_llm.workers import WorkerBudget

FABLE = "claude:fable"
SOL = "openai:gpt-5.6-sol"


class Recorder:
    """Fake invoke that records every call and replies scriptably."""

    def __init__(self, closeout=None, next_tasks=None):
        self.calls = []
        self._closeout = closeout or (
            "SUMMARY: built it\nREASONING: it was simplest\nDEAD ENDS: - Tried X; too slow."
        )
        self._next = list(next_tasks or ["DONE"])

    def __call__(self, model, prompt, system=None):
        self.calls.append({"model": model, "prompt": prompt})
        if "Name the single next task" in prompt:
            return self._next.pop(0) if self._next else "DONE"
        if "The task is finished" in prompt:
            return self._closeout
        return f"[{model}] output"

    def models(self):
        return [c["model"] for c in self.calls]

    def prompts_to(self, model):
        return [c["prompt"] for c in self.calls if c["model"] == model]


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def rec():
    return Recorder()


def _session(store, rec, **kw):
    return Session("Build a JSON parser", store, rec, **kw)


# -- collaboration scales with complexity ------------------------------------


def test_simple_task_has_no_collaborators(store, rec):
    s = _session(store, rec)
    spec = TaskSpec("t1", "trivial thing", complexity=Complexity.SIMPLE)
    assert s.collaborators_for(spec, s.brain_trust[0]) == []


def test_standard_task_draws_one_collaborator(store, rec):
    s = _session(store, rec)
    spec = TaskSpec("t1", "normal thing", complexity=Complexity.STANDARD)
    assert len(s.collaborators_for(spec, s.brain_trust[0])) == 1


def test_complex_task_draws_the_whole_brain_trust(store, rec):
    s = _session(store, rec)
    spec = TaskSpec("t1", "hard thing", complexity=Complexity.COMPLEX)
    lead = s.brain_trust[0]
    assert len(s.collaborators_for(spec, lead)) == len(s.brain_trust) - 1


def test_the_lead_is_never_its_own_collaborator(store, rec):
    s = _session(store, rec)
    for complexity in (Complexity.SIMPLE, Complexity.STANDARD, Complexity.COMPLEX):
        for lead in s.brain_trust:
            assert lead not in s.collaborators_for(TaskSpec("t", "x", complexity), lead)


def test_unknown_complexity_falls_back_to_standard(store, rec):
    s = _session(store, rec)
    spec = TaskSpec("t1", "x", complexity="wildly-complex")
    assert len(s.collaborators_for(spec, s.brain_trust[0])) == 1


# -- lead rotation -----------------------------------------------------------


def test_lead_rotates_across_the_brain_trust(store, rec):
    s = _session(store, rec)
    leads = [
        s.run_task(TaskSpec(f"t{i}", "work", complexity=Complexity.SIMPLE)).author
        for i in range(len(s.brain_trust))
    ]
    assert len(set(leads)) == len(s.brain_trust)


def test_an_explicit_lead_overrides_rotation(store, rec):
    s = _session(store, rec)
    got = s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE, lead=SOL))
    assert got.author == SOL


# -- what reaches the orchestrator -------------------------------------------


def test_only_the_summary_reaches_the_ledger_not_the_working_turns(store, rec):
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "implement string escaping", complexity=Complexity.SIMPLE))
    rendered = s.memory.render()
    assert "built it" in rendered
    assert "implement string escaping" not in rendered


def test_the_full_work_is_still_reachable_from_the_summary(store, rec):
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    drafts = [r for r in s.memory.ledger.refs() if r.kind == "draft"]
    assert drafts and "output" in s.memory.fetch(drafts[0])


def test_collaborator_contributions_are_stored_as_artifacts(store, rec):
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.COMPLEX))
    kinds = {r.kind for r in s.memory.ledger.refs()}
    assert any(k.startswith("review:") for k in kinds)


def test_dead_ends_survive_into_the_ledger(store, rec):
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    assert "Tried X" in s.memory.render()


def test_goal_is_verbatim_in_every_orchestrator_prompt(store, rec):
    s = _session(store, rec)
    s.run(max_tasks=1)
    orchestrator_prompts = rec.prompts_to(FABLE)
    assert orchestrator_prompts
    assert all("Build a JSON parser" in p for p in orchestrator_prompts)


def test_invariants_reach_every_orchestrator_prompt(store):
    rec = Recorder(next_tasks=["do a thing", "DONE"])
    s = Session("goal", store, rec, invariants=["Never force-push."])
    s.run(max_tasks=2)
    assert all("Never force-push." in p for p in rec.prompts_to(FABLE))


# -- collaborators stay independent ------------------------------------------


def test_collaborators_do_not_receive_the_session_history(store, rec):
    """Their value is an independent read; inheriting the lead's history erodes it."""
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "first task", complexity=Complexity.SIMPLE))
    s.run_task(TaskSpec("t2", "second task", complexity=Complexity.COMPLEX))
    collaborator_prompts = [
        c["prompt"] for c in rec.calls
        if "contributing an independent read" in c["prompt"]
    ]
    assert collaborator_prompts
    assert all("first task" not in p for p in collaborator_prompts)


def test_reviewers_are_told_not_to_self_filter(store, rec):
    """A reviewer instructed to be selective suppresses its own findings."""
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.COMPLEX))
    prompts = [c["prompt"] for c in rec.calls if "independent read" in c["prompt"]]
    assert all("do not filter" in p for p in prompts)


# -- the loop ----------------------------------------------------------------


def test_run_stops_when_the_orchestrator_says_done(store):
    rec = Recorder(next_tasks=["task one", "DONE"])
    s = _session(store, rec)
    assert len(s.run(max_tasks=10)) == 1


def test_run_respects_the_task_cap(store):
    rec = Recorder(next_tasks=["a"] * 50)
    s = _session(store, rec)
    assert len(s.run(max_tasks=3)) == 3


def test_tasks_accumulate_in_the_ledger_in_order(store):
    rec = Recorder(next_tasks=["a", "b", "DONE"])
    s = _session(store, rec)
    s.run()
    assert [e.seq for e in s.memory.ledger.entries] == [0, 1]


# -- seating and routing hold inside the loop --------------------------------


def test_an_unavailable_orchestrator_halts_the_run(store, rec):
    s = _session(store, rec, available=lambda k: k != FABLE)
    with pytest.raises(OrchestratorUnavailable):
        s.next_task()


def test_security_work_is_routed_away_from_the_rotation(store, rec):
    s = _session(store, rec)
    got = s.run_task(
        TaskSpec("t1", "audit the auth flow", complexity=Complexity.SIMPLE,
                 work_class=WorkClass.SECURITY)
    )
    assert got.author == SOL


def test_fable_never_leads_a_task(store, rec):
    """It orchestrates; a supervisor does not compete in the debate."""
    s = _session(store, rec)
    for i in range(6):
        assert s.run_task(TaskSpec(f"t{i}", "work", complexity=Complexity.SIMPLE)).author != FABLE


def test_brain_trust_matches_the_mode_roster(store, rec):
    s = _session(store, rec)
    assert s.brain_trust == MODE_ROSTERS["adversarial"]["peers"]


# -- worker budget is enforced through the session ---------------------------


def test_worker_budget_is_shared_with_the_pool(store, rec):
    s = _session(store, rec, config=SessionConfig(worker_budget=WorkerBudget(max_per_task=1)))
    assert s.workers.budget.max_per_task == 1


# -- close-out parsing -------------------------------------------------------


def test_closeout_parses_all_three_sections(store):
    rec = Recorder(closeout=(
        "SUMMARY: did the thing\n"
        "REASONING: because of the constraint\n"
        "DEAD ENDS:\n- Tried A; it deadlocked.\n- Tried B; too slow."
    ))
    s = _session(store, rec)
    got = s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    assert got.summary == "did the thing"
    assert got.reasoning == "because of the constraint"
    assert len(got.dead_ends) == 2


def test_closeout_without_sections_still_produces_a_usable_record(store):
    """Degrade to a usable record rather than losing the task; the raw work is
    stored either way."""
    rec = Recorder(closeout="I just wrote some prose with no headings at all.")
    s = _session(store, rec)
    got = s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    assert "prose" in got.summary
    assert got.reasoning  # ledger refuses an empty one


def test_closeout_with_no_dead_ends_is_fine(store):
    rec = Recorder(closeout="SUMMARY: clean run\nREASONING: nothing went wrong")
    s = _session(store, rec)
    assert s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE)).dead_ends == []
