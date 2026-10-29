"""One finding identity, declared deferrals, the correction reply shape,
the planning context and the review lens (Davis, 2026-10-28), at the
units: parsers, prompts and admission, with no controller run.

The controller cases are in tests/lifecycle/test_finding_identity.py and
tests/lifecycle/test_deferred_evidence.py."""
import inspect
import json

import pytest

from quadratus import checkpoint, delivery
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.delegation import DelegationLedger, InvocationEvent
from quadratus.memory import TaskMemory
from quadratus.run_budget import RunBudget, RunLimits
from quadratus.session import (
    Session,
    SessionConfig,
    TaskSpec,
    _parse_fetch,
    _parse_fetch_window,
    _render_fetches,
    _scenario_text_problem,
    _split_explanation,
)


def _session(tmp_path, replies=("DONE",), **config):
    it = iter(replies)
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: next(it),
                   config=SessionConfig(**config))


def _review(fid, author, task, *, requirement=("R1", 1), closed=False, **extra):
    return dict(fid=fid, author=author, role="collaborator", task=task, claim=f"{fid} claim",
                text=f"BLOCKING: {fid} claim", basis=dict(kind="none", ref=""), impact="correctness",
                source_digest="abc", requirement=list(requirement) if requirement else None, history=[],
                state="resolved" if closed else "open", closed=closed, holds_effects=False,
                audit_carried=False, **extra)


def _audit(fid, task="t1", status="open", target=None):
    return dict(id=fid, task=task, requirements=["R1"], kind="check.failed", target=target, steps=[], view=None,
                width=None, viewport=None, message="the check failed", status=status, resolution=None)


# -- 1. RESOLVES: one registry, every refusal named --------------------------------------------

@pytest.mark.parametrize("resolves, covers, problem", [
    (["RFT9-1"], ["R1"], "do not exist: RFT9-1"),
    (["RFT1-1", "RFT1-1"], ["R1"], "names a finding twice"),
    (["RFT1-2"], ["R1"], "already resolved: RFT1-2"),
    (["RFT1-3"], ["R1"], "only their own reviewer can close, and that seat is unavailable now: RFT1-3 (grok:default)"),
    (["RFT1-1"], ["R2"], "COVERS must include every requirement of the findings it resolves; missing: R1"),
    (["F1", "RFT1-1"], ["R1"], ""),
    (["RFT1-1"], ["R1"], ""),
    (["F1"], ["R1"], ""),
    (["F2"], ["R1"], "already resolved: F2"),
])
def test_resolves_accepts_review_ids_beside_audit_ids_and_names_every_refusal(tmp_path, resolves, covers, problem):
    session = _session(tmp_path, requirements_ledger=True)
    session._available = lambda key: key != "grok:default"
    session.findings.extend([_audit("F1"), _audit("F2", status="resolved")])
    session.review_findings.extend([_review("RFT1-1", "claude:opus", "t1"),
                                    _review("RFT1-2", "claude:opus", "t1", closed=True),
                                    _review("RFT1-3", "grok:default", "t1")])
    spec = TaskSpec(task_id="t2", description="repair")
    got = session._resolves_problem(resolves, covers, spec)
    assert (problem in got) if problem else got == "", got


def test_the_planner_is_told_the_review_ids_resolves_may_name_without_their_claims_twice(tmp_path):
    """The claims are in the obligations view; the findings prompt names ids
    and the closing rule, and the audit-finding paragraph keeps its own."""
    session = _session(tmp_path, requirements_ledger=True)
    session.memory.ledger.requirements["R1"] = "add returns the sum"
    session.review_findings.append(_review("RFT1-1", "claude:opus", "t1", hold="left open when t1 stopped"))
    assert _session(tmp_path / "off")._findings_prompt() == "", "no ledger, no RESOLVES, no note"
    text = session._findings_prompt()
    assert "Open review findings, by id: RFT1-1 (by claude:opus on t1)" in text
    assert "closes only when the reviewer who raised it rechecks it RESOLVED" in text
    assert "the harness never marks its requirement met" in text and "RFT1-1 claim" not in text
    session.findings.append(_audit("F1"))
    both = session._findings_prompt()
    assert "F1 (found by t1" in both and "ids from this list only" in both and "Open review findings, by id" in both
    view = session._obligations()
    line = next(n for n in view.notes if n.startswith("- RFT1-1"))
    assert "held: left open when t1 stopped" in line
    header = next(n for n in view.notes if n.startswith("Review findings open by id"))
    assert "RESOLVES may name any" in header and "never by a lead or the harness" in header


def test_a_capped_or_failed_close_holds_every_open_finding_once_with_its_reason(tmp_path):
    session = _session(tmp_path, requirements_ledger=True)
    spec = TaskSpec(task_id="t1", description="work")
    session.review_findings.extend([_review("RFT1-1", "claude:opus", "t1"), _review("RFT1-2", "openai:sol", "t1"),
                                    _review("RFT1-3", "claude:opus", "t1", closed=True)])
    session.unresolved_reviews.append(dict(task="t1", peer="openai:sol", finding="x", text="RFT1-2: UNRESOLVED x",
                                           finding_id="RFT1-2"))
    session._hold_task_findings(spec, "left open when t1 stopped at its lead turn limit before its recheck")
    held = [e["finding_id"] for e in session.unresolved_reviews]
    assert held == ["RFT1-2", "RFT1-1"], "the open finding not yet held is held once; a closed one is not"
    assert session.open_findings == ["RFT1-1: UNRESOLVED (left open when t1 stopped at its lead turn limit before "
                                     "its recheck): RFT1-1 claim"]
    assert session.review_findings[0]["hold"].startswith("left open when t1 stopped")
    assert "hold" not in session.review_findings[1] and "hold" not in session.review_findings[2]
    session._hold_task_findings(spec, "again")
    assert len(session.unresolved_reviews) == 2, "a second close holds nothing twice"
    for method in (Session._close_turn_limited, Session._close_failed):
        assert "_hold_task_findings(spec" in inspect.getsource(method)


def test_inheritance_follows_the_continuation_chain_and_reaches_the_lead(tmp_path):
    from quadratus.outcome import TaskOutcome
    session = _session(tmp_path)
    session.task_outcomes.extend([TaskOutcome(task_id="t1", intent="implementation", lead="x", covers=["R1"]),
                                  TaskOutcome(task_id="t2", intent="implementation", lead="x", covers=["R1"], continues="t1")])
    session.review_findings.extend([_review("RFT1-1", "claude:opus", "t1"), _review("RFT2-1", "openai:sol", "t2"),
                                    _review("RFT1-2", "claude:opus", "t1", closed=True)])
    spec = TaskSpec(task_id="t3", description="finish")
    assert session._continuation_chain("t2") == ["t2", "t1"]
    session._inherit_review_findings(spec, "t2")
    assert [r["fid"] for r in session._inherited_review_findings(spec, "t2")] == ["RFT1-1", "RFT2-1"]
    assert session.review_findings[0]["inherited_by"] == ["t3"] and "inherited_by" not in session.review_findings[2]
    session._continues = "t2"
    block = session._inherited_findings_block(spec)
    assert "RFT1-1 (raised by claude:opus on t1)" in block and "RFT2-1 (raised by openai:sol on t2)" in block
    assert "do not mark any of them resolved yourself" in block
    assert session._resolution_reviewers(spec, continues="t2", resolves=[]) == ["claude:opus", "openai:sol"]
    assert session._resolution_reviewers(spec, continues="", resolves=["RFT2-1", "F1"]) == ["openai:sol"]
    session._current_resolves = ["RFT2-1"]
    named, inherited = session._review_resolution_targets(spec)
    assert named == ["RFT2-1"] and [r["fid"] for r in inherited] == ["RFT1-1"]


def test_the_new_records_are_carried_by_the_checkpoint_and_the_run_record(tmp_path):
    for name in ("review_resolutions", "deferred_evidence", "prompt_measurements"):
        assert name in checkpoint._CARRIED and name in checkpoint._ADDED_LATER
    session = _session(tmp_path)
    session.review_findings.append(_review("RFT1-1", "claude:opus", "t1", hold="left open when t1 stopped",
                                           inherited_by=["t2"]))
    session.review_resolutions.append(dict(task="t2", named=["RFT1-1"], inherited=[], reviewers=["claude:opus"],
                                           rechecks=dict(task="t2", rechecked=[]), refused=None))
    session.deferred_evidence.append(dict(task="t1", peer="openai:sol", proof_ids=["P1"], declared="deferred",
                                          rechecked=None, reading=None))
    session.prompt_measurements.append(dict(kind="plan", prompt_chars=10, completed_index_chars=3,
                                            completed_preview_chars=9))
    state = checkpoint.capture_state(session)
    assert "uncarried" not in state
    encoded = json.loads(json.dumps(checkpoint._encode(state)))
    resumed = _session(tmp_path / "resumed")
    checkpoint.restore_state(resumed, checkpoint._decode(encoded))
    assert resumed.review_findings[0]["hold"] == "left open when t1 stopped"
    assert resumed.review_findings[0]["inherited_by"] == ["t2"]
    assert resumed.review_resolutions == session.review_resolutions
    assert resumed.deferred_evidence == session.deferred_evidence
    assert resumed.prompt_measurements == session.prompt_measurements
    older = {k: v for k, v in state.items() if k not in ("review_resolutions", "deferred_evidence", "prompt_measurements")}
    assert checkpoint.legacy_problem(older) == ""
    earlier = _session(tmp_path / "earlier")
    checkpoint.restore_state(earlier, older)
    assert earlier.review_resolutions == [] and earlier.deferred_evidence == [] and earlier.prompt_measurements == []
    source = inspect.getsource(__import__("quadratus.project_run", fromlist=["x"]))
    for name in ("review_resolutions", "deferred_evidence", "prompt_measurements"):
        assert f"'{name}'" in source


# -- 2. the correction reply: operation apart from explanation, the bound named --------------------

@pytest.mark.parametrize("line, operation, explanation", [
    ("nothing is stored | because: the requirement says add stores nothing", "nothing is stored",
     "the requirement says add stores nothing"),
    ("nothing is stored", "nothing is stored", ""),
    ("| because: the declared text is right", "", "the declared text is right"),
    ("nothing is stored | Why: adopted", "nothing is stored", "adopted"),
    ("a | b text with a pipe | reason: kept", "a | b text with a pipe", "kept"),
])
def test_the_explanation_segment_is_split_from_the_operation(line, operation, explanation):
    assert _split_explanation(line) == (operation, explanation)


def test_the_corrected_text_has_no_character_count_and_meets_the_declaration_s_packet_bound(tmp_path):
    """Root on 6c1b922: a 683-character state the declaration accepts was
    refused only for being over 400. The shape rules stay; the one size
    bound is the task's role packet, the same the declaration met."""
    from quadratus.scope import TaskScope, read_proofs
    long_state = "the store holds " + ", ".join(f"ticket {i} with prerequisite {i + 1}" for i in range(28))
    assert len(long_state) > 600
    assert _scenario_text_problem(long_state, "old") == ""
    assert _scenario_text_problem("a\nb", "old") == "the corrected text spans more than one line"
    assert _scenario_text_problem("old", "old") == "the corrected text equals the declared text"
    assert _scenario_text_problem("", "old") == "the corrected text is empty"
    text, why = _split_explanation(long_state + " | because: " + "y" * 569)
    assert text == long_state and len(why) == 569
    proofs = read_proofs([dict(id="P1", requirements=["R1"], operation="add is called", state="nothing stored",
                               expect="the sum", location="tests/test_app.py")])
    session = _session(tmp_path)
    spec = TaskSpec(task_id="t1", description="work",
                    scope=TaskScope(permitted_paths=["app.py"], intended_result="add", acceptance=["add works"],
                                    max_lines=20, proofs=proofs))
    assert session._correction_packet_problem(spec, "P1", "state", long_state) == ""
    declared = session._role_packet(spec, "lead")
    assert len(declared.encode()) <= 20_000
    huge = "x" * 21_000
    problem = session._correction_packet_problem(spec, "P1", "state", huge)
    assert problem.startswith("the corrected text puts this task's role packet over its limit, which the declaration met")
    assert dict(spec.scope.proofs[0])["state"] == "nothing stored", "a refused trial applied nothing"
    prompt = inspect.getsource(Session._correct_scenarios)
    assert "there is no character count" in prompt and "| because: <why, optional>" in prompt
    assert "Correctness and authority are not optional" in prompt and "never a quiet rewording" in prompt
    assert "MAX_SCENARIO_TEXT" not in inspect.getsource(S)


# -- 2b. exact retrieval: fetch windows ------------------------------------------------------------

@pytest.mark.parametrize("reply, parsed", [
    ("FETCH: abc123", ("abc123", 0)),
    ("fetch: abc123 FROM 40000", ("abc123", 40000)),
    ("FETCH: abc123 from 12", ("abc123", 12)),
    ("FETCH: abc123 FROM x", ("abc123 FROM x", 0)),
    ("FETCH: abc123\nand more", None),
    ("I will FETCH: abc123 later", None),
])
def test_a_fetch_request_names_an_artifact_and_optionally_a_window_offset(reply, parsed):
    assert _parse_fetch_window(reply) == parsed
    assert _parse_fetch(reply) == (parsed[0] if parsed else None)


def test_a_long_artifact_is_served_in_exact_windows_that_name_the_next_request():
    content = "".join(f"line {i:06d}\n" for i in range(9000))
    assert len(content) > 80_000
    first = _render_fetches([("art1", content, 0)])
    assert first.count("line 000000\n") == 1 and "[...truncated]" not in first
    shown = first.split("]\n", 1)[1].split("\n[", 1)[0]
    assert shown == content[:40_000], "nothing inside a window is elided"
    assert f"{len(content) - 40_000:,} characters remain" in first
    assert "reply exactly 'FETCH: art1 FROM 40000'" in first
    second = _render_fetches([("art1", content, 40_000)])
    assert second.split("]\n", 1)[1].split("\n[", 1)[0] == content[40_000:80_000]
    assert "'FETCH: art1 FROM 80000'" in second
    last = _render_fetches([("art1", content, 80_000)])
    assert last.endswith(content[80_000:]) and "remain" not in last
    assert "characters 80,000 to " in last
    whole = _render_fetches([("art2", "short text")])
    assert "whole]" in whole and whole.endswith("short text")
    past = _render_fetches([("art1", content, 999_999)])
    assert "past the end" in past and "line" not in past.split("\n", 1)[-1]


# -- 3. the planning context: index mode, measured; the planning call admitted -----------------

def _closed_tasks(session, n=2):
    for i in range(1, n + 1):
        memory = TaskMemory(f"t{i}", "claude:opus", session.store)
        memory.keep(json.dumps(dict(task=f"t{i}", payload="x" * 3000)), kind="task-scope", author="harness")
        memory.keep("line\n" * 40, kind="draft", author="claude:opus")
        summary = memory.close(summary=f"did t{i}", reasoning=f"because t{i}", dead_ends=[f"tried y{i}"])
        session.memory.absorb(summary)
        session.history.append(summary)


def test_the_planner_and_the_lead_get_the_completed_work_as_an_index_and_the_measurement_says_both_sizes(tmp_path):
    seen = []
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda k, p, **kw: seen.append(p) or "DONE",
                      config=SessionConfig())
    _closed_tasks(session)
    assert session.next_task() is None
    body = seen[-1]
    assert "[artifact " not in body and "fetch artifact" not in body, "no preview block reached the planner"
    assert "**Full work:** artifact" in body and "(task-scope)" in body and "(draft)" in body
    assert "did t1" in body and "**Why:** because t1" in body and "tried y1" in body
    plan = session.prompt_measurements[-1]
    assert plan["kind"] == "plan" and plan["entries"] == 2 and plan["refs"] == 4
    assert 0 < plan["completed_index_chars"] < plan["completed_preview_chars"]
    assert plan["prompt_chars"] == len(body) and plan["unit"].startswith("characters")
    lead = session._lead_prompt(TaskSpec(task_id="t3", description="do t3"))
    assert "[artifact " not in lead and "**Full work:** artifact" in lead and "do t3" in lead
    assert session.prompt_measurements[-1]["kind"] == "lead" and session.prompt_measurements[-1]["task"] == "t3"
    # The originals are still served whole behind any listed id.
    ref = session.memory.ledger.entries[0].refs[0]
    assert session.memory.fetch(ref.id).startswith('{"task": "t1"')


class _Invoker:
    def __init__(self, budget):
        self.run_budget = budget

    def __call__(self, key, prompt, **kw):
        return "DONE"


def _budgeted(tmp_path, **config):
    budget = RunBudget(RunLimits(max_calls=60, max_reported_tokens=6_000_000, wall_seconds=5400,
                                 max_concurrent_workers=2, reserve_tokens_per_call=200_000))
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), _Invoker(budget), config=SessionConfig(**config))
    return session, budget


def test_the_next_planning_call_is_admitted_at_its_measured_size_when_a_slot_follows(tmp_path):
    ledger = DelegationLedger()
    session, _ = _budgeted(tmp_path, delegation_ledger=ledger)
    session._max_tasks, session._slots_used = 5, 2
    stage = session._planning_stages()[0]
    assert (stage.name, stage.exposure, stage.tokens) == ("next planning call", "reserve", 200_000)
    ledger.record(InvocationEvent(task="run", role="orchestrator", invoked=True, input_tokens=400_000,
                                  output_tokens=51_817))
    ledger.record(InvocationEvent(task="t3", role="lead", invoked=True, input_tokens=900_000, output_tokens=1))
    assert session._planning_stages()[0].tokens == 451_817, "the last planning call's size, not the lead's"
    assert session._planning_stages(slots=2)[0].tokens == 451_817
    assert session._planning_stages(slots=3) == [], "a batch that fills the slots has no planning call after it"
    session._slots_used = 4
    assert session._planning_stages() == [], "the final slot's finish is held already"
    session._slots_used = 2
    session._explicit_tasks = ["a"]
    assert session._planning_stages() == []
    session._explicit_tasks = None
    session._terminal_route = dict(task="t3")
    assert session._planning_stages() == []


def test_a_measured_stage_counts_above_its_class_never_below(tmp_path):
    stages = [delivery.PlanStage("lead draft", "editing", editing=True),
              delivery.PlanStage("next planning call", "reserve", tokens=451_817),
              delivery.PlanStage("review", "reserve", tokens=10)]
    verdict = delivery.admit_plan(stages, calls_left=10, tokens_left=1_100_000, wall_left=100.0,
                                  exposures=dict(editing=500_000, reserve=200_000), wall_per_stage=0.0)
    assert verdict.tokens_needed == 500_000 + 451_817 + 200_000 and not verdict.feasible
    assert verdict.short == dict(tokens=51_817)
    with pytest.raises(ValueError):
        delivery.PlanStage("x", "reserve", tokens=-1)


def test_the_route_counts_the_planning_call_and_the_terminal_candidate_does_not(tmp_path):
    ledger = DelegationLedger()
    ledger.record(InvocationEvent(task="run", role="orchestrator", invoked=True, input_tokens=400_000,
                                  output_tokens=51_817))
    session, _ = _budgeted(tmp_path, delegation_ledger=ledger)
    session._max_tasks, session._slots_used = 5, 2
    spec = TaskSpec(task_id="t3", description="work")
    names = [r.name for r in session._plan_stages(spec, ["lead draft"])]
    assert "next planning call" in names
    assert "next planning call" not in [r.name for r in session._known_remainder(spec, after="review")], \
        "a mid-task repair route never counts the planning call"
    source = inspect.getsource(Session._refuse_unpayable_dispatch)
    assert 'r.name not in ("close-out", "next planning call")' in source


# -- 4. the review lens --------------------------------------------------------------------------

def test_the_ordinary_review_covers_the_whole_error_boundary_and_inherited_failures(tmp_path):
    session = _session(tmp_path)
    spec = TaskSpec(task_id="t1", description="add a CLI")
    prompt = session._collaborator_prompt(spec, "draft", "claude:opus")
    assert "Cover the whole error boundary of the work" in prompt
    assert "on malformed, missing, empty, out-of-range or oversized input" in prompt
    assert "Construction, parsing and checks that run before or outside the handler" in prompt
    assert "A failure the task inherits from code it did not write but that its own path reaches" in prompt
    assert "not to what the source did before" in prompt
    # Generic by construction: no candidate's defect is named.
    for word in ("OverflowError", "Content-Length", "sqlite3", "2**63"):
        assert word not in S._ERROR_BOUNDARY_LENS


def test_a_deferral_not_rechecked_is_listed_for_the_planner_and_a_rechecked_one_is_not(tmp_path):
    session = _session(tmp_path, requirements_ledger=True)
    session.memory.ledger.requirements["R1"] = "add returns the sum"
    session.deferred_evidence.extend([
        dict(task="t1", peer="openai:sol", proof_ids=["P1"], declared="deferred",
             rechecked="not rechecked: the merge gate did not pass", reading=None),
        dict(task="t1", peer="grok:default", proof_ids=["P1"], declared="deferred", rechecked="rechecked",
             reading={"P1": "SHOWN"}),
        dict(task="t3", peer="claude:opus", proof_ids=["P2"], declared="deferred", rechecked=None, reading=None)])
    notes = session._obligations().notes
    assert any(n.startswith("Deferred evidence (rows a parallel child could not execute") for n in notes)
    assert "- t1 by openai:sol: P1: not rechecked: the merge gate did not pass" in notes
    assert "- t3 by claude:opus: P2: awaiting the merge gate and the reviewer's recheck" in notes
    assert not any("grok:default" in n for n in notes), "a rechecked deferral is not open debt"


def test_the_terminal_patch_is_the_whole_carried_state_as_the_run_ends(tmp_path):
    """Root on 6c1b922: a last-slot batch's sealed state kept the pre-batch
    review_findings=[] and unresolved_reviews=[] beside the end's capped
    lists. The patch now carries every carried key as it is at the end,
    encoded per key, with task outcomes and the ledger."""
    from quadratus.outcome import TaskOutcome
    from quadratus.project_run import _terminal_state_patch
    session = _session(tmp_path)
    session.task_outcomes.append(TaskOutcome(task_id="t2", intent="implementation", lead="x", covers=["R1"]))
    session.review_findings.append(_review("RFT2-1", "claude:opus", "t2", hold="left open when t2 stopped"))
    session.unresolved_reviews.append(dict(task="t2", peer="claude:opus", finding="x", text="RFT2-1: UNRESOLVED x",
                                           finding_id="RFT2-1"))
    session.turn_limited.append("t2")
    session._partial_tasks.add("t2")
    session.deferred_evidence.append(dict(task="t1", peer="openai:sol", proof_ids=["P1"], declared="d",
                                          rechecked=None, reading=None))
    patch = _terminal_state_patch(session)
    assert "terminal_patch_omitted" not in patch
    assert [r["fid"] for r in patch["review_findings"]] == ["RFT2-1"]
    assert patch["unresolved_reviews"][0]["finding_id"] == "RFT2-1"
    assert patch["turn_limited"] == ["t2"] and patch["_partial_tasks"] == {"__set__": ["t2"]}
    assert [o["task_id"] for o in patch["task_outcomes"]] == ["t2"]
    assert patch["deferred_evidence"][0]["peer"] == "openai:sol" and patch["review_resolutions"] == []
    for name in checkpoint._CARRIED:
        assert name in patch, name
    decoded = checkpoint._decode(json.loads(json.dumps(patch)))
    assert decoded["_partial_tasks"] == {"t2"}


def test_a_scope_conflict_child_s_findings_are_held_through_the_shared_failed_record(tmp_path):
    """The scope conflict and the raised child share _record_unmerged_failure;
    the hold lands there, on the child's outcome, before the failed record."""
    from quadratus.outcome import TaskOutcome
    session = _session(tmp_path, requirements_ledger=True)
    session.memory.ledger.requirements["R2"] = "README documents add"
    outcome = TaskOutcome(task_id="t2", intent="implementation", lead="grok:default", covers=["R2"])
    session.task_outcomes.append(outcome)
    session.review_findings.append(_review("RFT2-1", "openai:sol", "t2", requirement=("R2", 1)))
    spec = TaskSpec(task_id="t2", description="document add")
    kept = session.store.put("{}", kind="parallel-unmerged", author="grok:default")
    record = session._record_unmerged_failure(spec, outcome, ["README.md", "app.py"], {}, kept, cause="scope",
                                              reason="changed files outside its scope: app.py", listed=False)
    assert record["cause"] == "scope" and session.failed_records["t2"] is record
    finding = session.review_findings[0]
    assert finding["hold"] == "left open when t2 was not merged (scope): changed files outside its scope: app.py"
    assert [e["finding_id"] for e in session.unresolved_reviews] == ["RFT2-1"]
    assert session.open_findings == ["RFT2-1: UNRESOLVED (left open when t2 was not merged (scope): changed files "
                                     "outside its scope: app.py): RFT2-1 claim"]
    assert any(f.kind == "unverified" and f.active and f.detail.startswith("RFT2-1: UNRESOLVED") for f in outcome.facts)
    assert session.failed == [], "listed=False: the conflict's caller lists the task"
    assert not finding["closed"] and finding["author"] == "openai:sol"
