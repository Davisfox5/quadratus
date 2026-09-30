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
