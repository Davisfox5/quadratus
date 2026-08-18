"""Tests for usage metering, the operator ASK channel, the revision round,
and browser evidence."""

from __future__ import annotations

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.cli_providers import _extract_claude_usage
from multi_llm.session import (
    Complexity,
    OperatorInputNeeded,
    RunStalled,
    Session,
    SessionConfig,
    TaskSpec,
)
from multi_llm.usage import CHARS_PER_TOKEN, PRICES, UsageMeter

from .test_session import Recorder

FABLE = "claude:fable"
OPUS = "claude:opus"


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def _session(store, rec, **kw):
    return Session("Build a JSON parser", store, rec, **kw)


# -- the meter ----------------------------------------------------------------


def test_estimated_records_are_marked_estimated():
    meter = UsageMeter()
    got = meter.record(model=OPUS, prompt="x" * 400, reply="y" * 100)
    assert not got.measured
    assert got.input_tokens == int(400 / CHARS_PER_TOKEN)
    assert got.output_tokens == int(100 / CHARS_PER_TOKEN)


def test_real_counts_beat_the_estimate():
    meter = UsageMeter()
    got = meter.record(model=OPUS, prompt="irrelevant", input_tokens=1000,
                       output_tokens=200)
    assert got.measured
    assert got.input_tokens == 1000


def test_cost_uses_the_price_sheet():
    meter = UsageMeter()
    price = PRICES[OPUS]
    got = meter.record(model=OPUS, input_tokens=1_000_000, output_tokens=1_000_000)
    assert got.cost_usd == pytest.approx(price.input_per_mtok + price.output_per_mtok)


def test_an_unknown_model_still_counts_tokens():
    meter = UsageMeter()
    got = meter.record(model="newvendor:shiny", input_tokens=10, output_tokens=10)
    assert got.cost_usd > 0  # default price, not a crash and not free


def test_records_persist_as_jsonl(tmp_path):
    path = tmp_path / "usage.jsonl"
    UsageMeter(path).record(model=OPUS, prompt="p", reply="r")
    assert len(path.read_text().splitlines()) == 1


def test_every_session_invocation_is_metered(store):
    meter = UsageMeter()
    rec = Recorder(next_tasks=["task one", "DONE"])
    s = _session(store, rec, config=SessionConfig(usage_meter=meter))
    s.run(max_tasks=2)
    assert len(meter.records) == len(rec.calls)
    assert meter.total_cost() > 0


def test_a_broken_meter_never_fails_the_run(store):
    class Exploding(UsageMeter):
        def record(self, **kwargs):
            raise RuntimeError("meter on fire")

    rec = Recorder(next_tasks=["task one", "DONE"])
    s = _session(store, rec, config=SessionConfig(usage_meter=Exploding()))
    assert len(s.run(max_tasks=2)) == 1  # run unaffected


def test_the_report_separates_sizing_from_billing():
    meter = UsageMeter()
    meter.record(model=OPUS, prompt="p" * 400, reply="r" * 40)
    report = meter.render_report()
    assert "token-estimated" in report
    assert "$" in report


def test_claude_envelope_usage_parses_and_folds_cache_tokens():
    stdout = (
        '{"result": "hi", "usage": {"input_tokens": 10, '
        '"cache_read_input_tokens": 90, "cache_creation_input_tokens": 5, '
        '"output_tokens": 7}}'
    )
    got = _extract_claude_usage(stdout)
    assert got == {"input_tokens": 105, "output_tokens": 7}
    assert _extract_claude_usage("not json") is None
    assert _extract_claude_usage('{"result": "hi"}') is None


# -- the ASK channel ----------------------------------------------------------


def test_an_answered_question_becomes_a_standing_ruling(store):
    rec = Recorder(next_tasks=["ASK: Postgres or SQLite?", "use SQLite then", "DONE"])
    s = _session(store, rec,
                 config=SessionConfig(ask_operator=lambda q: "SQLite, it's local-only"))
    s.run(max_tasks=5)
    rendered = s.memory.render()
    assert "Postgres or SQLite?" in rendered
    assert "local-only" in rendered
    assert "do not re-ask" in rendered


def test_the_ruling_reaches_the_next_orchestrator_prompt(store):
    rec = Recorder(next_tasks=["ASK: Postgres or SQLite?", "make the schema", "DONE"])
    s = _session(store, rec, config=SessionConfig(ask_operator=lambda q: "SQLite"))
    s.run(max_tasks=5)
    later_prompts = rec.prompts_to(FABLE)[1:]
    assert later_prompts
    assert all("SQLite" in p for p in later_prompts)


def test_an_ask_without_a_channel_raises_rather_than_guessing(store):
    rec = Recorder(next_tasks=["ASK: which auth provider?"])
    s = _session(store, rec)
    with pytest.raises(OperatorInputNeeded, match="which auth provider"):
        s.next_task()


def test_an_interrogating_orchestrator_is_stopped(store):
    rec = Recorder(next_tasks=["ASK: q1?", "ASK: q2?", "ASK: q3?", "ASK: q4?"])
    s = _session(store, rec, config=SessionConfig(ask_operator=lambda q: "answer"))
    with pytest.raises(RunStalled, match="interrogating"):
        s.next_task()


# -- the revision round -------------------------------------------------------


def test_critiqued_work_gets_a_revision_round(store):
    rec = Recorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    revision_prompts = [c["prompt"] for c in rec.calls if "Revise your work" in c["prompt"]]
    assert len(revision_prompts) == 1
    assert "Address every finding" in revision_prompts[0]


def test_the_revision_is_kept_as_an_artifact(store):
    rec = Recorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    kinds = {r.kind for r in s.memory.ledger.refs()}
    assert "revision" in kinds


def test_uncritiqued_work_pays_for_no_revision(store):
    rec = Recorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    assert not any("Revise your work" in c["prompt"] for c in rec.calls)


def test_the_revision_lands_before_the_closeout(store):
    """The close-out must summarise the revised work, not the first draft."""
    rec = Recorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    order = [c["prompt"] for c in rec.calls]
    revise_at = next(i for i, p in enumerate(order) if "Revise your work" in p)
    close_at = next(i for i, p in enumerate(order) if "The task is finished" in p)
    assert revise_at < close_at
