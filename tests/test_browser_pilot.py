"""Tests for the browser pilot: a scripted model driving a real page."""

from __future__ import annotations

import json

import pytest

from multi_llm.browser_pilot import PilotRun, drive, pilot_model
from multi_llm.registry import Capability, resolve
from multi_llm.task_kinds import DIFFICULTY_LADDER

playwright = pytest.importorskip("playwright.sync_api")

OPUS = "claude:opus"


@pytest.fixture(scope="module")
def app_page(tmp_path_factory):
    """A tiny app: a button that reveals a message when clicked."""
    root = tmp_path_factory.mktemp("app")
    page = root / "app.html"
    page.write_text(
        "<!doctype html><html><head><title>Tiny App</title></head><body>"
        "<button id='go' onclick=\"document.getElementById('out').textContent="
        "'IT WORKED'\">Go</button><div id='out'></div>"
        "</body></html>",
        encoding="utf-8",
    )
    return page


class ScriptedPilot:
    """A fake model that replies with a fixed sequence of actions."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.prompts = []

    def __call__(self, model, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.actions.pop(0))


# -- pilot selection ----------------------------------------------------------


def test_complex_flows_get_the_ladders_top_rung():
    assert pilot_model() == DIFFICULTY_LADDER["complex"]


def test_routine_automation_downshifts_to_a_cheap_coder():
    key = pilot_model(routine=True)
    spec = resolve(key)
    assert Capability.CHEAP in spec.caps and Capability.CODE in spec.caps


# -- the driven loop ----------------------------------------------------------


def test_a_pilot_can_click_read_and_declare_done(app_page, tmp_path):
    pilot = ScriptedPilot([
        {"action": "click", "selector": "#go"},
        {"action": "read"},
        {"action": "done", "reason": "the page shows IT WORKED"},
    ])
    run = drive(pilot, goal="press Go and confirm the result",
                start_url=str(app_page), out_dir=tmp_path)
    assert run.success and run.ended == "done"
    assert any("IT WORKED" in s.observation for s in run.steps)
    assert run.screenshot_path.endswith("final.png")


def test_the_goal_is_recited_in_every_pilot_prompt(app_page, tmp_path):
    pilot = ScriptedPilot([
        {"action": "read"},
        {"action": "done", "reason": "seen"},
    ])
    drive(pilot, goal="UNIQUE-GOAL-MARKER", start_url=str(app_page),
          out_dir=tmp_path)
    assert all("UNIQUE-GOAL-MARKER" in p for p in pilot.prompts)
    assert all(p.rstrip().endswith("Next action:") for p in pilot.prompts)


def test_an_identical_repeated_action_stops_the_run(app_page, tmp_path):
    pilot = ScriptedPilot([
        {"action": "click", "selector": "#go"},
        {"action": "click", "selector": "#go"},
    ])
    run = drive(pilot, goal="loop forever", start_url=str(app_page),
                out_dir=tmp_path)
    assert not run.success
    assert run.ended == "repeated-action"


def test_a_failed_action_comes_back_as_an_observation(app_page, tmp_path):
    pilot = ScriptedPilot([
        {"action": "click", "selector": "#does-not-exist"},
        {"action": "abort", "reason": "no such control"},
    ])
    run = drive(pilot, goal="click a ghost", start_url=str(app_page),
                out_dir=tmp_path)
    assert any("action failed" in s.observation for s in run.steps)
    assert run.ended == "abort" and not run.success


def test_garbage_replies_do_not_crash_the_loop(app_page, tmp_path):
    class Confused:
        def __init__(self):
            self.n = 0

        def __call__(self, model, prompt):
            self.n += 1
            if self.n == 1:
                return "I think I should probably click the button?"
            return json.dumps({"action": "done", "reason": "ok"})

    run = drive(Confused(), goal="g", start_url=str(app_page), out_dir=tmp_path)
    assert run.success
    assert any("not a single JSON action" in s.observation for s in run.steps)


def test_the_step_budget_is_a_hard_stop(app_page, tmp_path):
    pilot = ScriptedPilot([{"action": "read"}, {"action": "wait", "ms": 10}] * 10)
    run = drive(pilot, goal="dawdle", start_url=str(app_page),
                out_dir=tmp_path, max_steps=4)
    assert run.ended == "step-budget"
    assert len(run.steps) == 4


def test_the_record_renders_as_evidence(app_page, tmp_path):
    pilot = ScriptedPilot([{"action": "done", "reason": "trivially"}])
    run = drive(pilot, goal="nothing", start_url=str(app_page), out_dir=tmp_path)
    block = run.render()
    assert "pilot:" in block and "outcome: success" in block


def test_an_explicit_model_override_wins(app_page, tmp_path):
    pilot = ScriptedPilot([{"action": "done", "reason": "ok"}])
    run = drive(pilot, goal="g", start_url=str(app_page), out_dir=tmp_path,
                model="grok:grok-4.6")
    assert run.pilot == "grok:grok-4.6"


def test_pilot_run_defaults_are_safe():
    run = PilotRun(goal="g", pilot=OPUS)
    assert not run.success and run.steps == []
