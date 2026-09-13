"""Tests for the CLI probe.

The probe is how a table full of second-hand aliases becomes a table grounded
in what the installed binaries actually accept. These check that it walks the
alias chain only where walking it is legitimate, records what worked, and
reports a failure rather than papering over it.
"""

from __future__ import annotations

import pytest

from quadratus.config import Settings
from quadratus.latest import alias_for, load_cache
from quadratus.probe import (
    default_probe_set,
    probe_binaries,
    probe_models,
    render_report,
    roster_keys,
)
from quadratus.providers import ProviderError
from quadratus.registry import MODE_ROSTERS, ORCHESTRATOR_CHAIN, VENDORS
from quadratus.runtime import Fleet
from tests.test_runtime import FakeCLIProvider

ASTRA = "openai:gpt-6-astra"
FABLE = "claude:fable"
PINNED_ROW = "openai:gpt-5.6-terra"   # a genuinely pinned row
GROK_DEFAULT = "grok:default"


class PickyProvider(FakeCLIProvider):
    """A CLI that accepts only the aliases it was told about."""

    def __init__(self, name, accepts=()):
        super().__init__(name)
        self.accepts = set(accepts)
        self.tried = []

    def generate(self, prompt, *, system="", history=None):
        self.tried.append(self.model)
        if self.model not in self.accepts:
            raise ProviderError(f"unknown model {self.model!r}")
        return f"{self.model}-actual"


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "aliases.json"
    monkeypatch.setenv("QUADRATUS_ALIAS_CACHE", str(path))
    return path


def _fleet(monkeypatch, providers):
    monkeypatch.setattr(
        "quadratus.runtime.build_provider", lambda name, settings, **kw: providers[name]
    )
    return Fleet(Settings(backend="cli"))


# -- what gets probed --------------------------------------------------------


def test_the_default_set_is_the_seats_a_run_cannot_start_without():
    """Probing costs a call per model against the same windows the run then
    has to share, so the default is the models whose absence stops a run."""
    keys = default_probe_set()
    assert set(ORCHESTRATOR_CHAIN) <= set(keys)
    assert set(MODE_ROSTERS["adversarial"]["peers"]) <= set(keys)
    assert len(keys) == len(set(keys)), "no model is probed twice"
    assert len(keys) < len(roster_keys())


def test_probing_everything_is_opt_in():
    assert set(default_probe_set()) < set(roster_keys())


def test_binary_status_covers_the_lineup():
    statuses = probe_binaries(Settings(backend="cli"))
    assert [s.vendor for s in statuses] == list(VENDORS)
    assert all(s.binary for s in statuses)


def test_a_vendor_on_the_api_backend_needs_no_binary():
    (claude,) = [
        s for s in probe_binaries(Settings(backend="api")) if s.vendor == "claude"
    ]
    assert claude.ok, "an API-backed vendor is fine without a CLI installed"


# -- walking the alias chain -------------------------------------------------


def test_a_working_alias_is_recorded_and_used_next_time(monkeypatch, cache):
    providers = {"claude": PickyProvider("claude", accepts={"fable"})}
    fleet = _fleet(monkeypatch, providers)
    (result,) = probe_models([FABLE], fleet=fleet)
    assert result.ok and result.alias == "fable"
    assert result.reported == "fable-actual"
    assert load_cache(cache)[FABLE]["alias"] == "fable"
    assert alias_for(FABLE) == "fable"


def test_probe_honors_explicit_alias_without_trying_seed(monkeypatch, cache):
    monkeypatch.setenv('QUADRATUS_ALIAS_CLAUDE_FABLE', 'chosen-alias')
    provider = PickyProvider('claude', accepts={'chosen-alias'})
    fleet = _fleet(monkeypatch, {'claude': provider})
    result, = probe_models([FABLE], fleet=fleet)
    assert result.ok and result.alias == 'chosen-alias'
    assert provider.tried == ['chosen-alias']


def test_binary_probe_uses_operator_binary_override(monkeypatch):
    monkeypatch.setenv('QUADRATUS_CLI_BINARY_GROK', '/custom/grok')
    monkeypatch.setattr('shutil.which', lambda binary: binary if binary == '/custom/grok' else None)
    grok = next(s for s in probe_binaries(Settings(backend='cli')) if s.vendor == 'grok')
    assert grok.ok and grok.path == '/custom/grok'


def test_a_floating_model_falls_through_to_its_next_candidate(monkeypatch, cache):
    """Where a vendor will not take a bare line name, the probe is what finds
    the name it will take."""
    providers = {"openai": PickyProvider("openai", accepts={"gpt-6"})}
    fleet = _fleet(monkeypatch, providers)
    (result,) = probe_models([ASTRA], fleet=fleet)
    assert result.ok and result.alias == "gpt-6"
    assert result.rejected == ("gpt-6-astra",)
    assert providers["openai"].tried == ["gpt-6-astra", "gpt-6"]


def test_a_pinned_model_is_tried_once_and_never_walked(monkeypatch, cache):
    """A pinned row names one release deliberately; quietly succeeding on a
    different one would make the table a lie."""
    providers = {"openai": PickyProvider("openai", accepts={"gpt-5.6-luna"})}
    fleet = _fleet(monkeypatch, providers)
    (result,) = probe_models([PINNED_ROW], fleet=fleet)
    assert not result.ok
    assert providers["openai"].tried == ["gpt-5.6-terra"]


def test_a_vendor_default_row_is_probed_by_naming_no_model(monkeypatch, cache):
    """There is no alias to walk. What the probe is asking is whether the CLI
    answers at all, and what it says it is running."""
    providers = {"grok": PickyProvider("grok", accepts={""})}
    fleet = _fleet(monkeypatch, providers)
    (result,) = probe_models([GROK_DEFAULT], fleet=fleet)
    assert result.ok and result.alias == ""
    assert providers["grok"].tried == [""]
    assert load_cache(cache) == {}, "there is no alias worth remembering"


def test_a_total_failure_is_reported_with_the_reason(monkeypatch, cache):
    providers = {"claude": PickyProvider("claude", accepts=set())}
    fleet = _fleet(monkeypatch, providers)
    (result,) = probe_models([FABLE], fleet=fleet)
    assert not result.ok
    assert "unknown model" in result.detail
    assert load_cache(cache) == {}, "nothing is cached from a failure"


def test_a_key_outside_the_roster_is_reported_not_raised(monkeypatch, cache):
    fleet = _fleet(monkeypatch, {})
    (result,) = probe_models(["gemini:gemini-3.1-pro-preview"], fleet=fleet)
    assert not result.ok and "not in the roster" in result.detail


def test_the_probe_never_raises_on_a_dead_vendor(monkeypatch, cache):
    """Finding out a CLI is missing is the probe's whole job; it must report
    that, not crash on it."""
    providers = {"claude": FakeCLIProvider("claude", installed=False)}
    fleet = _fleet(monkeypatch, providers)
    (result,) = probe_models([FABLE], fleet=fleet)
    assert not result.ok


def test_writing_the_cache_can_be_suppressed(monkeypatch, cache):
    providers = {"claude": PickyProvider("claude", accepts={"fable"})}
    fleet = _fleet(monkeypatch, providers)
    probe_models([FABLE], fleet=fleet, write_cache=False)
    assert load_cache(cache) == {}


# -- the report --------------------------------------------------------------


def test_the_report_names_the_fix_for_a_failed_model(monkeypatch, cache):
    providers = {"claude": PickyProvider("claude", accepts=set())}
    fleet = _fleet(monkeypatch, providers)
    results = probe_models([FABLE], fleet=fleet)
    text = render_report(probe_binaries(Settings(backend="cli")), results)
    assert "QUADRATUS_ALIAS_CLAUDE_FABLE" in text


def test_the_report_calls_out_a_broken_orchestrator_chain(monkeypatch, cache):
    providers = {"claude": PickyProvider("claude", accepts=set())}
    fleet = _fleet(monkeypatch, providers)
    results = probe_models([FABLE], fleet=fleet)
    text = render_report([], results)
    assert "orchestrator chain is not whole" in text


def test_the_report_refuses_to_treat_a_self_reported_name_as_evidence(
    monkeypatch, cache
):
    """Models misname their own version routinely. The fact being recorded is
    that the round trip happened at all."""
    providers = {"claude": PickyProvider("claude", accepts={"fable"})}
    fleet = _fleet(monkeypatch, providers)
    results = probe_models([FABLE], fleet=fleet)
    text = render_report([], results)
    assert "not evidence" in text


# -- an exhausted window is not an alias problem -----------------------------


def test_a_spent_window_is_not_reported_as_a_bad_alias(monkeypatch, cache):
    """Observed on 2026-09-12: the Claude CLI returned "You've reached your
    Fable limit" and the report told the operator to go change a working
    alias -- sending them to fix the one thing that was not broken."""
    providers = {
        "claude": FakeCLIProvider(
            "claude",
            raises=ProviderError(
                "claude reported an error: You've reached your Fable limit. "
                "Switch to another model, or manage usage credits at claude.ai"
            ),
        )
    }
    fleet = _fleet(monkeypatch, providers)
    results = probe_models([FABLE], fleet=fleet)
    text = render_report([], results)
    assert "SPENT" in text
    assert "Nothing to change" in text
    assert "QUADRATUS_ALIAS_CLAUDE_FABLE" not in text


def test_a_seat_out_of_window_is_not_reported_as_a_broken_chain(monkeypatch, cache):
    """A fallback firing is the system working, not the system failing."""
    providers = {
        "claude": FakeCLIProvider(
            "claude", raises=ProviderError("You've reached your Fable limit.")
        )
    }
    fleet = _fleet(monkeypatch, providers)
    text = render_report([], probe_models([FABLE], fleet=fleet))
    assert "out of window" in text
    assert "not whole" not in text
