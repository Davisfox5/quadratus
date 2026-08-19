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
SOL = "openai:gpt-5.6-sol"


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


# -- the fix->verify cycle ----------------------------------------------------


class BlockingRecorder(Recorder):
    """Collaborators raise a BLOCKING finding; rechecks resolve on a script."""

    def __init__(self, recheck_verdicts):
        super().__init__()
        self._verdicts = list(recheck_verdicts)

    def __call__(self, model, prompt, system=None):
        if "contributing an independent read" in prompt:
            self.calls.append({"model": model, "prompt": prompt})
            return "BLOCKING: the escaping drops surrogate pairs"
        if "Check only your BLOCKING findings" in prompt:
            self.calls.append({"model": model, "prompt": prompt})
            return self._verdicts.pop(0)
        return super().__call__(model, prompt, system)


def test_a_blocking_finding_triggers_a_recheck(store):
    rec = BlockingRecorder(["RESOLVED"])
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    rechecks = [c for c in rec.calls if "Check only your BLOCKING" in c["prompt"]]
    assert len(rechecks) == 1


def test_an_unresolved_verdict_buys_exactly_one_more_revision(store):
    rec = BlockingRecorder(["UNRESOLVED: still drops them", "RESOLVED"])
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    fixes = [c for c in rec.calls if "blocking findings remain unresolved"
             in c["prompt"].lower()]
    assert len(fixes) == 1


def test_the_cycle_is_capped_and_leftovers_reach_the_closeout(store):
    rec = BlockingRecorder(["UNRESOLVED: no", "UNRESOLVED: still no"])
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    rechecks = [c for c in rec.calls if "Check only your BLOCKING" in c["prompt"]]
    assert len(rechecks) == 2  # capped by max_fix_cycles=2
    closeout = next(c["prompt"] for c in rec.calls if "The task is finished" in c["prompt"])
    assert "still unresolved at close" in closeout


def test_non_blocking_critiques_get_no_recheck(store):
    rec = Recorder()  # plain reviews carry no BLOCKING marker
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    assert not any("Check only your BLOCKING" in c["prompt"] for c in rec.calls)


def test_reviewers_are_told_how_to_mark_blocking_findings(store):
    rec = Recorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    review = next(c["prompt"] for c in rec.calls
                  if "contributing an independent read" in c["prompt"])
    assert "BLOCKING" in review


def test_rechecks_never_widen_scope(store):
    rec = BlockingRecorder(["RESOLVED"])
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.STANDARD))
    recheck = next(c["prompt"] for c in rec.calls
                   if "Check only your BLOCKING" in c["prompt"])
    assert "Do not raise new findings" in recheck


# -- the fetch channel --------------------------------------------------------


class FetchingRecorder(Recorder):
    """The orchestrator or lead asks for an artifact once, then answers."""

    def __init__(self, fetch_id, **kw):
        super().__init__(**kw)
        self._fetch_id = fetch_id
        self._asked = set()

    def __call__(self, model, prompt, system=None):
        marker = (model, "lead" if "You are leading" in prompt else "orch")
        if ("Fetched artifacts" not in prompt and marker not in self._asked
                and ("You are leading" in prompt or "Name the single next task" in prompt)):
            self._asked.add(marker)
            self.calls.append({"model": model, "prompt": prompt})
            return f"FETCH: {self._fetch_id}"
        return super().__call__(model, prompt, system)


def test_the_orchestrator_can_open_an_artifact_before_deciding(store):
    ref = store.put("the summary omitted this exact escaping rule",
                    kind="draft", author=OPUS)
    rec = FetchingRecorder(ref.id, next_tasks=["do the thing", "DONE"])
    s = _session(store, rec)
    spec = s.next_task()
    assert spec.description == "do the thing"
    served = [c["prompt"] for c in rec.calls if "Fetched artifacts" in c["prompt"]]
    assert served and "exact escaping rule" in served[0]


def test_a_lead_can_open_an_artifact_before_drafting(store):
    ref = store.put("full detail the preview skipped", kind="draft", author=OPUS)
    rec = FetchingRecorder(ref.id)
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    served = [c["prompt"] for c in rec.calls
              if "Fetched artifacts" in c["prompt"] and "You are leading" in c["prompt"]]
    assert served and "full detail the preview skipped" in served[0]


def test_an_unknown_artifact_id_is_corrected_not_fatal(store):
    rec = FetchingRecorder("no-such-id", next_tasks=["do the thing", "DONE"])
    s = _session(store, rec)
    assert s.next_task().description == "do the thing"
    served = [c["prompt"] for c in rec.calls if "Fetched artifacts" in c["prompt"]]
    assert served and "check the id" in served[0]


def test_fetching_is_budgeted(store):
    class Greedy(Recorder):
        def __call__(self, model, prompt, system=None):
            self.calls.append({"model": model, "prompt": prompt})
            if "Name the single next task" in prompt:
                return "FETCH: aaaaaaaaaaaa"  # forever
            return super().__call__(model, prompt, system)

    rec = Greedy()
    s = _session(store, rec)
    s.next_task()  # must terminate
    orch_calls = [c for c in rec.calls if "Name the single next task" in c["prompt"]]
    assert len(orch_calls) <= 1 + s.config.max_fetches


def test_a_draft_mentioning_fetch_in_prose_is_not_a_request(store):
    rec = Recorder()  # replies "[model] output" -- never a bare FETCH line
    s = _session(store, rec)
    got = s.run_task(TaskSpec("t1", "explain FETCH: semantics", complexity=Complexity.SIMPLE))
    assert got.summary  # ran straight through


# -- the consult channel ------------------------------------------------------


class ConsultingRecorder(Recorder):
    """The lead asks Sol one question, then drafts with the answer."""

    def __init__(self, consult_line="CONSULT Sol: is bcrypt still the right choice?"):
        super().__init__()
        self._line = consult_line
        self._asked = False

    def __call__(self, model, prompt, system=None):
        if "You are leading" in prompt and not self._asked:
            self._asked = True
            self.calls.append({"model": model, "prompt": prompt})
            return self._line
        return super().__call__(model, prompt, system)


def test_a_lead_can_consult_a_named_peer(store):
    rec = ConsultingRecorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "build the login flow", complexity=Complexity.SIMPLE))
    consults = [c for c in rec.calls if "area of strength" in c["prompt"]]
    assert len(consults) == 1
    assert consults[0]["model"] == SOL
    assert "bcrypt" in consults[0]["prompt"]


def test_the_consultant_answers_blind(store):
    """The consultant sees the task and the question -- never the draft, the
    ledger, or the session. Expertise, not agreement."""
    rec = ConsultingRecorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "build the login flow", complexity=Complexity.SIMPLE))
    consult = next(c["prompt"] for c in rec.calls if "area of strength" in c["prompt"])
    assert "Build a JSON parser" not in consult   # no goal/ledger
    assert "You are leading" not in consult       # no lead prompt


def test_the_answer_returns_to_the_lead_and_is_filed(store):
    rec = ConsultingRecorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "build the login flow", complexity=Complexity.SIMPLE))
    redraft = [c["prompt"] for c in rec.calls
               if "Consult answers" in c["prompt"] and "You are leading" in c["prompt"]]
    assert redraft
    kinds = {r.kind for r in s.memory.ledger.refs()}
    assert f"consult:{SOL}" in kinds


def test_the_lead_cannot_consult_itself_or_strangers(store):
    rec = ConsultingRecorder(consult_line="CONSULT Grok 4.6: hmm?")
    s = _session(store, rec)
    # lead for a SIMPLE task IS grok -- consulting itself must not resolve
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    assert not any("area of strength" in c["prompt"] for c in rec.calls)
    redraft = [c["prompt"] for c in rec.calls if "not a member you can consult" in c["prompt"]]
    assert redraft


def test_the_consult_budget_is_enforced(store):
    class Chatty(Recorder):
        def __call__(self, model, prompt, system=None):
            if "You are leading" in prompt:
                self.calls.append({"model": model, "prompt": prompt})
                return "CONSULT Sol: another question?"
            return super().__call__(model, prompt, system)

    rec = Chatty()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    consults = [c for c in rec.calls if "area of strength" in c["prompt"]]
    assert len(consults) == s.config.max_consults
    assert any("consult budget spent" in c["prompt"] for c in rec.calls)


def test_leads_are_told_the_channels_exist(store):
    rec = Recorder()
    s = _session(store, rec)
    s.run_task(TaskSpec("t1", "work", complexity=Complexity.SIMPLE))
    lead = next(c["prompt"] for c in rec.calls if "You are leading" in c["prompt"])
    assert "FETCH:" in lead and "CONSULT" in lead
