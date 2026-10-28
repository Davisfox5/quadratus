"""One finding identity (Davis, 2026-10-28), on the real controller below
the vendor CLIs, after controller-continuation-01 ended GoalUnconfirmedAtCap:
t4's capped close recorded none of Opus's eight open ids as debt, the
planner named them in RESOLVES and was sent back as naming findings that
do not exist, and the replacement planning call spent the room the last
task needed.

Pinned here:
- a capped close holds its open review findings in the debt accounting
  (the held entry, the legacy text, the ``hold`` on the record), serial and
  parallel, so the planner sees them and a capped child's rows reach the
  post-merge recheck;
- RESOLVES names review ids beside audit ids; a review finding settles
  only by its own reviewer's recheck after the resolving task closes clean;
  a continuation inherits its predecessor's open findings and the same
  recheck follows; the continuation's own reviewer, or its lead, closes
  nothing; an explicit RESOLVES kept open is the FindingsUnresolved stop,
  an inherited finding kept open stays held debt;
- the records are carried: review_resolutions in result.json and the
  sealed checkpoint.

What stays as it was: the generic cap and failure stops, the budgets, the
independent reviews and the audit (nothing here makes a requirement met).
"""
from pathlib import Path

import pytest

from quadratus import checkpoint
from tests.lifecycle import harness as H
from tests.lifecycle import replies
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_recovery_handoff import (
    CONTINUE,
    GROK_CAP,
    REQS,
    TASK,
    TYPED,
    _batch_script,
    _script,
)
from tests.lifecycle.test_step2_recovery import _plan, _simple

RESOLVE = CONTINUE + "\nRESOLVES: RFT1-1"
RESOLVE_ALONE = (_simple("app.py", "Finish add in app.py: validate both arguments before adding.", "R1")
                 + "\nRESOLVES: RFT1-1")


def _findings(replay):
    return {r["fid"]: r for r in H.result_json(replay)["review_findings"]}


def _held(replay, *, planner=1):
    return replay.of("orchestrator")[planner].prompt


# -- 1. serial: the capped close holds the finding, the planner names it, its reviewer closes it ---

@pytest.mark.requirements_ledger
def test_a_capped_close_holds_its_open_finding_and_resolves_settles_by_the_original_reviewer(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, RESOLVE)),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    assert record["turn_limited_tasks"] == ["t1"], replay.result.error
    finding = _findings(replay)["RFT1-1"]
    assert finding["hold"] == "left open when t1 stopped at its lead turn limit before its recheck"
    # The planner saw the id, its hold and the rule, and RESOLVES was accepted.
    prompt = _held(replay)
    assert "Open review findings, by id: RFT1-1 (by " in prompt and "held: left open when t1 stopped" in prompt
    assert "closes only when the reviewer who raised it rechecks it RESOLVED" in prompt
    assert "do not exist" not in prompt and "TASK SENT BACK" not in replay.of("orchestrator")[2].prompt
    # The continuing lead was handed the finding as written, never told to clear it.
    lead = [c for c in replay.of("lead") if c.task == "t2"][0].prompt
    assert "## Open review findings on the work you continue" in lead and "RFT1-1" in lead
    assert "do not mark any of them resolved yourself" in lead
    # One recheck call by the reviewer who raised it, on the resolving task's final source.
    rechecks = [c for c in replay.of("recheck") if c.task == "t2"]
    vendor = finding["author"].split(":")[0]
    assert len(rechecks) == 1 and rechecks[0].vendor == ("codex" if vendor == "openai" else vendor)
    assert "Task t2 named it in RESOLVES" in rechecks[0].prompt and "Expected ids: RFT1-1" in rechecks[0].prompt
    assert finding["closed"] and finding["history"][-1]["author"] == finding["author"]
    assert finding["history"][-1]["role"] == "recheck" and finding["history"][-1]["state"] == "resolved"
    resolution = record["review_resolutions"][0]
    assert (resolution["task"], resolution["named"], resolution["inherited"]) == ("t2", ["RFT1-1"], [])
    assert resolution["rechecks"]["rechecked"][0]["resolved"] == ["RFT1-1"] and resolution["refused"] is None
    assert not any("RFT1-1" in text for text in record["open_findings"])
    assert "Review findings open by id" not in replay.of("orchestrator")[2].prompt
    assert (replay.project / "app.py").read_text() == TYPED
    sealed = checkpoint.load(Path(replay.result.run_dir))
    assert sealed["state"]["review_resolutions"][0]["named"] == ["RFT1-1"]


@pytest.mark.requirements_ledger
def test_an_explicit_resolves_the_reviewer_keeps_open_is_the_findings_unresolved_stop(tmp_path, monkeypatch):
    """The continuation's own reviewer writes RESOLVED for the inherited id
    and its lead reports it done; neither closes it. The original reviewer
    keeps it open at the settlement recheck, so the explicit attempt stops
    the run as an unresolved RESOLVES does for an audit finding."""
    script = _script(GROK_CAP, orchestrator=_plan(REQS + TASK, RESOLVE))
    review, recheck = script.overrides["collaborator"], script.overrides["recheck"]
    script.overrides["collaborator"] = lambda c, r: ("RFT1-1: RESOLVED fixed by this task\n" + review(c, r)
                                                     if c.task == "t2" else review(c, r))
    script.overrides["recheck"] = lambda c, r: (replies.recheck_reply(c.prompt, default="UNRESOLVED still wrong")
                                                if "named it in RESOLVES" in c.prompt else recheck(c, r))
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    finding = _findings(replay)["RFT1-1"]
    assert not finding["closed"] and finding["history"][-1]["state"] == "unresolved"
    assert finding["history"][-1]["author"] == finding["author"]
    assert (replay.result.error or "").startswith("FindingsUnresolved: task t2 named RESOLVES RFT1-1"), replay.result.error
    record = H.result_json(replay)
    outcome = [t for t in record["workflow"]["tasks"] if t["task_id"] == "t2"][0]
    assert outcome["open_at_close"] == dict(findings=[], review_findings=["RFT1-1"], requirements=[])
    assert record["review_resolutions"][0]["rechecks"]["rechecked"][0]["kept"][0].startswith("RFT1-1: unresolved")
    close = [d for d in record["close_decisions"] if "t2" in d["closed"]][-1]
    assert close["stop"] == "FindingsUnresolved"


BASED = ("BLOCKING: add('x', 1) returns without the error the requirement names | basis: R1 | impact: correctness\n"
         "R1: NOT MET - add('x', 1) returns without the error the requirement names\nREVIEW: COMPLETE")


@pytest.mark.requirements_ledger
def test_an_inherited_finding_kept_open_stays_held_debt_and_names_its_carrier(tmp_path, monkeypatch):
    """A finding with a basis (the live run's EVIDENCE rows had one) is a
    legacy open text as well as a registry record: held debt at the close."""
    script = _script(GROK_CAP, orchestrator=_plan(REQS + TASK, CONTINUE))
    recheck, review = script.overrides["recheck"], script.overrides["collaborator"]
    script.overrides["collaborator"] = lambda c, r: BASED if c.task == "t1" else review(c, r)
    script.overrides["recheck"] = lambda c, r: (replies.recheck_reply(c.prompt, default="UNRESOLVED still wrong")
                                                if "Task t2 continues t1" in c.prompt else recheck(c, r))
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    finding = _findings(replay)["RFT1-1"]
    assert finding["inherited_by"] == ["t2"] and not finding["closed"]
    resolution = record["review_resolutions"][0]
    assert (resolution["named"], resolution["inherited"]) == ([], ["RFT1-1"])
    assert "FindingsUnresolved" not in (replay.result.error or ""), replay.result.error
    close = [d for d in record["close_decisions"] if "t2" in d["closed"]][-1]
    assert close["stop"] is None and close["holds"], "held review debt, not a stop"
    assert any(text.startswith("RFT1-1: UNRESOLVED (left open when t1 stopped") for text in record["open_findings"])
    later = replay.of("orchestrator")[2].prompt
    assert "RFT1-1" in later and "inherited by t2" in later


@pytest.mark.requirements_ledger
def test_a_resolves_without_continues_still_rechecks_and_the_debt_stays_with_the_capped_task(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, RESOLVE_ALONE)),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    assert record["review_resolutions"][0]["named"] == ["RFT1-1"]
    assert _findings(replay)["RFT1-1"]["closed"]
    assert "still owed: t1" in replay.of("orchestrator")[2].prompt, "closing the finding does not settle the cap"


# -- 2. parallel: a capped child's open findings reach the post-merge recheck --------------------

@pytest.mark.requirements_ledger
def test_a_capped_child_s_open_finding_reaches_the_post_merge_recheck(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _batch_script(GROK_CAP), files=FILES, max_tasks=4, record_complete=False)
    record = H.result_json(replay)
    assert sorted(record["turn_limited_tasks"]) == ["t1", "t2"]
    finding = _findings(replay)["RFT2-1"]
    assert finding["hold"].startswith("left open when t2 stopped at its lead turn limit")
    batch = record["parallel_batches"][0]
    rows = {r["task"]: r for r in batch["post_merge_rechecks"]}
    assert "t2" in rows and rows["t2"]["rechecked"][0]["findings"] == 1
    rechecks = [c for c in replay.of("recheck") if c.task == "t2" and "ran in parallel" in c.prompt]
    assert len(rechecks) == 1 and "RFT2-1" in rechecks[0].prompt
    assert "still owed: t1, t2" in replay.of("orchestrator")[1].prompt
