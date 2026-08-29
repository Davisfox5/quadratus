"""Tests for recovering structured output from prose-shaped model replies."""

from __future__ import annotations

import pytest

from quadratus.structured import (
    StructuredError,
    extract_json,
    generate_structured,
    schema_instruction,
)

VERDICT_KEYS = ["accept", "blocking_issues"]


def test_parses_bare_object():
    out = extract_json('{"accept": true, "blocking_issues": []}', required_keys=VERDICT_KEYS)
    assert out["accept"] is True


def test_strips_json_fence():
    """The observed real-world failure: models fence despite being told not to."""
    raw = '```json\n{"accept": false, "blocking_issues": ["bare except"]}\n```'
    out = extract_json(raw, required_keys=VERDICT_KEYS)
    assert out["blocking_issues"] == ["bare except"]


def test_strips_untagged_fence():
    raw = '```\n{"accept": true, "blocking_issues": []}\n```'
    assert extract_json(raw, required_keys=VERDICT_KEYS)["accept"] is True


def test_ignores_surrounding_prose():
    raw = (
        "Sure! Here is my assessment of the solution:\n\n"
        '{"accept": false, "blocking_issues": ["hardcoded password"]}\n\n'
        "Let me know if you would like me to revise it."
    )
    out = extract_json(raw, required_keys=VERDICT_KEYS)
    assert out["blocking_issues"] == ["hardcoded password"]


def test_tolerates_trailing_comma():
    raw = '{"accept": true, "blocking_issues": [],}'
    assert extract_json(raw, required_keys=VERDICT_KEYS)["accept"] is True


def test_tolerates_smart_quotes():
    raw = '{“accept”: false, “blocking_issues”: []}'
    assert extract_json(raw, required_keys=VERDICT_KEYS)["accept"] is False


def test_braces_inside_strings_do_not_break_scanning():
    raw = 'Note: the code used "}" as a delimiter.\n{"accept": true, "blocking_issues": []}'
    assert extract_json(raw, required_keys=VERDICT_KEYS)["accept"] is True


def test_skips_unrelated_object_and_finds_the_conforming_one():
    """A code snippet in the reply must not be mistaken for the payload."""
    raw = (
        'Here is the config I reviewed: {"host": "localhost", "port": 5432}\n'
        'And my verdict:\n{"accept": false, "blocking_issues": ["no TLS"]}'
    )
    out = extract_json(raw, required_keys=VERDICT_KEYS)
    assert out["blocking_issues"] == ["no TLS"]


def test_empty_response_raises():
    with pytest.raises(StructuredError):
        extract_json("   ", required_keys=VERDICT_KEYS)


def test_missing_key_reports_which_key():
    with pytest.raises(StructuredError, match="blocking_issues"):
        extract_json('{"accept": true}', required_keys=VERDICT_KEYS)


def test_no_json_at_all_raises():
    with pytest.raises(StructuredError):
        extract_json("I would rather explain this in prose.", required_keys=VERDICT_KEYS)


def test_schema_instruction_lists_every_field():
    text = schema_instruction({"accept": "boolean", "blocking_issues": "array of strings"})
    assert "accept" in text and "blocking_issues" in text


class _ScriptedProvider:
    """Returns a fixed sequence of replies, recording the prompts it received."""

    label = "Scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def generate(self, prompt, *, system="", history=None):
        self.prompts.append(prompt)
        return self._replies.pop(0)


def test_generate_structured_succeeds_first_try():
    p = _ScriptedProvider(['{"accept": true, "blocking_issues": []}'])
    out = generate_structured(
        p, "assess it", system="be terse",
        schema={"accept": "boolean", "blocking_issues": "array of strings"},
    )
    assert out["accept"] is True
    assert len(p.prompts) == 1


def test_generate_structured_retries_with_correction():
    p = _ScriptedProvider(
        ["I think it's fine honestly.", '{"accept": true, "blocking_issues": []}']
    )
    out = generate_structured(
        p, "assess it", system="be terse",
        schema={"accept": "boolean", "blocking_issues": "array of strings"},
    )
    assert out["accept"] is True
    assert len(p.prompts) == 2
    assert "CORRECTION REQUIRED" in p.prompts[1]


def test_generate_structured_gives_up_and_reports():
    p = _ScriptedProvider(["nope", "still nope", "nope again"])
    with pytest.raises(StructuredError, match="after 3 attempts"):
        generate_structured(
            p, "assess it", system="be terse",
            schema={"accept": "boolean", "blocking_issues": "array of strings"},
            max_attempts=3,
        )
    assert len(p.prompts) == 3
