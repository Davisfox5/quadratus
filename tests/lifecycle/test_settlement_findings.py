"""Settlement reads the resolving task's own typed findings too (map P3.4;
Sol inventory 5863608874).

A task naming RESOLVES settles its findings only if it closed with no open
finding of its own. That was read from the legacy list's growth alone; it
now also reads the active findings the projection files under the task, a
malformed record failing closed and a disagreement recorded on the task.
The finding is a real collaborator review left blocking; the legacy loss
is injected just before settlement, a controller invariant.
"""

import pytest

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import AUDIT, REPAIR, REQS, WIDE, _capture, _repair, _run

pytestmark = pytest.mark.requirements_ledger


def _blocked_review(tmp_path, monkeypatch):
    roles = {"collaborator": lambda call, replay: ("BLOCKING: the toolbar gap is inconsistent" if call.task == "t2"
                                                   else "No blocking findings."),
             "recheck": lambda call, replay: "STILL BLOCKING: the gap is still inconsistent"}
    return _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                {"t1": _capture(measured=WIDE), "t2": _repair()}, roles=roles)


def _t2(replay):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == "t2")


def test_a_finding_lost_from_the_legacy_list_still_blocks_settlement(tmp_path, monkeypatch):
    settle = Session._settle_resolution

    def losing(self, spec, checks_before, open_before):
        del self.open_findings[open_before:]
        return settle(self, spec, checks_before, open_before)
    monkeypatch.setattr(Session, "_settle_resolution", losing)
    run = H.run
    # A recorded mismatch makes the record incomplete on purpose.
    monkeypatch.setattr(H, "run", lambda *a, **kw: run(*a, **dict(kw, record_complete=False)))
    replay = _blocked_review(tmp_path, monkeypatch)
    f1 = replay.findings[0]
    assert f1["status"] == "open", "not settled over the task's own open finding"
    assert f1["last_attempt"] == "t2: it closed with open findings"
    assert "new findings: typed True, legacy False" in _t2(replay)["mismatches"]


# -- controls -----------------------------------------------------------------------

def test_the_same_finding_with_the_legacy_list_intact_blocks_the_same_way(tmp_path, monkeypatch):
    replay = _blocked_review(tmp_path, monkeypatch)
    f1 = replay.findings[0]
    assert f1["status"] == "open" and f1["last_attempt"] == "t2: it closed with open findings"
    assert not [m for m in _t2(replay)["mismatches"] if m.startswith("new findings:")]


def test_a_clean_resolving_task_still_settles(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, [REQS + AUDIT, REPAIR + "\nRESOLVES: F1"],
                  {"t1": _capture(measured=WIDE), "t2": _repair()})
    assert replay.findings[0]["status"] == "resolved" and replay.findings[0]["resolved_by"] == "t2"
    assert not [m for m in _t2(replay)["mismatches"] if m.startswith("new findings:")]
