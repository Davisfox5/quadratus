"""Common recovery lifecycle and improvement loop (Davis on #73, 2026-10-31).

A live run stopped after two of eight task slots because one reviewer reached
its round cap: the no-verdict review became a bare open string nothing could
hold, recheck or settle (6544825528). Root's baseline (6545984480) reproduced
the same three families across nine controls: serial first review, recheck,
batch child, explicit list, final slot, reviewer timeout and reviewer quota.

These are whole-controller replays below the vendor CLIs. Scripted replies
prove routing, accounting and persistence, never model compliance: the
recovery context a prompt carries is not proof a seat reads it. Every case
reads what the production path wrote (``result.json``: ``recoveries``,
``improvement``, ``review_findings``) and the prompts the seats were sent.
"""
import json
import subprocess

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script, covered_met
from tests.lifecycle.test_quality_completion import REQS, T1, _auditor
from tests.lifecycle.test_step2_recovery import _plan

R1 = REQS.replace("R2: README documents add\n", "")
RESOLVE = T1.replace("Implement add in app.py.", "Settle the owed review of t1.") + "\nRESOLVES: RFT1-1"
NARRATION = "NARRATION_CANARY_5521: opened app.py and was about to decide"
CAPPED = Settings(backend="cli", lead_max_turns=8)
OWED = "## The work you owe a judgment on"
CONTEXT = "## Recovery context"


def lead(call, replay):
    H.write(call, {"app.py": FIXED})
    return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'


def clean(call):
    return "No blocking findings.\n" + covered_met(call.prompt) + "REVIEW: COMPLETE"


def reviewer(*, completion, owed=None):
    """claude:opus as t1's collaborator: the first review caps; the one
    completion answers ``completion`` ("clean", "defect" or "cap"); an owed
    settlement answers ``owed`` (default clean)."""
    def collab(call, replay):
        if OWED in call.prompt:
            if owed == "cap":
                return H.claude_cap("Still reading.", num_turns=8)
            return clean(call) if owed in (None, "clean") else owed
        if CONTEXT in call.prompt:
            if completion == "clean":
                return clean(call)
            if completion == "defect":
                return "BLOCKING: add('x', 1) returns a string instead of raising TypeError."
            return H.claude_cap("Still checking.", num_turns=8)
        if call.task == "t1" and len(replay.of("collaborator")) == 1:
            return H.claude_cap(NARRATION, num_turns=8)
        return clean(call)
    return collab


def _run(tmp_path, monkeypatch, *replies, collaborator, max_tasks=3, **kw):
    script = Script(orchestrator=_plan(*replies), lead=lead, collaborator=collaborator,
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"},
                    **kw)
    return H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=max_tasks, record_complete=False,
                 settings=CAPPED)


def _rv(result, rid="RVt1-1"):
    return next(r for r in result["recoveries"] if r["id"] == rid)


def _finding(result, fid):
    return next(f for f in result["review_findings"] if f["fid"] == fid)


def _completions(replay):
    return [c for c in replay.calls if CONTEXT in c.prompt and OWED not in c.prompt]


# -- serial first review: recover once, else own the debt -----------------------------------

@pytest.mark.requirements_ledger
def test_a_capped_review_is_completed_once_by_the_same_seat_and_the_run_finishes(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="clean"))
    result = H.result_json(replay)
    assert replay.result.completed, replay.result.error
    rv = _rv(result)
    assert rv["outcome_class"] == "review_capped" and rv["seat"] == "claude:opus" and rv["role"] == "collaborator"
    assert rv["decision"] == "complete_judgment" and rv["result"].startswith("recovered")
    assert rv["failed_call"]["turn_limited"] is True and rv["failed_call"]["max_turns"] == 8
    # The allowance actually left when it was decided: t1's own slot is spent
    # while it runs, the goal question and the audit are held, and the job's
    # one completion is still unclaimed.
    allowance = rv["allowance"]
    assert allowance["slots_left"] == 2 and allowance["finish_held"] == ["goal", "audit"]
    assert allowance["judgment_completions_left"] == 1
    assert allowance["calls_left_after_finish"] == allowance["calls_left"] - 2
    (attempt,) = rv["attempts"]
    assert attempt["action"] == "complete_judgment" and attempt["outcome"] == "answered"
    assert attempt["call"]["invocation_id"] != rv["failed_call"]["invocation_id"], "a new outer call"
    assert rv["debt"] == [] and not any(f.get("kind") == "unreviewed" for f in result["review_findings"])
    # One completion, the same seat and role; the goal question and the audit still ran.
    (completion,) = _completions(replay)
    assert completion.role == "collaborator" and completion.model == replay.of("collaborator")[0].model
    assert replay.of("auditor"), "the final audit is still asked"
    # The capped call's narration is kept on the record, never in a prompt.
    assert all(NARRATION not in c.prompt for c in replay.calls)
    assert any(NARRATION in text for text in replay.artifact_texts("capped-narration:review:claude:opus"))
    # The proposal is a proposal.
    (proposal,) = result["improvement"]["proposals"]
    assert proposal["family"] == "review_capped|collaborator|claude"
    assert proposal["status"].startswith("proposed: not adopted") and "not confirmed" in proposal["confidence"]
    assert proposal["cost"]["calls"] == 2 and proposal["cost"]["calls_with_unknown_usage"] == 0


@pytest.mark.requirements_ledger
def test_a_completion_that_finds_a_defect_is_an_ordinary_finding_fixed_and_rechecked(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="defect"))
    result = H.result_json(replay)
    assert replay.result.completed, replay.result.error
    finding = _finding(result, "RFT1-1")
    assert finding["kind"] == "defect" and finding["closed"] and finding["state"] == "resolved"
    assert [c.role for c in replay.calls].count("revision") == 1 and replay.of("recheck")
    assert _rv(result)["result"].startswith("recovered")


@pytest.mark.requirements_ledger
def test_a_second_cap_is_owned_debt_settled_by_the_owner_through_resolves(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, collaborator=reviewer(completion="cap"))
    result = H.result_json(replay)
    owed = _finding(result, "RFT1-1")
    assert owed["kind"] == "unreviewed" and owed["author"] == "claude:opus" and owed["task"] == "t1"
    # Settled by its owner's completed review on the current source, in the
    # RESOLVES task, never by another seat or by the planner's word.
    assert owed["closed"] and owed["state"] == "resolved", owed
    (last,) = [d for d in owed["history"] if d["state"] == "resolved"]
    assert last["author"] == "claude:opus" and last["role"] == "recheck" and last["call"]
    (settle,) = [c for c in replay.calls if OWED in c.prompt]
    assert settle.task == "t1" and settle.role == "collaborator" and "claude" in settle.argv[0]
    assert "(RFT1-1)" in settle.prompt and "Files it changed (measured by the harness): app.py" in settle.prompt
    assert NARRATION not in settle.prompt
    rv = _rv(result)
    actions = [(a["action"], a["outcome"]) for a in rv["attempts"]]
    assert actions[0] == ("complete_judgment", "capped: no verdict")
    assert actions[1][0] == "owner_settlement" and actions[1][1].startswith("reviewed")
    assert rv["result"].startswith("recovered: the owner completed the owed review")
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_done_never_completes_over_owed_review_debt(tmp_path, monkeypatch):
    """Negative control: the planner says DONE with the debt open. Nothing
    settles it and nothing completes over it."""
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="cap"))
    result = H.result_json(replay)
    assert not replay.result.completed
    owed = _finding(result, "RFT1-1")
    assert owed["kind"] == "unreviewed" and not owed["closed"]
    assert not [c for c in replay.calls if OWED in c.prompt], "DONE is not a settlement"
    # Grades are not weakened: the auditor wrote R1 MET, and the owed review
    # still holds R1 NOT MET; the audit is not the seat whose judgment is owed.
    assert replay.of("auditor") and "R1: MET" in replay.of("auditor")[-1].reply
    assert result["requirements"]["status"]["R1"].startswith("NOT MET")
    assert "RFT1-1" in result["requirements"]["status"]["R1"]
    # The stop itself is an occurrence of the run's own rule, never retried.
    (stop,) = [r for r in result["recoveries"] if r["kind"] == "run"]
    assert stop["outcome_class"] == "stop_rule" and stop["decision"] == "stop: the run's own stop rule"
    assert stop["result"] == "stopped: " + replay.result.error.split(":", 1)[0]


@pytest.mark.requirements_ledger
def test_an_owner_settlement_that_caps_again_keeps_the_debt(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, collaborator=reviewer(completion="cap", owed="cap"))
    result = H.result_json(replay)
    assert not replay.result.completed
    owed = _finding(result, "RFT1-1")
    assert not owed["closed"]
    attempts = [a for a in _rv(result)["attempts"] if a["action"] == "owner_settlement"]
    assert [a["outcome"] for a in attempts if not a["outcome"].startswith("not attempted")] == ["capped: no verdict"]
    assert len([c for c in replay.calls if OWED in c.prompt]) == 1


# -- context: harness facts only ---------------------------------------------------------------

def _trace_for_first_review(seen):
    def reader(event):
        seen.append(event)
        if getattr(event, "role", "") != "collaborator" or len(seen) > 1:
            return None
        cwd = "/scratch/copy-7"
        return dict(cwd=cwd, tool_calls=[
            dict(name="Read", target=f"{cwd}/app.py", outcome="success"),
            dict(name="Read", target="tests/test_app.py", outcome="error"),
            dict(name="Read", target="/etc/passwd", outcome="success"),
            dict(name="Read", target="../outside.txt", outcome="success"),
            dict(name="Grep", target="PATTERN_CANARY_7731|secret", outcome="success"),
            dict(name="Bash", target="cat ~/.ssh/id_rsa # COMMAND_CANARY_4410", outcome="error"),
            dict(name="Read", target="PATH_CANARY|*.py", outcome="success"),
        ], reasoning=["REASONING_CANARY_9012"], files_read=["PATTERN_CANARY_7731|secret"],
            integrity=dict(lines=9, invalid=0, missing_files=0, truncated=False, complete=True))
    return reader


@pytest.mark.requirements_ledger
def test_the_completion_carries_observed_reads_through_the_allowlist_and_nothing_else(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("quadratus.project_run._live_call_trace", _trace_for_first_review(seen))
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="clean"))
    result = H.result_json(replay)
    obs = _rv(result)["observations"]
    assert obs["status"] == "observed" and obs["files_read"] == ["app.py"]
    assert obs["searches"] == 1 and obs["commands"] == 1 and obs["command_outcomes"] == {"error": 1}
    (completion,) = _completions(replay)
    context = completion.prompt[completion.prompt.index(CONTEXT):completion.prompt.index("It lacked:")]
    assert "project files a file-reading tool opened: app.py" in context
    assert "Opening a file does not show it was reviewed" in context and "not a list of what you left unread" in context
    dumped = json.dumps(result)
    for canary in ("PATTERN_CANARY_7731", "COMMAND_CANARY_4410", "REASONING_CANARY_9012", "PATH_CANARY",
                   "/etc/passwd", "outside.txt", "id_rsa", NARRATION):
        assert canary not in completion.prompt, canary
        assert canary not in dumped, canary
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_missing_context_is_said_and_nothing_is_assumed(tmp_path, monkeypatch):
    monkeypatch.setattr("quadratus.project_run._live_call_trace", lambda event: None)
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="clean"))
    obs = _rv(H.result_json(replay))["observations"]
    assert obs["status"] == "unavailable" and obs["reason"]
    (completion,) = _completions(replay)
    assert "could not observe that call's tool use" in completion.prompt
    assert "nothing is assumed about what it examined" in completion.prompt


@pytest.mark.requirements_ledger
def test_a_source_change_during_the_completion_voids_it_and_the_debt_is_held(tmp_path, monkeypatch):
    def collab(call, replay):
        if CONTEXT in call.prompt:
            (replay.project / "README.md").write_text("# app\n\nchanged during the completion\n")
            return clean(call)
        if len(replay.of("collaborator")) == 1:
            return H.claude_cap(NARRATION, num_turns=8)
        return clean(call)

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=collab, max_tasks=1)
    result = H.result_json(replay)
    rv = _rv(result)
    (attempt,) = rv["attempts"]
    assert attempt["outcome"].startswith("void: the bound identity changed during the completion")
    assert not rv["result"].startswith("recovered")
    owed = _finding(result, "RFT1-1")
    assert owed["kind"] == "unreviewed" and not owed["closed"]
    assert not replay.result.completed
    # The void attempt names the source the completion was bound to, never
    # the source it found afterwards (root's review of a7f9c6c, L2).
    (completion,) = result["judgment_completions"]
    assert attempt["source"] == completion["binding"]["source"]
    assert attempt["source"] != owed["source_digest"], "the debt was owed on the changed source; the void was not"


@pytest.mark.requirements_ledger
def test_a_later_settlement_labels_earlier_observations_as_history(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("quadratus.project_run._live_call_trace", _trace_for_first_review(seen))

    def resolving_lead(call, replay):
        if call.task == "t2":
            H.write(call, {"app.py": FIXED + "\n\ndef sub(a, b):\n    return a - b\n"})
            return 'Settled.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        return lead(call, replay)

    script = Script(orchestrator=_plan(R1 + T1, RESOLVE), lead=resolving_lead,
                    collaborator=reviewer(completion="cap"), auditor=_auditor({"R1": "MET - app.py"}),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False, settings=CAPPED)
    (settle,) = [c for c in replay.calls if OWED in c.prompt]
    assert "is history about an earlier source" in settle.prompt and "approves nothing" in settle.prompt
    assert replay.result.completed, replay.result.error


# -- owner and duplicate guards -------------------------------------------------------------

def test_only_the_owner_is_the_designated_judge_of_owed_debt(tmp_path):
    from quadratus import delivery
    finding = delivery.Finding("RFT1-1", "claude:opus", "collaborator", "UNREVIEWED: the review of t1 ...",
                               delivery.FindingBasis("none"), "unstated", "src", "t1", None, (), "unreviewed")
    assert delivery.designated_judge(finding, "openai:sol", "recheck"), "another seat is refused"
    assert not delivery.designated_judge(finding, "claude:opus", "recheck")
    assert not finding.holds_effects and not finding.audit_carried, "owed debt is not a defect"


# -- protective stops keep their evidence ------------------------------------------------------

@pytest.mark.requirements_ledger
def test_a_reviewer_timeout_stops_protectively_with_a_reconcile_record_and_no_retry(tmp_path, monkeypatch):
    def collab(call, replay):
        raise subprocess.TimeoutExpired(call.argv, 1)

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=collab, max_tasks=2)
    result = H.result_json(replay)
    assert not replay.result.completed and "unknown_usage" in replay.result.error
    assert len(replay.of("collaborator")) == 1, "no retry, no completion of an uncertain call"
    (rv,) = [r for r in result["recoveries"] if r["kind"] == "run"]
    assert rv["outcome_class"] == "unknown_usage" and rv["decision"].startswith("reconcile")
    assert rv["result"] == "stopped: RunBudgetExceeded"
    call = rv["failed_call"]
    assert call["outcome"] == "TimeoutError" and call["role"] == "collaborator"
    assert call["tokens"] is None and call["input_tokens"] is None, "unknown usage stays unknown, never zero"
    assert result["improvement"]["cost"]["calls_with_unknown_usage"] == 1
    assert result["improvement"]["proposals"][0]["status"].startswith("proposed")


@pytest.mark.requirements_ledger
def test_a_reviewer_quota_stops_protectively_with_no_fallback_vendor(tmp_path, monkeypatch):
    def collab(call, replay):
        return H.claude_error("You've reached your usage limit for this window.")

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=collab, max_tasks=2)
    result = H.result_json(replay)
    assert not replay.result.completed
    assert len(replay.of("collaborator")) == 1
    assert {c.vendor for c in replay.of("collaborator")} == {"claude"}, "no silent fallback vendor"
    (rv,) = [r for r in result["recoveries"] if r["kind"] == "run"]
    assert rv["outcome_class"] == "provider_quota" and rv["decision"].startswith("stop")


# -- capped recheck: the finding stays under its own id, settled by its author -------------

FINDING = "BLOCKING: add('x', 1) returns a string instead of raising TypeError."


def _recheck_reviewer(settle="RESOLVED"):
    def collab(call, replay):
        return FINDING if call.task == "t1" and len(replay.of("collaborator")) == 1 else clean(call)

    def recheck(call, replay):
        if call.task == "t1" and len(replay.of("recheck")) <= 2:
            return H.claude_cap("Re-reading the revision.", num_turns=8)
        return f"RFT1-1: {settle} - add rejects strings now"
    return collab, recheck


@pytest.mark.requirements_ledger
def test_a_capped_recheck_holds_the_finding_under_its_id_and_its_author_settles_it(tmp_path, monkeypatch):
    collab, recheck = _recheck_reviewer()
    script = Script(orchestrator=_plan(R1 + T1, RESOLVE), lead=lead, collaborator=collab, recheck=recheck,
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    rv = _rv(result)
    assert rv["outcome_class"] == "recheck_capped" and rv["debt"] == ["RFT1-1"]
    # The completion capped too; the RESOLVES task's recheck by the author
    # is the owner settlement, recorded on the same occurrence.
    assert [a["outcome"] for a in rv["attempts"]] == ["capped: no verdict", "recheck: resolved"]
    assert rv["attempts"][1]["action"] == "owner_settlement"
    finding = _finding(result, "RFT1-1")
    assert finding["kind"] == "defect" and finding["author"] == "claude:opus"
    assert finding["closed"] and finding["state"] == "resolved"
    judged = [d for d in finding["history"] if d["state"] == "resolved"]
    assert judged and judged[-1]["author"] == "claude:opus"
    assert rv["result"].startswith("recovered: its owner closed the owned debt (RFT1-1 resolved by claude:opus (recheck)")
    assert not [c for c in replay.calls if "blocking findings remain unresolved" in c.prompt.lower()], \
        "a capped recheck buys no fix round"
    # No bare string survives: every open text the run held was an id.
    assert all(not f.startswith("Task t1:") for f in result.get("open_findings") or [])
    assert replay.result.completed, replay.result.error


# -- explicit list: one owner settlement at the list's end -----------------------------------

STANDARD = ('KIND: backend standard\nSCOPE: {"permitted_paths": ["app.py"], "intended_result": "add works", '
            '"acceptance": ["add(1, 2) == 3"], "max_lines": 20}\nImplement add in app.py.')


@pytest.mark.parametrize("owed", ["clean", "cap"])
def test_an_explicit_list_settles_owed_review_once_at_its_end(tmp_path, monkeypatch, owed):
    script = Script(lead=lead, collaborator=reviewer(completion="cap", owed=owed))
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, tasks=[STANDARD], record_complete=False,
                   settings=CAPPED)
    result = H.result_json(replay)
    assert not replay.of("orchestrator"), "an explicit list makes no planner call"
    owed_record = next(f for f in result["review_findings"] if f.get("kind") == "unreviewed")
    settles = [c for c in replay.calls if OWED in c.prompt]
    assert len(settles) == 1 and "No task is left to name a settlement (the list's end)" in settles[0].prompt
    if owed == "clean":
        assert owed_record["closed"] and replay.result.completed, replay.result.error
    else:
        assert not owed_record["closed"] and not replay.result.completed


# -- the final slot: the finish is kept, the review is owed, the goal and audit still run ----

@pytest.mark.requirements_ledger
def test_with_no_budget_for_recovery_the_review_is_owed_and_the_finish_still_runs(tmp_path, monkeypatch):
    from tests.lifecycle.test_finish_capacity import _limits
    script = Script(orchestrator=_plan(R1 + T1), lead=lead, collaborator=reviewer(completion="clean"),
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False,
                   settings=CAPPED, limits=_limits(6))
    result = H.result_json(replay)
    roles = [c.role for c in replay.calls]
    assert roles[-2:] == ["orchestrator", "auditor"], roles
    assert not _completions(replay) and not [c for c in replay.calls if OWED in c.prompt]
    owed = _finding(result, "RFT1-1")
    assert owed["kind"] == "unreviewed" and not owed["closed"]
    rv = _rv(result)
    assert any(a["outcome"] == "refused to keep the finish" for a in rv["attempts"]), rv["attempts"]
    budget = json.loads((replay.result.run_dir / "budget.json").read_text())
    assert budget["finish_protection"]["started"] == ["goal", "audit"]
    assert budget["reserved_attempts"] <= 6
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_at_the_cap_the_owner_settles_once_before_the_goal_question_and_the_audit(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="cap"), max_tasks=1)
    result = H.result_json(replay)
    roles = [c.role for c in replay.calls]
    settles = [i for i, c in enumerate(replay.calls) if OWED in c.prompt]
    assert len(settles) == 1 and "(the cap)" in replay.calls[settles[0]].prompt
    assert roles[settles[0] + 1:] == ["orchestrator", "auditor"], roles
    assert _finding(result, "RFT1-1")["closed"]
    assert replay.result.completed, replay.result.error


# -- a batch child: its pending review comes back to the parent and is settled after the merge

from tests.lifecycle.test_finish_capacity import BATCH  # noqa: E402


@pytest.mark.requirements_ledger
def test_a_batch_childs_capped_review_is_owed_and_settled_by_its_owner_after_the_merge(tmp_path, monkeypatch):
    from tests.lifecycle.test_quality_completion import README

    def lead2(call, replay):
        if call.task == "t2":
            H.write(call, {"README.md": README})
            return 'Documented add.\nCHANGED: ["README.md"]'
        return lead(call, replay)

    def collab(call, replay):
        if OWED in call.prompt:
            return clean(call)
        if call.task == "t1":
            return H.claude_cap(NARRATION, num_turns=8)
        return clean(call)

    def auditor(call, replay):
        return _auditor({"R1": "MET - app.py", "R2": "MET - README.md"})(call, replay)

    script = Script(orchestrator=_plan(BATCH), lead=lead2, collaborator=collab, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    owed = next(f for f in result["review_findings"] if f.get("kind") == "unreviewed")
    assert owed["task"] == "t1" and owed["author"].startswith("claude")
    settles = [c for c in replay.calls if OWED in c.prompt]
    assert len(settles) == 1 and settles[0].task == "t1"
    assert owed["closed"], owed
    # The child's occurrence came back with it and names the settlement.
    rv = next(r for r in result["recoveries"] if r["task"] == "t1" and r["outcome_class"] == "review_capped")
    assert any(a["action"] == "owner_settlement" and a["outcome"].startswith("reviewed") for a in rv["attempts"])
    assert len({r["id"] for r in result["recoveries"]}) == len(result["recoveries"]), "ids unique after merge"


# -- resume and export parity: stable ids, nothing reset, nothing duplicated ----------------

@pytest.mark.requirements_ledger
def test_a_resumed_job_keeps_its_occurrences_and_settles_the_carried_debt(tmp_path, monkeypatch):
    from pathlib import Path

    from quadratus import checkpoint
    from quadratus.project_run import run_project
    from tests.lifecycle.test_step2_recovery import _interrupt_after

    _interrupt_after(monkeypatch, 1)
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, collaborator=reviewer(completion="cap"))
    assert "KeyboardInterrupt" in replay.result.error
    first = Path(replay.result.run_dir)
    from quadratus import jobstore
    carried = checkpoint.load(first)["state"]["recoveries"]
    exported = json.loads((first / "result.json").read_text())["recoveries"]
    terminal = checkpoint._decode(jobstore.read_terminal(first)["recoveries"])
    assert [r["id"] for r in carried] == ["RVt1-1"] and carried[0]["debt"] == ["RFT1-1"]
    # The checkpoint is the last quiescent point; the interruption's own
    # occurrence came after it and is kept in the committed terminal record,
    # which the export matches.
    assert terminal == exported and [r["id"] for r in exported] == ["RVt1-1", "RVrun-1"]
    assert exported[1]["outcome_class"] == "cancelled" and exported[1]["decision"] == "stop: protective"
    before = carried[0]["attempts"]

    settings = Settings(backend="cli", lead_max_turns=8)
    settings.backend_overrides = {}
    settings.openai_api_key = settings.anthropic_api_key = settings.xai_api_key = None
    second = Path(run_project("Build the feature.", replay.project, settings, allow_writes=True, check=H.GATE,
                              max_tasks=3, resume_from=first.name,
                              run_limits=H.RunLimits(max_calls=120, max_reported_tokens=6_000_000,
                                                     wall_seconds=600, max_concurrent_workers=2)).run_dir)
    result = json.loads((second / "result.json").read_text())
    ids = [r["id"] for r in result["recoveries"]]
    assert ids.count("RVt1-1") == 1 and ids.count("RVrun-1") == 1, ids
    assert next(r for r in result["recoveries"] if r["id"] == "RVrun-1")["carried_from"] == first.name
    rv = _rv(result)
    assert rv["attempts"][:len(before)] == before, "earlier attempts are kept as they were"
    assert rv["attempts"][-1]["action"] == "owner_settlement" and rv["attempts"][-1]["outcome"].startswith("reviewed")
    assert _finding(result, "RFT1-1")["closed"]
    (proposal,) = [p for p in result["improvement"]["proposals"] if p["family"].startswith("review_capped")]
    assert proposal["occurrences"] == ["RVt1-1"], "a resumed job does not count its occurrence twice"
    assert result["completed"], result.get("error")
    # The job's one judgment completion stays spent across the resume.
    budget = json.loads((second / "budget.json").read_text())
    assert budget["judgment_completions"] == 1


# -- mixed debt: an inherited defect and an owed review settle in one task -----------------

from tests.lifecycle.test_quality_completion import README as _README  # noqa: E402
from tests.lifecycle.test_step2_recovery import _simple  # noqa: E402

R12 = REQS
T2_DOC = _simple("README.md", "Document add in README.md.", "R2")
T3_MIXED = ("KIND: backend simple\nSCOPE: " + json.dumps(
    {"permitted_paths": ["README.md", "app.py"], "intended_result": "README.md shows an example of add",
     "acceptance": ["README.md meets the task"], "max_lines": 20,
     "proofs": H.proofs_for("R1", "tests/test_app.py")
     + [dict(H.proofs_for("R2", "README.md", evidence="reading")[0], id="P2")]})
    + "\nFinish the README: add an example of add.\nCOVERS: R1, R2\nCONTINUES: t2\nRESOLVES: RFT1-1")
EXAMPLE = _README + "\nExample: `add(1, 2)` returns `3`.\n"


@pytest.mark.requirements_ledger
def test_inherited_defect_and_owed_review_each_settle_by_their_own_judge(tmp_path, monkeypatch):
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    def lead_(call, replay):
        if call.task == "t1":
            return lead(call, replay)
        H.write(call, {"README.md": EXAMPLE if call.task == "t3" else _README})
        return ('Documented add.\n' + ('PROVES: P1 - tests/test_app.py::test_add\n' if call.task == "t3" else "")
                + 'CHANGED: ["README.md"]')

    def revision(call, replay):
        if call.task == "t2":
            return (H.grok_ok("Adding the example next", stop="max_turn_requests", num_turns=8)
                    if call.vendor == "grok" else H.claude_cap("Adding the example next", num_turns=8))
        return "Nothing to change after review.\nCHANGED: []"

    def collab(call, replay):
        if OWED in call.prompt:
            return clean(call)
        if call.task == "t1":
            return H.claude_cap(NARRATION, num_turns=8)
        if call.task == "t2":
            return "BLOCKING: README.md gives no example of add\n" + covered_met(call.prompt)
        return clean(call)

    def recheck(call, replay):
        fid = "RFT2-1"
        return f"{fid}: RESOLVED the README now shows add(1, 2)" if call.task == "t3" else f"{fid}: UNRESOLVED"

    def auditor(call, replay):
        return _auditor({"R1": "MET - app.py", "R2": "MET - README.md"})(call, replay)

    script = Script(orchestrator=_plan(R12 + T1, T2_DOC, T3_MIXED), lead=lead_, revision=revision,
                    collaborator=collab, recheck=recheck, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=4, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    owed, defect = _finding(result, "RFT1-1"), _finding(result, "RFT2-1")
    assert owed["kind"] == "unreviewed" and defect["kind"] == "defect"
    assert owed["closed"] and defect["closed"], (owed, defect)
    owed_judge = [d for d in owed["history"] if d["state"] == "resolved"][-1]
    defect_judge = [d for d in defect["history"] if d["state"] == "resolved"][-1]
    assert owed_judge["author"] == owed["author"] and defect_judge["author"] == defect["author"]
    settles = [c for c in replay.calls if OWED in c.prompt]
    assert len(settles) == 1 and settles[0].task == "t1"
    assert [c.task for c in replay.of("recheck")][-1] == "t3", "the inherited defect is rechecked by its author"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_the_owner_is_never_asked_twice_about_the_same_source(tmp_path, monkeypatch):
    """A settlement that gave no verdict is not repeated on unchanged source:
    a second RESOLVES whose task changes nothing gets no owner call."""
    def collab(call, replay):
        if OWED in call.prompt:
            return "I looked at app.py and it seems reasonable."
        if call.task == "t1" or CONTEXT in call.prompt:
            return H.claude_cap(NARRATION, num_turns=8)
        return clean(call)

    again = RESOLVE.replace("Settle the owed review of t1.", "Ask again for the owed review of t1.")
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, again, collaborator=collab, max_tasks=4)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2", "t3"], "both settling tasks ran"
    result = H.result_json(replay)
    assert len([c for c in replay.calls if OWED in c.prompt]) == 1
    attempts = [a for a in _rv(result)["attempts"] if a["action"] == "owner_settlement"]
    assert attempts[0]["outcome"] == "no valid verdict"
    assert any(a["outcome"] == "not attempted: the owner already judged this exact source without a verdict"
               for a in attempts[1:]), attempts
    assert not _finding(result, "RFT1-1")["closed"] and not replay.result.completed


# -- the owner's settlement is a review: its findings and NOT MET lines are held -------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("reply", [
    "BLOCKING: add('x', 1) returns a string instead of raising TypeError.",
    "R1: NOT MET - add('x', 1) returns a string instead of raising TypeError",
], ids=["blocking", "not-met-without-complete-line"])
def test_an_owner_settlement_that_finds_a_defect_holds_it_and_the_run_does_not_complete(tmp_path, monkeypatch,
                                                                                        reply):
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, collaborator=reviewer(completion="cap", owed=reply))
    result = H.result_json(replay)
    owed = _finding(result, "RFT1-1")
    assert owed["closed"], "the owner completed its review, so the debt it owed is settled"
    (new,) = [f for f in result["review_findings"] if f["fid"] != "RFT1-1"]
    assert new["kind"] == "defect" and new["author"] == "claude:opus" and new["task"] == "t1" and not new["closed"]
    assert ("NOT MET" in new["claim"]) is reply.startswith("R1:")
    assert any(new["fid"] in f for f in result.get("open_findings") or []) or not replay.result.completed
    assert not replay.result.completed
    rv = _rv(result)
    assert rv["attempts"][-1]["outcome"] == f"reviewed: new {new['fid']}"


# -- a source change during the owner's settlement voids it; the new source is not "judged" ---

@pytest.mark.requirements_ledger
def test_a_source_change_during_the_owner_settlement_voids_it_and_the_next_source_is_still_judged(
        tmp_path, monkeypatch):
    settles = []

    def collab(call, replay):
        if OWED in call.prompt:
            settles.append(call)
            if len(settles) == 1:
                (replay.project / "README.md").write_text("# app\n\nchanged during the owner's review\n")
            return clean(call)
        if call.task == "t1" or CONTEXT in call.prompt:
            return H.claude_cap(NARRATION, num_turns=8)
        return clean(call)

    again = RESOLVE.replace("Settle the owed review of t1.", "Ask again for the owed review of t1.")
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, again, collaborator=collab, max_tasks=4)
    result = H.result_json(replay)
    attempts = [a for a in _rv(result)["attempts"] if a["action"] == "owner_settlement"]
    assert attempts[0]["outcome"].startswith("void: the bound identity changed during the review (")
    assert "source" in attempts[0]["outcome"]
    assert attempts[1]["outcome"] == "reviewed: no findings", attempts
    assert attempts[0]["source"] != attempts[1]["source"], "the void judged the old source, never the new one"
    assert len(settles) == 2 and _finding(result, "RFT1-1")["closed"]


# -- at most two owner attempts, counted across every source ---------------------------------

@pytest.mark.requirements_ledger
def test_the_owner_is_asked_at_most_twice_and_a_third_resolves_is_refused(tmp_path, monkeypatch):
    def collab(call, replay):
        if OWED in call.prompt:
            return "I looked at app.py and it seems reasonable."
        if call.task == "t1" or CONTEXT in call.prompt:
            return H.claude_cap(NARRATION, num_turns=8)
        return clean(call)

    def lead_(call, replay):
        H.write(call, {"app.py": FIXED + f"\n# revision {call.task}\n"})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    tasks = [RESOLVE.replace("Settle the owed review of t1.", f"Settlement attempt {n} for t1.") for n in (1, 2, 3)]
    script = Script(orchestrator=_plan(R1 + T1, *tasks), lead=lead_, collaborator=collab,
                    auditor=_auditor({"R1": "MET - app.py"}), **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=5, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    assert len([c for c in replay.calls if OWED in c.prompt]) == 2
    made = [a for a in _rv(result)["attempts"] if a["action"] == "owner_settlement"
            and not a["outcome"].startswith("not attempted")]
    assert [a["outcome"] for a in made] == ["no valid verdict", "no valid verdict"]
    assert len({a["source"] for a in made}) == 2
    assert [c.task for c in replay.of("lead")] == ["t1", "t2", "t3"], "the third RESOLVES is refused at dispatch"
    assert not _finding(result, "RFT1-1")["closed"] and not replay.result.completed


# -- a recheck the author owes is never the audit's to settle ------------------------------

@pytest.mark.requirements_ledger
def test_a_recheck_with_no_verdict_is_not_handed_to_the_audit_and_its_author_is_asked_at_the_cap(
        tmp_path, monkeypatch):
    def collab(call, replay):
        return "BLOCKING: the docstring contradicts the goal" if call.task == "t1" and not replay.of("recheck") \
            else clean(call)

    def recheck(call, replay):
        return "I believe it is fine now."

    def auditor(call, replay):
        return _auditor({"R1": "MET - app.py"})(call, replay) + "\nRFT1-1: INVALIDATED tests/test_app.py::test_add passes"

    script = Script(orchestrator=_plan(R1 + T1), lead=lead, collaborator=collab, recheck=recheck, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    finding = _finding(result, "RFT1-1")
    assert finding["kind"] == "defect" and finding["owed_recheck"] and not finding["audit_carried"]
    assert not finding["closed"] and not any(d["role"] == "audit" for d in finding["history"])
    at_cap = [c for c in replay.of("recheck") if "No task is left to name a settlement (the cap)" in c.prompt]
    assert len(at_cap) == 1 and at_cap[0].vendor == replay.of("recheck")[0].vendor, "its author, once"
    assert not replay.result.completed


# -- one unfinished review is one owed record, however many ways it is unfinished ----------

@pytest.mark.requirements_ledger
def test_a_review_unfinished_two_ways_is_one_owed_record(tmp_path, monkeypatch):
    def collab(call, replay):
        return "I'll read app.py and the tests first." if call.task == "t1" else clean(call)

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=collab, max_tasks=1)
    result = H.result_json(replay)
    owed = [f for f in result["review_findings"] if f.get("kind") == "unreviewed"]
    assert len(owed) == 1 and owed[0]["author"] == "claude:opus"
    assert len([f for f in result.get("open_findings") or [] if owed[0]["fid"] in f]) <= 1


# -- a capped review of a capped final-batch child: recovered, owed, settled, cap lifted -----

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("settles", [True, False], ids=["clean-settlement", "no-verdict"])
def test_a_capped_final_batch_review_is_owed_and_a_clean_settlement_lifts_the_cap(tmp_path, monkeypatch, settles):
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    from tests.lifecycle.test_finish_capacity import BATCH
    from tests.lifecycle.test_quality_completion import README
    from tests.lifecycle.test_step2_recovery import _capped_lead

    def lead_(call, replay):
        if call.task == "t1":
            return _capped_lead(call, replay, {"app.py": FIXED})
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collab(call, replay):
        if call.task != "t1":
            return clean(call)
        if OWED in call.prompt:
            return clean(call) if settles else "Still looking at app.py."
        return H.claude_cap(NARRATION, num_turns=8) if call.vendor == "claude" else \
            H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8)

    def auditor(call, replay):
        return _auditor({"R1": "MET - app.py", "R2": "MET - README.md"})(call, replay)

    script = Script(orchestrator=_plan(BATCH), lead=lead_, collaborator=collab, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    review = result["parallel_batches"][0]["capped_final_reviews"][0]
    assert review["verdict"] == "held: review capped", review
    owed = next(f for f in result["review_findings"] if f.get("kind") == "unreviewed")
    assert owed["task"] == "t1" and owed["final_cap"]
    rv = next(r for r in result["recoveries"] if r["task"] == "t1" and r["outcome_class"] == "review_capped")
    assert rv["attempts"][0]["action"] == "complete_judgment"
    settle = [c for c in replay.calls if OWED in c.prompt]
    assert len(settle) == 1 and "(the cap)" in settle[0].prompt
    t1 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    lifted = any(f["kind"] == "cap" and "owed review, completed at its settlement" in f["detail"] for f in t1["facts"])
    assert lifted is settles and owed["closed"] is settles
    # Lifted, the cap no longer stops the job on t1; the requirements are
    # still the audit's (t1's capped lead bound no proof, so R1's evidence
    # is missing and the run is honestly incomplete either way).
    capped_stop = "capped or failed task(s) t1 not continued" in (replay.result.error or "")
    assert capped_stop is (not settles), replay.result.error
    assert not replay.result.completed


# -- root's review of a7f9c6c (6547961862): authority and learning boundaries ----------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("settlement", ["capped", "malformed"])
def test_a_post_merge_recheck_without_a_verdict_stays_its_authors_and_no_auditor_closes_it(
        tmp_path, monkeypatch, settlement):
    """R1: a parallel child's no-basis BLOCKING finding, its post-merge
    recheck by its author capped or verdict-less, and an auditor of another
    seat writing INVALIDATED for it. The finding is the author's owed
    recheck, so the audit is not handed it and cannot close it, and the run
    does not complete."""
    from tests.lifecycle.test_finish_capacity import BATCH
    from tests.lifecycle.test_quality_completion import README

    def lead2(call, replay):
        if call.task == "t2":
            H.write(call, {"README.md": README})
            return 'Documented add.\nCHANGED: ["README.md"]'
        return lead(call, replay)

    def collab(call, replay):
        if call.task == "t1" and not replay.of("recheck"):
            return "BLOCKING: the docstring contradicts the goal\n" + covered_met(call.prompt)
        return clean(call)

    merged = "while it ran in parallel with sibling tasks"
    post_merge = []

    def recheck(call, replay):
        if merged not in call.prompt and "No task is left to name a settlement" not in call.prompt:
            # Inside the child: the author keeps its finding.
            return "RFT1-1: UNRESOLVED the docstring still contradicts it"
        # After the merge and at the cap's settlement: never a verdict.
        post_merge.append(call)
        if settlement == "capped":
            return H.claude_cap("Re-reading the merged source.", num_turns=8)
        return "RFT1-1: I believe it is probably fine now."

    def auditor(call, replay):
        return (_auditor({"R1": "MET - app.py", "R2": "MET - README.md"})(call, replay)
                + "\nRFT1-1: INVALIDATED tests/test_app.py::test_add passes")

    script = Script(orchestrator=_plan(BATCH), lead=lead2, collaborator=collab, recheck=recheck, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    assert post_merge, "the author's post-merge recheck ran"
    finding = _finding(result, "RFT1-1")
    assert finding["kind"] == "defect" and finding["author"].startswith("claude")
    assert finding["owed_recheck"] and not finding["audit_carried"], finding
    assert not finding["closed"] and not any(d["role"] == "audit" and d["state"] != "unjudged"
                                             for d in finding["history"])
    assert "RFT1-1" not in (result["requirements"]["audits"][-1].get("findings_carried") or [])
    assert not replay.result.completed
    rv = next(r for r in result["recoveries"] if "RFT1-1" in (r.get("debt") or []))
    assert not rv["result"].startswith("recovered"), rv["result"]


def _drift_during(monkeypatch, marker, drift):
    from quadratus.session import Session
    original = Session._invoke_model

    def invoke(self, key, prompt, **kwargs):
        reply = original(self, key, prompt, **kwargs)
        if marker in prompt:
            drift(self)
        return reply
    monkeypatch.setattr(Session, "_invoke_model", invoke)


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("drift", ["check-settings", "requirement-text"])
def test_an_owner_settlement_whose_bound_identity_changes_settles_nothing(tmp_path, monkeypatch, drift):
    """R3: the source is unchanged but the effective check settings or the
    covered requirement's text change during the owner's review: the review
    is void and the owed record stays open."""
    from tests.lifecycle.test_judgment_integrity import _reworded, _stricter_gate
    _drift_during(monkeypatch, OWED, _stricter_gate if drift == "check-settings" else _reworded)
    replay = _run(tmp_path, monkeypatch, R1 + T1, RESOLVE, collaborator=reviewer(completion="cap"))
    result = H.result_json(replay)
    owed = _finding(result, "RFT1-1")
    assert not owed["closed"] and not replay.result.completed
    settlements = [a for a in _rv(result)["attempts"] if a["action"] == "owner_settlement"]
    assert settlements and settlements[0]["outcome"].startswith("void: the bound identity changed during the review")
    assert ("gate" if drift == "check-settings" else "covered") in settlements[0]["outcome"]


@pytest.mark.requirements_ledger
def test_a_final_batch_owner_settlement_is_held_to_the_executed_error_evidence_rule(tmp_path, monkeypatch):
    """R4: the capped child covers an error requirement; the owner's
    settlement says MET citing only a source file. The ordinary review rule
    applies: the MET is not counted, so the cap is not lifted, and the gap
    is kept for the audit. The owed review itself was completed."""
    H.grok_lead_routing(monkeypatch, trust=True)  # the case injects a Grok lead or peer
    from tests.lifecycle.test_finish_capacity import BATCH
    from tests.lifecycle.test_quality_completion import README
    from tests.lifecycle.test_step2_recovery import _capped_lead

    error_req = "Add integer numbers and reject non-integer arguments with TypeError"
    batch = BATCH.replace("R1: add(a, b) returns the sum", f"R1: {error_req}")

    def lead_(call, replay):
        if call.task == "t1":
            return _capped_lead(call, replay, {"app.py": FIXED})
        H.write(call, {"README.md": README})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collab(call, replay):
        if call.task != "t1":
            return clean(call)
        if OWED in call.prompt:
            return "NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE"
        return H.claude_cap(NARRATION, num_turns=8) if call.vendor == "claude" else \
            H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8)

    def auditor(call, replay):
        return _auditor({"R1": "MET - app.py", "R2": "MET - README.md"})(call, replay)

    script = Script(orchestrator=_plan(batch), lead=lead_, collaborator=collab, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=2, record_complete=False, settings=CAPPED)
    result = H.result_json(replay)
    owed = next(f for f in result["review_findings"] if f.get("kind") == "unreviewed")
    assert owed["closed"], "the owner completed its owed review"
    t1 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    assert not any(f["kind"] == "cap" and "owed review, completed at its settlement" in f["detail"]
                   for f in t1["facts"]), "a MET without executed negative-path evidence lifts no cap"
    assert any("without executed negative-path evidence" in f["detail"] for f in t1["facts"])
    assert "capped or failed task(s) t1 not continued" in (replay.result.error or "")
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_a_stopped_completion_exports_its_type_and_keeps_the_message_private(tmp_path, monkeypatch):
    """L1: the seat's completion call fails; the record says how it ended by
    type, the message stays in a private artifact, and the occurrence is
    stopped, never pending."""
    canary = "ERROR_CANARY_8812 upstream connection reset at 10.1.2.3"

    def collab(call, replay):
        if CONTEXT in call.prompt:
            return H.claude_error(canary)
        return H.claude_cap(NARRATION, num_turns=8)

    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=collab, max_tasks=1)
    result = H.result_json(replay)
    assert not replay.result.completed
    rv = _rv(result)
    assert rv["attempts"][-1]["outcome"] == "stopped: ProviderError" and rv["result"] == "stopped: ProviderError"
    exported = json.dumps(dict(recoveries=result["recoveries"], improvement=result["improvement"],
                               completions=result["judgment_completions"]))
    assert "ERROR_CANARY_8812" not in exported and "10.1.2.3" not in exported
    assert result["judgment_completions"][0]["error_artifact"]
    assert any("ERROR_CANARY_8812" in t for t in replay.artifact_texts("judgment-completion-error:review:claude:opus"))


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("transcript", ["malformed", "valid-empty"])
def test_a_transcript_read_only_in_part_is_never_called_observed(tmp_path, monkeypatch, transcript):
    """L3: the real live reader (``_live_call_trace``) on a claude transcript
    with one unreadable line reports partial context, and the prompt says
    the counts are lower bounds; a whole transcript with no tool use stays
    observed, with zero tools."""
    from quadratus import trace as T
    path = tmp_path / "session.jsonl"
    good = json.dumps({"type": "assistant", "cwd": "/scratch/copy",
                       "message": {"content": [{"type": "text", "text": "looking"}]}})
    path.write_text(good + "\n" + ("{not json at all\n" if transcript == "malformed" else ""))
    monkeypatch.setattr(T, "locate", lambda vendor, sid, roots: path)
    replay = _run(tmp_path, monkeypatch, R1 + T1, collaborator=reviewer(completion="clean"), max_tasks=1)
    obs = _rv(H.result_json(replay))["observations"]
    (completion,) = _completions(replay)
    if transcript == "malformed":
        assert obs["status"] == "partial" and obs["reason"] == "1 of 2 line(s) unreadable", obs
        assert "only in part (1 of 2 line(s) unreadable)" in completion.prompt and "lower bound" in completion.prompt
    else:
        assert obs["status"] == "observed" and obs["tool_calls"] == 0, obs
        assert "only in part" not in completion.prompt and "0 tool call(s)" in completion.prompt
