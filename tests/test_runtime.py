"""Tests for the bridge between roster keys and the vendor CLIs.

No subprocess is ever started: ``build_provider`` is replaced with a fake, so
these exercise the fleet's own decisions -- which provider answers for a key,
what alias it is handed, and what happens when a subscription window runs out.
"""

from __future__ import annotations

import pytest

from quadratus.config import Settings
from quadratus.latest import alias_env_var
from quadratus.providers import LLMProvider, ProviderError
from quadratus.registry import VENDORS
from quadratus.runtime import Fleet, UnknownModel, WindowExhausted
from quadratus.usage import UsageMeter

FABLE = "claude:fable"
OPUS = "claude:opus"
SOL = "openai:gpt-5.6-sol"
GROK = "grok:default"


class FakeCLIProvider(LLMProvider):
    """Stands in for a signed-in vendor CLI."""

    def __init__(self, name, *, installed=True, raises=None, usage=None):
        self.name = name
        self.label = name.title()
        self.model = ""
        self.calls = []
        self.cleaned = False
        self._raises = raises
        self.last_usage = usage
        self._usage = usage
        self._client = object() if installed else None
        self._init_error = None if installed else "'x' is not on PATH"
        self.max_retries = 1
        self.retry_base_delay = 0.0
        self.timeout = 1.0
        self.max_tokens = 100
        self.api_key = "cli-oauth"
        self.refusal_fallback_model = None

    def generate(self, prompt, *, system="", history=None):
        if not self.available():
            raise ProviderError(f"{self.label} is not available: {self._init_error}.")
        self.calls.append({"model": self.model, "prompt": prompt, "system": system})
        if self._raises is not None:
            raise self._raises
        self.last_usage = self._usage
        return f"[{self.name}:{self.model}] ok"

    def cleanup(self):
        self.cleaned = True


@pytest.fixture
def built(monkeypatch):
    """Every vendor present and healthy, plus a handle on what was built."""
    made = {}

    def fake_build(name, settings, **kw):
        made[name] = made.get(name) or FakeCLIProvider(name)
        return made[name]

    monkeypatch.setattr("quadratus.runtime.build_provider", fake_build)
    return made


@pytest.fixture
def fleet(built):
    return Fleet(Settings(backend="cli"))


# -- addressing --------------------------------------------------------------


def test_one_provider_serves_every_model_from_its_vendor(fleet, built):
    """A provider resolves a binary and owns a scratch directory; building one
    per roster key would multiply both for nothing, and would cost the vendor's
    warm prompt cache."""
    fleet.invoke(FABLE, "hello")
    fleet.invoke(OPUS, "hello")
    assert list(built) == ["claude"]
    assert [c["model"] for c in built["claude"].calls] == ["fable", "opus"]


def test_each_call_is_bound_to_the_alias_the_key_resolves_to(fleet, built):
    fleet.invoke(SOL, "hello")
    assert built["openai"].calls[0]["model"] == "gpt-5.6-sol"


def test_api_roster_models_do_not_collapse_to_one_provider_model(built):
    fleet = Fleet(Settings(backend='api'))
    fleet.invoke('openai:gpt-5.6-sol', 'hello')
    fleet.invoke('openai:gpt-5.6-luna', 'hello')
    assert [c['model'] for c in built['openai'].calls] == ['gpt-5.6-sol', 'gpt-5.6-luna']


def test_api_claude_seats_require_explicit_ids_except_configured_opus(built, monkeypatch):
    fleet = Fleet(Settings(backend='api'))
    assert not fleet.available('claude:fable')
    monkeypatch.setenv('QUADRATUS_API_MODEL_CLAUDE_FABLE', 'exact-fable-id')
    fleet.invoke('claude:fable', 'hello')
    assert built['claude'].calls[0]['model'] == 'exact-fable-id'


def test_an_operator_override_reaches_the_wire(fleet, built, monkeypatch):
    monkeypatch.setenv(alias_env_var(FABLE), "fable-next")
    fleet.invoke(FABLE, "hello")
    assert built["claude"].calls[0]["model"] == "fable-next"


def test_the_api_backend_uses_the_configured_model_id_not_an_alias(built):
    """An API takes no aliases, so there is nothing there that could follow a
    model line forward -- which is half the reason the CLI is the default."""
    fleet = Fleet(Settings(backend="api", claude_model="claude-opus-5"))
    fleet.invoke(OPUS, "hello")
    assert built["claude"].calls[0]["model"] == "claude-opus-5"


def test_a_model_from_outside_the_lineup_is_refused(fleet):
    with pytest.raises(UnknownModel, match="not in this lineup"):
        fleet.invoke("gemini:gemini-3.1-pro-preview", "hello")


# -- liveness ----------------------------------------------------------------


def test_everything_untried_is_assumed_available(fleet):
    """A pessimistic liveness check would halt runs that would have worked."""
    assert all(fleet.available(k) for k in (FABLE, OPUS, SOL, GROK))


def test_a_missing_binary_reports_the_vendor_unavailable(monkeypatch):
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(name, installed=(name != "grok")),
    )
    fleet = Fleet(Settings(backend="cli"))
    assert fleet.available(SOL)
    assert not fleet.available(GROK)


def test_a_model_outside_the_lineup_is_never_available(fleet):
    assert not fleet.available("gemini:gemini-3.6-flash")
    assert not fleet.available("claude:nonexistent")


# -- exhausted subscription windows ------------------------------------------


def test_an_exhausted_window_is_reported_as_such_not_as_a_transport_error(monkeypatch):
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(
            name, raises=ProviderError("Claude usage limit reached; resets at 4pm")
        ),
    )
    fleet = Fleet(Settings(backend="cli"))
    with pytest.raises(WindowExhausted):
        fleet.invoke(OPUS, "hello")


def test_an_exhausted_model_is_routed_around_for_the_rest_of_the_run(monkeypatch):
    """Retrying spends nothing and gains nothing until the window rolls over,
    so the policy layers need to see it as a routing fact."""
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(
            name, raises=ProviderError("weekly limit reached")
        ),
    )
    fleet = Fleet(Settings(backend="cli"))
    assert fleet.available(FABLE)
    with pytest.raises(WindowExhausted):
        fleet.invoke(FABLE, "hello")
    assert not fleet.available(FABLE)
    assert FABLE in fleet.exhausted


def test_one_model_running_out_does_not_take_its_vendor_with_it(monkeypatch):
    """These subscriptions meter per model. Observed 2026-09-12: the Claude
    CLI answered "You've reached your Fable limit. Switch to another model"
    while Opus answered normally in the same session. Marking the vendor down
    would take the whole Anthropic lineup out of a run over one model --
    exactly the failure the orchestrator fallback exists to prevent."""
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(
            name, raises=ProviderError("You've reached your Fable limit.")
        ),
    )
    fleet = Fleet(Settings(backend="cli"))
    with pytest.raises(WindowExhausted):
        fleet.invoke(FABLE, "hello")
    assert not fleet.available(FABLE)
    assert fleet.available(OPUS), "a sibling model is not out because Fable is"
    assert fleet.available(SOL)


def test_the_real_claude_limit_wording_is_recognised(monkeypatch):
    """The patterns written from imagination missed the one the CLI actually
    sends, so a spent window looked like a generic transport error."""
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(
            name,
            raises=ProviderError(
                "claude reported an error: You've reached your Fable limit. "
                "Switch to another model, or manage usage credits at claude.ai"
            ),
        ),
    )
    fleet = Fleet(Settings(backend="cli"))
    with pytest.raises(WindowExhausted):
        fleet.invoke(FABLE, "hello")


def test_an_ordinary_failure_does_not_take_the_vendor_down(monkeypatch):
    """Treating a transient 429 as an exhausted window would sideline a
    healthy vendor for the whole session."""
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(name, raises=RuntimeError("connection reset")),
    )
    fleet = Fleet(Settings(backend="cli"))
    with pytest.raises(RuntimeError):
        fleet.invoke(OPUS, "hello")
    assert fleet.available(OPUS)


def test_the_fleet_never_substitutes_a_different_model(monkeypatch):
    """Routing decisions are made with reasons upstream; a transport quietly
    swapping models would make those decisions unfalsifiable."""
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(name, installed=(name == "openai")),
    )
    fleet = Fleet(Settings(backend="cli"))
    with pytest.raises(ProviderError):
        fleet.invoke(OPUS, "hello")


# -- metering ----------------------------------------------------------------


def test_reported_token_counts_are_recorded_as_measured(monkeypatch):
    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(
            name, usage={"input_tokens": 1200, "output_tokens": 340}
        ),
    )
    meter = UsageMeter()
    Fleet(Settings(backend="cli"), usage_meter=meter).invoke(OPUS, "hello")
    (record,) = meter.records
    assert record.measured
    assert (record.input_tokens, record.output_tokens) == (1200, 340)
    assert record.model == OPUS


def test_a_cli_that_reports_nothing_falls_back_to_an_estimate(fleet, built):
    meter = UsageMeter()
    Fleet(Settings(backend="cli"), usage_meter=meter).invoke(OPUS, "x" * 400)
    (record,) = meter.records
    assert not record.measured
    assert record.input_tokens > 0


def test_metering_failure_never_fails_a_call(fleet, monkeypatch):
    class BrokenMeter(UsageMeter):
        def record(self, **kwargs):
            raise RuntimeError("meter exploded")

    metered = Fleet(Settings(backend="cli"), usage_meter=BrokenMeter())
    assert metered.invoke(OPUS, "hello")


# -- housekeeping ------------------------------------------------------------


def test_closing_drops_every_scratch_directory(fleet, built):
    fleet.invoke(OPUS, "hello")
    fleet.invoke(SOL, "hello")
    fleet.close()
    assert all(p.cleaned for p in built.values())


def test_the_fleet_is_a_context_manager(built):
    with Fleet(Settings(backend="cli")) as fleet:
        fleet.invoke(OPUS, "hello")
    assert built["claude"].cleaned


def test_status_covers_every_vendor_in_the_lineup(fleet):
    lines = fleet.status_lines()
    assert len(lines) == len(VENDORS)
    assert all(any(line.startswith(v) for line in lines) for v in VENDORS)


def test_status_says_which_model_is_spent(fleet):
    fleet.mark_exhausted(FABLE, "You've reached your Fable limit.")
    line = next(line for line in fleet.status_lines() if line.startswith("claude"))
    assert "window exhausted" in line and FABLE in line


# -- wiring the session engine to real subscriptions -------------------------


def test_new_session_drives_the_run_loop_through_the_fleet(built, tmp_path):
    """The run loop takes invoke/available as callables so a fake can drive it
    in tests. This is the other half: the same two, backed by subscriptions."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.runtime import new_session

    fleet = Fleet(Settings(backend="cli"))
    session = new_session("build a parser", ArtifactStore(tmp_path / "a"), fleet=fleet)
    session.next_task()
    assert built["claude"].calls, "the orchestrator seat was asked for a decision"
    assert built["claude"].calls[0]["model"] == "fable"


def test_new_session_reads_liveness_from_the_fleet(monkeypatch, tmp_path):
    """A vendor whose CLI is missing must reach the seat logic as a liveness
    fact, or the run halts on a model it could have routed around."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.routing import SeatReason
    from quadratus.runtime import new_session

    monkeypatch.setattr(
        "quadratus.runtime.build_provider",
        lambda name, settings, **kw: FakeCLIProvider(name, installed=(name != "claude")),
    )
    session = new_session("build a parser", ArtifactStore(tmp_path / "a"))
    seat = session.seat()
    # A missing binary takes the whole vendor out, which is the one failure
    # the same-vendor deputy cannot cover -- so the seat crosses vendors.
    assert seat.key == "openai:gpt-6-astra"
    assert seat.reason == SeatReason.FALLBACK_UNAVAILABLE


def test_the_meter_is_not_attached_twice(built, tmp_path):
    """The fleet sees the CLI's own token counts; the session only sees the
    strings. Metering both would double the counterfactual bill."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.runtime import new_session
    from quadratus.session import SessionConfig

    meter = UsageMeter()
    fleet = Fleet(Settings(backend="cli"), usage_meter=meter)
    session = new_session(
        "build a parser",
        ArtifactStore(tmp_path / "a"),
        fleet=fleet,
        config=SessionConfig(usage_meter=meter),
    )
    session.next_task()
    assert len(meter.records) == 1
