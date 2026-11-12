"""Cycle18 (PR73 6742757517, reconciled by root): the independent
restatement judgment is a first ask, its adverse reason reaches whoever can
repair it, and the audit is asked only for what it reads.

Run 20261109 (cycle17, engine cd08b58): ``_judge_restatements`` made its
first ask through ``_complete_judgment`` with ``raw=""``. The ask claimed the
job's one judgment completion and stored the no-reply placeholder
(fe723bcdb73a) as if a reply had arrived. Opus answered INSUFFICIENT for
O22/O46 on source 40e7; on 33dd and fb880 the new decisions were refused
"the job's allowance is spent" and nothing was asked. The INSUFFICIENT
reason reached no planner, lead or route, the finish route said the audit
would judge items the audit may not judge, and the audit prompt listed
those items under "awaiting your judgment" while its parser ignored them.

- 4a: one first ask per decision (judge, restatement author, item, cases,
  source, gate, environment, revisions); the shared completion only for an
  arrived reply that left an asked id without a verdict, with that reply as
  its raw; lines applied to exactly the asked ids.
- 4b: a current independent INSUFFICIENT carries its reason (recorded
  evidence) and the DISCHARGES/RUNS route to the planner, the owning lead
  and the finish route; stale and missing are said so.
- 4c: the audit prompt shows the auditor's own restatements apart, with the
  seat that judges them, and reads lines for its required ids only.
- 5b: the scenario grammar asks for one early-rejection scenario where the
  contract fixes every error response.
"""
import copy
import re
from types import SimpleNamespace

import pytest

from quadratus import providers
from quadratus import session as S
from quadratus.run_budget import FinishReserved
from tests.lifecycle import harness as H
from tests.lifecycle.test_cycle14_closure import ASKED, PLAN_SOL, RESTATED, _judge, _run, _then_done
from tests.lifecycle.test_cycle14_closure import _a2 as _shared_a2
from tests.lifecycle.test_done_declarations import (
    ADVERSE,
    CLEAN,
    SUFFICIENT,
    _answering,
    _declaring,
)
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
from tests.lifecycle.test_proof_binding import CASE, _at
from tests.lifecycle.test_reachable_evidence import _item, _lead
from tests.lifecycle.test_workflow_repair import Seen, _script

pytestmark = pytest.mark.requirements_ledger

PLACEHOLDER = "fe723bcdb73a"  # sha256("(no reply: the call stopped before answering)")[:12]
GAP = "INSUFFICIENT the cases never pass a negative operand through add"


def _a2(tmp_path, *, entries=None, **kw):
    """cycle14's unit session over deep copies: these tests edit nested
    restatement records, which a shallow copy would share across tests."""
    return _shared_a2(tmp_path, entries=copy.deepcopy(entries if entries is not None else [RESTATED]), **kw)


def _restatement_completions(record):
    return [c for c in record["judgment_completions"] if c["task"] == "restatement-judgment"]


# -- 4a lifecycle: source A judged INSUFFICIENT, source B still asked --------------------------------

def test_an_adverse_judgment_on_one_source_leaves_the_next_source_askable_and_routes_its_reason(tmp_path,
                                                                                                monkeypatch):
    """Cycle17 O22/O46: Opus judged the restatement INSUFFICIENT on 40e7, a
    later task changed the source, and the new decision was refused for the
    spent completion. Now: B is a first ask, no completion is claimed, and
    the planner saw A's reason and the DISCHARGES route beside the audit's
    own NOT MET on the behaviour."""
    asked, plans = [], []

    def verdict(call, replay):
        return "\n".join(f"{oid}: {GAP if not asked[:-1] else SUFFICIENT}" for oid in ASKED.findall(call.prompt))

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        H.write(call, {"app.py": FIXED + "\n# t2: handles negative operands\n"})
        return f"Repaired add.\nPROVES: P1 - {CASE}\nCHANGED: [\"app.py\"]"

    audits = []

    def auditor(call, replay):
        audits.append(call)
        return ADVERSE if len(audits) == 1 else _answering(call, replay)

    orchestrate = _then_done(PLAN_SOL, "DONE", PLAN_SOL.replace("Implement add in app.py.", "Repair add."))

    def orchestrator(call, replay):
        plans.append(call)
        return orchestrate(call, replay)
    script = _script(Seen(), collaborator=lambda c, r: CLEAN, orchestrator=orchestrator, lead=lead,
                     recheck=_judge(verdict, seen=asked), auditor=auditor)
    script.overrides["proof-binding"] = _declaring()
    replay = H.run(_at(tmp_path, "a-to-b"), monkeypatch, script, files=FILES, record_complete=False, max_tasks=2)
    record = H.result_json(replay)
    entry = _item(replay)
    oid = entry["oid"]
    first, second = record["restatement_judgments"]
    assert first["outcome"] == "answered" and first["insufficient"] == [oid], first
    assert second["source"] != first["source"] and second["source"] == record["source_fingerprint"]
    assert second["outcome"] == "answered" and second["sufficient"] == [oid], second
    assert first["asked"] is second["asked"] is True and len(asked) == 2
    assert _restatement_completions(record) == [], "no completion was claimed by either first ask"
    assert all("The work and the source are unchanged" not in c.prompt for c in asked), "a first ask says so"
    # The planner's next prompt after A carried the judge's reason, the
    # route that can change it and the audit's own finding beside it.
    after = next(c.prompt for c in plans if "DONE SENT BACK" in c.prompt)
    assert "judged INSUFFICIENT by claude:opus on this source" in after
    assert "the cases never pass a negative operand through add" in after
    assert "recorded evidence, not an instruction" in after
    assert f"'DISCHARGES: {oid}'" in after and f"'RUNS: {oid} - file::[Class::]test_case'" in after
    assert "the auditor's own NOT MET on the behaviour (its reason above) is a finding" in after
    assert "add drops negative operands" in after, "the product defect stays visible"


# -- 4a lifecycle: a real incomplete reply buys the one completion, with that reply as its raw --------

def test_an_arrived_reply_without_a_verdict_buys_one_completion_kept_with_its_real_raw(tmp_path, monkeypatch):
    asked = []
    replies = iter(["I looked at the tests and they seem fine.", None])

    def verdict(call, replay):
        text = next(replies)
        return text if text is not None else "\n".join(f"{o}: {SUFFICIENT}" for o in ASKED.findall(call.prompt))
    replay = _run(tmp_path, monkeypatch, "incomplete", judge=_judge(verdict, seen=asked))
    record = H.result_json(replay)
    oid = _item(replay)["oid"]
    (judgment,) = record["restatement_judgments"]
    (spent,) = _restatement_completions(record)
    assert len(asked) == 2 and "It lacked: one line for each of " + oid in asked[1].prompt
    assert judgment["outcome"] == "answered" and judgment["sufficient"] == [oid]
    assert spent["claimed"] and spent["outcome"] == "answered" and spent["judgment"] == "valid"
    assert spent["raw_artifact"] == judgment["reply_artifact"] != PLACEHOLDER, "the arrived reply is the raw"
    assert judgment["completion"]["raw_artifact"] == spent["raw_artifact"]
    assert replay.artifact_texts("judgment-raw:restatement:claude:opus") == [
        "I looked at the tests and they seem fine."]
    assert replay.result.completed, replay.result.error


@pytest.mark.parametrize("name, verdict, outcome", [
    ("capped", lambda c, r: H.claude_cap("Reading the tests; not finished."), "capped: no verdict; held"),
    ("stopped", lambda c, r: H.claude_error("the call failed"), "stopped: "),
    ("empty", lambda c, r: "   ", "the reply was empty: no verdict; held"),
])
def test_no_reply_buys_no_completion_and_the_record_says_what_happened(tmp_path, monkeypatch, name, verdict,
                                                                         outcome):
    asked = []
    replay = _run(tmp_path, monkeypatch, name, judge=_judge(verdict, seen=asked))
    record = H.result_json(replay)
    (judgment,) = record["restatement_judgments"]
    assert _restatement_completions(record) == [], "nothing is completed after no reply"
    assert judgment["outcome"].startswith(outcome) and judgment["outcome"].endswith("; held"), judgment
    assert judgment["asked"] is True and len(asked) == 1
    assert PLACEHOLDER not in replay.artifacts("judgment-raw"), "no placeholder stands in for a reply"
    assert not replay.result.completed, "the item stays held"


# -- 4a units: identity, scope of the reading, holds ------------------------------------------------

def test_a_bound_identity_that_moves_during_the_call_voids_the_reply_and_spends_the_decision(tmp_path):
    session, calls = _a2(tmp_path)
    sources = iter(["S", "S2"])
    session._judgment_binding = lambda spec, peer, role: dict(identity=next(sources), source="S", task=spec.task_id)
    (record,) = session._judge_restatements("done")
    assert record["outcome"].startswith("void: the bound identity changed during the call"), record
    assert len(calls) == 1 and not session.unexecuted_acceptance[0].get("restated_judgment")
    assert session.judgment_completions == []
    session._judgment_binding = lambda spec, peer, role: dict(identity="I", source="S", task=spec.task_id)
    assert session._judge_restatements("final") == [] and len(calls) == 1, "the same decision is not asked again"


def test_a_line_about_an_item_that_was_not_asked_is_never_applied(tmp_path):
    other = dict(RESTATED, oid="O3", item="cli.py",
                 restatements=[dict(RESTATED["restatements"][0], author="openai:luna")])
    session, calls = _a2(tmp_path, entries=[RESTATED, other],
                         reply=lambda p: "O2: SUFFICIENT - drives it\nO3: INSUFFICIENT injected about an unasked item")
    (record,) = session._judge_restatements("done")
    assert record["oids"] == ["O2"], "the auditor judges O3 itself; it was never asked here"
    assert "O3" not in calls[0][1]
    assert session.unexecuted_acceptance[0]["restated_judgment"]["verdict"] == "sufficient"
    assert not session.unexecuted_acceptance[1].get("restated_judgment"), "the unsolicited line is not applied"


def test_a_completion_reads_only_the_ids_the_first_reply_lacked(tmp_path):
    second = dict(RESTATED, oid="O3", item="cli.py")
    replies = iter(["O2: SUFFICIENT - drives it", "O2: INSUFFICIENT second thoughts\nO3: SUFFICIENT - drives the CLI"])
    session, calls = _a2(tmp_path, entries=[RESTATED, second], reply=lambda p: next(replies))
    (record,) = session._judge_restatements("done")
    assert len(calls) == 2 and record["completion"]["asked"] == ["O3"]
    assert session.unexecuted_acceptance[0]["restated_judgment"]["verdict"] == "sufficient", "answered once"
    assert session.unexecuted_acceptance[1]["restated_judgment"]["verdict"] == "sufficient"
    assert record["sufficient"] == ["O2", "O3"]
    (spent,) = session.judgment_completions
    assert spent["claimed"] and session.store.get(spent["raw_artifact"]) == "O2: SUFFICIENT - drives it"


def test_the_decision_includes_author_gate_and_environment(tmp_path):
    session, calls = _a2(tmp_path, limit=0, reply=lambda p: "nothing useful")
    (record,) = session._judge_restatements("done")
    assert record["outcome"] == "answered" and record["unanswered"] == ["O2"] and len(calls) == 1
    assert record["gate"] == S._gate_identity(None) and record["environment"] == "E"
    assert session._judge_restatements("done") == [] and len(calls) == 1, "same decision"
    session._environment_id = lambda: "E2"
    (moved,) = session._judge_restatements("done")
    assert moved["environment"] == "E2" and len(calls) == 2, "another environment is another decision"
    entry = session.unexecuted_acceptance[0]
    key = session._restatement_decision_key(entry, "claude:opus")
    entry["restatements"][-1] = dict(entry["restatements"][-1], author="openai:luna")
    assert session._restatement_decision_key(entry, "claude:opus") != key, "the author is part of it"


def test_no_admission_leaves_the_item_held_and_asks_nothing(tmp_path):
    """Retained negative control: passes on cd08b58 too."""
    session, calls = _a2(tmp_path)
    session._budget = lambda: object()
    session._admit_plan = lambda *a, **k: SimpleNamespace(feasible=False, detail="the reserve is not left")
    (record,) = session._judge_restatements("done")
    assert record["outcome"] == "not attempted: the reserve is not left; held"
    assert calls == [] and session.judgment_completions == []
    assert session._judge_restatements("final") == [] and calls == [], "the decision is not asked again"


def test_a_finish_hold_on_the_first_ask_buys_no_completion(tmp_path):
    session, calls = _a2(tmp_path)

    def refuse(key, prompt, **kw):
        raise FinishReserved("the finish is held")
    session._invoke_model = refuse
    (record,) = session._judge_restatements("done")
    assert session.judgment_completions == [], "the hold refused an ask, not a completion"
    assert record["outcome"] == "refused to keep the finish; held" and record["asked"]
    assert len(session.finish_refusals) == 1


def _refusing(tmp_path):
    session, _ = _a2(tmp_path)

    def stop(key, prompt, **kw):
        raise providers.ProviderRefusal("vendor text that must stay private")
    session._invoke_model = stop
    return session


def test_a_refusal_is_held_and_its_message_is_not_exported(tmp_path):
    """Retained negative control: passes on cd08b58 too."""
    session = _refusing(tmp_path)
    (record,) = session._judge_restatements("done")
    assert record["outcome"] == "stopped: ProviderRefusal; held"
    assert "vendor text" not in str(record)
    assert not session._restated_judged(session.unexecuted_acceptance[0])


def test_a_refused_first_ask_keeps_its_error_privately_and_claims_no_completion(tmp_path):
    session = _refusing(tmp_path)
    (record,) = session._judge_restatements("done")
    assert session.judgment_completions == []
    assert session.store.get(record["error_artifact"]) == "vendor text that must stay private"


# -- a mutation claim restated as ordinary cases stays honestly held ---------------------------------

MUTATION = dict(RESTATED, oid="O66", item="a deliberate mutation of add makes tests/test_app.py fail",
                why="mutation run needs a writable copy")


def test_a_mutation_check_restated_as_ordinary_cases_stays_held_unless_an_independent_judge_accepts(tmp_path):
    """Retained negative control: passes on cd08b58 too. Ordinary passing
    cases earn nothing by themselves; only an independent SUFFICIENT on a
    restatement that actually injects the fault settles the item."""
    session, calls = _a2(tmp_path, entries=[MUTATION], reply=lambda p: (
        "O66: INSUFFICIENT passing cases on unmutated code do not show the suite fails when add is broken"))
    (record,) = session._judge_restatements("done")
    entry = session.unexecuted_acceptance[0]
    assert record["insufficient"] == ["O66"] and not session._restated_judged(entry)
    assert session._discharging_check(entry) is None, "held: nothing credits ordinary passing cases by itself"
    session, _ = _a2(tmp_path / "injected", entries=[dict(MUTATION, cases=["tests/test_app.py::test_broken_add"])],
                     reply=lambda p: "O66: SUFFICIENT - the case patches add to a broken body and asserts failure")
    session._judge_restatements("done")
    assert session._restated_judged(session.unexecuted_acceptance[0])


def test_a_mutation_checks_adverse_reason_is_what_the_route_carries(tmp_path):
    session, _ = _a2(tmp_path, entries=[MUTATION], reply=lambda p: (
        "O66: INSUFFICIENT passing cases on unmutated code do not show the suite fails when add is broken"))
    session._judge_restatements("done")
    status = session._restatement_status(session.unexecuted_acceptance[0])
    assert "judged INSUFFICIENT by claude:opus" in status and "do not show the suite fails" in status


# -- 4b units: reason and route where they are read -------------------------------------------------

def _routed(tmp_path, *, judged=None, stated=False):
    session, _ = _a2(tmp_path)
    session._discharging_check = lambda e: None
    entry = session.unexecuted_acceptance[0]
    if judged is not None:
        entry["restated_judgment"] = dict(dict(by="claude:opus", verdict="insufficient", why="never reaches cli.py",
                                               cases=[CASE], source="S", revisions={"R1": 1}), **judged)
    if stated:
        session.requirement_audits.append(dict(stated=["R1"], source="S"))
    return session, entry


def test_a_current_adverse_judgment_reaches_the_finish_route_the_planner_row_and_the_owning_lead(tmp_path):
    session, entry = _routed(tmp_path, judged={})
    route = session._evidence_route("R1")["route"]
    assert "judged INSUFFICIENT by claude:opus on this source" in route and "never reaches cli.py" in route
    assert "'DISCHARGES: O2'" in route and "awaits the audit" not in route
    note = session._row_route_note(dict(kind="not_run", oid="O2"))
    assert "judged INSUFFICIENT by claude:opus" in note and "'DISCHARGES: O2'" in note
    entry["taken_over_by"] = [dict(task="t3", after_check=1)]
    block = session._discharges_block(SimpleNamespace(task_id="t3"))
    assert "never reaches cli.py" in block and "Your 'RUNS: O2 - file::[Class::]test_case' lines replace" in block


def test_a_stale_or_missing_judgment_is_never_shown_as_a_current_adverse_decision(tmp_path):
    session, _ = _routed(tmp_path, judged=dict(source="S0"))
    route = session._evidence_route("R1")["route"]
    assert "INSUFFICIENT judgment by claude:opus is stale (another source)" in route
    assert "judged INSUFFICIENT by" not in route and "never reaches cli.py" not in route
    assert "awaits a fresh judgment by claude:opus, an independent seat" in route
    session, _ = _routed(tmp_path / "missing")
    route = session._evidence_route("R1")["route"]
    assert "awaits an independent judgment by claude:opus, an independent seat, because the auditor openai:sol" \
        in route
    session._independent_judge = lambda *a, **k: None
    assert "no eligible independent seat (the auditor openai:sol wrote the restatement): held" in \
        session._evidence_route("R1")["route"]


def test_an_asked_decision_is_named_as_spent_not_as_awaiting(tmp_path):
    session, calls = _a2(tmp_path)
    session._budget = lambda: object()
    session._admit_plan = lambda *a, **k: SimpleNamespace(feasible=False, detail="no reserve")
    session._judge_restatements("done")
    session._discharging_check = lambda e: None
    route = session._evidence_route("R1")["route"]
    assert "already asked (not attempted: no reserve; held); held, and not asked again" in route


def test_a_product_defect_beside_held_evidence_stays_visible_and_a_lowering_alone_does_not_claim_one(tmp_path):
    session, _ = _routed(tmp_path, judged={}, stated=True)
    route = session._evidence_route("R1")["route"]
    assert route.startswith("the auditor's own NOT MET on the behaviour (its reason above) is a finding")
    assert "Separately, execution debt:" in route and "judged INSUFFICIENT" in route
    session, _ = _routed(tmp_path / "lowered", judged={})
    assert "the auditor's own NOT MET" not in session._evidence_route("R1")["route"]


# -- 4c: the audit prompt, its required list and its reader agree ------------------------------------

def test_the_audit_is_shown_its_own_restatements_apart_and_its_line_on_them_is_not_read(tmp_path, monkeypatch):
    audits = []

    def auditor(call, replay):
        audits.append(call)
        oid = re.search(r"reported NOT RUN: app\.py.*?; restated by", call.prompt)
        assert oid
        return _answering(call, replay) + "\nO2: SUFFICIENT - the auditor would accept its own restatement"
    script = _script(Seen(), collaborator=lambda c, r: CLEAN, orchestrator=_then_done(), lead=_lead(),
                     recheck=_judge(GAP), auditor=auditor)
    script.overrides["proof-binding"] = _declaring()
    replay = H.run(_at(tmp_path, "audit-parity"), monkeypatch, script, files=FILES, record_complete=False,
                   max_tasks=2)
    entry = _item(replay)
    oid = entry["oid"]
    prompt = audits[0].prompt
    assert "## Executed restatements another seat judges (harness records)" in prompt
    assert "## Executed restatements awaiting your judgment" not in prompt, "nothing asks for an ignored line"
    assert "The auditor's seat (openai:sol) wrote these restatements" in prompt
    assert f"{oid}: judged INSUFFICIENT by claude:opus on this source" in prompt
    required = prompt.split("## Lines this reply must contain", 1)[1]
    assert oid not in required, "the required list never names the auditor's own restatement"
    judged = entry["restated_judgment"]
    assert judged["by"] == "claude:opus" and judged["verdict"] == "insufficient", "the adverse decision stands"
    assert not replay.result.completed


def test_the_audit_reads_restatement_lines_for_its_required_ids_only(tmp_path):
    session, _ = _a2(tmp_path, auditor="openai:sol")
    session.unexecuted_acceptance[0]["restatements"][-1]["author"] = "claude:opus"
    other = dict(RESTATED, oid="O3", item="cli.py")
    other["restatements"] = [dict(RESTATED["restatements"][0], author="claude:opus")]
    session.unexecuted_acceptance.append(other)
    session._judge_restated("openai:sol", "O2: SUFFICIENT - read\nO3: SUFFICIENT - not asked", oids=["O2"])
    assert session.unexecuted_acceptance[0]["restated_judgment"]["verdict"] == "sufficient"
    assert not session.unexecuted_acceptance[1].get("restated_judgment")
    import inspect
    source = inspect.getsource(S.Session._audit_requirements)
    assert 'oids=[i for i, k in required if k == "restatement"]' in source


# -- 5b: the scenario grammar asks for one early rejection, generally --------------------------------

def test_the_scope_request_asks_for_one_early_rejection_scenario_in_the_scenario_grammar(tmp_path):
    rule = S._EARLY_REJECTION_SCENARIO_RULE
    scope = S._SCOPE_REQUEST
    assert rule in scope
    assert scope.index('Declare the proof scenarios in SCOPE as "proofs"') < scope.index(rule)
    assert "before that handler runs" in rule and "what the client actually receives" in rule
    for specific in ("HTTP/", "nope", "ticket", "0.9", "send_error", "malformed request line"):
        assert specific not in rule, "general guidance, no benchmark case"
    for word in ("must", "required", "refused", "sent back"):
        assert word not in rule.lower(), "guidance only: no new gate"
