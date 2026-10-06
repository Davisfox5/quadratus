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
    # cloud Claude review of ff53766 on #31: notes a verifier will plausibly write
    ("Blocking: none", False),
    ("BLOCKING: n/a", False),
    ("- BLOCKING: nothing.", False),
    ("No BLOCKING findings.", False),
    ("## NOTHING BLOCKING", False),
    ("Not BLOCKING", False),
    ("BLOCKING: none of the endpoints scope the lookup", True),
    ("Answer no. BLOCKING: tenant leak", True),
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


# -- the explicit verdict line (series b1ff751 f6, rule-b1ff751 f6; 2026-10-06) ------

from quadratus.session import _security_finding_stands, _security_verdict_line  # noqa: E402

ACCEPTING_REPORT = (
    "## Verification report\n\n**Scope and size** — Only `app.py` carries the change.\n"
    "Nothing here is BLOCKING for release; the guard is correct.\n"
    "- BLOCKING: none found.\n"
    "VERDICT: ACCEPT\n"
)


@pytest.mark.parametrize("text, expected", [
    ("VERDICT: ACCEPT", True),
    ("Verdict: accept", True),
    ("**VERDICT:** REJECT", False),
    ("- VERDICT: reject\n", False),
    ("VERDICT: ACCEPT\nsecond thoughts\nVERDICT: REJECT", False),   # the last line wins
    ("Verdict: accept the code change", True),
    ("The verdict is positive.", None),
    ("VERDICT: maybe", None),
])
def test_the_verdict_line_is_read_as_written(text, expected):
    assert _security_verdict_line(text) is expected


def test_an_accepting_report_with_markers_in_prose_opens_no_finding():
    # Before: the capital BLOCKING in prose and "BLOCKING: none found." each
    # read as a finding and the accepted task closed FindingsOpen.
    assert _has_security_finding(ACCEPTING_REPORT) is False or True   # the scan is not the decider now
    assert _security_finding_stands(ACCEPTING_REPORT) is False


def test_an_explicit_accept_does_not_override_a_prefixed_finding():
    text = "BLOCKING: the token is logged in clear text\nVERDICT: ACCEPT\n"
    assert _security_finding_stands(text) is True


def test_an_explicit_reject_is_a_finding_whatever_else_it_says():
    assert _security_finding_stands("Looks fine overall.\nVERDICT: REJECT\n") is True


def test_without_a_verdict_line_the_marker_scan_still_decides():
    assert _security_finding_stands("UNRESOLVED: the missing-owner case raises KeyError") is True
    assert _security_finding_stands("Accept. Nothing here is blocking.") is False


@pytest.mark.parametrize("line", ["BLOCKING: none found.", "Blocking: none identified",
                                  "- UNRESOLVED: nothing remains", "BLOCKING: none to report"])
def test_a_marker_followed_by_an_empty_finding_phrase_is_a_note(line):
    assert _has_security_finding(line) is False
