"""Cycle16 (PR73 6737404225): the owner's standing question reaches the
planner and the resolving lead.

cycle15 RFT4-11: the owner's rechecks said which declared scenarios were
missing; the planner, the only seat that admits a scenario, saw a
140-character claim; t7 declared P1 to P9 without them; its lead could bind
only declared scenarios; the owner was rightly adverse. Pinned here:

- the planner's view of an open review finding carries its own author's
  latest UNRESOLVED reason (bounded, attributed, labelled by source), read
  from the latest judged entry only, and a general rule on who admits a
  missing scenario;
- the resolving lead sees exactly the findings its own RESOLVES names and
  those it inherits by CONTINUES, once each, with the same reason;
- a valid one-case PROVES line naming an undeclared scenario binds nothing
  and no longer resets the declared bindings beside it.

The planner, lead and reviewers are scripted. The positive lifecycle tests
that the question is put in front of the planner and that a planner acting
on it can be resolved; it is not evidence of a real model's compliance."""
import json
from types import SimpleNamespace

import pytest

from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.session import Session, SessionConfig
from tests.lifecycle import harness as H
from tests.lifecycle import replies
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_recovery_handoff import GROK_CAP, REQS, TASK, TYPED, _script

REASON = "test_rejects_non_number exists but no declared scenario binds it"
REJECTS = "tests/test_app.py::test_rejects_non_number"
TESTS = ("import pytest\n\nfrom app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
         "def test_rejects_non_number():\n    with pytest.raises(TypeError):\n        add('x', 1)\n")


def _resolve(declare_missing: bool) -> str:
    proofs = H.proofs_for("R1", "tests/test_app.py")
    if declare_missing:
        proofs.append(dict(id="P2", requirements=["R1"], operation="add('x', 1) is called",
                           state="the project as delivered", expect="TypeError is raised",
                           location="tests/test_app.py"))
    scope = json.dumps({"permitted_paths": ["app.py", "tests/test_app.py"], "intended_result": "add rejects a "
                        "non-number", "acceptance": ["add rejects a non-number"], "max_lines": 30, "proofs": proofs})
    return (f"KIND: backend simple\nSCOPE: {scope}\nMake add reject a non-number in app.py.\nCOVERS: R1"
            "\nRESOLVES: RFT1-1")


def _run(tmp_path, monkeypatch, name, *, planner_ignores=False):
    H.grok_lead_routing(monkeypatch, trust=True)  # t1's grok lead is capped, holding its reviewer's finding
    seen = {}

    def planner(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return REQS + TASK
        if n == 2:
            # Scripted choice: declare the missing scenario only when the
            # prompt shows why it is needed.
            seen["planner"] = call.prompt
            return _resolve(REASON in call.prompt and not planner_ignores)
        return "DONE"
    script = _script(GROK_CAP, orchestrator=planner)
    script_lead, base_recheck = script.overrides["lead"], script.overrides["recheck"]

    def lead(call, replay):
        if call.task != "t2":
            return script_lead(call, replay)
        seen["lead"] = call.prompt
        H.write(call, {"app.py": TYPED, "tests/test_app.py": TESTS})
        lines = "PROVES: P1 - tests/test_app.py::test_add"
        if "P2" in call.prompt and "TypeError is raised" in call.prompt:
            lines += f"\nPROVES: P2 - {REJECTS}"
        return f'Made add reject a non-number.\n{lines}\nCHANGED: ["app.py", "tests/test_app.py"]'

    def recheck(call, replay):
        if call.task == "t1":
            return replies.recheck_reply(call.prompt, default=f"UNRESOLVED {REASON}")
        if "named it in RESOLVES" in call.prompt:
            bound = f"binding bound: {REJECTS}" in call.prompt or f": {REJECTS}" in call.prompt.split("P2", 1)[-1]
            return replies.recheck_reply(call.prompt, default=(f"RESOLVED - P2 binds {REJECTS}" if bound and "P2" in
                                                               call.prompt else f"UNRESOLVED {REASON}"))
        return base_recheck(call, replay)
    script.overrides["lead"] = lead
    script.overrides["recheck"] = recheck
    (tmp_path / name).mkdir()
    replay = H.run(tmp_path / name, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    return replay, seen


@pytest.mark.requirements_ledger
def test_the_standing_question_reaches_the_planner_and_the_resolving_lead_and_the_owner_closes_it(tmp_path,
                                                                                                    monkeypatch):
    replay, seen = _run(tmp_path, monkeypatch, "standing")
    planner = seen["planner"]
    assert "standing reason, as claude:opus last wrote it" in planner and REASON in planner, planner[-3000:]
    assert "recorded, not a verdict" in planner
    assert S._MISSING_SCENARIO_RULE in planner
    lead = seen["lead"]
    assert "## Open review findings this task resolves" in lead and "RFT1-1" in lead and REASON in lead
    assert "you cannot add a scenario" in lead and "do not mark any of them resolved yourself" in lead
    record = H.result_json(replay)
    finding = {r["fid"]: r for r in record["review_findings"]}["RFT1-1"]
    assert finding["closed"] and finding["history"][-1]["author"] == finding["author"], finding["history"][-1]
    assert record["review_resolutions"][-1]["named"] == ["RFT1-1"]
    rows = [o for o in record["evidence_obligations"] if o.get("task") == "t2" and o.get("proof_id") == "P2"]
    assert rows and rows[0]["cases"] == [REJECTS]
    assert "FindingsUnresolved" not in (replay.result.error or ""), replay.result.error


@pytest.mark.requirements_ledger
def test_a_planner_that_does_not_declare_the_scenario_still_meets_the_owners_adverse_stop(tmp_path, monkeypatch):
    """The context is offered, not enforced: a planner that leaves the
    scenario out gets the owner's genuine UNRESOLVED and the same stop."""
    replay, seen = _run(tmp_path, monkeypatch, "ignored", planner_ignores=True)
    assert REASON in seen["planner"], "the question was in front of the planner"
    assert (replay.result.error or "").startswith("FindingsUnresolved: task t2 named RESOLVES RFT1-1"), \
        replay.result.error
    record = H.result_json(replay)
    finding = {r["fid"]: r for r in record["review_findings"]}["RFT1-1"]
    assert not finding["closed"] and finding["history"][-1]["state"] == "unresolved"
    assert len([c for c in replay.of("recheck") if c.task == "t2" and "named it in RESOLVES" in c.prompt]) == 1


# -- units: one reading of the standing reason --------------------------------------------------

def _session(tmp_path, records=(), outcomes=()):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=str(tmp_path), requirements_ledger=True))
    session._source_fingerprint = lambda: "S2"
    session.review_findings = [dict(r) for r in records]
    for task_id, resolves, continues in outcomes:
        from quadratus.outcome import TaskOutcome
        session.task_outcomes.append(TaskOutcome(task_id=task_id, intent="repair", lead="openai:sol",
                                                 resolves=list(resolves), continues=continues))
    return session


def _finding(fid="RFT1-1", task="t1", author="claude:opus", history=(), closed=False):
    return dict(fid=fid, task=task, author=author, claim=f"{fid}: the declared scenarios omit X", text=None,
                state="unresolved", impact="contract", closed=closed, history=list(history))


def _h(state, author="claude:opus", why=REASON, digest="S2", role="recheck"):
    return dict(state=state, author=author, why=why, source_digest=digest, role=role)


@pytest.mark.parametrize("history, shown, where", [
    ([_h("unresolved")], True, "on this same source"),
    ([_h("unresolved", digest="S1")], True, "on an earlier source"),
    ([_h("unresolved", digest="")], True, "on an unrecorded source"),
    ([_h("unresolved"), _h("unjudged", why="")], True, "on this same source"),
    ([_h("unresolved"), _h("resolved", why="fixed")], False, None),
    ([_h("unresolved"), _h("unresolved", author="openai:sol", why="other seat")], False, None),
    ([_h("unresolved", author="openai:sol")], False, None),
    ([_h("unresolved", why="  ")], False, None),
    ([], False, None),
], ids=["same", "earlier", "unrecorded", "unjudged-skipped", "newer-resolved", "newer-other-seat",
        "other-seat-only", "empty-reason", "no-history"])
def test_the_standing_reason_is_the_latest_judged_entry_by_the_findings_own_author(tmp_path, history, shown, where):
    session = _session(tmp_path, [_finding(history=history)])
    reason = session._standing_reason(session.review_findings[0], limit=400)
    assert (reason is not None) == shown
    if shown:
        assert reason["where"] == where and reason["author"] == "claude:opus" and reason["why"] == REASON
    note = session._standing_reason_note(session.review_findings[0])
    assert (REASON in note) == shown and ("older reason" not in note)


def test_the_planner_view_carries_the_reason_bounded_and_attributed(tmp_path):
    long = "x" * 1000
    session = _session(tmp_path, [_finding(history=[_h("unresolved", why=long)])])
    note = session._standing_reason_note(session.review_findings[0])
    assert note.count("x") == S._STANDING_REASON_CHARS and "as claude:opus last wrote it" in note
    assert "recorded, not a verdict" in note and "on this same source" in note


def test_the_resolving_lead_sees_its_own_named_and_inherited_findings_once_each(tmp_path):
    named = _finding("RFT1-1", task="t1", history=[_h("unresolved")])
    inherited = _finding("RFT2-1", task="t2", author="openai:sol", history=[_h("unresolved", author="openai:sol",
                                                                                why="P3 still unbound")])
    other = _finding("RFT5-1", task="t5", history=[_h("unresolved", why="unrelated")])
    closed = _finding("RFT1-2", task="t1", closed=True)
    session = _session(tmp_path, [named, inherited, other, closed],
                       outcomes=[("t2", [], ""), ("t3", ["RFT1-1", "RFT2-1", "RFT1-2"], "t2"),
                                 ("t4", ["RFT5-1"], "")])
    spec = SimpleNamespace(task_id="t3")
    targets = [r["fid"] for r in session._resolution_targets_of(spec)]
    assert targets == ["RFT1-1", "RFT2-1"], "named first, the inherited one not repeated, closed and other tasks' out"
    block = session._resolution_findings_block(spec)
    assert block.count("RFT2-1 (raised by") == 1 and "RFT5-1" not in block and "RFT1-2" not in block
    assert REASON in block and "P3 still unbound" in block and "unrelated" not in block
    # Another task's resolves are never read for this one, whatever the
    # session's working state says.
    session._current_resolves = ["RFT5-1"]
    assert [r["fid"] for r in session._resolution_targets_of(spec)] == ["RFT1-1", "RFT2-1"]
    assert session._resolution_findings_block(SimpleNamespace(task_id="t9")) == ""


def test_a_parallel_child_holds_no_findings_and_is_shown_none(tmp_path):
    child = _session(tmp_path, [], outcomes=[("t3", ["RFT1-1"], "")])
    assert child._resolution_findings_block(SimpleNamespace(task_id="t3")) == ""


# -- units: an undeclared scenario in a valid PROVES line resets nothing else -------------------

def _proves_session(tmp_path):
    session = Session("goal", ArtifactStore(tmp_path / "p"), lambda *a, **k: "",
                      config=SessionConfig(project=str(tmp_path)))
    session._active_spec = SimpleNamespace(task_id="t4")
    session._rebind_not_run = lambda task: None
    session.evidence_obligations = [dict(oid=f"O{i}", task="t4", kind="acceptance", proof_id=f"P{i}",
                                         cases=["tests/test_app.py::test_add"], binding="bound", execution="ran",
                                         sufficiency="unjudged") for i in (1, 2)]
    return session


def _bindings(session):
    return {r["proof_id"]: (r["binding"], list(r["cases"])) for r in session.evidence_obligations}


BOUND = ("bound", ["tests/test_app.py::test_add"])


@pytest.mark.parametrize("reply, expected, problem", [
    ("PROVES: P8 - tests/test_cli.py::Cli::test_x", {"P1": BOUND, "P2": BOUND}, "P8: not a scenario of this task"),
    ("PROVES: P1 - tests/test_app.py::test_add\nPROVES: P8 - tests/test_cli.py::test_x",
     {"P1": BOUND, "P2": BOUND}, "nothing was bound for it"),
], ids=["unknown-alone", "known-plus-unknown"])
def test_a_valid_line_naming_an_undeclared_scenario_binds_nothing_and_resets_nothing(tmp_path, reply, expected,
                                                                                   problem):
    session = _proves_session(tmp_path)
    session._record_proves("openai:sol", "revision", reply)
    assert _bindings(session) == expected
    assert any(problem in p["problem"] for p in session.proof_binding_problems), session.proof_binding_problems
    assert not any(r.get("proof_id") == "P8" for r in session.evidence_obligations), "no P8 is created"


@pytest.mark.parametrize("reply, reset", [
    ("PROVES: P8 - app.py::test_x", {"P1", "P2"}),                       # unknown, not a test path
    ("PROVES: P8 - tests/test_cli.py", {"P1", "P2"}),                    # unknown, not a case
    ("PROVES: P8 tests/test_cli.py::test_x", {"P1", "P2"}),              # no separator
    ("PROVES: the CLI tests", {"P1", "P2"}),                             # names no scenario
    ("PROVES: P1 - app.py", {"P1"}),                                     # malformed known line
    ("PROVES: P1 - tests/test_app.py::test_add\nPROVES: P1 - tests/test_app.py::test_add", {"P1"}),  # duplicate
], ids=["unknown-non-test", "unknown-non-case", "no-separator", "no-scenario", "malformed-known", "duplicate"])
def test_everything_short_of_a_valid_undeclared_line_keeps_failing_closed(tmp_path, reply, reset):
    session = _proves_session(tmp_path)
    session._record_proves("openai:sol", "revision", reply)
    for pid, (binding, cases) in _bindings(session).items():
        if pid in reset:
            assert binding.startswith("invalid declaration") and cases == [], (pid, binding)
        else:
            assert (binding, cases) == BOUND, pid


# -- root review of b592380: the inherited half reads the task's own recorded CONTINUES -----------

def _continuation_session(tmp_path):
    first = _finding("RFT1-1", task="t1", history=[_h("unresolved", why="t1's question")])
    fifth = _finding("RFT5-1", task="t5", history=[_h("unresolved", why="t5's question")])
    return _session(tmp_path, [first, fifth],
                    outcomes=[("t1", [], ""), ("t5", [], ""), ("t2", [], ""), ("t3", [], "t1")])


@pytest.mark.parametrize("task, working, expected", [
    ("t2", "t1", []),                    # own outcome continues nothing; another task's working state says t1
    ("t9", "t1", []),                    # no outcome at all
    ("t3", "t5", ["RFT1-1"]),            # own continuation t1 wins over the working t5
    ("t3", None, ["RFT1-1"]),            # the actual continuation, unchanged
], ids=["empty-own-continuation", "absent-outcome", "own-differs-from-working", "own-continuation"])
def test_the_inherited_findings_come_from_the_tasks_own_recorded_continuation(tmp_path, task, working, expected):
    session = _continuation_session(tmp_path)
    session._continues = working
    spec = SimpleNamespace(task_id=task, description="Repair add.")
    assert [r["fid"] for r in session._resolution_targets_of(spec)] == expected
    block = session._resolution_findings_block(spec)
    revision = session._revision_prompt(spec, "the draft", ["[Reviewer A]\nNO FINDINGS"])
    for fid, why in (("RFT1-1", "t1's question"), ("RFT5-1", "t5's question")):
        shown = fid in expected
        assert (fid in block) == shown and (why in block) == shown, (fid, block)
        assert (fid in revision) == shown, "the revision prompt reads the same selection"


def test_the_draft_context_reads_the_same_selection(tmp_path):
    session = _continuation_session(tmp_path)
    session._continues = "t1"
    spec = SimpleNamespace(task_id="t2", description="Repair add.", scope=None)
    assert not any("RFT1-1" in block for block in session._lead_context(spec))
