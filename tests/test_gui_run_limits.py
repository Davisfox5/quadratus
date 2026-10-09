"""The Project UI can bound a run: optional Run limits forward a validated
RunLimits and SurveyConfig to the shared runner, the effective allowance is
shown before dispatch and saved with the run, and leaving the controls off is
the run the GUI made before they existed (Codex GUI plan on #35,
2026-10-09)."""

import json

import pytest

from quadratus import gui
from quadratus.config import Settings
from quadratus.run_budget import RunLimits
from quadratus.session import SurveyConfig

DIAGNOSTIC = 'Diagnostic: 90 calls, 5M tokens, 1 hour'


def _fake_runner(monkeypatch, tmp_path, seen):
    class Result:
        report, diff, run_dir = "report", "", tmp_path

    def fake_run_project(goal, folder, settings, **kw):
        seen.update(kw)
        return Result()
    monkeypatch.setattr("quadratus.project_run.run_project", fake_run_project)


def test_the_diagnostic_preset_is_the_frozen_allowance():
    limits, survey = gui.limits_from_form(*gui.preset_values(DIAGNOSTIC))
    assert limits == RunLimits(max_calls=90, max_reported_tokens=5_000_000, wall_seconds=3600,
                               max_concurrent_workers=2, reserve_tokens_per_call=250_000,
                               max_tokens_per_call=1_500_000)
    assert survey == SurveyConfig(recovery_tasks=4)


def test_limits_off_and_no_recovery_is_the_old_run():
    assert gui.limits_from_form(False, 90, 5_000_000, 3600, 2, 250_000, 1_500_000, 0) == (None, None)
    limits, survey = gui.limits_from_form(False, 90, 5_000_000, 3600, 2, 250_000, 1_500_000, 3)
    assert limits is None and survey == SurveyConfig(recovery_tasks=3)


def test_zero_per_call_threshold_means_none_and_gradio_floats_are_whole_numbers():
    limits, _ = gui.limits_from_form(True, 24.0, 500000.0, 900.0, 2.0, 0.0, 0.0, 0.0)
    assert limits == RunLimits()
    assert type(limits.max_calls) is int and limits.max_tokens_per_call is None


@pytest.mark.parametrize("values, named", [
    ((True, 0, 5_000_000, 3600, 2, 0, 0, 0), "Maximum model calls"),
    ((True, 90, None, 3600, 2, 0, 0, 0), "Total reported tokens"),
    ((True, 90, 5_000_000, 1.5, 2, 0, 0, 0), "Time limit"),
    ((True, 90, 5_000_000, 3600, 0, 0, 0, 0), "Parallel workers"),
    ((True, 90, 5_000_000, 3600, 2, -1, 0, 0), "Tokens that must remain"),
    ((True, 90, 5_000_000, 3600, 2, 0, "abc", 0), "Stop after a single call"),
    ((False, 90, 5_000_000, 3600, 2, 0, 0, -2), "Extra recovery tasks"),
    ((True, 90, 1_000, 3600, 2, 250_000, 0, 0), "reserve_tokens_per_call cannot exceed"),
])
def test_a_bad_field_is_refused_by_name(values, named):
    with pytest.raises(ValueError, match=named):
        gui.limits_from_form(*values)


def test_the_survey_allowance_itself_refuses_a_negative_or_non_whole_count():
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError, match="recovery_tasks"):
            SurveyConfig(recovery_tasks=bad)


def test_the_summary_states_every_number_and_never_promises_a_ceiling():
    text = gui.limits_summary(10, *gui.preset_values(DIAGNOSTIC))
    for number in ("Task limit: 10", "90 model calls", "5,000,000 reported", "Time limit 3,600 seconds", "not cut off",
                   "2 parallel", "250,000 tokens", "1,500,000 tokens", "up to 4 extra"):
        assert number in text, number
    assert "not a hard ceiling" in text and "not an invoice" in text
    assert "No shared run allowance" in gui.limits_summary(20, False, 24, 500_000, 900, 2, 0, 0, 0)
    assert "not valid" in gui.limits_summary(20, True, 0, 500_000, 900, 2, 0, 0, 0)
    assert "Task limit" in gui.limits_summary(None, False, 24, 500_000, 900, 2, 0, 0, 0)


def test_run_project_ui_forwards_the_limits_and_shows_them_first(tmp_path, monkeypatch):
    seen = {}
    _fake_runner(monkeypatch, tmp_path, seen)
    limits, survey = gui.limits_from_form(*gui.preset_values(DIAGNOSTIC))
    outputs = list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 10, Settings(),
                                      run_limits=limits, survey=survey))
    assert seen["run_limits"] is limits and seen["survey"] is survey and seen["max_tasks"] == 10
    assert "90 model calls" in outputs[0][0] and outputs[-1][0] == "report"
    seen.clear()
    outputs = list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings()))
    assert seen["run_limits"] is None and seen["survey"] is None
    assert "No shared run allowance" in outputs[0][0]


def _callback(demo, name):
    for block in demo.fns.values():
        if getattr(block.fn, "__name__", None) == name:
            return block.fn
    raise AssertionError(f"no {name} callback")


def test_the_real_run_button_validates_and_forwards_the_form(tmp_path, monkeypatch):
    pytest.importorskip("gradio")
    import gradio as gr
    seen = {}
    _fake_runner(monkeypatch, tmp_path, seen)
    demo = gui.build_interface(Settings())
    labels = {getattr(block, "label", None) for block in demo.blocks.values()}
    assert {"Use run limits", "Fill from preset", "Maximum model calls", "Parallel workers"} <= labels
    run_selected = _callback(demo, "run_selected")
    common = ("goal", str(tmp_path), True, "", "adversarial", 10, "", "", False, "", "", "")
    list(run_selected(*common, *gui.preset_values(DIAGNOSTIC)))
    assert seen["run_limits"] == RunLimits(max_calls=90, max_reported_tokens=5_000_000, wall_seconds=3600,
                                           max_concurrent_workers=2, reserve_tokens_per_call=250_000,
                                           max_tokens_per_call=1_500_000)
    assert seen["survey"] == SurveyConfig(recovery_tasks=4)
    seen.clear()
    with pytest.raises(gr.Error, match="Maximum model calls"):
        list(run_selected(*common, True, 0, 5_000_000, 3600, 2, 0, 0, 0))
    assert seen == {}, "a refused form never reaches the runner"
    list(run_selected(*common, False, 24, 500_000, 900, 2, 0, 0, 0))
    assert seen["run_limits"] is None and seen["survey"] is None


def test_the_runner_saves_the_selected_allowance_before_any_model_call(tmp_path, monkeypatch):
    from quadratus.project_run import run_project
    from quadratus.session import Session

    (tmp_path / 'app.py').write_text('x\n')
    seen = {}

    def run(self, **kwargs):
        seen['saved'] = json.loads(next((tmp_path / '.quadratus' / 'runs').iterdir())
                                   .joinpath('run-limits.json').read_text())
        seen['workers'] = self.config.worker_budget.max_concurrent
        seen['survey'] = self.config.survey
        return None
    monkeypatch.setattr(Session, 'run', run)
    limits, survey = gui.limits_from_form(*gui.preset_values(DIAGNOSTIC))
    notes = []
    result = run_project('g', tmp_path, Settings(backend='cli'), allow_writes=True, max_tasks=10,
                         run_limits=limits, survey=survey, progress=notes.append)
    assert seen['workers'] == 2 and seen['survey'] == SurveyConfig(recovery_tasks=4)
    assert seen['saved']['run_limits']['max_calls'] == 90
    assert seen['saved']['survey_recovery_tasks'] == 4 and seen['saved']['max_tasks'] == 10
    record = json.loads((result.run_dir / 'result.json').read_text())
    assert record['selected_limits'] == seen['saved']
    assert record['budget']['limits']['reserve_tokens_per_call'] == 250_000
    assert any(n.startswith('Run limits: Task limit: 10. At most 90 model calls and 5,000,000') for n in notes)


def test_a_busy_port_suggestion_stays_inside_the_accepted_range(monkeypatch):
    busy = {65535}
    monkeypatch.setattr(gui, "_port_busy", lambda port: port in busy)
    assert gui._free_port_near(65535) == 1024
    busy.update(range(1024, 1100))
    assert gui._free_port_near(65535, tries=200) == 1100
    monkeypatch.setattr(gui, "_port_busy", lambda port: True)
    assert gui._free_port_near(7860) is None
    assert gui._free_port_near(7860, tries=10 ** 6) is None
