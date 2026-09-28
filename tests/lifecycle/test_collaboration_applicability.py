"""Design collaboration applicability is fixed at dispatch (map P3.4;
Codex ruling 5864370275).

Three consumers read live ``design_cross_check and is_design_task(spec)``:
the cross-vendor collaborator, the render set collaborators see, and the
review prompt's design lens. They now read the task contract's
``design_collaboration_applicable``, the same predicate fixed at dispatch,
with the live reading recorded beside it. It is not ``design_review``,
which needs evidence: no-project and verification-disabled design tasks
keep all three. The drift is a synthetic write during the task, a
controller invariant.
"""

import dataclasses

from quadratus.artifacts import ArtifactStore
from quadratus.contract import Required, TaskContract
from quadratus.outcome import TaskOutcome, missing_facts
from quadratus.session import Session, SessionConfig, TaskSpec
from tests.lifecycle.test_contract_applicability import _run, _t1
from tests.lifecycle.test_lifecycle_matrix import _edits_and_captures

LENS = "This is design work. Besides correctness, judge the design"


def _lensed(replay, task="t1"):
    return [LENS in c.prompt for c in replay.of("collaborator") if c.task == task]


def _field(replay, value):
    """Asserted last, after the behaviour: the contract carries the value."""
    assert _t1(replay)["contract"]["required"].get("design_collaboration_applicable", "absent") is value


def _drift_at_draft(monkeypatch, value):
    """Change the live setting after dispatch, as the lead's draft starts."""
    draft = Session._draft_with_channels

    def drifted(self, *args, **kw):
        self.config = dataclasses.replace(self.config, design_cross_check=value)
        return draft(self, *args, **kw)
    monkeypatch.setattr(Session, "_draft_with_channels", drifted)


def test_the_design_lens_is_kept_when_the_setting_drifts_off(tmp_path, monkeypatch):
    _drift_at_draft(monkeypatch, False)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures, record_complete=False)
    lensed = _lensed(replay)
    assert lensed and all(lensed), "the lens fixed at dispatch reached the collaborators"
    assert "design_collaboration_applicable: contract True, legacy False" in str(_t1(replay)["mismatches"])
    assert not replay.result.completed


# -- controls -----------------------------------------------------------------------

def test_an_ordinary_design_task_gets_the_lens(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    lensed = _lensed(replay)
    assert lensed and all(lensed)
    _field(replay, True)


def test_a_non_design_task_gets_no_lens(tmp_path, monkeypatch):
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: DECL_T1), files=FILES)
    assert not any(_lensed(replay))
    _field(replay, False)


def test_verification_disabled_keeps_collaboration(tmp_path, monkeypatch):
    run_task = Session.run_task

    def off(self, spec):
        self.config = dataclasses.replace(self.config, design_self_verify=False)
        return run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", off)
    replay = _run(tmp_path, monkeypatch, _edits_and_captures)
    required = _t1(replay)["contract"]["required"]
    assert required["design_evidence"] == "disabled" and required["design_review"] is False
    lensed = _lensed(replay)
    assert lensed and all(lensed)
    _field(replay, True)


def test_a_design_task_with_no_project_keeps_the_cross_vendor_collaborator(tmp_path):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "DONE",
                      config=SessionConfig(project=None))
    spec = TaskSpec("t1", "Add an Import button to the page.", kind="frontend")
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._contract = session._build_contract(spec, outcome)
    assert session._contract.required.design_evidence == "none"
    lead = session.brain_trust[0]
    chosen = session.collaborators_for(spec, lead)
    assert any(p.partition(":")[0] != lead.partition(":")[0] for p in chosen)
    assert outcome.mismatches == []
    assert getattr(session._contract.required, "design_collaboration_applicable", "absent") is True


# -- the record ---------------------------------------------------------------------

def test_the_field_round_trips_through_the_contract_record():
    contract = TaskContract("t1", "implementation", owner="claude:opus",
                            required=Required(design_collaboration_applicable=True))
    assert contract.to_dict()["required"]["design_collaboration_applicable"] is True


def _record(required):
    from tests.test_outcome import _closed
    task = _closed()
    task.contract["required"] = required
    return task


def test_an_older_record_without_the_field_says_so_and_is_not_read_as_false():
    required = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
                    settlement=False, design_instruction="none", security_verdict="none")
    assert missing_facts(_record(required)) == [
        "t1.contract.required.design_collaboration_applicable (absent: recorded before this field existed)"]


def test_a_malformed_value_is_missing():
    required = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
                    settlement=False, design_collaboration_applicable="yes",
                    design_instruction="none", security_verdict="none")
    assert missing_facts(_record(required)) == ["t1.contract.required.design_collaboration_applicable"]
