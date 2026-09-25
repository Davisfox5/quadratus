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
    assert "quadratus.design_evidence" in prompt and "t6" in prompt and "unverified design work" in prompt
    assert "design_evidence" not in _session(tmp_path)._lead_prompt(TaskSpec("t1", "parse csv"))
    assert "design_evidence" not in _session(tmp_path, design_self_verify=False)._lead_prompt(_ui())


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
    assert "--ignore-user-config" in codex
    assert "--ignore-rules" not in codex, "the project's execpolicy rules must survive neutral mode"
    GrokCLIProvider(model="")._build_argv("p", "")  # no flag exists; nothing to add


def test_the_run_records_what_was_requested_not_what_it_assumes(monkeypatch):
    from quadratus.config import Settings
    from quadratus.project_run import _preferences_record
    assert _preferences_record(Settings())["requested"] == "personal configuration not suppressed"
    record = _preferences_record(Settings(neutral_preferences=True))
    assert record["requested"] == "neutral" and record["observed"] == "not verified per CLI"
    assert any("user_rules" in gap for gap in record["not_removable"])
    assert any(".rules" in gap for gap in record["not_removable"])


def test_neutral_mode_travels_in_settings_not_the_process(monkeypatch):
    from quadratus.config import Settings
    from quadratus.runtime import Fleet
    fleet = Fleet(Settings(neutral_preferences=True))
    monkeypatch.setattr("quadratus.cli_providers.CLIProvider.available", lambda self: True)
    view = fleet.provider_for("openai:gpt-5.6-sol")
    assert "--ignore-user-config" in view._build_argv("p", "")
    import os
    assert "QUADRATUS_NEUTRAL_PREFERENCES" not in os.environ


def test_design_evidence_is_real_screenshots_at_both_widths(tmp_path):
    """End to end: a real headless render of a real page, checked by the harness."""
    import time
    pytest.importorskip("playwright")
    from quadratus.design_evidence import capture, check
    page = tmp_path / "index.html"
    page.write_text("<!doctype html><title>t</title><button id=b>Import preview</button>")
    started = time.time() - 1
    ok, problem, _ = check(tmp_path, "t6", started)
    assert not ok and "no desktop screenshot" in problem
    try:
        capture(str(page), "t6", tmp_path)
    except Exception as exc:  # noqa: BLE001 -- no browser on this machine
        pytest.skip(f"headless browser unavailable: {exc}")
    ok, problem, shots = check(tmp_path, "t6", started)
    assert ok, problem
    assert len(shots) == 2
    assert not check(tmp_path, "t6", time.time() + 60)[0], "stale evidence does not count"


def test_missing_design_evidence_or_reviewer_is_an_open_finding(tmp_path):
    session = _session(tmp_path)
    session.project = tmp_path
    session.config = __import__("dataclasses").replace(session.config, allow_writes=True)
    session._task_started = 0
    from quadratus.memory import TaskMemory
    task = TaskMemory("t6", "grok:default", session.store)
    session._check_design(_ui(), "grok:default", [], task)
    joined = " ".join(session.open_findings)
    assert "without rendered evidence" in joined and "no reviewer from another vendor" in joined
    assert session.design_checks[0]["verified"] is False

