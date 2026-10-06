"""Renders that miss the change buy one recapture, not an open finding.

Series b1ff751 f5 (both arms) and rule-b1ff751 f5 (2026-10-05/06): the empty
state appears only when a project has no clips, the dispatched capture never
set that up, the cross-vendor reviewer rightly replied that the renders did
not show the change, and the task closed FindingsOpen on three different
leads with every grader passing. The lead redeclares the page and steps once;
the harness recaptures and the reviewer judges again.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.runtime import Fleet
from quadratus.session import Session, SessionConfig, _parse_capture_line
from tests.test_design_fix_delivery import _postdate
from tests.test_preferences_in_product import _fake_evidence, _ui

LEAD, REVIEWER = "grok:default", "claude:opus"
BLIND = "BLOCKING: the renders do not show the changed interface"
DECLARED = {"path": "/index.html", "steps": []}
REACHED = {"path": "/projects/empty", "steps": [{"action": "click", "selector": "#new-project"},
                                              {"action": "wait", "selector": ".empty-state"}]}


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)


def _run(tmp_path, monkeypatch, reviews, lead_reply, *, capture_failure=""):
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "index.html").write_text("<button id=preview>Import preview</button>\n")
    fleet = Fleet(Settings(backend="cli"), project=Project(root, exclude={root / ".quadratus"}),
                  allow_writes=True)
    view = SimpleNamespace()
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **k: view)
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)
    prompts, reviews = [], list(reviews)
    lead_replies = list(lead_reply) if isinstance(lead_reply, list) else [lead_reply]

    def generate(model_key, provider, prompt, role):
        prompts.append((model_key, prompt))
        if "CAPTURE:" in prompt and ("Reply with exactly one line" in prompt
                                     or "carried no CAPTURE: line" in prompt):
            return lead_replies.pop(0) if len(lead_replies) > 1 else lead_replies[0]
        return reviews.pop(0)
    monkeypatch.setattr(fleet, "_generate", generate)

    captures = []

    def harness_capture(self, spec):
        captures.append(dict(spec.scope.capture))
        if capture_failure and len(captures) > 1:
            return capture_failure
        _fake_evidence(root)
        _postdate(root)
        return ""
    monkeypatch.setattr(Session, "_harness_capture", harness_capture)
    monkeypatch.setattr(Session, "_harness_captures", lambda self, spec: True)

    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), fleet.invoke,
                      config=SessionConfig(project=root, allow_writes=True,
                                           project_excludes=(root / ".quadratus",),
                                           capture_profile=SimpleNamespace(origin="http://127.0.0.1:1")))
    spec = _ui()
    spec.scope = replace(spec.scope, capture=dict(DECLARED))
    session._active_spec = spec
    session._task_before = Project(root).contents()
    session._last_edit_started = 0
    task = TaskMemory(spec.task_id, LEAD, session.store)
    try:
        session._check_design(spec, LEAD, [REVIEWER], task)
    finally:
        fleet.close()
    return session, spec, prompts, captures


def test_a_blind_render_buys_one_redeclared_recapture(tmp_path, monkeypatch):
    session, spec, prompts, captures = _run(
        tmp_path, monkeypatch, [BLIND, "APPROVED"], "CAPTURE: " + json.dumps(REACHED))
    assert captures == [DECLARED, REACHED], "recaptured once, with the lead's page and steps"
    assert spec.scope.capture == REACHED
    record = session.design_checks[0]
    assert record["final_review"]["verdict"] == "APPROVED"
    assert record["recapture"]["verified"] is True and record["recapture"]["capture"] == REACHED
    assert not [f for f in session.open_findings if "design" in f]
    ask = next(p for _, p in prompts if "CAPTURE:" in p and "Reply with exactly one line" in p)
    assert "/index.html" in ask and "do not show the changed interface" in ask


def test_an_independent_blocker_beside_the_blind_line_blocks_the_recapture(tmp_path, monkeypatch):
    """Codex review of c3abcf0: the first review named a real design defect as
    well; a render redeclaration must not clear it."""
    mixed = BLIND + "\nBLOCKING: the delete action silently removes saved projects without confirmation."
    session, spec, prompts, captures = _run(tmp_path, monkeypatch, [mixed], "CAPTURE: " + json.dumps(REACHED))
    assert captures == [DECLARED] and spec.scope.capture == DECLARED, "no recapture was spent"
    assert not [p for _, p in prompts if "Reply with exactly one line" in p]
    findings = [f for f in session.open_findings if "design" in f]
    assert any("delete action" in f for f in findings) and any("do not show" in f for f in findings)


@pytest.mark.parametrize("verdict, blind", [
    (BLIND, True),
    ("blocking: The Renders Do Not Show The Changed Interface", True),
    ("- **BLOCKING:** the renders do not show the changed interface.", True),
    (BLIND + "\nBLOCKING: contrast is too low", False),
    (BLIND + "; BLOCKING: the delete action silently removes saved projects", False),
    (BLIND + "\nUNRESOLVED: the empty state is never reachable", False),
    (BLIND + "\n- BLOCKING: contrast", False),
    (BLIND + " and the header overlaps the toolbar", False),
    ("APPROVED", False),
    ("", False),
    ("The renders do not show the changed interface, but the layout is fine.", False),
])
def test_only_a_verdict_that_is_nothing_but_the_blind_line_is_blind(verdict, blind):
    from quadratus.session import _renders_blind
    assert _renders_blind(verdict) is blind


def test_a_second_clause_on_the_blind_line_blocks_the_recapture(tmp_path, monkeypatch):
    """Codex re-review of 9b8c056: one line, two findings."""
    mixed = BLIND + "; BLOCKING: the delete action silently removes saved projects without confirmation."
    session, spec, prompts, captures = _run(tmp_path, monkeypatch, [mixed], "CAPTURE: " + json.dumps(REACHED))
    assert captures == [DECLARED]
    assert any("delete action" in f for f in session.open_findings)


def test_a_second_blind_render_is_the_open_finding_it_was(tmp_path, monkeypatch):
    session, spec, prompts, captures = _run(
        tmp_path, monkeypatch, [BLIND, BLIND], "CAPTURE: " + json.dumps(REACHED))
    assert len(captures) == 2, "one recapture, never a second"
    assert [f for f in session.open_findings if "do not show the changed interface" in f]


def test_no_usable_declaration_keeps_the_verdict_and_spends_no_capture(tmp_path, monkeypatch):
    session, spec, prompts, captures = _run(tmp_path, monkeypatch, [BLIND], "CAPTURE: none\nNo route shows it.")
    assert captures == [DECLARED] and spec.scope.capture == DECLARED
    assert session.design_checks[0]["recapture"]["declared"] is None
    assert [f for f in session.open_findings if "do not show the changed interface" in f]


def test_repeating_the_dispatched_capture_spends_no_recapture(tmp_path, monkeypatch):
    session, spec, prompts, captures = _run(tmp_path, monkeypatch, [BLIND], "CAPTURE: " + json.dumps(DECLARED))
    assert captures == [DECLARED]
    assert "same page and steps" in session.design_checks[0]["recapture"]["problem"]


def test_a_failed_recapture_is_recorded_unverified(tmp_path, monkeypatch):
    session, spec, prompts, captures = _run(
        tmp_path, monkeypatch, [BLIND], "CAPTURE: " + json.dumps(REACHED), capture_failure="the preview died")
    record = session.design_checks[0]
    assert record["verified"] is False and "the preview died" in record["final_review"]["verdict"]
    assert ("t6", "the preview died") in session._design_unverified


@pytest.mark.parametrize("reply, capture, why", [
    ("CAPTURE: " + json.dumps(REACHED), REACHED, ""),
    ("I think\n`CAPTURE: " + json.dumps(REACHED) + "`", REACHED, ""),
    ("CAPTURE: none", None, "no page and steps reach"),
    ("Here is the page: /x", None, "no CAPTURE: line"),
    ('CAPTURE: {"path": "/x", "steps": [{"action": "hover", "selector": "a"}]}', None, "did not parse"),
    ('CAPTURE: {"path": "/x", "steps": [{"action": "file", "selector": "input", '
     '"path": ".quadratus/capture-fixtures/t9/a.csv"}]}', None, "capture-fixtures/t6/"),
])
def test_the_capture_line_parses_like_a_scope_capture(reply, capture, why):
    got, reason = _parse_capture_line(reply, "t6")
    assert got == capture and why in reason


# -- series rule-3572b72 f5: the marker after prose on the same line is the declaration ---

def test_a_capture_marker_after_prose_on_the_same_line_is_read(tmp_path, monkeypatch):
    # The saved f5 reply held "load.CAPTURE: {...}" after a sentence; the
    # line-anchored read recorded "no CAPTURE: line" and the verdict stood.
    reply = ("The empty state shows when a project has no clips, so the steps must create one before the "
             "wait.CAPTURE: " + json.dumps(REACHED) + " which reaches it.")
    session, spec, prompts, captures = _run(tmp_path, monkeypatch, [BLIND, "APPROVED"], reply)
    assert captures == [DECLARED, REACHED] and spec.scope.capture == REACHED
    assert session.design_checks[0]["recapture"]["verified"] is True
    assert len([p for _, p in prompts if "Reply with exactly one line" in p and "CAPTURE:" in p]) == 1, \
        "one bounded declaration round, never a re-ask"


def test_a_reply_with_no_marker_at_all_keeps_the_verdict_with_no_second_call(tmp_path, monkeypatch):
    session, spec, prompts, captures = _run(tmp_path, monkeypatch, [BLIND], "narration only, no declaration")
    assert len([p for _, p in prompts if "CAPTURE:" in p and "Reply with exactly one line" in p]) == 1
    assert captures == [DECLARED]
    assert session.design_checks[0]["recapture"]["problem"] == "no CAPTURE: line in the reply"


@pytest.mark.parametrize("reply, expected", [
    ("CAPTURE: none", None),
    ("I think CAPTURE: none. Nothing shows it.", None),
    ('prose CAPTURE: {"path": "/x", "steps": []} trailing words', {"path": "/x", "steps": []}),
    ('CAPTURE: {"path": "/a", "steps": []}\nlater CAPTURE: {"path": "/b", "steps": []}', {"path": "/b", "steps": []}),
    ("the word capture: appears but with no object", "unparsed"),
])
def test_the_declaration_is_read_wherever_the_marker_sits(reply, expected):
    capture, why = _parse_capture_line(reply, "t6")
    if expected == "unparsed":
        assert capture is None and ("did not parse" in why or "no CAPTURE" in why)
    elif expected is None:
        assert capture is None and "no page and steps" in why
    else:
        assert capture["path"] == expected["path"]
