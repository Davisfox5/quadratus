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
    (root / "templates").mkdir(parents=True, exist_ok=True)
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

    supplies = list(supply) if isinstance(supply, list) else [supply]

    def generate(model_key, provider, prompt, role):
        prompts.append((model_key, prompt))
        if "The harness cannot capture this task yet" in prompt or "carried no FIXTURE block" in prompt:
            answer = supplies.pop(0) if len(supplies) > 1 else supplies[0]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return "APPROVED"
    monkeypatch.setattr(fleet, "_generate", generate)

    captures = []

    def harness_capture(self, spec):
        captures.append(dict(spec.scope.capture))
        # As the real harness capture does: the attempt retires the old
        # measurement first and records its own once the renders are written.
        self._harness_tasks.add(spec.task_id)
        self._capture_receipts.pop(spec.task_id, None)
        if not (root / FIXTURE).is_file():
            return f"the capture exited with 2: error: file step path is not a regular file in the project: '{FIXTURE}'"
        _fake_evidence(root)
        _postdate(root)
        from quadratus.design_evidence import view_receipt
        self._capture_receipts[spec.task_id] = {v: view_receipt(root, spec.task_id, v) for v in ("desktop", "mobile")}
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


def test_a_reply_without_the_block_gets_no_second_round_and_no_capture(tmp_path, monkeypatch):
    session, prompts, captures = _run(tmp_path, monkeypatch, supply="I would write a,b first.")
    asks = [p for _, p in prompts if "The harness cannot capture this task yet" in p]
    assert len(asks) == 1 and captures == [], "one bounded round, never a re-ask, nothing to capture against"
    record = session.design_checks[-1]
    assert record["fixtures_written"] == [] and "no FIXTURE block" in record["fixture_problems"][0]
    assert record["verified"] is False and "could not be supplied" in record["problem"]
    assert any("without clean rendered evidence" in f for f in session.open_findings)


def test_an_unexpected_path_is_never_written_and_an_oversized_sample_is_refused(tmp_path, monkeypatch):
    elsewhere = "FIXTURE app.py:\n```\nprint(1)\n```\n"
    session, _, captures = _run(tmp_path, monkeypatch, supply=elsewhere)
    assert not (tmp_path / "project" / "app.py").exists() and captures == []
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
])
def test_fixture_blocks_are_parsed_as_written(reply, expected):
    found, problems = _parse_fixture_blocks(reply)
    assert found == expected and problems == []


def test_duplicate_blocks_for_one_path_keep_nothing_and_an_unclosed_fence_is_malformed():
    # Codex review of 351d3ba: the first of two different blocks for the same
    # declared fixture was written and the task reached APPROVED.
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\n```\n1\n```\nFIXTURE a.csv:\n```\n2\n```")
    assert found == {} and problems == ["a.csv: more than one FIXTURE header for the same path"]
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\n```\n1\nFIXTURE b.csv:\n```\n2\n```")
    assert found == {} and "malformed" in problems[0]


def test_a_partial_trailing_duplicate_header_keeps_nothing():
    # Codex review of 6844b97: a complete block followed by the same header
    # with an unclosed or missing fence returned the first body.
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\n```\n1\n```\nFIXTURE a.csv:\n```\n2")
    assert found == {} and "more than one FIXTURE header" in problems[0]
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\n```\n1\n```\nFIXTURE a.csv:\nnot fenced")
    assert found == {} and "more than one FIXTURE header" in problems[0]
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\nnot fenced at all")
    assert found == {} and "no complete fenced block" in problems[0]


def test_a_malformed_later_header_still_counts_as_a_duplicate():
    # Codex review of ada4c75: "FIXTURE a.csv: corrected content follows"
    # after a valid block escaped the duplicate accounting.
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\n```\n1\n```\nFIXTURE a.csv: corrected content follows")
    assert found == {} and "more than one FIXTURE header" in problems[0]
    found, problems = _parse_fixture_blocks("FIXTURE a.csv:\n```\n1\n```\nFIXTURE a.csv corrected\n```\n2\n```")
    assert found == {} and "more than one FIXTURE header" in problems[0]
    found, problems = _parse_fixture_blocks("FIXTURE :\n```\n1\n```")
    assert found == {} and "names no path" in problems[0]


def test_a_failed_write_never_publishes_the_declared_name(tmp_path, monkeypatch):
    import os

    from quadratus.session import _write_fixture_bound
    root = tmp_path / "project"
    root.mkdir()
    real_write = os.write
    calls = []

    def short_then_full(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, bytes(data[:5]))
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "write", short_then_full)
    # Codex review of a7cde45: with the discard denied too, five bytes under
    # the declared name became evidence for a later check. The declared
    # name is now created only by linking a fully written file.
    monkeypatch.setattr(os, "ftruncate", lambda fd, length: (_ for _ in ()).throw(PermissionError(13, "denied")))
    why = _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/x.csv", b"start,end\n1,abc\n")
    assert "before the file was published" in why and "declared name does not exist" in why
    folder = root / ".quadratus" / "capture-fixtures" / "t6"
    assert not (folder / "x.csv").exists()
    leftovers = [p.name for p in folder.iterdir()]
    assert leftovers and all(name.startswith(".supply-") for name in leftovers), "only the private temporary"
    from quadratus.design_evidence import _fixture
    with pytest.raises(ValueError, match="not a regular file"):
        _fixture(root, ".quadratus/capture-fixtures/t6/x.csv", "t6")
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("g", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(project=root))
    spec = _ui()
    spec.scope = replace(spec.scope, capture={"path": "/", "steps": [
        {"action": "file", "selector": "#f", "path": ".quadratus/capture-fixtures/t6/x.csv"}]})
    assert session._missing_own_fixtures(spec) == [".quadratus/capture-fixtures/t6/x.csv"]


def test_a_complete_write_is_published_by_link_and_the_temporary_is_gone(tmp_path):
    from quadratus.session import _write_fixture_bound
    root = tmp_path / "project"
    root.mkdir()
    assert _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/y.csv", b"a,b\n") == ""
    folder = root / ".quadratus" / "capture-fixtures" / "t6"
    assert (folder / "y.csv").read_bytes() == b"a,b\n"
    assert [p.name for p in folder.iterdir()] == ["y.csv"]


def test_a_target_appearing_before_publication_is_never_overwritten(tmp_path, monkeypatch):
    import os

    from quadratus.session import _write_fixture_bound
    root = tmp_path / "project"
    folder = root / ".quadratus" / "capture-fixtures" / "t6"
    folder.mkdir(parents=True)
    real_fsync = os.fsync

    def plant_then_sync(fd):
        (folder / "z.csv").write_text("concurrent owner replacement")
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", plant_then_sync)
    why = _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/z.csv", b"a,b\n")
    assert "appeared before publication" in why
    assert (folder / "z.csv").read_text() == "concurrent owner replacement"


def test_the_bound_write_refuses_links_at_the_operation_and_never_overwrites(tmp_path):
    from quadratus.session import _write_fixture_bound
    root = tmp_path / "project"
    (root / ".quadratus" / "capture-fixtures").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".quadratus" / "capture-fixtures" / "t6").symlink_to(outside)
    why = _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/x.csv", b"a,b\n")
    assert "refused at the operation" in why and not any(outside.iterdir())
    (root / ".quadratus" / "capture-fixtures" / "t6").unlink()
    (root / ".quadratus" / "capture-fixtures" / "t6").mkdir()
    (root / ".quadratus" / "capture-fixtures" / "t6" / "x.csv").write_text("existing")
    why = _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/x.csv", b"a,b\n")
    assert "appeared before publication" in why
    assert (root / ".quadratus" / "capture-fixtures" / "t6" / "x.csv").read_text() == "existing"
    assert _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/y.csv", b"a,b\n") == ""
    assert (root / ".quadratus" / "capture-fixtures" / "t6" / "y.csv").read_bytes() == b"a,b\n"
    (root / ".quadratus" / "capture-fixtures" / "t6" / "link.csv").symlink_to(outside / "escaped.csv")
    why = _write_fixture_bound(root, ".quadratus/capture-fixtures/t6/link.csv", b"a,b\n")
    assert why and not (outside / "escaped.csv").exists()


def test_a_duplicate_block_in_the_supply_round_is_a_problem_not_a_write(tmp_path, monkeypatch):
    twice = f"FIXTURE {FIXTURE}:\n```\na,b\n```\nFIXTURE {FIXTURE}:\n```\nc,d\n```\n"
    session, _, captures = _run(tmp_path, monkeypatch, supply=twice)
    assert not (tmp_path / "project" / FIXTURE).exists() and captures == []
    record = session.design_checks[-1]
    assert record["fixtures_written"] == [] and "more than one FIXTURE header" in record["fixture_problems"][0]
    assert record["verified"] is False


@pytest.mark.parametrize("link_at", [".quadratus", ".quadratus/capture-fixtures",
                                     ".quadratus/capture-fixtures/t6", FIXTURE])
def test_a_symlink_anywhere_on_the_fixture_path_refuses_the_write(tmp_path, monkeypatch, link_at):
    # Codex review of 351d3ba: four linked layouts wrote the bytes outside
    # the project before the capture's reader could refuse them.
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "project"
    (root / "templates").mkdir(parents=True)
    link = root / link_at
    link.parent.mkdir(parents=True, exist_ok=True)
    if link_at == FIXTURE:
        link.symlink_to(outside / "escaped.csv")
    else:
        link.symlink_to(outside)
    session, _, captures = _run(tmp_path, monkeypatch)
    assert not any(outside.iterdir()), "nothing written outside the project"
    record = session.design_checks[-1]
    assert record["fixtures_written"] == [] and "symlink" in record["fixture_problems"][0]
    assert record["verified"] is False and captures == []


def test_a_capped_supply_call_writes_nothing_and_does_not_stop_the_run(tmp_path, monkeypatch):
    """The fixture-supply call is bounded like the redeclare call; a cap on
    it is a supply that did not arrive, never a run stop (series
    rule-7590b13 f5 on the sibling path)."""
    from quadratus.providers import TurnLimitReached
    capped = TurnLimitReached("grok stopped at its turn limit (6 of 6) before finishing",
                              partial_text=SUPPLY, turns=6)
    session, prompts, captures = _run(tmp_path, monkeypatch, supply=capped)
    asks = [p for _, p in prompts if "The harness cannot capture this task yet" in p]
    assert len(asks) == 1 and captures == []
    assert not (tmp_path / "project" / FIXTURE).exists(), "the partial text is never read as a block"
    record = session.design_checks[-1]
    assert record["fixtures_written"] == [] and "stopped at 6 rounds" in record["fixture_problems"][0]
    assert record["verified"] is False and "could not be supplied" in record["problem"]
