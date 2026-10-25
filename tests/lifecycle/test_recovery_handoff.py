"""Recovery handoff repair (Davis, 2026-10-25), after proof-workflow-01 ended
FindingsOpen with task and recovery slots unspent.

The terminal's shape: a parallel batch in which one child's revision hit
Claude's turn cap (kept, merged, continuable) and the other's second
revision stopped at grok's ``max_turn_requests``, which the extractor did
not know, so it was read as a failed, unmerged task, a legacy open finding
stopped the batch, and no planning call could continue either child.

What these controls pin, on the real controller below the vendor CLIs:

- the typed cap, serial and in a parallel child, closes the task capped with
  its record, its files, its exact usage and the effort it was sent at, and
  the planner is asked next, told what is owed, and may continue it within
  the recovery allowance; a spent allowance stops the run by name;
- every other incomplete stop on a revision keeps its own class and the
  run's existing stop: a refusal, a cancellation below the configured
  count, an unknown stop, a cap stop carrying an error, a pre-turn
  authentication failure, a refused budget and unknown usage are none of
  them a cap and none of them a continuation (root boundary evidence,
  2026-10-25: each stops after that one revision, keeps the files, and
  spends no further writer or planning call);
- a raised parallel child keeps that stop and now also leaves the durable
  handoff a scope conflict leaves: a failed record naming the kept artifact
  and the files' base bytes.
"""
import json
from pathlib import Path

import pytest

from quadratus import checkpoint
from quadratus.config import Settings
from quadratus.session import SurveyConfig
from tests.lifecycle import harness as H
from tests.lifecycle import replies
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_step2_recovery import _plan, _simple

REQS = "REQUIREMENTS:\nR1: add(a, b) returns the sum of two numbers and rejects a non-number\n"
TASK = _simple("app.py", "Implement add in app.py.", "R1")
CONTINUE = _simple("app.py", "Finish add in app.py: validate both arguments before adding.", "R1") + "\nCONTINUES: t1"
NOT_MET = "R1: NOT MET - add('x', 1) returns without the error the requirement names\nREVIEW: COMPLETE"
MET = "NO FINDINGS\nR1: MET - app.py validates both arguments and returns the sum\nREVIEW: COMPLETE"
NARRATION = ("The blocking finding is the argparse failure path: non-integer IDs still exit with "
             "plain-text usage instead of JSON. I'll read the CLI ")
TYPED = ("def add(a, b):\n"
         "    for name, value in (('a', a), ('b', b)):\n"
         "        if not isinstance(value, (int, float)):\n"
         "            raise TypeError(name)\n"
         "    return a + b\n")
FIRST_REVISION = FIXED + "\n# validation pending\n"
PARTIAL = FIXED + "\n# validation pending\n# TODO: reject non-numbers\n"


def _ledger(replay):
    return [json.loads(line) for line in (Path(replay.result.run_dir) / "invocations.jsonl").read_text().splitlines()
            if json.loads(line).get("invoked")]


def _roles(replay):
    return [c.role for c in replay.calls]


def _budget(replay):
    return json.loads((Path(replay.result.run_dir) / "budget.json").read_text())


def _script(second_revision, *, orchestrator, writes_partial=True):
    """A grok lead whose review finds R1 unmet, whose first revision does
    not settle it, and whose second revision answers ``second_revision``
    (a callable given the call, or a raw envelope)."""
    def lead(call, replay):
        if "CONTINUES" in call.prompt or "validate both arguments" in call.prompt:
            H.write(call, {"app.py": TYPED})
            return 'Validated both arguments.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if "TypeError" in Path(call.cwd, "app.py").read_text():
            return MET
        return NOT_MET

    def revision(call, replay):
        if call.task != "t1":
            return "Nothing to change.\nCHANGED: []"
        nth = len([c for c in replay.of("revision") if c.task == call.task])
        if nth == 1:
            H.write(call, {"app.py": FIRST_REVISION})
            return 'Started on validation.\nCHANGED: ["app.py"]'
        if writes_partial:
            H.write(call, {"app.py": PARTIAL})
        return second_revision(call) if callable(second_revision) else second_revision

    def recheck(call, replay):
        source = Path(call.cwd, "app.py")
        if source.exists() and "TypeError" in source.read_text():
            return replies.recheck_reply(call.prompt, default="RESOLVED app.py now rejects a non-number")
        return replies.recheck_reply(call.prompt, default="UNRESOLVED add still accepts a non-number")

    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                    auditor=lambda c, r: "R1: MET - app.py", **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["revision"] = revision
    script.overrides["recheck"] = recheck
    return script


GROK_CAP = H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8)


# -- 1. the typed cap on a serial revision: capped, recorded, continued ---------------

@pytest.mark.requirements_ledger
def test_a_serial_revision_capped_by_max_turn_requests_is_continued_by_the_planner(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, CONTINUE)),
                   files=FILES, max_tasks=3, record_complete=False)
    assert [c.vendor for c in replay.of("lead")][0] == "grok"
    revisions = [c for c in replay.of("revision") if c.task == "t1"]
    assert len(revisions) == 2, "the first revision did not settle the finding; the second was the capped one"
    # The capped call's row: the typed cap, its exact usage, its turns, the effort it was sent at.
    row = [r for r in _ledger(replay) if r["task"] == "t1" and r["role"] == "revision"][-1]
    assert row["outcome"] == "TurnLimitReached" and row["turn_limited"] is True
    assert (row["input_tokens"], row["output_tokens"], row["model_turns"]) == (100, 10, 8)
    assert row["effort"] == "high" and "fix cycle 2" in " ".join(row["effort_reasons"])
    # The task closed capped with its partial work kept and the record written.
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == ["t1"] and result["failed_tasks"] == []
    assert replay.artifacts("turn-limited"), "the capped record is an artifact"
    # The planner was asked next, told the debt, and its continuation ran with the handoff.
    planners = replay.of("orchestrator")
    assert len(planners) >= 3, [c.role for c in replay.calls]
    assert "still owed: t1" in planners[1].prompt
    assert "STOPPED AT THE LEAD TURN LIMIT" in planners[1].prompt
    continuation = [c for c in replay.of("lead") if c.task == "t2"]
    assert len(continuation) == 1 and "## Handoff from t1" in continuation[0].prompt
    assert "stopped at its round limit after 8 rounds" in continuation[0].prompt
    assert "It changed app.py" in continuation[0].prompt and NARRATION[:60] in continuation[0].prompt
    assert "nothing is owed" in planners[2].prompt, "the clean continuation took the debt over"
    assert (replay.project / "app.py").read_text() == TYPED


@pytest.mark.requirements_ledger
def test_a_continuation_in_the_last_slot_leaves_a_sealed_terminal_with_no_partial_debt(tmp_path, monkeypatch):
    """Root's provisional review of the batch: the continuation runs in the
    last slot, so no quiescent checkpoint follows it; the sealed terminal
    must still show the debt cleared, the lists agreeing with the records,
    and stay sealed and non-resumable."""
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, CONTINUE)),
                   files=FILES, max_tasks=2, record_complete=False)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], _roles(replay)
    sealed = checkpoint.load(Path(replay.result.run_dir))
    assert sealed["sealed"] and not sealed["resumable"]
    state = checkpoint._decode(sealed["state"])
    assert state["_partial_tasks"] == set(), "the last task cleared the debt after the last quiescent point"
    assert state["turn_limited"] == ["t1"] and state["failed"] == []
    assert set(state["turn_limited_records"]) == {"t1"} and state["failed_records"] == {}
    assert "t1" in state["_cap_baselines"]
    assert (replay.project / "app.py").read_text() == TYPED
    assert "capped or failed task(s)" not in (replay.result.error or ""), replay.result.error


@pytest.mark.requirements_ledger
def test_a_spent_recovery_allowance_stops_the_continuation_by_name(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, CONTINUE)),
                   files=FILES, max_tasks=3, record_complete=False, survey=SurveyConfig(recovery_tasks=0))
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"]
    assert replay.result.error.startswith("SurveyAllowanceSpent"), replay.result.error
    assert [c.task for c in replay.of("lead")] == ["t1"], "no continuation lead ran"
    assert (replay.project / "app.py").read_text() == PARTIAL, "the capped work is kept as it was left"


# -- 2. every other incomplete stop on a revision keeps its class and the run's stop ---

NEGATIVES = {
    "refusal": (H.grok_ok("", stop="refusal", num_turns=8), None, "ProviderRefusal"),
    "cancelled-below-cap": (H.grok_ok(NARRATION, stop="cancelled", num_turns=8), 14, "ProviderError"),
    "future-unknown": (H.grok_ok(NARRATION, stop="future_unknown", num_turns=8), None, "ProviderError"),
    "cap-with-permission-denied": (H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8,
                                             error="permission denied: write app.py"), None, "ProviderError"),
    "cap-with-authentication-required": (H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8,
                                                   error="authentication required"), None, "ProviderError"),
    "pre-turn-authentication": (json.dumps({"type": "error", "message": "authentication required"}), None,
                                "ProviderError"),
}


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", sorted(NEGATIVES))
def test_a_revision_that_stops_any_other_way_is_not_a_cap_and_is_not_continued(tmp_path, monkeypatch, case):
    envelope, cap, klass = NEGATIVES[case]
    settings = Settings(backend="cli", lead_max_turns=cap) if cap else None
    replay = H.run(tmp_path, monkeypatch,
                   _script(envelope, orchestrator=_plan(REQS + TASK, CONTINUE), writes_partial=False),
                   files=FILES, max_tasks=3, record_complete=False, settings=settings)
    roles = _roles(replay)
    assert roles.count("revision") == 2 and roles[-1] == "revision", roles
    assert roles.count("orchestrator") == 1, "no planning call followed the failed revision"
    assert not replay.result.completed
    assert klass in (replay.result.error or ""), replay.result.error
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == [] and result["failed_tasks"] == []
    assert not replay.artifacts("turn-limited") and not replay.artifacts("task-failed")
    row = [r for r in _ledger(replay) if r["task"] == "t1" and r["role"] == "revision"][-1]
    assert row["outcome"] == klass and row["turn_limited"] is False
    assert (replay.project / "app.py").read_text() == FIRST_REVISION, "the first revision's files are kept"


@pytest.mark.requirements_ledger
def test_unknown_usage_on_a_capped_revision_stays_unknown_and_is_not_zero(tmp_path, monkeypatch):
    no_usage = json.dumps({"text": NARRATION, "stopReason": "max_turn_requests", "num_turns": 8,
                           "sessionId": "s-unknown"})
    replay = H.run(tmp_path, monkeypatch, _script(no_usage, orchestrator=_plan(REQS + TASK, CONTINUE)),
                   files=FILES, max_tasks=3, record_complete=False)
    row = [r for r in _ledger(replay) if r["task"] == "t1" and r["role"] == "revision"][-1]
    assert row["outcome"] == "TurnLimitReached"
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert _budget(replay)["unknown_usage_attempts"] == 1
    assert not replay.result.completed


# -- 3. the terminal's own shape: a parallel batch with one capped child per vendor ------

BATCH_REQS = ("REQUIREMENTS:\nR1: add(a, b) returns the sum of two numbers and rejects a non-number\n"
              "R2: README documents add and states what it returns\n")
README_PARTIAL = "# app\n\n`add(a, b)` is documented here.\n"
README_SECOND = README_PARTIAL + "\nIt adds two numbers.\n"
README_DONE = "# app\n\n`add(a, b)` returns the sum of two numbers.\n"


def _block(kind, path, text, covers):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} written",
                        "acceptance": [f"{path} meets the task"], "max_lines": 20,
                        "proofs": H.proofs_for(covers, "tests/test_app.py" if path == "app.py" else path,
                                               evidence="reading" if path == "README.md" else None)})
    return f"KIND: {kind}\nSCOPE: {scope}\n{text}\nCOVERS: {covers}"


BATCH = (BATCH_REQS + "PARALLEL\n" + _block("architect complex", "app.py", "Implement add in app.py.", "R1")
         + "\n---\n" + _block("backend simple", "README.md", "Document add in README.md.", "R2"))
CONTINUE_README = _block("backend simple", "README.md", "Finish the README: state that add returns the sum.",
                         "R2") + "\nCONTINUES: t2"


def _batch_script(second_revision):
    """Child t1 (Opus) stops at Claude's turn cap while drafting; child t2
    (grok) is reviewed, revised once without settling R2, and its second
    revision answers ``second_revision``."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return H.claude_cap("I'll run the tests next", num_turns=14)
        if call.task == "t2":
            H.write(call, {"README.md": README_PARTIAL})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"README.md": README_DONE})
        return 'Documented what add returns.\nCHANGED: ["README.md"]'

    def collaborator(call, replay):
        readme = Path(call.cwd, "README.md")
        if readme.exists() and "returns the sum" in readme.read_text():
            return "NO FINDINGS\nR2: MET - README.md states what add returns\nREVIEW: COMPLETE"
        return "R2: NOT MET - README.md does not state what add returns\nREVIEW: COMPLETE"

    def revision(call, replay):
        if call.task != "t2":
            return "Nothing to change.\nCHANGED: []"
        nth = len([c for c in replay.of("revision") if c.task == call.task])
        if nth == 1:
            H.write(call, {"README.md": README_SECOND})
            return 'Expanded the README.\nCHANGED: ["README.md"]'
        return second_revision

    def recheck(call, replay):
        readme = Path(call.cwd, "README.md")
        if readme.exists() and "returns the sum" in readme.read_text():
            return replies.recheck_reply(call.prompt, default="RESOLVED the README states what add returns")
        return replies.recheck_reply(call.prompt, default="UNRESOLVED the README still does not state the result")

    script = Script(orchestrator=_plan(BATCH, CONTINUE_README), lead=lead, collaborator=collaborator,
                    auditor=lambda c, r: "R1: NOT MET - add accepts a non-number\nR2: MET - README.md",
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["revision"] = revision
    script.overrides["recheck"] = recheck
    return script


@pytest.mark.requirements_ledger
def test_a_batch_with_a_claude_capped_child_and_a_grok_capped_child_reaches_the_planner(tmp_path, monkeypatch):
    """proof-workflow-01's shape with the cap typed: both children close
    capped and merged, the batch does not stop, the planner is told what is
    owed, and its continuation of the grok child runs with the handoff."""
    replay = H.run(tmp_path, monkeypatch, _batch_script(GROK_CAP), files=FILES, max_tasks=4, record_complete=False)
    leads = {c.task: c.vendor for c in replay.of("lead") if c.task in ("t1", "t2")}
    assert leads == {"t1": "claude", "t2": "grok"}
    result = H.result_json(replay)
    assert sorted(result["turn_limited_tasks"]) == ["t1", "t2"] and result["failed_tasks"] == []
    assert result["open_findings"] == [], "no legacy finding was written for a capped child"
    assert (result.get("parallel_batches") or [{}])[0].get("tasks") == ["t1", "t2"]
    rows = {(r["task"], r["role"], r["outcome"]) for r in _ledger(replay)}
    assert ("t2", "revision", "TurnLimitReached") in rows and ("t1", "lead", "TurnLimitReached") in rows
    grok_cap = [r for r in _ledger(replay) if r["task"] == "t2" and r["role"] == "revision"][-1]
    assert (grok_cap["input_tokens"], grok_cap["output_tokens"], grok_cap["model_turns"]) == (100, 10, 8)
    # Both children's partial work was merged, so the continuation builds on it.
    assert (replay.project / "app.py").read_text() == FIXED
    planners = replay.of("orchestrator")
    assert len(planners) >= 3, _roles(replay)
    assert "still owed: t1, t2" in planners[1].prompt
    continuation = [c for c in replay.of("lead") if c.task == "t3"]
    assert len(continuation) == 1 and "## Handoff from t2" in continuation[0].prompt
    assert "stopped at its round limit after 8 rounds" in continuation[0].prompt
    assert "It changed README.md" in continuation[0].prompt
    assert (replay.project / "README.md").read_text() == README_DONE
    assert "No longer owed: t2" in planners[2].prompt and "still owed: t1" in planners[2].prompt
    assert "FindingsOpen" not in (replay.result.error or ""), replay.result.error


@pytest.mark.requirements_ledger
def test_a_raised_child_keeps_the_stop_and_leaves_a_durable_failed_handoff(tmp_path, monkeypatch):
    """The same batch, with the grok child's second revision a cap stop that
    also carries an error: a failure, not a cap. The batch stops as before
    (root boundary evidence) with the child unmerged and its files kept, and
    the record now holds the failed handoff a scope conflict would leave."""
    failing = H.grok_ok(NARRATION, stop="max_turn_requests", num_turns=8, error="permission denied: write README.md")
    replay = H.run(tmp_path, monkeypatch, _batch_script(failing), files=FILES, max_tasks=4, record_complete=False)
    assert len(replay.of("orchestrator")) == 1, "no planning call followed the stopped batch"
    assert (replay.result.error or "").startswith("FindingsOpen"), replay.result.error
    assert "Parallel task t2 was not merged (ProviderError" in replay.result.error
    result = H.result_json(replay)
    assert result["turn_limited_tasks"] == ["t1"] and result["failed_tasks"] == ["t2"]
    assert len(result["open_findings"]) == 1 and "kept in artifact" in result["open_findings"][0]
    kept = replay.artifact_texts("parallel-unmerged")
    assert len(kept) == 1 and json.loads(kept[0]) == {"README.md": README_SECOND}
    assert (replay.project / "README.md").read_text() == FILES["README.md"], "nothing of t2 was merged"
    assert (replay.project / "app.py").read_text() == FIXED, "the capped child was merged"
    sealed = checkpoint.load(Path(replay.result.run_dir))
    assert sealed["sealed"], "the terminal record is sealed, never resumed"
    state = checkpoint._decode(sealed["state"])
    record = state["failed_records"]["t2"]
    assert record["cause"] == "transport" and record["changed"] == ["README.md"]
    assert record["unmerged_artifact"] and record["unmerged_artifact"] in result["open_findings"][0]
    assert "README.md" in state["_cap_baselines"]["t2"]
    assert "t2" in state["_partial_tasks"]
    assert record["role"] == "revision", "the call that failed, as the child recorded it, not a lead draft"
    assert state["failed"] == ["t2"] and state["turn_limited"] == ["t1"], "the sealed lists agree with the records"
    row = [r for r in _ledger(replay) if r["task"] == "t2" and r["role"] == "revision"][-1]
    assert row["outcome"] == "ProviderError" and row["turn_limited"] is False
