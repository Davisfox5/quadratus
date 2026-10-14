"""Step 2 (2026-10-13, #35): recovery that changes approach, conflicts that
can be continued, and interrupted jobs that resume from a durable quiescent
checkpoint with their record, lineage and spent allowance.

Whole-controller replays below the vendor CLI (tests.lifecycle.harness),
plus parallel-batch controls on the scripted fork session.
"""

import json
from pathlib import Path

import pytest

from quadratus import checkpoint
from quadratus.checkpoint import CheckpointRefused
from quadratus.config import Settings
from quadratus.project_run import run_project
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.session import Session
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2, FILES, FIXED, T1, Script

LIMITS = RunLimits(max_calls=120, max_reported_tokens=6_000_000, wall_seconds=600, max_concurrent_workers=2)


def _plan(*replies):
    """Orchestrator replies in order across episodes, then DONE."""
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return replies[n - 1] if n <= len(replies) else "DONE"
    return orchestrator


_NEXT_TASK = Session.next_task


def _interrupt_after(monkeypatch, tasks: int):
    """A KeyboardInterrupt when the planner is asked for the task after
    ``tasks`` slots: the quiescent checkpoint was written just before, and no
    call is in flight."""
    def next_task(self, *args, **kwargs):
        if getattr(self, "_slots_used", len(self.history)) == tasks and not Session._interrupted_once:
            Session._interrupted_once = True
            raise KeyboardInterrupt
        return _NEXT_TASK(self, *args, **kwargs)
    monkeypatch.setattr(Session, "next_task", next_task)
    monkeypatch.setattr(Session, "_interrupted_once", False, raising=False)


def _episode(replay, *, resume_from, goal="Build the feature.", max_tasks=2, limits=LIMITS, **workflow):
    settings = Settings(backend="cli")
    settings.backend_overrides = {}
    settings.openai_api_key = settings.anthropic_api_key = settings.xai_api_key = None
    return run_project(goal, replay.project, settings, allow_writes=True, check=H.GATE, max_tasks=max_tasks,
                       run_limits=limits, resume_from=resume_from, **workflow)


def _interrupted(tmp_path, monkeypatch, script=None, max_tasks=2):
    _interrupt_after(monkeypatch, 1)
    replay = H.run(tmp_path, monkeypatch, script or Script(orchestrator=_plan(DECL_T1, DECL_T2)),
                   files=FILES, max_tasks=max_tasks, record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error
    return replay, Path(replay.result.run_dir)


def _budget(run_dir):
    return json.loads((Path(run_dir) / "budget.json").read_text())


# -- interruption recovery, accepted-work preservation, budget carryover ----------

def test_an_interrupted_job_resumes_with_its_record_lineage_and_spent_allowance(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    spent = _budget(first)
    assert spent["in_flight"] == 0 and spent["reserved_attempts"] > 0
    body = checkpoint.load(first)
    assert body["resumable"] and not body["sealed"] and body["state"]["slots_used"] == 1
    assert (replay.project / "app.py").read_text() == FIXED, "t1's accepted work is in the project"

    calls_before = len(replay.calls)
    second = Path(_episode(replay, resume_from=first.name).run_dir)
    lineage = json.loads((second / "lineage.json").read_text())
    assert lineage == dict(root=first.name, parent=first.name, episode=2)
    assert json.loads((first / "resumed-by.json").read_text())["child"] == second.name
    # The planner sees t1's record, and the job's next task is t2: one slot left.
    planner = [c for c in replay.calls[calls_before:] if c.role.startswith("orchestrator")]
    assert "### Task t1" in planner[0].prompt
    result = json.loads((second / "result.json").read_text())
    assert result["tasks"] == 2 and [t["task_id"] for t in result["workflow"]["tasks"]][:1] == ["t1"]
    assert result["lineage"]["episode"] == 2
    # The allowance is the job's: the new episode's totals include the old.
    budget = _budget(second)
    assert budget["carried_from_earlier_episodes"]["calls"] == spent["reserved_attempts"]
    assert budget["reserved_attempts"] == spent["reserved_attempts"] + (len(replay.calls) - calls_before)
    assert budget["reported_tokens"] >= spent["reported_tokens"]
    assert (replay.project / "app.py").read_text() == FIXED


def test_the_resumed_episode_keeps_the_jobs_run_start_test_baseline(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    body = checkpoint.load(first)
    originals = checkpoint._decode(body["state"]["_original_tests"])
    assert originals == {"tests/test_app.py": FILES["tests/test_app.py"].encode()}


# -- stale, in-flight, tampered, sealed and forked resumes fail closed --------------

def test_a_changed_source_refuses_the_resume(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    (replay.project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    with pytest.raises(CheckpointRefused, match="source"):
        _episode(replay, resume_from=first.name)
    assert not (first / "resumed-by.json").exists()


@pytest.mark.parametrize("change", ["goal", "limits"])
def test_a_different_goal_or_allowance_refuses_the_resume(tmp_path, monkeypatch, change):
    replay, first = _interrupted(tmp_path, monkeypatch)
    kwargs = (dict(goal="Build something else.") if change == "goal"
              else dict(limits=RunLimits(max_calls=999, max_reported_tokens=6_000_000, wall_seconds=600,
                                         max_concurrent_workers=2)))
    with pytest.raises(CheckpointRefused, match=change):
        _episode(replay, resume_from=first.name, **kwargs)


def test_a_call_in_flight_when_the_run_ended_refuses_the_resume(tmp_path, monkeypatch):
    def lead(call, replay):
        raise KeyboardInterrupt   # mid-call: reserved, never finished
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1), lead=lead), files=FILES,
                   max_tasks=2, record_complete=False)
    first = Path(replay.result.run_dir)
    # The transport accounts the interrupted attempt with no usage: unknown.
    assert _budget(first)["unknown_usage_attempts"] == 1
    with pytest.raises(CheckpointRefused, match="unknown_usage"):
        _episode(replay, resume_from=first.name)


def test_an_altered_checkpoint_refuses_the_resume(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    data = json.loads((first / "checkpoint.json").read_text())
    data["body"]["consumed"]["calls"] = 0
    (first / "checkpoint.json").write_text(json.dumps(data))
    with pytest.raises(CheckpointRefused, match="digest"):
        _episode(replay, resume_from=first.name)


def test_a_finished_run_is_sealed_and_never_resumed(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1)), files=FILES, max_tasks=1)
    first = Path(replay.result.run_dir)
    assert checkpoint.load(first)["sealed"]
    with pytest.raises(CheckpointRefused, match="sealed"):
        _episode(replay, resume_from=first.name)


def test_a_parent_is_resumed_once(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch, max_tasks=3)
    _interrupt_after(monkeypatch, 2)
    second = Path(_episode(replay, resume_from=first.name, max_tasks=3).run_dir)
    with pytest.raises(CheckpointRefused, match="already resumed"):
        _episode(replay, resume_from=first.name, max_tasks=3)
    # The latest episode is the one to resume, and the lineage continues.
    third = Path(_episode(replay, resume_from=second.name, max_tasks=3).run_dir)
    assert json.loads((third / "lineage.json").read_text()) == dict(root=first.name, parent=second.name,
                                                                     episode=3)


def test_a_resume_from_outside_the_state_folder_is_refused(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    with pytest.raises(CheckpointRefused, match="not a run of this project state"):
        _episode(replay, resume_from=str(tmp_path))


@pytest.mark.parametrize("change", [dict(mode="constructive"), dict(direct_tier=True),
                                    dict(decider_labels="all")])
def test_a_different_run_configuration_refuses_the_resume(tmp_path, monkeypatch, change):
    """Root review of aaa06a6: mode, tier and decider labels decide dispatch."""
    replay, first = _interrupted(tmp_path, monkeypatch)
    with pytest.raises(CheckpointRefused, match="workflow"):
        _episode(replay, resume_from=first.name, **change)
    assert not (first / "resumed-by.json").exists()


def test_the_interrupted_record_carries_the_time_up_to_the_interruption(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    body = checkpoint.load(first)
    budget = _budget(first)
    for field, name in (("elapsed_seconds", "elapsed_seconds"), ("input_tokens", "input_tokens"),
                        ("output_tokens", "output_tokens"), ("reserved_attempts", "calls")):
        assert budget[field] >= body["consumed"][name], field


@pytest.mark.parametrize("field,value,match", [("in_flight", 1, "in flight"),   # a killed process
                                                ("unknown_usage_attempts", 1, "unknown_usage"),
                                                ("reserved_attempts", 0, "less than the checkpoint"),
                                                ("input_tokens", 0, "less than the checkpoint"),
                                                ("output_tokens", 0, "less than the checkpoint"),
                                                ("elapsed_seconds", 0.0, "less than the checkpoint"),
                                                ("input_tokens", -1, "non-negative"),
                                                ("elapsed_seconds", float("nan"), "finite"),
                                                ("output_tokens", "10", "not a number"),
                                                ("reserved_attempts", None, "not a number")])
def test_a_budget_record_with_unknown_or_missing_usage_refuses_the_resume(tmp_path, monkeypatch, field, value,
                                                                          match):
    replay, first = _interrupted(tmp_path, monkeypatch)
    budget = _budget(first)
    budget[field] = value
    (first / "budget.json").write_text(json.dumps(budget))
    with pytest.raises(CheckpointRefused, match=match):
        _episode(replay, resume_from=first.name)


# -- unfinished work: held facts, changed approach -----------------------------------

SCOPE_BREACH = "KIND: backend standard\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."


def test_a_failed_task_hands_the_planner_held_facts_and_an_unchanged_retry_is_refused(tmp_path, monkeypatch):
    retry = SCOPE_BREACH + "\nCONTINUES: t1"
    changed = ("KIND: backend standard\nSCOPE: " + json.dumps(dict(T1, permitted_paths=["app.py", "util.py",
                                                                                         "tests/test_app.py"]))
               + "\nImplement add in app.py, with its helper in util.py.\nCONTINUES: t1")

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "util.py": "HELPER = 1\n"})
            return 'Implemented add.\nCHANGED: ["app.py", "util.py"]'
        H.write(call, {"app.py": FIXED, "util.py": "HELPER = 1\n"})
        return 'Implemented add with its helper.\nCHANGED: ["app.py", "util.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(SCOPE_BREACH, retry, changed), lead=lead),
                   files=FILES, max_tasks=3, record_complete=False)
    planners = replay.of("orchestrator")
    assert "Harness record" in planners[1].prompt and "unfinished: failed (scope)" in planners[1].prompt
    assert "a continuation must change the approach" in planners[1].prompt
    assert "with the same task text and scope that did not finish" in planners[2].prompt
    leads = [c.task for c in replay.of("lead")]
    assert leads.count("t1") == 1, "the unchanged retry never reached a lead"
    assert len(set(leads)) == 2, "the changed continuation was dispatched"


@pytest.mark.parametrize("field,value,match", [
    ("in_flight", None, "in_flight is missing"), ("in_flight", "0", "in_flight is missing"),
    ("in_flight", True, "in_flight is missing"),
    ("unknown_usage_attempts", None, "unknown_usage_attempts is missing"),
    ("unknown_usage_attempts", -1, "unknown_usage_attempts is missing"),
    ("stop_reason", None, "stop_reason is missing"), ("stop_reason", 0, "stop_reason is missing")])
def test_an_absent_or_malformed_uncertainty_field_is_unknown_not_zero(tmp_path, monkeypatch, field, value, match):
    """Root review of dc70adb: a missing in_flight, unknown_usage_attempts or
    stop_reason resumed as if it read zero or empty."""
    replay, first = _interrupted(tmp_path, monkeypatch)
    budget = _budget(first)
    if value is None:
        del budget[field]
    else:
        budget[field] = value
    (first / "budget.json").write_text(json.dumps(budget))
    with pytest.raises(CheckpointRefused, match=match):
        _episode(replay, resume_from=first.name)
    assert not (first / "resumed-by.json").exists()


def test_a_record_with_known_zero_uncertainty_still_resumes(tmp_path, monkeypatch):
    replay, first = _interrupted(tmp_path, monkeypatch)
    budget = _budget(first)
    assert (budget["in_flight"], budget["unknown_usage_attempts"], budget["stop_reason"]) == (0, 0, "")
    second = Path(_episode(replay, resume_from=first.name).run_dir)
    assert json.loads((second / "lineage.json").read_text())["episode"] == 2


# -- budget persistence and identity precision (root review of aaa06a6) -------------

def test_time_between_calls_reaches_the_record_the_checkpoint_is_written_against(tmp_path):
    """The quiescent checkpoint and budget.json come from one persisted
    snapshot, so time spent on checks between calls is carried."""
    clock = [0.0]
    budget = RunBudget(LIMITS, path=tmp_path / "budget.json", clock=lambda: clock[0])
    ticket, _ = budget.reserve(transport="cli", price_key="openai:gpt-5.6-sol")
    clock[0] = 10.0
    budget.finish(ticket, {"input_tokens": 100, "output_tokens": 10})
    clock[0] = 30.0                          # checks and merge work between calls
    snapshot = budget.persist()
    identity = dict(goal="g")
    checkpoint.write(tmp_path, dict(version=checkpoint.VERSION, sealed=False, resumable=True,
                                    identities=identity, consumed=checkpoint.consumed(snapshot)))
    spent = checkpoint.validate(tmp_path, checkpoint.load(tmp_path), identity)
    assert spent["elapsed_seconds"] == 30.0 and spent["calls"] == 1 and spent["input_tokens"] == 100


def test_a_stale_budget_record_is_refused_rather_than_read_as_free_time(tmp_path):
    clock = [0.0]
    budget = RunBudget(LIMITS, path=tmp_path / "budget.json", clock=lambda: clock[0])
    ticket, _ = budget.reserve(transport="cli", price_key="openai:gpt-5.6-sol")
    clock[0] = 10.0
    budget.finish(ticket, {"input_tokens": 100, "output_tokens": 10})   # persisted at 10
    clock[0] = 30.0
    identity = dict(goal="g")
    checkpoint.write(tmp_path, dict(version=checkpoint.VERSION, sealed=False, resumable=True,
                                    identities=identity, consumed=checkpoint.consumed(budget.snapshot())))
    with pytest.raises(CheckpointRefused, match="elapsed_seconds"):
        checkpoint.validate(tmp_path, checkpoint.load(tmp_path), identity)


def test_settings_identity_binds_controls_and_leaves_out_only_credentials():
    from types import SimpleNamespace
    digest = checkpoint.settings_identity
    assert digest(SimpleNamespace(max_tokens=1000, openai_api_key="first")) != \
        digest(SimpleNamespace(max_tokens=500000, openai_api_key="first"))
    assert digest(SimpleNamespace(max_tokens=1000, openai_api_key="first")) == \
        digest(SimpleNamespace(max_tokens=1000, openai_api_key="second"))
    assert digest(Settings(backend="cli")) != digest(Settings(backend="cli", lead_max_turns=7))


# -- the planner plans against the job's task allowance (medium job on 3e59970) ----

def test_the_planner_is_told_the_slots_left_counting_the_task_it_names(tmp_path, monkeypatch):
    """Step 2 medium job on 3e59970: a cap of 3 was never stated to the
    planner; it spent the cap on storage and HTTP and never planned the CLI."""
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1, DECL_T2)), files=FILES, max_tasks=3,
                   record_complete=False)
    planners = replay.of("orchestrator")
    assert "Task slots: 3 of 3 remain for this job, counting the task you name now." in planners[0].prompt
    assert "Task slots: 2 of 3 remain" in planners[1].prompt
    assert "Task slots: 1 of 3 remain" in planners[2].prompt


@pytest.mark.requirements_ledger
def test_the_planner_is_told_which_requirements_no_task_has_covered(tmp_path, monkeypatch):
    reqs = "REQUIREMENTS:\nR1: add works\nR2: README documents add\n"
    first = reqs + DECL_T1 + "\nCOVERS: R1"
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(first, DECL_T2 + "\nCOVERS: R2"),
                                                  auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md"),
                   files=FILES, max_tasks=3, record_complete=False)
    second = replay.of("orchestrator")[1].prompt
    assert "Requirements no task has covered yet: R2." in second
    assert "say which will be left open" in second


# -- acceptance the configured checks can show (r02 on 3b629cf) --------------------

@pytest.mark.parametrize("check", [H.GATE, None], ids=["configured-check", "no-check"])
def test_committed_test_acceptance_is_scoped_to_automated_behaviour_under_configured_checks(
        tmp_path, monkeypatch, check):
    """r02: t1's acceptance named a scratch two-Store Python session; the
    lead reported it NOT RUN and R1-R7 stayed open. Root's review of 86e2841:
    the shared SCOPE text then required every acceptance item to be a
    committed test, which a read-only review, design judgment or harness
    capture cannot satisfy, with or without checks. The rule now lives only
    in the configured-checks note and covers automated code behaviour; the
    other channels are named as unchanged. NOT RUN is unchanged."""
    # No check given and no tests to detect: the project has no configured check.
    files = FILES if check else {k: v for k, v in FILES.items() if not k.startswith("tests/")}
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1)), files=files, max_tasks=1,
                   check=check)
    first = replay.of("orchestrator")[0].prompt
    assert "Each acceptance item must be a condition a committed test shows" not in first
    assert '"edits": "none" to SCOPE' in first, "read-only reviews are still offered"
    scoped = "Where acceptance is automated code behaviour these checks can establish"
    if check is None:
        assert "## Configured project checks" not in first, "the project has no configured check"
        assert scoped not in first and "keeps its requirements open as NOT RUN" not in first
    else:
        assert scoped in first and "keeps its requirements open as NOT RUN" in first
        assert ("independent design or audit judgment, or a read-only (\"edits\": \"none\") review, "
                "which adds no files") in first
    assert "--quadratus-report" not in first, "still no command line reaches the planner"


# -- a capped draft in the last slot (r03 on f4eafea) --------------------------------

DECL_SIMPLE = "KIND: backend simple\nSCOPE: " + json.dumps(T1) + "\nImplement add in app.py."


@pytest.mark.parametrize("source", ["checked", "failing"])
def test_a_capped_draft_in_the_last_slot_meets_an_independent_review_only_when_checked(
        tmp_path, monkeypatch, source):
    """r03: t3's Grok lead hit its turn cap in the last slot with the recovery
    spent; the checks passed on its in-scope source and nothing could continue
    it, so the job ended with no independent read of the work. That source now
    goes to one cross-vendor review and the existing gate and close-out; the
    lead stays capped. Source the checks do not pass closes capped as before."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed

    def lead(call, replay):
        H.write(call, {"app.py": fixed if source == "checked" else "def add(a, b):\n    return a - b\n"})
        return H.grok_ok("I'll run the tests next", stop="cancelled", num_turns=8)

    seen = []

    def collaborator(call, replay):
        seen.append(call)
        return "NO FINDINGS"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_SIMPLE), lead=lead,
                                                 collaborator=collaborator),
                   files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8))
    assert replay.of("lead")[0].vendor == "grok"
    capped = "capped or failed task(s) t1 not continued" in (replay.result.error or "")
    if source == "failing":
        assert not seen and capped, "unchecked source still closes capped, unreviewed"
        return
    assert len(seen) == 1 and seen[0].vendor != "grok", "one review from another vendor"
    assert "HARNESS RECORD: the lead stopped at its turn limit" in seen[0].prompt
    assert "Nothing here says the work is finished" in seen[0].prompt
    assert [c.task for c in replay.of("lead")] == ["t1"], "no further lead turn or task was added"
    assert not capped, "the reviewed task is not left as unfinished debt"
    assert [c.role for c in replay.calls] == ["orchestrator", "lead", "collaborator", "closeout", "orchestrator"]
    assert replay.result.completed, "completed only on the orchestrator's own answer at the cap"


@pytest.mark.parametrize("closing", ["capped", "finished"])
def test_every_task_review_judges_the_original_goal_not_only_the_task_paraphrase(tmp_path, monkeypatch, closing):
    """Root review of d0f3263, then r04 on 5366412: a reviewer saw only the
    task's paraphrase, and r03's and r04's CLI tasks dropped the goal's
    error contract; both closed and the defect stayed. Here the task text
    omits the goal's error condition. The peer receives the goal verbatim,
    holds the source to it, and the unmet condition stays open after the
    lead's one revision instead of the job completing, whether the draft
    was capped (the last-slot handoff) or finished (an ordinary review)."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    contract = "A non-number argument must raise TypeError naming the argument."
    goal = "Implement add(a, b) in app.py. " + contract
    declared = DECL_SIMPLE if closing == "capped" else DECL_SIMPLE.replace("backend simple", "backend standard")

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        if closing == "capped":
            return H.grok_ok("I'll run the tests next", stop="cancelled", num_turns=8)
        return 'Implemented add.\nCHANGED: ["app.py"]'

    seen = []

    def collaborator(call, replay):
        seen.append(call)
        if contract in call.prompt:
            return "BLOCKING: add('x', 1) does not raise TypeError naming the argument.\nREVIEW: COMPLETE"
        return "NO FINDINGS"

    def recheck(call, replay):
        return "UNRESOLVED: add still accepts a non-number without the TypeError the goal requires."

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead,
                                                 collaborator=collaborator, recheck=recheck),
                   files=FILES, max_tasks=1, goal=goal, settings=Settings(backend="cli", lead_max_turns=8))
    assert contract not in declared, "the task text omits the goal's error condition"
    assert seen and all(contract in c.prompt for c in seen), "every peer received the original goal"
    assert "it outranks the task text where they differ" in seen[0].prompt
    assert "It grants nothing" in seen[0].prompt
    assert [c.task for c in replay.of("lead")] == ["t1"] and replay.of("revision"), "the lead answered once"
    assert not replay.result.completed, "the goal's unmet condition stays open"
