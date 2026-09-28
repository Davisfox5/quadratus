"""A post-loop exception still re-checks the audit ledger (map P3.4; Opus
audit 23d6460, Codex 5862699144).

A run that ends incomplete re-checks resolved findings at the end: a
resolution a later task's changes undid is reopened. An exception from the
loop does the same before it is re-raised. An exception after the loop (the
end-of-run dependency check) skipped it, so an undone resolution stayed
``resolved`` on the record. The end-of-run exception is injected, a
controller invariant; the undone resolution is a real later edit.
"""

import pytest

from quadratus.session import Session
from tests.lifecycle.test_audit_findings import AUDIT, REPAIR, REQS, WIDE, _capture, _repair, _run

pytestmark = pytest.mark.requirements_ledger

UNDO = REPAIR.replace("Fix the mobile overflow.", "Restyle the toolbar.")


def _journey(tmp_path, monkeypatch):
    """t1 audits (F1), t2 resolves F1, t3 restyles the page and its renders
    are not clean, so the run stops DesignUnverified after t3."""
    return _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1", UNDO],
                {"t1": _capture(measured=WIDE), "t2": _repair(),
                 "t3": _repair(css=".toolbar { gap: 0; }\n", clean=False)}, max_tasks=6)


def _fail_at_end(monkeypatch):
    verify = Session._verify_dependencies

    def failing(self, window):
        if window == "at the end of the run":
            raise OSError("the dependency tree could not be read")
        return verify(self, window)
    monkeypatch.setattr(Session, "_verify_dependencies", failing)


def test_without_an_exception_the_undone_resolution_is_reopened(tmp_path, monkeypatch):
    replay = _journey(tmp_path, monkeypatch)
    assert replay.result.error.startswith("DesignUnverified: task t3"), replay.result.error
    f1 = replay.findings[0]
    assert f1["resolved_by"] == "t2" or f1.get("reopened"), f1
    assert f1["status"] == "open" and "no longer holds" in f1["reopened"], f1


def test_a_post_loop_exception_still_reopens_the_undone_resolution(tmp_path, monkeypatch):
    _fail_at_end(monkeypatch)
    replay = _journey(tmp_path, monkeypatch)
    assert replay.result.error == "OSError: the dependency tree could not be read"
    f1 = replay.findings[0]
    assert f1["status"] == "open" and "no longer holds" in f1["reopened"], f1
