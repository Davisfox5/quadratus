"""Tests for the three memory scopes and the pointer-backed ledger."""

from __future__ import annotations

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.ledger import Ledger
from multi_llm.memory import NoMemory, PersistentMemory, TaskMemory
from multi_llm.workers import FanOutExceeded, WorkerBudget, WorkerPool


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# -- artifacts: the summary is an index, not a replacement -------------------


def test_raw_output_survives_and_is_fetchable(store):
    ref = store.put("line one\nline two\nsecret detail", kind="draft", author="opus")
    assert "secret detail" in store.get(ref)


def test_reference_carries_a_preview_but_not_the_whole_thing(store):
    body = "\n".join(f"line {i}" for i in range(100))
    ref = store.put(body, kind="draft", author="opus")
    assert ref.lines == 100
    assert "line 0" in ref.preview
    assert "line 99" not in ref.preview
    assert "fetch artifact" in ref.render()


def test_identical_content_stores_once(store):
    a = store.put("same", kind="draft", author="opus")
    b = store.put("same", kind="draft", author="sol")
    assert a.id == b.id
    assert len(store.ids()) == 1


def test_missing_artifact_raises(store):
    with pytest.raises(KeyError):
        store.get("nope")


# -- worker bees: no memory --------------------------------------------------


def test_worker_memory_keeps_nothing():
    m = NoMemory()
    m.record("user", "remember this")
    assert m.turns() == []


# -- brain trust: full memory within a task, wiped at close ------------------


def test_peer_keeps_full_fidelity_during_a_task(store):
    task = TaskMemory("t1", "claude:opus", store)
    task.record("user", "build the parser")
    task.record("assistant", "here is my draft")
    task.record("user", "reviewer says the escaping is wrong")
    assert len(task.turns()) == 3
    assert "escaping" in task.turns()[-1].content


def test_closing_a_task_wipes_working_memory(store):
    task = TaskMemory("t1", "claude:opus", store)
    task.record("assistant", "a long draft nobody needs next task")
    task.close(summary="built the parser", reasoning="recursive descent was simplest")
    assert task.turns() == []
    assert task.closed


def test_a_closed_task_cannot_be_written_to(store):
    task = TaskMemory("t1", "claude:opus", store)
    task.close(summary="done", reasoning="because")
    with pytest.raises(RuntimeError, match="closed"):
        task.record("user", "one more thing")


def test_closing_twice_is_refused(store):
    task = TaskMemory("t1", "claude:opus", store)
    task.close(summary="done", reasoning="because")
    with pytest.raises(RuntimeError, match="already closed"):
        task.close(summary="done again", reasoning="because")


def test_a_summary_without_reasoning_is_refused(store):
    """A bare conclusion cannot be re-derived, and the next reader gets only
    this unless it fetches an artifact."""
    task = TaskMemory("t1", "claude:opus", store)
    with pytest.raises(ValueError, match="reasoning"):
        task.close(summary="did the thing", reasoning="   ")


def test_task_summary_carries_pointers_to_the_full_work(store):
    task = TaskMemory("t1", "claude:opus", store)
    task.keep("the entire 400-line draft", kind="draft")
    result = task.close(summary="built it", reasoning="simplest approach")
    assert result.refs
    assert "400-line draft" in store.get(result.refs[0])


# -- orchestrator: persistent, append-only -----------------------------------


def test_orchestrator_absorbs_completed_tasks(store):
    mem = PersistentMemory("Build a JSON parser", store)
    task = TaskMemory("t1", "claude:opus", store)
    task.keep("full draft here", kind="draft")
    mem.absorb(task.close(summary="parser done", reasoning="recursive descent"))
    assert len(mem.ledger) == 1


def test_orchestrator_can_open_the_original_behind_a_summary(store):
    """The whole point of pointers: recover what the summary skipped."""
    mem = PersistentMemory("Build a JSON parser", store)
    task = TaskMemory("t1", "claude:opus", store)
    task.keep("the summary omits this specific escaping rule", kind="draft")
    mem.absorb(task.close(summary="parser done", reasoning="recursive descent"))
    ref = mem.ledger.refs()[0]
    assert "specific escaping rule" in mem.fetch(ref)


def test_ledger_has_no_way_to_rewrite_an_entry():
    """Summarising a summary compounds loss; there is no such method."""
    ledger = Ledger()
    for name in ("update", "rewrite", "recompact", "compact", "delete"):
        assert not hasattr(ledger, name)


def test_ledger_entries_are_frozen():
    import dataclasses

    ledger = Ledger()
    entry = ledger.append(task_id="t1", author="opus", summary="s", reasoning="r")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.summary = "rewritten"


def test_reasoning_is_required_on_the_ledger_too():
    ledger = Ledger()
    with pytest.raises(ValueError, match="reasoning"):
        ledger.append(task_id="t1", author="opus", summary="s", reasoning="")


# -- rendering: position matters ---------------------------------------------


def test_goal_is_first_and_verbatim_and_current_task_is_last(store):
    mem = PersistentMemory("EXACT ORIGINAL WORDING", store)
    task = TaskMemory("t1", "claude:opus", store)
    mem.absorb(task.close(summary="did a thing", reasoning="a reason"))
    out = mem.render(current="THE QUESTION NOW")
    assert out.index("EXACT ORIGINAL WORDING") < out.index("did a thing")
    assert out.index("did a thing") < out.index("THE QUESTION NOW")


def test_invariants_are_re_emitted_on_every_render(store):
    """A rule findable only by reading the log gets summarised out eventually."""
    mem = PersistentMemory("goal", store, invariants=["Never force-push."])
    for _ in range(5):
        assert "Never force-push." in mem.render()


def test_narrowing_the_view_does_not_discard_entries(store):
    mem = PersistentMemory("goal", store)
    for i in range(5):
        t = TaskMemory(f"t{i}", "claude:opus", store)
        mem.absorb(t.close(summary=f"task {i} summary", reasoning="r"))
    narrowed = mem.render(recent=2)
    assert "task 4 summary" in narrowed
    assert "task 0 summary" not in narrowed
    assert "not shown" in narrowed
    assert len(mem.ledger) == 5  # still all there


def test_dead_ends_are_rendered_so_they_are_not_re_proposed(store):
    mem = PersistentMemory("goal", store)
    task = TaskMemory("t1", "claude:opus", store)
    mem.absorb(
        task.close(
            summary="used recursive descent",
            reasoning="simplest for this grammar",
            dead_ends=["Tried a PEG generator; it could not express the ambiguity."],
        )
    )
    assert "PEG generator" in mem.render()


# -- worker commissioning ----------------------------------------------------


@pytest.fixture
def pool(store):
    return WorkerPool(store=store, run=lambda model, prompt: f"[{model}] did: {prompt}")


def test_worker_reports_to_its_commissioning_peer_not_the_orchestrator(store, pool):
    task = TaskMemory("t1", "claude:opus", store)
    result = pool.commission(
        task=task, parent_key="claude:opus", prompt="check the RFC", label="rfc"
    )
    assert result.parent == "claude:opus"
    assert any("worker rfc" in t.content for t in task.turns())


def test_worker_output_is_stored_whole_even_though_the_peer_sees_a_trim(store):
    long_output = "x" * 5000
    pool = WorkerPool(store=store, run=lambda m, p: long_output)
    task = TaskMemory("t1", "claude:opus", store)
    result = pool.commission(
        task=task, parent_key="claude:opus", prompt="go", label="big"
    )
    assert len(result.summary) < 2000
    assert len(store.get(result.ref)) == 5000


def test_fan_out_is_capped_per_task(store):
    pool = WorkerPool(store=store, run=lambda m, p: "ok", budget=WorkerBudget(max_per_task=2))
    task = TaskMemory("t1", "claude:opus", store)
    for i in range(2):
        pool.commission(task=task, parent_key="claude:opus", prompt="go", label=f"w{i}")
    with pytest.raises(FanOutExceeded, match="budget"):
        pool.commission(task=task, parent_key="claude:opus", prompt="go", label="w3")


def test_workers_cannot_commission_workers(store, pool):
    task = TaskMemory("t1", "claude:opus", store)
    with pytest.raises(FanOutExceeded, match="depth"):
        pool.commission(
            task=task, parent_key="claude:opus", prompt="go", label="w", depth=1
        )


def test_raising_delegation_depth_is_refused():
    with pytest.raises(ValueError, match="depth is fixed at 1"):
        WorkerBudget(max_depth=2)


def test_a_closed_task_cannot_commission(store, pool):
    task = TaskMemory("t1", "claude:opus", store)
    task.close(summary="done", reasoning="because")
    with pytest.raises(RuntimeError, match="closed"):
        pool.commission(task=task, parent_key="claude:opus", prompt="go", label="w")


def test_workers_default_to_the_parents_own_provider(store, pool):
    """Same-provider workers reuse the parent's cached prefix."""
    assert pool.preferred_model("claude:opus").startswith("claude:")


def test_budgets_are_tracked_per_task_not_globally(store):
    pool = WorkerPool(store=store, run=lambda m, p: "ok", budget=WorkerBudget(max_per_task=1))
    t1 = TaskMemory("t1", "claude:opus", store)
    t2 = TaskMemory("t2", "openai:gpt-5.6-sol", store)
    pool.commission(task=t1, parent_key="claude:opus", prompt="go", label="a")
    pool.commission(task=t2, parent_key="openai:gpt-5.6-sol", prompt="go", label="b")
    assert pool.remaining("t1") == 0 and pool.remaining("t2") == 0


# -- the whole flow ----------------------------------------------------------


def test_end_to_end_one_task_through_the_architecture(store):
    """Peer works with full memory, commissions a worker, closes, and only the
    summary plus pointers reach the orchestrator."""
    mem = PersistentMemory("Build a JSON parser", store, invariants=["No new deps."])
    pool = WorkerPool(store=store, run=lambda m, p: "RFC 8259 section 7 says ...")

    task = TaskMemory("t1", "claude:opus", store)
    task.record("user", "implement string escaping")
    pool.commission(task=task, parent_key="claude:opus", prompt="read the RFC", label="rfc")
    task.keep("def unescape(s): ...  # 300 lines", kind="draft")
    summary = task.close(
        summary="implemented escaping per RFC 8259",
        reasoning="the RFC is unambiguous about surrogate pairs",
        dead_ends=["Tried a regex; it cannot handle nested surrogate pairs."],
    )
    mem.absorb(summary)

    rendered = mem.render(current="what next?")
    # Orchestrator sees the summary, the reasoning, the dead end, and the rules.
    assert "implemented escaping per RFC 8259" in rendered
    assert "surrogate pairs" in rendered
    assert "No new deps." in rendered
    # It does not see the peer's raw turns...
    assert "implement string escaping" not in rendered
    # ...but can still reach the full draft when it needs to.
    drafts = [r for r in mem.ledger.refs() if r.kind == "draft"]
    assert "300 lines" in mem.fetch(drafts[0])
    # And the peer's working memory is gone.
    assert task.turns() == []
