"""Tests for floating-alias resolution.

The property being defended: a seat addressed by model line follows that line
forward, and the operator can always overrule what the table believes.
"""

from __future__ import annotations

import json

from quadratus.latest import (
    alias_env_var,
    alias_for,
    load_cache,
    record_resolution,
    resolution_source,
)
from quadratus.registry import ORCHESTRATOR_CHAIN, resolve

FABLE = "claude:fable"
ASTRA = "openai:gpt-6-astra"
GROK = "grok:default"        # vendor-default: names no model at all
PINNED = "openai:gpt-5.6-terra"  # names one release, deliberately


def test_the_seed_is_used_when_nothing_else_says_otherwise():
    assert alias_for(FABLE, env={}, cache={}) == resolve(FABLE).alias


def test_an_operator_override_beats_everything():
    """Whoever is reading the vendor's release notes outranks a cached probe
    and a compiled-in table both."""
    env = {alias_env_var(FABLE): "fable-6"}
    cache = {FABLE: {"alias": "fable"}}
    assert alias_for(FABLE, env=env, cache=cache) == "fable-6"
    assert resolution_source(FABLE, env=env, cache=cache)[1] == "operator override"


def test_a_probed_alias_beats_the_seed():
    """The probe is the only source grounded in evidence rather than belief."""
    cache = {ASTRA: {"alias": "gpt-6-astra-2026-11"}}
    assert alias_for(ASTRA, env={}, cache=cache) == "gpt-6-astra-2026-11"
    assert resolution_source(ASTRA, env={}, cache=cache)[1] == "probed"


def test_a_cached_alias_is_ignored_for_a_pinned_model():
    """A pinned row names one release on purpose; honouring a stale probe
    there would quietly move work onto a model the table does not name."""
    cache = {PINNED: {"alias": "grok-4.7"}}
    assert alias_for(PINNED, env={}, cache=cache) == resolve(PINNED).alias


def test_an_override_still_applies_to_a_pinned_model():
    """Ignoring the cache is about stale evidence, not about refusing the
    operator -- a pinned row is exactly what someone overrides at 2am."""
    env = {alias_env_var(PINNED): "grok-4.7"}
    assert alias_for(PINNED, env=env, cache={}) == "grok-4.7"


def test_a_vendor_default_row_resolves_to_no_model_at_all():
    """Not an alias: the CLI is handed no model flag and applies its own
    current default, so the vendor's own pointer is what moves."""
    assert alias_for(GROK, env={}, cache={}) == ""
    assert resolution_source(GROK, env={}, cache={})[1].startswith("the CLI's own default")


def test_a_stale_probe_cannot_pin_a_vendor_default_row():
    cache = {GROK: {"alias": "grok-4.5"}}
    assert alias_for(GROK, env={}, cache=cache) == ""


def test_env_var_names_are_derived_not_listed():
    """A new roster row should need no new plumbing."""
    assert alias_env_var("claude:fable") == "QUADRATUS_ALIAS_CLAUDE_FABLE"
    assert alias_env_var("openai:gpt-5.6-sol") == "QUADRATUS_ALIAS_OPENAI_GPT_5_6_SOL"


def test_an_unknown_key_degrades_instead_of_raising():
    """Routing tables and the roster can disagree mid-edit; a hard failure
    here would turn a typo into a dead run."""
    assert alias_for("nobody:nothing", env={}, cache={}) == "nothing"
    assert resolution_source("nobody:nothing", env={}, cache={})[1] == "unknown model"


def test_empty_and_whitespace_overrides_are_not_overrides():
    assert alias_for(FABLE, env={alias_env_var(FABLE): "   "}, cache={}) == "fable"


# -- the cache on disk -------------------------------------------------------


def test_a_recorded_resolution_round_trips(tmp_path):
    path = tmp_path / "aliases.json"
    record_resolution(FABLE, "fable", reported="Claude Fable 5.1", path=path)
    entry = load_cache(path)[FABLE]
    assert entry["alias"] == "fable"
    assert entry["reported"] == "Claude Fable 5.1"
    assert entry["probed_at"] > 0


def test_recording_preserves_other_models(tmp_path):
    path = tmp_path / "aliases.json"
    record_resolution(FABLE, "fable", path=path)
    record_resolution(ASTRA, "gpt-6-astra", path=path)
    assert set(load_cache(path)) == {FABLE, ASTRA}


def test_a_missing_cache_is_empty_rather_than_an_error(tmp_path):
    assert load_cache(tmp_path / "nope.json") == {}


def test_a_corrupt_cache_is_empty_rather_than_an_error(tmp_path):
    """This sits on the invocation path. A cache is an optimisation over the
    seed, never a dependency."""
    path = tmp_path / "aliases.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_cache(path) == {}


def test_a_cache_of_the_wrong_shape_is_ignored(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"models": ["not", "a", "dict"]}), encoding="utf-8")
    assert load_cache(path) == {}


def test_a_malformed_entry_falls_back_to_the_seed():
    for bad in ({}, {"alias": ""}, {"alias": None}, "nonsense"):
        assert alias_for(FABLE, env={}, cache={FABLE: bad}) == "fable"


# -- the property the whole module exists for --------------------------------


def test_every_orchestrator_seat_can_be_overridden_without_a_code_change():
    """The seat is picked once per session. If following a model line forward
    required editing Python, it would not get done."""
    for key in ORCHESTRATOR_CHAIN:
        env = {alias_env_var(key): "whatever-ships-next"}
        assert alias_for(key, env=env, cache={}) == "whatever-ships-next"
