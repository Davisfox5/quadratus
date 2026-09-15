"""A seat is an invocation, not just a model; routing has to see the difference.

On 2026-09-14 a docs/rote task that said "run `node tests/ui/mutation_check.js`
twice" was seated on grok:worker, whose allowlist holds no command execution.
The call cancelled and the run ended. These tests pin the three helpers that
let routing notice: what a task needs, what a seat can do, and how a rung
that cannot do it is climbed past.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quadratus.registry import resolve
from quadratus.task_kinds import (
    KNOWN_NEEDS,
    Need,
    NoCapableSeat,
    TaskKind,
    escalate_from,
    needs_from_text,
    normalise_needs,
    route,
    seat_capabilities,
    seat_satisfies,
)

OPUS = "claude:opus"
SOL = "openai:gpt-5.6-sol"
GROK = "grok:default"
GROK_WORKER = "grok:worker"
FABLE = "claude:fable"

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/handoffs/reliability-repair/gametape-completion/worker-task.json"


# -- what a seat can do -------------------------------------------------------


def test_the_restricted_worker_can_only_return_a_patch():
    assert resolve(GROK_WORKER).restricted
    assert seat_capabilities(GROK_WORKER) == {Need.PATCH}


def test_agentic_seats_can_do_everything():
    for key in (GROK, SOL, OPUS, FABLE):
        assert not resolve(key).restricted
        assert seat_capabilities(key) == KNOWN_NEEDS


def test_a_key_the_roster_does_not_describe_is_not_made_stricter():
    """Availability, not this table, decides whether an unknown key is callable."""
    assert seat_capabilities("nobody:nothing") == KNOWN_NEEDS


def test_patch_is_satisfied_by_a_restricted_seat_but_execution_is_not():
    assert seat_satisfies(GROK_WORKER, {Need.PATCH})
    assert not seat_satisfies(GROK_WORKER, {Need.EXECUTE})
    assert not seat_satisfies(GROK_WORKER, {Need.DIRECT_WRITE})
    assert seat_satisfies(GROK_WORKER, ())


# -- explicit labels ----------------------------------------------------------


def test_explicit_needs_are_normalised():
    assert normalise_needs(["Execute", " patch", "direct_write"]) == KNOWN_NEEDS
    assert normalise_needs([]) == frozenset()


def test_an_unknown_explicit_need_raises_rather_than_vanishing():
    with pytest.raises(ValueError, match="exec"):
        normalise_needs(["exec"])


# -- inference from the task text ---------------------------------------------


def test_the_recorded_failing_task_is_seen_to_need_execution_and_a_patch():
    """The task from the 2026-09-14 run, verbatim from the evidence bundle."""
    task = json.loads(EVIDENCE.read_text())
    needs = needs_from_text(task["description"], task["scope"]["acceptance"])
    assert Need.EXECUTE in needs
    assert Need.PATCH in needs
    assert Need.DIRECT_WRITE not in needs


@pytest.mark.parametrize("text", [
    "Run `pytest -q` and record the summary.",
    "Re-run node tests/ui/mutation_check.js twice with color disabled.",
    "Execute npm test before and after the change.",
    "Confirm the exit code is zero.",
    "Report which tests passed after the change.",
])
def test_running_something_needs_an_agentic_seat(text):
    assert Need.EXECUTE in needs_from_text(text)


@pytest.mark.parametrize("text", [
    "Edit only the four result lines in docs/BULK_EDIT.md.",
    "Fix the off-by-one in quadratus/scope.py.",
    "Add a paragraph to README.md describing the lock.",
])
def test_changing_source_needs_a_patch_at_least(text):
    needs = needs_from_text(text)
    assert Need.PATCH in needs
    assert Need.EXECUTE not in needs


@pytest.mark.parametrize("text", [
    "Regenerate package-lock.json after bumping the dependency.",
    "Replace the placeholder logo.png with the new asset.",
    "Update the binary file fixtures/sample.mp4.",
])
def test_files_that_cannot_travel_as_text_need_direct_writes(text):
    assert Need.DIRECT_WRITE in needs_from_text(text)


@pytest.mark.parametrize("text", [
    "Explain what the mutation harness measures.",
    "Summarise the difference between the two records in docs/BULK_EDIT.md.",
    "Which node version was used? Answer in one line.",
    "Explain icon.svg.",
    "Describe what fixtures/sample.mp4 is used for.",
    "Is package-lock.json checked in?",
    "",
])
def test_prose_that_merely_mentions_things_infers_nothing(text):
    assert needs_from_text(text) == frozenset()


@pytest.mark.parametrize("text", [
    "Change the fill color in icon.svg.",
    "Fix the viewBox in static/logo.svg.",
])
def test_svg_is_text_and_travels_as_a_patch(text):
    """Codex's finding on b73f827: SVG source is patchable, and a bare media
    mention must not promote a read-only errand to an agentic seat."""
    assert needs_from_text(text) == {Need.PATCH}


def test_acceptance_criteria_count_as_much_as_the_description():
    assert needs_from_text("Tidy the doc.", ["Run `node --test tests/ui/*.test.js` and capture the output"]) >= {Need.EXECUTE}


# -- routing honours needs ----------------------------------------------------


def test_rote_work_without_needs_still_lands_on_the_worker():
    assert route(TaskKind.DOCS, default=OPUS, difficulty="rote") == GROK_WORKER


def test_rote_work_that_must_run_commands_climbs_past_the_worker():
    """The 2026-09-14 route, with the need visible: the same rung, one seat up."""
    got = route(TaskKind.DOCS, default=OPUS, difficulty="rote", needs={Need.EXECUTE})
    assert got == GROK


def test_rote_work_that_only_needs_a_patch_keeps_the_cheap_rung():
    assert route(TaskKind.DOCS, default=OPUS, difficulty="rote", needs={Need.PATCH}) == GROK_WORKER


def test_needs_are_honoured_at_every_step_including_fallbacks():
    """With the ladder's agentic seats down, the rotation default and the
    candidates are checked for capability too -- a restricted candidate is not
    handed an execution task just because it is last in line."""
    down = {GROK, SOL, OPUS}
    got = route(TaskKind.DOCS, default=GROK_WORKER, difficulty="rote",
                candidates=[GROK_WORKER, FABLE], needs={Need.EXECUTE},
                available=lambda k: k not in down)
    assert got == FABLE


def test_stated_needs_nobody_can_meet_stop_loudly():
    with pytest.raises(NoCapableSeat) as caught:
        route(TaskKind.DOCS, default=GROK_WORKER, difficulty="rote",
              candidates=[GROK_WORKER], needs={Need.EXECUTE},
              available=lambda k: k == GROK_WORKER)
    assert caught.value.needs == {Need.EXECUTE}
    assert GROK_WORKER in caught.value.tried


def test_without_needs_the_never_raises_contract_stands():
    assert route(TaskKind.DOCS, default=GROK_WORKER, difficulty="rote",
                 available=lambda _k: False) == GROK_WORKER


def test_a_pinned_kind_is_checked_for_capability_too():
    """Testing pins Sol; if Sol were somehow a restricted seat the pin would
    yield rather than seat an execution task on it."""
    got = route(TaskKind.TEST, default=OPUS, needs={Need.EXECUTE},
                capabilities=lambda k: {Need.PATCH} if k == SOL else KNOWN_NEEDS)
    assert got == OPUS


# -- escalation after a capability failure ------------------------------------


def test_escalation_from_the_worker_is_the_agentic_seat_above_it():
    assert escalate_from(GROK_WORKER, needs={Need.EXECUTE}) == GROK


def test_escalation_skips_an_unavailable_seat():
    assert escalate_from(GROK_WORKER, needs={Need.EXECUTE}, available=lambda k: k != GROK) == SOL


def test_escalation_is_strictly_upward_and_ends_at_the_top():
    assert escalate_from(OPUS, needs={Need.EXECUTE}) is None
    assert escalate_from(SOL, needs=()) == OPUS


def test_escalation_from_a_seat_off_the_ladder_is_none():
    assert escalate_from(FABLE, needs={Need.EXECUTE}) is None
    assert escalate_from("nobody:nothing") is None
