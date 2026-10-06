"""A declared capture fixture the lead could not write is supplied, not written.

Series rule-3572b72 f1 t3 (2026-10-06): the built-in policy tells every
builder "never change .quadratus/**" while the capture note said "you must
write .quadratus/capture-fixtures/t3/malformed.csv". The Opus lead of t2
wrote its sample anyway; the Sol lead of t3 obeyed the ban, the capture
exited 2 on the missing file, no call was spent, and the run ended on that
debt with three tasks closed clean. The sample is harness state, so the
harness writes it: one bounded round asks the lead for the content
(FIXTURE <path>: and a fenced block), validates the size, and writes the
file under the task's own fixture folder. No write grant changes.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.config import Settings
from quadratus.memory import TaskMemory
from quadratus.project import Project
from quadratus.runtime import Fleet
from quadratus.session import Session, SessionConfig, _parse_fixture_blocks
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


SUPPLY = f"FIXTURE {FIXTURE}:\n```csv\nstart,end\n1,abc\n```\n"


def _run(tmp_path, monkeypatch, *, supply=SUPPLY, capture=CAPTURE, present=False, review_only=False):
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
            return supply
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


def test_a_missing_declared_fixture_is_supplied_by_the_lead_and_written_by_the_harness(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch)
    asks = [p for _, p in prompts if "The harness cannot capture this task yet" in p]
    assert len(asks) == 1 and FIXTURE in asks[0] and "FIXTURE <path>:" in asks[0]
    assert "refuses writes under .quadratus/" in asks[0] and "does not edit" in asks[0]
    assert len(captures) == 1, "the capture ran once, after the harness wrote the sample"
    root = tmp_path / "project"
    assert (root / FIXTURE).read_text() == "start,end\n1,abc"
    record = session.design_checks[-1]
    assert record["missing_fixtures"] == [FIXTURE] and record["fixtures_written"] == [FIXTURE]
    assert record["verified"] is True and record["final_review"]["verdict"] == "APPROVED"
    assert not any("without clean rendered evidence" in f for f in session.open_findings)


def test_a_reply_without_the_block_gets_no_second_round_and_the_capture_fails_as_before(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, supply="I would write a,b first.")
    asks = [p for _, p in prompts if "The harness cannot capture this task yet" in p]
    assert len(asks) == 1 and len(captures) == 1
    record = session.design_checks[-1]
    assert record["fixtures_written"] == [] and "no FIXTURE block" in record["fixture_problems"][0]
    assert record["verified"] is False and "not a regular file" in record["problem"]
    assert any("without clean rendered evidence" in f for f in session.open_findings)


def test_an_unexpected_path_is_never_written_and_an_oversized_sample_is_refused(tmp_path, monkeypatch):
    elsewhere = "FIXTURE app.py:\n```\nprint(1)\n```\n"
    session, _, _ = _run(tmp_path, monkeypatch, supply=elsewhere)
    assert not (tmp_path / "project" / "app.py").exists()
    assert session.design_checks[-1]["fixtures_written"] == []
    big = f"FIXTURE {FIXTURE}:\n```\n" + "x" * 1_000_001 + "\n```\n"
    session, _, _ = _run(tmp_path / "b", monkeypatch, supply=big)
    record = session.design_checks[-1]
    assert record["fixtures_written"] == [] and "exceeds" in record["fixture_problems"][0]
    assert not (tmp_path / "b" / "project" / FIXTURE).exists()


def test_a_present_fixture_or_a_committed_sample_spends_no_round(tmp_path, monkeypatch):
    _, prompts, captures = _run(tmp_path, monkeypatch, present=True)
    assert not any("cannot capture this task yet" in p for _, p in prompts) and len(captures) == 1
    committed = {"path": "/index.html", "steps": [{"action": "file", "selector": "#f", "path": "tests/fixtures/x.csv"},
                                                  {"action": "wait", "selector": "#errors tr"}]}
    session, prompts, _ = _run(tmp_path.joinpath("b"), monkeypatch, capture=committed, present=True)
    assert not any("cannot capture this task yet" in p for _, p in prompts)
    assert session._missing_own_fixtures(session._active_spec) == []


def test_a_review_only_task_gets_the_round_too_since_it_edits_nothing(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, review_only=True)
    assert any("cannot capture this task yet" in p for _, p in prompts) and len(captures) == 1
    assert session.design_checks[-1]["fixtures_written"] == [FIXTURE]


@pytest.mark.parametrize("reply, expected", [
    ("FIXTURE a/b.csv:\n```\nx,y\n```", {"a/b.csv": "x,y"}),
    ("FIXTURE `a/b.csv`\n```csv\nx,y\n1,2\n```\ntrailing", {"a/b.csv": "x,y\n1,2"}),
    ("FIXTURE one.csv:\n```\n1\n```\nFIXTURE two.csv:\n```\n2\n```", {"one.csv": "1", "two.csv": "2"}),
    ("no blocks here", {}),
    ("FIXTURE a.csv:\nnot fenced", {}),
])
def test_fixture_blocks_are_parsed_as_written(reply, expected):
    assert _parse_fixture_blocks(reply) == expected
