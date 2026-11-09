"""Requirement rulings must preserve the identity of each ambiguity."""

import pytest

from quadratus.ledger import Ledger


@pytest.mark.parametrize("ruling", [
    "Q: R10, which screen? -- A: the settings screen",
    "R11: use the existing parser",
    "For R100, retain the current behavior",
])
def test_a_ruling_for_another_requirement_does_not_settle_its_id_prefix(ruling):
    ledger = Ledger()
    ledger.ambiguous = {"R1": "Which format?", "R10": "Which screen?"}
    ledger.ambiguous_since = {"R1": 0, "R10": 0}
    ledger.rulings.append(ruling)

    assert not ledger.settled("R1")


@pytest.mark.parametrize("ruling", [
    "Q: R1, which format? -- A: JSON",
    "For (R1), use JSON",
    "R1: JSON",
])
def test_an_exact_requirement_ruling_still_settles_its_ambiguity(ruling):
    ledger = Ledger()
    ledger.ambiguous = {"R1": "Which format?", "R10": "Which screen?"}
    ledger.rulings.append(ruling)

    assert ledger.settled("R1")
    assert not ledger.settled("R10")
