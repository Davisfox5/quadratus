"""The verifier's security-finding detector (Q9-v2 series, 2026-09-24)."""

import pytest

from quadratus.session import _has_security_finding


@pytest.mark.parametrize("verdict, finding", [
    ("RESOLVED\n## Two minor notes, neither blocking", False),
    ("RESOLVED\nOne non-blocking note for the record: a dict.get carries no predicate", False),
    ("Verdict: accept.\nNot blocking, worth noting: x", False),
    ("Accept. Nothing here is blocking.", False),
    ("Unresolved questions: none", False),
    ("UNRESOLVED QUESTIONS: none", True),  # uppercase is a marker: fail safe
    ("One NON-BLOCKING note", False),
    ("This authorization defect is BLOCKING.", True),
    ("Not blocking, but UNRESOLVED: missing evidence.", True),
    ("BLOCKING: tenant leak in get_record", True),
    ("- BLOCKING: tenant leak", True),
    ("**BLOCKING:** tenant leak", True),
    ("1. BLOCKING: the predicate is not called", True),
    ("UNRESOLVED: the missing-owner case raises KeyError", True),
    ("## UNRESOLVED", True),
    ("UNRESOLVED", True),
])
def test_only_a_line_opening_with_a_marker_is_a_finding(verdict, finding):
    assert _has_security_finding(verdict) is finding
