"""Reproduction only: a post-loop exception skips the resolved-finding
re-check that the in-loop exception path runs (audit 23d6460, section 2).

The journey is a whole-controller replay with fake providers: t1 audits
the import page and records F1, t2 repairs it and resolves F1, t3 (in
scope) edits README.md and app.py, so the source no longer matches t2's
renders and the integration gate fails. A failed gate ends the loop through
``_name_findings_stop`` with no re-check, so ``_finish_run`` is the first
place F1 would be looked at again.

Reachable vs synthetic:

* Everything up to the end of the loop is reachable with ordinary scripted
  replies (the same shape as test_audit_findings' failed-gate case).
* The exception after the loop is **injected** (``_verify_dependencies`` at
  "at the end of the run"), as tests/test_post_loop_exception.py does. In
  the engine as it stands, ``DependencyWatch.verify`` turns every I/O
  failure into a dependency exception that ``_finish_run`` catches, so
  only a signal (KeyboardInterrupt, which project_run persists), an
  interpreter failure, or a defect can raise there.

These tests pin today's behaviour. No engine change is made here; see
docs/review/postloop-ledger-reproduction.md.
"""

import json

import pytest

from quadratus.config import Settings
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_audit_findings import (
    AUDIT,
    DOCS,
    MET,
    NO_EDIT,
    REPAIR,
    REQS,
    WIDE,
    _capture,
    _repair,
)
from tests.lifecycle.test_lifecycle_matrix import T2, Script, _design_files

pytestmark = pytest.mark.requirements_ledger

END = "at the end of the run"
BREAKING_DOCS = DOCS.replace(json.dumps(T2), json.dumps(dict(T2, permitted_paths=["README.md", "app.py"])))
BROKEN_ADD = "def add(a, b):\n    return a - b - 1\n"
STALE = "its resolving evidence no longer holds"


def _breaking_docs(call, replay):
    """In scope; changes the source t2's renders were taken of, and fails the gate."""
    H.write(call, {"README.md": "# app\n\nchanged later\n", "app.py": BROKEN_ADD})
    return 'Documented.\nCHANGED: ["README.md", "app.py"]'


def _rogue_docs(call, replay):
    """Out of scope (templates/index.html): the loop itself raises (existing reachable case)."""
    H.write(call, {"README.md": "# app\n\nx\n", "templates/index.html": "<p>moved</p>\n"})
    return 'Documented.\nCHANGED: ["README.md", "templates/index.html"]'


def _journey(tmp_path, monkeypatch, *, plan, t3=None, record_complete=True):
    """test_audit_findings._run's replay, with ``record_complete`` exposed for exception exits."""
    plan = list(plan)
    leads = {"t1": _capture(measured=WIDE), "t2": _repair(), "t3": t3}
    audits = [MET] * 8
    script = Script(
        orchestrator=lambda call, replay: plan.pop(0) if plan else "DONE",
        lead=lambda call, replay: leads[call.task](call, replay),
        revision=lambda call, replay: NO_EDIT,
        **{"design-review": lambda call, replay: "APPROVED",
           "requirements-review": lambda call, replay: "COMPLETE",
           "auditor": lambda call, replay: audits.pop(0) if audits else MET,
           "design-fix": lambda call, replay: "Captured again.\nCHANGED: []",
           "gate-fix": lambda call, replay: "Could not see why.\nCHANGED: []"})
    return H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=8,
                 settings=Settings(backend="cli"), record_complete=record_complete)


def _gate_journey(tmp_path, monkeypatch, **kw):
    return _journey(tmp_path, monkeypatch, plan=[REQS + AUDIT, REPAIR + "\nRESOLVES: F1", BREAKING_DOCS],
                    t3=_breaking_docs, **kw)


def _raise_at(monkeypatch, cls, name, window_or_none, exc):
    """Synthetic: ``cls.name`` raises ``exc`` (only for ``window_or_none`` when given)."""
    real = getattr(cls, name)
    fired = []

    def patched(self, *args):
        if window_or_none is None or args[:1] == (window_or_none,):
            fired.append(name)
            raise exc
        return real(self, *args)
    monkeypatch.setattr(cls, name, patched)
    return fired


def _keep_session(monkeypatch):
    held = {}
    real = Session.run

    def run(self, *a, **k):
        held["session"] = self
        return real(self, *a, **k)
    monkeypatch.setattr(Session, "run", run)
    return held


def _record(replay):
    """What the run says (outcome) beside what its ledger says (findings, requirements)."""
    data = H.result_json(replay)
    f1 = data["findings"][0]
    facts = replay.workflow["run"]["facts"]
    return dict(completed=replay.result.completed, error=replay.result.error,
                stop=facts[-1].get("legacy") if facts else None,
                f1=(f1["status"], f1.get("resolved_by"), f1.get("reopened", ""), f1.get("unresolved_reason", "")),
                ledger={rid: data["requirements"]["status"].get(rid, "") for rid in ("R1", "R2")})


def _still_verifies(session):
    f1 = next(f for f in session.findings if f["id"] == "F1")
    return not session._identity_problem(f1["resolved_by"], f1["target"], f1["steps"], f1["resolution"])


# -- controls -----------------------------------------------------------------------------

def test_control_without_t3_the_resolution_holds(tmp_path, monkeypatch):
    replay = _journey(tmp_path, monkeypatch, plan=[REQS + AUDIT, REPAIR + "\nRESOLVES: F1"])
    f1 = H.result_json(replay)["findings"][0]
    assert (f1["status"], f1["resolved_by"]) == ("resolved", "t2") and replay.result.completed


def test_ordinary_end_rechecks_and_reopens_the_undone_resolution(tmp_path, monkeypatch):
    """Reachable: the same journey with no exception. ``_finish_run`` re-checks."""
    replay = _gate_journey(tmp_path, monkeypatch)
    rec = _record(replay)
    assert H.gate_results(replay)[-1] == "FAILED", "t3's own gate failure ends the loop"
    assert not rec["completed"]
    status, resolved_by, reopened, why = rec["f1"]
    assert (status, resolved_by) == ("open", "t2") and reopened.startswith(STALE)
    assert why.startswith("open when the run stopped")
    assert rec["ledger"] == {"R1": "NOT MET: open finding F1", "R2": "NOT MET: open finding F1"}


def test_in_loop_exception_at_the_same_point_rechecks(tmp_path, monkeypatch):
    """Synthetic, same journey: the raise is moved one step earlier, inside
    the loop (the failed-gate stop), so ``run``'s own except path handles it."""
    fired = _raise_at(monkeypatch, Session, "_name_findings_stop", None, OSError("synthetic in-loop failure"))
    replay = _gate_journey(tmp_path, monkeypatch, record_complete=False)
    rec = _record(replay)
    assert fired == ["_name_findings_stop"]
    assert not rec["completed"] and rec["error"].startswith("OSError")
    assert rec["f1"][0] == "open" and rec["f1"][2].startswith(STALE)
    assert rec["ledger"]["R1"] == "NOT MET: open finding F1"


def test_in_loop_reachable_exception_after_an_undo_rechecks(tmp_path, monkeypatch):
    """Reachable: t3 writes out of scope and the loop raises (the existing
    test_an_exception_after_a_source_change_reopens_an_earlier_resolution)."""
    replay = _journey(tmp_path, monkeypatch, plan=[REQS + AUDIT, REPAIR + "\nRESOLVES: F1", DOCS], t3=_rogue_docs)
    rec = _record(replay)
    assert not rec["completed"] and rec["error"]
    assert rec["f1"][0] == "open" and rec["f1"][2].startswith(STALE)
    assert rec["ledger"]["R1"] == "NOT MET: open finding F1"


def test_post_loop_exception_over_a_valid_resolution_is_not_stale(tmp_path, monkeypatch):
    """Synthetic post-loop raise with no undo: 'resolved' after the exception
    is correct here, so the reproduction below is about the undo, not the raise."""
    _raise_at(monkeypatch, Session, "_verify_dependencies", END, OSError("synthetic end-of-run failure"))
    held = _keep_session(monkeypatch)
    replay = _journey(tmp_path, monkeypatch, plan=[REQS + AUDIT, REPAIR + "\nRESOLVES: F1"], record_complete=False)
    assert not replay.result.completed and replay.result.error.startswith("OSError")
    assert H.result_json(replay)["findings"][0]["status"] == "resolved"
    assert _still_verifies(held["session"])


# -- the reproduction ----------------------------------------------------------------------

@pytest.mark.parametrize("exc", [OSError("synthetic end-of-run failure"), KeyboardInterrupt()],
                         ids=["OSError", "KeyboardInterrupt"])
def test_post_loop_exception_leaves_the_undone_resolution_resolved(tmp_path, monkeypatch, exc):
    """Reachable journey + synthetic post-loop raise: outcome incomplete,
    ledger still says F1 resolved and its requirements not NOT MET."""
    fired = _raise_at(monkeypatch, Session, "_verify_dependencies", END, exc)
    held = _keep_session(monkeypatch)
    replay = _gate_journey(tmp_path, monkeypatch, record_complete=False)
    rec = _record(replay)
    assert fired == ["_verify_dependencies"], "raised exactly once, after the loop"
    assert H.gate_results(replay)[-1] == "FAILED", "the loop ended as in the ordinary control"
    # Outcome: incomplete; the exception is the typed stop (a07e7bf).
    assert rec["completed"] is False
    assert rec["error"].startswith(type(exc).__name__) and rec["stop"] == type(exc).__name__
    # Ledger: F1 still resolved by t2 -- no reopening, no distrust note, no reason.
    # ``reopened`` is present as None (never set by a re-check), not absent.
    assert rec["f1"] == ("resolved", "t2", None, "")
    assert not any(v.startswith("NOT MET") for v in rec["ledger"].values()), rec["ledger"]
    # And the row is wrong, not just unannotated: the skipped re-check would reopen it.
    assert not _still_verifies(held["session"])


def test_post_loop_failing_recheck_is_not_distrusted(tmp_path, monkeypatch):
    """Second arm of the audit: the re-check itself raises after the loop.
    The in-loop path answers that with ``_distrust_resolutions``
    (test_a_failed_recheck_on_an_exception_exit_distrusts_resolutions);
    the post-loop path does not. Synthetic: the re-check is replaced."""
    fired = _raise_at(monkeypatch, Session, "_recheck_resolved_findings", None, OSError("synthetic recheck failure"))
    replay = _gate_journey(tmp_path, monkeypatch, record_complete=False)
    rec = _record(replay)
    assert fired == ["_recheck_resolved_findings"], "only _finish_run's call site is on this path"
    assert rec["completed"] is False and rec["error"].startswith("OSError")
    assert rec["f1"] == ("resolved", "t2", None, "")
    assert not any(v.startswith("NOT MET") for v in rec["ledger"].values()), rec["ledger"]
