"""Tests for waves, the arbiter's REDO channel, and open-question bubbling.

No model is called; invoke is faked throughout. The wave design under test:
the orchestrator names every task that can start now, independent tasks run
concurrently (many instances of one model is fine -- they are stateless CLI
calls), the orchestrator is the final arbiter and may reject finished work,
and open questions bubble up from any level through the ledger, rendered
loudly until addressed.
"""

from __future__ import annotations

import threading

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.session import RunStalled, Session, SessionConfig
from multi_llm.task_kinds import TaskKind


class Recorder:
    """Fake invoke that records every call and replies scriptably."""

    def __init__(self, waves=None, closeout=None):
        self.calls = []
        self._lock = threading.Lock()
        self._waves = list(waves or ["DONE"])
        self._closeout = closeout or (
            "SUMMARY: built it\nREASONING: it was simplest"
        )

    def __call__(self, model, prompt, system=None):
        with self._lock:
            self.calls.append({"model": model, "prompt": prompt})
        if "Name the next wave of tasks" in prompt:
            with self._lock:
                return self._waves.pop(0) if self._waves else "DONE"
        if "The task is finished" in prompt:
            return self._closeout
        return f"[{model}] output"

    def prompts_to_orchestrator(self):
        return [c["prompt"] for c in self.calls
                if "Name the next wave of tasks" in c["prompt"]]


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def _session(store, rec, **kw):
    return Session("Build a JSON parser", store, rec, **kw)


# -- naming a wave -------------------------------------------------------------


def test_a_wave_of_task_lines_becomes_that_many_specs(store):
    rec = Recorder(waves=[
        "TASK backend simple: build the tokenizer\n"
        "TASK test standard: test the number grammar\n"
        "TASK docs rote: draft the README skeleton",
    ])
    s = _session(store, rec)
    wave = s.next_wave()
    assert [t.description for t in wave] == [
        "build the tokenizer",
        "test the number grammar",
        "draft the README skeleton",
    ]
    assert [t.kind for t in wave] == ["backend", "test", "docs"]
    assert [t.complexity for t in wave] == ["simple", "standard", "rote"]
    assert [t.task_id for t in wave] == ["t1", "t2", "t3"]


def test_unknown_labels_on_a_task_line_degrade_instead_of_dropping(store):
    rec = Recorder(waves=["TASK astrology mystifying: do the thing"])
    s = _session(store, rec)
    wave = s.next_wave()
    assert len(wave) == 1
    assert wave[0].kind == TaskKind.GENERAL
    assert wave[0].complexity == "simple"
    assert wave[0].description == "do the thing"


def test_a_bare_reply_is_still_a_single_task_wave(store):
    """Legacy shape: no TASK lines means the whole reply is one task."""
    rec = Recorder(waves=["KIND: debug complex\nfind the null deref"])
    s = _session(store, rec)
    wave = s.next_wave()
    assert len(wave) == 1
    assert wave[0].kind == "debug"
    assert wave[0].complexity == "complex"


def test_done_still_ends_the_run(store):
    rec = Recorder(waves=["DONE"])
    s = _session(store, rec)
    assert s.next_wave() is None


def test_task_ids_keep_counting_across_waves(store):
    rec = Recorder(waves=[
        "TASK general simple: first",
        "TASK general simple: second\nTASK general simple: third",
        "DONE",
    ])
    s = _session(store, rec)
    ids = [t.task_id for w in iter(s.next_wave, None) for t in w]
    assert ids == ["t1", "t2", "t3"]


def test_the_wave_prompt_states_independence_and_arbitership(store):
    rec = Recorder(waves=["DONE"])
    s = _session(store, rec)
    s.next_wave()
    prompt = rec.prompts_to_orchestrator()[0]
    assert "cannot see each other's results" in prompt
    assert "final arbiter" in prompt
    assert "REDO" in prompt
    assert "OPEN QUESTION" in prompt


# -- running a wave in parallel -------------------------------------------------


def test_independent_tasks_in_one_wave_run_at_the_same_time(store):
    """Two same-difficulty tasks route to the same model; both instances must
    be live at once -- which also proves multiple instances of one model."""
    barrier = threading.Barrier(2, timeout=10)

    class Meeting(Recorder):
        def __call__(self, model, prompt, system=None):
            if "You are leading this task" in prompt:
                barrier.wait()  # raises BrokenBarrierError if run one-lane
            return super().__call__(model, prompt, system)

    rec = Meeting(waves=[
        "TASK general simple: build the tokenizer\n"
        "TASK general simple: build the string escaper",
        "DONE",
    ])
    s = _session(store, rec)
    summaries = s.run()
    assert len(summaries) == 2
    assert summaries[0].author == summaries[1].author  # same model, twice


def test_wave_width_is_capped_by_max_parallel_tasks(store):
    peak = {"now": 0, "max": 0}
    gate = threading.Lock()
    entered = threading.Semaphore(0)

    class Counting(Recorder):
        def __call__(self, model, prompt, system=None):
            if "You are leading this task" in prompt:
                with gate:
                    peak["now"] += 1
                    peak["max"] = max(peak["max"], peak["now"])
                entered.release()
                # Hold long enough that a third task would overlap if allowed.
                import time
                time.sleep(0.05)
                with gate:
                    peak["now"] -= 1
            return super().__call__(model, prompt, system)

    rec = Counting(waves=[
        "\n".join(f"TASK general simple: piece {i}" for i in range(5)),
        "DONE",
    ])
    s = _session(store, rec, config=SessionConfig(max_parallel_tasks=2))
    s.run()
    assert peak["max"] <= 2


def test_a_failing_task_does_not_tear_down_its_siblings(store):
    class OneBadApple(Recorder):
        def __call__(self, model, prompt, system=None):
            if "You are leading this task" in prompt and "doomed" in prompt:
                raise RuntimeError("provider fell over")
            return super().__call__(model, prompt, system)

    rec = OneBadApple(waves=[
        "TASK general simple: doomed piece\n"
        "TASK general simple: healthy piece",
    ])
    s = _session(store, rec)
    with pytest.raises(RuntimeError, match="provider fell over"):
        s.run()
    # The sibling still finished and its summary is real progress.
    assert len(s.history) == 1


def test_a_repeated_description_across_consecutive_waves_stalls(store):
    rec = Recorder(waves=[
        "TASK general simple: build the lexer",
        "TASK general simple: build the lexer",
    ])
    s = _session(store, rec)
    with pytest.raises(RunStalled, match="two consecutive waves"):
        s.run()


# -- the arbiter's REDO channel --------------------------------------------------


def test_a_redo_reissues_the_task_with_the_objection_in_hand(store):
    rec = Recorder(waves=[
        "TASK backend standard: build the parser core",
        "REDO t1: error recovery is missing; a bad token aborts the parse",
        "DONE",
    ])
    s = _session(store, rec)
    summaries = s.run()
    assert [x.task_id for x in summaries] == ["t1", "t1-r1"]
    redo = s._specs["t1-r1"]
    assert redo.kind == "backend"
    assert redo.complexity == "standard"
    assert "build the parser core" in redo.description
    assert "error recovery is missing" in redo.description
    # It points at the rejected work rather than making the new lead guess.
    assert "artifact" in redo.description


def test_redo_past_the_cap_stalls_loudly(store):
    rec = Recorder(waves=[
        "TASK general simple: build it",
        "REDO t1: not good enough",
        "REDO t1-r1: still not good enough",
        "REDO t1-r2: no",
    ])
    s = _session(store, rec, config=SessionConfig(max_redos=2))
    with pytest.raises(RunStalled, match="rejected task 't1' 3 times"):
        s.run()


def test_a_redo_of_an_unknown_task_id_is_skipped_not_fatal(store):
    rec = Recorder(waves=[
        "REDO t99: never existed\nTASK general simple: real work",
        "DONE",
    ])
    s = _session(store, rec)
    wave = s.next_wave()
    assert [t.description for t in wave] == ["real work"]


# -- open questions bubble to the orchestrator -----------------------------------


def test_close_out_open_questions_reach_the_ledger_loudly(store):
    rec = Recorder(
        waves=["TASK general simple: wire the config loader", "DONE"],
        closeout=(
            "SUMMARY: wired it\nREASONING: simplest\n"
            "OPEN QUESTIONS:\n- Should defaults live in code or in a file? "
            "Operator taste, not derivable."
        ),
    )
    s = _session(store, rec)
    s.run()
    assert s.history[0].open_questions == [
        "Should defaults live in code or in a file? Operator taste, not derivable."
    ]
    rendered = s.memory.render()
    assert "OPEN QUESTIONS (must be addressed, not skipped)" in rendered
    assert "defaults live in code or in a file" in rendered
    # And the next wave's orchestrator prompt carried it.
    assert "defaults live in code or in a file" in rec.prompts_to_orchestrator()[-1]


def test_a_surviving_gate_failure_becomes_an_open_question(store):
    class AlwaysRed:
        def run(self):
            class R:
                passed = False
                def render(self):
                    return "FAIL: 3 tests red"
            return R()

    rec = Recorder(waves=["TASK general simple: change the schema", "DONE"])
    s = _session(store, rec, config=SessionConfig(integration_gate=AlwaysRed()))
    s.run()
    assert any("integration check was still failing" in q
               for q in s.history[0].open_questions)
