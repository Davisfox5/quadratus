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
    NARRATION,
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


# -- 3. a raised or unmerged child keeps its findings under their reviewer ------------------------

FAILING = H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8, error="permission denied: write README.md")


@pytest.mark.requirements_ledger
def test_a_raised_child_s_open_findings_are_held_before_its_failed_record_is_written(tmp_path, monkeypatch):
    """Root's control on 6c1b922: the ProviderError child goes through
    _record_unmerged_failure, not _close_failed, and its RFT2-1 had no hold.
    Nothing is merged or retried and the stop stays."""
    replay = H.run(tmp_path, monkeypatch, _batch_script(FAILING), files=FILES, max_tasks=4, record_complete=False)
    record = H.result_json(replay)
    assert record["failed_tasks"] == ["t2"] and len(replay.of("orchestrator")) == 1
    assert (replay.project / "README.md").read_text() == FILES["README.md"], "nothing was merged"
    finding = _findings(replay)["RFT2-1"]
    assert not finding["closed"] and finding["hold"].startswith("left open when t2 was not merged (")
    sealed = checkpoint.load(Path(replay.result.run_dir))
    state = checkpoint._decode(sealed["state"])
    assert sealed["sealed"] and "RFT2-1" in {e.get("finding_id") for e in state["unresolved_reviews"]}
    # The verdict line carried no basis, so the finding is carried to the
    # audit by id (the note on t2's own outcome), not a legacy open text;
    # with a basis it would be the legacy text as in the capped cases.
    outcome = [t for t in record["workflow"]["tasks"] if t["task_id"] == "t2"][0]
    assert any("RFT2-1 open, carried to the final audit by id" in (f.get("detail") or "") for f in outcome["facts"])
    assert state["failed_records"]["t2"]["cause"] != "scope" and state["failed_records"]["t2"]["unmerged_artifact"]
    assert not replay.result.completed


# -- 4. the sealed terminal state is the end's state --------------------------------------------

@pytest.mark.requirements_ledger
def test_the_sealed_terminal_state_carries_the_end_s_findings_and_outcomes(tmp_path, monkeypatch):
    """Root's control on 6c1b922: a last-slot batch's sealed checkpoint
    kept review_findings=[] and unresolved_reviews=[] from the pre-batch
    quiescent point beside the end's capped lists."""
    replay = H.run(tmp_path, monkeypatch, _batch_script(GROK_CAP), files=FILES, max_tasks=2, record_complete=False)
    record = H.result_json(replay)
    assert sorted(record["turn_limited_tasks"]) == ["t1", "t2"]
    sealed = checkpoint.load(Path(replay.result.run_dir))
    assert sealed["sealed"] and not sealed.get("resumable")
    state = checkpoint._decode(sealed["state"])
    assert "terminal_patch_omitted" not in state
    assert {r["fid"] for r in state["review_findings"]} == {r["fid"] for r in record["review_findings"]} >= {"RFT2-1"}
    assert {e.get("finding_id") for e in state["unresolved_reviews"]} >= {"RFT2-1"}
    assert [o["task_id"] for o in state["task_outcomes"]] == [o["task_id"] for o in record["workflow"]["tasks"]]
    assert sorted(state["turn_limited"]) == ["t1", "t2"] and state["open_findings"] == record["open_findings"]
    assert state["close_decisions"] == record["close_decisions"]


# -- 5. the corrected scenario field has no character count ------------------------------------

@pytest.mark.requirements_ledger
def test_a_long_corrected_field_the_declaration_accepts_is_applied(tmp_path, monkeypatch):
    from tests.lifecycle.test_close_family import CONTRADICTION, PROPOSAL, TAIL
    from tests.lifecycle.test_proof_binding import _acceptance, _at, _proofs, _scope
    from tests.lifecycle.test_proof_binding import _plan as _proof_plan
    from tests.lifecycle.test_workflow_repair import CORRECTION_HEAD, MET, Seen
    from tests.lifecycle.test_workflow_repair import _script as _repair_script
    seen = Seen()
    finding = f"BLOCKING: SCENARIO P1 - {CONTRADICTION} | {PROPOSAL}{TAIL}"
    long_state = "nothing is stored; " + "; ".join(f"ticket {i} is absent" for i in range(40))
    assert len(long_state) > 600

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            assert "there is no character count" in call.prompt
            return f"SCENARIO P1: state: {long_state} | because: the store starts empty for every ticket"
        return _proof_plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, "long"), monkeypatch,
                   _repair_script(seen, orchestrator=orchestrator, collaborator=lambda c, r: finding + MET,
                                  recheck=lambda c, r: "RFT1-1: RESOLVED the corrected scenario holds"),
                   files=FILES, max_tasks=1, record_complete=False)
    record = H.result_json(replay)
    corrections = record["scenario_corrections"]
    assert [c["status"] for c in corrections] == ["corrected"], corrections
    assert corrections[0]["explanation"] == "the store starts empty for every ticket"
    assert dict(_acceptance(replay)[0]["scenario"])["state"] == long_state
