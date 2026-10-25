"""Family resolution and effort at the Fleet, with fake providers
(quadratus.runtime over quadratus.latest and quadratus.effort): one answer
per job, drift recorded rather than followed, legacy keys honoured on the
ledger, and an effort the model's ladder lacks refused by name."""
import json

import pytest

from quadratus import checkpoint
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.runtime import Fleet, UnsupportedEffort
from tests.conftest import write_codex_catalog
from tests.test_runtime import FakeCLIProvider

SOL, LEGACY_SOL, FABLE, GROK = "openai:sol", "openai:gpt-5.6-sol", "claude:fable", "grok:default"


@pytest.fixture
def built(monkeypatch):
    made = {}

    def fake_build(name, settings, **kw):
        made[name] = made.get(name) or FakeCLIProvider(name)
        return made[name]
    monkeypatch.setattr("quadratus.runtime.build_provider", fake_build)
    return made


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_job_resolves_a_family_once_and_keeps_it_while_the_catalogue_moves(tmp_path, monkeypatch, built):
    path = write_codex_catalog(tmp_path / "models_cache.json")
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(path))
    fleet = Fleet(Settings(backend="cli"))
    fleet.invoke(SOL, "hello")
    assert built["openai"].calls[-1]["model"] == "gpt-6.1-sol"
    write_codex_catalog(path, models=[dict(slug="gpt-7-sol", supported_reasoning_levels=["low", "medium", "high"])])
    fleet.invoke(SOL, "again")
    assert built["openai"].calls[-1]["model"] == "gpt-6.1-sol", "no mid-job drift"
    record = fleet.resolution_record()
    assert record["resolutions"][SOL]["wire"] == "gpt-6.1-sol" and record["codex_catalog"]["status"] == "ok"
    fresh = Fleet(Settings(backend="cli"))
    fresh.invoke(SOL, "next job")
    assert built["openai"].calls[-1]["model"] == "gpt-7-sol", "the next job follows the client forward"


def test_a_resumed_episode_keeps_the_jobs_resolution_and_records_the_drift(tmp_path, monkeypatch, built):
    path = write_codex_catalog(tmp_path / "models_cache.json")
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(path))
    first = Fleet(Settings(backend="cli"))
    first.invoke(SOL, "hello")
    carried = first.resolution_record()["resolutions"]
    write_codex_catalog(path, models=[dict(slug="gpt-7-sol", supported_reasoning_levels=["low", "medium", "high"])])
    resumed = Fleet(Settings(backend="cli"), resolutions=carried)
    resumed.invoke(SOL, "resumed")
    assert built["openai"].calls[-1]["model"] == "gpt-6.1-sol"
    held = resumed.resolution_record()["resolutions"][SOL]
    assert held["source"].endswith("(frozen)") and "now resolves sol to 'gpt-7-sol'" in held["provenance"]["drift"]
    # A frozen record is kept even when the current catalogue cannot resolve the family at all.
    path.unlink()
    again = Fleet(Settings(backend="cli"), resolutions=carried)
    again.invoke(SOL, "still")
    assert built["openai"].calls[-1]["model"] == "gpt-6.1-sol"
    assert "cannot resolve it now" in again.resolution_record()["resolutions"][SOL]["provenance"]["drift"]
    assert "model_resolution" in checkpoint._CARRIED, "the record rides the checkpoint"


def test_an_unavailable_family_is_not_available_and_names_its_reason(tmp_path, monkeypatch, built):
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(tmp_path / "absent.json"))
    fleet = Fleet(Settings(backend="cli"))
    assert fleet.available(FABLE) and fleet.available(GROK) and not fleet.available(SOL)
    with pytest.raises(Exception, match="catalogue missing"):
        fleet.invoke(SOL, "hello")
    assert built["openai"].calls == []
    lines = fleet.alias_lines()
    assert any(line.startswith("openai:sol -> unavailable (catalogue missing") for line in lines)
    assert any("claude:fable -> 'fable' (family alias" in line for line in lines)
    status = [line for line in fleet.status_lines() if line.startswith("openai")][0]
    assert "catalogue missing" in status


def test_a_legacy_key_reaches_the_wire_as_its_family_and_the_ledger_keeps_both(tmp_path, built):
    ledger = DelegationLedger(path=tmp_path / "invocations.jsonl")
    fleet = Fleet(Settings(backend="cli"), delegation_ledger=ledger)
    with invocation("t1", "lead"):
        fleet.invoke(LEGACY_SOL, "hello")
    assert built["openai"].calls[-1]["model"] == "gpt-6.1-sol"
    row = [r for r in _rows(tmp_path / "invocations.jsonl") if r["invoked"]][-1]
    assert (row["requested_model"], row["canonical_model"], row["wire_model"]) == (LEGACY_SOL, SOL, "gpt-6.1-sol")
    assert row["effort"] == "medium" and row["effort_policy"] == "effort/1" and row["effort_reasons"]
    assert row["resolution_source"] == "latest sol in the installed client's catalogue"


def test_every_call_carries_an_explicit_effort_decided_from_the_context(tmp_path, built):
    ledger = DelegationLedger(path=tmp_path / "invocations.jsonl")
    fleet = Fleet(Settings(backend="cli"), delegation_ledger=ledger)
    fleet.invoke(FABLE, "no context at all")
    with invocation("t1", "collaborator", effort_inputs=dict(role="collaborator", complexity="complex")):
        fleet.invoke(SOL, "review")
    with invocation("t1", "revision", effort_inputs=dict(role="revision", complexity="simple", cycle=2)):
        fleet.invoke(SOL, "fix")
    with invocation("t1", "worker", origin="worker"):
        fleet.invoke("openai:luna", "errand")
    rows = [r for r in _rows(tmp_path / "invocations.jsonl") if r["invoked"]]
    assert [(r["role"], r["effort"]) for r in rows] == [("direct", "medium"), ("collaborator", "high"),
                                                        ("revision", "high"), ("worker", "low")]
    assert all(r["effort"] for r in rows), "never the CLI's own default"
    assert rows[-1]["effort_reasons"][0].startswith("bounded seat")


def test_an_effort_the_resolved_model_lacks_is_refused_by_name(tmp_path, monkeypatch, built):
    narrow = write_codex_catalog(tmp_path / "models_cache.json", models=[
        dict(slug="gpt-6.1-sol", supported_reasoning_levels=["low", "medium"])])
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(narrow))
    fleet = Fleet(Settings(backend="cli"))
    with invocation("t1", "lead", effort_inputs=dict(role="lead", complexity="simple")):
        fleet.invoke(SOL, "medium is on the ladder")
    assert built["openai"].calls[-1]["model"] == "gpt-6.1-sol"
    with invocation("audit", "auditor", effort_inputs=dict(role="auditor")):
        with pytest.raises(UnsupportedEffort, match="high is not on the openai ladder \\(low, medium\\)"):
            fleet.invoke(SOL, "the audit asks for high")
    assert len(built["openai"].calls) == 1, "refused before any call"


def test_a_forked_fleet_shares_the_jobs_resolutions(tmp_path, monkeypatch, built):
    path = write_codex_catalog(tmp_path / "models_cache.json")
    monkeypatch.setenv("QUADRATUS_CODEX_CATALOG", str(path))
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n")
    parent = Fleet(Settings(backend="cli"), project=project)
    parent._resolution(SOL)
    write_codex_catalog(path, models=[dict(slug="gpt-7-sol", supported_reasoning_levels=["low"])])
    child = parent.fork(project)
    assert child._resolution(SOL).wire == "gpt-6.1-sol" and child.resolution_record()["codex_catalog"] == \
        parent.resolution_record()["codex_catalog"]


def test_the_grok_default_is_read_from_the_listing_once_and_recorded_beside_the_seat(tmp_path, monkeypatch, built):
    from types import SimpleNamespace
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="grok-4.7 (default)\ngrok-4.6\nYou are not authenticated.\n", stderr="")
    monkeypatch.setenv("QUADRATUS_GROK_MODELS", "1")
    monkeypatch.setattr("quadratus.catalog.subprocess.run", run)
    built_grok = FakeCLIProvider("grok")
    built_grok._client = "/opt/homebrew/bin/grok"
    monkeypatch.setattr("quadratus.runtime.build_provider", lambda name, settings, **kw: built_grok if name == "grok" else FakeCLIProvider(name))
    fleet = Fleet(Settings(backend="cli"))
    fleet.invoke(GROK, "hello")
    fleet.invoke(GROK, "again")
    assert seen == [["/opt/homebrew/bin/grok", "models"]], "one listing per job, no generation"
    assert built_grok.calls[-1]["model"] == "", "the seat still sends no model flag"
    record = fleet.resolution_record()
    assert record["resolutions"][GROK]["observed_default"] == "grok-4.7"
    assert record["grok_models"]["notices"] == ["You are not authenticated."]
    assert "grok models: ok, default grok-4.7" in [line for line in fleet.status_lines() if line.startswith("grok")][0]
    monkeypatch.setenv("QUADRATUS_GROK_MODELS", "0")
    off = Fleet(Settings(backend="cli"))
    off.invoke(GROK, "hello")
    assert off.resolution_record()["grok_models"]["reason"] == "disabled by QUADRATUS_GROK_MODELS"
