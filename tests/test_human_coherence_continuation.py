"""Self-captured UI work keeps a provable identity across an explicit continuation."""

import copy
import json
from pathlib import Path

import pytest

from quadratus.config import Settings
from quadratus.contract import self_capture_state
from quadratus.outcome import completion_blockers
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.replies import audit_reply, requested_obligations
from tests.lifecycle.test_lifecycle_matrix import DESIGN, Script, _design_files, judging
from tests.test_outcome import _capped, _continuing

GOAL = "Show an Import button on the page."
REQS = "REQUIREMENTS:\nR1: The page shows an Import button.\n"


def _declaration(text, *, capture=None, expect="the Import button is visible"):
    scope = dict(DESIGN, proofs=H.proofs_for(
        "R1", "templates/index.html", evidence="capture", operation="the import page is rendered",
        state="the button has been implemented", expect=expect))
    if capture is not None:
        scope["capture"] = capture
    return "KIND: frontend standard\nSCOPE: " + json.dumps(scope) + "\n" + text + "\nCOVERS: R1"


def _audit(call, replay):
    return audit_reply(call.prompt, {"R1": "MET - templates/index.html"}, {
        row["oid"]: "SUFFICIENT - templates/index.html contains the Import button shown by the approved renders"
        for row in requested_obligations(call.prompt) if row["modality"] == "legacy"})


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["same claim", "different state", "unfinished", "foreign task"])
def test_a_capped_self_capture_task_can_only_finish_through_its_real_continuation(tmp_path, monkeypatch, case):
    monkeypatch.setattr(Session, "_pick_lead", lambda self, spec: "claude:opus")
    successor = _declaration("Finish and capture the Import button.", expect=(
        "the Import button is visible in a settings dialog" if case == "different state"
        else "the Import button is visible"))
    if case != "foreign task":
        successor += "\nCONTINUES: t1"
    plans = iter([REQS + _declaration("Start the Import button."),
                  successor])

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"templates/index.html": "<button>Import</button>\n"})
            return H.claude_cap("The button exists, but its styling and captures remain.")
        H.write(call, {"static/style.css": "button { padding: 8px; }\n"})
        if case == "unfinished":
            return H.claude_cap("The continuation still needs renders and review.")
        H.evidence(Path(call.cwd), "t2", age=H.FRESH)
        return 'Finished and captured the button.\nCHANGED: ["static/style.css"]'

    script = Script(orchestrator=lambda c, r: next(plans, "DONE"), lead=lead,
                    collaborator=lambda c, r: "NO FINDINGS\nR1: MET - templates/index.html\nREVIEW: COMPLETE",
                    auditor=_audit, **{"requirements-review": lambda c, r: "COMPLETE",
                                      "design-review": judging(lambda c, r: "APPROVED", capture=(
                                          "SUFFICIENT - the mobile and desktop renders show the Import button"))})
    replay = H.run(tmp_path, monkeypatch, script, files=_design_files(), max_tasks=3,
                   settings=Settings(backend="cli", lead_max_turns=14), goal=GOAL)
    if case == "foreign task":
        assert [t["task_id"] for t in replay.workflow["tasks"]] == ["t1"]
        assert not replay.result.completed and "not continued: t1" in replay.result.error
        return  # Intake already refuses the attempt to bypass the explicit continuation.
    assert len(replay.workflow["tasks"]) == 2, replay.result.error
    first, second = replay.workflow["tasks"]
    assert first["closed_as"] == "turn_limited" and first["contract"]["required"]["design_evidence"] == "self"
    assert "evidence" not in first["edges"], "the initial cap returned before design verification"
    state = first["contract"]["intended_state"]
    assert state["self_capture"] == 1 and state["task"] == "t1"
    if case != "unfinished":
        assert second["edges"]["evidence"] is True
        assert second["edges"]["delivered"] is True and second["edges"]["reviewer"] is True
    if case == "same claim":
        assert second["continues"] == "t1" and second["contract"]["intended_state"] == state
        assert second["contract"]["inherits"]["intended_state"] == state
        assert replay.result.completed, replay.result.error
    else:
        assert not replay.result.completed, "unfinished or unrelated work cannot discharge the first task"
        assert set(first["unsatisfied"]) >= {"evidence", "delivered", "reviewer"}
        if case in ("different state", "foreign task"):
            assert second["contract"]["intended_state"] != state
        else:
            assert second["closed_as"] == "turn_limited" and "evidence" not in second["edges"]


def _state(task="t1", *, capture=None, predecessor=None):
    return self_capture_state(task, goal=GOAL, requirements=[("R1", "The page shows an Import button.")],
                              scope=dict(DESIGN, capture=capture), predecessor=predecessor)


def _pair():
    first = _capped(design_review=True)
    first.contract.update(required=dict(first.contract["required"], design_evidence="self"),
                          intended_state=_state())
    first.recover("cap")
    successor = _continuing("t2", "t1", design_review=True)
    successor.contract.update(required=dict(successor.contract["required"], design_evidence="self"),
                              intended_state=_state("t2", predecessor=first.contract))
    successor.edges.update(evidence=True, delivered=True, reviewer=True)
    assert completion_blockers([first, successor]) == []
    return first, successor


@pytest.mark.parametrize("failure", ["different state", "unfinished", "waived design", "foreign task", "legacy"])
def test_only_finished_design_of_the_same_explicit_lineage_discharges_it(failure):
    first, successor = _pair()
    if failure == "different state":
        successor.contract["intended_state"] = _state("t2", capture={"path": "/other", "steps": []},
                                                       predecessor=first.contract)
    elif failure == "unfinished":
        successor.note("cap", "the continuation did not finish")
    elif failure == "waived design":
        successor.contract["required"].update(design_evidence="disabled", design_review=False)
    elif failure == "foreign task":
        successor.continues = ""
        successor.contract["intended_state"] = _state("t2")
    else:
        first.contract["intended_state"] = None
    blocked = completion_blockers([first, successor])
    assert all(f"t1.{edge} unsatisfied" in blocked for edge in ("evidence", "delivered", "reviewer"))


def test_identity_survives_json_restore_but_not_another_ui_claim():
    first, successor = _pair()
    restored = json.loads(json.dumps(successor.contract))
    assert _state("t3", predecessor=restored) == first.contract["intended_state"]
    assert _state("t3", predecessor=restored) is not restored["intended_state"]
    changed = copy.deepcopy(DESIGN)
    changed["proofs"] = [{"evidence": "capture", "operation": "open settings", "state": "a dialog is open",
                          "expect": "Save is visible", "requirements": ["R1"]}]
    assert self_capture_state("t3", goal=GOAL, requirements=[("R1", "The page shows an Import button.")],
                              scope=changed, predecessor=restored) != first.contract["intended_state"]


def test_unnumbered_ui_claim_keeps_its_result_and_acceptance_binding():
    state = self_capture_state("t1", goal=GOAL, requirements=[], scope=DESIGN)
    predecessor = dict(required={"design_evidence": "self"}, intended_state=state)
    narrowed = dict(DESIGN, permitted_paths=["static/style.css"], max_lines=10)
    assert self_capture_state("t2", goal=GOAL, requirements=[], scope=narrowed,
                              predecessor=predecessor) == state
    changed = dict(narrowed, acceptance=["the page shows a Save button"])
    assert self_capture_state("t2", goal=GOAL, requirements=[], scope=changed,
                              predecessor=predecessor) != state
