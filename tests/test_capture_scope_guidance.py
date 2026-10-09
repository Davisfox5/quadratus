"""The orchestrator is told the capture step schema and the fixture path rule.

Phase-4 rerun on a6c9576 (2026-09-28, run 20260928T094824Z): the orchestrator
declared steps as {"click": ...} objects, spent its one correction on that,
then named the sample its previous task had committed under tests/fixtures/
and stalled on a path rule it had never been shown. Both rules are now in its
instructions, the correction carries them, and a committed sample is valid.
"""
import json

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.session import _CAPTURE_SCOPE_REQUEST, RunStalled, Session, SessionConfig

SCOPE = dict(permitted_paths=["templates/index.html"], intended_result="The preview shows the rows",
             acceptance=["rows appear"], max_lines=20, edits="none")
KEYED = [{"click": "#open"}, {"wait": "#dialog[open]"}]
VALID = [{"action": "click", "selector": "#open"},
         {"action": "file", "selector": "#csv", "path": "tests/fixtures/import_preview_sample.csv"},
         {"action": "wait", "selector": "#rows tr"}]


def _declaration(steps):
    scope = dict(SCOPE, capture={"path": "/", "steps": steps})
    return "KIND: frontend standard\nSCOPE: " + json.dumps(scope) + "\nAudit the preview table."


def _session(tmp_path, replies, capture=True):
    calls = []

    def invoke(model, prompt, **kwargs):
        calls.append(prompt)
        return next(replies)

    session = Session("audit", ArtifactStore(tmp_path / ".quadratus"), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True,
                                           capture_profile=object() if capture else None))
    return session, calls


def test_the_instructions_name_the_step_schema_and_both_fixture_paths():
    assert '{"action": "file", "selector": "<file input selector>", "path": "<sample file>"}' in _CAPTURE_SCOPE_REQUEST
    assert "non-hidden file already in the project" in _CAPTURE_SCOPE_REQUEST
    assert ".quadratus/capture-fixtures/<task id>/<name>" in _CAPTURE_SCOPE_REQUEST


def test_the_orchestrator_prompt_carries_the_capture_rules_when_a_profile_is_set(tmp_path):
    session, calls = _session(tmp_path, iter([_declaration(VALID)]))
    spec = session.next_task()
    assert spec.scope.capture["steps"][1]["path"] == "tests/fixtures/import_preview_sample.csv"
    assert len(calls) == 1 and _CAPTURE_SCOPE_REQUEST in calls[0]


def test_the_orchestrator_prompt_omits_the_capture_rules_without_a_profile(tmp_path):
    session, calls = _session(tmp_path, iter([_declaration([])]), capture=False)
    session.next_task()
    assert _CAPTURE_SCOPE_REQUEST not in calls[0]


def test_the_correction_for_a_capture_error_carries_the_capture_rules(tmp_path):
    session, calls = _session(tmp_path, iter([_declaration(KEYED), _declaration(VALID)]))
    spec = session.next_task()
    assert len(calls) == 2
    assert "CORRECTION REQUIRED: SCOPE capture steps need action click, wait, confirm or file" in calls[1]
    assert _CAPTURE_SCOPE_REQUEST in calls[1], "the correction states every rule the declaration is held to"
    assert spec.scope.capture["steps"][0] == {"action": "click", "selector": "#open"}


def test_a_second_invalid_capture_still_stalls(tmp_path):
    session, _ = _session(tmp_path, iter([_declaration(KEYED), _declaration(KEYED)]))
    with pytest.raises(RunStalled, match="remains invalid after correction"):
        session.next_task()


# -- the lead is told the fixture it must write (diagnostic run 20260930T020711Z) ----

def _harness_lead_prompt(tmp_path, steps):
    from types import SimpleNamespace

    from quadratus.scope import TaskScope
    from quadratus.session import TaskSpec
    from quadratus.task_kinds import TaskKind
    session = Session("audit", ArtifactStore(tmp_path / ".quadratus"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, design_self_verify=True,
                                           capture_profile=SimpleNamespace(origin="http://127.0.0.1:5000")))
    spec = TaskSpec("t2", "Audit the preview table.", kind=TaskKind.FRONTEND,
                    scope=TaskScope(["templates/index.html"], acceptance=["rows appear"], max_lines=20,
                                    review_only=True, capture={"path": "/", "steps": steps}))
    return session._lead_prompt(spec)


def test_the_lead_is_told_to_write_its_own_capture_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr("quadratus.session.Session._harness_captures", lambda self, spec: True)
    text = _harness_lead_prompt(tmp_path, [{"action": "file", "selector": "#csv",
                                            "path": ".quadratus/capture-fixtures/t2/manifest.csv"},
                                           {"action": "wait", "selector": "#rows tr"}])
    assert "uploads .quadratus/capture-fixtures/t2/manifest.csv into #csv" in text
    assert "do not create it" in text and "harness asks you for its content" in text and "needs no CHANGED entry" in text
    assert "recorded as unverified design work" in text


def test_the_lead_is_told_a_committed_sample_must_stay_put(tmp_path, monkeypatch):
    monkeypatch.setattr("quadratus.session.Session._harness_captures", lambda self, spec: True)
    text = _harness_lead_prompt(tmp_path, [{"action": "file", "selector": "#csv",
                                            "path": "tests/fixtures/sample.csv"}])
    assert "uploads the committed file tests/fixtures/sample.csv into #csv" in text
    assert "do not move, rename or delete it" in text


def test_no_file_step_adds_no_fixture_note(tmp_path, monkeypatch):
    monkeypatch.setattr("quadratus.session.Session._harness_captures", lambda self, spec: True)
    from quadratus.session import _capture_fixture_note
    text = _harness_lead_prompt(tmp_path, [{"action": "wait", "selector": "#rows tr"}])
    assert "(after 1 declared interaction steps)" in text
    assert "harness asks you for its content" not in text and "must remain a regular file" not in text
    assert _capture_fixture_note(type("S", (), {"task_id": "t2", "scope": None})()) == ""
