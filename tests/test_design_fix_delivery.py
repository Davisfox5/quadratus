"""A design-fix call declares only its own edits (GameTape run 12, 2026-09-26).

The call re-ran the tests and captured fresh renders without editing source,
then declared the three files the task's earlier calls had changed. Fleet's
exact CHANGED check stopped the run before the final design review. These
tests drive the real Fleet dispatcher, so the check is exercised, not stubbed.
"""

import json
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.runtime import Fleet
from quadratus.session import PartialWorkStopped, Session, SessionConfig
from tests.test_preferences_in_product import _fake_evidence, _ui

LEAD, REVIEWER = "grok:default", "claude:opus"


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)


def _run(tmp_path, monkeypatch, fix_reply):
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "index.html").write_text("<button>Import</button>\n")
    fleet = Fleet(Settings(backend="cli"), project=Project(root, exclude={root / ".quadratus"}),
                  allow_writes=True)
    view = SimpleNamespace()
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **k: view)
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)
    prompts = []

    def generate(model_key, provider, prompt, role):
        prompts.append((model_key, prompt))
        if "rendered evidence for this design task is missing" in prompt:
            _fake_evidence(root)          # fresh renders, written by the capture command
            return fix_reply
        return "APPROVED"                  # the cross-vendor final review
    monkeypatch.setattr(fleet, "_generate", generate)

    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), fleet.invoke,
                      config=SessionConfig(project=root, allow_writes=True,
                                           project_excludes=(root / ".quadratus",)))
    spec = _ui()
    session._active_spec = spec
    session._task_before = Project(root).contents()
    # The task's earlier draft and revision already changed the source.
    (root / "templates" / "index.html").write_text("<button id=preview>Import preview</button>\n")
    session._last_edit_started = 0
    task = TaskMemory(spec.task_id, LEAD, session.store)
    try:
        session._check_design(spec, LEAD, [REVIEWER], task)
    finally:
        fleet.close()
    return session, prompts, root


def test_an_evidence_only_fix_declares_nothing_and_reaches_the_final_review(tmp_path, monkeypatch):
    session, prompts, root = _run(tmp_path, monkeypatch, "Tests pass; renders captured.\nCHANGED: []")
    fix = next(p for _, p in prompts if "rendered evidence for this design task is missing" in p)
    note = next(line for line in fix.splitlines() if line.startswith("Files this task has already changed"))
    assert "templates/index.html" in note and "already recorded" in fix
    assert "end with exactly CHANGED: []" in fix and "never list them" in fix
    record = session.design_checks[0]
    assert record["verified"] is True and record["final_review"]["reviewer"] == REVIEWER
    assert not [f for f in session.open_findings if "design" in f]
    assert (root / "templates" / "index.html").read_text().startswith("<button id=preview>")
    assert not session.completed


def test_repeating_the_tasks_earlier_files_is_still_rejected(tmp_path, monkeypatch):
    reply = 'Scaffold already present; renders captured.\nCHANGED: ["templates/index.html"]'
    with pytest.raises(PartialWorkStopped, match="CHANGED report"):
        _run(tmp_path, monkeypatch, reply)


def test_the_capture_output_is_not_a_source_change(tmp_path, monkeypatch):
    """The evidence lands under .quadratus/, which the project excludes."""
    session, _, root = _run(tmp_path, monkeypatch, "Renders captured.\nCHANGED: []")
    summary = root / ".quadratus" / "design-evidence" / "t6" / "summary.json"
    assert json.loads(summary.read_text())["target"]
    assert session.design_checks[0]["verified"] is True


def test_the_fix_prompt_without_earlier_edits_still_states_the_rule(tmp_path):
    from quadratus.session import _design_fix_delivery
    text = _design_fix_delivery("")
    assert text.startswith("\nYour CHANGED line lists only files this call itself")
    assert "CHANGED: []" in text
