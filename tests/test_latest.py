"""Tests for family resolution (quadratus.latest, 2026-10-24).

The property being defended: a seat addressed by family follows that family
forward on the authority of the installed client, the operator can always
overrule it visibly, and nothing ever selects an older edition from memory.
"""

from __future__ import annotations

import json
import os

import pytest

from quadratus.latest import (
    ModelUnavailable,
    alias_env_var,
    alias_for,
    load_cache,
    record_resolution,
    resolution_source,
    resolve_model,
)
from quadratus.registry import ORCHESTRATOR_CHAIN, canonical_key, resolve
from tests.conftest import write_codex_catalog

FABLE = "claude:fable"
ASTRA = "openai:astra"
SOL = "openai:sol"
GROK = "grok:default"        # vendor-default: names no model at all
LEGACY_SOL = "openai:gpt-5.6-sol"


def _env(**extra):
    """No operator override; the test session's catalogue."""
    return {"QUADRATUS_CODEX_CATALOG": os.environ["QUADRATUS_CODEX_CATALOG"], **extra}


def test_a_claude_family_alias_goes_on_the_wire_as_written():
    assert alias_for(FABLE, env=_env()) == resolve(FABLE).alias == "fable"
    assert resolution_source(FABLE, env=_env()) == ("fable", "family alias; the CLI resolves it")


def test_an_operator_override_beats_everything():
    """Whoever is reading the vendor's release notes outranks the client's
    catalogue and the table both, and the record says so."""
    env = _env(**{alias_env_var(FABLE): "fable-6"})
    assert alias_for(FABLE, env=env) == "fable-6"
    assert resolution_source(FABLE, env=env)[1] == "operator override"
    pinned = resolve_model(SOL, env=_env(**{alias_env_var(SOL): "gpt-6-sol"}))
    assert pinned.wire == "gpt-6-sol" and pinned.supported == ("low", "medium", "high", "xhigh")
    assert pinned.provenance["note"] == "pinned slug is in the catalogue"
    outside = resolve_model(SOL, env=_env(**{alias_env_var(SOL): "gpt-9-sol-preview"}))
    assert outside.wire == "gpt-9-sol-preview" and outside.supported is None
    assert "not in the catalogue" in outside.provenance["note"]


def test_a_stale_probe_record_never_defeats_a_family_alias():
    """The alias cache is a probe's record, not a source: a dated id probed
    months ago must not pin the seat the CLI keeps current."""
    cache = {FABLE: {"alias": "claude-fable-5-20260301"}, ASTRA: {"alias": "gpt-6-astra-2026-02"}}
    assert alias_for(FABLE, env=_env(), cache=cache) == "fable"
    assert alias_for(ASTRA, env=_env(), cache=cache) == "gpt-6-astra"


def test_an_openai_family_resolves_to_the_catalogues_latest_release():
    found = resolve_model(SOL, env=_env())
    assert found.available and found.wire == "gpt-6.1-sol" and found.family == "sol"
    assert found.source == "latest sol in the installed client's catalogue"
    assert found.supported == ("low", "medium", "high", "xhigh", "max"), "ultra is listed, never usable"
    assert found.provenance["catalog"]["status"] == "ok" and found.provenance["listed"] == ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"]
    assert resolve_model("openai:terra", env=_env()).wire == "gpt-5.6-terra", "the latest Terra the client lists"
    assert resolve_model("openai:luna", env=_env()).wire == "gpt-6-luna"


def test_a_legacy_version_key_resolves_to_its_family(tmp_path):
    assert canonical_key(LEGACY_SOL) == SOL and resolve(LEGACY_SOL) is resolve(SOL)
    found = resolve_model(LEGACY_SOL, env=_env())
    assert found.requested == LEGACY_SOL and found.key == SOL and found.wire == "gpt-6.1-sol"
    assert alias_env_var(LEGACY_SOL) == alias_env_var(SOL) == "QUADRATUS_ALIAS_OPENAI_SOL"


def test_a_missing_stale_or_malformed_catalogue_is_unavailable_with_the_reason(tmp_path):
    missing = resolve_model(SOL, env={"QUADRATUS_CODEX_CATALOG": str(tmp_path / "none.json")})
    assert not missing.available and missing.wire is None and missing.reason.startswith("catalogue missing")
    with pytest.raises(ModelUnavailable, match="catalogue missing"):
        alias_for(SOL, env={"QUADRATUS_CODEX_CATALOG": str(tmp_path / "none.json")})
    stale = write_codex_catalog(tmp_path / "stale.json", fetched_at="2026-09-01T00:00:00Z")
    found = resolve_model(SOL, env={"QUADRATUS_CODEX_CATALOG": str(stale)})
    assert not found.available and found.reason.startswith("catalogue stale")
    assert resolution_source(SOL, env={"QUADRATUS_CODEX_CATALOG": str(stale)})[1].startswith("unavailable: catalogue stale")
    (tmp_path / "bad.json").write_text("{")
    assert "malformed" in resolve_model(SOL, env={"QUADRATUS_CODEX_CATALOG": str(tmp_path / "bad.json")}).reason


def test_a_family_the_catalogue_does_not_list_is_unavailable_not_guessed(tmp_path):
    only = write_codex_catalog(tmp_path / "only.json", models=[dict(slug="gpt-6-luna", visibility="list",
                                                                   supported_reasoning_levels=["low", "medium"])])
    found = resolve_model(SOL, env={"QUADRATUS_CODEX_CATALOG": str(only)})
    assert not found.available and found.reason == "no sol model in the catalogue"


def test_a_future_release_moves_the_family_forward_by_number(tmp_path):
    later = write_codex_catalog(tmp_path / "later.json", models=[
        dict(slug="gpt-6.1-sol", supported_reasoning_levels=["low", "medium", "high"]),
        dict(slug="gpt-7-sol", supported_reasoning_levels=["low", "medium", "high", "xhigh"]),
        dict(slug="gpt-6.10-sol", supported_reasoning_levels=["low"]),
    ])
    found = resolve_model(SOL, env={"QUADRATUS_CODEX_CATALOG": str(later)})
    assert found.wire == "gpt-7-sol" and found.supported == ("low", "medium", "high", "xhigh")


def test_a_vendor_default_row_resolves_to_no_model_at_all():
    """Not an alias: the CLI is handed no model flag and applies its own
    current default, so the vendor's own pointer is what moves."""
    assert alias_for(GROK, env=_env()) == ""
    assert resolution_source(GROK, env=_env())[1].startswith("the CLI's own default")
    assert alias_for(GROK, env=_env(), cache={GROK: {"alias": "grok-4.5"}}) == ""


def test_the_observed_grok_default_is_recorded_beside_a_vendor_default_row():
    from quadratus.catalog import parse_grok_models
    listing = parse_grok_models("grok-4.7 (default)\ngrok-4.6\n")
    found = resolve_model(GROK, env=_env(), grok=listing)
    assert found.wire == "" and found.observed_default == "grok-4.7"
    assert found.provenance["grok"]["available"] == ["grok-4.7", "grok-4.6"]


def test_env_var_names_are_derived_not_listed():
    """A new roster row should need no new plumbing."""
    assert alias_env_var("claude:fable") == "QUADRATUS_ALIAS_CLAUDE_FABLE"
    assert alias_env_var("openai:sol") == "QUADRATUS_ALIAS_OPENAI_SOL"


def test_an_unknown_key_is_unavailable_with_the_reason():
    found = resolve_model("nobody:nothing", env=_env())
    assert not found.available and "outside the active roster" in found.reason
    assert resolution_source("nobody:nothing", env=_env()) == ("", "unavailable: nobody:nothing is outside the active roster")
    with pytest.raises(ModelUnavailable):
        alias_for("nobody:nothing", env=_env())


def test_empty_and_whitespace_overrides_are_not_overrides():
    assert alias_for(FABLE, env=_env(**{alias_env_var(FABLE): "   "})) == "fable"


# -- the probe's record on disk ----------------------------------------------


def test_a_recorded_resolution_round_trips_under_the_family_key(tmp_path):
    path = tmp_path / "aliases.json"
    record_resolution(FABLE, "fable", reported="Claude Fable 5.1", path=path)
    record_resolution(LEGACY_SOL, "gpt-6.1-sol", path=path)
    entries = load_cache(path)
    assert entries[FABLE]["alias"] == "fable" and entries[FABLE]["reported"] == "Claude Fable 5.1"
    assert entries[FABLE]["probed_at"] > 0 and set(entries) == {FABLE, SOL}


def test_a_missing_or_corrupt_record_is_empty_rather_than_an_error(tmp_path):
    assert load_cache(tmp_path / "nope.json") == {}
    (tmp_path / "aliases.json").write_text("{not json at all", encoding="utf-8")
    assert load_cache(tmp_path / "aliases.json") == {}
    (tmp_path / "shape.json").write_text(json.dumps({"models": ["not", "a", "dict"]}), encoding="utf-8")
    assert load_cache(tmp_path / "shape.json") == {}


# -- the property the whole module exists for --------------------------------


def test_every_orchestrator_seat_can_be_overridden_without_a_code_change():
    """The seat is picked once per session. If following a model line forward
    required editing Python, it would not get done."""
    for key in ORCHESTRATOR_CHAIN:
        env = _env(**{alias_env_var(key): "whatever-ships-next"})
        assert alias_for(key, env=env) == "whatever-ships-next"


def test_every_orchestrator_seat_resolves_on_the_installed_clients_authority():
    assert resolve_model("claude:fable", env=_env()).wire == "fable"
    assert resolve_model("openai:astra", env=_env()).wire == "gpt-6-astra"
