"""The requirements auditor sees the harness's own check facts.

Overnight r02 (20261011T134747Z-b14aa88b): the audit prompt carried only
"gate suite: passed". The auditor ran the browser tests in its own copy,
which had no Playwright, and marked R6 NOT MET against a same-source
harness pass of 121 Python and 64 Node tests with none skipped. The audit
now sees, per command id, the status, counts and whether it ran on the
delivered source. It still judges for itself: nothing here makes a MET.

The helper is imported inside the tests that call it, so the real-prompt
test fails on the old engine by what the prompt says, not at import.
"""

import dataclasses

import pytest

from tests.lifecycle.test_direct_tier import _decl, _run

pytestmark = pytest.mark.requirements_ledger

DELIVERED = "a" * 64


def _receipt(**changes):
    receipt = dict(id="check", required=True, status="passed", reason="exit 0", tests=3,
                   source_hash=DELIVERED, command="/private/grader/run.py",
                   report=dict(state="parsed", counts=dict(passed=3, failed=0, errors=0, skipped=0)))
    receipt.update(changes)
    return receipt


def test_the_real_audit_prompt_carries_counts_and_source_not_commands(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _decl())
    [audit] = replay.of("auditor")
    assert "## Latest project checks (run by the harness" in audit.prompt
    assert "passed, 0 failed, 0 skipped; on the delivered source" in audit.prompt
    assert "a test you cannot run there is not evidence that it fails" in audit.prompt
    assert "A check that failed on the delivered source is evidence against it" in audit.prompt
    assert "none of these establishes a pass" in audit.prompt
    assert "shows nothing about" not in audit.prompt
    assert "Judge each requirement yourself" in audit.prompt
    assert "--quadratus-report" not in audit.prompt, "no command line reaches the auditor"


def test_a_check_on_an_earlier_source_is_named_as_such():
    from quadratus.session import _audit_check_facts
    text = _audit_check_facts([dict(passed=True, receipts=[_receipt(source_hash="b" * 64)])], DELIVERED)
    assert "on an EARLIER source, not the delivered one" in text


def test_failures_skips_and_unknowns_are_stated_not_smoothed():
    from quadratus.session import _audit_check_facts
    text = _audit_check_facts([dict(passed=False, receipts=[
        _receipt(id="extra-1", status="failed", reason="nonzero exit",
                 report=dict(state="parsed", counts=dict(passed=58, failed=4, errors=0, skipped=2))),
        _receipt(id="extra-2", status="error", reason="timed out after 600s", tests=None, report=None),
    ])], DELIVERED)
    assert "extra-1 (required): failed: nonzero exit; 58 passed, 4 failed, 2 skipped" in text
    assert "extra-2 (required): error: timed out after 600s; counts unknown" in text
    assert "/private/grader" not in text


def test_a_single_command_check_without_a_record_says_so():
    from quadratus.session import _audit_check_facts
    text = _audit_check_facts([dict(passed=True, command="pytest", receipts=[])], DELIVERED)
    assert text == "- check: passed; counts unknown; source not established"


def test_a_missing_runner_reason_does_not_name_its_path(tmp_path):
    # Root review of c983abe: a real GateSuite with a missing executable
    # puts the path in the receipt's reason; the audit must not repeat it.
    from quadratus.integration import GateCommand, GateSuite
    from quadratus.session import _audit_check_facts, _check_for_models
    runner = "/private/grader/run.py"
    result = GateSuite([GateCommand(id="extra-1", argv=(runner,))], cwd=tmp_path).run()
    [receipt] = result.receipts
    assert runner in receipt.reason, "the control: the raw reason carries the path"
    check = dict(passed=result.passed, command=result.command,
                 receipts=[dataclasses.asdict(r) for r in result.receipts])
    text = _audit_check_facts([check], DELIVERED)
    assert text.startswith("- extra-1 (required): blocked: runner unavailable")
    assert "/private" not in text and "grader" not in text and "run.py" not in text
    gate = _check_for_models(check)["gates"][0]
    assert "/private" not in gate["reason"] and "run.py" not in gate["reason"]
