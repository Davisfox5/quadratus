"""The fixed-answer decision inventory and the Decisions API placeholder
(2026-09-30). Nothing here calls a network: the transport refuses until the
contract is published, and the refusal names what is missing."""
import pytest

from quadratus import decisions as D
from quadratus.task_kinds import DIFFICULTY_LADDER


def test_every_inventory_row_has_a_rule_and_finite_answers():
    for key, row in D.DECISIONS.items():
        assert row["rule"] and len(row["answers"]) >= 2, key
        assert isinstance(row["delegable"], bool), key


def test_difficulty_answers_are_the_ladder_rungs():
    assert set(D.DECISIONS["task.difficulty"]["answers"]) == set(DIFFICULTY_LADDER)


def test_deterministic_admission_and_parsers_are_never_delegable():
    assert set(D.delegable()) == {"task.difficulty", "task.kind", "worker.escalate"}
    for key in ("task.tier", "reply.control", "security.route"):
        assert D.DECISIONS[key]["delegable"] is False


def test_the_rule_decider_only_accepts_listed_answers():
    decision = D.Decision(id="task.difficulty", question="?", answers=("rote", "simple"))
    assert D.RuleDecider().decide(decision, answer="simple").source == "rule"
    with pytest.raises(ValueError):
        D.RuleDecider().decide(decision, answer="hard")


def test_the_openai_decider_refuses_with_the_missing_contract_named():
    decider = D.OpenAIDecisionsDecider(api_key="sk-test")
    reason = decider.available()
    assert "not published" in reason and "endpoint path" in reason and "pricing" in reason
    with pytest.raises(D.DecisionsUnavailable, match="not published"):
        decider.decide(D.Decision(id="task.difficulty", question="?", answers=("rote", "simple")))


def test_an_endpoint_without_a_key_is_a_billing_refusal_not_a_guess():
    decider = D.OpenAIDecisionsDecider(api_key=None)
    decider.endpoint = "/v1/decisions"
    assert "billed endpoint" in decider.available()


# ---- Jev through the TypeSafe SDK contract (typesafe-sdk 0.7.2) -------------

class _Answer:
    def __init__(self, choice, confidence=0.9, probabilities=None):
        self.choice, self.confidence = choice, confidence
        self.probabilities = probabilities or {choice: confidence}


class _Usage:
    input_tokens, output_tokens = 120, 12


class _Response:
    def __init__(self, name, answer, model="jev-1.13"):
        self.model, self.usage, self.choices = model, _Usage(), {name: answer}


class _Client:
    """The SDK duck type: system_one(state=, questions=, model=)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def system_one(self, *, state, questions, model=None):
        self.calls.append(dict(state=state, questions=questions, model=model))
        (name, question), = questions.items()
        return _Response(name, self.answer if not callable(self.answer) else self.answer(question))


class _Meter:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)


DIFFICULTY = D.Decision(id="task.difficulty", question="How hard?", answers=("rote", "simple", "standard", "complex"),
                        context="Rename one variable in app.py.")


@pytest.fixture(autouse=True)
def _no_ambient_jev_configuration(monkeypatch):
    """The tests choose their own key, host and model; the shell's do not leak in."""
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_jev_asks_one_choice_question_and_returns_a_verdict_with_source_and_confidence():
    client, meter = _Client(_Answer("rote", 0.82, {"rote": 0.82, "simple": 0.15})), _Meter()
    decider = D.JevDecider(client=client, meter=meter)
    verdict = decider.decide(DIFFICULTY)
    assert verdict.answer == "rote" and verdict.source == "jev:jev-1.13" and verdict.confidence == 0.82
    assert '"rote": 0.82' in verdict.note
    assert verdict.usage["input_tokens"] == 120 and verdict.usage["host"] == "typesafe"
    assert isinstance(verdict.usage["seconds"], float) and client.calls and decider.calls[0]["seconds"] >= 0
    question = client.calls[0]["questions"]["task_difficulty"]
    assert question["type"] == "choice" and set(question["criteria"]) == set(DIFFICULTY.answers)
    assert client.calls[0]["state"] == DIFFICULTY.context and client.calls[0]["model"] == "jev-latest"


def test_jev_calls_are_metered_as_billed_api_calls():
    meter = _Meter()
    D.JevDecider(client=_Client(_Answer("simple")), meter=meter).decide(DIFFICULTY)
    assert meter.records == [dict(model="jev:jev-1.13", prompt=DIFFICULTY.context, reply="",
                                  input_tokens=120, output_tokens=12)]


def test_an_answer_outside_the_set_is_refused_not_used():
    with pytest.raises(D.DecisionsUnavailable, match="not one of"):
        D.JevDecider(client=_Client(_Answer("hard"))).decide(DIFFICULTY)


def test_a_transport_error_is_a_refusal_with_the_cause():
    class Broken:
        def system_one(self, **kw):
            raise RuntimeError("429 rate limited")
    with pytest.raises(D.DecisionsUnavailable, match="Jev call failed: RuntimeError: 429"):
        D.JevDecider(client=Broken()).decide(DIFFICULTY)


def test_jev_refuses_without_the_sdk_or_a_key(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "typesafe_sdk", None)
    assert "typesafe-sdk is not installed" in D.JevDecider(api_key="k").available()
    monkeypatch.delitem(__import__("sys").modules, "typesafe_sdk")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    pytest.importorskip("typesafe_sdk")
    assert "no TYPESAFE_API_KEY" in D.JevDecider(api_key="").available()


def test_decider_names():
    assert D.decider_from_name(None) is None and D.decider_from_name("rule") is None
    assert isinstance(D.decider_from_name("jev"), D.JevDecider)
    with pytest.raises(ValueError, match="unknown decider"):
        D.decider_from_name("luna")


def test_jev_prices_are_on_the_sheet():
    from quadratus.usage import PRICES
    assert PRICES["jev:jev-latest"].input_per_mtok == 0.042 and PRICES["jev:jev-latest"].output_per_mtok == 0.0


def test_a_vercel_gateway_key_defaults_to_the_gateway_host_and_model(monkeypatch):
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
    gateway = D.JevDecider(api_key="vck_example")
    assert gateway.base_url == D.JEV_GATEWAY_BASE_URL and gateway.model == "typesafe-ai/jev"
    assert gateway.host == "vercel-gateway"
    direct = D.JevDecider(api_key="ts_example")
    assert direct.base_url is None and direct.model == "jev-latest" and direct.host == "typesafe"
    explicit = D.JevDecider(api_key="vck_example", base_url="https://example.test", model="jev-1.13")
    assert explicit.base_url == "https://example.test" and explicit.model == "jev-1.13"


def test_the_call_record_names_the_host():
    decider = D.JevDecider(api_key="vck_example", client=_Client(_Answer("simple")))
    decider.decide(DIFFICULTY)
    assert decider.calls[0]["host"] == "vercel-gateway"


# ---- Codex review of #44 (3e4528a): budget, provenance, CLI ---------------

def test_jev_calls_reserve_and_finish_the_run_budget():
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits
    budget = RunBudget(RunLimits(max_calls=1, max_reported_tokens=1_000_000, wall_seconds=600,
                                 max_concurrent_workers=1))
    decider = D.JevDecider(api_key="ts_x", client=_Client(_Answer("simple")), budget=budget)
    decider.decide(DIFFICULTY)
    snap = budget.snapshot()
    assert snap["reserved_attempts"] == 1 and snap["input_tokens"] == 120 and snap["output_tokens"] == 12
    with pytest.raises(RunBudgetExceeded, match="call_limit"):
        decider.decide(DIFFICULTY)


def test_a_failed_jev_call_finishes_its_ticket_and_the_budget_rule_decides():
    """A billed call that returns no usage latches the budget (unknown_usage),
    exactly as a provider's does; the budget stop outranks the refusal and
    no reservation is left open."""
    from quadratus.run_budget import RunBudget, RunBudgetExceeded, RunLimits

    class Broken:
        def system_one(self, **kw):
            raise RuntimeError("boom")
    budget = RunBudget(RunLimits(max_calls=5, max_reported_tokens=1_000_000, wall_seconds=600,
                                 max_concurrent_workers=1))
    with pytest.raises(RunBudgetExceeded, match="unknown_usage"):
        D.JevDecider(api_key="ts_x", client=Broken(), budget=budget).decide(DIFFICULTY)
    snap = budget.snapshot()
    assert snap["reserved_attempts"] == 1 and snap["unknown_usage_attempts"] == 1 and not budget._active


def test_metadata_records_which_field_was_stated():
    from quadratus.taskmeta import parse_metadata
    kw = dict(known_kinds={"backend", "docs"}, known_difficulties={"rote", "simple", "complex"},
              default_kind="general", default_difficulty="simple")
    both = parse_metadata("KIND: backend complex\nDo it.", **kw)
    assert both.kind_stated and both.difficulty_stated
    degraded = parse_metadata("KIND: typo complex\nDo it.", **kw)
    assert not degraded.kind_stated and degraded.difficulty_stated and degraded.difficulty == "complex"
    partial = parse_metadata("KIND: backend\nDo it.", **kw)
    assert partial.kind_stated and not partial.difficulty_stated and partial.confidence == "labelled"
    none = parse_metadata("Do it.", **kw)
    assert not none.kind_stated and not none.difficulty_stated


def test_the_projectless_cli_session_resolves_the_decider():
    from types import SimpleNamespace

    from quadratus.cli import _decider_for
    assert isinstance(_decider_for(SimpleNamespace(decider="jev")), D.JevDecider)
    assert _decider_for(SimpleNamespace(decider="rule")) is None
    assert _decider_for(SimpleNamespace()) is None


# --- label definitions (decision_labels, 2026-10-01) ---------------------------

from quadratus import decision_labels as L  # noqa: E402
from quadratus.task_kinds import ROUTING  # noqa: E402


def test_every_routed_kind_and_every_rung_has_a_definition_with_an_example():
    assert set(L.KIND_DEFINITIONS) == set(ROUTING)
    assert set(L.DIFFICULTY_DEFINITIONS) == set(DIFFICULTY_LADDER) == set(D.DECISIONS["task.difficulty"]["answers"])
    for table in (L.KIND_DEFINITIONS, L.DIFFICULTY_DEFINITIONS):
        for label, text in table.items():
            assert "Example:" in text and len(text) > 60, label


def test_the_jev_question_carries_definitions_guidance_and_the_labels_version():
    client = _Client(_Answer("frontend"))
    decision = D.describe("task.kind", tuple(ROUTING), context="Add the favicon route, SVG, link, and endpoint test.")
    verdict = D.JevDecider(client=client).decide(decision)
    question = client.calls[0]["questions"]["task_kind"]
    assert question["criteria"] == L.KIND_DEFINITIONS and None not in question["criteria"].values()
    assert question["instructions"].startswith(D.DECISIONS["task.kind"]["question"])
    assert "choose frontend over backend" in question["instructions"]
    assert verdict.usage["labels"] == L.LABELS_VERSION == decision.labels


def test_difficulty_definitions_are_about_reasoning_not_line_count():
    decision = D.describe("task.difficulty", tuple(DIFFICULTY_LADDER))
    assert "Line count is not difficulty" in decision.guidance
    for word in ("reasoning", "context", "depend"):
        assert any(word in text for text in decision.definitions.values()), word


def test_an_undefined_answer_is_refused_rather_than_sent_bare():
    with pytest.raises(KeyError, match="no definition for mystery"):
        D.describe("task.kind", ("backend", "mystery"))


def test_a_hand_built_decision_still_sends_bare_criteria_and_no_labels_version():
    client = _Client(_Answer("rote"))
    verdict = D.JevDecider(client=client).decide(DIFFICULTY)
    criteria = client.calls[0]["questions"]["task_difficulty"]["criteria"]
    assert set(criteria) == set(DIFFICULTY.answers) and set(criteria.values()) == {None}
    assert "labels" not in verdict.usage


def test_the_docs_carry_the_rendered_definitions_verbatim():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "docs" / "decisions-api.md").read_text(encoding="utf-8")
    assert L.render_markdown() in doc
    assert f"`{L.LABELS_VERSION}`" in doc
