"""Cycle02 (Davis, 2026-11-07, PR73 6705254133), after completion-cycle01
ended INCOMPLETE with every grader passing:

1. A NOT RUN item naming only production files (O15: tickets/api.py and
   tickets/cli.py) had no route: no check can take a production module as a
   whole test operand. The owning lead may now restate it as exact cases
   ('RUNS: O<n> - file::case'), or a later task takes it over with
   'DISCHARGES: O<n>'. The harness settles the cases from a complete passing
   run on the current source, and the independent audit judges, by its own
   '<O-id>: SUFFICIENT|INSUFFICIENT' line, whether they exercise the whole
   reported behaviour.
2. A bound case a later task removed (O24) is marked stale at the first check
   that shows it, and the existing exact-redeclaration route is named.
3. A DONE whose requirements the record already holds not met is sent back
   before an audit is bought while a continuation is admissible; the cap's
   goal question is not asked when the record already makes completion
   impossible.

Real Session and controller with scripted providers and the project's own
pytest gate."""
import re

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios, recheck_reply
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
from tests.lifecycle.test_proof_binding import (
    CASE,
    NEGATIVE,
    PROOF_R1,
    SHOWN,
    _at,
    _plan,
    _proofs,
    _scope,
)
from tests.lifecycle.test_workflow_repair import MET, SKIP_TESTS, Seen, _script

CODE = "BLOCKING: no negative case covers subtraction of negatives | basis: R1 | impact: correctness"
PRODUCTION = "NOT RUN: app.py - its socket path is outside my allowed paths, so I did not exercise it"
PLAN1 = _plan(_scope(proofs=_proofs("R1")))
JUDGE = re.compile(r"judge it only by your own line '(O\d+): SUFFICIENT")
RESTATE = re.compile(r"^- (O\d+)(?: \(reported by \w+, holds [^)]*\))?: NOT RUN: app\.py", re.MULTILINE)


def _lead(*, tests=None, note=PRODUCTION):
    def lead(call, replay):
        files = {"app.py": FIXED}
        if tests is not None:
            files["tests/test_app.py"] = tests
        H.write(call, files)
        return f"Implemented add.\nPROVES: P1 - {CASE}\n{note}\nCHANGED: {sorted(files)!r}".replace("'", '"')
    return lead


def _restating(case=CASE):
    """A revision that restates the task's own production-path item."""
    def revision(call, replay):
        found = RESTATE.search(call.prompt)
        assert found, "the revision prompt names the item and the RUNS form"
        return f"Restated the item as the case that exercises it.\nRUNS: {found.group(1)} - {case}\nCHANGED: []"
    return revision


def _auditor(verdict="SUFFICIENT test_add drives add through the path the item named and asserts its result"):
    """The default scenario judgment plus the audit's own line on every
    restated item the prompt asks it to judge."""
    base = judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN)

    def auditor(call, replay):
        reply = base(call, replay)
        return reply + "".join(f"\n{oid}: {verdict}" for oid in dict.fromkeys(JUDGE.findall(call.prompt)))
    return auditor


REVIEW_JUDGE = re.compile(r"^- (O\d+) \(reported by t\d+\): NOT RUN: .*; restated by this task's lead as ", re.MULTILINE)


def _judging(base, verdict="SUFFICIENT test_add drives add through the path the item named and asserts its result"):
    """A reviewer's reply plus its own line on every restated item it is
    handed; ``verdict`` None writes none."""
    def reviewer(call, replay):
        reply = base(call, replay)
        if verdict is None:
            return reply
        return reply + "".join(f"\n{oid}: {verdict}" for oid in dict.fromkeys(REVIEW_JUDGE.findall(call.prompt)))
    return reviewer


def _item(replay):
    (entry,) = [e for e in H.result_json(replay)["unexecuted_acceptance"] if e["item"] == "app.py"]
    return entry


# -- 1. the owning lead restates a production-path item and the audit judges it ------------------

@pytest.mark.requirements_ledger
def test_a_production_path_item_restated_as_cases_settles_on_the_audits_judgment(tmp_path, monkeypatch):
    """Regression for completion-cycle01 O15. Before cycle02 the RUNS line
    was read by nothing, the item stayed held and the audit lowered R1."""
    seen = Seen()
    script = _script(seen, lead=_lead(), collaborator=lambda c, r: CODE + MET, revision=_restating(),
                     recheck=_judging(lambda c, r: recheck_reply(c.prompt, default="RESOLVED handled")),
                     auditor=judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN))
    replay = H.run(_at(tmp_path, "restated"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    entry = _item(replay)
    assert entry["executable"] is False and entry["task"] == "t1" and entry["why"].startswith("its socket path")
    (restated,) = entry["restatements"]
    assert restated["cases"] == [CASE] and restated["task"] == "t1" and restated["previous"]["cases"] == []
    assert entry["binding"]["kind"] == "case" and entry["cases"] == [CASE]
    judged = entry["restated_judgment"]
    assert judged["verdict"] == "sufficient" and judged["cases"] == [CASE] and judged["by"] != restated["author"]
    record = H.result_json(replay)
    assert [c["stage"] for c in record["review_checks"]] in (["review"], ["review", "recheck"]), \
        "settled against recorded runs"
    from tools.truth_check import _not_run_cleared
    assert _not_run_cleared(entry, record["checks"], record), "the saved record clears it by the same rule"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("name, verdict", [("insufficient", "INSUFFICIENT it never reaches the socket path"),
                                           ("unjudged", None)])
def test_a_restated_item_without_a_sufficient_judgment_stays_held(tmp_path, monkeypatch, name, verdict):
    seen = Seen()
    auditor = _auditor(verdict) if verdict else judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN)
    script = _script(seen, lead=_lead(), collaborator=lambda c, r: CODE + MET, revision=_restating(),
                     recheck=_judging(lambda c, r: recheck_reply(c.prompt, default="RESOLVED handled"), verdict),
                     auditor=auditor)
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    entry = _item(replay)
    assert entry["restatements"] and (entry.get("restated_judgment") or {}).get("verdict") != "sufficient"
    from tools.truth_check import _not_run_cleared
    record = H.result_json(replay)
    assert not _not_run_cleared(entry, record["checks"], record)
    assert not replay.result.completed


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("name, tests, case, problem", [
    ("skipped", SKIP_TESTS, NEGATIVE, None),
    ("absent", None, "tests/test_app.py::test_absent", "not defined where it says"),
])
def test_a_restatement_that_is_not_executed_or_not_defined_never_clears(tmp_path, monkeypatch, name, tests, case,
                                                                        problem):
    seen = Seen()
    script = _script(seen, lead=_lead(tests=tests), collaborator=lambda c, r: CODE + MET,
                     revision=_restating(case), recheck=_judging(lambda c, r: recheck_reply(c.prompt, default="RESOLVED")),
                     auditor=_auditor())
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    entry = _item(replay)
    record = H.result_json(replay)
    if problem:
        assert not entry.get("restatements"), "an undefined case restates nothing"
        assert any(problem in p["problem"] for p in record["proof_binding_problems"])
    else:
        assert entry["restatements"] and entry["restated_judgment"]["verdict"] == "sufficient", \
            "judged sufficient, and still not executed: the skip holds it"
    assert not replay.result.completed


def test_a_restatement_by_another_task_or_judged_by_its_author_counts_for_nothing(tmp_path):
    from types import SimpleNamespace

    from tests.lifecycle.test_proof_binding import _session
    session = _session(tmp_path)
    entry = dict(task="t1", oid="O2", item="app.py", why="", cases=[], binding=dict(kind="paths"),
                 requirements=["R1"], after_check=0)
    session.unexecuted_acceptance = [entry]
    session._standing_not_run = lambda: [entry]
    session._active_spec = SimpleNamespace(task_id="t2")
    session._record_runs("claude:opus", "lead", f"RUNS: O2 - {CASE}")
    assert not entry.get("restatements"), "t2 neither reported nor took it over"
    assert any("not a NOT RUN item this task" in p["problem"] for p in session.proof_binding_problems)
    entry.update(restatements=[dict(task="t1", author="claude:opus", cases=[CASE])], cases=[CASE],
                 binding=dict(kind="case", cases=[CASE]))
    session._judge_restated("claude:opus", f"O2: SUFFICIENT {CASE} exercises it")
    assert entry.get("restated_judgment") is None, "the restatement's author cannot judge it"
    session._judge_restated("openai:sol", f"O2: SUFFICIENT {CASE} exercises it")
    assert session._restated_judged(entry)
    entry["cases"] = ["tests/test_app.py::test_other"]
    assert not session._restated_judged(entry), "a judgment of other cases does not carry over"


# -- 2. a later task takes the item over -----------------------------------------------------------

def _taking(oid_holder):
    plan = ("KIND: backend simple\nSCOPE: " + __import__("json").dumps(_scope(proofs=_proofs("R1")))
            + "\nCover add's socket path with its existing test.\nCOVERS: R1\nDISCHARGES: {oid}")

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return PLAN1
        if n == 2:
            found = re.search(r"'DISCHARGES: (O\d+)'", call.prompt)
            assert found, "the planner is shown the takeover route for the held item"
            oid_holder.append(found.group(1))
            return plan.replace("{oid}", oid_holder[-1])
        return "DONE"
    return orchestrator


@pytest.mark.requirements_ledger
def test_a_later_task_takes_over_the_item_its_lead_restates_it_and_the_audit_judges(tmp_path, monkeypatch):
    seen, taken = Seen(), []

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        assert "## NOT RUN items this task takes over" in call.prompt and taken[-1] in call.prompt
        H.write(call, {"app.py": FIXED})
        return f"Covered.\nPROVES: P1 - {CASE}\nRUNS: {taken[-1]} - {CASE}\nCHANGED: [\"app.py\"]"
    script = _script(seen, orchestrator=_taking(taken), lead=lead, auditor=_auditor(),
                     collaborator=_judging(lambda c, r: "NO FINDINGS" + MET))
    replay = H.run(_at(tmp_path, "takeover"), monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    entry = _item(replay)
    assert entry["task"] == "t1" and [t["task"] for t in entry["taken_over_by"]] == ["t2"]
    assert entry["restatements"][-1]["task"] == "t2" and entry["restated_judgment"]["verdict"] == "sufficient"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_discharges_line_naming_no_standing_item_is_sent_back(tmp_path):
    from tests.lifecycle.test_proof_binding import _session
    session = _session(tmp_path)
    session._standing_not_run = lambda: [dict(oid="O2")]
    assert session._discharges_problem(None) == ""
    assert "names no item" in session._discharges_problem([])
    assert "O9" in session._discharges_problem(["O9"]) and "standing: O2" in session._discharges_problem(["O9"])
    assert session._discharges_problem(["O2"]) == ""


# -- 3. DONE meets the record's blockers before an audit is bought -----------------------------------

@pytest.mark.requirements_ledger
def test_a_done_over_a_held_item_is_sent_back_before_any_audit_while_a_continuation_is_admissible(tmp_path,
                                                                                                    monkeypatch):
    """Regression for completion-cycle01's ordering: the planner said DONE
    with O15 held; a 307k audit and 266k of planning were spent before the
    repair could not be paid. Now the first DONE costs no audit."""
    seen, taken, audits_at_done = Seen(), [], []

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return PLAN1
        if n == 2:
            audits_at_done.append(len(replay.of("auditor")))
            return "DONE"
        if n == 3:
            assert "No audit was asked" in call.prompt and "DISCHARGES: O" in call.prompt
            found = re.search(r"'DISCHARGES: (O\d+)'", call.prompt)
            taken.append(found.group(1))
            return ("KIND: backend simple\nSCOPE: " + __import__("json").dumps(_scope(proofs=_proofs("R1")))
                    + f"\nCover add's socket path with its existing test.\nCOVERS: R1\nDISCHARGES: {taken[-1]}")
        return "DONE"

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        H.write(call, {"app.py": FIXED})
        return f"Covered.\nPROVES: P1 - {CASE}\nRUNS: {taken[-1]} - {CASE}\nCHANGED: [\"app.py\"]"
    auditor_calls = []

    def auditor(call, replay):
        auditor_calls.append(len(replay.of("orchestrator")))
        return _auditor()(call, replay)
    script = _script(seen, orchestrator=orchestrator, lead=lead, auditor=auditor,
                     collaborator=_judging(lambda c, r: "NO FINDINGS" + MET))
    replay = H.run(_at(tmp_path, "done-gate"), monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    (gate, *_rest) = record["done_gates"]
    assert gate["audit_asked"] is False and "R1" in gate["blocked"] and gate["continuation"]["feasible"]
    assert auditor_calls and min(auditor_calls) >= 3, "no audit before the continuation ran"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_with_no_slot_left_the_audit_is_still_asked_and_the_stop_says_so(tmp_path, monkeypatch):
    seen = Seen()
    script = _script(seen, lead=_lead(), auditor=judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1,
                                                                   basis=SHOWN))
    replay = H.run(_at(tmp_path, "no-slot"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    record = H.result_json(replay)
    assert not replay.result.completed
    assert all(g["audit_asked"] for g in record["done_gates"])


# -- 4. the cap's goal question is not asked when nothing could complete the run --------------------

@pytest.mark.requirements_ledger
def test_at_the_cap_a_known_incomplete_record_skips_the_goal_question(tmp_path, monkeypatch):
    """Regression for completion-cycle01's final 168,783-token confirmation:
    the DONE audit already judged this source, a held item no cap step can
    settle remains, and no slot is left. The goal question is not asked."""
    seen = Seen()
    plain = ("KIND: backend simple\nSCOPE: " + __import__("json").dumps(_scope(proofs=_proofs("R1")))
             + "\nTidy add.\nCOVERS: R1")
    replies = iter([PLAN1, "DONE", "DONE", plain])

    def orchestrator(call, replay):
        return next(replies, "DONE")

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        return f"Nothing to change.\nPROVES: P1 - {CASE}\nCHANGED: []"
    script = _script(seen, orchestrator=orchestrator, lead=lead,
                     auditor=judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN))
    replay = H.run(_at(tmp_path, "cap"), monkeypatch, script, files=FILES, max_tasks=2, record_complete=False)
    record = H.result_json(replay)
    gates = record["done_gates"]
    assert [g["audit_asked"] for g in gates] == [False, True], "refused once, then the repeated DONE is audited"
    assert len(replay.of("orchestrator")) == 4, "no goal question after the cap"
    skipped = record["goal_question_skipped"]
    assert "R1" in skipped["reason"] and skipped["source"] == record["source_fingerprint"]
    assert skipped["slots_used"] == 2 and skipped["max_tasks"] == 2 and skipped["budget"]
    assert "the goal question was not asked" in (replay.result.error or "")
    assert "task cap (2) was reached" in (replay.result.error or "")
    assert not replay.result.completed


@pytest.mark.requirements_ledger
def test_at_the_cap_evidence_that_can_still_be_settled_keeps_the_goal_question(tmp_path, monkeypatch):
    seen = Seen()
    script = _script(seen, orchestrator=lambda c, r: PLAN1, lead=_lead(note=""),
                     auditor=judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN))
    replay = H.run(_at(tmp_path, "cap-open"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert len(replay.of("orchestrator")) == 2, "the goal question was asked"
    assert H.result_json(replay)["goal_question_skipped"] is None


# -- 5. a removed bound case is stale at once, and the redeclaration route is named ------------------

@pytest.mark.requirements_ledger
def test_a_bound_case_a_later_task_removed_is_stale_and_its_redeclaration_route_named(tmp_path, monkeypatch):
    """Regression for completion-cycle01 O24: t1 binds a case it added; t2
    renames it. The row is stale at t2's gate, its judgment dropped, and the
    planner is told the exact-redeclaration route."""
    from tests.lifecycle.test_workflow_repair import NEG_TESTS
    requirements = "R1: add(a, b) returns the sum\nR2: README documents add"
    readme = dict(permitted_paths=["README.md", "tests/test_app.py"], intended_result="README documents add",
                  acceptance=["README names add"], max_lines=30,
                  proofs=_proofs("R2", location="README.md", evidence="reading"))
    plans = [_plan(_scope(proofs=_proofs("R1")), "R1", requirements),
             "KIND: docs simple\nSCOPE: " + __import__("json").dumps(readme) + "\nDocument add.\nCOVERS: R2"]
    renamed = NEG_TESTS.replace("def test_negative(", "def test_negative_sum(")
    later = []

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 3:
            later.append(call.prompt)
        return plans[n - 1] if n <= len(plans) else "DONE"

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "tests/test_app.py": NEG_TESTS})
            return f'Implemented add.\nPROVES: P1 - {NEGATIVE}\nCHANGED: ["app.py", "tests/test_app.py"]'
        H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n", "tests/test_app.py": renamed})
        return 'Documented add and renamed its test.\nCHANGED: ["README.md", "tests/test_app.py"]'
    seen = Seen()
    script = _script(seen, orchestrator=orchestrator, lead=lead,
                     collaborator=lambda c, r: "NO FINDINGS" + (MET if c.task == "t1" else "\nR2: MET - README.md"))
    replay = H.run(_at(tmp_path, "stale"), monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    (row,) = [o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance"]
    assert row["binding"].startswith(f"stale: {NEGATIVE}") and row["execution"] != "ran"
    assert row["sufficiency"] == "unjudged" and row["binding_history"][-1]["stale"] == [NEGATIVE]
    assert row["binding_history"][-1]["by_task"] == "t2"
    assert later and "declares this scenario again exactly" in later[0], "the planner reads the route"
    assert not replay.result.completed


def test_a_case_the_run_still_reports_is_never_stale(tmp_path):
    """A dynamic case the source does not define statically but the run
    reports is unknown, not stale; a failed check decides nothing."""
    from tests.lifecycle.test_proof_binding import _session
    session = _session(tmp_path, project=tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_other():\n    assert True\n")
    session._source_fingerprint = lambda: "S"
    row = dict(oid="O1", task="t1", kind="acceptance", modality="test", binding="bound", cases=[CASE],
               requirements=[["R1", 1]], execution="ran", sufficiency="sufficient", discharged_cases=[CASE])
    session.evidence_obligations = [row]
    run = dict(id="check", passed=True, required=True, filtered=False, tests=2, skipped=0, report_cases=[CASE],
               cwd=".")
    import quadratus.session as S
    original = S._check_runs
    try:
        S._check_runs = lambda check, project: [run]
        session.checks = [dict(source="S", passed=True)]
        session._mark_stale_bindings()
        assert row["binding"] == "bound", "the run still reports it"
        run["report_cases"] = ["tests/test_app.py::test_other"]
        session.checks = [dict(source="S", passed=False), ]
        session._stale_checked_at = None
        session._mark_stale_bindings()
        assert row["binding"] == "bound", "a failed check decides nothing"
        session.checks = [dict(source="S", passed=True)]
        session._stale_checked_at = None
        session._mark_stale_bindings()
        assert row["binding"].startswith("stale") and row["sufficiency"] == "unjudged"
        never = dict(row, oid="O2", binding="bound", discharged_cases=None, sufficiency="unjudged", execution="missing")
        session.evidence_obligations = [never]
        session._stale_checked_at = None
        session._mark_stale_bindings()
        assert never["binding"] == "bound", "a case never shown executed is not 'removed'"
    finally:
        S._check_runs = original


# -- 6. prompt parity -------------------------------------------------------------------------------

def test_the_interface_and_single_outcome_rules_are_stated_where_reviewers_and_leads_read_them():
    from quadratus import session as S
    from quadratus.scope import TEST_SUBSTANCE_RULE
    assert "default responses" in S._CONTRACT_BOUNDARY_RULE and "held to its contract" in S._CONTRACT_BOUNDARY_RULE
    assert "responses a framework or server sends by default" in S._ERROR_BOUNDARY_LENS
    assert "accepts more than one outcome where the goal names one proves neither" in TEST_SUBSTANCE_RULE
    assert "decided first" in TEST_SUBSTANCE_RULE
