"""Leftovers of the 2026-11-11 merge: prompts, comments and dead code that
still stated one line's rule after the other line's rule won.

Each test names the conformance finding it closes (leftovers:N). All
offline: no vendor CLI, no model call."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig

ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    """A source file with every run of whitespace collapsed to one space."""
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def _session(tmp_path, reply="R1: MET - app.py\nR5: MET - app.py"):
    root = tmp_path / "project"
    root.mkdir(parents=True)
    (root / "app.py").write_text("x = 1\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True))
    ledger = session.memory.ledger
    ledger.requirements.update({"R1": "typing filters the cards", "R5": "no console errors while typing"})
    ledger.requirement_status.update({"R1": "covered by t1", "R5": "covered by t3"})
    session.requirement_reviews.append(dict(reviewer="openai:sol", result="reviewed"))
    session._auditor = lambda: "openai:sol"
    session._invoke_model = lambda key, prompt, **kw: reply
    return session


def _report(session, task, covers, line):
    session._active_spec = SimpleNamespace(task_id=task)
    session._current_covers = list(covers)
    session._record_not_run("openai:sol", "lead", line)
    session._active_spec, session._current_covers = None, []


# -- leftovers:2: the DONE refusal states one route per NOT RUN item ---------------------

def test_the_done_refusal_never_says_nothing_lifts_an_item_its_route_line_says_how_to_lift(tmp_path):
    session = _session(tmp_path)
    _report(session, "t3", ["R5"], "NOT RUN: the browser scenario - no playwright")
    assert session._requirements_satisfied() is False
    refusal = session._done_refusal
    # The development line's route for the item is in the refusal ...
    assert "'DISCHARGES: O1'" in refusal and "'RUNS: O1 - file::[Class::]test_case'" in refusal
    # ... and nothing beside it tells the planner to leave the item alone.
    assert "No task lifts the report" not in refusal
    assert "stands for the rest of this run" not in refusal
    assert "Name a task that fixes them (with COVERS)." in refusal


def test_a_case_bound_item_is_told_its_cases_and_only_an_item_with_no_id_is_stuck(tmp_path):
    session = _session(tmp_path)
    bound = dict(task="t2", item="P3", why="no browser", requirements=["R5"], kind="not_run", oid="O4",
                 cases=["tests/test_a.py::test_x"], binding=dict(kind="scenario"), executable=True)
    orphan = dict(task="t2", item="the whole suite", why="", requirements=["R1"], kind="not_run",
                  cases=[], binding=dict(kind="none"), executable=False)
    session._standing_not_run = lambda: [bound, orphan]
    session._bound_cases_gone = lambda entry: []
    text, stuck = session._not_run_remedy(["R1", "R5"])
    assert ("R5: task t2 reported NOT RUN: P3. Lifted only when a later required check passes with "
            "tests/test_a.py::test_x executed and passed") in text
    assert stuck == ["R1"], "only the item with no obligation id has no route"
    assert text.rstrip().endswith("No task lifts the report on R1: do not name one for it.")


# -- leftovers:3: the editing instructions agree with NOT_RUN_RULE ----------------------

def test_the_editing_instructions_do_not_call_a_scenario_or_case_item_unmatchable():
    source = _text("quadratus/runtime.py").replace('" "', "")
    assert "NOT_RUN_RULE" in source
    for gone in ("must name the test file it is about", "can never be matched to a later check"):
        assert gone not in source, gone
    # The rule it sits beside names the forms that carry no file path.
    assert "'P<n>' for a declared scenario" in S.NOT_RUN_RULE
    # The sentence's own examples, read by the parser the discharge uses.
    assert S._item_paths("node tests/browser/run.js") == ["tests/browser/run.js"]
    assert S._item_paths("the browser scenario") == []


# -- leftovers:4: no planning text speaks of a size ceiling ----------------------------

def test_the_planning_state_text_does_not_name_a_size_ceiling(tmp_path):
    session = _session(tmp_path)
    session.memory.ledger.requirement_status["R5"] = "open"
    session._max_tasks = 3
    note = session._slots_note()
    assert "Requirements no task has covered yet: R5" in note
    assert "size ceiling" not in note
    assert "the size figure is a guide and stops nothing" in note
    for module in ("session", "session_admission", "session_prompts", "session_seat_prompts", "task_kinds",
                   "baseline"):
        assert "size ceiling" not in _text(f"quadratus/{module}.py"), module


# -- leftovers:5: no model close-out code is left to read as live ----------------------

def test_the_model_close_out_and_its_reply_parser_are_gone_and_the_harness_record_stays():
    from quadratus import reply_parsers
    for name in ("_close_out", "_refused_close_out"):
        assert not hasattr(Session, name), name
    for name in ("_parse_closeout", "_map_entry", "_CLOSEOUT_HEADER", "_CLOSEOUT_STYLING", "_NO_DEAD_ENDS"):
        assert not hasattr(S, name) and not hasattr(reply_parsers, name), name
    for module in ("session", "session_closeout"):
        source = _text(f"quadratus/{module}.py")
        assert '_invocation_role("closeout")' not in source and "The task is finished at this checkpoint" not in source
    # What writes a task's record, and the excerpt the review diff still uses.
    assert callable(Session._task_close_out) and callable(Session._factual_close_out)
    assert S._closeout_excerpt("short", 2000) == "short"


# -- leftovers:8: one measured diff block, and no capture nothing reads ----------------

def test_the_development_lines_own_diff_block_and_its_unread_capture_are_gone(tmp_path):
    assert not hasattr(Session, "_measured_change") and not hasattr(S, "REVIEW_DIFF_CHARS")
    assert not hasattr(_session(tmp_path), "_review_before")
    assert "_review_before" not in _text("quadratus/session.py")
    # The block review seats do get.
    assert callable(Session._review_diff) and callable(Session._recheck_diff)


# -- leftovers:6: operator text promises no summary call and no model close-out --------

def test_the_limits_text_the_cli_help_and_the_readme_promise_no_summary_call():
    from quadratus.cli import build_parser
    from quadratus.run_budget import RunLimits, describe_limits
    text = describe_limits(RunLimits(max_reported_tokens=5_000_000, reserve_tokens_per_call=250_000))
    assert "An ordinary call starts only while at least 250,000 tokens of the total remain." in text
    assert "summary call" not in text and "64,000" not in text
    helped = " ".join(build_parser().format_help().split())
    assert "summary call" not in helped and "no model close-out" not in helped
    assert "The harness writes the close-out on every tier" in helped
    readme = _text("README.md")
    assert "one-turn summary" not in readme.lower() and "no model close-out" not in readme
    assert "the harness writes the close-out on every tier" in readme
