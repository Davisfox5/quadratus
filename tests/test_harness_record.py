"""The planner sees what the harness measured before what a lead wrote
(batch 2 gui-ui-v3 on 5d9f5ff: t6's close-out said the preview failed and
R4 was blocked; the harness had verified both views and Opus had approved;
the planner read only the account and spent t7 recapturing)."""

from quadratus.artifacts import ArtifactStore
from quadratus.ledger import Ledger
from quadratus.memory import TaskSummary
from quadratus.outcome import TaskOutcome
from quadratus.session import Session, SessionConfig

T6_ACCOUNT = ("R4 remained blocked on visual acceptance. Preview startup failed with `Operation not "
              "permitted`, so no desktop/mobile screenshots were produced.")


def _session(tmp_path):
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "", config=SessionConfig())


def test_a_verified_capture_reaches_the_planner_ahead_of_a_contradicting_account(tmp_path):
    session = _session(tmp_path)
    session.design_checks.append({
        "task": "t6", "verified": True, "problem": "",
        "screenshots": ["/p/.quadratus/design-evidence/t6/desktop/page.png",
                        "/p/.quadratus/design-evidence/t6/mobile/page.png", "target: http://127.0.0.1:53073/"],
        "final_review": {"reviewer": "claude:opus", "verdict": "APPROVED"}})
    session._outcome = TaskOutcome("t6", "audit")
    session._outcome.edge("checks", True)
    session.memory.absorb(TaskSummary(task_id="t6", author="openai:gpt-5.6-sol", summary=T6_ACCOUNT,
                                      reasoning="review-only"))
    text = session.memory.render()
    record = text.index("Harness record")
    assert record < text.index("R4 remained blocked")
    block = text[record:text.index("R4 remained blocked")]
    assert "design capture: verified by the harness (desktop, mobile)" in block
    assert "final design review by claude:opus: APPROVED" in block
    assert "required checks: passed" in block


def test_an_unverified_capture_says_so_and_a_task_without_records_has_no_block(tmp_path):
    session = _session(tmp_path)
    session.design_checks.append({"task": "t2", "verified": False, "problem": "the capture exited 2",
                                  "screenshots": []})
    session.memory.absorb(TaskSummary(task_id="t2", author="x", summary="did it", reasoning="r"))
    session.memory.absorb(TaskSummary(task_id="t3", author="x", summary="plain task", reasoning="r"))
    text = session.memory.render()
    assert "design capture: not verified (the capture exited 2)" in text
    assert text.count("Harness record") == 1


def test_the_ledger_entry_keeps_the_record_and_older_callers_still_work():
    ledger = Ledger()
    entry = ledger.append(task_id="t1", author="a", summary="s", reasoning="r")
    assert entry.harness == [] and "Harness record" not in entry.render()
    entry = ledger.append(task_id="t2", author="a", summary="s", reasoning="r", harness=["required checks: failed"])
    assert "Harness record" in entry.render().splitlines()[2]
    assert entry.render().splitlines()[3] == "- required checks: failed"
