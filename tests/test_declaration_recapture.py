"""A final wait already visible at load is a declaration defect, not a design defect.

Series rule-3572b72 f2 t1 (2026-10-06): the harness qualifier rejected both
renders because the declared final wait (#project-search) was visible before
any step ran, and the engine spent its one design-fix call asking the lead
to fix the source, which was not the problem; the call ran to the 20-round
cap at 961k tokens and the run ended DesignUnverified. The qualifier now
reports that case under its own record kind, and the session answers it
with the one capture redeclaration the blind review already gets, never a
design-fix.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.design_evidence import CAPTURE_DECLARATION, check_records, evidence_dir
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.runtime import Fleet
from quadratus.session import Session, SessionConfig, _declaration_only
from tests.test_design_fix_delivery import _postdate
from tests.test_preferences_in_product import _fake_evidence, _ui

LEAD, REVIEWER = "grok:default", "claude:opus"
DECLARED = {"path": "/index.html", "steps": [{"action": "wait", "selector": "#project-search"}]}
REDECLARED = {"path": "/index.html", "steps": [{"action": "click", "selector": "#project-search"},
                                               {"action": "wait", "selector": "#results tr"}]}


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)


def _steps_summary(root, requested, *, visible_before):
    """Append step records to the fake evidence: every step ran and passed;
    the final wait was (or was not) visible before any step ran."""
    summary_path = evidence_dir(root, "t6") / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["steps"] = [dict(s) for s in requested]
    for view in summary["views"].values():
        view["steps"] = [dict(n=i + 1, action=s["action"], selector=s["selector"], ok=True)
                         for i, s in enumerate(requested)]
        view["steps"][-1]["visible_before_steps"] = visible_before
    summary_path.write_text(json.dumps(summary))


def test_check_records_reports_a_visible_final_wait_under_its_own_kind(tmp_path):
    _fake_evidence(tmp_path)
    _postdate(tmp_path)
    _steps_summary(tmp_path, DECLARED["steps"], visible_before=True)
    ok, problem, _, records = check_records(tmp_path, "t6", 0)
    assert not ok and "already visible before any step ran" in problem
    assert [r["kind"] for r in records] == [CAPTURE_DECLARATION, CAPTURE_DECLARATION]
    assert _declaration_only(records)
    _steps_summary(tmp_path, DECLARED["steps"], visible_before=False)
    ok, _, _, records = check_records(tmp_path, "t6", 0)
    assert ok and records == []


def test_a_mixed_record_set_is_not_declaration_only():
    assert not _declaration_only([dict(kind=CAPTURE_DECLARATION), dict(kind="page.unclean")])
    assert not _declaration_only([])


def _run(tmp_path, monkeypatch, lead_reply, *, second_visible=False):
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "index.html").write_text("<input id=project-search>\n")
    fleet = Fleet(Settings(backend="cli"), project=Project(root, exclude={root / ".quadratus"}),
                  allow_writes=True)
    view = SimpleNamespace()
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **k: view)
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)
    prompts = []

    def generate(model_key, provider, prompt, role):
        prompts.append((model_key, prompt))
        if "Reply with exactly one line" in prompt and "CAPTURE:" in prompt:
            return lead_reply
        if "Fix it in source" in prompt:
            return "Fixed.\nCHANGED: []"
        return "APPROVED"
    monkeypatch.setattr(fleet, "_generate", generate)

    captures = []

    def harness_capture(self, spec):
        captures.append(dict(spec.scope.capture))
        _fake_evidence(root)
        _postdate(root)
        _steps_summary(root, spec.scope.capture["steps"],
                       visible_before=(len(captures) == 1) or second_visible)
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
    return session, prompts, captures


def test_a_visible_final_wait_buys_one_redeclaration_not_a_design_fix(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, "CAPTURE: " + json.dumps(REDECLARED))
    asks = [p for _, p in prompts if "CAPTURE:" in p and "Reply with exactly one line" in p]
    assert len(asks) == 1 and "the capture check replied" in asks[0] and "already visible" in asks[0]
    assert not any("Fix it in source" in p for _, p in prompts), "no design-fix on sound source"
    assert len(captures) == 2 and captures[1] == REDECLARED
    record = session.design_checks[-1]
    assert record["recapture"]["verified"] is True and record["verified"] is True
    assert record["final_review"]["verdict"] == "APPROVED"
    assert not any("without clean rendered evidence" in f for f in session.open_findings)


def test_a_redeclaration_that_still_waits_on_a_visible_element_gets_no_fix_and_stays_unverified(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, "CAPTURE: " + json.dumps(REDECLARED),
                                      second_visible=True)
    assert len(captures) == 2
    assert not any("Fix it in source" in p for _, p in prompts)
    record = session.design_checks[-1]
    assert record["verified"] is False and "already visible" in record["problem"]
    assert any("without clean rendered evidence" in f for f in session.open_findings)


def test_capture_none_leaves_the_declaration_problem_on_the_record(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, "CAPTURE: none\nthe page has no result state")
    assert len(captures) == 1 and not any("Fix it in source" in p for _, p in prompts)
    record = session.design_checks[-1]
    assert record["verified"] is False and record["recapture"]["declared"] is None
