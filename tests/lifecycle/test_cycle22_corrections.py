"""Cycle22 (root's reconciliation of assessment 6754811165, run21 on 652b055).

A. A NOT RUN item bound to an exact case that a later task renamed away
   (run21 O37) is settled by its owning lead's RUNS declaration of current
   exact cases at DONE, against the receipt already on record, judged by an
   independent seat: no new gate run, no slot, no alias.
B. NOT RUN ranges read one way (ASCII hyphen and en dash), and a mixed
   mutation claim stays held.
C. A bundle with a binding stage and an editor revision folds the binding
   into the revision: the scenario + binding + editor + two-reviewer bundle
   is four calls, both reviewers recheck.
D. A later reviewer's task-qualified SCENARIO t<k>:P<n> is corrected once by
   the planner on the earlier task's own rows.

Real Session and controller with scripted providers and the project's own
pytest gate.
"""
import dataclasses
import json

import pytest

from quadratus import session as S
from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios, recheck_reply, required_lines
from tests.lifecycle.test_done_declarations import CLEAN, PLAN1, SUFFICIENT
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_workflow_repair import MET, Seen, _script

pytestmark = pytest.mark.requirements_ledger

OLD = "tests/test_app.py::test_cleanup"
NEW = "tests/test_app.py::test_cleanup_keeps_result"
BASE_TESTS = "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
OLD_TESTS = BASE_TESTS + "\n\ndef test_cleanup():\n    assert add(0, 0) == 0\n"
NEW_TESTS = BASE_TESTS + "\n\ndef test_cleanup_keeps_result():\n    assert add(0, 0) == 0\n"
PLAN2 = PLAN1.replace("REQUIREMENTS:\nR1: add(a, b) returns the sum\n", "").replace(
    "Implement add in app.py.", "Rename the cleanup test in tests/test_app.py to say what it keeps.")


def _leads(*, t2_extra=""):
    """t1 reports the cleanup case NOT RUN by exact identity; t2 renames it."""
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED, "tests/test_app.py": OLD_TESTS})
            return (f"Implemented add.\nPROVES: P1 - {CASE}\nNOT RUN: {OLD} - the socket was denied here\n"
                    'CHANGED: ["app.py", "tests/test_app.py"]')
        H.write(call, {"tests/test_app.py": NEW_TESTS})
        return f'Renamed the cleanup test.\nPROVES: P1 - {CASE}\n{t2_extra}CHANGED: ["tests/test_app.py"]'
    return lead


def _orchestrator(*plans):
    replies = iter(plans)
    return lambda call, replay: next(replies, "DONE")


def _answering(verdict=SUFFICIENT):
    def auditor(call, replay):
        base = judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, basis=SHOWN)(call, replay)
        said = {line.split(":", 1)[0].strip() for line in base.splitlines() if ":" in line}
        asked = required_lines(call.prompt)
        return base + "".join(f"\n{oid}: {verdict}" for oid in asked.get("restatement", []) + asked.get("row", [])
                              if oid not in said)
    return auditor


def _run(tmp_path, monkeypatch, name, *, declaration=None, auditor=None, t2_extra="", max_tasks=2):
    seen = Seen()
    script = _script(seen, collaborator=lambda c, r: CLEAN, orchestrator=_orchestrator(PLAN1, PLAN2),
                     lead=_leads(t2_extra=t2_extra), auditor=auditor or _answering())
    if declaration is not None:
        script.overrides["proof-binding"] = declaration
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=max_tasks, record_complete=False)


def _old_item(replay):
    (entry,) = [e for e in H.result_json(replay)["unexecuted_acceptance"] if e["item"] == OLD]
    return entry


def _declare(case=NEW, seen=None):
    def answer(call, replay):
        if seen is not None:
            seen.append(call.prompt)
        line = next(x for x in call.prompt.splitlines() if x.startswith("- O") and OLD in x)
        return f"RUNS: {line[2:].split(':', 1)[0]} - {case}"
    return answer


# -- A ------------------------------------------------------------------------------------------

def test_a_renamed_case_is_settled_at_done_by_its_owners_declaration_with_no_new_run(tmp_path, monkeypatch):
    prompts = []
    replay = _run(tmp_path, monkeypatch, "settled", declaration=_declare(seen=prompts))
    record = H.result_json(replay)
    entry = _old_item(replay)
    oid = entry["oid"]
    assert entry["item"] == OLD and entry["task"] == "t1", "the original item is kept as written"
    (restated,) = entry["restatements"]
    assert restated["role"] == "proof-binding" and restated["cases"] == [NEW]
    assert restated["previous"]["cases"] == [OLD], "the cases it replaced are on the record, never aliased"
    judged = entry["restated_judgment"]
    assert judged["verdict"] == "sufficient" and judged["by"] != restated["author"]
    (prompt,) = prompts
    assert f"its bound case {OLD} is no longer defined on the current source" in prompt
    (decl,) = record["done_declarations"]
    assert decl["owner"] == "t1" and decl["oids"] == [oid] and decl["settled"] == [oid]
    assert len(record["checks"]) == 2, "settled on t2's gate receipt: no new run"
    assert sorted({c.task for c in replay.of("lead")}) == ["t1", "t2"], "no slot beyond the two tasks"
    audit = replay.of("auditor")[0].prompt
    assert f"replacing {OLD}" in audit and "judge the new cases against everything the original item reported" in audit
    assert replay.result.completed, replay.result.error


def test_a_declaration_of_an_undefined_case_leaves_the_item_held(tmp_path, monkeypatch):
    replay = _run(tmp_path, monkeypatch, "absent", declaration=_declare(case="tests/test_app.py::test_missing"))
    entry = _old_item(replay)
    assert not entry.get("restatements") and entry["cases"] == [OLD]
    problems = H.result_json(replay)["proof_binding_problems"]
    assert any("test_missing" in p["problem"] for p in problems)
    assert not replay.result.completed


def test_an_insufficient_judgment_leaves_the_restated_item_held(tmp_path, monkeypatch):
    adverse = "INSUFFICIENT test_cleanup_keeps_result asserts only add(0, 0); the cleanup path is unexercised"
    replay = _run(tmp_path, monkeypatch, "insufficient", declaration=_declare(), auditor=_answering(adverse))
    entry = _old_item(replay)
    assert entry["restatements"] and entry["restated_judgment"]["verdict"] == "insufficient"
    assert not replay.result.completed


def test_a_later_tasks_lead_cannot_restate_another_tasks_item(tmp_path, monkeypatch):
    """Foreign owner: t2's own RUNS line for t1's item binds nothing; the
    declaration still goes to t1's lead alone."""
    prompts = []
    replay = _run(tmp_path, monkeypatch, "foreign", declaration=_declare(seen=prompts), t2_extra="RUNS: O1 - " + NEW
                  + "\n")
    problems = H.result_json(replay)["proof_binding_problems"]
    assert any("not a NOT RUN item this task reported or took over" in p["problem"] and p["task"] == "t2"
               for p in problems)
    (decl,) = H.result_json(replay)["done_declarations"]
    assert decl["owner"] == "t1" and len(prompts) == 1


def test_the_planner_is_shown_the_route_before_done(tmp_path, monkeypatch):
    """With three slots the planner reads the route note after t2, and with
    no declaration answer the item stays held as written."""
    replay = _run(tmp_path, monkeypatch, "route", declaration=lambda c, r: "I cannot say.", max_tasks=3)
    planner = [c.prompt for c in replay.of("orchestrator")]
    assert any(f"its bound case {OLD} is no longer defined on the current source" in p for p in planner)
    entry = _old_item(replay)
    assert not entry.get("restatements") and not replay.result.completed


# -- B ------------------------------------------------------------------------------------------

PLAN_TWO = _plan(_scope(proofs=_proofs("R1", "R1")))


def _two_case_lead(not_run):
    tests = BASE_TESTS + "\n\ndef test_second():\n    assert add(2, 2) == 4\n"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED, "tests/test_app.py": tests})
        return (f"Implemented add.\nPROVES: P1 - {CASE}\nPROVES: P2 - tests/test_app.py::test_second\n"
                f"NOT RUN: {not_run} - the socket was denied here\nCHANGED: [\"app.py\", \"tests/test_app.py\"]")
    return lead


def _range_run(tmp_path, monkeypatch, name, not_run):
    seen = Seen()
    auditor = judging_scenarios(lambda c, r: f"R1: MET - {CASE}", *_proofs("R1", "R1"), basis=SHOWN)
    script = _script(seen, collaborator=lambda c, r: "NO FINDINGS" + MET, orchestrator=_orchestrator(PLAN_TWO),
                     lead=_two_case_lead(not_run), auditor=auditor)
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)


@pytest.mark.parametrize("text", ["P1-P2 HTTP acceptance", "P1–P2 HTTP acceptance", "P1, P2 HTTP acceptance"])
def test_a_range_binds_every_declared_scenario_in_it_whichever_dash(tmp_path, monkeypatch, text):
    replay = _range_run(tmp_path, monkeypatch, "range", text)
    (entry,) = H.result_json(replay)["unexecuted_acceptance"]
    assert entry["binding"]["scenario"] == "P1, P2" and sorted(entry["cases"]) == sorted(
        [CASE, "tests/test_app.py::test_second"])
    assert replay.result.completed, "the gate's own complete run discharged both"


@pytest.mark.parametrize("text, why", [
    ("P1-P3 HTTP acceptance", "names P3, which this task does not declare"),
    ("P2-P1 HTTP acceptance", "is not an increasing range"),
    ("P1-P2-P3 acceptance", "chains more than one range"),
    ("mutation detection and P1–P2 HTTP acceptance", "a claim a passing run cannot establish"),
])
def test_an_unusable_range_or_mixed_mutation_claim_binds_nothing_and_says_why(tmp_path, monkeypatch, text, why):
    replay = _range_run(tmp_path, monkeypatch, "bad", text)
    (entry,) = H.result_json(replay)["unexecuted_acceptance"]
    assert entry["cases"] == [] and why in entry["binding"]["problem"]
    assert not replay.result.completed, "held as written: a passing suite discharges nothing here"


# -- C ------------------------------------------------------------------------------------------

UNBOUND = "BLOCKING: EVIDENCE P1 - no case is bound to P1, so no run can show it | basis: P1 | impact: contract"
CODE = "BLOCKING: no negative case covers add(-1, -2) | basis: R1 | impact: correctness"
SCEN = ("BLOCKING: SCENARIO P1 - the prior state 'two tickets exist' contradicts add, which stores nothing "
        "| proposal: state: nothing is stored | basis: R1 | impact: contract")
SIMPLE1 = _plan(_scope(proofs=_proofs("R1")))
RESOLVING = (f"KIND: backend simple\nSCOPE: {json.dumps(_scope(proofs=_proofs('R1')))}\n"
             "Fix add in app.py as the open findings ask.\nCOVERS: R1\nRESOLVES: RFT1-1")
CODE2 = "BLOCKING: no case covers add(0, -1) | basis: R1 | impact: correctness"


def _bundle_run(tmp_path, monkeypatch, name, *, limit=4):
    """Run21 t8's shape: t2 RESOLVES t1's open finding, and t2's own review
    raises a scenario defect, an unbound scenario and a code defect, so its
    correction bundle carries the planner's correction, the binding, the
    editor revision, its reviewer's recheck and the resolution recheck by
    t1's reviewer. ``limit`` is the ordinary calls left, as for t8."""
    stages_seen = []
    real = S.Session._admit_plan

    def admit(self, owner, stages, **kw):
        verdict = real(self, owner, stages, **kw)
        if kw.get("scope") != "fix-cycle" or owner != "t2":
            return verdict
        stages_seen.append([s.name for s in stages])
        fits = len(stages) <= limit
        return dataclasses.replace(verdict, feasible=bool(fits and verdict.feasible),
                                   detail=verdict.detail if fits else f"{len(stages)} call(s) > {limit} left")
    monkeypatch.setattr(S.Session, "_admit_plan", admit)

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]' if call.task == "t1" else \
            'Fixed add.\nCHANGED: ["app.py"]'

    def revision(call, replay):
        if call.task == "t2":
            assert "Scenarios of your task that bind no case, or bind invalidly" in call.prompt
            H.write(call, {"tests/test_app.py": BASE_TESTS + "\n\ndef test_zero():\n    assert add(0, -1) == -1\n"})
            return f'Added the zero case.\nPROVES: P1 - {CASE}\nCHANGED: ["tests/test_app.py"]'
        return "Nothing to change.\nCHANGED: []"

    def review(call, replay):
        return (CODE if call.task == "t1" else f"{SCEN}\n{UNBOUND}\n{CODE2}") + MET

    def recheck(call, replay):
        if call.task == "t1" and "in RESOLVES" not in call.prompt:
            return recheck_reply(call.prompt, default="UNRESOLVED still open")
        return recheck_reply(call.prompt, default="RESOLVED - shown on this source")

    plans = iter([SIMPLE1, RESOLVING])

    def orchestrator(call, replay):
        if "--- SCENARIO CORRECTION" in call.prompt:
            return "SCENARIO P1: state: nothing is stored | because: add stores nothing"
        return next(plans, "DONE")
    seen = Seen()
    script = _script(seen, orchestrator=orchestrator, lead=lead, collaborator=review, revision=revision,
                     recheck=recheck, auditor=_answering())
    limits = RunLimits(max_calls=60, max_reported_tokens=10_000_000, wall_seconds=900, max_concurrent_workers=2)
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=3, record_complete=False,
                   limits=limits)
    return replay, stages_seen


def test_the_scenario_binding_editor_and_two_reviewer_bundle_is_four_calls_and_runs_whole(tmp_path, monkeypatch):
    replay, stages = _bundle_run(tmp_path, monkeypatch, "four")
    first = stages[0]
    assert first[:2] == ["scenario correction", "fix revision"] and len(first) == 4, first
    assert sum(1 for s in first if "recheck by" in s) == 2 and any(s.startswith("resolution recheck") for s in first)
    record = H.result_json(replay)
    assert not any(r["what"] == "fix round" for r in record["finish_refusals"]), "admitted whole at four calls"
    assert [c for c in replay.of("proof-binding") if c.task == "t2"] == [], "the declaration rode in the revision"
    route = next(r for r in record["evidence_routes"] if r.get("folded"))
    assert route["task"] == "t2" and route["declared"] == [f"PROVES: P1 - {CASE}"]
    rechecks = [c for c in replay.of("recheck") if c.task == "t2"]
    assert len(rechecks) >= 2, "its own reviewer and the RESOLVES owner both rechecked"


def test_without_an_editor_the_binding_keeps_its_own_declaration_call(tmp_path, monkeypatch):
    seen = Seen()
    script = _script(seen, collaborator=lambda c, r: UNBOUND + MET,
                     recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED - shown"), auditor=_answering(),
                     lead=lambda c, r: (H.write(c, {"app.py": FIXED}) or 'Implemented add.\nCHANGED: ["app.py"]'))
    script.overrides["proof-binding"] = lambda c, r: f"PROVES: P1 - {CASE}"
    replay = H.run(_at(tmp_path, "noeditor"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert len(replay.of("proof-binding")) == 1 and not any(r.get("folded")
                                                            for r in H.result_json(replay)["evidence_routes"])


# -- D ------------------------------------------------------------------------------------------

#: A later task's own, different scenario: an identical redeclaration
#: would supersede t1's row, which is not the case this route is for.
LATER_PROOF = _proofs("R1", expect="the sum is returned again")[0]
LATER = ("KIND: backend simple\nSCOPE: " + json.dumps(_scope(proofs=[LATER_PROOF])) + "\nTEXT\nCOVERS: R1")
QUALIFIED = ("BLOCKING: SCENARIO t1:P1 - t1's prior state 'two tickets exist' contradicts add, which stores "
             "nothing | proposal: state: nothing is stored | basis: R1 | impact: contract")
OWN_P1 = QUALIFIED.replace("SCENARIO t1:P1", "SCENARIO P1").replace("t1's prior", "the prior")


def _prior_run(tmp_path, monkeypatch, name, *, claim=QUALIFIED, plans=None, limit=None, max_tasks=2,
               reply="SCENARIO t1:P1: state: nothing is stored | because: add stores nothing"):
    """t1 closes clean with its P1 as declared; a later task's reviewer
    names t1's scenario. ``limit``: ordinary calls left at the fix cycle."""
    if limit is not None:
        real = S.Session._admit_plan

        def admit(self, owner, stages, **kw):
            verdict = real(self, owner, stages, **kw)
            if kw.get("scope") != "fix-cycle":
                return verdict
            return dataclasses.replace(verdict, feasible=len(stages) <= limit and verdict.feasible,
                                       detail=f"{len(stages)} call(s) > {limit} left")
        monkeypatch.setattr(S.Session, "_admit_plan", admit)
    plans = iter(plans or [SIMPLE1, LATER.replace("TEXT", "Check add in app.py once more.")])
    asked = []

    def orchestrator(call, replay):
        if "--- SCENARIO CORRECTION" in call.prompt:
            asked.append(call.prompt)
            return reply
        return next(plans, "DONE")

    def review(call, replay):
        return ("NO FINDINGS" if call.task == "t1" else claim) + MET

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
    auditor = judging_scenarios(lambda c, r: f"R1: MET - {CASE}", PROOF_R1, dict(PROOF_R1, state="nothing is stored"),
                                LATER_PROOF, dict(LATER_PROOF, state="nothing is stored"), basis=SHOWN)
    seen = Seen()
    script = _script(seen, orchestrator=orchestrator, lead=lead, collaborator=review, auditor=auditor,
                     recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED - the scenario now reads right"))
    limits = (RunLimits(max_calls=60, max_reported_tokens=10_000_000, wall_seconds=900, max_concurrent_workers=2)
              if limit is not None else None)
    replay = H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=max_tasks, record_complete=False,
                   limits=limits)
    return replay, asked, seen


def _rows_of(replay, task):
    return [o for o in H.result_json(replay)["evidence_obligations"] if o["task"] == task and o["kind"] == "acceptance"]


def test_a_later_reviewer_corrects_an_earlier_tasks_scenario_on_its_own_rows(tmp_path, monkeypatch):
    replay, asked, seen = _prior_run(tmp_path, monkeypatch, "prior")
    record = H.result_json(replay)
    (prompt,) = asked
    assert "Scenario t1:P1 of the earlier task t1" in prompt and "'SCENARIO t<k>:P<n>: ...'" in prompt
    assert "SCENARIO t<k>:P<n> - <the contradiction>" in seen.prompts["collaborator"][-1], "the route is shown"
    (t1_row,) = _rows_of(replay, "t1")
    (t2_row,) = _rows_of(replay, "t2")
    assert dict(t1_row["scenario"])["state"] == "nothing is stored"
    assert dict(t2_row["scenario"])["state"] == "two tickets exist", "t1:P1 never touches t2's own P1"
    last = t1_row["scenario_lineage"][-1]
    assert last["raised_in"] == "t2" and last["before"] == "two tickets exist" and last["owner_lead"]
    assert t1_row["requirements"] == t2_row["requirements"] and t1_row["location"] == "tests/test_app.py"
    (fix,) = [c for c in record["scenario_corrections"] if c["proof_id"] == "t1:P1"]
    assert fix["status"] == "corrected" and fix["task"] == "t2"
    t1 = next(t for t in record["workflow"]["tasks"] if t["task_id"] == "t1")
    assert t1["contract"]["scope"]["proofs"][0]["state"] == "two tickets exist", "the frozen contract is untouched"
    assert replay.result.completed, replay.result.error


def test_an_unqualified_id_is_always_the_reviewing_tasks_own(tmp_path, monkeypatch):
    replay, asked, _ = _prior_run(tmp_path, monkeypatch, "own", claim=OWN_P1,
                                  reply="SCENARIO P1: state: nothing is stored | because: add stores nothing")
    (t1_row,) = _rows_of(replay, "t1")
    (t2_row,) = _rows_of(replay, "t2")
    assert dict(t2_row["scenario"])["state"] == "nothing is stored"
    assert dict(t1_row["scenario"])["state"] == "two tickets exist" and not t1_row.get("scenario_lineage")


@pytest.mark.parametrize("claim, why", [
    (QUALIFIED.replace("t1:P1", "t9:P1"), "names task t9, which is not an earlier closed task of this run"),
    (QUALIFIED.replace("t1:P1", "t1:P7"), "t1 has no standing scenario P7 on the record"),
])
def test_a_qualified_id_the_record_does_not_hold_stays_held(tmp_path, monkeypatch, claim, why):
    replay, asked, _ = _prior_run(tmp_path, monkeypatch, "unknown", claim=claim)
    assert asked == [], "no planner correction call"
    findings = [f for f in H.result_json(replay)["review_findings"] if f["task"] == "t2"]
    assert any(f.get("route") == "unroutable" and why in (f.get("route_note") or "") for f in findings), findings
    (t1_row,) = _rows_of(replay, "t1")
    assert not t1_row.get("scenario_lineage")


def test_an_unreachable_owner_keeps_the_earlier_scenario_held(tmp_path, monkeypatch):
    original = S.Session._owner_lead
    monkeypatch.setattr(S.Session, "_owner_lead", lambda self, tid: "claude:retired" if tid == "t1"
                        else original(self, tid))
    replay, asked, _ = _prior_run(tmp_path, monkeypatch, "unreachable")
    assert asked == []
    findings = [f for f in H.result_json(replay)["review_findings"] if f["task"] == "t2"]
    assert any("is not reachable" in (f.get("route_note") or "") for f in findings)
    (t1_row,) = _rows_of(replay, "t1")
    assert dict(t1_row["scenario"])["state"] == "two tickets exist" and not t1_row.get("scenario_lineage"), \
        "held as declared: nothing is revised without a reachable owner"


def test_an_earlier_scenario_is_corrected_once_for_the_run(tmp_path, monkeypatch):
    third = LATER.replace("TEXT", "Review add in app.py a third time.")
    second = LATER.replace("TEXT", "Review add in app.py once more.")
    replay, asked, _ = _prior_run(tmp_path, monkeypatch, "once", plans=[SIMPLE1, second, third], max_tasks=3)
    assert len(asked) == 1, "t3's reviewer naming t1:P1 again is not routed"
    findings = [f for f in H.result_json(replay)["review_findings"] if f["task"] == "t3"]
    assert any("already corrected once" in (f.get("route_note") or "") for f in findings)


def test_an_unaffordable_bundle_corrects_nothing(tmp_path, monkeypatch):
    replay, asked, _ = _prior_run(tmp_path, monkeypatch, "budget", limit=1)
    assert asked == [] and any(r["what"] == "fix round" for r in H.result_json(replay)["finish_refusals"])
    (t1_row,) = _rows_of(replay, "t1")
    assert dict(t1_row["scenario"])["state"] == "two tickets exist" and not t1_row.get("scenario_lineage")
