"""The no-finding sentinel on a judge's NEW or BLOCKING line (close family,
2026-10-26). ``recheck_verdicts`` used ``re.match("(none|n/?a|nothing)\\b")``,
so a concrete finding that opened with negative language was dropped in
silence (root's two whole-controller controls on bf30f82). The sentinel is
now the whole claim: none, n/a or nothing, alone or with a bounded tail.
"""
import pytest

from quadratus import judgment
from quadratus.judgment import _empty_claim

FINDINGS = [
    'Nothing rejects nonnumeric inputs in app.py:add; add("a", "b") returns "ab" | basis: R1 | impact: correctness',
    "None of the arguments are checked for type in app.py:add | basis: R1 | impact: correctness",
    "nothing tests negative numbers | basis: R1 | impact: correctness",
    "N/A handling: the CLI prints N/A for a missing id instead of JSON | basis: R2 | impact: contract",
    "Nothing else guards the empty title",
]
SENTINELS = ["none", "None.", "N/A", "n/a", "nothing", "nothing found", "none to report.", "Nothing further",
             "nothing new", "none remaining", "none | basis: R1 | impact: other"]


@pytest.mark.parametrize("claim", FINDINGS)
def test_negative_language_that_says_something_is_a_finding(claim):
    assert not _empty_claim(claim)
    reading = judgment.recheck_verdicts(f"F1: UNRESOLVED still open\nNEW: {claim}", ["F1"])
    assert [n.line for n in reading.new] == [claim]


@pytest.mark.parametrize("claim", SENTINELS)
def test_the_standalone_sentinel_is_no_finding(claim):
    assert _empty_claim(claim)
    reading = judgment.recheck_verdicts(f"F1: RESOLVED shown\nNEW: {claim}", ["F1"])
    assert reading.new == ()
