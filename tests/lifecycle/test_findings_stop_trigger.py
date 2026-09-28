"""The serial open-findings stop reads the typed record too (map P3.4;
Sol inventory 5863608874).

After each serial task the run stopped on the legacy ``open_findings``
list alone. It now stops on the projected active findings
(quadratus.finding_state) or the legacy list; a malformed record fails
closed, and a disagreement is recorded on the closing task. The legacy
loss is injected just before the trigger, a controller invariant; the
finding itself is a real design review's.
"""

from quadratus.session import Session
from tests.lifecycle import test_lifecycle_matrix as matrix
from tests.lifecycle.test_lifecycle_matrix import FILES, Script


def _lose_legacy_at_close(monkeypatch):
    note = Session._note

    def losing(self, message):
        if message.startswith("task ") and " closed by " in message:
            self.open_findings.clear()
        return note(self, message)
    monkeypatch.setattr(Session, "_note", losing)


def _t1(replay):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")


def test_an_open_finding_lost_from_the_legacy_list_still_stops_the_run(tmp_path, monkeypatch):
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import (
        _design_files,
        _design_script,
        _edits_and_captures,
    )
    _lose_legacy_at_close(monkeypatch)
    script = _design_script("Renders refreshed.\nCHANGED: []",
                            review="BLOCKING: the renders show a page where the Import button does not appear")
    script.overrides["lead"] = _edits_and_captures
    script.overrides["orchestrator"] = lambda call, replay: (
        matrix.DECL_DESIGN if len(replay.of("orchestrator")) == 1 else matrix.DECL_T2)
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3, record_complete=False)
    assert len(replay.of("orchestrator")) == 1, "the run stopped after t1; no further task was asked for"
    assert replay.result.error.startswith("FindingsOpen: a task closed with open work"), replay.result.error
    assert "open findings: typed True, legacy False" in _t1(replay)["mismatches"]
    assert not replay.result.completed


# -- controls -----------------------------------------------------------------------

def test_the_same_finding_with_the_legacy_list_intact_stops_the_same_way(tmp_path, monkeypatch):
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import (
        _design_files,
        _design_script,
        _edits_and_captures,
    )
    script = _design_script("Renders refreshed.\nCHANGED: []",
                            review="BLOCKING: the renders show a page where the Import button does not appear")
    script.overrides["lead"] = _edits_and_captures
    script.overrides["orchestrator"] = lambda call, replay: (
        matrix.DECL_DESIGN if len(replay.of("orchestrator")) == 1 else matrix.DECL_T2)
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3, record_complete=False)
    assert replay.result.error.startswith("FindingsOpen: a task closed with open work"), replay.result.error
    assert len(replay.of("orchestrator")) == 1
    assert not [m for m in _t1(replay)["mismatches"] if m.startswith("open findings:")]


def test_a_task_with_no_findings_does_not_stop(tmp_path, monkeypatch):
    from tests.lifecycle import harness as H
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: (
        matrix.DECL_T1 if len(replay.of("orchestrator")) == 1 else "DONE")), files=FILES, max_tasks=3)
    assert replay.result.completed, replay.result.error
    assert not [m for t in replay.workflow["tasks"] for m in t["mismatches"] if m.startswith("open findings:")]
