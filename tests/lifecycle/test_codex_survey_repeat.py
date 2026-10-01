"""Codex review of 1995d02 (#41), P2: a no-progress cap repeat is judged on
the harness's signature, never on the model's changing prose."""
import pytest

from quadratus.config import Settings
from quadratus.session import SurveyConfig
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
from tests.lifecycle.test_survey_run import _continue, _plan


@pytest.mark.parametrize('vary_words', [False, True])
def test_no_progress_caps_repeat_despite_different_model_words(tmp_path, monkeypatch, vary_words):
    def lead(call, replay):
        text = f'Still working on {call.task}.' if vary_words else 'Still working.'
        return H.claude_cap(text, num_turns=14)
    plan = _plan(DECL_T1, _continue('t1', 'retry'), _continue('t2', 'retry again'))
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=plan, lead=lead),
                   files=FILES, max_tasks=4, settings=Settings(backend='cli', lead_max_turns=14),
                   survey=SurveyConfig(recovery_tasks=3), record_complete=False)
    assert [c.task for c in replay.of('lead')] == ['t1', 't2'], replay.result.error
    assert replay.result.error.startswith('SurveyRepeatStop'), replay.result.error
