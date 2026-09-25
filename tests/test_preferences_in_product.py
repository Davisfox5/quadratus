"""Davis's personal model rules as product behaviour, and the switch to run without them."""

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import ClaudeCLIProvider, CodexCLIProvider, GrokCLIProvider
from quadratus.scope import TaskScope
from quadratus.session import Complexity, Session, SessionConfig, TaskSpec, is_design_task
from quadratus.task_kinds import TaskKind


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch):
    monkeypatch.setattr('shutil.which', lambda _: '/unused/cli')
    for vendor in ('OPENAI', 'CLAUDE', 'GROK'):
        monkeypatch.delenv(f'QUADRATUS_CLI_ARGS_{vendor}', raising=False)
    monkeypatch.delenv('QUADRATUS_NEUTRAL_PREFERENCES', raising=False)


def _ui():
    return TaskSpec("t6", "Add the import preview button", complexity=Complexity.SIMPLE,
                    scope=TaskScope(permitted_paths=["templates/index.html", "static/js/app.js"]))


def test_design_tasks_are_recognised_by_kind_or_scope():
    assert is_design_task(_ui())
    assert is_design_task(TaskSpec("t", "x", kind=TaskKind.FRONTEND))
    assert not is_design_task(TaskSpec("t", "x", scope=TaskScope(permitted_paths=["app.py", "tests/test_app.py"])))


def _session(tmp_path, **config):
    return Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(**config))


def test_the_lead_verifies_its_own_design_work(tmp_path):
    prompt = _session(tmp_path)._lead_prompt(_ui())
    assert "desktop width" in prompt and "mobile" in prompt and "not finished" in prompt
    assert "desktop width" not in _session(tmp_path)._lead_prompt(TaskSpec("t1", "parse csv"))
    assert "desktop width" not in _session(tmp_path, design_self_verify=False)._lead_prompt(_ui())


def test_design_work_draws_a_reviewer_from_another_vendor_even_when_simple(tmp_path):
    session = _session(tmp_path)
    peers = session.collaborators_for(_ui(), "grok:default")
    assert peers and all(p.partition(":")[0] != "grok" for p in peers)
    assert session.collaborators_for(TaskSpec("t1", "parse csv", complexity=Complexity.SIMPLE), "grok:default") == []
    assert _session(tmp_path, design_cross_check=False).collaborators_for(_ui(), "grok:default") == []


def test_the_design_reviewer_is_briefed_on_aesthetics(tmp_path):
    prompt = _session(tmp_path)._collaborator_prompt(_ui(), "draft", "claude:opus")
    assert "aesthetic" in prompt and "mobile width" in prompt


def test_neutral_mode_drops_personal_config_but_keeps_sign_in(monkeypatch):
    plain = ClaudeCLIProvider(model="opus")._build_argv("p", "")
    assert "--setting-sources" not in plain
    monkeypatch.setenv("QUADRATUS_NEUTRAL_PREFERENCES", "1")
    claude = ClaudeCLIProvider(model="opus")._build_argv("p", "")
    assert claude[claude.index("--setting-sources") + 1] == "project,local" and "--bare" not in claude
    codex = CodexCLIProvider("gpt-5.6-sol")._build_argv("p", "")
    assert "--ignore-user-config" in codex and "--ignore-rules" in codex
    GrokCLIProvider(model="")._build_argv("p", "")  # no flag exists; nothing to add


def test_the_run_records_whether_preferences_shaped_it(monkeypatch):
    from quadratus.project_run import _preferences_record
    assert _preferences_record()["mode"] == "active"
    monkeypatch.setenv("QUADRATUS_NEUTRAL_PREFERENCES", "1")
    record = _preferences_record()
    assert record["mode"] == "neutralised" and any("user_rules" in gap for gap in record["not_removable"])
