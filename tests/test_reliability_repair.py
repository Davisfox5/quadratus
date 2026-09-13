"""Regressions for the failures the 2026-09-13 GameTape trials actually hit.

Every test here reproduces something observed, not something imagined. Where
the trial captured the exact model output, that output is replayed from
``docs/handoffs/reliability-repair/evidence/`` rather than paraphrased -- a
paraphrase of a malformed reply tends to be less malformed than the original,
which is precisely the property under test.

The trials also produced one *success* worth pinning down: a read-only review
completed a full Sol -> Opus -> Sol -> RESOLVED cycle and correctly reported
its subject not ready. The suspected review-completion loop did not occur, so
several tests here assert that it still does not.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.cli_providers import _extract_native_children, _launch
from quadratus.delegation import (
    DelegationLedger,
    InvocationEvent,
    NativeChild,
    Origin,
    reconcile,
)
from quadratus.project import Project
from quadratus.providers import PartialWorkSuspected, ProviderError
from quadratus.scope import TaskScope, changed_paths, count_change_lines
from quadratus.session import (
    PartialWorkStopped,
    RunStalled,
    Session,
    SessionConfig,
    TaskSpec,
    _describe_tree_change,
    _review_subject_note,
)
from quadratus.task_kinds import TaskKind
from quadratus.taskmeta import AmbiguousMetadata, parse_control, parse_metadata
from quadratus.workers import RepeatedFailure, WorkerBudget, WorkerPool

EVIDENCE = (
    Path(__file__).resolve().parent.parent
    / "docs" / "handoffs" / "reliability-repair" / "evidence"
)

FABLE = "claude:fable"
SOL = "openai:gpt-5.6-sol"
OPUS = "claude:opus"
LUNA = "openai:gpt-5.6-luna"

KNOWN_KINDS = {"general", "test", "security", "review", "frontend", "decompose"}
KNOWN_DIFFICULTIES = {"rote", "simple", "standard", "complex"}


def _evidence(name: str) -> str:
    return (EVIDENCE / name).read_text(encoding="utf-8")


def _parse(reply: str, **kw):
    return parse_metadata(
        reply,
        known_kinds=KNOWN_KINDS,
        known_difficulties=KNOWN_DIFFICULTIES,
        default_kind="general",
        default_difficulty="simple",
        **kw,
    )


# -- 1. task metadata and control messages -----------------------------------


def test_the_actual_prefaced_kind_line_from_the_trial_is_honoured():
    """The observed failure, replayed verbatim.

    Fable prefaced ``KIND: test rote`` with a paragraph of confirmation prose.
    The old first-line-only parser saw prose, defaulted to general/simple, and
    a testing assignment pinned to Sol quietly rode the difficulty ladder
    instead.
    """
    reply = _evidence("task-metadata-response.txt")
    assert not reply.startswith("KIND:"), "fixture must retain its leading prose"

    meta = _parse(reply)

    assert meta.kind == "test"
    assert meta.difficulty == "rote"
    assert meta.confidence == "labelled"
    # The instruction, not the preface, is the task.
    assert meta.description.startswith("Fix test isolation")
    assert "That matches the reproduced gate failure" not in meta.description


def test_a_defaulted_route_is_never_indistinguishable_from_a_stated_one():
    meta = _parse("just do the thing")
    assert meta.kind == "general"
    assert meta.confidence == "defaulted"
    assert meta.defaulted
    assert meta.notes, "a default with no provenance is the original bug"


def test_an_unknown_kind_degrades_loudly_rather_than_silently():
    """The settled trade is kept -- a mislabelled task costs a routing
    preference, a rejected round costs the task -- but it is now on the record.
    """
    meta = _parse("KIND: astrology\ndo the thing")
    assert meta.kind == "general"
    assert meta.confidence == "degraded"
    assert any("astrology" in n for n in meta.notes)


def test_contradictory_labels_fail_rather_than_picking_one():
    with pytest.raises(AmbiguousMetadata):
        _parse("KIND: test rote\nKIND: security complex\ndo the thing")


def test_a_label_buried_past_the_preface_window_does_not_capture_routing():
    """A label mentioned in passing halfway down an essay is not a decision."""
    reply = "\n".join(["prose"] * 10 + ["KIND: security complex", "do it"])
    meta = _parse(reply)
    assert meta.kind == "general"
    assert meta.confidence == "defaulted"


@pytest.mark.parametrize(
    "reply, verb",
    [
        ("ASK: which database?", "ASK"),
        ("FETCH: 0123456789ab", "FETCH"),
        ("CONSULT opus: is this sound?", "CONSULT"),
        ('WORKER {"errand":"code"}', "WORKER"),
        ("DONE", "DONE"),
    ],
)
def test_every_control_verb_still_parses_unprefaced(reply, verb):
    """The channels that already worked must keep working, unchanged."""
    control = parse_control(reply)
    assert control is not None and control.verb == verb


def test_a_prefaced_ask_is_still_recognised_as_a_question():
    control = parse_control(
        "Looking at the ledger, the schema choice is not mine to make.\n"
        "ASK: should clips be stored per project or globally?"
    )
    assert control is not None
    assert control.verb == "ASK"
    assert control.body == "should clips be stored per project or globally?"
    assert control.prefaced


def test_a_task_description_mentioning_done_is_not_a_done():
    """``DONE`` is a whole-line verb; a task that talks about done-criteria is
    a task."""
    assert parse_control("DONE-criteria: the suite is green") is None


def test_a_labelled_done_still_ends_the_run():
    assert parse_control("KIND: general\nDONE").verb == "DONE"


# -- 2. worker failure recovery ----------------------------------------------


def test_the_trials_first_worker_patch_is_accepted_and_the_second_is_rejected(tmp_path):
    """The real success-then-failure sequence, against a real repository.

    The first Luna response applied. The second began with prose and carried a
    diff whose hunk header claims to replace 87 lines while supplying no
    context or removal lines at all -- ``git apply`` calls it a corrupt patch.
    Both halves matter: the patch validation must not be loosened to make the
    second one apply, and the rejection must not take the run with it.
    """
    root = tmp_path / "gametape"
    (root / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    project = Project(root)

    first = _evidence("worker-first-valid-response.txt")
    patch = first.split("```diff\n", 1)[1].rsplit("```", 1)[0]
    project.apply_patch(patch)

    applied = (root / "docs" / "BULK_TAGGING.md").read_text()
    assert applied.splitlines()[0] == "# Bulk clip tagging — design and API contract"
    expected = _evidence("BULK_TAGGING-first-applied.md")
    assert applied == expected, "the first patch must reproduce the recorded file"

    second = _evidence("worker-second-rejected-response.txt")
    assert not second.startswith("PATCH:"), "fixture must retain its leading prose"
    corrupt = second.split("```diff\n", 1)[1].rsplit("```", 1)[0]

    with pytest.raises(ProviderError):
        project.apply_patch(corrupt)

    # Rejected means unchanged. A half-applied patch would be worse than none.
    assert (root / "docs" / "BULK_TAGGING.md").read_text() == expected


def test_a_rejected_patch_reaches_the_lead_instead_of_ending_the_run(tmp_path):
    """The abort that stopped the trial.

    ``WorkerPool.commission`` records the failed fingerprint and re-raises.
    The single-worker drafting path used to let that escape, so one malformed
    worker answer ended the whole run before the lead could revise, reroute, or
    close out honestly.
    """
    store = ArtifactStore(tmp_path / "artifacts")
    seen = {"worker_calls": 0}

    def run(model, prompt, allow_writes=False):
        seen["worker_calls"] += 1
        raise ProviderError("PATCH must contain a unified diff with --- and +++ paths.")

    pool = WorkerPool(store=store, run=run)
    prompts = []

    def invoke(model, prompt, system=None, allow_writes=False):
        prompts.append(prompt)
        if "Name the single next task" in prompt:
            return "DONE"
        if "The task is finished" in prompt:
            return "SUMMARY: closed incomplete\nREASONING: the worker failed"
        if len(prompts) < 3:
            return 'WORKER {"errand":"draft","instruction":"write the design"}'
        return "I could not get a usable patch from the worker; closing incomplete."

    session = Session("goal", store, invoke, config=SessionConfig())
    session.workers = pool
    draft = session._draft_with_channels(OPUS, TaskSpec("t1", "design it"), _memory(store))

    assert seen["worker_calls"] >= 1
    assert "closing incomplete" in draft
    # The lead was told what happened and what it may legally do next.
    failure_prompt = next(p for p in prompts if "failed" in p and "Worker errand" in p)
    assert "different worker" in failure_prompt
    assert "close the task incomplete" in failure_prompt


def test_repeated_failure_protection_survives_the_new_recovery_path(tmp_path):
    """A failure being recoverable must not make a verbatim retry legal."""
    store = ArtifactStore(tmp_path / "artifacts")
    pool = WorkerPool(store=store, run=_always_fails)
    task = _memory(store)

    with pytest.raises(ProviderError):
        pool.commission(task=task, parent_key=OPUS, prompt="same", label="a", errand="draft")
    with pytest.raises(RepeatedFailure):
        pool.commission(task=task, parent_key=OPUS, prompt="same", label="b", errand="draft")


def test_the_same_prompt_on_a_different_worker_is_still_allowed(tmp_path):
    """A changed strategy, and deliberately still permitted."""
    store = ArtifactStore(tmp_path / "artifacts")
    calls = []

    def run(model, prompt, allow_writes=False):
        calls.append(model)
        if len(calls) == 1:
            raise ProviderError("nope")
        return "PATCH: fine"

    pool = WorkerPool(store=store, run=run)
    task = _memory(store)
    with pytest.raises(ProviderError):
        pool.commission(task=task, parent_key=OPUS, prompt="same", label="a", errand="draft")
    result = pool.commission(
        task=task, parent_key=OPUS, prompt="same", label="b", errand="check"
    )
    assert result.error is None
    assert len(set(calls)) == 2


def test_a_lead_that_only_fails_workers_stalls_rather_than_looping(tmp_path):
    """Recovery must not become an unbounded retry budget."""
    store = ArtifactStore(tmp_path / "artifacts")
    pool = WorkerPool(store=store, run=_always_fails)
    counter = {"n": 0}

    def invoke(model, prompt, system=None, allow_writes=False):
        counter["n"] += 1
        return 'WORKER {"errand":"draft","instruction":"attempt %d"}' % counter["n"]

    session = Session("goal", store, invoke, config=SessionConfig(max_worker_failures=3))
    session.workers = pool
    with pytest.raises(RunStalled, match="not converging"):
        session._draft_with_channels(OPUS, TaskSpec("t1", "design it"), _memory(store))


def test_the_worker_budget_is_still_charged_for_a_failed_errand(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    pool = WorkerPool(store=store, run=_always_fails, budget=WorkerBudget(max_per_task=2))
    task = _memory(store)
    with pytest.raises(ProviderError):
        pool.commission(task=task, parent_key=OPUS, prompt="one", label="a", errand="draft")
    assert pool.remaining(task.task_id) == 1


# -- 3. task scope -----------------------------------------------------------


def test_an_undersized_task_growing_into_a_feature_is_detected():
    """The trial's fixture task that implemented the rest of the goal."""
    scope = TaskScope(
        permitted_paths=["tests/test_basic.py"],
        intended_result="patch RECORDINGS_DIR in the client fixture",
        max_lines=4,
    )
    diff = _fake_diff({
        "tests/test_basic.py": 4,
        "app.py": 131,
        "static/js/app.js": 210,
    })
    report = scope.assess(diff)

    assert report.blocking, "writes outside the permitted path must block"
    assert report.out_of_scope == ["app.py", "static/js/app.js"]
    assert report.oversized
    assert "app.py" in report.render()


def test_a_task_that_stays_inside_its_scope_passes_quietly():
    scope = TaskScope(permitted_paths=["tests/*.py"], max_lines=20)
    report = scope.assess(_fake_diff({"tests/test_basic.py": 4}))
    assert report.within_scope and not report.blocking and not report.oversized


def test_a_directory_prefix_covers_the_files_under_it():
    scope = TaskScope(permitted_paths=["docs"])
    assert scope.permits("docs/BULK_TAGGING.md")
    assert not scope.permits("app.py")


def test_forbidden_paths_win_over_permitted_ones():
    scope = TaskScope(permitted_paths=["**"], forbidden_paths=["data", ".quadratus"])
    assert scope.permits("app.py")
    assert not scope.permits("data/projects.json")


def test_an_unscoped_task_says_so_instead_of_passing_silently():
    report = TaskScope.unbounded().assess(_fake_diff({"app.py": 400}))
    assert report.within_scope
    assert any("No permitted paths" in n for n in report.notes)


def test_scope_expansion_is_reported_and_never_reverted(tmp_path):
    """Partial and user work is preserved; the report is the response."""
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "keep.txt").write_text("operator's own edit\n")
    project = Project(root)
    before = project.contents()
    (root / "app.py").write_text("print('wide')\n" * 40)

    spec = TaskSpec("t1", "tiny fix", scope=TaskScope(permitted_paths=["docs"], max_lines=5))
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, _null_invoke,
                      config=SessionConfig(project=root, allow_writes=True))
    report = session._assess_scope(spec, _memory(store), before)

    assert report is not None and report.blocking
    assert "app.py" in report.out_of_scope
    # Nothing rolled back -- neither the over-wide change nor the user's file.
    assert (root / "app.py").exists()
    assert (root / "keep.txt").read_text() == "operator's own edit\n"
    assert session.open_findings and "outside its declared scope" in session.open_findings[0]


def test_scope_is_stated_to_the_lead_before_the_work():
    rendered = TaskScope(
        permitted_paths=["tests/test_basic.py"],
        intended_result="patch the fixture",
        acceptance=["69 tests pass", "no new files under data/"],
        max_lines=4,
    ).render()
    assert "tests/test_basic.py" in rendered
    assert "patch the fixture" in rendered
    assert "69 tests pass" in rendered
    assert "about 4 changed lines" in rendered


def test_changed_paths_and_line_counts_read_a_real_diff(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "a.txt").write_text("one\n")
    project = Project(root)
    before = project.contents()
    (root / "a.txt").write_text("one\ntwo\n")
    (root / "b.txt").write_text("new\n")
    diff = project.diff(before)
    assert set(changed_paths(diff)) == {"a.txt", "b.txt"}
    assert count_change_lines(diff) >= 2


# -- 4. timeout and cancellation recovery ------------------------------------


def test_a_timed_out_editing_call_is_not_replayed():
    """The 900-second replay the trial had to be stopped by hand."""
    provider = _FakeEditor(allow_writes=True, fail_with=TimeoutError("timed out after 900s"))
    with pytest.raises(PartialWorkSuspected):
        provider.generate("rewrite the module")
    assert provider.calls == 1, "a writing prompt must not be re-sent blind"


def test_a_timed_out_read_only_call_is_still_retried():
    """Read-only work changed nothing, so re-sending it is a genuine retry."""
    provider = _FakeEditor(allow_writes=False, fail_with=TimeoutError("timed out"))
    with pytest.raises(ProviderError) as caught:
        provider.generate("review this")
    assert not isinstance(caught.value, PartialWorkSuspected)
    assert provider.calls > 1


def test_a_rate_limit_on_an_editing_call_is_still_retried():
    """The vendor never ran it, so nothing partial can exist."""
    provider = _FakeEditor(allow_writes=True, fail_with=RuntimeError("rate limit reached"))
    with pytest.raises(ProviderError) as caught:
        provider.generate("edit this")
    assert not isinstance(caught.value, PartialWorkSuspected)
    assert provider.calls > 1


def test_partial_edits_are_inspected_and_preserved_after_a_stop(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "a.py").write_text("original\n")
    store = ArtifactStore(tmp_path / "artifacts")

    def invoke(model, prompt, system=None, allow_writes=False):
        # Write, then stop -- exactly the shape of the observed timeout.
        (root / "a.py").write_text("half rewritten\n")
        (root / "new.py").write_text("and a new file\n")
        raise PartialWorkSuspected("timed out after 900s")

    session = Session("goal", store, invoke,
                      config=SessionConfig(project=root, allow_writes=True))
    with pytest.raises(PartialWorkStopped) as caught:
        session._edit(OPUS, "rewrite it")

    state = caught.value.partial
    assert state["inspected"] is True
    assert set(state["changed"]) == {"a.py", "new.py"}
    assert state["changed_lines"] >= 2
    assert "preserved" in state["note"]
    # Preserved means preserved.
    assert (root / "a.py").read_text() == "half rewritten\n"
    assert (root / "new.py").exists()
    assert "a.py" in caught.value.render()


def test_unknown_partial_state_is_reported_as_unknown_not_none(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, _null_invoke, config=SessionConfig())
    state = session._inspect_partial_edits(None)
    assert state["inspected"] is False
    assert "unknown" in state["note"].lower()


@pytest.mark.skipif(not hasattr(__import__("os"), "killpg"), reason="POSIX only")
def test_a_timed_out_cli_takes_its_grandchildren_with_it(tmp_path):
    """``subprocess.run`` kills only the process it started.

    Every vendor CLI here is a launcher that spawns its own children, and those
    used to survive the parent being killed -- still holding the working tree,
    and accumulating over a long run.
    """
    marker = tmp_path / "child-alive"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import time,pathlib; pathlib.Path(r'{marker}').write_text('x'); time.sleep(30)\"])\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _launch([sys.executable, "-c", script], timeout=3, cwd=str(tmp_path), env=None)

    assert marker.exists(), "the grandchild must actually have started"
    # Give the group kill a moment, then confirm nothing is left holding on.
    leftover = subprocess.run(
        ["pgrep", "-f", "pathlib.Path"], capture_output=True, text=True, check=False
    )
    assert marker.read_text() == "x"
    assert "time.sleep(30)" not in leftover.stdout


# -- 5. native delegation and usage ------------------------------------------


def test_the_trials_native_child_is_reconciled_without_double_counting():
    """The 135,105 tokens that were spent inside an authorised run and appeared
    in no total it reported."""
    evidence = json.loads(
        (EVIDENCE / "routing-trial" / "review" / "native-delegation-evidence.json")
        .read_text(encoding="utf-8")
    )
    parent = evidence["parent_usage"]
    child = evidence["child_usage"]

    events = [InvocationEvent(
        task="t1", role="lead", origin=Origin.SEAT,
        requested_model=SOL, resolved_model=SOL, invoked=True, outcome="ok",
        input_tokens=parent["input_tokens"], output_tokens=parent["output_tokens"],
        session_id="parent-session",
    )]
    children = [NativeChild(
        session_id=evidence["child_session"],
        model=evidence["child_model"],
        input_tokens=child["input_tokens"],
        output_tokens=child["output_tokens"],
        tool_name="spawn_agent",
    )]

    totals = reconcile(events, children)
    assert totals["controlled_tokens"] == parent["total_tokens"] == 209549
    assert totals["native_child_tokens"] == child["total_tokens"] == 135105
    assert totals["known_minimum_tokens"] == 209549 + 135105


def test_cumulative_child_snapshots_are_taken_at_their_maximum_not_summed():
    """Vendor session files restate the session total on every update."""
    ledger = DelegationLedger()
    for running_total in (40_000, 90_000, 127_405):
        ledger.observe_native(NativeChild(
            session_id="child-1", model=SOL,
            input_tokens=running_total, output_tokens=7_700,
        ))
    assert ledger.native_tokens() == 127_405 + 7_700
    assert len(ledger.native_children) == 1


def test_a_child_quadratus_itself_dispatched_is_not_counted_twice():
    events = [InvocationEvent(
        task="t1", role="worker", origin=Origin.WORKER, resolved_model=LUNA,
        invoked=True, outcome="ok", input_tokens=100, output_tokens=10,
        session_id="shared-id",
    )]
    children = [NativeChild(session_id="shared-id", input_tokens=100, output_tokens=10)]
    totals = reconcile(events, children)
    assert totals["controlled_tokens"] == 110
    assert totals["native_child_tokens"] == 0


def test_a_spawn_agent_event_is_extracted_from_a_codex_stream():
    """Fixture-driven; no access to anyone's real session directory."""
    stdout = "\n".join([
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
        json.dumps({
            "name": "spawn_agent",
            "session_id": "parent-1",
            "child_session": "child-1",
            "child_model": "gpt-5.6-sol",
            "usage": {"input_tokens": 127405, "output_tokens": 7700},
        }),
    ])
    children = _extract_native_children(stdout)
    assert len(children) == 1
    assert children[0].session_id == "child-1"
    assert children[0].total_tokens == 135105


def test_an_unidentifiable_spawn_is_recorded_with_unknown_usage():
    stdout = json.dumps({"name": "spawn_agent", "arguments": {"task_name": "second_review"}})
    children = _extract_native_children(stdout)
    assert len(children) == 1
    assert children[0].total_tokens is None, "unknown must not become zero"


def test_auxiliary_vendor_usage_is_not_counted_as_a_quadratus_worker():
    """Claude's envelopes list Haiku rows nobody dispatched."""
    events = [
        InvocationEvent(task="t1", role="lead", origin=Origin.SEAT, resolved_model=OPUS,
                        invoked=True, outcome="ok", input_tokens=1000, output_tokens=100),
        InvocationEvent(task="t1", role="vendor-internal", origin=Origin.AUXILIARY,
                        resolved_model="claude:haiku", invoked=True, outcome="ok",
                        input_tokens=500, output_tokens=50),
    ]
    totals = reconcile(events, [])
    assert totals["controlled_tokens"] == 1100
    assert totals["auxiliary_tokens"] == 550


def test_a_call_that_reported_no_usage_stays_unknown_not_zero():
    ledger = DelegationLedger()
    ledger.record(InvocationEvent(
        task="t1", role="orchestrator", resolved_model=FABLE,
        invoked=True, outcome="KeyboardInterrupt", seconds=13.48,
    ))
    unknown = ledger.unknown_events()
    assert len(unknown) == 1
    assert unknown[0].total_tokens is None
    report = ledger.render_report()
    assert "unknown" in report.lower()
    assert "KeyboardInterrupt" in report


def test_a_selected_model_that_never_ran_is_not_reported_as_coverage():
    """Grok was selected in the trial's first task and never reached."""
    ledger = DelegationLedger()
    ledger.record(InvocationEvent(task="t1", role="lead", resolved_model=OPUS,
                                  selected=True, invoked=True, outcome="ok",
                                  input_tokens=1, output_tokens=1))
    ledger.record(InvocationEvent(task="t1", role="collaborator",
                                  resolved_model="grok:default",
                                  selected=True, invoked=False, outcome="selected"))
    assert ledger.invoked_models() == [OPUS]
    assert ledger.selected_never_invoked() == ["grok:default"]
    assert "not coverage" in ledger.render_report()


def test_transport_and_post_return_failures_are_accounted_separately():
    ledger = DelegationLedger()
    ledger.record(InvocationEvent(task="t1", role="worker", origin=Origin.WORKER,
                                  resolved_model=LUNA, invoked=True,
                                  outcome="TimeoutError"))
    ledger.record(InvocationEvent(task="t1", role="worker", origin=Origin.WORKER,
                                  resolved_model=LUNA, invoked=True, attempt=2,
                                  outcome="ProviderError", post_return_failure=True,
                                  input_tokens=64675, output_tokens=4636))
    report = ledger.render_report()
    assert "failed after return" in report
    # The call that returned text is metered; the one that never returned is not.
    assert ledger.controlled_tokens() == 64675 + 4636
    assert len(ledger.unknown_events()) == 1


def test_native_delegation_is_declared_as_outside_harness_control():
    ledger = DelegationLedger()
    ledger.observe_native(NativeChild(session_id="child-1", model=SOL))
    report = ledger.render_report()
    assert "Not observable or not controllable by the harness" in report
    assert "outside Quadratus worker selection and budgets" in report


def test_subscription_usage_is_kept_apart_from_api_cost():
    ledger = DelegationLedger()
    ledger.record(InvocationEvent(task="t1", role="lead", resolved_model=OPUS,
                                  invoked=True, outcome="ok",
                                  input_tokens=10, output_tokens=1))
    assert "Not an API charge" in ledger.render_report()


def test_the_full_trial_usage_summary_reconciles_to_its_recorded_minimum():
    """End-to-end against the numbers the trial actually published."""
    summary = json.loads(
        (EVIDENCE / "routing-trial" / "usage-summary.json").read_text(encoding="utf-8")
    )
    events = [
        InvocationEvent(task="t", role="seat", origin=Origin.SEAT,
                        resolved_model=model, invoked=True, outcome="ok",
                        input_tokens=agg["input_tokens"], output_tokens=agg["output_tokens"])
        for model, agg in summary["models"].items()
    ]
    events.append(InvocationEvent(
        task="t", role="orchestrator", origin=Origin.SEAT, resolved_model=FABLE,
        invoked=True, outcome="KeyboardInterrupt", seconds=13.48,
    ))
    children = [NativeChild(
        session_id="child-1", model=SOL,
        input_tokens=127405, output_tokens=7700,
    )]

    totals = reconcile(events, children)
    assert totals["controlled_tokens"] == summary["quadratus_metered_tokens"] == 1564506
    assert totals["native_child_tokens"] == summary["additional_verified_native_child_tokens"]
    assert totals["known_minimum_tokens"] == summary["known_minimum_tokens"] == 1699611
    assert totals["unknown_invocations"] == len(summary["unknown_calls"]) == 1


# -- 6. verification and reporting -------------------------------------------


def test_a_gate_failure_names_the_files_that_changed():
    """The trial's fixture wrote into data/recordings and the message said only
    that *something* had changed."""
    before = {"app.py": b"x", "tests/test_basic.py": b"y"}
    after = dict(before)
    after["data/recordings/clip1.webm"] = b"binary"
    after["app.py"] = b"x modified"

    message = _describe_tree_change(before, after)
    assert "data/recordings/clip1.webm" in message
    assert "1 added" in message and "1 modified" in message
    assert "not valid" in message


def test_an_unexpected_new_source_file_is_still_caught():
    """Narrowing the fingerprint to tracked files would hide this entirely."""
    message = _describe_tree_change({"app.py": b"x"}, {"app.py": b"x", "scratch.py": b"new"})
    assert "scratch.py" in message


def test_the_same_tree_check_still_invalidates_a_moved_tree(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("one\n")
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, _null_invoke, config=SessionConfig(project=root))

    class MovingGate:
        cwd = str(root)

        def run(self):
            from quadratus.integration import GateResult
            (root / "a.py").write_text("changed during the run\n")
            return GateResult(True, "pytest -q", 0, "1 passed")

    result = session._check(MovingGate())
    assert result.passed is False
    assert "a.py" in result.output


def test_an_untouched_tree_lets_a_passing_gate_pass(tmp_path):
    """The read-only review's passing same-tree gate must keep passing."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("one\n")
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, _null_invoke, config=SessionConfig(project=root))

    class QuietGate:
        cwd = str(root)

        def run(self):
            from quadratus.integration import GateResult
            return GateResult(True, "pytest -q", 0, "79 passed")

    assert session._check(QuietGate()).passed is True


def test_a_read_only_run_is_not_told_to_update_the_project(tmp_path):
    """The conflicting instruction behind the review probe's blocker reports."""
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, _null_invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=False))
    delivery = session._revision_delivery()
    assert "Update the project" not in delivery
    assert "read-only" in delivery
    assert "not ready" in delivery


def test_a_granted_run_is_still_told_to_update_the_project(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    session = Session("goal", store, _null_invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    assert "Update the project" in session._revision_delivery()


def test_a_review_reporting_its_subject_unready_is_not_an_unfinished_review():
    """The probe completed correctly; that outcome is now pinned down."""
    note = _review_subject_note(TaskSpec("t1", "review the design", kind=TaskKind.REVIEW))
    assert "complete and successful review" in note
    assert "Do not ask for the subject to be fixed" in note


def test_a_normal_task_gets_no_review_subject_note():
    assert _review_subject_note(TaskSpec("t1", "build it", kind=TaskKind.GENERAL)) == ""


def test_exported_source_references_survive_the_review_copy_being_deleted(tmp_path):
    """A finding citing the disposable copy is a dead link by the time it is
    read. The isolation is right; the reference was not."""
    from quadratus.runtime import _relativise_snapshot_paths

    snapshot = tmp_path / "quadratus-review-abc123"
    snapshot.mkdir()
    reply = (
        f"BLOCKING: {snapshot}/app.py line 503 rewrites the whole file.\n"
        f"See also {snapshot}/static/js/app.js:1123.\n"
        f"The copy at {snapshot} is read-only."
    )
    cleaned = _relativise_snapshot_paths(reply, snapshot)

    assert str(snapshot) not in cleaned
    assert "app.py line 503" in cleaned
    assert "static/js/app.js:1123" in cleaned
    assert "the project root is read-only" in cleaned


def test_relativising_leaves_an_unrelated_reply_alone(tmp_path):
    from quadratus.runtime import _relativise_snapshot_paths

    snapshot = tmp_path / "quadratus-review-abc123"
    snapshot.mkdir()
    reply = "BLOCKING: app.py line 503 rewrites the whole file."
    assert _relativise_snapshot_paths(reply, snapshot) == reply


# -- helpers -----------------------------------------------------------------


def _memory(store):
    from quadratus.memory import TaskMemory
    return TaskMemory("t1", OPUS, store)


def _null_invoke(model, prompt, system=None, allow_writes=False):
    return f"[{model}] output"


def _always_fails(model, prompt, allow_writes=False):
    raise ProviderError("Bounded editor returned no PATCH or explicit NO CHANGES result.")


def _fake_diff(files: dict) -> str:
    """A unified diff touching each named file with N added lines."""
    out = []
    for name, count in files.items():
        out.append(f"--- a/{name}")
        out.append(f"+++ b/{name}")
        out.append(f"@@ -0,0 +1,{count} @@")
        out.extend("+line" for _ in range(count))
    return "\n".join(out) + "\n"


class _FakeEditor:
    """A provider whose transport always fails, to exercise the retry rules."""

    label = "Fake"
    name = "fake"

    def __init__(self, *, allow_writes, fail_with):
        from quadratus.providers import LLMProvider

        self.calls = 0
        self.allow_writes = allow_writes
        self._fail_with = fail_with
        self._impl = LLMProvider.__new__(LLMProvider)
        self._impl.label = self.label
        self._impl.model = "fake-1"
        self._impl.max_retries = 3
        self._impl.retry_base_delay = 0.0
        self._impl.allow_writes = allow_writes
        self._impl.refusal_fallback_model = None
        self._impl._init_error = None
        self._impl._call = self._call
        self._impl._retryable = lambda exc: True

    def _call(self, prompt, system, turns):
        self.calls += 1
        raise self._fail_with

    def generate(self, prompt, **kw):
        from quadratus.providers import LLMProvider
        return LLMProvider._generate_once(self._impl, prompt, "sys", [])


# -- concurrency -------------------------------------------------------------


def test_concurrent_worker_failures_do_not_tear_down_their_siblings(tmp_path):
    """A failed errand in a parallel batch is a result, not a crash.

    ``commission_many`` already behaved this way; the single-worker path now
    agrees with it, so this pins the behaviour the two are converging on.
    """
    store = ArtifactStore(tmp_path / "artifacts")

    def run(model, prompt, allow_writes=False):
        if "bad" in prompt:
            raise ProviderError("corrupt patch")
        return f"ok: {prompt}"

    pool = WorkerPool(store=store, run=run, budget=WorkerBudget(max_concurrent=4))
    jobs = [
        {"prompt": "good one", "label": "a", "errand": "draft"},
        {"prompt": "bad one", "label": "b", "errand": "draft"},
        {"prompt": "good two", "label": "c", "errand": "check"},
        {"prompt": "bad two", "label": "d", "errand": "check"},
    ]
    results = pool.commission_many(task=_memory(store), parent_key=OPUS, jobs=jobs)

    assert len(results) == 4
    assert sorted(r.label for r in results if r.error) == ["b", "d"]
    assert sorted(r.label for r in results if not r.error) == ["a", "c"]


def test_concurrent_commissions_charge_the_budget_exactly_once_each(tmp_path):
    """The counter is shared mutable state across the pool's threads."""
    store = ArtifactStore(tmp_path / "artifacts")
    pool = WorkerPool(
        store=store,
        run=lambda m, p, allow_writes=False: "ok",
        budget=WorkerBudget(max_per_task=12, max_concurrent=4),
    )
    task = _memory(store)
    jobs = [
        {"prompt": f"errand {i}", "label": f"w{i}", "errand": "check"}
        for i in range(8)
    ]
    results = pool.commission_many(task=task, parent_key=OPUS, jobs=jobs)

    assert len([r for r in results if not r.error]) == 8
    assert pool.spawned(task.task_id) == 8
    assert pool.remaining(task.task_id) == 4


def test_a_concurrent_batch_that_exceeds_the_budget_fails_only_the_overflow(tmp_path):
    """FanOutExceeded arrives as an error on the overflowing errands, and the
    ones inside the budget still return their work."""
    store = ArtifactStore(tmp_path / "artifacts")
    pool = WorkerPool(
        store=store,
        run=lambda m, p, allow_writes=False: "ok",
        budget=WorkerBudget(max_per_task=3, max_concurrent=4),
    )
    task = _memory(store)
    jobs = [
        {"prompt": f"errand {i}", "label": f"w{i}", "errand": "check"}
        for i in range(6)
    ]
    results = pool.commission_many(task=task, parent_key=OPUS, jobs=jobs)

    succeeded = [r for r in results if not r.error]
    failed = [r for r in results if r.error]
    assert len(succeeded) == 3
    assert len(failed) == 3
    assert all("budget" in r.error for r in failed)
    assert pool.spawned(task.task_id) == 3


def test_the_delegation_ledger_is_safe_under_concurrent_recording(tmp_path):
    """Workers run concurrently, so the record they write to must survive it."""
    import threading

    ledger = DelegationLedger(path=tmp_path / "invocations.jsonl")

    def record(index):
        ledger.record(InvocationEvent(
            task="t1", role=f"worker-{index}", origin=Origin.WORKER,
            resolved_model=LUNA, invoked=True, outcome="ok",
            input_tokens=100, output_tokens=10,
        ))

    threads = [threading.Thread(target=record, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ledger.events) == 25
    assert ledger.controlled_tokens() == 25 * 110
    written = (tmp_path / "invocations.jsonl").read_text().strip().splitlines()
    assert len(written) == 25
    assert all(json.loads(line)["origin"] == Origin.WORKER for line in written)


def test_native_children_observed_concurrently_are_still_counted_once(tmp_path):
    """Cumulative restatements arriving from several threads must not stack."""
    import threading

    ledger = DelegationLedger()
    readings = [40_000, 90_000, 127_405, 90_000, 40_000]

    def observe(value):
        ledger.observe_native(NativeChild(
            session_id="child-1", model=SOL,
            input_tokens=value, output_tokens=7_700,
        ))

    threads = [threading.Thread(target=observe, args=(v,)) for v in readings * 5]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ledger.native_children) == 1
    assert ledger.native_tokens() == 127_405 + 7_700
