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
