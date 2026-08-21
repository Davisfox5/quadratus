"""Tests for the glue: scan, interview, persistence, judge, probes, scoreboard.

These are the pieces that turn the tested machine into a runnable system.
No vendor CLI is called anywhere; everything injectable is injected.
"""

from __future__ import annotations

import json

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.codebase_map import CodebaseMap
from multi_llm.interview import InterviewAborted, conduct_interview
from multi_llm.memory import PersistentMemory
from multi_llm.persistence import SessionLog
from multi_llm.probes import probe_models, render_probe_report
from multi_llm.repo_scan import scan_repo, seed_map
from multi_llm.scoreboard import build_scoreboard, render_scoreboard
from multi_llm.session import Session, SessionConfig


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# -- repo scan: the existing-codebase entry ------------------------------------


def _fake_repo(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "node_modules" / "junk").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hi')\n")
    (root / "src" / "util.py").write_text("x = 1\n")
    (root / "tests" / "test_main.py").write_text("def test(): pass\n")
    (root / "node_modules" / "junk" / "dep.js").write_text("ignored\n")
    (root / "pyproject.toml").write_text("[project]\nname='p'\n")
    (root / "README.md").write_text("# Proj\n\nA thing.\n")
    return root


def test_scan_reads_the_shape_and_skips_dependency_dirs(tmp_path):
    report = scan_repo(_fake_repo(tmp_path))
    assert report.has_code
    assert report.languages.get("Python") == 3
    assert "JavaScript" not in report.languages  # node_modules skipped
    assert "pyproject.toml" in report.manifests
    assert report.check_command == ["python", "-m", "pytest", "-q"]
    assert "# Proj" in report.readme_head
    assert "src" in report.top_dirs and "node_modules" not in report.top_dirs


def test_scan_of_an_empty_directory_is_quietly_empty(tmp_path):
    report = scan_repo(tmp_path / "nothing-here")
    assert not report.has_code
    assert report.summary() == ""


def test_seeding_the_map_is_idempotent_with_scan_provenance(tmp_path):
    repo = _fake_repo(tmp_path)
    cmap = CodebaseMap(tmp_path / "map.jsonl")
    first = seed_map(scan_repo(repo), cmap)
    assert first > 0
    assert all(n.author == "scan" for n in cmap.notes)
    assert seed_map(scan_repo(repo), cmap) == 0  # unchanged repo adds nothing


# -- the interview -------------------------------------------------------------


def test_interview_asks_then_produces_the_goal(tmp_path):
    replies = iter([
        "Who is the application for?",
        "GOAL: Build a recipe box for one home cook; day one is add and "
        "search; sharing can wait; runs locally.",
    ])
    asked = []

    goal = conduct_interview(
        "let's build an application",
        invoke=lambda model, prompt: next(replies),
        ask=lambda q: asked.append(q) or "just me, a home cook",
    )
    assert goal.startswith("Build a recipe box")
    assert asked == ["Who is the application for?"]


def test_interview_gets_the_scan_and_is_told_not_to_reask_it(tmp_path):
    prompts = []

    def invoke(model, prompt):
        prompts.append(prompt)
        return "GOAL: Improve the error handling in this Python service."

    conduct_interview(
        "improve error handling",
        invoke=invoke,
        ask=lambda q: "answer",
        context="Existing codebase at /x: 300 files.\nLanguages: Python (250).",
    )
    assert "Existing codebase at /x" in prompts[0]
    assert "Do not ask anything this scan answers" in prompts[0]


def test_interview_refuses_to_build_a_goal_on_silence():
    with pytest.raises(InterviewAborted):
        conduct_interview(
            "build something",
            invoke=lambda m, p: "What should it do?",
            ask=lambda q: "",
        )


def test_interview_budget_forces_a_goal_from_what_it_has():
    calls = {"n": 0}

    def invoke(model, prompt):
        calls["n"] += 1
        if "question budget is spent" in prompt:
            return "GOAL: Build the thing with assumed defaults."
        return f"Question {calls['n']}?"

    goal = conduct_interview(
        "build the thing", invoke=invoke, ask=lambda q: "an answer",
        max_rounds=3,
    )
    assert goal == "Build the thing with assumed defaults."


# -- persistence and resume ----------------------------------------------------


def _run_one_task(store, log_path):
    class Rec:
        def __call__(self, model, prompt, system=None):
            if "Name the next wave of tasks" in prompt:
                return "TASK general simple: build the loader"
            if "The task is finished" in prompt:
                return "SUMMARY: built the loader\nREASONING: simplest path"
            return f"[{model}] output"

    s = Session(
        "Build a JSON parser", store, Rec(),
        config=SessionConfig(session_log=SessionLog(log_path)),
    )
    s.run_task(s.next_wave()[0])
    return s


def test_ledger_entries_and_rulings_survive_the_process(tmp_path, store):
    log_path = tmp_path / "ledger.jsonl"
    first = _run_one_task(store, log_path)
    first._add_ruling("Q: SQLite or Postgres? -- A: SQLite")

    fresh = PersistentMemory("Build a JSON parser", store)
    slog = SessionLog(log_path)
    assert slog.restore_into(fresh) == 1
    assert fresh.ledger.entries[0].task_id == "t1"
    assert fresh.ledger.entries[0].summary == "built the loader"
    assert fresh.ledger.entries[0].refs  # artifact pointers survived
    assert "SQLite" in fresh.ledger.rulings[0]


def test_a_resumed_session_numbers_onward_and_can_redo_old_work(tmp_path, store):
    log_path = tmp_path / "ledger.jsonl"
    _run_one_task(store, log_path)

    class Rec:
        def __call__(self, model, prompt, system=None):
            return f"[{model}] output"

    resumed = Session("Build a JSON parser", store, Rec())
    restored = resumed.restore(SessionLog(log_path))
    assert restored == 1
    assert resumed._task_counter == 1  # next task is t2, not a second t1
    redo = resumed._reissue("t1", "the loader ignores encodings")
    assert redo is not None and redo.task_id == "t1-r1"
    assert "the loader ignores encodings" in redo.description


def test_restore_refuses_a_ledger_that_already_has_entries(tmp_path, store):
    log_path = tmp_path / "ledger.jsonl"
    live = _run_one_task(store, log_path)
    with pytest.raises(RuntimeError, match="already has entries"):
        SessionLog(log_path).restore_into(live.memory)


def test_a_torn_final_line_does_not_poison_the_log(tmp_path, store):
    log_path = tmp_path / "ledger.jsonl"
    _run_one_task(store, log_path)
    with log_path.open("a") as fh:
        fh.write('{"type": "entry", "task_id": "t2", "au')  # crash mid-write
    fresh = PersistentMemory("Build a JSON parser", store)
    assert SessionLog(log_path).restore_into(fresh) == 1


def test_goal_storage_and_rotation(tmp_path):
    slog = SessionLog(tmp_path / "ledger.jsonl")
    slog.record_goal("Build a recipe box.")
    slog.append_ruling("Q: x -- A: y")
    assert slog.stored_goal() == "Build a recipe box."
    archive = slog.rotate()
    assert archive.exists() and not slog.exists()
    assert slog.stored_goal() is None


# -- the no-stake completion judge ---------------------------------------------


def test_an_unmet_verdict_sends_the_run_back_once(store):
    class Rec:
        def __init__(self):
            self.judge_calls = 0
            self.waves = [
                "TASK general simple: build the parser",
                "DONE",
                "TASK general simple: add the error paths",
                "DONE",
            ]

        def __call__(self, model, prompt, system=None):
            if "Name the next wave of tasks" in prompt:
                return self.waves.pop(0)
            if "You are a completion judge" in prompt:
                self.judge_calls += 1
                return ("UNMET: the goal names error handling; no task "
                        "touched it" if self.judge_calls == 1 else "MET")
            if "The task is finished" in prompt:
                return "SUMMARY: done\nREASONING: fine"
            return f"[{model}] output"

    rec = Rec()
    s = Session("Build a parser with error handling", store, rec,
                config=SessionConfig(done_judge="claude:haiku"))
    summaries = s.run()
    assert len(summaries) == 2  # the veto bought a second round of work
    assert rec.judge_calls == 2
    assert any("completion judge" in r for r in s.memory.ledger.rulings)


def test_a_second_done_stands_even_over_the_judge(store):
    class Rec:
        def __call__(self, model, prompt, system=None):
            if "Name the next wave of tasks" in prompt:
                return "DONE"
            if "You are a completion judge" in prompt:
                return "UNMET: never satisfied"
            return f"[{model}] output"

    s = Session("Build it", store, Rec(),
                config=SessionConfig(done_judge="claude:haiku"))
    assert s.run() == []  # ends despite the judge; objection is in the record


def test_no_judge_configured_means_done_is_done(store):
    calls = []

    def rec(model, prompt, system=None):
        calls.append(model)
        return "DONE"

    s = Session("Build it", store, rec)
    assert s.run() == []
    assert len(calls) == 1  # exactly one orchestrator call, no judge call


# -- probes --------------------------------------------------------------------


def test_probe_uses_the_injected_runner_and_reports_both_ways():
    keys = ["claude:opus", "claude:haiku"]

    def run(key, prompt):
        if key == "claude:haiku":
            raise RuntimeError("model not found: haiku-nope")
        return "OK"

    results = probe_models(keys, run=run)
    assert [r.ok for r in results] == [True, False]
    report = render_probe_report(results)
    assert "1/2 models answered" in report
    assert "model not found" in report


def test_probe_covers_every_routing_structure():
    from multi_llm.probes import system_model_keys

    keys = system_model_keys()
    assert "claude:fable" in keys       # orchestrator chain
    assert "openai:gpt-5.6-sol" in keys  # brain trust
    assert "claude:haiku" in keys        # control plane / workers
    assert "grok:grok-4.20" in keys      # escalation target
    assert len(keys) == len(set(keys))


# -- the scoreboard ------------------------------------------------------------


def test_scoreboard_counts_reviews_and_recheck_outcomes(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.put("BLOCKING: the cache is never invalidated", kind="review:openai:gpt-5.6-sol", author="openai:gpt-5.6-sol")
    store.put("NO FINDINGS", kind="review:claude:opus", author="claude:opus")
    store.put("RESOLVED", kind="recheck:openai:gpt-5.6-sol", author="openai:gpt-5.6-sol")
    store.put("the draft itself", kind="draft", author="grok:grok-4.6")

    stats = build_scoreboard(store.root)
    sol = stats["openai:gpt-5.6-sol"]
    assert sol.reviews == 1 and sol.blocking_raised == 1
    assert sol.rechecks == 1 and sol.rechecks_resolved == 1
    opus = stats["claude:opus"]
    assert opus.reviews == 1 and opus.no_findings == 1
    assert "grok:grok-4.6" not in stats  # drafts are not reviews

    rendered = render_scoreboard(stats)
    assert "GPT-5.6 Sol" in rendered
    assert "accepted the fix in 1 (100%)" in rendered


def test_scoreboard_on_an_empty_store_says_so(tmp_path):
    assert "No reviewer activity" in render_scoreboard(
        build_scoreboard(tmp_path / "empty")
    )


def test_rechecks_are_now_kept_as_artifacts(store):
    """The session files recheck verdicts under the reviewer's real name --
    the scoreboard's input."""
    class Rec:
        def __call__(self, model, prompt, system=None):
            if "Name the next wave of tasks" in prompt:
                return "TASK general standard: build it"
            if "contributing an independent read" in prompt:
                return "BLOCKING: it ignores empty input"
            if "Check only your BLOCKING findings" in prompt:
                return "RESOLVED"
            if "The task is finished" in prompt:
                return "SUMMARY: built\nREASONING: fine"
            return f"[{model}] output"

    s = Session("Build it", store, Rec())
    s.run_task(s.next_wave()[0])
    kinds = [json.loads(p.read_text())["kind"] for p in store.root.glob("*.json")]
    assert any(k.startswith("recheck:") for k in kinds)
