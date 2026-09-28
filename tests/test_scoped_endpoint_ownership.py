"""The scoped-endpoint missing-owner rule and the verifier's finding marker."""

import inspect

from quadratus import session
from quadratus.policy import load_library


def test_scoped_endpoint_denies_a_missing_owner_and_needs_test_evidence():
    origin, _, cards = load_library()
    checklist = {c["id"]: c for c in cards["scoped-endpoint"]["checklist"]}
    assert cards["scoped-endpoint"]["version"] == "0.2.0"
    assert "verifier" in checklist["missing-owner-denied"]["applies_to"]
    assert checklist["missing-owner-denied"]["gate_id"] == "cross-tenant-test"
    assert set(checklist["ownership-test-evidence"]["applies_to"]) == {"reviewer", "verifier"}


def test_the_verifier_is_told_the_marker_a_finding_must_open_with():
    assert "BLOCKING:" in inspect.getsource(session.Session._verifier_prompt)
