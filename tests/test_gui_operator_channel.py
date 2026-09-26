"""The Project UI answers the planner's ASK and carries the neutral switch."""

import threading

from quadratus import gui
from quadratus.config import Settings


def test_a_question_reaches_the_ui_and_the_answer_reaches_the_run(tmp_path, monkeypatch):
    seen = {}

    class Result:
        report, diff, run_dir = "report", "", tmp_path

    def fake_run_project(goal, folder, settings, *, ask_operator=None, progress=None, **kw):
        seen["neutral"] = settings.neutral_preferences
        progress("asking")
        seen["answer"] = ask_operator("R2: which screen gets the button?")
        return Result()

    monkeypatch.setattr("quadratus.project_run.run_project", fake_run_project)
    channel = gui.OperatorChannel(timeout=10)
    outputs = []

    def drive():
        for out in gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings(),
                                      channel=channel, neutral=True):
            outputs.append(out[0])
            if "The planner asks" in out[0] and "answered" not in seen:
                seen["answered"] = channel.answer("the tagging screen")

    worker = threading.Thread(target=drive)
    worker.start()
    worker.join(20)
    assert not worker.is_alive()
    assert any("R2: which screen" in o for o in outputs)
    assert seen["answered"] == "Answer sent; the run continues."
    assert seen["answer"] == "the tagging screen" and seen["neutral"] is True
    assert outputs[-1] == "report"


def test_an_unanswered_question_stops_incomplete_rather_than_guessing():
    import pytest

    from quadratus.session import OperatorInputNeeded
    channel = gui.OperatorChannel(timeout=0.05)
    with pytest.raises(OperatorInputNeeded):
        channel.ask("R2?")
    assert channel.answer("late") == "No question is waiting for an answer."
