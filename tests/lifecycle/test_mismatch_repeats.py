"""A contract drift is recorded once per distinct note (map P3.4; O-NEXT-11
at 5ad711e, Sol ruling 5865650035).

``_contract_agrees``, ``_required`` and ``_design_instruction`` appended one
line per disagreeing read, so a drift that persisted across recovery rounds
(the full gate after a design-fix, ``collaborators_for`` after a lead
recovery, one review prompt per collaborator) was recorded several times.
They now keep the first occurrence of each exact note and every distinct
one. Blocking decisions read presence and are unchanged; serialized lists,
counts and the guard's stop text shorten. Whole-controller replays from
O-NEXT-11's measurement, flipped to the deduplicated counts; the drift is a
synthetic write, a controller invariant.
"""

import collections
import dataclasses

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_contract_applicability import _run, _t1


def _counts(replay):
    return dict(collections.Counter(_t1(replay)["mismatches"]))


def _drift_from_draft(monkeypatch, **changes):
    """Change the live config as t1's lead starts drafting, and keep it
    changed for the rest of the task: a drift that persists across every
    later round."""
    draft = Session._draft_with_channels

    def drifted(self, lead, spec, *args, **kw):
        if spec.task_id == "t1":
            self.config = dataclasses.replace(self.config, **changes)
        return draft(self, lead, spec, *args, **kw)
    monkeypatch.setattr(Session, "_draft_with_channels", drifted)


def _no_renders(call, replay):
    """A design lead that edits but leaves no renders: one design-fix."""
    if call.task != "t1":
        return "Documented.\nCHANGED: []"
    H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
    return 'Added the button.\nCHANGED: ["templates/index.html"]'


def test_r1_checks_drift_is_recorded_once_across_full_gate_runs(tmp_path, monkeypatch):
    """The gate's own drift (integration_gate gone live) across a design-fix,
    which re-runs the full gate: one line per full gate run."""
    _drift_from_draft(monkeypatch, integration_gate=None)
    replay = _run(tmp_path, monkeypatch, _no_renders, record_complete=False)
    assert len(replay.of("design-fix")) == 1
    assert _counts(replay) == {"checks: contract True, legacy False": 1}
    assert not replay.result.completed


def test_r2_collaboration_drift_is_recorded_once_across_readers(tmp_path, monkeypatch):
    """design_cross_check drifts off at the draft and stays off through the
    review, the design-fix and the design check."""
    _drift_from_draft(monkeypatch, design_cross_check=False)
    replay = _run(tmp_path, monkeypatch, _no_renders, record_complete=False)
    assert len(replay.of("design-fix")) == 1
    collaborators = [c for c in replay.of("collaborator") if c.task == "t1"]
    counts = _counts(replay)
    assert collaborators, "the review still ran"
    assert counts == {
        "design_collaboration_applicable: contract True, legacy False": 1,
        "design_review: contract True, legacy False": 1,
    }, counts
    assert not replay.result.completed


def test_r3_collaboration_drift_is_recorded_once_across_a_lead_recovery(tmp_path, monkeypatch):
    """The first lead fails without touching source; the recovery lead
    redrafts. collaborators_for runs for each lead, and the drift persists."""
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "grok:default")

    def lead(call, replay):
        if call.vendor == "grok" and call.task == "t1":
            return H.grok_ok("I'll create it", stop="cancelled", num_turns=3)
        return _no_renders(call, replay)
    _drift_from_draft(monkeypatch, design_cross_check=False)
    replay = _run(tmp_path, monkeypatch, lead, record_complete=False)
    leads = [c for c in replay.of("lead") if c.task == "t1"]
    assert [c.vendor for c in leads][:1] == ["grok"] and len(leads) == 2
    collaborators = [c for c in replay.of("collaborator") if c.task == "t1"]
    counts = _counts(replay)
    assert collaborators, "the review still ran"
    assert counts == {
        "design_collaboration_applicable: contract True, legacy False": 1,
        "design_review: contract True, legacy False": 1,
    }, counts
    assert not replay.result.completed


# -- distinct values stay distinct ---------------------------------------------------

def test_distinct_live_values_are_each_kept_in_first_order(tmp_path):
    """Sol, 5865650035: dedupe compares the whole note, not the field."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.contract import Required, TaskContract
    from quadratus.outcome import TaskOutcome
    from quadratus.session import SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE", config=SessionConfig())
    session._outcome = TaskOutcome("t1", "implementation")
    session._contract = TaskContract("t1", "implementation", required=Required(design_instruction="self"))
    for live in ("none", "harness", "none", "harness"):
        session._contract_agrees("design_instruction", live)
    assert session._outcome.mismatches == ["design_instruction: contract 'self', legacy 'none'",
                                           "design_instruction: contract 'self', legacy 'harness'"]


def test_a_missing_contract_is_recorded_once_per_value(tmp_path):
    from quadratus.artifacts import ArtifactStore
    from quadratus.outcome import TaskOutcome
    from quadratus.session import SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE", config=SessionConfig())
    session._outcome = TaskOutcome("t1", "implementation")
    for _ in range(3):
        assert session._required("checks", True) is True, "the legacy reading still decides"
    assert session._outcome.mismatches == ["checks: contract missing, legacy True"]


# -- formatting stress case, not an observed replay -------------------------------------

def test_repeated_lines_would_push_a_later_blocker_out_of_the_stop_text():
    """A synthetic formatting stress case (Sol, 5865650035): six identical
    lines are more than the current roster can produce (at most five), but
    they show mechanically how repeats in the 600-character guard text can
    hide a later independent blocker, and that the deduplicated list keeps
    it. The decision itself (blocked or not) is the same either way."""
    from quadratus.outcome import completion_blockers
    from tests.test_outcome import _closed

    line = "design_collaboration_applicable: contract True, legacy False"
    task = _closed()
    task.mismatches = [line] * 6 + ["design_review: contract True, legacy False"]
    task.note("integrity", "evidence identity mismatch on the final render")
    blockers = completion_blockers([task])
    assert "t1.integrity" not in "; ".join(blockers)[:600]

    task.mismatches = list(dict.fromkeys(task.mismatches))
    assert "t1.integrity" in "; ".join(completion_blockers([task]))[:600]
