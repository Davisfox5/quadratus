"""Tests for the five externally-informed enhancements.

Each traces to a measured failure or success in a deployed system: recitation
and the failure-loop breaker from Manus's published lessons and field
failures, cross-family verification and the codebase map from Blitzy's
audited harness, the plan gate from its AAP review step.
"""

from __future__ import annotations

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.codebase_map import CodebaseMap
from multi_llm.memory import TaskMemory
from multi_llm.routing import cross_family_verifier
from multi_llm.session import Complexity, RunStalled, Session, SessionConfig, TaskSpec
from multi_llm.task_kinds import TaskKind
from multi_llm.workers import RepeatedFailure, WorkerPool

from .test_session import Recorder  # reuse the scripted fake

FABLE = "claude:fable"
OPUS = "claude:opus"
SOL = "openai:gpt-5.6-sol"
TRUST = [OPUS, SOL, "gemini:gemini-3.1-pro", "grok:grok-4.6"]


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def rec():
    return Recorder()


def _session(store, rec, **kw):
    return Session("Build a JSON parser", store, rec, **kw)


# -- 1: recitation ------------------------------------------------------------


def test_the_task_is_restated_at_the_end_of_the_lead_prompt(store, rec):
    """The end of a long context is the position attention favours; the
    objective is re-emitted there so history piling up cannot bury it."""
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "implement RFC 8259 string escaping",
                        complexity=Complexity.SIMPLE))
    prompt = next(c["prompt"] for c in rec.calls if "You are leading" in c["prompt"])
    first = prompt.index("implement RFC 8259 string escaping")
    last = prompt.rindex("implement RFC 8259 string escaping")
    assert last > first  # appears twice...
    assert last > prompt.index("You are leading")  # ...and once after the role line


# -- 2: cross-family verification ---------------------------------------------


def test_verifier_comes_from_a_different_vendor():
    assert cross_family_verifier(SOL, candidates=TRUST).startswith("claude:")
    assert not cross_family_verifier(OPUS, candidates=TRUST).startswith("claude:")


def test_no_cross_family_candidate_returns_none_not_a_self_check():
    assert cross_family_verifier(OPUS, candidates=[OPUS, FABLE]) is None


def test_security_verification_never_stays_within_one_vendor(store, rec):
    """If the chain degrades until worker and deputy share a vendor, a
    cross-family peer is drafted for the verification instead."""
    down = {SOL}  # Sol down -> worker falls to Sonnet, deputy is Opus: both claude
    s = _session(store, rec, available=lambda k: k not in down)
    s.run_task(TaskSpec("t1", "audit the auth flow", kind=TaskKind.SECURITY))
    verify = next(c for c in rec.calls if "verifying security work" in c["prompt"])
    assert not verify["model"].startswith("claude:")


# -- 3: the codebase map ------------------------------------------------------


def test_map_notes_survive_to_a_fresh_instance(tmp_path):
    path = tmp_path / "map.jsonl"
    CodebaseMap(path).amend(topic="conventions", note="tests use pytest fixtures",
                            author=OPUS, session="s1")
    reloaded = CodebaseMap(path)
    assert len(reloaded) == 1
    assert "pytest fixtures" in reloaded.render()


def test_map_is_append_only(tmp_path):
    m = CodebaseMap(tmp_path / "map.jsonl")
    for name in ("update", "rewrite", "delete", "compact", "prune"):
        assert not hasattr(m, name)


def test_empty_or_topicless_notes_are_refused(tmp_path):
    m = CodebaseMap(tmp_path / "map.jsonl")
    with pytest.raises(ValueError):
        m.amend(topic="x", note="   ", author=OPUS)
    with pytest.raises(ValueError):
        m.amend(topic=" ", note="a fact", author=OPUS)


def test_render_narrows_by_topic_without_touching_the_store(tmp_path):
    m = CodebaseMap(tmp_path / "map.jsonl")
    m.amend(topic="gotchas", note="the parser is quadratic", author=OPUS)
    m.amend(topic="deps", note="pinned to urllib3<2", author=SOL)
    narrowed = m.render(topics=["gotchas"])
    assert "quadratic" in narrowed and "urllib3" not in narrowed
    assert len(m) == 2


def test_the_map_reaches_orchestrator_and_lead_prompts(store, tmp_path):
    m = CodebaseMap(tmp_path / "map.jsonl")
    m.amend(topic="gotchas", note="the config loader swallows KeyError", author=OPUS)
    rec = Recorder(next_tasks=["do a thing", "DONE"])
    s = _session(store, rec, config=SessionConfig(codebase_map=m))
    s.run(max_tasks=2)
    assert all("swallows KeyError" in p for p in rec.prompts_to(FABLE))
    lead_prompts = [c["prompt"] for c in rec.calls if "You are leading" in c["prompt"]]
    assert lead_prompts and all("swallows KeyError" in p for p in lead_prompts)


def test_the_goal_still_leads_and_the_question_still_closes(store, tmp_path):
    """The map must not displace the two positions that matter most."""
    m = CodebaseMap(tmp_path / "map.jsonl")
    m.amend(topic="gotchas", note="MAP-MARKER", author=OPUS)
    rec = Recorder(next_tasks=["do a thing", "DONE"])
    s = _session(store, rec, config=SessionConfig(codebase_map=m))
    s.run(max_tasks=1)
    prompt = rec.prompts_to(FABLE)[0]
    assert prompt.index("Build a JSON parser") < prompt.index("MAP-MARKER")
    assert prompt.index("MAP-MARKER") < prompt.index("Name the single next task")


def test_closeouts_teach_the_map(store, tmp_path):
    m = CodebaseMap(tmp_path / "map.jsonl")
    rec = Recorder(closeout=(
        "SUMMARY: built it\nREASONING: simplest\n"
        "MAP NOTES:\n- conventions: errors are wrapped in AppError\n"
        "- not-a-note-without-content:"
    ))
    s = _session(store, rec, config=SessionConfig(codebase_map=m))
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    assert len(m) == 1
    assert "AppError" in m.render()


def test_without_a_map_closeouts_do_not_ask_for_notes(store, rec):
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    closeout = next(c["prompt"] for c in rec.calls if "The task is finished" in c["prompt"])
    assert "MAP NOTES" not in closeout


# -- 4: the failure-loop breaker ----------------------------------------------


def _failing_pool(store, fail_times):
    calls = {"n": 0}

    def run(model, prompt):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise RuntimeError("boom")
        return "ok"

    return WorkerPool(store=store, run=run)


def test_a_verbatim_retry_of_a_failed_prompt_is_refused(store):
    pool = _failing_pool(store, fail_times=1)
    task = TaskMemory("t1", OPUS, store)
    with pytest.raises(RuntimeError, match="boom"):
        pool.commission(task=task, parent_key=OPUS, prompt="fetch the RFC", label="a")
    with pytest.raises(RepeatedFailure, match="Rephrase"):
        pool.commission(task=task, parent_key=OPUS, prompt="fetch the RFC", label="b")


def test_a_rephrased_retry_is_allowed(store):
    pool = _failing_pool(store, fail_times=1)
    task = TaskMemory("t1", OPUS, store)
    with pytest.raises(RuntimeError, match="boom"):
        pool.commission(task=task, parent_key=OPUS, prompt="fetch the RFC", label="a")
    got = pool.commission(
        task=task, parent_key=OPUS,
        prompt="fetch RFC 8259 section 7 via the mirror", label="b",
    )
    assert got.summary == "ok"


def test_the_same_prompt_is_fine_on_a_different_task(store):
    pool = _failing_pool(store, fail_times=1)
    t1 = TaskMemory("t1", OPUS, store)
    with pytest.raises(RuntimeError, match="boom"):
        pool.commission(task=t1, parent_key=OPUS, prompt="fetch the RFC", label="a")
    t2 = TaskMemory("t2", OPUS, store)
    assert pool.commission(
        task=t2, parent_key=OPUS, prompt="fetch the RFC", label="a"
    ).summary == "ok"


def test_a_refused_retry_does_not_consume_budget(store):
    pool = _failing_pool(store, fail_times=1)
    task = TaskMemory("t1", OPUS, store)
    with pytest.raises(RuntimeError, match="boom"):
        pool.commission(task=task, parent_key=OPUS, prompt="p", label="a")
    spent = pool.spawned("t1")
    with pytest.raises(RepeatedFailure):
        pool.commission(task=task, parent_key=OPUS, prompt="p", label="b")
    assert pool.spawned("t1") == spent


def test_a_stalled_run_raises_rather_than_burning_the_window(store):
    rec = Recorder(next_tasks=["build the lexer", "build the lexer"])
    s = _session(store, rec)
    with pytest.raises(RunStalled, match="twice in a row"):
        s.run(max_tasks=10)


def test_revisiting_a_task_later_is_not_a_stall(store):
    rec = Recorder(next_tasks=["build the lexer", "build the parser",
                               "build the lexer", "DONE"])
    s = _session(store, rec)
    assert len(s.run(max_tasks=10)) == 3


# -- 5: the plan gate ---------------------------------------------------------


def test_a_declined_plan_runs_nothing(store):
    rec = Recorder(next_tasks=["should never be asked"])
    seen = {}

    def gate(plan):
        seen["plan"] = plan
        return False

    s = _session(store, rec, config=SessionConfig(plan_gate=gate))
    assert s.run(max_tasks=5) == []
    assert len(rec.calls) == 1  # the plan request and nothing else
    assert "Do not start work" in rec.calls[0]["prompt"]


def test_an_approved_plan_runs_normally(store):
    rec = Recorder(next_tasks=["task one", "DONE"])
    s = _session(store, rec, config=SessionConfig(plan_gate=lambda _p: True))
    assert len(s.run(max_tasks=5)) == 1


def test_the_plan_request_carries_the_size_ceiling(store, rec):
    s = _session(store, rec)
    s.plan()
    from multi_llm.task_kinds import MAX_TASK_LINES
    assert str(MAX_TASK_LINES) in rec.calls[0]["prompt"]


def test_no_gate_means_no_extra_invocation(store):
    rec = Recorder(next_tasks=["DONE"])
    s = _session(store, rec)
    s.run(max_tasks=1)
    assert len(rec.calls) == 1  # just next_task; no plan round


# -- the worker tree, concurrency, and tool requests ---------------------------

from multi_llm.workers import pick_worker, worker_menu  # noqa: E402


def test_the_tree_picks_by_errand_and_spreads_vendors():
    picks = {pick_worker(e) for e in ("lookup", "read", "check", "format")}
    vendors = {p.split(":")[0] for p in picks}
    assert vendors == {"grok", "gemini", "claude", "openai"}


def test_demanding_errands_escalate_within_their_own_family():
    """The skill stays matched; the horsepower goes up one tier."""
    assert pick_worker("check", demanding=True) == "claude:sonnet"
    assert pick_worker("read", demanding=True) == "gemini:gemini-3.6-thinking"
    assert pick_worker("format", demanding=True) == "openai:gpt-5.6-terra"
    assert pick_worker("lookup", demanding=True) == "grok:grok-4.20"


def test_a_bump_target_missing_from_the_roster_degrades_to_the_base():
    from multi_llm.registry import resolve
    from multi_llm.workers import WORKER_ESCALATION
    for bumped in WORKER_ESCALATION.values():
        # every configured bump must resolve today, or pick_worker would
        # silently fall back -- this test is the tripwire for roster drift
        assert resolve(bumped) is not None, bumped


def test_escalation_never_crosses_vendor_lines():
    from multi_llm.registry import resolve
    from multi_llm.workers import WORKER_ESCALATION
    for base, bumped in WORKER_ESCALATION.items():
        assert resolve(base).provider == resolve(bumped).provider, base


def test_an_unknown_errand_gets_the_careful_generalist():
    assert pick_worker("interpretive-dance") == "claude:haiku"


def test_the_menu_reaches_the_lead_prompt(store, rec):
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    lead = next(c["prompt"] for c in rec.calls if "You are leading" in c["prompt"])
    assert "Worker bees" in lead and "lookup" in lead and "NEED TOOL" in lead


def test_the_same_prompt_may_be_rerouted_to_a_different_worker(store):
    """A lookup that failed on a stale-knowledge model is rationally re-sent,
    unchanged, to the scout. Only same-prompt-same-model is a death spiral."""
    def run(model, prompt):
        if model == "claude:haiku":
            raise RuntimeError("knowledge too old")
        return "found it"

    pool = WorkerPool(store=store, run=run)
    task = TaskMemory("t1", OPUS, store)
    with pytest.raises(RuntimeError):
        pool.commission(task=task, parent_key=OPUS, prompt="latest React API", label="a")
    got = pool.commission(task=task, parent_key=OPUS, prompt="latest React API",
                          label="b", errand="lookup")
    assert got.summary == "found it"
    assert got.model == "grok:grok-4-1-fast"


def test_workers_actually_run_concurrently(store):
    """Three errands that each wait for the others can only finish together."""
    import threading
    barrier = threading.Barrier(3, timeout=5)

    def run(model, prompt):
        barrier.wait()
        return "ok"

    pool = WorkerPool(store=store, run=run)
    task = TaskMemory("t1", OPUS, store)
    results = pool.commission_many(
        task=task, parent_key=OPUS,
        jobs=[{"prompt": f"p{i}", "label": f"w{i}"} for i in range(3)],
    )
    assert [r.error for r in results] == [None, None, None]


def test_one_failed_errand_does_not_tear_down_its_siblings(store):
    def run(model, prompt):
        if "bad" in prompt:
            raise RuntimeError("boom")
        return "fine"

    pool = WorkerPool(store=store, run=run)
    task = TaskMemory("t1", OPUS, store)
    results = pool.commission_many(
        task=task, parent_key=OPUS,
        jobs=[{"prompt": "good one", "label": "a"},
              {"prompt": "bad one", "label": "b"},
              {"prompt": "another good", "label": "c"}],
    )
    assert [bool(r.error) for r in results] == [False, True, False]
    assert any("FAILED" in t.content for t in task.turns())


def test_a_worker_can_ask_for_a_tool_it_lacks(store):
    pool = WorkerPool(store=store, run=lambda m, p: "NEED TOOL: file write access\nI can draft it but not save it.")
    task = TaskMemory("t1", OPUS, store)
    got = pool.commission(task=task, parent_key=OPUS, prompt="save the config", label="w")
    assert got.needs_tool == "file write access"
    assert "needs a tool" in got.summary


def test_the_reissued_errand_carries_the_grant(store):
    seen = {}

    def run(model, prompt, allow_writes=False):
        seen["allow_writes"] = allow_writes
        return "done"

    pool = WorkerPool(store=store, run=run)
    task = TaskMemory("t1", OPUS, store)
    pool.commission(task=task, parent_key=OPUS, prompt="save the config",
                    label="w", allow_writes=True)
    assert seen["allow_writes"] is True


def test_the_lifetime_ceiling_still_exists(store):
    from multi_llm.workers import FanOutExceeded, WorkerBudget
    pool = WorkerPool(store=store, run=lambda m, p: "ok",
                      budget=WorkerBudget(max_per_task=2))
    task = TaskMemory("t1", OPUS, store)
    pool.commission(task=task, parent_key=OPUS, prompt="a", label="a")
    pool.commission(task=task, parent_key=OPUS, prompt="b", label="b")
    with pytest.raises(FanOutExceeded):
        pool.commission(task=task, parent_key=OPUS, prompt="c", label="c")


def test_worker_menu_names_every_errand_in_the_tree():
    from multi_llm.workers import WORKER_TREE
    menu = worker_menu()
    for errand in WORKER_TREE:
        assert errand in menu, errand
