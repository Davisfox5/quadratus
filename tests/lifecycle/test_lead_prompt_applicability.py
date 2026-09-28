"""The lead prompt's design/capture instructions against the task contract
(map P3.4 follow-up; O-NEXT-01 on #25).

``Session._lead_prompt`` chooses the design instruction from live
configuration (``design_self_verify``, ``is_design_task`` and
``_harness_captures``) each time a lead prompt is built. The task contract
fixes ``Required.design_evidence`` at dispatch. When configuration differs
while the prompt is built, the lead is told one thing and checked against
another, and nothing is recorded: the prompt calls no ``_required``, so no
mismatch reaches the task's outcome.

The drift is a synthetic write that holds only while the prompt is built,
so every later stage sees the dispatch configuration: a controller
invariant, not observed behaviour. Each seam has a passing test that pins
today's behaviour and a strict xfail for the contract-following
expectation. The controls at the end are ordinary journeys that must keep
their current prompt text after any correction, including the no-project
design task, whose contract says ``"none"`` while its lead is still asked to
capture. See docs/review/lead-prompt-applicability-seam.md.
"""

import dataclasses

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.outcome import TaskOutcome
from quadratus.preview import CaptureProfile
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec
from tests.lifecycle import harness as H
from tests.lifecycle.test_contract_applicability import _run, _t1
from tests.lifecycle.test_lifecycle_matrix import _edits_and_captures

SELF = "so you verify it yourself before you finish"
COMMAND = "-m quadratus.design_evidence"
HARNESS = "The harness itself starts the preview"
PROFILE = CaptureProfile(preview=("true",), origin="http://127.0.0.1:5000")
CAPTURE = {"path": "/index.html", "steps": []}

SEAM = ("_lead_prompt reads live design_self_verify / _harness_captures after dispatch; "
        "the contract's design_evidence is fixed at dispatch (O-NEXT-01)")


def _drift_while_prompting(monkeypatch, **changes):
    """Apply ``changes`` to the live config only while a t1 lead prompt is
    built; restore the dispatch config before anything else reads it."""
    build = Session._lead_prompt

    def drifted(self, spec, **kw):
        if spec.task_id != "t1":
            return build(self, spec, **kw)
        before = self.config
        self.config = dataclasses.replace(before, **changes)
        try:
            return build(self, spec, **kw)
        finally:
            self.config = before
    monkeypatch.setattr(Session, "_lead_prompt", drifted)


def _dispatch_t1_with(monkeypatch, **changes):
    """t1 dispatched under ``changes`` (the contract is built from them)."""
    run_task = Session.run_task

    def dispatched(self, spec):
        if spec.task_id == "t1":
            self.config = dataclasses.replace(self.config, **changes)
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", dispatched)


def _t1_lead_prompts(replay):
    prompts = [c.prompt for c in replay.of("lead") if c.task == "t1"]
    assert prompts, "t1 had a lead call"
    return prompts


def _evidence_field(replay):
    return _t1(replay)["contract"]["required"]["design_evidence"]


# -- design_self_verify drift, whole controller ---------------------------------------

def _verify_off_while_prompting(tmp_path, monkeypatch):
    _drift_while_prompting(monkeypatch, design_self_verify=False)
    return _run(tmp_path, monkeypatch, _edits_and_captures)


def test_today_a_required_capture_is_left_out_of_the_prompt_when_the_setting_drifts_off(tmp_path, monkeypatch):
    replay = _verify_off_while_prompting(tmp_path, monkeypatch)
    assert _evidence_field(replay) == "self", "the contract requires self-captured renders"
    assert not any(SELF in p or COMMAND in p for p in _t1_lead_prompts(replay)), \
        "today the lead is not told to capture"
    assert _t1(replay)["mismatches"] == [], "and nothing about the prompt is recorded"
    assert _t1(replay)["evidence"]["verified"] is True, "t1 still closes as verified design work"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_a_required_capture_stays_in_the_prompt_when_the_setting_drifts_off(tmp_path, monkeypatch):
    replay = _verify_off_while_prompting(tmp_path, monkeypatch)
    assert all(SELF in p and COMMAND in p for p in _t1_lead_prompts(replay))


def _verify_on_while_prompting(tmp_path, monkeypatch):
    _dispatch_t1_with(monkeypatch, design_self_verify=False)
    _drift_while_prompting(monkeypatch, design_self_verify=True)
    return _run(tmp_path, monkeypatch, _edits_and_captures)


def test_today_a_disabled_capture_is_asked_for_when_the_setting_drifts_on(tmp_path, monkeypatch):
    replay = _verify_on_while_prompting(tmp_path, monkeypatch)
    assert _evidence_field(replay) == "disabled"
    assert all(SELF in p and COMMAND in p for p in _t1_lead_prompts(replay)), \
        "today the lead is asked for renders the contract will not check"
    assert "disabled by the operator" in _t1(replay)["evidence"]["problem"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_a_disabled_capture_is_not_asked_for_when_the_setting_drifts_on(tmp_path, monkeypatch):
    replay = _verify_on_while_prompting(tmp_path, monkeypatch)
    assert not any(SELF in p or COMMAND in p for p in _t1_lead_prompts(replay))


# -- harness capture availability drift, controller level ----------------------------
# A whole run with a capture profile ends in a real preview and browser
# capture; these build the contract and the prompt on a real Session instead.

def _session(tmp_path, *, project=True, profile=None, verify=True):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE",
                   config=SessionConfig(project=root if project else None, allow_writes=project,
                                        capture_profile=profile, design_self_verify=verify))


def _design_spec(capture=CAPTURE):
    return TaskSpec("t1", "Add an Import button to the page.", kind="frontend",
                    scope=TaskScope(permitted_paths=["templates/index.html"], capture=capture))


def _dispatch(session, spec):
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._contract = session._build_contract(spec, outcome)
    return session._contract.required.design_evidence


def _prompt_under(session, spec, **changes):
    before = session.config
    session.config = dataclasses.replace(before, **changes)
    try:
        return session._lead_prompt(spec)
    finally:
        session.config = before


def _harness_then_no_profile(tmp_path):
    session, spec = _session(tmp_path, profile=PROFILE), _design_spec()
    assert _dispatch(session, spec) == "harness"
    return session, _prompt_under(session, spec, capture_profile=None)


def test_today_a_harness_task_is_told_to_capture_itself_when_the_profile_drifts_away(tmp_path):
    session, prompt = _harness_then_no_profile(tmp_path)
    assert SELF in prompt and COMMAND in prompt and HARNESS not in prompt, \
        "today the lead is told to start a server and run the capture"
    assert session._outcome.mismatches == [], "and nothing is recorded"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_a_harness_task_keeps_the_harness_instruction_when_the_profile_drifts_away(tmp_path):
    _, prompt = _harness_then_no_profile(tmp_path)
    assert HARNESS in prompt and COMMAND not in prompt


def _self_then_profile(tmp_path):
    session, spec = _session(tmp_path), _design_spec()
    assert _dispatch(session, spec) == "self"
    return session, _prompt_under(session, spec, capture_profile=PROFILE)


def test_today_a_self_capture_task_is_told_not_to_capture_when_a_profile_drifts_in(tmp_path):
    session, prompt = _self_then_profile(tmp_path)
    assert HARNESS in prompt and COMMAND not in prompt, \
        "today the lead is told the harness captures, which the contract will not do"
    assert session._outcome.mismatches == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=SEAM)
def test_a_self_capture_task_keeps_the_capture_command_when_a_profile_drifts_in(tmp_path):
    _, prompt = _self_then_profile(tmp_path)
    assert SELF in prompt and COMMAND in prompt and HARNESS not in prompt


# -- controls: ordinary journeys, no drift, text any correction must keep ------------

def test_an_ordinary_design_task_is_told_to_capture_itself(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    assert _evidence_field(replay) == "self"
    assert all(SELF in p and COMMAND in p and HARNESS not in p for p in _t1_lead_prompts(replay))
    assert _t1(replay)["mismatches"] == []


def test_a_non_design_task_gets_no_design_instruction(tmp_path, monkeypatch):
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: DECL_T1), files=FILES)
    assert _evidence_field(replay) == "none"
    assert not any(SELF in p or COMMAND in p or HARNESS in p for p in _t1_lead_prompts(replay))


def test_verification_disabled_throughout_gets_no_design_instruction(tmp_path, monkeypatch):
    _dispatch_t1_with(monkeypatch, design_self_verify=False)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    assert _evidence_field(replay) == "disabled"
    assert not any(SELF in p or COMMAND in p or HARNESS in p for p in _t1_lead_prompts(replay))


def test_a_harness_design_task_gets_the_harness_instruction(tmp_path):
    session, spec = _session(tmp_path, profile=PROFILE), _design_spec()
    assert _dispatch(session, spec) == "harness"
    prompt = session._lead_prompt(spec)
    assert HARNESS in prompt and "http://127.0.0.1:5000/index.html" in prompt and COMMAND not in prompt


def test_a_design_task_with_no_project_is_still_told_to_capture(tmp_path):
    """The contract says "none" (no writable project), yet today's prompt
    asks for renders. A correction that keys the prompt on
    ``design_evidence`` alone would drop this text; it must not."""
    session, spec = _session(tmp_path, project=False), _design_spec(capture=None)
    assert _dispatch(session, spec) == "none"
    prompt = session._lead_prompt(spec)
    assert SELF in prompt and COMMAND in prompt and HARNESS not in prompt
    assert session._outcome.mismatches == []


def test_a_design_task_with_no_project_and_verification_off_gets_no_instruction(tmp_path):
    session, spec = _session(tmp_path, project=False, verify=False), _design_spec(capture=None)
    assert _dispatch(session, spec) == "none"
    prompt = session._lead_prompt(spec)
    assert not any(marker in prompt for marker in (SELF, COMMAND, HARNESS))
