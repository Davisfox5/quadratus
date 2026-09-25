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
    assert not ok and "no summary.json" in problem
    try:
        capture(str(page), "t6", tmp_path)
    except Exception as exc:  # noqa: BLE001 -- no browser on this machine
        pytest.skip(f"headless browser unavailable: {exc}")
    ok, problem, shots = check(tmp_path, "t6", started)
    assert ok, problem
    assert len(shots) == 3 and shots[-1].startswith("target: ")
    assert not check(tmp_path, "t6", time.time() + 60)[0], "stale evidence does not count"


def _design_session(tmp_path, invoke):
    import dataclasses
    session = Session("goal", ArtifactStore(tmp_path / "a"), invoke, config=SessionConfig())
    session.project = tmp_path
    session.config = dataclasses.replace(session.config, allow_writes=True)
    session._last_edit_started = 0
    return session


def test_missing_evidence_gets_one_fix_call_then_an_open_finding(tmp_path):
    from quadratus.memory import TaskMemory
    calls = []
    session = _design_session(tmp_path, lambda model, prompt, **k: calls.append(prompt) or "tried")
    session._check_design(_ui(), "grok:default", [], TaskMemory("t6", "grok:default", session.store))
    assert sum("rendered evidence for this design task is missing" in p for p in calls) == 1
    joined = " ".join(session.open_findings)
    assert "without clean rendered evidence" in joined and "no reviewer from another vendor" in joined
    assert session.design_checks[0]["verified"] is False


def _fake_evidence(root, clean=True, width=(1280, 390)):
    import json
    import struct
    import zlib

    from quadratus.design_evidence import evidence_dir

    def png(w):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, 1, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * w)) + chunk(b"IEND", b""))
    views = {}
    for name, w in zip(("desktop", "mobile"), width, strict=True):
        folder = evidence_dir(root, "t6") / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page.png").write_bytes(png(w))
        views[name] = dict(clean=clean, console_errors=[] if clean else ["Uncaught TypeError: x"],
                           failed_requests=[] if clean else ["GET /missing.png 404"])
    (evidence_dir(root, "t6") / "summary.json").write_text(json.dumps(dict(target="http://127.0.0.1:5000/", views=views)))


def test_a_render_with_console_errors_or_failed_requests_fails_the_check(tmp_path):
    """Codex review: a broken page rendered cleanly-shaped PNGs and passed."""
    from quadratus.design_evidence import check
    _fake_evidence(tmp_path, clean=False)
    ok, problem, _ = check(tmp_path, "t6", 0)
    assert not ok and "not clean" in problem and "TypeError" in problem and "404" in problem
    _fake_evidence(tmp_path, clean=True)
    ok, _, shots = check(tmp_path, "t6", 0)
    assert ok and "target: http://127.0.0.1:5000/" in shots


def test_renders_older_than_the_last_edit_do_not_count(tmp_path):
    import time

    from quadratus.design_evidence import check
    _fake_evidence(tmp_path)
    assert not check(tmp_path, "t6", time.time() + 5)[0]


def test_the_final_renders_go_to_the_cross_vendor_reviewer(tmp_path):
    from quadratus.memory import TaskMemory
    _fake_evidence(tmp_path)
    prompts = []

    def invoke(model, prompt, **k):
        prompts.append((model, prompt))
        return "BLOCKING: the results table overflows at mobile width"
    session = _design_session(tmp_path, invoke)
    session._check_design(_ui(), "grok:default", ["claude:opus"], TaskMemory("t6", "grok:default", session.store))
    assert prompts and prompts[0][0] == "claude:opus" and "final renders" in prompts[0][1]
    assert any("overflows at mobile" in f for f in session.open_findings)
    assert session.design_checks[0]["verified"] is True

