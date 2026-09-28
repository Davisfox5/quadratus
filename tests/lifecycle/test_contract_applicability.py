"""Design evidence applicability comes from the task's own contract (map P3.4,
package 2).

``_check_design`` used to re-read live configuration at the check. The
contract fixes the requirement at dispatch; the live reading is still
computed, and any disagreement is a recorded mismatch, so such a run cannot
count as complete. Whole-controller replays. The configuration drift is a
synthetic write between dispatch and the check: a controller invariant, not
observed behaviour. The ordinary routes are unchanged (see the same-journey
controls at the end and the existing design lifecycle cases).
"""

import dataclasses
import json

from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import (
    DECL_T2,
    DESIGN,
    _design_files,
    _design_script,
    _edits_and_captures,
)

DECL = "KIND: frontend standard\nSCOPE: " + json.dumps(DESIGN) + "\nReview the import page."


def _run(tmp_path, monkeypatch, lead, *, record_complete=True):
    script = _design_script("Renders refreshed.\nCHANGED: []")
    script.overrides["orchestrator"] = lambda call, replay: DECL if len(replay.of("orchestrator")) == 1 else DECL_T2
    script.overrides["lead"] = lead
    script.overrides["revision"] = lambda call, replay: "Nothing to change.\nCHANGED: []"
    return H.run(tmp_path, monkeypatch, script, files=_design_files(), record_complete=record_complete)


def _set_verify(session, value):
    session.config = dataclasses.replace(session.config, design_self_verify=value)


def _drift_at_check(monkeypatch, value):
    """Change the live setting after dispatch, just before the design check."""
    check = Session._check_design

    def drifted(self, spec, *args):
        _set_verify(self, value)
        return check(self, spec, *args)
    monkeypatch.setattr(Session, "_check_design", drifted)


def _t1(replay):
    return next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")


def _evidence(replay):
    return json.loads(replay.artifact_texts("design-evidence")[0])


def test_a_required_check_is_not_dropped_when_the_setting_drifts_off(tmp_path, monkeypatch):
    _drift_at_check(monkeypatch, False)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)
    record = _evidence(replay)
    assert record["verified"] is True, "the check fixed at dispatch still ran"
    assert "disabled by the operator" not in str(record.get("problem"))
    assert "design_evidence: contract 'self', legacy 'disabled'" in json.dumps(_t1(replay)), "mismatch recorded"
    assert not replay.result.completed, "a recorded mismatch cannot count as complete"


def test_a_disabled_requirement_stays_disabled_when_the_setting_drifts_on(tmp_path, monkeypatch):
    run_task = Session.run_task

    def dispatched_off(self, spec):
        _set_verify(self, spec.task_id != "t1")
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", dispatched_off)
    _drift_at_check(monkeypatch, True)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)
    record = _t1(replay)["evidence"]
    assert not replay.of("design-fix") and not replay.of("design-review"), "nothing the contract did not require"
    assert record["verified"] is None and "disabled by the operator" in record["problem"]
    assert "design_evidence: contract 'disabled', legacy 'self'" in json.dumps(_t1(replay))
    assert not replay.result.completed


# -- same-journey controls: no drift, identical routes -------------------------

def test_a_self_captured_design_task_still_verifies(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    assert _evidence(replay)["verified"] is True and not replay.of("design-fix")
    assert _t1(replay)["contract"]["required"]["design_evidence"] == "self"


def test_missing_renders_still_get_the_one_recapture(tmp_path, monkeypatch):
    def lead(call, replay):
        H.write(call, {"templates/index.html": "<button id=import>Import</button>\n"})
        return 'Added the button.\nCHANGED: ["templates/index.html"]'
    replay = _run(tmp_path, monkeypatch, lead)
    assert len(replay.of("design-fix")) == 1
    assert "DesignUnverified" in replay.result.error or not replay.result.completed


def test_a_non_design_task_gets_no_design_check(tmp_path, monkeypatch):
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: DECL_T1), files=FILES)
    assert not replay.artifact_texts("design-evidence") and not replay.of("design-fix")
    assert _t1(replay)["contract"]["required"]["design_evidence"] == "none"


def test_an_operator_disabled_check_is_recorded_not_satisfied(tmp_path, monkeypatch):
    run_task = Session.run_task

    def off(self, spec):
        _set_verify(self, False)
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", off)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    record = _t1(replay)["evidence"]
    assert record["verified"] is None and "disabled by the operator" in record["problem"]
    assert _t1(replay)["contract"]["required"]["design_evidence"] == "disabled"
    assert not replay.of("design-fix")


# -- package 3: design_review applicability ------------------------------------

def _review_drift_at_check(monkeypatch, value):
    check = Session._check_design

    def drifted(self, spec, *args):
        self.config = dataclasses.replace(self.config, design_cross_check=value)
        return check(self, spec, *args)
    monkeypatch.setattr(Session, "_check_design", drifted)


def _reviews(replay):
    return [c for c in replay.of("design-review") if c.task == "t1"]


def test_a_required_review_is_not_dropped_when_the_setting_drifts_off(tmp_path, monkeypatch):
    _review_drift_at_check(monkeypatch, False)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)
    assert len(_reviews(replay)) == 1, "the review fixed at dispatch still ran"
    assert _evidence(replay)["final_review"]["verdict"] == "APPROVED"
    assert "design_review: contract True, legacy False" in json.dumps(_t1(replay))
    assert not replay.result.completed


def test_an_unrequired_review_is_not_added_when_the_setting_drifts_on(tmp_path, monkeypatch):
    run_task = Session.run_task

    def dispatched_off(self, spec):
        self.config = dataclasses.replace(self.config, design_cross_check=spec.task_id != "t1")
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", dispatched_off)
    _review_drift_at_check(monkeypatch, True)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)
    assert not _reviews(replay), "no review the contract did not require"
    assert "no reviewer from another vendor" not in json.dumps(replay.workflow)
    assert "design_review: contract False, legacy True" in json.dumps(_t1(replay))
    assert not replay.result.completed


def test_the_default_design_task_still_gets_one_cross_vendor_review(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    reviews = _reviews(replay)
    lead = next(c for c in replay.of("lead") if c.task == "t1")
    assert len(reviews) == 1 and reviews[0].vendor != lead.vendor, "one review, from another vendor"
    assert _t1(replay)["contract"]["required"]["design_review"] is True
    assert _t1(replay)["edges"]["reviewer"] is True


def test_with_the_review_off_throughout_no_review_is_required(tmp_path, monkeypatch):
    run_task = Session.run_task

    def off(self, spec):
        self.config = dataclasses.replace(self.config, design_cross_check=False)
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", off)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    assert not _reviews(replay)
    assert _t1(replay)["contract"]["required"]["design_review"] is False
    assert _evidence(replay)["verified"] is True
