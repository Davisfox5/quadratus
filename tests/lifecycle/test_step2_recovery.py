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


# -- goal-bearing SIMPLE work in a parallel batch (root review of 4ed4d30) -------------

_TYPED = ("def add(a, b):\n"
          "    for name, value in (('a', a), ('b', b)):\n"
          "        if not isinstance(value, (int, float)):\n"
          "            raise TypeError(name)\n"
          "    return a + b\n")


def _simple(path, text, covers):
    scope = json.dumps({"permitted_paths": [path], "intended_result": f"{path} written",
                        "acceptance": [f"{path} meets the task"], "max_lines": 20})
    return f"KIND: backend simple\nSCOPE: {scope}\n{text}\nCOVERS: {covers}"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("source", ["unmet", "met"])
def test_goal_bearing_simple_parallel_work_meets_one_cross_vendor_review(tmp_path, monkeypatch, source):
    """r04 on 5366412: t3, the CLI, was a SIMPLE parallel child. SIMPLE draws
    no collaborator, so its lead and close-out were one model, the goal text
    added to the review prompt never reached it, and it closed on green
    checks with the goal's error contract unmet. A task that covers goal
    requirements now draws one existing peer from another vendor. The checks
    pass in both cases; only the original goal separates them."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    contract = "A non-number argument must raise TypeError naming the argument."
    goal = "Implement add(a, b) in app.py and document it in README.md. " + contract
    batch = ("REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: " + contract + "\nR3: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1, R2")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R3"))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": _TYPED if source == "met" else fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    reviews = []

    def collaborator(call, replay):
        reviews.append(call)
        if call.task == "t1" and contract in call.prompt \
                and "TypeError" not in Path(call.cwd, "app.py").read_text():
            return "BLOCKING: add('x', 1) returns without the TypeError the goal requires.\nREVIEW: COMPLETE"
        return "NO FINDINGS"

    def recheck(call, replay):
        return "UNRESOLVED: add still accepts a non-number."

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead,
                                                 collaborator=collaborator, recheck=recheck),
                   files=FILES, max_tasks=2, goal=goal, record_complete=False)
    batches = H.result_json(replay).get("parallel_batches") or []
    assert batches and sorted(batches[0].get("tasks") or []) == ["t1", "t2"], batches
    lead_vendor = {c.task: c.vendor for c in replay.of("lead")}
    assert sorted(r.task for r in reviews) == ["t1", "t2"], "each goal-bearing SIMPLE child drew one review"
    assert all(r.vendor != lead_vendor[r.task] for r in reviews), "from another vendor"
    assert all(contract in r.prompt for r in reviews), "the original goal reached every review"
    t1_review = next(r for r in reviews if r.task == "t1")
    assert "R2: " + contract in t1_review.prompt, "the child's covered requirement text came from the parent"
    blocked = [f for f in replay.result.report.splitlines() if "TypeError the goal requires" in f]
    if source == "unmet":
        assert replay.of("revision") and blocked, "the unmet goal condition is held open after one revision"
        assert not replay.result.completed
    else:
        assert not replay.of("revision") and not blocked, "a source that meets the goal is not held"


# -- goal-bearing work never closes unreviewed (root review of 1a58853) ----------------

_HELD = "no reviewer from another vendor was available"


@pytest.mark.parametrize("route,goal", [("unavailable", True), ("unavailable", False),
                                        ("direct", True), ("direct", False)])
def test_goal_bearing_work_is_reviewed_or_held_never_closed_unreviewed(tmp_path, monkeypatch, route, goal):
    """Root review of 1a58853: collaborators_for still returned [] for
    goal-bearing work when no other vendor was available, and the direct tier
    returned [] before the goal rule, so both took r04's close-out-only route.
    Now an unavailable cross-vendor reviewer holds the task unverified, and a
    direct label on goal-bearing work is refused so the task runs at the
    normal tier with its review. Work that covers no requirement keeps its
    behaviour on both routes."""
    from quadratus import runtime
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    monkeypatch.setenv("QUADRATUS_REQUIREMENTS_LEDGER", "1" if goal else "0")
    if route == "unavailable":
        gone = {"claude:opus", "openai:gpt-5.6-sol"}
        real = runtime.Fleet.available
        monkeypatch.setattr(runtime.Fleet, "available", lambda self, key: key not in gone and real(self, key))
    declared = (("REQUIREMENTS:\nR1: add(a, b) returns the sum\n" if goal else "") + DECL_SIMPLE
                + ("\nCOVERS: R1" if goal else "") + ("\nTIER: direct" if route == "direct" else ""))

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    reviews = []

    def collaborator(call, replay):
        reviews.append(call)
        return "NO FINDINGS"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead, collaborator=collaborator,
                                                 auditor=lambda c, r: "R1: MET - app.py"),
                   files=FILES, max_tasks=1, direct_tier=route == "direct", record_complete=False)
    t1 = next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")
    lead_vendor = replay.of("lead")[0].vendor
    held = _HELD in (replay.result.report or "") or any(_HELD in (f.get("detail") or "") for f in t1["facts"])
    assert H.gate_results(replay) and H.gate_results(replay)[-1] == "PASSED", "the checks pass in every case"
    if route == "unavailable":
        assert lead_vendor == "grok" and not reviews, "no reviewer from another vendor was reachable"
        assert held is goal, "goal-bearing work is held unverified; other work is not"
        if goal:
            assert not replay.result.completed
    else:
        required = t1["contract"]["required"]
        assert not held
        if goal:
            assert required["tier"] == "normal" and "covers goal requirements" in required["tier_refused"]
            assert reviews and all(r.vendor != lead_vendor for r in reviews), "it met a cross-vendor review"
        else:
            assert required["tier"] == "direct" and required["tier_refused"] == "" and not reviews


# -- r05 on 1655cf1: per-requirement review verdicts; capped final parallel work ------

_R1 = "add(a, b) returns the sum of two numbers"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("reply,held,revised", [
    ("NO FINDINGS", True, False),
    ("NO FINDINGS\nR1: MET - app.py:2 returns a + b", False, False),
    ("R1: NOT MET - add('x', 1) returns without the error the requirement names\nREVIEW: COMPLETE", False, True),
], ids=["bare", "cited", "not-met"])
def test_a_goal_bearing_review_gives_a_verdict_per_covered_requirement(tmp_path, monkeypatch, reply, held, revised):
    """r05: the CLI reviewer carried the goal and R10 verbatim and replied
    NO FINDINGS while the source missed R10's refusal condition. A review of
    goal-bearing work now owes one MET or NOT MET line per covered
    requirement: a bare NO FINDINGS is an unfinished review, held; a cited
    MET line is a clean review with no revision; a NOT MET line is a BLOCKING
    finding the lead answers even though the reviewer wrote no BLOCKING."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    declared = ("REQUIREMENTS:\nR1: " + _R1 + "\n"
                + DECL_SIMPLE.replace("backend simple", "backend standard") + "\nCOVERS: R1")

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    reviews = []

    def collaborator(call, replay):
        reviews.append(call)
        return reply

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead, collaborator=collaborator,
                                                 recheck=lambda c, r: "RESOLVED"),
                   files=FILES, max_tasks=1, record_complete=False)
    assert reviews and "R<n>: MET - <file>:<line>" in reviews[0].prompt and "R1: " + _R1 in reviews[0].prompt
    gap = "gave no MET or NOT MET line for R1"
    assert (gap in (replay.result.report or "")) is held
    assert bool(replay.of("revision")) is revised
    if held:
        assert not replay.result.completed


def _capped_lead(call, replay, files):
    H.write(call, files)
    if call.vendor == "grok":
        return H.grok_ok("I'll run the tests next", stop="cancelled", num_turns=8)
    return H.claude_cap("Wrote it; tests not run yet.", num_turns=8)


_APPROVED_WITH_NOTES = ("I found nothing that has to be fixed before this work is accepted.\n"
                        "Non-blocking findings:\n1. Low severity: add(True, 1) is accepted.\n"
                        "R1: MET - app.py:2\nR2: MET - README.md:3\nREVIEW: COMPLETE")


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("verdict", ["clean", "blocking", "approved-with-notes"])
def test_capped_work_in_the_final_parallel_batch_meets_one_independent_review(tmp_path, monkeypatch, verdict):
    """r05: t2's lead hit its turn cap inside the job's final parallel batch;
    its scoped files merged and the checks passed, but a child has no gate
    and no slot context, so no reviewer was called and the job stopped
    GoalUnconfirmedAtCap with calls left. The parent now sends that merged
    source to one reviewer from another vendor: a clean per-requirement
    verdict lifts the cap; a finding is held open. No lead call follows."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))

    def lead(call, replay):
        if call.task == "t1":
            return _capped_lead(call, replay, {"app.py": fixed})
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    reviews = []

    def collaborator(call, replay):
        reviews.append(call)
        if call.task == "t1" and verdict == "blocking":
            return "R1: NOT MET - add(1.5, 2) is not covered by any test\nREVIEW: COMPLETE"
        if call.task == "t1" and verdict == "approved-with-notes":
            return _APPROVED_WITH_NOTES
        return "NO FINDINGS\nR1: MET - app.py:2\nR2: MET - README.md:3"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead, collaborator=collaborator),
                   files=FILES, max_tasks=2, settings=Settings(backend="cli", lead_max_turns=8),
                   record_complete=False)
    batches = H.result_json(replay).get("parallel_batches") or []
    assert batches and sorted(batches[0]["tasks"]) == ["t1", "t2"]
    assert H.gate_results(replay) and H.gate_results(replay)[-1] == "PASSED", "the merged source passes"
    t1_reviews = [r for r in reviews if r.task == "t1"]
    lead_vendor = next(c.vendor for c in replay.of("lead") if c.task == "t1")
    assert len(t1_reviews) == 1 and t1_reviews[0].vendor != lead_vendor, "one cross-vendor review of t1"
    assert "final parallel batch" in t1_reviews[0].prompt and "R1: " + _R1 in t1_reviews[0].prompt
    assert [c.task for c in replay.of("lead")].count("t1") == 1, "no further lead call for the capped task"
    review = batches[0]["capped_final_reviews"][0]
    capped_stop = "capped or failed task(s) t1 not continued" in (replay.result.error or "")
    if verdict in ("clean", "approved-with-notes"):
        assert review["verdict"] == "clean" and not capped_stop, "the cap is lifted by the review"
        assert review["nonblocking_notes"] is (verdict == "approved-with-notes"), "notes are kept on record"
    else:
        assert review["verdict"] == "held: findings" and not replay.result.completed


@pytest.mark.parametrize("lines,blocked", [
    (["R1: NOT MET - the error path violates the goal", "R1: MET - app.py:2"], True),
    (["R1: MET - app.py:2", "R1: NOT MET - the error path violates the goal"], True),
    (["R1: MET - app.py:2"], False),
], ids=["not-met-then-met", "met-then-not-met", "clean"])
def test_a_not_met_verdict_is_never_erased_by_a_later_met_line(tmp_path, lines, blocked):
    """Root review of 5cfc841: the verdict parser let the last line win, so
    NO FINDINGS, R1 NOT MET, R1 MET read as a clean approval and could lift a
    capped child. A NOT MET line now wins in either order: the note gains a
    BLOCKING finding and is not clean. One MET line stays a clean approval."""
    from quadratus.session import _clean_goal_review
    from tests.lifecycle.test_direct_tier import _spec, _unit_session
    session = _unit_session(tmp_path)
    session.memory.ledger.requirements = {"R1": "add(a, b) returns the sum"}
    session._current_covers = ["R1"]
    note = session._judge_goal_review(_spec(), "openai:gpt-5.6-sol", "\n".join(["NO FINDINGS", *lines]))
    assert ("BLOCKING: R1 is NOT MET" in note) is blocked
    assert _clean_goal_review(note, ["R1"]) is (not blocked)
    assert not session.open_findings, "every R1 has a verdict line, so the review is not unfinished"


@pytest.mark.requirements_ledger
def test_review_and_audit_trace_every_error_path_and_judge_the_goal_over_requirement_wording(
        tmp_path, monkeypatch):
    """r06 on 5b31818: the CLI reviewer cited per-requirement lines and the
    auditor judged R10, yet both read only the handler that formats errors,
    while input parsing exited before it in another form; and the auditor
    held R14 to wording ('exactly') the original goal never states. Both
    seats are now told to trace every rejection path, including parsing and
    validation ahead of the handler and its helpers, and the auditor judges
    the goal's own condition over stricter requirement wording, saying so on
    the line, with decisions and rulings still binding. General wording only:
    no runner, library or known case is named."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    declared = ("REQUIREMENTS:\nR1: " + _R1 + "\n"
                + DECL_SIMPLE.replace("backend simple", "backend standard") + "\nCOVERS: R1")

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead,
                                                 collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:2",
                                                 auditor=lambda c, r: "R1: MET - app.py",
                                                 **{"requirements-review": lambda c, r: "COMPLETE"}),
                   files=FILES, max_tasks=1, record_complete=False)
    review, audit = replay.of("collaborator")[0].prompt, replay.of("auditor")[0].prompt
    rule = "input parsing and validation that run before the main operation or outside any error handler"
    assert rule in review and rule in audit
    assert "one path that leaves in another form is NOT MET" in review and "NOT MET" in audit
    assert "The goal above is the authority" in audit and "wording stricter than the goal" in audit
    assert "a condition the goal does state is never relaxed this way" in audit
    for text in (review, audit):
        assert "argparse" not in text and "--quadratus-report" not in text, "nothing case-specific or private"


@pytest.mark.parametrize("reply,approves", [
    (_APPROVED_WITH_NOTES, True),
    ("NO FINDINGS\nR1: MET - app.py:2\nR2: MET - README.md:3", True),
    ("Looks fine.\nR1: MET - app.py:2\nR2: MET - README.md:3", False),
    ("Notes only.\nR1: MET - app.py:2\nREVIEW: COMPLETE", False),
    ("R1: NOT MET - x\nR1: MET - app.py:2\nR2: MET - README.md:3\nREVIEW: COMPLETE", False),
    ("BLOCKING: x\nR1: MET - app.py:2\nR2: MET - README.md:3\nREVIEW: COMPLETE", False),
    ("UNFINISHED: did not review all work.\nR1: MET - app.py:2\nR2: MET - README.md:3\nREVIEW: COMPLETE", False),
    ("REVIEW: INCOMPLETE\nR1: MET - app.py:2\nR2: MET - README.md:3\nREVIEW: COMPLETE", False),
    ("REVIEW: REJECT\nR1: MET - app.py:2\nR2: MET - README.md:3\nREVIEW: COMPLETE", False),
    ("R3: NOT MET - another goal condition fails\nR1: MET - app.py:2\nR2: MET - README.md:3\n"
     "REVIEW: COMPLETE", False),
], ids=["notes-complete", "no-findings", "no-verdict-form", "missing-requirement", "contradiction", "blocking",
        "unfinished", "review-incomplete", "review-reject", "uncovered-not-met"])
def test_an_approval_keeps_its_notes_and_never_waives_a_hold(reply, approves):
    """r07 on 820bed6: a complete approval with every covered requirement MET
    and only non-blocking notes was held because only a bare NO FINDINGS
    counted. It now approves where no revision can follow; a reply with no
    verdict form, a missing requirement line, a NOT MET line in either order
    or a BLOCKING finding never does; nor (root review of 6a1ff8a) does an
    UNFINISHED line, a REVIEW: line other than COMPLETE, or a NOT MET line
    for a requirement the task does not cover."""
    from quadratus.session import _approving_review
    assert _approving_review(reply, ["R1", "R2"]) is approves


def _gate(tmp_path, *argvs):
    from quadratus.integration import GateCommand, GateSuite
    return GateSuite([GateCommand(id=f"c{i}", argv=tuple(a)) for i, a in enumerate(argvs)], cwd=tmp_path)


@pytest.mark.parametrize("argvs,seat,applies", [
    ((), "openai:gpt-5.6-sol", False),
    ((("python", "-m", "pytest", "-q", "tests/test_existing.py"),), "openai:gpt-5.6-sol", False),
    ((("python", "-m", "pytest", "-q", "tests", "-k", "add"),), "openai:gpt-5.6-sol", False),
    ((("python", "-m", "pytest", "-q", "tests", "--quadratus-report={report}"),), "openai:gpt-5.6-sol", True),
    ((("python3", "-m", "unittest", "discover", "-s", "tests"),), "openai:gpt-5.6-sol", True),
    ((("python", "-m", "pytest", "-q", "tests"),), "claude:opus", False),
    ((("python3", "-m", "pytest", "-q", "/private/operator/PRIVATE-GRADING-SENTINEL"),), "openai:gpt-5.6-sol", False),
    ((("python", "-m", "pytest", "-q", "../outside"),), "openai:gpt-5.6-sol", False),
    ((("python", "-m", "pytest", "-q", "-c", "other.ini", "tests"),), "openai:gpt-5.6-sol", False),
    ((("python", "-m", "pytest", "-q", "missing"),), "openai:gpt-5.6-sol", False),
    ((("python3", "-m", "unittest", "discover", "-s", "/private/operator"),), "openai:gpt-5.6-sol", False),
], ids=["empty-suite", "explicit-file", "filtered", "discovering-pytest", "unittest-discover", "no-scratch-seat",
        "absolute-private", "traversal", "config-flag", "missing-dir", "unittest-absolute"])
def test_a_scratch_demonstration_is_asked_only_where_an_approved_command_would_run_it(
        tmp_path, argvs, seat, applies):
    """Root review of b24b6db: the probe was asked for with an empty suite or
    an explicit file list, where no approved command runs a new file. It now
    applies only to a seat whose copy takes scratch and an approved command
    that would discover a new test file: no filter, no explicit test file."""
    from tests.lifecycle.test_direct_tier import _unit_session
    session = _unit_session(tmp_path)
    (Path(session.project) / "tests").mkdir()
    session.config.integration_gate = _gate(Path(session.project), *argvs)
    assert session._probe_applies(seat) is applies


def _role_for(tmp_path, monkeypatch, *, role, checks, facts, probe):
    """The role text a real Fleet copy-bound call delivers, with a fake
    generation boundary (no provider)."""
    from quadratus.config import Settings
    from quadratus.delegation import invocation
    from quadratus.runtime import Fleet
    seen = {}
    monkeypatch.setattr(Fleet, "_generate", lambda self, key, provider, prompt, system, **kw:
                        seen.update(role=system, prompt=prompt) or "NO FINDINGS")
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    fleet = Fleet(Settings(backend="cli"), project=tmp_path)
    fleet.check_commands = tuple(checks)
    with invocation("t1", role, harness_checks=facts, probe=probe):
        fleet.invoke("openai:gpt-5.6-sol", "review this")
    return seen["role"]


@pytest.mark.parametrize("facts", ["- check passed on this source", None], ids=["recorded-checks", "no-records"])
@pytest.mark.parametrize("checks,probe,named", [
    (("python -m pytest -q tests", "python3 -m unittest discover -s tests"), True, True),
    (("python -m pytest -q tests/test_existing.py",), True, False),
    (("python -m pytest -q tests",), False, False),
    (("python3 -m pytest -q /private/operator/PRIVATE-GRADING-SENTINEL",), True, False),
    (("python -m pytest -q tests", "python3 -m pytest -q /private/operator/PRIVATE-GRADING-SENTINEL"), True, True),
], ids=["discovering", "explicit-file-only", "not-asked", "private-only", "private-beside-public"])
def test_the_runtime_names_the_one_approved_command_a_scratch_test_may_run(
        tmp_path, monkeypatch, facts, checks, probe, named):
    """Root review of b24b6db: with current harness records the copy-bound
    role replaced the command list with facts and said not to run the checks
    again, while the review prompt asked for a scratch run. The role now keeps
    the records and that rule, and adds one exception naming the approved
    commands that would discover a new scratch file, for that file only.
    Nothing is named when no approved command would run it, or when the call
    was not asked for a demonstration."""
    role = _role_for(tmp_path, monkeypatch, role="collaborator", checks=checks, facts=facts, probe=probe)
    exception = "ONE EXCEPTION, FOR A NEW SCRATCH TEST ONLY"
    assert (exception in role) is named
    if facts:
        assert "do not run the check commands again here" in role, "the recorded checks still stand"
    if named:
        tail = role.split(exception, 1)[1]
        assert "python -m pytest -q tests\n" in tail
        assert ("python3 -m unittest discover -s tests" in tail) is ("python3 -m unittest discover -s tests" in checks)
        assert "not to confirm the delivered source again" in tail
        assert "quadratus-report" not in role
    if probe:
        exception_text = role.split(exception, 1)[1] if exception in role else ""
        assert "PRIVATE-GRADING-SENTINEL" not in exception_text, "a private path is never named by the exception"


@pytest.mark.requirements_ledger
def test_the_review_seat_is_asked_for_a_scratch_run_and_the_auditor_never_sees_a_command(tmp_path, monkeypatch):
    """Whole controller: the codex review of goal-bearing work gets the scratch
    rule and the runtime exception naming the approved command; a claude or
    grok review gets neither; the auditor gets no probe and no command line
    (the standing rule: no command lines, paths or operands to the planner or
    auditor)."""
    from quadratus.session import _PROBE_RULE
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    seen = []
    from quadratus import runtime
    real = runtime.Fleet._generate

    def capture(self, key, provider, prompt, system, **kw):
        seen.append((key, system, prompt))
        return real(self, key, provider, prompt, system, **kw)
    monkeypatch.setattr(runtime.Fleet, "_generate", capture)
    declared = ("REQUIREMENTS:\nR1: " + _R1 + "\n"
                + DECL_SIMPLE.replace("backend simple", "backend complex") + "\nCOVERS: R1")

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead,
                                        collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:2",
                                        auditor=lambda c, r: "R1: MET - app.py",
                                        **{"requirements-review": lambda c, r: "COMPLETE"}),
          files=FILES, max_tasks=1, record_complete=False)
    reviews = [(k, r, p) for k, r, p in seen if "contributing an independent" in p]
    audits = [(k, r, p) for k, r, p in seen if "You are an independent auditor" in p]
    assert reviews and audits
    for key, role, prompt in reviews:
        codex = key.startswith("openai:")
        assert (_PROBE_RULE in prompt) is codex and ("ONE EXCEPTION, FOR A NEW SCRATCH TEST ONLY" in role) is codex
    assert any(k.startswith("openai:") for k, _, _ in reviews) and any(not k.startswith("openai:") for k, _, _ in reviews)
    for _, role, prompt in audits:
        assert _PROBE_RULE not in prompt and "ONE EXCEPTION" not in role and "-m pytest" not in prompt


# -- r09 on ded0095: parallel findings are rechecked on the merged source -----------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("shape,after_merge", [
    ("deferred", "RESOLVED"), ("deferred", "UNRESOLVED: add still missing"),
    ("blocking", "RESOLVED"), ("blocking", "UNRESOLVED: add still missing"),
])
def test_a_parallel_finding_on_sibling_work_is_rechecked_once_on_the_merged_source(
        tmp_path, monkeypatch, shape, after_merge):
    """r09: t3's reviewer blocked twice on HTTP routes that the still-running
    sibling t2 owned; the finding stayed open after the merge although the
    merged source held the routes, and the job stopped FindingsOpen. A child's
    reviewer now sees its siblings' paths and may write DEFERRED for their
    work; that, or a finding left unresolved after the child's own rounds, is
    held open and rechecked once by the same reviewer on the merged source
    after the merge gate passes. RESOLVED closes exactly those findings;
    UNRESOLVED keeps them. No lead call follows the recheck."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    reviews = []

    def collaborator(call, replay):
        reviews.append(call)
        if call.task == "t2" and shape == "deferred":
            return "DEFERRED: t1 - the documented add is not in app.py yet\nR2: MET - README.md:3\nREVIEW: COMPLETE"
        if call.task == "t2":
            return "BLOCKING: app.py does not define the add the README documents\nR2: MET - README.md:3"
        return "NO FINDINGS\nR1: MET - app.py:2"

    merged = []

    def recheck(call, replay):
        if "now merged" in call.prompt:
            merged.append(call)
            return after_merge
        return "UNRESOLVED: app.py still lacks add in this copy"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead,
                                                 collaborator=collaborator, recheck=recheck),
                   files=FILES, max_tasks=2, record_complete=False)
    assert len(merged) == 1 and merged[0].task == "t2", "one recheck on the merged source, by t2's reviewer"
    t2_review = next(r for r in reviews if r.task == "t2")
    assert "This task runs in parallel with" in t2_review.prompt and "- t1: may change only app.py" in t2_review.prompt
    rechecks = (H.result_json(replay).get("parallel_batches") or [{}])[0].get("post_merge_rechecks")
    assert rechecks and rechecks[0]["task"] == "t2"
    still_open = "FindingsOpen" in (replay.result.error or "")
    if after_merge == "RESOLVED":
        assert rechecks[0]["rechecked"][0]["verdict"] == "resolved" and not still_open
    else:
        assert rechecks[0]["rechecked"][0]["verdict"].startswith("kept") and still_open
    assert [c.task for c in replay.of("lead")].count("t2") == 1
    after = replay.calls[replay.calls.index(merged[0]) + 1:]
    assert not [c for c in after if c.role.startswith(("lead", "revision"))], "no editing call after the merge recheck"


def test_a_scratch_demonstration_is_measured_from_the_copy_not_read_from_the_reply(tmp_path, monkeypatch):
    """r09: the codex reviewer asked for a scratch demonstration only read
    source and reran the existing suites. Whether the call left a new test
    file in its copy is now measured by the runtime and handed back; the
    reply's own claim is never the evidence."""
    from quadratus.config import Settings
    from quadratus.delegation import invocation, invocation_context
    from quadratus.runtime import Fleet
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")

    def generate(self, key, provider, prompt, system, **kw):
        if "write" in prompt:
            (Path(provider.workdir) / "tests" / "test_scratch_refusal.py").write_text("def test_r():\n    pass\n")
        return "NO FINDINGS\nR1: MET - scratch run passed"
    monkeypatch.setattr(Fleet, "_generate", generate)
    fleet = Fleet(Settings(backend="cli"), project=tmp_path)
    fleet.check_commands = ("python -m pytest -q tests",)
    seen = {}
    for label, prompt in (("wrote", "write a scratch test"), ("claimed", "only claim it")):
        with invocation("t1", "collaborator", probe=True):
            fleet.invoke("openai:gpt-5.6-sol", prompt)
            seen[label] = (invocation_context.get() or {}).get("scratch_tests")
    assert seen["wrote"] == ["tests/test_scratch_refusal.py"]
    assert seen["claimed"] == [], "a claimed run with no new file in the copy is measured as none"
    assert not (tmp_path / "tests" / "test_scratch_refusal.py").exists(), "scratch never reaches the project"


# -- root on 40b2133: the parallel child never received the scratch rule -----------------

_SCRATCH = {
    "passes": "def test_scratch_refusal():\n    assert 1 + 1 == 2\n",
    "fails": "def test_scratch_refusal():\n    assert 1 + 1 == 3\n",
    "skipped": "import pytest\n\n\n@pytest.mark.skip(reason='scratch')\ndef test_scratch_refusal():\n    pass\n",
    "claimed": None,
}


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("variant,status", [
    ("passes", "passed"), ("fails", "failed"), ("skipped", "not run"), ("claimed", "not run"),
])
def test_a_parallel_childs_reviewer_gets_the_scratch_rule_and_the_harness_runs_its_test(
        tmp_path, monkeypatch, variant, status):
    """Root on 40b2133: r09's parallel codex reviewer never received
    _PROBE_RULE or the runtime exception, because _run_batch gives each child
    integration_gate=None and the rule required a gate. A child now reads the
    commands its parent approved, for the demonstration only, while its own
    gate stays off. The scratch test the reviewer leaves in its copy is run by
    the harness with the approved command, in that copy, and only a case of
    that file shown executed and passed is recorded as passed. A failing,
    skipped or absent scratch test stays unverified, and none of them is a
    MET or reaches the project."""
    from quadratus.delegation import invocation_context
    from quadratus.session import _PROBE_RULE, Session
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))
    gates = []
    real_run_task = Session.run_task

    def run_task(self, spec):
        if getattr(self, "_batch_siblings", None) is not None:
            gates.append((spec.task_id, self.config.integration_gate))
        return real_run_task(self, spec)
    monkeypatch.setattr(Session, "run_task", run_task)

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    asked = []

    def collaborator(call, replay):
        context = invocation_context.get() or {}
        if call.vendor == "codex" and call.task == "t2":
            asked.append((_PROBE_RULE in call.prompt, bool(context.get("probe")),
                           bool(context.get("probe_checks"))))
            if _SCRATCH[variant] is not None:
                H.write(call, {"tests/test_scratch_refusal.py": _SCRATCH[variant]})
            return "NO FINDINGS\nR2: MET - README.md:3 (scratch test_scratch_refusal executed and passed)"
        return "NO FINDINGS\n" + ("R1: MET - app.py:2" if call.task == "t1" else "R2: MET - README.md:3")

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead,
                                                 collaborator=collaborator),
                   files=FILES, max_tasks=2, record_complete=False)
    assert asked and asked[0] == (True, True, True), "the child's codex review carried the rule and the checks"
    assert sorted(t for t, _ in gates) == ["t1", "t2"] and all(g is None for _, g in gates), \
        "each child's own integration gate stays off"
    proofs = [p for p in H.result_json(replay).get("scratch_proofs") or [] if p["task"] == "t2"]
    expected = status if _sandboxed() or variant == "claimed" else "not run"
    assert len(proofs) == 1 and proofs[0]["status"] == expected, proofs
    if variant == "passes" and _sandboxed():
        assert proofs[0]["cases"] == ["tests/test_scratch_refusal.py::test_scratch_refusal: passed"]
        assert proofs[0]["receipts"] and all("command" not in r for r in proofs[0]["receipts"])
    if variant == "claimed":
        assert proofs[0]["files"] == [], "a claimed run with no file in the copy is measured as none"
    assert not (replay.project / "tests" / "test_scratch_refusal.py").exists(), "scratch never reaches the project"


def test_a_scratch_run_that_rewrites_handed_source_proves_nothing(tmp_path, monkeypatch):
    """The harness runs the scratch test in the copy; a test that changes a
    handed file while it runs is recorded not run, never passed."""
    import sys

    from quadratus.config import Settings
    from quadratus.delegation import invocation, invocation_context
    from quadratus.integration import GateCommand
    from quadratus.runtime import Fleet
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    scratch = ("from pathlib import Path\n\n\ndef test_scratch_refusal():\n"
               "    Path('app.py').write_text('x = 2\\n')\n")
    rewrote = {"rewrites": scratch, "clean": "def test_scratch_refusal():\n    assert True\n"}

    def generate(self, key, provider, prompt, system, **kw):
        (Path(provider.workdir) / "tests" / "test_scratch_refusal.py").write_text(rewrote[prompt])
        return "NO FINDINGS"
    monkeypatch.setattr(Fleet, "_generate", generate)
    fleet = Fleet(Settings(backend="cli"), project=tmp_path)
    check = GateCommand(id="check", argv=(sys.executable, "-m", "pytest", "-q", "tests"))
    seen = {}
    for label in rewrote:
        with invocation("t1", "collaborator", probe=True, probe_checks=(check,)):
            fleet.invoke("openai:gpt-5.6-sol", label)
            seen[label] = (invocation_context.get() or {}).get("scratch_proof")
    assert seen["clean"]["status"] == ("passed" if _sandboxed() else "not run")
    assert seen["rewrites"]["status"] == "not run"
    assert ("handed file changed" if _sandboxed() else "no sandbox") in seen["rewrites"]["reason"]
    assert (tmp_path / "app.py").read_text() == "x = 1\n"


def test_a_node_case_name_a_handed_test_also_declares_is_not_scratch_identity():
    """node prints case names without their file: a scratch case counts only
    when no handed test file declares the same name."""
    from quadratus.integration import GateReceipt, GateResult, case_record, scratch_receipt
    output = "✔ rejects bad input (1.0ms)\n✔ scratch only case (1.0ms)\n"
    receipt = GateReceipt(id="check", status="passed", reason="exit 0", required=True,
                          cases=case_record(output, "node"))
    result = GateResult(True, "gate suite", 0, output, (receipt,))
    handed = {"tests/app.test.js": b"test('rejects bad input', () => {})\n"}
    shared = {"tests/scratch.test.js": b"test('rejects bad input', () => {})\n"}
    unique = {"tests/scratch.test.js": b"test('scratch only case', () => {})\n"}
    assert scratch_receipt([result], shared, handed)["status"] == "not run"
    assert scratch_receipt([result], unique, handed)["status"] == "passed"
    failed = GateReceipt(id="check", status="failed", reason="nonzero exit", required=True,
                         cases=case_record("✖ scratch only case (1.0ms)\n", "node"))
    assert scratch_receipt([GateResult(False, "gate suite", 1, "", (failed,))], unique, handed)["status"] == "failed"


# -- root review of 1c14ea9: isolation and instance identity of the scratch run -----------

def _scratch_fleet(tmp_path, monkeypatch, checks):
    """A real Fleet whose codex review writes the scratch file its prompt
    names into its copy; returns run(source) -> scratch_proof."""
    import sys

    from quadratus.config import Settings
    from quadratus.delegation import invocation, invocation_context
    from quadratus.integration import GateCommand
    from quadratus.runtime import Fleet
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "app.py").write_text("x = 1\n")
    (project / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    monkeypatch.setattr("shutil.which", lambda name: f"/fake/bin/{name}")

    def generate(self, key, provider, prompt, system, **kw):
        (Path(provider.workdir) / "tests" / "test_scratch_refusal.py").write_text(prompt)
        return "NO FINDINGS"
    monkeypatch.setattr(Fleet, "_generate", generate)
    fleet = Fleet(Settings(backend="cli"), project=project)
    fleet.check_commands = tuple(checks)
    check = GateCommand(id="check", argv=(sys.executable, "-m", "pytest", "-q", "tests"))

    def run(source):
        with invocation("t1", "collaborator", probe=True, probe_checks=(check,)):
            fleet.invoke("openai:gpt-5.6-sol", source)
            return (invocation_context.get() or {}).get("scratch_proof")
    return project, run


def _sandboxed() -> bool:
    import os
    import platform
    return ((platform.system() == "Linux" and os.path.isfile("/usr/bin/bwrap"))
            or (platform.system() == "Darwin" and os.path.exists("/usr/bin/sandbox-exec")))


def test_a_scratch_run_writes_only_its_copy_and_cannot_read_private_or_live_paths(tmp_path, monkeypatch):
    """Root review of 1c14ea9: under the gate runner a reviewer's scratch test
    changed a marker outside its copy and the proof still said passed. The
    run now sits in a sandbox holding it to the reviewer's copy policy: the
    copy is the only writable root, the live project and every absolute path
    an approved check names outside the copy are unreadable, and no network.
    Each forbidden test below passes only if its action succeeds, so a
    passed proof would mean the boundary failed. Where no sandbox exists the
    run is not run, never executed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("held\n")
    private = tmp_path / "private-grading"
    private.mkdir()
    (private / "answers.txt").write_text("PRIVATE-ANSWER-SENTINEL\n")
    project, run = _scratch_fleet(tmp_path, monkeypatch, (
        "python -m pytest -q tests", f"python3 -m pytest -q {private}"))
    write = (f"from pathlib import Path\n\n\ndef test_scratch_refusal():\n"
             f"    Path({str(marker)!r}).write_text('modified\\n')\n")
    read_private = (f"from pathlib import Path\n\n\ndef test_scratch_refusal():\n"
                    f"    assert 'PRIVATE-ANSWER' in Path({str(private / 'answers.txt')!r}).read_text()\n")
    read_live = (f"from pathlib import Path\n\n\ndef test_scratch_refusal():\n"
                 f"    assert Path({str(project / 'app.py')!r}).read_text() == 'x = 1\\n'\n")
    positive = "def test_scratch_refusal():\n    assert 1 + 1 == 2\n"
    proofs = {name: run(source) for name, source in
              (("write", write), ("private", read_private), ("live", read_live), ("positive", positive))}
    assert marker.read_text() == "held\n", "a scratch run never writes outside its copy"
    for name in ("write", "private", "live"):
        assert proofs[name]["status"] != "passed", (name, proofs[name])
        assert "PRIVATE-ANSWER-SENTINEL" not in repr(proofs[name])
    if _sandboxed():
        assert all(proofs[n]["status"] == "failed" for n in ("write", "private", "live")), proofs
        assert proofs["positive"]["status"] == "passed", proofs["positive"]
    else:
        assert all(p["status"] == "not run" and "sandbox" in p["reason"] for p in proofs.values()), proofs
    assert not (project / "tests" / "test_scratch_refusal.py").exists()


@pytest.mark.parametrize("false_case,status", [
    ("    assert value or not value\n", "passed"),
    ("    if not value:\n        pytest.skip('false instance')\n", "not run"),
    ("    assert value\n", "failed"),
], ids=["all-pass", "false-skipped", "false-failed"])
def test_every_parametrized_instance_of_a_scratch_case_must_execute_and_pass(
        tmp_path, monkeypatch, false_case, status):
    """Root review of 1c14ea9: a parametrized scratch case whose True
    instance passed and False instance skipped was collapsed to its function
    and passed. Each instance keeps its own id: a skipped instance is not
    run, a failed one is failed, and only all instances passing is passed."""
    _, run = _scratch_fleet(tmp_path, monkeypatch, ("python -m pytest -q tests",))
    source = ("import pytest\n\n\n@pytest.mark.parametrize('value', [True, False])\n"
              "def test_every_case(value):\n" + false_case)
    proof = run(source)
    if not _sandboxed():
        assert proof["status"] == "not run"
        return
    assert proof["status"] == status, proof
    cases = proof["cases"]
    assert any("test_every_case[True]" in c for c in cases) and any("test_every_case[False]" in c for c in cases)


# -- r10 on f6c028c: a capped revision in the last slot meets its own reviewer -------------

_FINDING = "BLOCKING: add('x', 1) returns a string instead of raising TypeError naming the argument."


@pytest.mark.parametrize("case", ["resolved", "unresolved", "failing", "out-of-scope", "non-blocking"])
def test_a_capped_revision_in_the_last_slot_meets_the_reviewer_whose_findings_it_answered(
        tmp_path, monkeypatch, case):
    """r10: t3's capped Grok draft took the last-slot handoff to a review,
    which found a real CLI defect. The revision fixed it inside scope and
    the checks passed, but it also stopped at its cap and the task closed
    turn-limited with no recheck. Now that reviewer checks its own finding
    once on the revised source. RESOLVED lets the task close normally; any
    other verdict stays open. The writer is never called again. Failing
    checks, an out-of-scope edit or a note with no blocking finding keep the
    capped close exactly as before."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return H.grok_ok("I'll run the tests next", stop="cancelled", num_turns=8)

    revised = {"resolved": _TYPED, "unresolved": _TYPED, "non-blocking": _TYPED,
               "failing": "def add(a, b):\n    return a - b\n", "out-of-scope": _TYPED}

    def revision(call, replay):
        H.write(call, {"app.py": revised[case]})
        if case == "out-of-scope":
            H.write(call, {"README.md": "# app\n\nadd raises TypeError.\n"})
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    reviews, rechecks = [], []

    def collaborator(call, replay):
        reviews.append(call)
        if case == "non-blocking":
            return "Consider naming the argument in the TypeError message.\nREVIEW: COMPLETE"
        return _FINDING + "\nREVIEW: COMPLETE"

    def recheck(call, replay):
        rechecks.append(call)
        return "RESOLVED" if case == "resolved" else "UNRESOLVED: add('x', 1) still returns a string."

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_SIMPLE), lead=lead, revision=revision,
                                                 collaborator=collaborator, recheck=recheck),
                   files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8),
                   record_complete=False)
    result = H.result_json(replay)
    writers = [c for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix")]
    assert [c.role for c in writers] == ["lead", "revision"], "the capped writer is never called again"
    assert len(reviews) == 1
    if case in ("resolved", "unresolved"):
        assert len(rechecks) == 1 and rechecks[0].vendor == reviews[0].vendor, "the same reviewer, once"
        assert rechecks[0].model == reviews[0].model
        assert _FINDING in rechecks[0].prompt and "stopped at its turn limit" in rechecks[0].prompt
        assert "while revising" in rechecks[0].prompt
        assert result["turn_limited_tasks"] == [], "the rechecked task is not unfinished debt"
        assert [c.role for c in replay.calls][:6] == ["orchestrator", "lead", "collaborator", "revision",
                                                      "recheck", "closeout"]
        if case == "resolved":
            assert replay.result.completed, "completed only on the orchestrator's answer at the cap"
        else:
            assert not replay.result.completed and "FindingsOpen" in (replay.result.error or ""), \
                "an unresolved finding stays open"
        return
    assert not rechecks, "no recheck for work that is not the last slot's checked, in-scope source"
    assert not replay.result.completed
    if case == "out-of-scope":
        assert result["failed_tasks"] == ["t1"], "the scope breach still fails the task"
    else:
        assert result["turn_limited_tasks"] == ["t1"], "the capped close applies as before"


def test_a_capped_fix_in_the_last_slot_meets_the_reviewer_whose_finding_stayed_open(tmp_path, monkeypatch):
    """r10, the fix-cycle form: the revision finished, its recheck came back
    UNRESOLVED, and the one fix stopped at its cap. The reviewer whose finding
    stayed open checks it once on the fixed source; no further fix follows."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return H.grok_ok("I'll run the tests next", stop="cancelled", num_turns=8)

    def revision(call, replay):
        if len(replay.of("revision")) == 1:
            H.write(call, {"app.py": "def add(a, b):\n    return a + b  # still unchecked\n"})
            return 'Revised add.\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": _TYPED})
        return H.grok_ok("Now checking the TypeError path", stop="cancelled", num_turns=8)

    rechecks = []

    def recheck(call, replay):
        rechecks.append(call)
        return "RESOLVED" if "while revising" in call.prompt else "UNRESOLVED: add('x', 1) still returns a string."

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_SIMPLE), lead=lead, revision=revision,
                                                 collaborator=lambda c, r: _FINDING + "\nREVIEW: COMPLETE",
                                                 recheck=recheck),
                   files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8),
                   record_complete=False)
    roles = [c.role for c in replay.calls]
    assert roles.count("revision") == 2 and roles.count("recheck") == 2, roles
    assert "while revising" in rechecks[1].prompt and _FINDING in rechecks[1].prompt
    assert len({(c.vendor, c.model) for c in rechecks}) == 1, "the same reviewer both times"
    assert H.result_json(replay)["turn_limited_tasks"] == [] and replay.result.completed


# -- r11 on 136a3ec: a revision that crosses the size stop -------------------------------

_BLOATED = _TYPED + "".join(f"\n\ndef _unused_{i}():\n    return {i}\n" for i in range(20))


@pytest.mark.parametrize("case", ["trimmed", "still-over", "out-of-scope", "fix-overrun"])
def test_a_revision_past_the_size_stop_spends_the_remaining_fix_allowance_bringing_it_back(
        tmp_path, monkeypatch, case):
    """r11: t2's in-bound draft was revised past the hard stop by one line
    and the task failed with a fix cycle unused and no slot left. A size-only
    overrun with a cycle left now spends that cycle on one call to bring the
    change back under the stop; nothing is reverted and the stop is measured
    again. Still over, a path outside scope, or no cycle left fails the task
    as before. The fix prompt now carries the same room as the revision."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    declared = DECL_SIMPLE.replace("backend simple", "backend standard")

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def revision(call, replay):
        n = len(replay.of("revision"))
        if case == "fix-overrun":
            H.write(call, {"app.py": _TYPED if n == 1 else _BLOATED})
        elif n == 1:
            H.write(call, {"app.py": _BLOATED})
            if case == "out-of-scope":
                H.write(call, {"README.md": "# app\n\nadd raises TypeError.\n"})
        else:
            H.write(call, {"app.py": _TYPED if case == "trimmed" else _BLOATED + "\n# still here\n"})
        return 'Revised add.\nCHANGED: ["app.py"]'

    def recheck(call, replay):
        if case == "fix-overrun" and len(replay.of("recheck")) == 1:
            return "UNRESOLVED: the TypeError message does not name the argument."
        return "RESOLVED"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead, revision=revision,
                                                 collaborator=lambda c, r: _FINDING + "\nREVIEW: COMPLETE",
                                                 recheck=recheck),
                   files=FILES, max_tasks=1, record_complete=False)
    result = H.result_json(replay)
    writers = [c for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix")]
    if case == "trimmed":
        assert [c.role for c in writers] == ["lead", "revision", "revision"], "one call from the allowance"
        assert "past the run's stop at 60" in writers[2].prompt and "nothing was reverted" in writers[2].prompt
        assert result["failed_tasks"] == [] and replay.of("recheck"), "the trimmed source meets the recheck"
        assert replay.result.completed
        return
    assert result["failed_tasks"] == ["t1"], "the stop still fails the task"
    assert not replay.result.completed
    if case == "still-over":
        assert [c.role for c in writers] == ["lead", "revision", "revision"]
    elif case == "out-of-scope":
        assert [c.role for c in writers] == ["lead", "revision"], "a path breach buys no trim"
    else:
        assert [c.role for c in writers] == ["lead", "revision", "revision"], "no cycle left, no trim"
        assert "lines of room for this revision" in writers[2].prompt, "the fix is told its room"


# -- r11: every editing call carries the original goal, not only the generated task --------

_CONTRACT = "A non-number argument must raise TypeError naming the argument."
_RECIPE = ("Implement add in app.py. Keep the existing behaviour for bad input: it already "
           "satisfies the refusal, so add no type check.")


@pytest.mark.parametrize("path", ["revision-and-fix", "gate-fix"])
def test_every_editing_call_after_the_draft_carries_the_original_goal_over_a_generated_recipe(
        tmp_path, monkeypatch, path):
    """r11: t2's revision prompt held the generated task text, which
    prescribed the defective behaviour, and not the original goal; only the
    draft had the goal, through the memory render. Revision, fix and gate-fix
    prompts now carry the goal and the covered requirements verbatim, and
    say that task text or a generated recipe does not change them. It grants
    nothing: the scope line is unchanged."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    goal = "Implement add(a, b) in app.py. " + _CONTRACT
    declared = ("KIND: backend standard\nSCOPE: " + json.dumps(T1) + "\n" + _RECIPE)

    def lead(call, replay):
        H.write(call, {"app.py": fixed if path == "revision-and-fix" else "def add(a, b):\n    return a - b\n"})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def revision(call, replay):
        H.write(call, {"app.py": _TYPED})
        return 'Revised add.\nCHANGED: ["app.py"]'

    def gate_fix(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Fixed add.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if path == "gate-fix":
            return "NO FINDINGS"
        return "BLOCKING: add('x', 1) does not raise TypeError naming the argument.\nREVIEW: COMPLETE"

    def recheck(call, replay):
        return "UNRESOLVED: still no TypeError." if len(replay.of("recheck")) == 1 else "RESOLVED"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead, revision=revision,
                                                 collaborator=collaborator, recheck=recheck,
                                                 **{"gate-fix": gate_fix}),
                   files=FILES, max_tasks=1, goal=goal, record_complete=False)
    assert _CONTRACT not in declared, "the task text omits, and contradicts, the goal's condition"
    later = [c for c in replay.calls if c.role in ("revision", "gate-fix")]
    expected = ["revision", "revision"] if path == "revision-and-fix" else ["gate-fix"]
    assert [c.role for c in later] == expected
    for call in later:
        assert _CONTRACT in call.prompt and "## Original goal (verbatim" in call.prompt, call.role
        assert "a generated plan or recipe" in call.prompt and "does not change them" in call.prompt
        assert "It grants nothing" in call.prompt


@pytest.mark.requirements_ledger
def test_a_parallel_childs_draft_carries_the_requirements_it_covers_verbatim(tmp_path, monkeypatch):
    """r11: a parallel child's ledger is off, so its draft had the goal but not
    the original text of the requirements its task covers. It now does."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead,
                                                 collaborator=lambda c, r: "NO FINDINGS\n" + (
                                                     "R1: MET - app.py:2" if c.task == "t1" else
                                                     "R2: MET - README.md:3")),
                   files=FILES, max_tasks=2, record_complete=False)
    drafts = {c.task: c.prompt for c in replay.of("lead")}
    assert "## Original requirements this task covers (verbatim)\n\nR1: " + _R1 in drafts["t1"]
    assert "R2: README documents add" in drafts["t2"] and "R1: " + _R1 not in drafts["t2"].split(
        "## Original requirements this task covers (verbatim)", 1)[1].split("\n\n", 2)[1]


@pytest.mark.parametrize("role", ["revision", "gate-fix", "security-fix", "design-fix", "fixture-supply",
                                  "capture-redeclare", "lead"])
def test_every_editing_role_after_the_draft_is_held_to_the_goal_at_the_shared_boundary(tmp_path, role):
    """Root review of af5814d: design-fix, security-fix and fixture-supply
    prompts still carried no original goal; only the builders patched in
    af5814d did. The goal and covered requirements now enter once, at the
    boundary where every editing role already gets the lead's packet, so
    no route can miss them and none carries them twice. The draft ('lead')
    already has the goal through the memory render and gets no second copy
    here; grants and the call itself are unchanged."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.delegation import invocation
    from quadratus.session import Session, SessionConfig, TaskSpec
    seen = []

    def invoke(key, prompt, *, allow_writes=False):
        seen.append((prompt, allow_writes))
        return 'Done.\nCHANGED: []'

    goal = "Implement add(a, b) in app.py. " + _CONTRACT
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    session = Session(goal, ArtifactStore(tmp_path / ".quadratus"), invoke, available=lambda key: True,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    session._active_spec = TaskSpec("t1", _RECIPE)
    session._current_covers = ["R1"]
    session._requirement_texts = {"R1": _CONTRACT}
    if role in ("revision", "gate-fix", "security-fix", "design-fix"):
        session._edit("anthropic:claude-opus-5", "Task: " + _RECIPE + "\n\nFix it.", role=role)
    else:
        with invocation("t1", role):
            session._invoke_model("anthropic:claude-opus-5", "Task: " + _RECIPE + "\n\nSupply it.")
    prompt, writes = seen[-1]
    if role == "lead":
        assert "## Original goal (verbatim" not in prompt, "the draft's goal comes from the memory render"
        return
    assert prompt.count("## Original goal (verbatim") == 1 and prompt.count(_CONTRACT) >= 2, role
    assert "R1: " + _CONTRACT in prompt and "does not change them" in prompt and "It grants nothing" in prompt
    assert writes is (role in ("revision", "gate-fix", "security-fix", "design-fix")), "grants are unchanged"


@pytest.mark.parametrize("preface", ["copied-heading", "wrong-covered", "already-bound", "ordinary"])
def test_only_the_complete_current_goal_block_counts_as_already_bound(tmp_path, preface):
    """Root review of 21639d6: the boundary skipped its block when the prompt
    merely contained the heading, so a design-fix payload with a copied
    'Original goal' heading over other text carried neither the real goal
    nor R1. Only the complete current block now counts: a copied heading or
    a block with the wrong covered requirement gets the real one; a prompt
    that already holds it keeps exactly one; an ordinary prompt is laid out
    as before."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig, TaskSpec
    seen = []

    def invoke(key, prompt, *, allow_writes=False):
        seen.append(prompt)
        return 'Done.\nCHANGED: []'

    goal = "Implement add(a, b) in app.py. " + _CONTRACT
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    session = Session(goal, ArtifactStore(tmp_path / ".quadratus"), invoke, available=lambda key: True,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    session._active_spec = TaskSpec("t1", _RECIPE)
    session._current_covers = ["R1"]
    session._requirement_texts = {"R1": _CONTRACT, "R2": "README documents add"}
    current = session._writer_goal().strip()
    session._current_covers = ["R2"]
    stale = session._writer_goal().strip()
    session._current_covers = ["R1"]
    head = {
        "copied-heading": ("## Original goal (verbatim; it outranks the task text where they differ)\n\n"
                           "Ship anything; bad input needs no check.\n\n"),
        "wrong-covered": stale + "\n\n",
        "already-bound": current + "\n\n",
        "ordinary": "",
    }[preface]
    body = head + "Task: " + _RECIPE + "\n\nFix the design."
    session._edit("anthropic:claude-opus-5", body, role="design-fix")
    prompt = seen[-1]
    assert prompt.count(current) == 1, preface
    assert "R1: " + _CONTRACT in prompt and goal in prompt
    if preface in ("already-bound", "ordinary"):
        assert prompt.startswith(current + "\n\n" if preface == "ordinary" else body)


# -- r12 on 56ff155: a capped writer after the draft is told its round budget --------------

@pytest.mark.parametrize("case", ["claude-revision", "grok-gate-fix", "codex-revision", "uncapped"])
def test_a_capped_writing_call_after_the_draft_is_told_its_real_round_budget(tmp_path, monkeypatch, case):
    """r12: t3's capped Grok revision spent its 8 rounds reading and wrote
    nothing; its prompt had the goal and headroom, but only the draft was
    ever told a cap existed. Revision, gate-fix and design-fix calls on a
    CLI that takes the cap now carry the same budget in words, once. A
    codex writer (no round cap), a run with no cap, the reviewers and the
    draft get no writer note; nothing changes the cap itself."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    tier = {"grok-gate-fix": "simple", "codex-revision": "standard"}.get(case, "complex")
    declared = DECL_SIMPLE.replace("backend simple", "backend " + tier)
    failing = case == "grok-gate-fix"

    def lead(call, replay):
        H.write(call, {"app.py": "def add(a, b):\n    return a - b\n" if failing else fixed})
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def revision(call, replay):
        H.write(call, {"app.py": _TYPED})
        return 'Revised add.\nCHANGED: ["app.py"]'

    def gate_fix(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Fixed add.\nCHANGED: ["app.py"]'

    review = "NO FINDINGS" if failing else _FINDING + "\nREVIEW: COMPLETE"
    settings = Settings(backend="cli", lead_max_turns=None if case == "uncapped" else 8)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead, revision=revision,
                                                 collaborator=lambda c, r: review,
                                                 recheck=lambda c, r: "RESOLVED", **{"gate-fix": gate_fix}),
                   files=FILES, max_tasks=1, settings=settings, record_complete=False)
    note = ("This call has at most 8 tool rounds. At round 8 this call stops; the edits already on disk are "
            "kept, and the task's checks, its reviewers and the final audit still decide what happens to them.")
    writers = [c for c in replay.calls if c.role in ("revision", "gate-fix")]
    assert [c.role for c in writers] == (["gate-fix"] if failing else ["revision"])
    others = [c for c in replay.calls if c.role in ("lead", "collaborator", "recheck")]
    assert others and all(note not in c.prompt for c in others), "no writer note outside the writer roles"
    draft = replay.of("lead")[0]
    capped_cli = draft.vendor in ("grok", "claude")
    assert draft.prompt.count("## Your round budget") == (1 if capped_cli and case != "uncapped" else 0)
    if case in ("claude-revision", "grok-gate-fix"):
        assert writers[0].vendor == ("grok" if failing else "claude")
        assert writers[0].prompt.count(note) == 1, "the real cap, once"
        assert "have the edit written by about round 5" in writers[0].prompt
    else:
        assert all(note not in c.prompt and "## Your round budget" not in c.prompt for c in writers)
        if case == "codex-revision":
            assert writers[0].vendor == "codex", "a CLI with no round cap is told none"


@pytest.mark.parametrize("role,key,writes,told", [
    ("design-fix", "claude:opus", True, True),
    ("security-fix", "claude:opus", True, False),
    ("design-fix", "openai:gpt-5.6-sol", True, False),
    ("design-fix", "claude:opus", False, False),
])
def test_the_writer_budget_note_follows_the_cap_the_fleet_applies(tmp_path, role, key, writes, told):
    """At the editing boundary: the note goes only where the runtime caps the
    call (LEAD_CAPPED_ROLES on a CLI with a round flag) and the call can
    write. security-fix is not capped and codex has no round cap, so neither
    is told one."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig, TaskSpec
    seen = []

    def invoke(k, prompt, *, allow_writes=False):
        seen.append(prompt)
        return 'Done.\nCHANGED: []'

    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    session = Session("Implement add.", ArtifactStore(tmp_path / ".quadratus"), invoke, available=lambda k: True,
                      config=SessionConfig(project=tmp_path, allow_writes=writes, lead_max_turns=8))
    session._active_spec = TaskSpec("t1", "Implement add.")
    session._edit(key, "Task: Implement add.\n\nFix it.", role=role)
    assert ("This call has at most 8 tool rounds" in seen[-1]) is told
    if told:
        assert "the edits already on disk are kept" in seen[-1] and "unreviewed" not in seen[-1]
        assert "unfinished" not in seen[-1] and "unchecked" not in seen[-1], "no status promise"
        assert "start with the files they name" in seen[-1] and "read only" not in seen[-1], \
            "a priority, not a narrower read authority"


# -- r13 on 5e8926b: a finished draft past the size stop ----------------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["trimmed", "still-over", "path-breach", "capped-draft", "exhausted", "unresolved"])
def test_a_finished_draft_past_the_size_stop_spends_one_fix_cycle_coming_back_under_it(
        tmp_path, monkeypatch, case):
    """r13: two finished parallel drafts crossed the hard stop inside their
    paths and failed before any check or peer read them. A size-only overrun
    of a finished draft now spends one cycle of the task's own fix allowance
    on one call to come back under the stop; that cycle is charged against
    the later revision and fixes. The trimmed source still meets the checks,
    the peer and the final audit. Still over, a path breach, a capped draft,
    an exhausted allowance or an unresolved finding all hold as before."""
    tier = "standard" if case != "capped-draft" else "simple"
    declared = ("REQUIREMENTS:\nR1: " + _R1 + "\n"
                + DECL_SIMPLE.replace("backend simple", "backend " + tier) + "\nCOVERS: R1")

    def lead(call, replay):
        H.write(call, {"app.py": _BLOATED})
        if case == "path-breach":
            H.write(call, {"README.md": "# app\n\nadd.\n"})
        if case == "capped-draft":
            return H.grok_ok("I'll trim it next", stop="cancelled", num_turns=8)
        return 'Implemented add.\nCHANGED: ["app.py"]'

    def revision(call, replay):
        n = len(replay.of("revision"))
        if n == 1:
            H.write(call, {"app.py": _BLOATED + "\n# kept\n" if case == "still-over" else _TYPED})
        else:
            H.write(call, {"app.py": _BLOATED if case == "exhausted" else _TYPED + "\n# revised\n"})
        return 'Revised add.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if case in ("exhausted", "unresolved"):
            return _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE"
        return "NO FINDINGS\nR1: MET - app.py:5"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(declared), lead=lead, revision=revision,
                                                 collaborator=collaborator,
                                                 recheck=lambda c, r: "UNRESOLVED: add('x', 1) still returns.",
                                                 auditor=lambda c, r: "R1: MET - app.py:5",
                                                 **{"requirements-review": lambda c, r: "COMPLETE"}),
                   files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8),
                   record_complete=False)
    result = H.result_json(replay)
    writers = [c for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix")]
    roles = [c.role for c in writers]
    if case == "trimmed":
        assert roles == ["lead", "revision"] and "Your draft made this task's change" in writers[1].prompt
        assert "Do not remove behaviour the task or the goal requires" in writers[1].prompt
        # R15: the trim aims at the stated bound; the stop stays the failure line.
        assert "Aim for about 40 lines, the task's stated bound; the run stops at 60" in writers[1].prompt
        assert "at most 60 lines" not in writers[1].prompt
        assert result["failed_tasks"] == [] and replay.of("collaborator"), "the trimmed draft meets its peer"
        assert replay.of("auditor"), "and the final audit"
        assert (replay.project / "app.py").read_text() == _TYPED
        return
    if case == "exhausted":
        assert roles == ["lead", "revision", "revision"], "trim, then a revision that overruns: no second trim"
        assert result["failed_tasks"] == ["t1"] and not replay.result.completed
        return
    if case == "unresolved":
        assert roles == ["lead", "revision", "revision"], "trim, then the one revision the allowance leaves"
        assert len(replay.of("recheck")) == 1 and "FindingsOpen" in (replay.result.error or "")
        return
    if case == "capped-draft":
        # R16 on 78f76bb: a capped draft's in-path overrun now gets the same
        # one trim, stating the cap (test_a_capped_draft_past_the_size_stop...),
        # and the trimmed source meets the last-slot capped review.
        assert roles == ["lead", "revision"] and "stopped at its 8-round limit" in writers[1].prompt
        assert result["failed_tasks"] == [] and replay.of("collaborator")
        return
    assert not replay.result.completed and result["failed_tasks"] == ["t1"]
    assert not replay.of("collaborator"), "no peer reads source past the stop"
    if case == "still-over":
        assert roles == ["lead", "revision"]
    elif case == "path-breach":
        assert roles == ["lead"], "no trim"
    else:
        raise AssertionError(case)


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("fits", [True, False], ids=["trimmed", "still-over"])
def test_a_parallel_childs_finished_draft_past_the_stop_is_trimmed_inside_the_child(tmp_path, monkeypatch, fits):
    """r13's actual shape: the overrun drafts were parallel children. The
    child spends its own task's cycle; trimmed, it merges and its review
    still runs; still over, it fails exactly as before."""
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))
    bloated = _TYPED + "".join(f"\n\ndef _unused_{i}():\n    return {i}\n" for i in range(8))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": bloated})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def revision(call, replay):
        H.write(call, {"app.py": _TYPED if fits else bloated + "\n# kept\n"})
        return 'Trimmed add.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead, revision=revision,
                                                 collaborator=lambda c, r: "NO FINDINGS\n" + (
                                                     "R1: MET - app.py:5" if c.task == "t1" else
                                                     "R2: MET - README.md:3")),
                   files=FILES, max_tasks=2, record_complete=False)
    result = H.result_json(replay)
    trims = [c for c in replay.calls if c.role == "revision" and c.task == "t1"]
    assert len(trims) == 1 and "Your draft made this task's change" in trims[0].prompt
    reviewed = {c.task for c in replay.of("collaborator")}
    if fits:
        assert "t1" not in result["failed_tasks"] and "t1" in reviewed, "merged and reviewed"
        assert (replay.project / "app.py").read_text() == _TYPED
    else:
        assert "t1" in result["failed_tasks"] and "t1" not in reviewed


# -- R14 on 164859f: the cap goal question is the goal decision, not a second audit -------

_CAP = "task cap for this run has been reached"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["done-met", "fetch-then-done", "not-done", "audit-not-met", "hedged-done"])
def test_the_cap_goal_question_lists_full_work_by_id_and_the_audit_still_decides(tmp_path, monkeypatch, case):
    """R14: the cap question rendered every task's full work inline (43k
    characters) to an uncapped seat that also read the delivered files; its
    183k tokens left the final audit below the call reserve. The question
    now lists each task's full work by artifact id, FETCH serves any of them
    whole, and it says the independent audit follows a DONE. The goal,
    requirements and harness records are rendered as before. NOT DONE, a
    hedged DONE or an audit NOT MET still hold the run."""
    import re

    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    declared = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_T1 + "\nCOVERS: R1"
    caps = []

    def orchestrator(call, replay):
        if _CAP not in call.prompt:
            return declared if not replay.of("orchestrator")[:-1] else "DONE"
        caps.append(call.prompt)
        if case == "fetch-then-done" and len(caps) == 1:
            ids = re.findall(r"artifact ([0-9a-f]{12}) \(draft\)", call.prompt)
            return "FETCH: " + ids[0]
        return {"not-done": "NOT DONE: R1 is unchecked",
                "hedged-done": "DONE, though R1 is unchecked"}.get(case, "DONE")

    def lead(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Implemented add with the sum.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead,
        collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:2",
        auditor=lambda c, r: "R1: NOT MET - no test of a negative sum" if case == "audit-not-met"
        else "R1: MET - app.py:2",
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=1, record_complete=False)
    assert caps, "the cap question was asked"
    first = caps[0]
    assert "## Goal (verbatim, unchanged)" in first and "R1" in first and _R1 in first
    assert "**Harness record" in first, "measured facts still rendered"
    assert "**Full work:** artifact" in first and "shown in full" not in first, "full work by id"
    assert "Implemented add with the sum." not in first, "no inline artifact text"
    assert "your DONE does not stand in for that check" in first
    audited = bool(replay.of("auditor"))
    if case == "fetch-then-done":
        assert len(caps) == 2 and "Implemented add with the sum." in caps[1], "FETCH serves the original whole"
    if case in ("done-met", "fetch-then-done"):
        assert audited and replay.result.completed
    elif case == "audit-not-met":
        assert audited and not replay.result.completed, "the audit, not the goal answer, decides"
    else:
        assert not audited and not replay.result.completed, "no DONE, no audit, no completion"


@pytest.mark.requirements_ledger
def test_the_final_audit_is_admitted_exactly_at_the_unchanged_call_reserve(tmp_path, monkeypatch):
    """R14's stop: the audit was refused because the remaining reported
    budget sat below the ordinary-call reserve. The reserve is unchanged:
    with exactly the reserve left the auditor is invoked, one token less
    and it is refused with the run held incomplete."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    declared = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_T1 + "\nCOVERS: R1"
    reserve = 1000

    def run(where, total):
        def orchestrator(call, replay):
            if _CAP in call.prompt:
                return "DONE"
            return declared if not replay.of("orchestrator")[:-1] else "DONE"

        def lead(call, replay):
            H.write(call, {"app.py": fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        where.mkdir()
        return H.run(where, monkeypatch, Script(
            orchestrator=orchestrator, lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:2",
            auditor=lambda c, r: "R1: MET - app.py:2", **{"requirements-review": lambda c, r: "COMPLETE"}),
            files=FILES, max_tasks=1, record_complete=False,
            limits=RunLimits(max_calls=120, max_reported_tokens=total, wall_seconds=600,
                             max_concurrent_workers=2, reserve_tokens_per_call=reserve))

    calibration = run(tmp_path / "calibrate", 6_000_000)
    before = next(i for i, c in enumerate(calibration.calls) if c.role == "auditor")
    used = before * (H.USAGE["input_tokens"] + H.USAGE["output_tokens"])
    admitted = run(tmp_path / "at", used + reserve)
    refused = run(tmp_path / "below", used + reserve - 1)
    assert admitted.of("auditor") and admitted.result.completed
    assert not refused.of("auditor") and not refused.result.completed
    assert "reported_token_reserve" in (refused.result.error or "") or "RunBudgetExceeded" in (
        refused.result.error or "")


# -- R15 on 7f628ae: a capped revision that changed nothing --------------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["retried-fixed", "edited-capped", "capped-again", "no-cycle", "non-blocking"])
def test_a_capped_revision_that_changed_nothing_gets_one_measured_retry_from_the_allowance(
        tmp_path, monkeypatch, case):
    """R15: t3's capped revision had a concrete blocking finding, read for all
    8 rounds and wrote nothing; the task closed turn-limited with a fix cycle
    unused. A capped revision that left the source byte-identical, with a
    blocking finding open and a cycle left, now gets one more revision from
    that cycle carrying what the harness measured. The recheck reads the
    source (RESOLVED only if the fix is there), and the audit still decides.
    An edit made before the cap, a second no-edit cap, no cycle left, or no
    blocking finding keep the existing capped paths."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    declared = ("REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_T1 + "\nCOVERS: R1")

    def lead(call, replay):
        H.write(call, {"app.py": _BLOATED if case == "no-cycle" else fixed})
        return H.claude_ok('Implemented add.\nCHANGED: ["app.py"]')

    def revision(call, replay):
        n = len(replay.of("revision"))
        if case == "no-cycle" and n == 1:
            H.write(call, {"app.py": fixed})                      # the draft trim
            return H.claude_ok('Trimmed.\nCHANGED: ["app.py"]')
        if case == "edited-capped":
            H.write(call, {"app.py": _TYPED})
            return H.claude_cap("Checking the TypeError path", num_turns=8)
        if case == "retried-fixed" and n == 2:
            H.write(call, {"app.py": _TYPED})
            return H.claude_ok('Added the TypeError.\nCHANGED: ["app.py"]')
        return H.claude_cap("Still reading app.py", num_turns=8)

    def collaborator(call, replay):
        if case == "non-blocking":
            return "Consider naming the argument.\nR1: MET - app.py:2\nREVIEW: COMPLETE"
        return _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE"

    def recheck(call, replay):
        source = (Path(call.cwd) / "app.py").read_text()
        return "RESOLVED" if "TypeError" in source else "UNRESOLVED: add('x', 1) still returns a string."

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(declared), lead=lead, revision=revision, collaborator=collaborator, recheck=recheck,
        auditor=lambda c, r: "R1: MET - app.py:2", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    writers = [c for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix")]
    roles = [c.role for c in writers]
    measured = "changed no file: the source is byte-identical to before it"
    if case == "retried-fixed":
        assert roles == ["lead", "revision", "revision"] and measured in writers[2].prompt
        assert _FINDING in writers[2].prompt, "the same findings, with the measured outcome"
        assert replay.of("recheck") and replay.of("auditor") and replay.result.completed
        assert (replay.project / "app.py").read_text() == _TYPED
        return
    if case == "capped-again":
        assert roles == ["lead", "revision", "revision"] and measured in writers[2].prompt
        assert measured not in writers[1].prompt and not replay.result.completed, "no third call"
        return
    assert all(measured not in c.prompt for c in writers), "no measured retry"
    if case != "edited-capped":
        assert not replay.result.completed
    expected = {"edited-capped": ["lead", "revision"], "no-cycle": ["lead", "revision", "revision"],
                "non-blocking": ["lead", "revision"]}[case]
    assert roles == expected, roles


@pytest.mark.requirements_ledger
def test_a_parallel_childs_capped_no_edit_revision_gets_the_same_measured_retry(tmp_path, monkeypatch):
    """R15's shape: t3 was a parallel child. The child spends its own task's
    remaining cycle; the fixed source merges and the task is not left
    turn-limited."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def revision(call, replay):
        if len([c for c in replay.of("revision") if c.task == "t1"]) == 1:
            return H.grok_ok("Reading app.py", stop="cancelled", num_turns=8)
        H.write(call, {"app.py": _TYPED})
        return 'Added the TypeError.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if call.task == "t1":
            return _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE"
        return "NO FINDINGS\nR2: MET - README.md:3"

    def recheck(call, replay):
        return "RESOLVED" if "TypeError" in (Path(call.cwd) / "app.py").read_text() else "UNRESOLVED: no TypeError"

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead, revision=revision,
                                                 collaborator=collaborator, recheck=recheck),
                   files=FILES, max_tasks=2, settings=Settings(backend="cli", lead_max_turns=8),
                   record_complete=False)
    t1 = [c for c in replay.of("revision") if c.task == "t1"]
    assert len(t1) == 2 and "changed no file" in t1[1].prompt
    assert "t1" not in H.result_json(replay)["turn_limited_tasks"]
    assert (replay.project / "app.py").read_text() == _TYPED, "the fix merged"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("shape", ["serial", "parallel"])
def test_a_capped_trim_is_never_taken_for_a_no_edit_revision(tmp_path, monkeypatch, shape):
    """Root review of b5ea0ef: the first revision overran the stop and its
    trim, the task's second cycle, restored the pre-revision source and then
    stopped at its cap. The cap escaped before the trim was counted, the
    source matched the mark taken before the revision, and the no-edit retry
    bought a third writer call with the default two cycles. A cap raised
    inside a trim now keeps the existing capped paths: two writer calls."""
    from quadratus.session import SessionConfig
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    assert SessionConfig().max_fix_cycles == 2, "the default allowance this guards"
    revisions = []

    def revision(call, replay):
        n = len([c for c in replay.of("revision") if c.task == "t1"])
        revisions.append(call)
        if n == 1:
            H.write(call, {"app.py": fixed + "".join(f"# note {i}\n" for i in range(80))})
            return 'Revised add.\nCHANGED: ["app.py"]'
        if n == 2:
            H.write(call, {"app.py": fixed})
            if call.vendor == "claude":
                return H.claude_cap("Trimming the comments", num_turns=8)
            return H.grok_ok("Trimming the comments", stop="cancelled", num_turns=8)
        H.write(call, {"app.py": _TYPED})
        return 'Added the TypeError.\nCHANGED: ["app.py"]'

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": fixed})
            return 'Implemented add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collaborator(call, replay):
        if call.task == "t1":
            return _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE"
        return "NO FINDINGS\nR2: MET - README.md:3"

    def recheck(call, replay):
        return "RESOLVED" if "TypeError" in (Path(call.cwd) / "app.py").read_text() else "UNRESOLVED: no TypeError"

    if shape == "serial":
        plan = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_SIMPLE + "\nCOVERS: R1"
        plan = plan.replace("backend simple", "backend complex")
        max_tasks = 1
    else:
        plan = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
                "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
                + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))
        max_tasks = 2
    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(plan), lead=lead, revision=revision, collaborator=collaborator, recheck=recheck,
        auditor=lambda c, r: "R1: MET - app.py:2\nR2: MET - README.md:3",
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=max_tasks, settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    assert revisions[1].vendor in ("claude", "grok"), "the trim ran on a CLI that takes the round cap"
    assert len(revisions) == 2, f"revision and its trim only, got {len(revisions)}"
    assert all("changed no file" not in c.prompt for c in revisions)
    assert "TypeError" not in (replay.project / "app.py").read_text()
    assert not replay.result.completed


# -- R16 on 78f76bb: a capped initial draft past the size stop -----------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["trimmed", "still-over", "path-breach", "capped-trim", "exhausted"])
def test_a_capped_draft_past_the_size_stop_gets_the_same_one_trim(tmp_path, monkeypatch, case):
    """R16: t2's draft stopped at its 8-round cap with 153 changed lines
    inside its paths against a stated 85, and the capped close failed it on
    scope with both fix cycles unused. Its kept edits now get the one trim a
    finished draft gets, charged as the first cycle; under the stop, the
    existing capped paths decide (here the last-slot review), with the cap
    still recorded. Still over, a path breach, a capped trim (no further
    writer) or an exhausted allowance hold as before."""
    declared = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_SIMPLE + "\nCOVERS: R1"

    def lead(call, replay):
        H.write(call, {"app.py": _BLOATED})
        if case == "path-breach":
            H.write(call, {"README.md": "# app\n\nadd.\n"})
        return H.grok_ok("Still adding helpers", stop="cancelled", num_turns=8)

    def revision(call, replay):
        n = len(replay.of("revision"))
        if n == 1 and case == "capped-trim":
            H.write(call, {"app.py": _BLOATED + "\n# trimming\n"})
            return H.grok_ok("Trimming", stop="cancelled", num_turns=8)
        if n == 1:
            H.write(call, {"app.py": _BLOATED + "\n# kept\n" if case == "still-over" else _TYPED})
            return 'Trimmed.\nCHANGED: ["app.py"]'
        H.write(call, {"app.py": _BLOATED})
        return 'Revised.\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if case == "exhausted":
            return _FINDING + "\nR1: NOT MET - TypeError\nREVIEW: COMPLETE"
        return "NO FINDINGS\nR1: MET - app.py:5"

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(declared), lead=lead, revision=revision, collaborator=collaborator,
        recheck=lambda c, r: "UNRESOLVED: no", auditor=lambda c, r: "R1: MET - app.py:5",
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=1, settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    roles = [c.role for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "design-fix")]
    trims = [c for c in replay.calls if c.role == "revision" and "stopped at its 8-round limit" in c.prompt]
    if case == "trimmed":
        assert roles == ["lead", "revision"] and len(trims) == 1
        assert "Aim for about 40 lines" in trims[0].prompt
        assert replay.of("collaborator") and replay.of("auditor") and replay.result.completed
        assert (replay.project / "app.py").read_text() == _TYPED
        assert any(f.get("kind") == "cap" for t in replay.workflow["tasks"] for f in t["facts"]), "cap kept"
        return
    assert not replay.result.completed
    if case == "path-breach":
        assert roles == ["lead"] and result["failed_tasks"] == ["t1"], "no trim for a path breach"
    elif case == "still-over":
        assert roles == ["lead", "revision"] and result["failed_tasks"] == ["t1"]
    elif case == "capped-trim":
        assert roles == ["lead", "revision"], "a capped trim buys no further writer"
        assert result["failed_tasks"] == ["t1"] and not replay.of("collaborator")
    else:
        assert roles == ["lead", "revision", "revision"] and result["failed_tasks"] == ["t1"], \
            "trim, then a revision that overruns: no second trim"


@pytest.mark.requirements_ledger
def test_a_parallel_childs_capped_draft_past_the_stop_is_trimmed_and_reviewed_after_the_merge(
        tmp_path, monkeypatch):
    """R16's actual shape: t2 was a capped parallel child in the final batch.
    Trimmed under the stop it closes capped, merges, and meets the existing
    final-batch capped-child review instead of failing on scope."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))
    bloated = fixed + "".join(f"\n\ndef _unused_{i}():\n    return {i}\n" for i in range(8))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": bloated})
            return H.grok_ok("Adding helpers", stop="cancelled", num_turns=8)
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def revision(call, replay):
        H.write(call, {"app.py": fixed})
        return 'Trimmed.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(batch), lead=lead, revision=revision,
                                                 collaborator=lambda c, r: "NO FINDINGS\n" + (
                                                     "R1: MET - app.py:2" if c.task == "t1" else
                                                     "R2: MET - README.md:3")),
                   files=FILES, max_tasks=2, settings=Settings(backend="cli", lead_max_turns=8),
                   record_complete=False)
    result = H.result_json(replay)
    assert "t1" not in result["failed_tasks"], "trimmed, not failed on scope"
    assert (replay.project / "app.py").read_text() == fixed, "the trimmed source merged"
    reviews = (result.get("parallel_batches") or [{}])[0].get("capped_final_reviews") or []
    assert reviews and reviews[0]["task"] == "t1", "the existing capped final-child review ran"


@pytest.mark.requirements_ledger
def test_a_parallel_childs_capped_trim_that_lands_in_bounds_is_held_not_reviewed(tmp_path, monkeypatch):
    """Root review of 860769e: the trim of a capped parallel draft wrote an
    in-bounds source and then stopped at its own cap; the child closed on the
    draft's cap, and the final-batch capped-child review completed the run.
    Two caps in one task are now held: no review, not completed, and no
    third writer call."""
    from tests.lifecycle.test_lifecycle_matrix import FIXED as fixed
    batch = ("REQUIREMENTS:\nR1: " + _R1 + "\nR2: README documents add\n"
             "PARALLEL\n" + _simple("app.py", "Implement add in app.py.", "R1")
             + "\n---\n" + _simple("README.md", "Document add in README.md.", "R2"))
    bloated = fixed + "".join(f"\n\ndef _unused_{i}():\n    return {i}\n" for i in range(8))

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": bloated})
            return H.grok_ok("Adding helpers", stop="cancelled", num_turns=8)
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def revision(call, replay):
        H.write(call, {"app.py": _TYPED})
        return H.grok_ok("Trimmed; checking once more", stop="cancelled", num_turns=8)

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=_plan(batch), lead=lead, revision=revision,
        collaborator=lambda c, r: "NO FINDINGS\n" + ("R1: MET - app.py:5" if c.task == "t1" else
                                                     "R2: MET - README.md:3"),
        auditor=lambda c, r: "R1: MET - app.py:5\nR2: MET - README.md:3",
        **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=2, settings=Settings(backend="cli", lead_max_turns=8), record_complete=False)
    result = H.result_json(replay)
    writers = [c for c in replay.calls if c.task == "t1" and c.role in ("lead", "revision")]
    assert [c.role for c in writers] == ["lead", "revision"], "draft and its trim, nothing more"
    assert "t1" in result["turn_limited_tasks"]
    reviews = (result.get("parallel_batches") or [{}])[0].get("capped_final_reviews") or []
    t1 = [r for r in reviews if r["task"] == "t1"]
    assert t1 and not t1[0]["reviewed"] and t1[0]["verdict"].startswith("held"), t1
    assert not replay.result.completed


# -- R17 on a3cf7e4: the planner's view of settled and owed unfinished work ----------------

@pytest.mark.parametrize("t2_outcome", ["closed", "failed"])
def test_after_a_resume_the_planner_is_told_which_unfinished_work_is_still_owed(tmp_path, monkeypatch, t2_outcome):
    """R17: t1 stopped unfinished, t2 CONTINUES t1 closed, and after a real
    pause and resume the planner still read t1's 'not done ... CONTINUES: t1'
    record with nothing saying it was settled or that the one recovery task
    was spent; it named t3 CONTINUES t1 and the run stopped. The planner now
    gets the current state: what is still owed, what a later task took over,
    and the recovery allowance used. The scripted planner continues t1 unless
    told it is no longer owed. Genuine debt stays named, and a spent
    allowance still stops a real continuation."""
    from quadratus.session import SurveyConfig
    from tests.lifecycle.test_survey_run import _continue
    _interrupt_after(monkeypatch, 2)
    planner_prompts = []

    def orchestrator(call, replay):
        planner_prompts.append(call.prompt)
        n = len(planner_prompts)
        if n == 1:
            return DECL_T1
        if n == 2:
            return _continue("t1", "t1 wrote outside its scope; t2 keeps to the listed files")
        if "Unfinished work as the harness holds it now" not in call.prompt and n > 3:
            return "DONE"
        if "still owed: t2" in call.prompt:
            return DECL_T2 + "\nCONTINUES: t2\nHYPOTHESIS: t2 also wrote outside its scope"
        if "No longer owed: t1" in call.prompt:
            return DECL_T2
        return DECL_T2 + "\nCONTINUES: t1\nHYPOTHESIS: t1 is not done"

    def lead(call, replay):
        if call.task == "t1" or (call.task == "t2" and t2_outcome == "failed"):
            H.write(call, {"app.py": FIXED, "other.py": f"{call.task}\n"})
            return 'Did both.\nCHANGED: ["other.py", "app.py"]'
        if call.task == "t2":
            H.write(call, {"app.py": FIXED})
            return 'Finished add.\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
        return 'Documented add.\nCHANGED: ["README.md"]'

    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=orchestrator, lead=lead), files=FILES, max_tasks=3,
                   survey=SurveyConfig(recovery_tasks=1), record_complete=False)
    assert "KeyboardInterrupt" in replay.result.error, replay.result.error
    first = Path(replay.result.run_dir)
    second = _episode(replay, resume_from=first.name, max_tasks=3, survey=SurveyConfig(recovery_tasks=1))
    resumed = planner_prompts[2]
    leads = [c.task for c in replay.of("lead")]
    if t2_outcome == "closed":
        assert "t3" in leads and "SurveyAllowanceSpent" not in (second.error or ""), \
            "fresh work dispatched after the resume"
    else:
        assert "t3" not in leads and "SurveyAllowanceSpent" in (second.error or ""), \
            "a spent allowance never authorizes another real continuation"
    assert "Unfinished work as the harness holds it now" in resumed
    assert "Recovery tasks (CONTINUES or RESOLVES) used: 1 of 1; none is left" in resumed
    assert "### Task t1" in resumed, "the historical record is still rendered"
    if t2_outcome == "closed":
        assert "nothing is owed" in resumed and "No longer owed: t1" in resumed
    else:
        assert "still owed: t2" in resumed and "No longer owed: t1" in resumed
