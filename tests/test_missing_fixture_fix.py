"""A declared capture fixture the lead never wrote buys one design-fix call.

Series rule-3572b72 f1 t3 (2026-10-06): the lead was told to write the
capture-only sample under .quadratus/capture-fixtures/t3/ and did not. The
capture exited 2 on the missing file, no fix call was spent (a bad render
gets one; a failed capture got none), and the run ended on that debt with
three tasks closed clean. The missing sample is checked before the capture
runs; the lead gets the one design-fix call to write it.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.runtime import Fleet
from quadratus.session import Session, SessionConfig
from tests.test_design_fix_delivery import _postdate
from tests.test_preferences_in_product import _fake_evidence, _ui

LEAD, REVIEWER = "grok:default", "claude:opus"
FIXTURE = ".quadratus/capture-fixtures/t6/malformed.csv"
CAPTURE = {"path": "/index.html", "steps": [{"action": "file", "selector": "#import-file", "path": FIXTURE},
                                            {"action": "wait", "selector": "#errors tr"}]}


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)


def _run(tmp_path, monkeypatch, *, fix_writes=True, capture=CAPTURE, present=False, review_only=False):
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "index.html").write_text("<button id=preview>Import preview</button>\n")
    if present:
        (root / FIXTURE).parent.mkdir(parents=True)
        (root / FIXTURE).write_text("a,b\n1\n")
    fleet = Fleet(Settings(backend="cli"), project=Project(root, exclude={root / ".quadratus"}),
                  allow_writes=True)
    view = SimpleNamespace()
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **k: view)
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)
    prompts = []

    def generate(model_key, provider, prompt, role):
        prompts.append((model_key, prompt))
        if "The harness cannot capture this task yet" in prompt:
            if fix_writes:
                (root / FIXTURE).parent.mkdir(parents=True, exist_ok=True)
                (root / FIXTURE).write_text("a,b\n1\n")
            return "Wrote the sample.\nCHANGED: []"
        return "APPROVED"
    monkeypatch.setattr(fleet, "_generate", generate)

    captures = []

    def harness_capture(self, spec):
        captures.append(dict(spec.scope.capture))
        if not (root / FIXTURE).is_file():
            return f"the capture exited with 2: error: file step path is not a regular file in the project: '{FIXTURE}'"
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
    spec.scope = replace(spec.scope, capture=dict(capture), **({"review_only": True} if review_only else {}))
    session._active_spec = spec
    session._task_before = Project(root).contents()
    session._last_edit_started = 0
    task = TaskMemory(spec.task_id, LEAD, session.store)
    try:
        session._check_design(spec, LEAD, [REVIEWER], task)
    finally:
        fleet.close()
    return session, prompts, captures


def test_a_missing_declared_fixture_buys_one_fix_call_before_the_capture(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch)
    fixes = [p for _, p in prompts if "The harness cannot capture this task yet" in p]
    assert len(fixes) == 1 and FIXTURE in fixes[0] and "needs no CHANGED entry" in fixes[0]
    assert len(captures) == 1, "the capture ran once, after the fixture existed"
    record = session.design_checks[-1]
    assert record["missing_fixtures"] == [FIXTURE] and record["verified"] is True
    assert record["final_review"]["verdict"] == "APPROVED"
    assert not any("without clean rendered evidence" in f for f in session.open_findings)


def test_a_lead_that_still_does_not_write_it_gets_no_second_call(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, fix_writes=False)
    fixes = [p for _, p in prompts if "The harness cannot capture this task yet" in p]
    assert len(fixes) == 1 and len(captures) == 1
    record = session.design_checks[-1]
    assert record["verified"] is False and "not a regular file" in record["problem"]
    assert any("without clean rendered evidence" in f for f in session.open_findings)


def test_a_present_fixture_or_a_committed_sample_spends_no_fix_call(tmp_path, monkeypatch):
    _, prompts, captures = _run(tmp_path, monkeypatch, present=True)
    assert not any("cannot capture this task yet" in p for _, p in prompts) and len(captures) == 1
    committed = {"path": "/index.html", "steps": [{"action": "file", "selector": "#f", "path": "tests/fixtures/x.csv"},
                                                  {"action": "wait", "selector": "#errors tr"}]}
    session, prompts, _ = _run(tmp_path.joinpath("b"), monkeypatch, capture=committed, present=True)
    assert not any("cannot capture this task yet" in p for _, p in prompts)
    assert session._missing_own_fixtures(session._active_spec) == []


def test_a_review_only_task_spends_no_fix_call(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, review_only=True)
    assert not any("cannot capture this task yet" in p for _, p in prompts)
    assert session.design_checks[-1]["verified"] is False
