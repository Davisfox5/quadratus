"""Cycle05 (PR73 6715806056) units: which DONE blockers a declaration may
settle, what a declaration call that does not answer leaves, the receipts a
declared case may settle on, the record carried across a resume, and the
audit's one required-line list read by the same rule that wrote it.

A real Session with its record set by hand. The lifecycle controls (real
controller, scripted providers, the project's own pytest gate) are in
tests/lifecycle/test_done_declarations.py."""
from types import SimpleNamespace

import pytest

from quadratus import checkpoint, judgment
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.memory import TaskSummary
from quadratus.outcome import TaskOutcome
from quadratus.providers import ProviderError, TurnLimitReached
from quadratus.run_budget import FinishReserved
from quadratus.session import Session, SessionConfig

CASE = "tests/test_app.py::test_add"
ITEM = dict(task="t1", oid="O2", item="app.py", why="outside my paths", role="lead", cases=[], kind="not_run",
            binding=dict(kind="paths"), requirements=["R1"], after_check=0, executable=False)
STALE = dict(oid="O3", task="t1", kind="acceptance", modality="test", proof_id="P1", runner="gate",
             execution="missing", binding="stale: tests/test_app.py::test_old no longer defined or run after t2",
             requirements=[["R1", 1]], behavior="add", scenario=[["proof_id", "P1"]])


def _session(tmp_path, *, entries=None, rows=(), lead="claude:opus", auditor="openai:sol", passed=True,
             cases=(CASE,), work=(), need_editing=False):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=str(tmp_path)))
    session._source_fingerprint = lambda: "S"
    session._current_gate = lambda: None
    session._environment_id = lambda: "E"
    session.checks = [dict(source="S", passed=passed, environment="E", gate="")]
    session.unexecuted_acceptance = [dict(e) for e in (entries if entries is not None else [ITEM])]
    session.evidence_obligations = [dict(r) for r in rows]
    session._standing_not_run = lambda: list(session.unexecuted_acceptance)
    session._discharge_record = lambda e: None
    session._editing_work = lambda: list(work)
    session._blockers_need_editing = need_editing
    session._auditor = lambda: auditor
    session._available = lambda key: True
    session.task_outcomes = [TaskOutcome(task_id="t1", intent="implementation", lead=lead)] if lead else []
    session.history.append(TaskSummary(task_id="t1", author=lead or "harness", summary="Implemented add.",
                                       reasoning=""))
    run = dict(id="check", tests=1, skipped=0, filtered=False, cwd=".", report_cases=list(cases))
    return session, run


@pytest.fixture
def runs(monkeypatch):
    holder = {}
    monkeypatch.setattr(S, "_check_runs", lambda check, root=None: [holder["run"]])
    monkeypatch.setattr(S, "_complete_run", lambda run: True)
    monkeypatch.setattr(S, "_run_passed_cases", lambda run: set(run.get("report_cases") or ()))
    return holder


def test_an_item_naming_nothing_runnable_is_planned_for_its_owning_lead_with_its_close_out(tmp_path, runs):
    session, runs["run"] = _session(tmp_path)
    plan, why = session._done_declaration_plan({"R1": "NOT RUN"})
    decision = plan["t1"].pop("decision")
    assert why == "" and plan == {"t1": dict(lead="claude:opus", intent="Implemented add.", oids=["O2"], pids=[])}
    assert decision == session._declaration_decision("t1", ["O2"], [])
    session, runs["run"] = _session(tmp_path / "s", entries=[dict(ITEM, taken_over_by=[dict(task="t3")])])
    session.task_outcomes.append(TaskOutcome(task_id="t3", intent="implementation", lead="openai:sol",))
    session._auditor = lambda: "xai:grok"
    plan, _ = session._done_declaration_plan({"R1": "NOT RUN"})
    assert list(plan) == ["t3"], "the task that took the item over owns it"
    session, runs["run"] = _session(tmp_path / "r", entries=[], rows=[STALE])
    plan, why = session._done_declaration_plan({"R1": "stale"})
    assert why == "" and plan["t1"]["pids"] == ["P1"]


@pytest.mark.parametrize("name, kw, change, why", [
    ("needs editing", dict(need_editing=True), None, "needs a code or test change"),
    ("editing work", dict(work=["task t2's last check still fails"]), None, "editing work is known"),
    ("failed check", dict(passed=False), None, "did not pass on the current source"),
    ("no executed case", dict(cases=()), None, "names an executed case"),
    ("no auditor", dict(auditor=None), None, "no auditor"),
    ("unknown owner", dict(entries=[dict(ITEM, task="")]), None, "owning task is unknown"),
    ("no lead", dict(lead=""), None, "no lead seat is on record"),
    ("lead is the auditor, no other judge", dict(lead="openai:sol"),
     lambda s: setattr(s, "_independent_judge", lambda *a, **k: None), "could not be judged independently"),
    ("stale row's lead is the auditor", dict(lead="openai:sol", entries=[], rows=[STALE]), None,
     "could not be judged independently"),
    ("bound item", dict(entries=[dict(ITEM, cases=[CASE])]), None, "only their execution settles it"),
    ("skip fact", dict(entries=[dict(ITEM, kind="skipped", oid=None)]), None, "only execution settles them"),
    ("runnable item", dict(entries=[dict(ITEM, executable=True)]), None, "names something a check can run"),
    ("ambiguous id", dict(entries=[ITEM, dict(ITEM, item="cli.py")]), None, "no unique id"),
    ("no runner", dict(entries=[], rows=[dict(STALE, runner=None)]), None, "no authorized runner"),
    ("failed row", dict(entries=[], rows=[dict(STALE, execution="failed", binding="bound")]), None, "failed"),
    ("not a scenario", dict(entries=[], rows=[dict(STALE, proof_id=None)]), None, "not a declared test scenario"),
    ("unavailable lead", {}, lambda s: setattr(s, "_available", lambda k: False), "is unavailable"),
    ("already asked", {}, lambda s: s.done_declarations.append(
        dict(owner="t1", decision=s._declaration_decision("t1", ["O2"], []))), "already asked"),
    ("legacy owner-only record", {}, lambda s: s.done_declarations.append(dict(owner="t1")), "already asked"),
])
def test_a_blocker_a_declaration_cannot_settle_keeps_the_edit_route(tmp_path, runs, name, kw, change, why):
    session, runs["run"] = _session(tmp_path, **kw)
    if change:
        change(session)
    plan, said = session._done_declaration_plan({"R1": "held"})
    assert plan == {} and why in said, (name, said)


def test_a_merged_childs_record_spends_the_same_decision_exactly_as_its_own_would(tmp_path, runs):
    """Parallel children's done_declarations come back whole beside the
    parent's (the merge extends the list); the owner and decision they
    record are refused again on the same state, and the record survives a
    checkpoint round trip."""
    session, runs["run"] = _session(tmp_path)
    decided = dict(owner="t1", outcome="no line for a listed id", oids=["O2"],
                   decision=session._declaration_decision("t1", ["O2"], []))
    child = SimpleNamespace(done_declarations=[decided])
    session.done_declarations.extend(dict(d) for d in child.done_declarations)
    assert "already asked" in session._done_declaration_plan({"R1": "held"})[1]
    state = checkpoint.capture_state(session)
    assert state["done_declarations"] == [decided]
    resumed, _ = _session(tmp_path / "resumed")
    checkpoint.restore_state(resumed, state)
    assert "already asked" in resumed._done_declaration_plan({"R1": "held"})[1], "the same state, resumed"


def test_a_new_decision_for_an_asked_owner_gets_its_own_declaration(tmp_path, runs):
    """Root's control on 9c17005 (PR73 6716888092): t1 was asked about O2 on
    S; later work left a new held item O9 on S2. That is another decision,
    admitted once; the same O9 on S2 again is refused."""
    session, runs["run"] = _session(tmp_path, entries=[dict(ITEM, oid="O9")])
    asked_before = dict(owner="t1", source="S", oids=["O2"], pids=[], outcome="declared",
                        decision=session._declaration_decision("t1", ["O2"], []))
    session.done_declarations = [asked_before]
    session._source_fingerprint = lambda: "S2"
    session.checks = [dict(source="S2", passed=True, environment="E", gate="")]
    plan, why = session._done_declaration_plan({"R1": "new held item after later work"})
    assert why == "" and plan["t1"]["oids"] == ["O9"]
    session.done_declarations.append(dict(owner="t1", oids=["O9"], decision=plan["t1"]["decision"]))
    assert "already asked" in session._done_declaration_plan({"R1": "held"})[1], "the same new state is asked once"


def test_roots_control_a_record_from_before_the_decision_is_matched_by_its_source_and_ids(tmp_path, runs):
    """Root's exact control on 9c17005: the record 9c17005 wrote (owner,
    source S, O2) against a new held item O9 on S2. Not the same decision."""
    session, runs["run"] = _session(tmp_path, entries=[dict(ITEM, oid="O9")])
    session.done_declarations = [dict(owner="t1", source="S", oids=["O2"], pids=[], outcome="declared")]
    session._source_fingerprint = lambda: "S2"
    session.checks = [dict(source="S2", passed=True, environment="E", gate="")]
    plan, why = session._done_declaration_plan({"R1": "new held item after later work"})
    assert why == "" and plan["t1"]["oids"] == ["O9"], why
    session.done_declarations = [dict(owner="t1", source="S2", oids=["O9"], pids=[], outcome="declared")]
    assert "already asked" in session._done_declaration_plan({"R1": "held"})[1], "the same state as it recorded it"


@pytest.mark.parametrize("name, change", [
    ("source", lambda s: setattr(s, "_source_fingerprint", lambda: "S3")),
    ("environment", lambda s: setattr(s, "_environment_id", lambda: "E2")),
    ("item", lambda s: s.unexecuted_acceptance[0].update(item="app.py cli.py")),
    ("requirement revision", lambda s: setattr(s, "_requirement_revisions", lambda: {"R1": 2})),
])
def test_each_part_of_the_decision_moves_it_and_the_answer_never_does(tmp_path, runs, name, change):
    session, runs["run"] = _session(tmp_path)
    before = session._declaration_decision("t1", ["O2"], [])
    session.done_declarations.append(dict(owner="t1", outcome="unparseable: names no id", decision=before))
    assert session._declaration_decision("t1", ["O2"], []) == before, "a recorded answer changes nothing"
    change(session)
    assert session._declaration_decision("t1", ["O2"], []) != before, name


@pytest.mark.parametrize("name, effect, outcome", [
    ("capped", TurnLimitReached("cap", turns=6, partial_text="RUNS: O2 - " + CASE), "capped at 6 rounds"),
    ("finish", FinishReserved("keep the finish"), "refused to keep the finish"),
    ("refused", ProviderError("the seat declined"), "stopped: ProviderError"),
    ("moved", "moved", "void: source changed during the call"),
])
def test_a_declaration_call_that_does_not_answer_on_the_same_source_leaves_every_id_held(tmp_path, runs, name,
                                                                                         effect, outcome):
    session, runs["run"] = _session(tmp_path)
    read = []
    session._record_runs = lambda *a: read.append(a)
    session._record_proves = lambda *a: read.append(a)

    def invoke(key, prompt):
        # The rules the reader applies are the ones the prompt states.
        assert key == "claude:opus" and "Do not change files" in prompt and "O2" in prompt
        assert "Only lines naming the ids above (O2) are read" in prompt
        assert "a RUNS or PROVES line that names no id leaves the whole reply unread" in prompt
        assert "'RUNS: O<n> - file::[Class::]test_case'" in prompt
        if effect == "moved":
            session._source_fingerprint = lambda: "T"
            return f"RUNS: O2 - {CASE}"
        raise effect
    session._invoke_model = invoke
    record = session._declare_at_done("t1", dict(lead="claude:opus", intent="", oids=["O2"], pids=[]))
    assert record["outcome"].startswith(outcome) and record["held"] == ["O2"] and read == []
    assert session.done_declarations == [record], "recorded before the call: never offered again"
    assert record["decision"] == session._declaration_decision("t1", ["O2"], []) or name == "moved"
    if name == "capped":
        assert record["partial_artifact"], "the partial text is kept and never read"
    if name == "finish":
        assert session.finish_refusals[-1]["what"] == "DONE declaration"


def test_only_listed_ids_are_read_and_an_unnamed_line_unreads_the_reply(tmp_path, runs):
    session, runs["run"] = _session(tmp_path, rows=[dict(STALE)])
    read = []
    session._record_runs = lambda key, role, text: read.append(("runs", session._active_spec.task_id, text))
    session._record_proves = lambda key, role, text: read.append(("proves", session._active_spec.task_id, text))
    record = dict(dropped=[], intent="")
    session._read_done_declaration("t1", "claude:opus", f"RUNS: O2 - {CASE}\nRUNS: O9 - {CASE}\nPROVES: P1 - "
                                   f"{CASE}\nPROVES: P7 - {CASE}", ["O2"], ["P1"], record)
    assert record["outcome"] == "declared" and record["dropped"] == [f"RUNS: O9 - {CASE}", f"PROVES: P7 - {CASE}"]
    assert read == [("proves", "t1", f"PROVES: P1 - {CASE}"), ("runs", "t1", f"RUNS: O2 - {CASE}")]
    assert session._active_spec is None, "the owner is the active task only while its lines are read"
    read.clear()
    record = dict(dropped=[], intent="")
    session._read_done_declaration("t1", "claude:opus", f"RUNS: O2 - {CASE}\nRUNS: the add case", ["O2"], [], record)
    assert record["outcome"].startswith("unparseable") and read == []


def test_a_declared_case_settles_only_on_a_passing_receipt_of_this_source_gate_and_environment(tmp_path, runs):
    session, runs["run"] = _session(tmp_path)
    entry = dict(ITEM, cases=[CASE], binding=dict(kind="case", cases=[CASE]), restatements=[dict(task="t1")],
                 gate="", environment="E")
    session._discharge_record = Session._discharge_record.__get__(session)
    assert session._discharge_record(entry) is not None, "the receipt on record settles it"
    for name, change in [("stale", dict(source="R")), ("failed", dict(passed=False)),
                         ("environment", dict(environment="other")), ("gate", dict(gate="another"))]:
        session.checks = [dict(source="S", passed=True, environment="E", gate="", **{})]
        session.checks[0].update(change)
        assert session._discharge_record(entry) is None, name
    session.checks = [dict(source="S", passed=True, environment="E", gate="")]
    runs["run"] = dict(runs["run"], report_cases=["tests/test_app.py::test_other"])
    assert session._discharge_record(entry) is None, "a run that did not execute the declared case"


# -- the audit's one required-line list --------------------------------------------------------------

def _audit_session(tmp_path):
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(project=None))
    unexecuted = dict(ITEM)
    restated = dict(ITEM, oid="O7", item="cli.py", cases=[CASE], requirements=["R2"],
                    restatements=[dict(task="t1", author="claude:opus", cases=[CASE])])
    session.unexecuted_acceptance = [unexecuted, restated]
    session._standing_not_run = lambda: [unexecuted, restated]
    session._discharge_record = lambda e: dict(index=0) if e is restated else None
    session._unnamed_standing = lambda: []
    session._open_inspections = lambda: []
    rows = [dict(oid="O3", task="t1", kind="acceptance", modality="test", execution="ran", runner="gate",
                 requirements=[["R1", 1]], sufficiency="unjudged", behavior="add", gate=""),
            dict(oid="O4", task="t1", kind="acceptance", modality="legacy", execution="missing", runner=None,
                 requirements=[["R2", 1]], sufficiency="unjudged", behavior="docs", gate=""),
            dict(oid="O5", task="t1", kind="acceptance", modality="capture", execution="ran", runner=None,
                 requirements=[["R2", 1]], sufficiency="unjudged", behavior="render", gate=""),
            dict(oid="O6", task="t9", kind="acceptance", modality="test", execution="ran", runner="gate",
                 requirements=[["R9", 1]], sufficiency="unjudged", behavior="other requirement", gate=""),
            dict(oid="O7", task="t1", kind="not_run", modality="test", execution="missing", runner="gate",
                 requirements=[["R2", 1]], behavior="cli.py", entry=dict(item="cli.py"))]
    return session, rows


def test_the_audit_prompt_lists_exactly_what_its_reader_requires_and_the_reader_finds_omissions(tmp_path):
    session, rows = _audit_session(tmp_path)
    audited = {"R1": "add", "R2": "docs"}
    required = session._audit_required_ids(audited, [dict(fid="F1")], rows, "openai:sol")
    assert required == [("R1", "requirement"), ("R2", "requirement"), ("F1", "finding"), ("O3", "row"),
                        ("O4", "row"), ("O7", "restatement")], "no capture row, no row of another requirement"
    assert session._audit_required_ids(audited, [], rows, "claude:opus")[-1] == ("O4", "row"), \
        "a restatement its own author audits is not asked of it"
    block = session._audit_lines_block(required)
    assert "- requirements (MET or NOT MET): R1, R2" in block
    assert "- executed restatements (SUFFICIENT or INSUFFICIENT): O7" in block
    # The restatement is awaiting judgment, never "not shown to have run".
    shown = session._not_run_block()
    head, awaiting = shown.split("## Executed restatements awaiting your judgment")
    assert "app.py" in head and "cli.py" not in head and "- O7:" in awaiting
    assert "listed under this heading" in head, "the NOT MET rule names only its own heading"
    debt = session._obligations_block([r for r in rows if r["kind"] == "not_run"])
    assert "O7" not in debt, "not offered as a debt row to judge nothing about"
    reply = "R1: MET - app.py\n- R2: NOT MET - docs; at README.md; example: none\nO3: SUFFICIENT ran\n**O4**: OK"
    assert session._audit_omitted(reply, required) == ["F1", "O7"], "a malformed line is an answer, not an omission"


def test_the_completion_reads_only_omitted_ids_and_a_conflict_in_it_judges_nothing(tmp_path):
    session, _rows = _audit_session(tmp_path)
    asked = []

    def complete(spec, peer, kind, raw, gaps, **kw):
        asked.append(kw["prompt"])
        session.judgment_completions.append(dict(task="audit", seat=peer, kind="audit", outcome="answered",
                                                 raw_artifact="raw", reply_artifact="rep"))
        return "R1: MET - app.py\nF1: UNRESOLVED stands\nO7: SUFFICIENT covers it\nO7: INSUFFICIENT does not"
    session._complete_judgment = complete
    required = [("R1", "requirement"), ("F1", "finding"), ("O7", "restatement")]
    merged, record = session._complete_audit("openai:sol", "R1: NOT MET - wrong", ["F1", "O7"], required, "BODY")
    assert asked and asked[0].startswith("BODY") and "R1" not in asked[0].rsplit("must contain", 1)[1]
    assert record["dropped_lines"] == 1 and record["answered"] == ["F1", "O7"] and record["outcome"] == "answered"
    assert merged.splitlines()[0] == "R1: NOT MET - wrong" and "R1: MET" not in merged
    verdicts, conflicts = judgment.sufficiency_reading(merged, ["O7"])
    assert "O7" in conflicts and "O7" not in verdicts, "a conflicting completion judges nothing"
    session._complete_judgment = lambda *a, **k: None
    session.judgment_completions.append(dict(task="audit", seat="openai:sol", kind="audit",
                                             outcome="not attempted: the job's allowance is spent"))
    merged, record = session._complete_audit("openai:sol", "R1: MET - x", ["O7"], required, "BODY")
    assert merged == "R1: MET - x" and record["still_omitted"] == ["O7"]
    assert record["outcome"].startswith("not attempted")


@pytest.mark.parametrize("snap, short", [
    (dict(reserved_attempts=9, reported_tokens=0, elapsed_seconds=0), "no call is left"),
    (dict(reserved_attempts=1, reported_tokens=950, elapsed_seconds=0), "do not hold one reserve-sized call"),
    (dict(reserved_attempts=1, reported_tokens=0, elapsed_seconds=600), "no wall time is left"),
])
def test_a_completion_the_budget_cannot_hold_is_neither_claimed_nor_called(tmp_path, snap, short):
    """test_finish_capacity's nine-call run: the audit took the last call, and
    a completion would have reached the latched call limit and stopped the
    run. It is priced first: no claim, no call, the omission stands."""
    session, _rows = _audit_session(tmp_path)
    limits = dict(max_calls=9, max_reported_tokens=1000, reserve_tokens_per_call=100, wall_seconds=600)
    session._budget = lambda: SimpleNamespace(snapshot=lambda: dict(snap, limits=limits))
    session._complete_judgment = lambda *a, **k: pytest.fail("no completion call may be made")
    merged, record = session._complete_audit("openai:sol", "R1: MET - x", ["O7"], [("O7", "restatement")], "BODY")
    assert merged == "R1: MET - x" and short in record["outcome"] and record["still_omitted"] == ["O7"]
    assert session.judgment_completions == [], "the shared allowance is not spent"
