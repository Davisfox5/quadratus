"""The lead prompt's design/capture instruction follows the task contract
(map P3.4; O-NEXT-01 8ab7333, Codex ruling 5864880543).

``Session._lead_prompt`` chose the design instruction from live
configuration (``design_self_verify``, ``is_design_task`` and
``_harness_captures``) each time a lead prompt was built, while the contract
fixed ``design_evidence`` at dispatch. It now reads
``Required.design_instruction`` ("harness" | "self" | "none"), computed at
dispatch with the prompt's own predicate, and a "harness" instruction names
the page fixed at dispatch (``TaskContract.capture_page``). The live readings
are recorded beside them as mismatches.

The drift is a synthetic write that holds only while the prompt is built,
so every later stage sees the dispatch configuration: a controller
invariant, not observed behaviour. The controls at the end are ordinary
journeys that keep their prompt text, including the no-project design task
and a security task with UI scope, whose contracts say evidence ``"none"``
while their leads are still asked to capture. See
docs/review/lead-prompt-applicability-seam.md.
"""

import dataclasses

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
    return _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)


def test_a_required_capture_stays_in_the_prompt_when_the_setting_drifts_off(tmp_path, monkeypatch):
    replay = _verify_off_while_prompting(tmp_path, monkeypatch)
    assert all(SELF in p and COMMAND in p for p in _t1_lead_prompts(replay))
    assert "design_instruction: contract 'self', legacy 'none'" in _t1(replay)["mismatches"]
    assert not replay.result.completed


def _verify_on_while_prompting(tmp_path, monkeypatch):
    _dispatch_t1_with(monkeypatch, design_self_verify=False)
    _drift_while_prompting(monkeypatch, design_self_verify=True)
    return _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)


def test_a_disabled_capture_is_not_asked_for_when_the_setting_drifts_on(tmp_path, monkeypatch):
    replay = _verify_on_while_prompting(tmp_path, monkeypatch)
    assert _evidence_field(replay) == "disabled"
    assert not any(SELF in p or COMMAND in p for p in _t1_lead_prompts(replay))
    assert "design_instruction: contract 'none', legacy 'self'" in _t1(replay)["mismatches"]
    assert not replay.result.completed


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


def test_a_harness_task_keeps_the_harness_instruction_when_the_profile_drifts_away(tmp_path):
    session, prompt = _harness_then_no_profile(tmp_path)
    assert HARNESS in prompt and COMMAND not in prompt
    assert "http://127.0.0.1:5000/index.html" in prompt, "the page fixed at dispatch, not the live profile"
    assert session._outcome.mismatches == [
        "design_instruction: contract 'harness', legacy 'self'",
        "capture_page: contract 'http://127.0.0.1:5000/index.html', legacy None"]


def _self_then_profile(tmp_path):
    session, spec = _session(tmp_path), _design_spec()
    assert _dispatch(session, spec) == "self"
    return session, _prompt_under(session, spec, capture_profile=PROFILE)


def test_a_self_capture_task_keeps_the_capture_command_when_a_profile_drifts_in(tmp_path):
    session, prompt = _self_then_profile(tmp_path)
    assert SELF in prompt and COMMAND in prompt and HARNESS not in prompt
    assert session._outcome.mismatches == ["design_instruction: contract 'self', legacy 'harness'"]


def test_a_harness_task_keeps_its_page_when_the_origin_drifts(tmp_path):
    """Codex 5864880543 (1): a different non-null origin while prompting."""
    session, spec = _session(tmp_path, profile=PROFILE), _design_spec()
    assert _dispatch(session, spec) == "harness"
    moved = dataclasses.replace(PROFILE, origin="http://127.0.0.1:6000")
    prompt = _prompt_under(session, spec, capture_profile=moved)
    assert HARNESS in prompt and "http://127.0.0.1:5000/index.html" in prompt and "6000" not in prompt
    assert session._outcome.mismatches == [
        "capture_page: contract 'http://127.0.0.1:5000/index.html', legacy 'http://127.0.0.1:6000/index.html'"]


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


def test_a_security_task_with_ui_scope_is_still_told_to_capture(tmp_path):
    """Codex 5864880543 (2): a security task's evidence is "none", yet its
    lead keeps today's self-capture instruction."""
    from quadratus.session import WorkClass
    session = _session(tmp_path)
    spec = dataclasses.replace(_design_spec(capture=None), work_class=WorkClass.SECURITY)
    assert _dispatch(session, spec) == "none"
    prompt = session._lead_prompt(spec)
    assert SELF in prompt and COMMAND in prompt and HARNESS not in prompt
    assert session._outcome.mismatches == []
    assert getattr(session._contract.required, "design_instruction", "absent") == "self"


# -- the record ---------------------------------------------------------------------

def _record(required, **contract):
    from tests.test_outcome import _closed
    task = _closed()
    task.contract["required"] = required
    task.contract.update(contract)
    return task


BASE = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
            settlement=False, design_collaboration_applicable=False, security_verdict="none", operator_limits="none")


def test_an_older_record_without_the_instruction_says_so():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE))) == [
        "t1.contract.required.design_instruction (absent: recorded before this field existed)"]


def test_an_unknown_instruction_is_missing():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE, design_instruction="capture"))) == [
        "t1.contract.required.design_instruction"]


def test_a_harness_instruction_without_its_page_is_missing():
    from quadratus.outcome import missing_facts
    assert missing_facts(_record(dict(BASE, design_instruction="harness"), capture_page=None)) == [
        "t1.contract.capture_page"]
    assert missing_facts(_record(dict(BASE, design_instruction="harness"),
                                 capture_page="http://127.0.0.1:5000/")) == []
