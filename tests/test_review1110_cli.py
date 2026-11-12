"""Regressions for the 2026-11-10 whole-repo review, command-line workstream.

A command-line run can be bounded like a GUI run, the budget arithmetic holds
when calls overlap, no flag is accepted and then ignored, and no mode seats a
bounded worker row or starts a run its own audit rule cannot let finish.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import cli, gui, project_run
from quadratus import decisions as D
from quadratus import registry as R
from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import ClaudeCLIProvider
from quadratus.config import Settings
from quadratus.project import Project
from quadratus.providers import ProviderError
from quadratus.run_budget import (
    LIMIT_FIELDS,
    LIMIT_LABELS,
    MIN_LEAD_TURNS,
    TURN_CONTEXT_TOKENS,
    APICostRate,
    RunBudget,
    RunBudgetExceeded,
    RunLimits,
    describe_limits,
    effective_lead_turns,
    lead_turns_for,
    limits_from_fields,
    whole_number,
)
from quadratus.session import Session, SessionConfig, SurveyConfig
from quadratus.session_knobs import SESSION_KNOBS, session_options
from quadratus.usage import DEFAULT_PRICE, PRICES, UsageMeter

GOAL = "Fix the bug"
DIAGNOSTIC = 'Diagnostic: 90 calls, 5M tokens, 1 hour'
DIAGNOSTIC_FLAGS = ["--max-calls", "90", "--max-reported-tokens", "5000000", "--wall-seconds", "3600",
                    "--reserve-tokens-per-call", "250000", "--max-tokens-per-call", "1500000",
                    "--survey-recovery", "4"]


# -- gui_records 8: the reserve check counts calls still in flight -----------------------------


def test_overlapping_calls_cannot_all_start_on_the_same_headroom():
    """500k of headroom and a 400k reserve admitted three attempts at once."""
    b = RunBudget(RunLimits(max_reported_tokens=500_000, reserve_tokens_per_call=400_000))
    first, _ = b.reserve()
    assert b.snapshot()['in_flight_reserved_tokens'] == 400_000
    with pytest.raises(RunBudgetExceeded, match='reported_token_reserve'):
        b.reserve()
    snap = b.snapshot()
    assert snap['reserved_attempts'] == 1 and snap['in_flight'] == 1
    assert 'in-flight' in snap['reserve_boundary']
    assert first == 1


def test_parallel_attempts_are_admitted_only_while_their_reserves_fit():
    b = RunBudget(RunLimits(max_reported_tokens=1_000_000, reserve_tokens_per_call=300_000, max_calls=50))

    def attempt(_):
        try:
            return b.reserve()[0]
        except RunBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        tickets = [t for t in pool.map(attempt, range(30)) if t is not None]
    assert len(tickets) == 3, "three 300k reserves fit in 1M; a fourth does not"
    assert b.snapshot()['in_flight_reserved_tokens'] == 900_000


def test_a_finished_call_releases_what_it_held_and_sequential_calls_are_unchanged():
    b = RunBudget(RunLimits(max_reported_tokens=1_000_000, reserve_tokens_per_call=400_000))
    one, _ = b.reserve()
    two, _ = b.reserve()                                   # 1M - 400k held = 600k covers 400k
    assert b.snapshot()['in_flight_reserved_tokens'] == 800_000
    b.finish(one, {'input_tokens': 100_000, 'output_tokens': 0})
    b.finish(two, {'input_tokens': 100_000, 'output_tokens': 0})
    assert b.snapshot()['in_flight_reserved_tokens'] == 0
    three, _ = b.reserve()                                 # 800k left, nothing held
    b.finish(three, {'input_tokens': 300_000, 'output_tokens': 0})
    four, _ = b.reserve()                                  # 500k left covers 400k, as before the fix
    assert four == 4


def test_each_ticket_holds_the_reserve_it_was_admitted_with_shaped_included():
    b = RunBudget(RunLimits(max_reported_tokens=1_000_000, reserve_tokens_per_call=500_000))
    shaped, _ = b.reserve(expected_tokens=64_000)          # a one-turn summary call holds 64k, not 500k
    assert b.snapshot()['in_flight_reserved_tokens'] == 64_000
    b.reserve()                                            # 936k left covers an ordinary 500k
    assert b.snapshot()['in_flight_reserved_tokens'] == 564_000
    b.reserve(expected_tokens=64_000)                      # 436k left covers another summary call
    assert b.snapshot()['in_flight_reserved_tokens'] == 628_000
    with pytest.raises(RunBudgetExceeded, match='reported_token_reserve'):
        b.reserve()                                        # 372k left does not cover 500k
    b2 = RunBudget(RunLimits(max_reported_tokens=1_000_000, reserve_tokens_per_call=500_000))
    b2.reserve(expected_tokens=64_000)
    b2.finish(shaped, {'input_tokens': 20_000, 'output_tokens': 1_000})
    assert b2.snapshot()['in_flight_reserved_tokens'] == 0, "the hold ends with the call, whatever it reported"


def test_no_reserve_means_no_hold_and_the_old_post_return_rule():
    b = RunBudget(RunLimits(max_reported_tokens=10))
    for _ in range(5):
        b.reserve()
    assert b.snapshot()['in_flight_reserved_tokens'] == 0 and b.snapshot()['in_flight'] == 5


def test_two_provider_calls_at_once_each_need_their_own_reserve(monkeypatch):
    """The same rule through the path a real call takes: while one provider
    call is running, a second on the same budget is refused before its
    transport starts, and the first still comes back and is counted."""
    monkeypatch.setattr(ClaudeCLIProvider, 'available', lambda _: True)
    running, release, transport_calls = threading.Event(), threading.Event(), []

    def call(self, *args):
        transport_calls.append(self)
        if len(transport_calls) == 1:
            running.set()
            assert release.wait(30)
        self.last_usage = dict(input_tokens=1_000, output_tokens=10)
        return 'answer'

    monkeypatch.setattr(ClaudeCLIProvider, '_call', call)
    budget = RunBudget(RunLimits(max_reported_tokens=500_000, reserve_tokens_per_call=400_000))
    first, second = ClaudeCLIProvider('fixture'), ClaudeCLIProvider('fixture')
    first.run_budget = second.run_budget = budget
    with ThreadPoolExecutor(max_workers=1) as pool:
        in_flight = pool.submit(first.generate, 'one')
        assert running.wait(30)
        try:
            assert budget.snapshot()['in_flight_reserved_tokens'] == 400_000
            with pytest.raises(RunBudgetExceeded, match='reported_token_reserve'):
                second.generate('two')
            assert len(transport_calls) == 1, "the refused call never reached its transport"
        finally:
            release.set()
        # The stop is latched, so the first call's return reports it; its
        # answer is kept and its tokens are counted, as for any budget stop.
        with pytest.raises(RunBudgetExceeded, match='reported_token_reserve') as stopped:
            in_flight.result(timeout=30)
    assert stopped.value.provider_response == 'answer'
    snap = budget.snapshot()
    assert snap['reserved_attempts'] == 1 and snap['in_flight'] == 0 and snap['reported_tokens'] == 1_010
    assert snap['in_flight_reserved_tokens'] == 0 and snap['stop_reason'] == 'reported_token_reserve'


def test_the_limits_text_says_overlapping_calls_each_need_their_own_reserve():
    limits = RunLimits(max_reported_tokens=5_000_000, reserve_tokens_per_call=250_000)
    text = describe_limits(limits)
    assert "An ordinary call starts only while at least 250,000 tokens of the total remain" in text
    assert "While a call is running, the amount it needed to start stays set aside" in text
    assert "set aside" not in describe_limits(RunLimits(max_reported_tokens=5_000_000))


# -- gui_records 12: a response that names an unpriced model -----------------------------------


def test_a_resolved_model_name_with_no_rate_is_priced_as_the_reserved_key(tmp_path):
    limits = RunLimits(max_cost_usd=1.0, max_reported_tokens=10_000_000,
                       api_cost_rates=(APICostRate('jev:jev-latest', 0.042, 0.0, 'test'),))
    b = RunBudget(limits, path=tmp_path / 'budget.json')
    for _ in range(2):
        ticket, _ = b.reserve(transport='api', price_key='jev:jev-latest')
        b.finish(ticket, dict(input_tokens=1_000_000, output_tokens=5), price_key='jev:jev-1.13.0')
    saved = json.loads((tmp_path / 'budget.json').read_text())
    assert saved['stop_reason'] == '' and saved['unknown_api_cost_attempts'] == 0
    assert saved['api_cost_usd'] == pytest.approx(0.084)
    assert saved['api_price_fallbacks'] == [
        dict(reported_model='jev:jev-1.13.0', priced_as='jev:jev-latest', attempts=2)]
    b.reserve(transport='api', price_key='jev:jev-latest')  # the run goes on


class _DatedJev:
    """The SDK duck type, answering under a dated release name instead of
    the alias it was asked for."""

    def system_one(self, *, state, questions, model=None, **options):
        (name, _), = questions.items()
        answer = SimpleNamespace(choice="simple", confidence=0.9, probabilities={"simple": 0.9})
        return SimpleNamespace(model="jev-1.13.0", choices={name: answer},
                               usage=SimpleNamespace(input_tokens=1_000, output_tokens=5))


def test_a_jev_decision_answered_under_a_dated_name_does_not_stop_the_run(tmp_path, monkeypatch):
    """The finding as it happened: a rate for jev:jev-latest, a response
    naming jev-1.13.0, and the run stopped after its first decision."""
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    limits = RunLimits(max_cost_usd=1.0, max_reported_tokens=10_000_000,
                       api_cost_rates=(APICostRate('jev:jev-latest', 0.042, 0.0, 'test'),))
    budget = RunBudget(limits, path=tmp_path / 'budget.json')
    decider = D.JevDecider(api_key="ts_x", client=_DatedJev(), budget=budget)
    decision = D.Decision(id="task.difficulty", question="How hard?", answers=("rote", "simple"),
                          context="Rename one variable.")
    for _ in range(3):
        assert decider.decide(decision).source == "jev:jev-1.13.0"
    saved = json.loads((tmp_path / 'budget.json').read_text())
    assert saved['stop_reason'] == '' and saved['unknown_api_cost_attempts'] == 0
    assert saved['api_cost_usd'] == pytest.approx(3 * 1_000 * 0.042 / 1_000_000)
    assert saved['api_price_fallbacks'] == [
        dict(reported_model='jev:jev-1.13.0', priced_as='jev:jev-latest', attempts=3)]


def test_a_priced_resolved_name_uses_its_own_rate_and_records_no_fallback():
    limits = RunLimits(max_cost_usd=10.0, max_reported_tokens=10_000_000, api_cost_rates=(
        APICostRate('jev:jev-latest', 1.0, 0.0, 'test'), APICostRate('jev:jev-1.13.0', 3.0, 0.0, 'test')))
    b = RunBudget(limits)
    ticket, _ = b.reserve(transport='api', price_key='jev:jev-latest')
    b.finish(ticket, dict(input_tokens=1_000_000, output_tokens=0), price_key='jev:jev-1.13.0')
    assert b.snapshot()['api_cost_usd'] == pytest.approx(3.0)
    assert b.snapshot()['api_price_fallbacks'] == []


def test_a_call_with_no_priced_key_to_reserve_under_still_stops():
    b = RunBudget(RunLimits(max_cost_usd=1.0, api_cost_rates=(APICostRate('jev:jev-latest', 1, 0, 'test'),)))
    with pytest.raises(RunBudgetExceeded, match='api_price_unavailable'):
        b.reserve(transport='api', price_key='')
    assert b.snapshot()['reserved_attempts'] == 0


def test_unknown_usage_is_still_unknown_cost_even_when_a_fallback_rate_exists():
    b = RunBudget(RunLimits(max_cost_usd=1.0, api_cost_rates=(APICostRate('jev:jev-latest', 1, 0, 'test'),)))
    ticket, _ = b.reserve(transport='api', price_key='jev:jev-latest')
    with pytest.raises(RunBudgetExceeded, match='unknown_usage'):
        b.finish(ticket, None, price_key='jev:jev-1.13.0')
    assert b.snapshot()['unknown_api_cost_attempts'] == 1


def test_the_meter_prices_an_unlisted_reported_name_at_the_key_the_call_was_made_under(tmp_path):
    assert 'jev:jev-9.9.9' not in PRICES
    path = tmp_path / 'usage.jsonl'
    meter = UsageMeter(path)
    fell_back = meter.record(model='jev:jev-9.9.9', input_tokens=1_000_000, output_tokens=0,
                             fallback_model='jev:jev-latest')
    assert fell_back.priced_as == 'jev:jev-latest'
    assert fell_back.cost_usd == pytest.approx(PRICES['jev:jev-latest'].input_per_mtok)
    shrug = meter.record(model='jev:jev-9.9.9', input_tokens=1_000_000, output_tokens=0)
    assert shrug.priced_as == '' and shrug.cost_usd == pytest.approx(DEFAULT_PRICE.input_per_mtok)
    known = meter.record(model='claude:opus', input_tokens=1_000_000, output_tokens=0,
                         fallback_model='jev:jev-latest')
    assert known.priced_as == '' and known.cost_usd == pytest.approx(PRICES['claude:opus'].input_per_mtok)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]['priced_as'] == 'jev:jev-latest' and lines[0]['model'] == 'jev:jev-9.9.9'
    assert 'priced_as' not in lines[1] and 'priced_as' not in lines[2], "an ordinary line is unchanged"
    assert '(priced as jev:jev-latest)' in meter.render_report()


# -- gui_records 21: the lead cap's floor against a small per-call threshold --------------------


def test_the_text_says_when_the_minimum_lead_cap_plans_more_than_the_threshold():
    limits = RunLimits(max_reported_tokens=1_000_000, max_tokens_per_call=150_000)
    turns, source = effective_lead_turns(None, limits)
    assert turns == MIN_LEAD_TURNS == lead_turns_for(150_000), "the arithmetic is unchanged"
    text = describe_limits(limits, None, 5, turns, source)
    planned = MIN_LEAD_TURNS * TURN_CONTEXT_TOKENS
    assert f"{MIN_LEAD_TURNS} rounds plan about {planned:,} tokens for one call" in text
    assert "more than the 150,000-token per-call threshold" in text
    assert "does not keep a call under that threshold" in text
    assert f"never below {MIN_LEAD_TURNS} rounds" in text and f"threshold of at least {planned:,} tokens" in text


@pytest.mark.parametrize("ceiling", [480_000, 600_000, 1_500_000])
def test_a_threshold_the_cap_fits_in_gets_no_such_sentence(ceiling):
    limits = RunLimits(max_reported_tokens=5_000_000, max_tokens_per_call=ceiling)
    text = describe_limits(limits, None, 5, *effective_lead_turns(None, limits))
    assert "Lead turn cap" in text and "plan about" not in text


def test_an_operator_cap_larger_than_the_threshold_is_said_too_without_the_derived_note():
    limits = RunLimits(max_reported_tokens=5_000_000, max_tokens_per_call=600_000)
    text = describe_limits(limits, None, 5, *effective_lead_turns(30, limits))
    assert "30 rounds plan about 1,800,000 tokens" in text and "A derived cap" not in text
    assert "plan about" not in describe_limits(None, None, 5, *effective_lead_turns(30, None))


def test_the_lead_cap_arithmetic_is_what_it_was():
    assert lead_turns_for(1_500_000) == 20 and lead_turns_for(100_000) == MIN_LEAD_TURNS == 8
    assert lead_turns_for(None) is None and lead_turns_for(0) is None


# -- design 3.11: one validation for the form and the command line -----------------------------


FORM_CASES = [
    (True, 90, 5_000_000, 3600, 2, 250_000, 1_500_000, 4),
    (True, 24.0, 500000.0, 900.0, 2.0, 0.0, 0.0, 0.0),
    (True, "90", "5_000_000", "3600", "2", "250000", "0", ""),
    (False, 90, 5_000_000, 3600, 2, 250_000, 1_500_000, 0),
    (False, 90, 5_000_000, 3600, 2, 250_000, 1_500_000, 3),
    (True, 0, 5_000_000, 3600, 2, 0, 0, 0),
    (True, 90, None, 3600, 2, 0, 0, 0),
    (True, 90, 5_000_000, 1.5, 2, 0, 0, 0),
    (True, 90, 5_000_000, 3600, 0, 0, 0, 0),
    (True, 90, 5_000_000, 3600, 2, -1, 0, 0),
    (True, 90, 5_000_000, 3600, 2, 0, "abc", 0),
    (False, 90, 5_000_000, 3600, 2, 0, 0, -2),
    (True, 90, 1_000, 3600, 2, 250_000, 0, 0),
    (True, True, 5_000_000, 3600, 2, 0, 0, 0),
    (True, 90, 5_000_000, float("inf"), 2, 0, 0, 0),
]


def _outcome(call):
    try:
        return call()
    except ValueError as exc:
        return f"ValueError: {exc}"


@pytest.mark.parametrize("values", FORM_CASES)
def test_the_project_form_calls_the_shared_validation(values, monkeypatch):
    """The GUI had its own copy of this rule and a test held the two equal,
    word for word. It now calls the shared one: every form value goes to it
    under its field name, and what it returns or refuses is the form's answer."""
    enabled, *fields = values
    shared = _outcome(lambda: limits_from_fields(enabled, dict(zip(LIMIT_FIELDS, fields, strict=True))))
    calls = []

    def recorded(*args, **kwargs):
        calls.append((args, kwargs))
        return limits_from_fields(*args, **kwargs)

    monkeypatch.setattr(gui, "limits_from_fields", recorded)
    assert shared == _outcome(lambda: gui.limits_from_form(*values))
    assert calls == [((enabled, dict(zip(LIMIT_FIELDS, fields, strict=True))), {})]


def test_the_shared_fields_are_the_forms_and_each_has_a_label():
    assert gui.LIMIT_FIELDS is LIMIT_FIELDS and gui.LIMIT_LABELS is LIMIT_LABELS
    assert not hasattr(gui, "_whole") and not hasattr(gui, "_LIMIT_LABELS"), "the form keeps no copy"
    assert set(LIMIT_FIELDS) <= set(LIMIT_LABELS) and set(LIMIT_FIELDS) == set(cli._LIMIT_FLAGS)


def test_whole_number_names_what_it_refuses():
    assert whole_number("5_000_000", "--max-reported-tokens", 1) == 5_000_000
    assert whole_number(3.0, "x", 1) == 3
    for bad, said in [("", "enter a whole number"), (None, "enter a whole number"), (True, "enter a whole number"),
                      ("abc", "is not a number"), ("1.5", "at least 1"), ("0", "at least 1"), ("nan", "at least 1")]:
        with pytest.raises(ValueError, match=said) as refused:
            whole_number(bad, "--max-calls", 1)
        assert "--max-calls" in str(refused.value)


# -- the command line: run limits ---------------------------------------------------------------


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """Stand in for the shared runner and record what the command line sent it."""
    seen = {}

    def run(goal, project, settings, **kwargs):
        seen.update(kwargs, goal=goal, project=project, settings=settings)
        return SimpleNamespace(completed=True, report="checked", run_dir=tmp_path)

    monkeypatch.setattr(project_run, "run_project", run)
    return seen


def _refused(argv, capsys, monkeypatch, *, before_settings=True):
    """The argparse refusal for ``argv``: exit 2, nothing configured or started."""
    if before_settings:
        monkeypatch.setattr(cli, "_build_settings",
                            lambda *a: pytest.fail("refused lines must not reach settings or providers"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    assert stopped.value.code == 2
    return capsys.readouterr().err


def test_the_command_line_builds_the_limits_the_form_builds(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), *DIAGNOSTIC_FLAGS]) == 0
    limits, survey = gui.limits_from_form(*gui.preset_values(DIAGNOSTIC))
    assert runner["run_limits"] == limits == RunLimits(
        max_calls=90, max_reported_tokens=5_000_000, wall_seconds=3600, max_concurrent_workers=2,
        reserve_tokens_per_call=250_000, max_tokens_per_call=1_500_000)
    assert runner["survey"] == survey == SurveyConfig(recovery_tasks=4)


def test_no_limit_flag_is_exactly_the_call_made_before(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path)]) == 0
    assert "run_limits" not in runner and "session_options" not in runner
    assert runner["survey"] is None and runner["max_tasks"] == 20 and runner["mode"] == "adversarial"
    assert runner["decider"] == "rule" and runner["decider_labels"] == "unstated"
    assert runner["settings"].lead_max_turns is None


def test_recovery_alone_is_a_survey_with_no_limits(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--survey-recovery", "3"]) == 0
    assert "run_limits" not in runner and runner["survey"] == SurveyConfig(recovery_tasks=3)
    runner.clear()
    assert cli.main([GOAL, "--project", str(tmp_path), "--survey-recovery", "0"]) == 0
    assert runner["survey"] is None


def test_optional_limit_flags_default_to_off_and_two_workers(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--max-calls", "30", "--max-reported-tokens",
                     "2_000_000", "--wall-seconds", "1800", "--max-concurrent-workers", "3"]) == 0
    assert runner["run_limits"] == RunLimits(max_calls=30, max_reported_tokens=2_000_000, wall_seconds=1800,
                                             max_concurrent_workers=3)
    assert runner["run_limits"].reserve_tokens_per_call == 0 and runner["run_limits"].max_tokens_per_call is None


@pytest.mark.parametrize("flags, named", [
    (["--max-calls", "90"], "missing: --max-reported-tokens, --wall-seconds"),
    (["--max-tokens-per-call", "1500000"], "missing: --max-calls, --max-reported-tokens, --wall-seconds"),
    (["--max-calls", "abc", "--max-reported-tokens", "5", "--wall-seconds", "5"], "--max-calls: 'abc' is not"),
    (["--max-calls", "0", "--max-reported-tokens", "5", "--wall-seconds", "5"], "--max-calls must be a whole"),
    (["--max-calls", "5", "--max-reported-tokens", "5", "--wall-seconds", "1.5"], "--wall-seconds must be a whole"),
    (["--max-calls", "5", "--max-reported-tokens", "1000", "--wall-seconds", "5",
      "--reserve-tokens-per-call", "250000"], "reserve_tokens_per_call cannot exceed max_reported_tokens"),
    (["--survey-recovery", "-1"], "--survey-recovery must be a whole number of at least 0"),
])
def test_a_bad_or_partial_limit_is_refused_by_flag_before_anything_is_opened(flags, named, tmp_path, capsys,
                                                                            monkeypatch, runner):
    monkeypatch.setattr(Project, "open", lambda *a, **k: pytest.fail("nothing is opened or cloned"))
    assert named in _refused([GOAL, "--project", str(tmp_path), *flags], capsys, monkeypatch)
    assert not runner


@pytest.mark.parametrize("engine", [[], ["--session"]])
def test_run_limits_need_a_project(engine, capsys, monkeypatch):
    error = _refused([GOAL, *engine, *DIAGNOSTIC_FLAGS], capsys, monkeypatch)
    assert "--max-calls" in error and "--survey-recovery" in error and "require --project" in error


def test_the_lead_turn_cap_flag_is_the_operators_setting(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--lead-max-turns", "12", *DIAGNOSTIC_FLAGS]) == 0
    assert runner["settings"].lead_max_turns == 12
    assert effective_lead_turns(runner["settings"].lead_max_turns, runner["run_limits"]) == (12, "operator setting")


@pytest.mark.parametrize("value", ["0", "-3", "1.5", "many"])
def test_a_lead_turn_cap_below_one_round_is_refused(value, tmp_path, capsys, monkeypatch):
    error = _refused([GOAL, "--project", str(tmp_path), "--lead-max-turns", value], capsys, monkeypatch)
    assert "--lead-max-turns" in error and "whole number of at least 1" in error


def test_a_limited_command_line_run_saves_the_allowance_before_the_first_call(tmp_path, monkeypatch, capsys):
    """End to end through the real runner: the flags reach RunBudget, the
    derived lead cap reaches the session, and the record says both."""
    (tmp_path / "app.py").write_text("x\n")
    seen = {}

    def run(self, **kwargs):
        seen["lead"] = self.config.lead_max_turns
        seen["workers"] = self.config.worker_budget.max_concurrent
        return None
    monkeypatch.setattr(Session, "run", run)
    cli.main([GOAL, "--project", str(tmp_path), *DIAGNOSTIC_FLAGS])
    out = capsys.readouterr().out
    assert ">> Run limits: Task limit: 20. At most 90 model calls and 5,000,000 reported" in out
    run_dir = next((tmp_path / ".quadratus" / "runs").iterdir())
    saved = json.loads((run_dir / "run-limits.json").read_text())
    assert saved["run_limits"]["max_calls"] == 90 and saved["survey_recovery_tasks"] == 4
    assert saved["lead_max_turns"] == seen["lead"] == 20 and seen["workers"] == 2
    assert "An ordinary call starts only while at least 250,000 tokens" in saved["described"]


# -- the command line: session settings ---------------------------------------------------------


KNOB_FLAGS = ["--recent-entries", "4", "--max-fix-cycles", "1", "--max-consults", "1", "--no-spread-leads",
              "--no-design-cross-check"]
KNOBS = dict(recent_entries=4, max_fix_cycles=1, max_consults=1, spread_leads=False, design_cross_check=False)


def test_session_settings_reach_the_project_runner(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--allow-writes", "--max-parallel-tasks", "1",
                     *KNOB_FLAGS]) == 0
    assert runner["session_options"] == dict(KNOBS, max_parallel_tasks=1)


def test_the_runner_sets_them_on_the_session_and_none_is_the_old_config(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x\n")
    seen = []
    monkeypatch.setattr(Session, "run", lambda self, **kw: seen.append(self.config))
    notes = []
    project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), progress=notes.append,
                            session_options=dict(KNOBS, max_parallel_tasks=2))
    project_run.run_project(GOAL, tmp_path, Settings(backend="cli"))
    chosen, plain = seen
    for name, value in dict(KNOBS, max_parallel_tasks=2).items():
        assert getattr(chosen, name) == value
        assert getattr(plain, name) == getattr(SessionConfig(), name)
    assert any(n.startswith("Session settings: design_cross_check=False") for n in notes)


@pytest.mark.parametrize("options, said", [
    (dict(max_gate_fixes=5), "'max_gate_fixes' is not a session setting"),
    (dict(recent_entries=0), "recent_entries must be a whole number of at least 1"),
    (dict(max_consults=True), "max_consults must be a whole number"),
    (dict(spread_leads=0), "spread_leads must be True or False"),
])
def test_the_runner_refuses_a_setting_outside_the_table_before_the_lock(options, said, tmp_path, monkeypatch):
    monkeypatch.setattr(project_run, "_project_lock", lambda *a: pytest.fail("must refuse before locking"))
    with pytest.raises(ValueError, match=said):
        project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), session_options=options)


def test_every_session_setting_is_a_session_config_field_with_a_stated_default():
    fields = {f.name: f for f in dataclasses.fields(SessionConfig)}
    defaults = SessionConfig()
    assert session_options(None) == {} and session_options({}) == {}
    for name, floor in SESSION_KNOBS.items():
        assert name in fields
        if floor is None:
            assert isinstance(getattr(defaults, name), bool)
    text = cli.build_parser().format_help()
    flat = " ".join(text.split())
    assert f"first revision included (default {defaults.max_fix_cycles})" in flat
    assert f"per task (default {defaults.max_consults})" in flat
    assert f"run at once (default {defaults.max_parallel_tasks};" in flat
    assert defaults.recent_entries is None and "ledger entries (default: all)" in flat
    assert defaults.spread_leads is True and defaults.design_cross_check is True
    assert f"run at once (default {RunLimits.max_concurrent_workers})" in flat


@pytest.mark.parametrize("flag", ["--recent-entries", "--max-fix-cycles", "--max-consults",
                                  "--max-parallel-tasks"])
def test_a_count_below_its_floor_is_refused_by_flag(flag, tmp_path, capsys, monkeypatch):
    error = _refused([GOAL, "--project", str(tmp_path), "--allow-writes", flag, "0"], capsys, monkeypatch)
    assert flag in error and "whole number of at least 1" in error


def test_parallel_tasks_are_refused_where_nothing_runs_in_parallel(tmp_path, capsys, monkeypatch):
    error = _refused([GOAL, "--project", str(tmp_path), "--max-parallel-tasks", "2"], capsys, monkeypatch)
    assert "--max-parallel-tasks requires --allow-writes" in error
    error = _refused([GOAL, "--session", "--max-parallel-tasks", "2"], capsys, monkeypatch)
    assert "--max-parallel-tasks require --project" in error


# -- the projectless session: settings, lock, no dead grant --------------------------------------


class _Fleet:
    """The vendor CLIs' stand-in for a projectless session."""

    def __init__(self, settings=None, **kw):
        self.settings, self.kw, self.usage_meter, self.closed = settings, kw, kw.get("usage_meter"), False

    def status_lines(self):
        return ["claude [cli]: Claude (opus)"]

    def close(self):
        self.closed = True


@pytest.fixture
def projectless(monkeypatch):
    made = {}

    def fleet(*a, **kw):
        made["fleet"] = _Fleet(*a, **kw)
        return made["fleet"]

    def session(goal, store, *, fleet, config, **kw):
        made["config"] = config
        return SimpleNamespace(run=lambda **kwargs: made.setdefault("run", kwargs) and [])

    monkeypatch.setattr("quadratus.runtime.Fleet", fleet)
    monkeypatch.setattr("quadratus.runtime.new_session", session)
    return made


def test_a_projectless_session_takes_the_settings_and_the_lead_cap(tmp_path, projectless):
    assert cli.main(["--session", GOAL, "--state-dir", str(tmp_path / "s"), "--lead-max-turns", "9",
                     *KNOB_FLAGS]) == 0
    config = projectless["config"]
    for name, value in KNOBS.items():
        assert getattr(config, name) == value
    assert config.lead_max_turns == 9 == projectless["fleet"].settings.lead_max_turns
    assert config.max_parallel_tasks == SessionConfig().max_parallel_tasks
    assert config.integration_gate is None and "allow_writes" not in projectless["fleet"].kw
    assert projectless["run"] == dict(max_tasks=20)


@pytest.mark.parametrize("option", [["--check", "pytest -q"], ["--allow-writes"]])
def test_the_projectless_gate_and_write_grant_were_never_reachable(option, capsys, monkeypatch):
    error = _refused(["--session", GOAL, *option], capsys, monkeypatch)
    assert option[0] in error and "require --project" in error


def test_a_second_projectless_session_in_one_state_directory_is_refused(tmp_path, projectless, capsys):
    state = tmp_path / "state"
    state.mkdir()
    with (state / "run.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(["--session", GOAL, "--state-dir", str(state)]) == 1
        assert "Another Quadratus run is using" in capsys.readouterr().out
        assert not projectless, "no fleet, no session, nothing written beside the lock"
        assert sorted(p.name for p in state.iterdir()) == ["run.lock"]
    assert cli.main(["--session", GOAL, "--state-dir", str(state)]) == 0, "released when the holder ends"
    assert cli.main(["--session", GOAL, "--state-dir", str(state)]) == 0, "and by the session itself"


def test_the_lock_is_held_while_the_session_runs(tmp_path, monkeypatch, projectless):
    state = tmp_path / "state"
    inner = []

    def session(goal, store, *, fleet, config, **kw):
        def run(**kwargs):
            with pytest.raises(ProviderError, match="Another Quadratus run"):
                with cli._state_lock(state):
                    pass
            inner.append("checked")
            return []
        return SimpleNamespace(run=run)

    monkeypatch.setattr("quadratus.runtime.new_session", session)
    assert cli.main(["--session", GOAL, "--state-dir", str(state)]) == 0
    assert inner == ["checked"]


def test_the_default_state_directory_shares_the_project_runners_lock(tmp_path, monkeypatch, projectless, capsys):
    """Same file as ``--project .`` uses, so the two kinds of run keep each
    other out of one folder's .quadratus."""
    monkeypatch.chdir(tmp_path)
    with project_run._project_lock(Project(tmp_path)):
        assert cli.main(["--session", GOAL]) == 1
    assert "Another Quadratus run" in capsys.readouterr().out and not projectless
    assert cli.main(["--session", GOAL]) == 0
    with pytest.raises(ProviderError, match="Another Quadratus"):
        with cli._state_lock(tmp_path / ".quadratus"), project_run._project_lock(Project(tmp_path)):
            pass


# -- found in review: a projectless session that stopped is not reported complete -------------------


class _NeverDone:
    """A fleet whose orchestrator names another task every time, so the
    session meets its task cap without the goal being confirmed."""

    def __init__(self, settings=None, *, usage_meter=None, **kw):
        self.usage_meter, self.named = usage_meter, 0

    def invoke(self, model, prompt, *, system=None):
        if "Name the single next task" in prompt:
            self.named += 1
            return f"build part {self.named} of the parser"
        if "The task is finished" in prompt:
            return "SUMMARY: did a part\nREASONING: it was next\nDEAD ENDS: - none"
        return f"[{model}] output"

    def available(self, key):
        return True

    def status_lines(self):
        return ["claude [cli]: Claude (opus)"]

    def close(self):
        pass


def test_a_projectless_session_stopped_at_its_task_cap_exits_1_and_says_incomplete(tmp_path, monkeypatch, capsys):
    """Through the real engine. This printed "2 task(s) complete" and exited 0
    while the session itself had recorded GoalUnconfirmedAtCap."""
    monkeypatch.setattr("quadratus.runtime.Fleet", _NeverDone)
    assert cli.main(["--session", GOAL, "--state-dir", str(tmp_path / "s"), "--max-tasks", "2"]) == 1
    out = capsys.readouterr().out
    assert "2 task(s) closed; the run is incomplete" in out and "task(s) complete" not in out
    assert "The run stopped incomplete: GoalUnconfirmedAtCap" in out


_CLOSED = SimpleNamespace(task_id="t1", author="claude:opus", summary="did it", reasoning="", refs=[])


@pytest.mark.parametrize("ended, summaries, code, said, unsaid", [
    (dict(completed=True, stop_reason=""), [_CLOSED], 0, "1 task(s) complete", "incomplete"),
    (dict(completed=False, stop_reason="FindingsUnresolved: R2 not met"), [_CLOSED], 1,
     "The run stopped incomplete: FindingsUnresolved: R2 not met", "task(s) complete"),
    (dict(completed=False, stop_reason=""), [_CLOSED], 1,
     "The run stopped incomplete: the session did not report the goal met", "task(s) complete"),
    (dict(completed=False, stop_reason="TaskFailureBreaker: two tasks failed"), [], 1,
     "stopped incomplete before any task closed: TaskFailureBreaker", "Nothing ran."),
    (dict(completed=False, stop_reason="PlanDeclined: the operator's plan gate declined"), [], 0,
     "Nothing ran.", "incomplete"),
    (dict(completed=True, stop_reason=""), [], 0, "Nothing ran.", "incomplete"),
])
def test_the_projectless_exit_status_is_the_sessions_own_verdict(ended, summaries, code, said, unsaid, tmp_path,
                                                                 monkeypatch, capsys, projectless):
    monkeypatch.setattr("quadratus.runtime.new_session", lambda goal, store, *, fleet, config, **kw:
                        SimpleNamespace(run=lambda **kwargs: list(summaries), **ended))
    assert cli.main(["--session", GOAL, "--state-dir", str(tmp_path / "s")]) == code
    out = capsys.readouterr().out
    assert said in out and unsaid not in out


# -- gui_records 19: no flag is accepted and ignored ---------------------------------------------


ENGINE_ONLY = [["--mode", "adversarial"], ["--max-tasks", "5"], ["--plan-gate"], ["--decider", "rule"],
               ["--state-dir", "x"], ["--decider-labels", "unstated"], ["--security-verdict-json"],
               ["--neutral-preferences"], ["--lead-max-turns", "9"], ["--recent-entries", "3"],
               ["--max-fix-cycles", "1"], ["--max-consults", "1"], ["--no-spread-leads"],
               ["--no-design-cross-check"]]


@pytest.mark.parametrize("option", ENGINE_ONLY)
def test_an_engine_flag_on_a_bare_pipeline_run_is_refused_by_name(option, capsys, monkeypatch):
    monkeypatch.setattr(cli, "Orchestrator", lambda *a, **k: pytest.fail("the pipeline must not start"))
    error = _refused([GOAL, *option], capsys, monkeypatch)
    assert option[0] in error and "require --session or --project" in error


@pytest.mark.parametrize("option", ENGINE_ONLY)
def test_the_same_flags_are_accepted_by_a_session(option, tmp_path, projectless):
    extra = [] if option[0] == "--state-dir" else option
    assert cli.main(["--session", GOAL, "--state-dir", str(tmp_path / "s"), *extra]) == 0
    assert projectless["run"] == dict(max_tasks=5 if option[0] == "--max-tasks" else 20)


@pytest.mark.parametrize("option, also", [
    (["--rounds", "2"], ""), (["--show-stages"], ""),
    (["--claude-model", "m"], "QUADRATUS_ALIAS_<VENDOR>_<SEAT>"),
    (["--openai-model", "m"], "QUADRATUS_ALIAS"), (["--grok-model", "m"], "QUADRATUS_ALIAS"),
])
@pytest.mark.parametrize("engine", [["--session"], ["--project", "p"]])
def test_a_pipeline_flag_on_an_engine_run_is_refused_by_name(option, also, engine, capsys, monkeypatch):
    error = _refused([GOAL, *engine, *option], capsys, monkeypatch)
    assert option[0] in error and "cannot be combined with --session or --project" in error and also in error


def test_the_pipeline_still_takes_its_own_flags(tmp_path, monkeypatch, capsys):
    seen = {}

    class Pipeline:
        def __init__(self, settings):
            seen["rounds"], seen["model"] = settings.rounds, settings.claude_model
            self.available = [SimpleNamespace(status="fake [api]: ok")]

        def run(self, prompt, progress=None):
            stage = SimpleNamespace(label="plan", role="planner", content="the plan")
            return SimpleNamespace(stages=[stage, stage], final="the answer", final_provider="fake", task=prompt)

    monkeypatch.setattr(cli, "Orchestrator", Pipeline)
    out = tmp_path / "solution.md"
    assert cli.main([GOAL, "--rounds", "2", "--show-stages", "--claude-model", "m", "-o", str(out)]) == 0
    assert seen == dict(rounds=2, model="m") and "the answer" in out.read_text()
    assert "the plan" in capsys.readouterr().out


def test_several_misplaced_flags_are_all_named_at_once(capsys, monkeypatch):
    error = _refused([GOAL, "--mode", "collaborative", "--max-tasks", "3", "--check", "pytest", "--history", "2"],
                     capsys, monkeypatch)
    assert "--check require --project" in error
    assert "--max-tasks, --mode require --session or --project" in error and "does not use them" in error
    assert "--history can only be used with --monitor" in error


def test_monitor_takes_only_what_it_reads(tmp_path, capsys, monkeypatch):
    for extra in (["--mode", "solo"], ["--max-tasks", "3"], ["--allow-writes"], ["--status"], ["-o", "out.md"],
                  ["--session"], ["--rounds", "2"]):
        error = _refused(["--monitor", "--project", str(tmp_path), *extra], capsys, monkeypatch)
        assert "--monitor reads a run and starts nothing" in error
        assert (extra[0] if extra[0] != "-o" else "--output") in error
    assert "takes no prompt" in _refused(["--monitor", "--project", str(tmp_path), "go"], capsys, monkeypatch)
    for lonely in (["--history", "3"], ["--serve"], ["--series", str(tmp_path)]):
        assert "can only be used with --monitor" in _refused([GOAL, *lonely], capsys, monkeypatch)
    assert cli.main(["--monitor", "--project", str(tmp_path), "--state-dir", str(tmp_path / "s"),
                     "--history", "2", "-v"]) == 0


def test_a_full_run_line_with_policy_preview_added_still_previews_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "Orchestrator", lambda *a, **k: pytest.fail("a preview constructs no provider"))
    monkeypatch.setattr("quadratus.runtime.Fleet", lambda *a, **k: pytest.fail("a preview starts no fleet"))
    assert cli.main([GOAL, "--project", str(tmp_path), "--policy-preview", "--path", "a.py", "--check",
                     "pytest -q", "--max-tasks", "3", "--mode", "collaborative", *DIAGNOSTIC_FLAGS]) == 0
    assert json.loads(capsys.readouterr().out)["primary_family"] == "pure-logic"
    error = _refused(["--policy-preview", "--path", "a.py"], capsys, monkeypatch)
    assert "--policy-preview, --path require --project" in error


def test_flags_one_run_would_not_honour_are_refused_together(tmp_path, capsys, monkeypatch):
    project = ["--project", str(tmp_path)]
    error = _refused([GOAL, *project, "--decider-labels", "all"], capsys, monkeypatch)
    assert "--decider-labels all requires --decider jev" in error
    error = _refused([GOAL, "--session", "--decider-labels", "all", "--decider", "rule"], capsys, monkeypatch)
    assert "--decider-labels all requires --decider jev" in error
    error = _refused([GOAL, *project, "--plan-gate", "--tasks", "tasks.json"], capsys, monkeypatch)
    assert "--plan-gate cannot be combined with --tasks" in error


def test_decider_labels_all_is_accepted_with_jev(tmp_path, runner):
    assert cli.main([GOAL, "--project", str(tmp_path), "--decider", "jev", "--decider-labels", "all"]) == 0
    assert runner["decider"] == "jev" and runner["decider_labels"] == "all"


def test_every_option_declares_what_it_needs_and_parses_to_not_given():
    """A flag added without ``flag(..., needs=)`` would be accepted anywhere
    and never checked: declare it, with None, False or [] as its parse
    default so a typed flag can be told from an absent one."""
    parser = cli.build_parser()
    declared = {id(a) for a in parser.flags}
    for action in parser._actions:
        if not action.option_strings or "--help" in action.option_strings:
            continue
        assert id(action) in declared, f"{action.option_strings} was added without flag()"
        assert action.default is None or action.default is False or action.default == [], action.option_strings
        assert action.needs in (None, "project", "engine", "pipeline", "monitor"), action.option_strings
    nothing = parser.parse_args([])
    assert not [a.option_strings for a in parser.flags if cli._given(nothing, a)]
    assert set(cli._DEFAULTS) <= {a.dest for a in parser.flags}
    assert sorted(cli._LIMIT_FLAGS) == sorted(LIMIT_FIELDS)


# -- modes: no restricted senior seat, and no run its audit rule cannot let finish ----------------


def test_no_mode_seats_a_restricted_row_in_any_role():
    for mode, roles in R.MODE_ROSTERS.items():
        for role, keys in roles.items():
            for key in keys:
                assert not R.resolve(key).restricted, f"{mode}.{role} -> {key}"
    assert R.MODE_ROSTERS["collaborative"]["peers"] == ["claude:opus", "openai:sol"]
    assert R.MODE_ROSTERS["collaborative"]["planners"] == ["claude:opus", "openai:sol"]
    assert R.resolve("openai:gpt-5.6-terra").restricted, "Terra stays what it is: an escalation target"


def test_a_restricted_seat_is_rejected_at_import():
    R.assert_senior_seats_unrestricted()
    old = {"collaborative": {"peers": ["claude:opus", "openai:terra"], "reviewers": [], "planners": []}}
    with pytest.raises(ValueError, match="mode 'collaborative' peers seats openai:terra"):
        R.assert_senior_seats_unrestricted(old)
    with pytest.raises(ValueError, match="planners seats claude:haiku"):
        R.assert_senior_seats_unrestricted({"m": {"peers": [], "reviewers": [], "planners": ["claude:haiku"]}})
    with pytest.raises(ValueError, match="names an unknown model: claude:nope"):
        R.assert_senior_seats_unrestricted({"m": {"peers": ["claude:nope"]}})
    with pytest.raises(ValueError, match="the orchestrator chain seats claude:sonnet"):
        R.assert_senior_seats_unrestricted({}, ["claude:sonnet"])


def _import_registry_source(name, source, monkeypatch):
    """Run ``source`` as a module the way an import would."""
    module = types.ModuleType(name)
    module.__package__ = "quadratus"
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile(source, R.__file__, "exec"), module.__dict__)
    return module


def test_the_registry_cannot_be_imported_with_a_restricted_peer(monkeypatch):
    """The check runs at import: with the old collaborative roster put back,
    the module itself refuses to load, whatever the tests call."""
    source = Path(R.__file__).read_text(encoding="utf-8")
    now = '"peers": ["claude:opus", "openai:sol"],\n        "reviewers": ["claude:opus"],'
    assert source.count(now) == 1
    before = now.replace("openai:sol", "openai:terra")
    with pytest.raises(ValueError, match="mode 'collaborative' peers seats openai:terra"):
        _import_registry_source("quadratus._registry_with_terra", source.replace(now, before), monkeypatch)
    loaded = _import_registry_source("quadratus._registry_as_written", source, monkeypatch)
    assert loaded.MODE_ROSTERS == R.MODE_ROSTERS


def test_collaborative_leads_and_peers_can_do_what_a_senior_seat_does():
    from quadratus.task_kinds import KNOWN_NEEDS, seat_capabilities
    for key in R.MODE_ROSTERS["collaborative"]["peers"]:
        assert seat_capabilities(key) == KNOWN_NEEDS
    assert seat_capabilities("openai:gpt-5.6-terra") != KNOWN_NEEDS


def test_the_audit_gap_is_solo_only_and_says_what_to_use():
    assert R.mode_audit_gap("adversarial") == "" and R.mode_audit_gap("collaborative") == ""
    assert R.mode_audit_gap("no-such-mode") == ""
    gap = R.mode_audit_gap("solo")
    assert "claude:opus" in gap and "claude:fable" in gap and "another vendor" in gap
    assert "'adversarial' or 'collaborative'" in gap and "QUADRATUS_REQUIREMENTS_LEDGER=0" in gap


@pytest.mark.parametrize("mode", sorted(R.MODE_ROSTERS))
@pytest.mark.parametrize("down", [(), ("claude:fable",)])
def test_the_registry_rule_is_the_sessions_auditor_rule(mode, down, tmp_path):
    """``Session._auditor`` is the rule; the registry helper the refusal is
    built on must name the same seats for every mode and either seat."""
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(mode=mode), available=lambda key: key not in down)
    seat = session.seat().key
    expected = R.cross_vendor_peers(mode, seat)
    assert session._auditor() == (expected[0] if expected else None)
    if mode == "solo" and not down:
        assert seat == "claude:fable" and session._auditor() is None, "the run could never pass its audit"


@pytest.mark.requirements_ledger
def test_solo_is_refused_before_any_call_while_the_ledger_is_on(tmp_path, capsys, monkeypatch, runner, projectless):
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "1")
    for engine in (["--project", str(tmp_path)], ["--session", "--state-dir", str(tmp_path / "s")]):
        error = _refused([GOAL, *engine, "--mode", "solo"], capsys, monkeypatch)
        assert "--mode solo is refused" in error and "'adversarial' or 'collaborative'" in error
    assert not runner and not projectless


@pytest.mark.requirements_ledger
def test_the_shared_runner_refuses_solo_too_so_the_gui_cannot_start_it(tmp_path, monkeypatch):
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "1")
    monkeypatch.setattr(project_run, "_project_lock", lambda *a: pytest.fail("must refuse before locking"))
    with pytest.raises(ValueError, match="mode 'solo' seats only claude:opus"):
        project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), mode="solo")


@pytest.mark.requirements_ledger
def test_the_other_modes_start_with_the_ledger_on(tmp_path, monkeypatch, runner):
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "1")
    for mode in ("adversarial", "collaborative"):
        assert cli.main([GOAL, "--project", str(tmp_path), "--mode", mode]) == 0
        assert runner["mode"] == mode


def test_solo_still_runs_with_the_ledger_off(tmp_path, monkeypatch):
    assert not SessionConfig().requirements_ledger, "the suite's default: the ledger is off"
    (tmp_path / "app.py").write_text("x\n")
    seen = []
    monkeypatch.setattr(Session, "run", lambda self, **kw: seen.append(self.config.mode))
    cli.main([GOAL, "--project", str(tmp_path), "--mode", "solo"])
    project_run.run_project(GOAL, tmp_path, Settings(backend="cli"), mode="solo")
    assert seen == ["solo", "solo"]
