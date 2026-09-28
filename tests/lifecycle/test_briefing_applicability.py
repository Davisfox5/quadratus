"""The design reviewers' briefing follows the task contract (map P3.4;
O-NEXT-08 on #25, comment 5865273760, finding 1).

7403511 moved the lead prompt's design instruction onto the contract
(``Required.design_instruction``). ``Session._brief_design_reviewers`` still
asks the live ``_harness_captures(spec)`` whether the harness, not the lead,
renders the task. When the capture profile drifts after dispatch the
briefing contradicts what the lead was told, and nothing is recorded:

* contract "self", a profile appears: the lead was told to capture its own
  renders, the briefing returns early and the reviewers never see them;
* contract "harness", the profile vanishes: the lead was told the harness
  captures, the briefing reads the draft's evidence folder anyway.

The xfails assert the contract-following behaviour and are strict, so the
engine correction in docs/review/briefing-applicability-seam.md must turn
them green. The drift is a synthetic write that holds only while the
briefing runs. The controls are no-drift journeys any correction must keep.

Stubbed: ``quadratus.design_evidence.check`` in the tests that need the
lead's renders to pass, since a real pass needs a browser capture. It
returns ``(True, "", [<desktop>, <mobile>])`` and counts its calls. The
evidence files it names are written to disk, so ``_evidence_files`` and
``runtime.evidence_refusals`` run unstubbed. The disabled controls use the
real ``check`` on an empty evidence folder.
"""

import dataclasses

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.design_evidence import evidence_dir
from quadratus.outcome import TaskOutcome
from quadratus.preview import CaptureProfile
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec

PROFILE = CaptureProfile(preview=("true",), origin="http://127.0.0.1:5000")
CAPTURE = {"path": "/index.html", "steps": []}
SHOWN = ".quadratus/design-evidence/t1/desktop/page.png"


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
    return session._contract.required.design_evidence, session._contract.required.design_instruction


def _brief_under(session, spec, **changes):
    """Brief the reviewers with ``changes`` applied to the live config only
    while the briefing runs."""
    before = session.config
    session.config = dataclasses.replace(before, **changes)
    try:
        session._brief_design_reviewers(spec)
    finally:
        session.config = before


def _lead_renders(session, monkeypatch):
    """The lead's renders for t1 on disk, and a passing ``check`` over them.
    Returns the list of ``check`` calls."""
    folder = evidence_dir(session.project, "t1")
    shots = []
    for view in ("desktop", "mobile"):
        (folder / view).mkdir(parents=True, exist_ok=True)
        (folder / view / "page.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (folder / view / "evidence.json").write_text("{}")
        shots.append(str(folder / view / "page.png"))
    (folder / "summary.json").write_text("{}")
    calls = []

    def check(*args, **kwargs):
        calls.append(args)
        return True, "", list(shots)
    monkeypatch.setattr("quadratus.design_evidence.check", check)
    return calls


def _briefed(session):
    return bool(session._review_evidence) and SHOWN in session._design_note


def _not_briefed(session):
    return session._review_evidence == [] and session._design_note == ""


# -- drift: the live profile disagrees with the contract while briefing --------------

@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="O-NEXT-12: _brief_design_reviewers reads the live _harness_captures")
def test_a_self_capture_task_is_briefed_when_a_profile_drifts_in(tmp_path, monkeypatch):
    session, spec = _session(tmp_path), _design_spec()
    assert _dispatch(session, spec) == ("self", "self")
    calls = _lead_renders(session, monkeypatch)
    _brief_under(session, spec, capture_profile=PROFILE)
    assert _briefed(session), "the lead was told to capture; its renders reach the reviewers"
    assert len(calls) == 1
    assert session._outcome.mismatches == ["design_instruction: contract 'self', legacy 'harness'"]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="O-NEXT-12: _brief_design_reviewers reads the live _harness_captures")
def test_a_harness_task_is_not_briefed_when_the_profile_drifts_away(tmp_path, monkeypatch):
    session, spec = _session(tmp_path, profile=PROFILE), _design_spec()
    assert _dispatch(session, spec) == ("harness", "harness")
    calls = _lead_renders(session, monkeypatch)
    _brief_under(session, spec, capture_profile=None)
    assert _not_briefed(session), "the harness captures after the final edit; no draft is promised"
    assert calls == []
    assert session._outcome.mismatches == ["design_instruction: contract 'harness', legacy 'self'"]


# -- controls: no drift, behaviour any correction must keep ---------------------------

def test_a_self_capture_task_is_briefed_with_the_lead_renders(tmp_path, monkeypatch):
    session, spec = _session(tmp_path), _design_spec()
    assert _dispatch(session, spec) == ("self", "self")
    calls = _lead_renders(session, monkeypatch)
    session._brief_design_reviewers(spec)
    assert _briefed(session) and len(calls) == 1
    assert session._outcome.mismatches == []


def test_a_harness_task_is_not_briefed_with_the_draft(tmp_path, monkeypatch):
    session, spec = _session(tmp_path, profile=PROFILE), _design_spec()
    assert _dispatch(session, spec) == ("harness", "harness")
    calls = _lead_renders(session, monkeypatch)
    session._brief_design_reviewers(spec)
    assert _not_briefed(session) and calls == []
    assert session._outcome.mismatches == []


def test_verification_disabled_with_no_renders_is_not_briefed(tmp_path):
    session, spec = _session(tmp_path, verify=False), _design_spec()
    assert _dispatch(session, spec) == ("disabled", "none")
    session._brief_design_reviewers(spec)
    assert _not_briefed(session)
    assert session._outcome.mismatches == []


def test_verification_disabled_with_a_profile_is_not_briefed(tmp_path):
    session, spec = _session(tmp_path, profile=PROFILE, verify=False), _design_spec()
    assert _dispatch(session, spec) == ("disabled", "none")
    session._brief_design_reviewers(spec)
    assert _not_briefed(session)
    assert session._outcome.mismatches == []


def test_a_non_design_task_is_not_briefed(tmp_path, monkeypatch):
    session = _session(tmp_path, profile=PROFILE)
    spec = TaskSpec("t1", "Tidy the parser.", kind="refactor",
                    scope=TaskScope(permitted_paths=["quadratus/parser.py"]))
    assert _dispatch(session, spec) == ("none", "none")
    calls = _lead_renders(session, monkeypatch)
    session._brief_design_reviewers(spec)
    assert _not_briefed(session) and calls == []
    assert session._outcome.mismatches == []


def test_a_design_task_with_no_project_is_not_briefed(tmp_path, monkeypatch):
    session, spec = _session(tmp_path, project=False), _design_spec(capture=None)
    assert _dispatch(session, spec) == ("none", "self")
    calls = []
    monkeypatch.setattr("quadratus.design_evidence.check", lambda *a, **k: calls.append(a))
    session._brief_design_reviewers(spec)
    assert _not_briefed(session) and calls == []
    assert session._outcome.mismatches == []
