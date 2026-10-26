"""Workflow repair (Davis, 2026-10-24), after the ticket run stopped on
open findings: what a reviewer is shown before it judges, where a defect
in the plan's scenario text goes, how an evidence gap settles, and what
the record says about a requirement no task ever took on.

Real Session and controller with scripted providers, the project's own
pytest gate; every claim here is a record the harness wrote or a prompt
a seat was handed. Nothing infers MET from a green run, nothing closes a
finding but the reviewer who raised it, and no seat gets a call or a
grant it did not have before."""
import json
from pathlib import Path

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_proof_binding import (
    CASE,
    NEGATIVE,
    _acceptance,
    _at,
    _lead,
    _plan,
    _proofs,
    _scope,
)

NEG_TESTS = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
             "def test_negative():\n    assert add(-1, -2) == -3\n")
SKIP_TESTS = ("import pytest\nfrom app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
              "@pytest.mark.skip(reason='not yet')\ndef test_negative():\n    assert add(-1, -2) == -3\n")
MET = "\nR1: MET - app.py"
CODE_FINDING = "BLOCKING: no negative case covers subtraction of negatives | basis: R1 | impact: correctness"
SCENARIO_FINDING = ("BLOCKING: SCENARIO P1 - the prior state 'two tickets exist' contradicts add, which stores nothing "
                    "| proposal: state: nothing is stored | basis: R1 | impact: contract")
EVIDENCE_FINDING = "BLOCKING: EVIDENCE P1 - test_add is not shown executed on this source | basis: P1 | impact: contract"
CORRECTION_HEAD = "--- SCENARIO CORRECTION (t1) ---"


def _rows(prompt: str) -> str:
    """The proof-rows block a judging prompt carries."""
    start = prompt.find("## Proof scenarios this task declared")
    assert start >= 0, "no proof rows in the prompt"
    return prompt[start:start + 1600]


def _roles(replay) -> dict:
    out = {}
    for call in replay.calls:
        out[call.role] = out.get(call.role, 0) + 1
    return out


class Seen:
    """Prompts by role, in call order."""

    def __init__(self):
        self.prompts = {}

    def keep(self, role, call):
        self.prompts.setdefault(role, []).append(call.prompt)


def _script(seen, *, orchestrator=None, lead=None, collaborator=None, revision=None, recheck=None, auditor=None):
    def plan(call, replay):
        seen.keep("orchestrator", call)
        if orchestrator is not None:
            return orchestrator(call, replay)
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"

    def review(call, replay):
        seen.keep("collaborator", call)
        return (collaborator or (lambda c, r: "NO FINDINGS" + MET))(call, replay)

    def recheck_(call, replay):
        seen.keep("recheck", call)
        return (recheck or (lambda c, r: "RFT1-1: RESOLVED shown now"))(call, replay)

    def revision_(call, replay):
        seen.keep("revision", call)
        return (revision or (lambda c, r: "Nothing to change.\nCHANGED: []"))(call, replay)
    script = Script(orchestrator=plan, lead=lead or _lead(), collaborator=review,
                    auditor=auditor or (lambda c, r: f"R1: MET - {CASE}"),
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["recheck"] = recheck_
    script.overrides["revision"] = revision_
    return script


# -- 1. the reviewer and the recheck see the harness's own run on the exact source ---------------

@pytest.mark.requirements_ledger
def test_the_reviewer_is_handed_the_harness_run_on_the_exact_source_before_it_judges(tmp_path, monkeypatch):
    seen = Seen()
    replay = H.run(_at(tmp_path, "review"), monkeypatch, _script(seen), files=FILES, max_tasks=1,
                   record_complete=False)
    rows = _rows(seen.prompts["collaborator"][0])
    assert f"binding bound: {CASE}; execution ran (receipt observed-1)" in rows
    record = H.result_json(replay)
    observed = record["review_checks"]
    assert [c["stage"] for c in observed] == ["review"] and observed[0]["passed"]
    assert observed[0]["source"] == record["source_fingerprint"], "the run was on the bytes the reviewer judged"
    row = _acceptance(replay)[0]
    # The task's own gate still ran afterwards (never replaced by the
    # observation); a row already shown on this source keeps its receipt.
    assert row["execution"] == "ran" and row["receipts"] == ["observed-1"]
    assert H.gate_results(replay) == ["PASSED"] and len(record["checks"]) == 1
    assert "revision" not in _roles(replay) and "recheck" not in _roles(replay)
    assert set(_roles(replay)) <= {"orchestrator", "lead", "collaborator", "auditor", "requirements-review"}
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_after_a_revision_the_recheck_reads_a_fresh_receipt_on_the_revised_source_and_its_new_case(tmp_path, monkeypatch):
    seen = Seen()
    sources = {}

    def revision(call, replay):
        H.write(call, {"tests/test_app.py": NEG_TESTS})
        return (f"Added the negative case.\nPROVES: P1 - {CASE}\nPROVES: P1 - {NEGATIVE}\n"
                f"CHANGED: {json.dumps(['tests/test_app.py'])}")

    def recheck(call, replay):
        sources["recheck"] = _rows(call.prompt)
        return "RFT1-1: RESOLVED the negative case is bound and ran"
    replay = H.run(_at(tmp_path, "revised"), monkeypatch,
                   _script(seen, collaborator=lambda c, r: CODE_FINDING + MET, revision=revision, recheck=recheck),
                   files=FILES, max_tasks=1, record_complete=False)
    rows = sources["recheck"]
    assert f"binding bound: {CASE}, {NEGATIVE}; execution ran (receipt observed-2)" in rows
    record = H.result_json(replay)
    assert [c["stage"] for c in record["review_checks"]] == ["review", "recheck"]
    first, second = record["review_checks"]
    assert first["source"] != second["source"] and second["source"] == record["source_fingerprint"]
    row = _acceptance(replay)[0]
    assert row["source_digest"] == record["source_fingerprint"] and row["discharged_cases"] == [CASE, NEGATIVE]
    assert H.gate_results(replay) == ["PASSED"] and replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_source_the_checks_fail_on_discharges_nothing_and_the_reviewer_is_told_why(tmp_path, monkeypatch):
    seen = Seen()

    def lead(call, replay):
        H.write(call, {"app.py": "def add(a, b):\n    return a - b\n"})
        return f"Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: [\"app.py\"]"
    replay = H.run(_at(tmp_path, "failed"), monkeypatch, _script(seen, lead=lead), files=FILES, max_tasks=1,
                   record_complete=False)
    rows = _rows(seen.prompts["collaborator"][0])
    assert f"binding bound: {CASE}; execution missing" in rows and "(receipt" not in rows
    assert ("The harness ran the task's checks on this exact source before review and they did not pass; "
            "a failed run discharges no row") in rows
    record = H.result_json(replay)
    assert [c["passed"] for c in record["review_checks"]] == [False]
    assert _acceptance(replay)[0]["execution"] != "ran" and not replay.result.completed


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("name, lead, expect", [
    ("unbound", _lead(proves=()), "binding unbound: no case bound; execution missing"),
    ("skipped", _lead(proves=(CASE, NEGATIVE), tests=SKIP_TESTS),
     f"binding bound: {CASE}, {NEGATIVE}; execution missing"),
])
def test_an_unbound_or_skipped_case_stays_missing_although_the_run_passed(tmp_path, monkeypatch, name, lead, expect):
    seen = Seen()
    replay = H.run(_at(tmp_path, name), monkeypatch, _script(seen, lead=lead), files=FILES, max_tasks=1,
                   record_complete=False)
    rows = _rows(seen.prompts["collaborator"][0])
    assert expect in rows and "(receipt" not in rows
    record = H.result_json(replay)
    assert [c["passed"] for c in record["review_checks"]] == [True], "the run passed; the row still is not shown"
    assert _acceptance(replay)[0]["execution"] != "ran"


# -- 2. a scenario defect goes to the planner, a code defect to the editor, both back to the reviewer

@pytest.mark.requirements_ledger
def test_mixed_scenario_and_code_findings_split_between_planner_and_editor_and_both_are_rechecked(tmp_path, monkeypatch):
    seen = Seen()
    requirement = "R1: add(a, b) returns the sum"

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            seen.keep("correction", call)
            return "SCENARIO P1: state: nothing is stored"
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"

    def revision(call, replay):
        H.write(call, {"tests/test_app.py": NEG_TESTS})
        return (f"Added the negative case.\nPROVES: P1 - {CASE}\nPROVES: P1 - {NEGATIVE}\n"
                f"CHANGED: {json.dumps(['tests/test_app.py'])}")

    def recheck(call, replay):
        return "RFT1-1: RESOLVED the scenario now starts from nothing stored\nRFT1-2: RESOLVED the negative case ran"
    replay = H.run(_at(tmp_path, "mixed"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, revision=revision, recheck=recheck,
                           collaborator=lambda c, r: SCENARIO_FINDING + "\n" + CODE_FINDING + MET),
                   files=FILES, max_tasks=1, record_complete=False)
    # One planner call, through the seated orchestrator, on the task, with
    # the goal, the verbatim requirement and the reviewer's proposal.
    assert len(seen.prompts["correction"]) == 1
    correction = seen.prompts["correction"][0]
    assert requirement in correction and "proposal: state: nothing is stored" in correction
    assert "not yours or the reviewer's to change" in correction and "Reviewer A" in correction
    calls = [c for c in replay.calls if CORRECTION_HEAD in c.prompt]
    assert [(c.role, c.task) for c in calls] == [("orchestrator", "t1")]
    # The editor saw the code finding only, with the routing stated.
    prompt = seen.prompts["revision"][0]
    assert CODE_FINDING in prompt and "SCENARIO P1" not in prompt
    assert "findings RFT1-1 were routed to the planner or the harness" in prompt
    # The recheck saw the corrected scenario, its lineage and the fresh receipt.
    rows = _rows(seen.prompts["recheck"][0])
    assert "given nothing is stored; expect the sum is returned" in rows
    assert "corrected by the planner on finding RFT1-1: state was 'two tickets exist'" in rows
    assert "execution ran (receipt observed-2)" in rows
    record = H.result_json(replay)
    corrections = record["scenario_corrections"]
    assert len(corrections) == 1 and corrections[0]["status"] == "corrected"
    assert (corrections[0]["field"], corrections[0]["before"], corrections[0]["after"]) == (
        "state", "two tickets exist", "nothing is stored")
    assert corrections[0]["findings"] == ["RFT1-1"] and corrections[0]["reply_artifact"]
    row = _acceptance(replay)[0]
    assert dict(row["scenario"])["state"] == "nothing is stored" and "given nothing is stored" in row["behavior"]
    lineage = row["scenario_lineage"]
    assert len(lineage) == 1 and lineage[0]["before"] == "two tickets exist" and lineage[0]["execution"] == "ran"
    # Re-shown by the receipts on this source; judged afresh (the final
    # audit's judgment is of the corrected row, bound to it).
    assert row["execution"] == "ran" and row["judged_binding"] != lineage[0].get("judged_binding", "")
    # The user's requirement text never moved; the finding ids kept their history.
    assert record["requirements"]["listed"] == {"R1": "add(a, b) returns the sum"}
    states = {f["fid"]: (f["state"], f.get("route")) for f in record["review_findings"]}
    assert states == {"RFT1-1": ("resolved", "scenario: corrected"), "RFT1-2": ("resolved", None)}
    assert all(len(f["history"]) == 1 and f["history"][0]["role"] == "recheck" for f in record["review_findings"])
    admission = next(a for a in record["plan_admissions"] if a["scope"] == "fix-cycle")
    stages = " ".join(admission["stages"])
    assert "scenario correction(reserve)" in stages and "fix revision(editing" in stages
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_scenario_only_round_makes_no_editing_call(tmp_path, monkeypatch):
    seen = Seen()

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            return "SCENARIO P1: state: nothing is stored"
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, "planonly"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: SCENARIO_FINDING + MET,
                           recheck=lambda c, r: "RFT1-1: RESOLVED corrected"),
                   files=FILES, max_tasks=1, record_complete=False)
    assert "revision" not in _roles(replay) and _roles(replay)["recheck"] == 1
    record = H.result_json(replay)
    admission = next(a for a in record["plan_admissions"] if a["scope"] == "fix-cycle")
    stages = " ".join(admission["stages"])
    assert "scenario correction(reserve)" in stages and "fix revision" not in stages
    assert [c["stage"] for c in record["review_checks"]] == ["review"], "no second run of an unchanged source"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("name, reply, status", [
    ("keep", "SCENARIO P1: KEEP - the state is right as declared", "kept by the planner"),
    ("same", "SCENARIO P1: state: two tickets exist", "refused: the corrected text equals the declared text"),
    ("field", "SCENARIO P1: requirements: [\"R1\", \"R2\"]", "no valid line for it in the reply; the scenario stays as declared"),
    ("location", "SCENARIO P1: location: tests/other.py", "no valid line for it in the reply; the scenario stays as declared"),
])
def test_a_correction_that_is_not_one_scenario_field_changes_nothing_and_the_finding_stands(tmp_path, monkeypatch, name, reply, status):
    seen = Seen()

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            return reply
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"
    rechecks = []

    def recheck(call, replay):
        rechecks.append(call.prompt)
        return "RFT1-1: UNRESOLVED the scenario still contradicts itself"
    replay = H.run(_at(tmp_path, name), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: SCENARIO_FINDING + MET,
                           recheck=recheck),
                   files=FILES, max_tasks=1, record_complete=False)
    record = H.result_json(replay)
    assert [c["status"] for c in record["scenario_corrections"]] == [status]
    row = _acceptance(replay)[0]
    assert dict(row["scenario"])["state"] == "two tickets exist" and "scenario_lineage" not in row
    assert record["requirements"]["listed"] == {"R1": "add(a, b) returns the sum"}
    # Kept or unanswered, the finding stays the reviewer's to judge against
    # the declared text, with the planner's answer in front of it. It is a
    # claim about the plan, so no editor round is bought for it (close
    # family, 2026-10-26; before, the ordinary fix round carried it to the
    # editor as code work). It held it open, so the run ends on it: the plan
    # channel cannot make a finding disappear, and nothing becomes met.
    assert "RFT1-1" in rechecks[0] and "the planner's answer was: " in rechecks[0]
    assert "revision" not in _roles(replay), "no editor round for a finding about the plan's text"
    assert not replay.result.completed and "RFT1-1" in str(replay.result.error)
    assert record["review_findings"][0]["state"] == "unresolved"
    assert len([c for c in replay.calls if CORRECTION_HEAD in c.prompt]) == 1, "one correction per scenario"


@pytest.mark.requirements_ledger
def test_a_planner_that_would_change_the_requirement_raises_the_operator_question_and_changes_nothing(tmp_path, monkeypatch):
    seen = Seen()

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            return "SCENARIO P1: REQUIREMENT - R1 would have to allow a stored ticket count"
        return _plan(_scope(proofs=_proofs("R1"))) if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, "requirement"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: SCENARIO_FINDING + MET),
                   files=FILES, max_tasks=1, record_complete=False)
    assert not replay.result.completed
    error = str(replay.result.error)
    assert "OperatorInputNeeded" in error or "Does it stand as written" in error
    assert "R1 reads: add(a, b) returns the sum" in error
    record = H.result_json(replay)
    assert record["requirements"]["listed"] == {"R1": "add(a, b) returns the sum"}
    assert record["requirements"]["operator_rulings"] == []
    assert [c["status"] for c in record["scenario_corrections"]] == ["requirement question: no operator channel"]
    row = _acceptance(replay)[0]
    assert dict(row["scenario"])["state"] == "two tickets exist" and "scenario_lineage" not in row
    assert "revision" not in _roles(replay) and "recheck" not in _roles(replay)


@pytest.mark.requirements_ledger
def test_a_scenario_claim_naming_no_scenario_of_the_task_is_held_and_never_the_editors(tmp_path, monkeypatch):
    """Close family, 2026-10-26: an unbound scenario id used to go to the
    editor as code work. A typed claim the harness cannot route is held for
    its reviewer, who is told why at the recheck; no correction, no edit."""
    seen = Seen()
    stray = SCENARIO_FINDING.replace("SCENARIO P1", "SCENARIO P7")
    replay = H.run(_at(tmp_path, "stray"), monkeypatch,
                   _script(seen, collaborator=lambda c, r: stray + MET,
                           recheck=lambda c, r: "RFT1-1: UNRESOLVED the scenario is still wrong"),
                   files=FILES, max_tasks=1, record_complete=False)
    assert not any(CORRECTION_HEAD in c.prompt for c in replay.calls)
    assert "revision" not in _roles(replay), "an unbound typed claim is not permission to edit"
    assert "held, not routed and not given to the editor: names scenario P7, which this task does not declare" \
        in seen.prompts["recheck"][0]
    record = H.result_json(replay)
    assert record["scenario_corrections"] == []
    finding = record["review_findings"][0]
    assert finding["route"] == "unroutable" and finding["route_note"] == \
        "names scenario P7, which this task does not declare"
    assert finding["state"] == "unresolved" and not replay.result.completed
    assert any(r["action"].startswith("held unroutable") for r in record["evidence_routes"])


# -- 3. an evidence gap settles through the harness and the original reviewer, serial and parallel

@pytest.mark.requirements_ledger
def test_an_evidence_finding_takes_no_editor_call_and_is_rechecked_against_the_harness_run(tmp_path, monkeypatch):
    seen = Seen()
    replay = H.run(_at(tmp_path, "evidence"), monkeypatch,
                   _script(seen, collaborator=lambda c, r: EVIDENCE_FINDING + MET,
                           recheck=lambda c, r: "RFT1-1: RESOLVED observed-1 shows test_add ran on this source"),
                   files=FILES, max_tasks=1, record_complete=False)
    assert "revision" not in _roles(replay) and _roles(replay)["recheck"] == 1
    rows = _rows(seen.prompts["recheck"][0])
    assert "execution ran (receipt observed-1)" in rows
    record = H.result_json(replay)
    routes = record["evidence_routes"]
    assert len(routes) == 1 and routes[0]["finding"] == "RFT1-1" and routes[0]["proof_id"] == "P1"
    assert routes[0]["action"] == "no new run: a harness run on this exact source is already recorded"
    assert routes[0]["execution_before"] == ["ran"] and routes[0]["execution_after"] == ["ran"]
    assert [c["stage"] for c in record["review_checks"]] == ["review"]
    finding = record["review_findings"][0]
    assert finding["state"] == "resolved" and finding["history"][-1]["author"] == "openai:sol" or \
        finding["state"] == "resolved"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_green_run_never_closes_an_evidence_finding_by_itself(tmp_path, monkeypatch):
    seen = Seen()
    replay = H.run(_at(tmp_path, "green"), monkeypatch,
                   _script(seen, collaborator=lambda c, r: EVIDENCE_FINDING + MET,
                           recheck=lambda c, r: "RFT1-1: UNRESOLVED the receipt names a run I cannot read"),
                   files=FILES, max_tasks=1, record_complete=False)
    record = H.result_json(replay)
    assert record["review_findings"][0]["state"] == "unresolved"
    assert H.gate_results(replay) == ["PASSED"] and not replay.result.completed
    assert "RFT1-1" in str(replay.result.error)


@pytest.mark.requirements_ledger
def test_a_parallel_child_holds_an_evidence_finding_for_the_post_merge_recheck_with_the_merge_gate_receipt(tmp_path, monkeypatch):
    requirements = "REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
    seen = Seen()

    def block(path, covers, proofs):
        scope = dict(permitted_paths=[path], intended_result=f"{path} written", acceptance=[f"{path} meets the task"],
                     max_lines=20, proofs=proofs)
        return f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nWrite {path}.\nCOVERS: {covers}"
    batch = (requirements + "PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n"
             + block("README.md", "R2", _proofs("R2", location="README.md", evidence="reading")))

    def orchestrator(call, replay):
        return batch if len(replay.of("orchestrator")) == 1 else "DONE"

    def lead(call, replay):
        if "Write README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        seen.keep(f"collaborator:{call.task}", call)
        if call.task == "t1":
            return EVIDENCE_FINDING + "\nR1: MET - app.py"
        return "NO FINDINGS\nR2: MET - README.md"

    def recheck(call, replay):
        seen.keep("recheck", call)
        return "RFT1-1: RESOLVED the merge gate's receipt shows test_add ran on the merged source"

    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        return (f"R1: MET - {CASE}\nR2: MET - README.md"
                + "".join(f"\n{o}: SUFFICIENT README.md names add(a, b) and its result" for o in docs))
    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["recheck"] = recheck
    script.overrides["revision"] = lambda c, r: pytest.fail("no editor call for an evidence gap in a child")
    replay = H.run(_at(tmp_path, "parallel"), monkeypatch, script, files=FILES, max_tasks=4, record_complete=False)
    # In the child: the rows said why nothing was shown, and no run happened.
    child = _rows(seen.prompts["collaborator:t1"][0])
    assert "Execution in a parallel child: the harness runs no checks in a child's copy" in child
    assert f"binding bound: {CASE}; execution missing" in child
    assert "revision" not in _roles(replay)
    # After the merge: one recheck by the same reviewer, handed the row
    # discharged by the merge gate's own receipt on the merged source.
    assert len(seen.prompts["recheck"]) == 1
    merged = _rows(seen.prompts["recheck"][0])
    assert f"binding bound: {CASE}; execution ran (receipt check-1)" in merged
    record = H.result_json(replay)
    routes = record["evidence_routes"]
    assert len(routes) == 1 and routes[0]["action"].startswith("held for the post-merge recheck")
    finding = next(f for f in record["review_findings"] if f["fid"] == "RFT1-1")
    assert finding["state"] == "resolved" and finding["history"][-1]["role"] == "recheck"
    batch_record = record["workflow"] if "parallel_batches" not in record else record["parallel_batches"][-1]
    assert batch_record is not None
    row = next(o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance")
    assert row["execution"] == "ran" and row["receipts"] == ["check-1"] and row["source_digest"] == record["source_fingerprint"]
    assert replay.result.completed, replay.result.error


# -- 4. a requirement no task took on stays visible to the planner and at the stop ------------------

@pytest.mark.requirements_ledger
def test_a_listed_requirement_no_task_took_on_is_named_at_a_findings_open_stop(tmp_path, monkeypatch):
    seen = Seen()
    requirements = "R1: add(a, b) returns the sum\nR2: README documents add"

    def orchestrator(call, replay):
        return _plan(_scope(proofs=_proofs("R1")), "R1", requirements) if len(replay.of("orchestrator")) == 1 else "DONE"
    replay = H.run(_at(tmp_path, "stop"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, collaborator=lambda c, r: CODE_FINDING + MET,
                           recheck=lambda c, r: "RFT1-1: UNRESOLVED still no negative case"),
                   files=FILES, max_tasks=1, record_complete=False)
    # The ticket run's shape: the last slot closes on an open finding and
    # the run stops there, with R2 listed, reviewed and never scheduled.
    # The last slot's held review finding no longer stops the run before the
    # finish (close family, 2026-10-26): the cap's completion decision runs,
    # the goal is confirmed, and the requirements check names R2 as never
    # covered; the finding stays open on the record.
    assert not replay.result.completed and str(replay.result.error).startswith("RequirementsUnmet")
    assert "R2" in str(replay.result.error)
    assert any("RFT1-1" in text for text in H.result_json(replay)["open_findings"])
    record = H.result_json(replay)
    assert record["requirements"]["listed"]["R2"] == "README documents add"
    assert not str(record["requirements"]["status"].get("R2", "")).startswith(("covered", "met"))
    report = Path(replay.result.run_dir, "report.md").read_text()
    assert "Requirements no finished task covers when the run stopped: R2: README documents add" in report
    assert "R1" not in report.split("Requirements no finished task covers when the run stopped: ")[1].split("\n")[0]
    assert [c.task for c in replay.of("lead")] == ["t1"], "no task for R2 was ever scheduled"


@pytest.mark.requirements_ledger
def test_the_planner_is_shown_the_requirement_nobody_took_on_and_done_is_refused_until_a_task_covers_it(tmp_path, monkeypatch):
    seen = Seen()
    requirements = "R1: add(a, b) returns the sum\nR2: README documents add"
    readme = dict(permitted_paths=["README.md"], intended_result="README documents add", acceptance=["README names add"],
                  max_lines=10, proofs=_proofs("R2", location="README.md", evidence="reading"))

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return _plan(_scope(proofs=_proofs("R1")), "R1", requirements)
        if n == 2:
            return "DONE"
        if n == 3:
            return _plan(readme, "R2", requirements).replace("Implement add in app.py.", "Document add in README.md.")
        return "DONE"

    def lead(call, replay):
        if "Document add in README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        return _lead()(call, replay)

    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        return (f"R1: MET - {CASE}\nR2: MET - README.md"
                + "".join(f"\n{o}: SUFFICIENT README.md names add(a, b) and its result" for o in docs))
    replay = H.run(_at(tmp_path, "covered"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, lead=lead, auditor=auditor,
                           collaborator=lambda c, r: "NO FINDINGS\n" + ("R2: MET - README.md" if "README" in c.prompt
                                                                     else "R1: MET - app.py")),
                   files=FILES, max_tasks=3, record_complete=False)
    prompts = seen.prompts["orchestrator"]
    assert "- R2: not covered; taken on by no task yet" in prompts[1]
    assert "not covered by any finished task: R2" in prompts[2] and "DONE SENT BACK" in prompts[2]
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    record = H.result_json(replay)
    assert str(record["requirements"]["status"]["R2"]).startswith("met")
    report = Path(replay.result.run_dir, "report.md").read_text()
    assert "Requirements no finished task covers" not in report
    assert replay.result.completed, replay.result.error


# -- 5. the receipt is the source's, not the task's (root's two-task control on fff4e99) -----------

@pytest.mark.requirements_ledger
def test_a_later_task_on_unchanged_source_is_shown_the_run_that_already_passed_on_it(tmp_path, monkeypatch):
    """Two tasks, the second leaving the bytes as the first left them: no
    second run (the harness already ran this source under this gate), and
    the second task's reviewer is handed that run as its receipt."""
    seen = Seen()
    requirements = "R1: add(1, 2) returns 3\nR2: add(2, 1) returns 3"
    first = _plan(_scope(proofs=_proofs("R1")), covers="R1", requirements=requirements)
    second = _plan(_scope(proofs=_proofs("R2")), covers="R2", requirements=requirements)

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return first if n == 1 else second if n == 2 else "DONE"

    def lead(call, replay):
        if call.task == "t1":
            return _lead()(call, replay)
        return f"Nothing to change: add already meets R2.\nPROVES: P1 - {CASE}\nCHANGED: []"
    prompts = []

    def collaborator(call, replay):
        prompts.append((call.task, _rows(call.prompt)))
        return "NO FINDINGS\nR1: MET - app.py\nR2: MET - app.py"
    replay = H.run(_at(tmp_path, "twice"), monkeypatch,
                   _script(seen, orchestrator=orchestrator, lead=lead, collaborator=collaborator,
                           auditor=lambda c, r: f"R1: MET - {CASE}\nR2: MET - {CASE}"),
                   files=FILES, max_tasks=2, record_complete=False)
    assert [task for task, _ in prompts] == ["t1", "t2"]
    assert "execution ran (receipt observed-1)" in prompts[0][1]
    assert "execution ran (receipt check-1)" in prompts[1][1], prompts[1][1]
    record = H.result_json(replay)
    assert [c["stage"] for c in record["review_checks"]] == ["review"], "no second run of unchanged bytes"
    rows = {o["task"]: o for o in record["evidence_obligations"] if o["kind"] == "acceptance"}
    assert rows["t2"]["execution"] == "ran" and rows["t2"]["receipts"] == ["check-1"]
    assert rows["t2"]["source_digest"] == rows["t1"]["source_digest"] == record["source_fingerprint"]
    assert replay.result.completed, replay.result.error


def test_a_receipt_is_refused_on_any_identity_mismatch_whatever_its_age(tmp_path):
    """The identity, not the index: an older passing run on exactly this
    source, gate, environment and revisions is the receipt; a failed run, a
    run on other bytes, under another gate, in another environment, or
    binding other revisions discharges nothing however recent."""
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    project = tmp_path / "p"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n")
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig(project=project))
    session.memory.ledger.requirements.update({"R1": "one", "R2": "two"})
    session._requirement_texts = {}
    current = session._source_fingerprint()
    env = session._environment_id()
    revisions = session._requirement_revisions()
    base = dict(passed=True, source=current, gate="G", environment=env, requirements=dict(revisions), receipts=[])
    session.checks = [
        dict(base),                                                        # check-1: the old passing run
        dict(base, passed=False),                                          # check-2: failed on this source
        dict(base, source="other"),                                        # check-3: other bytes
        dict(base, gate="H"),                                              # check-4: other gate
        dict(base, environment="elsewhere"),                               # check-5: other environment
        dict(base, requirements={"R1": revisions["R1"] + 1, "R2": revisions["R2"]}),  # check-6: other revision
    ]
    session._check_passed_cases = lambda check: {"tests/test_app.py::test_add"}
    row = dict(oid="O1", kind="acceptance", task="t9", gate="G", cases=["tests/test_app.py::test_add"],
               requirements=[["R1", revisions["R1"]]], entry=dict(after_check=6, after_observed=0), modality="test")
    receipt = session._obligation_receipt(row)
    assert receipt is not None and receipt["id"] == "check-1", receipt
    # Every newer record was refused for its own reason; drop the old one and nothing discharges.
    session.checks = session.checks[1:]
    assert session._obligation_receipt(row) is None
    # A bound case the run did not show: refused even on the right identity.
    session.checks = [dict(base)]
    session._check_passed_cases = lambda check: {"tests/test_app.py::test_other"}
    assert session._obligation_receipt(row) is None
    # The same rules for a review observation on this source.
    session.checks = []
    session.review_checks = [dict(base, stage="review", task="t1")]
    session._check_passed_cases = lambda check: {"tests/test_app.py::test_add"}
    assert session._obligation_receipt(row)["id"] == "observed-1"
    session.review_checks = [dict(base, stage="review", task="t1", passed=False)]
    assert session._obligation_receipt(row) is None


# -- 6. a parallel child's scenario correction reads the original requirement text (root control) --

@pytest.mark.requirements_ledger
def test_a_parallel_child_scenario_correction_carries_the_verbatim_requirement(tmp_path, monkeypatch):
    requirements = "REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"
    seen = Seen()

    def block(path, covers, proofs):
        scope = dict(permitted_paths=[path], intended_result=f"{path} written", acceptance=[f"{path} meets the task"],
                     max_lines=20, proofs=proofs)
        return f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nWrite {path}.\nCOVERS: {covers}"
    batch = (requirements + "PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n"
             + block("README.md", "R2", _proofs("R2", location="README.md", evidence="reading")))

    def orchestrator(call, replay):
        if CORRECTION_HEAD in call.prompt:
            seen.keep("correction", call)
            return "SCENARIO P1: state: nothing is stored"
        return batch if len(replay.of("orchestrator")) == 1 else "DONE"

    def lead(call, replay):
        if "Write README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if call.task == "t1":
            return SCENARIO_FINDING + "\nR1: MET - app.py"
        return "NO FINDINGS\nR2: MET - README.md"

    def recheck(call, replay):
        seen.keep("recheck", call)
        return "RFT1-1: RESOLVED the scenario now starts from nothing stored"

    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        return (f"R1: MET - {CASE}\nR2: MET - README.md"
                + "".join(f"\n{o}: SUFFICIENT README.md names add(a, b) and its result" for o in docs))
    script = Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator, auditor=auditor,
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["recheck"] = recheck
    script.overrides["revision"] = lambda c, r: pytest.fail("a scenario-only round makes no editing call")
    replay = H.run(_at(tmp_path, "parallel-scenario"), monkeypatch, script, files=FILES, max_tasks=4,
                   record_complete=False)
    assert len(seen.prompts["correction"]) == 1
    correction = seen.prompts["correction"][0]
    assert "R1: add(a, b) returns the sum" in correction and "Reviewer A" in correction
    calls = [c for c in replay.calls if CORRECTION_HEAD in c.prompt]
    assert [(c.role, c.task) for c in calls] == [("orchestrator", "t1")]
    # Re-checked in the child by the reviewer who raised it, on the corrected row.
    assert len(seen.prompts["recheck"]) == 1 and "given nothing is stored" in _rows(seen.prompts["recheck"][0])
    record = H.result_json(replay)
    corrections = record["scenario_corrections"]
    assert [c["status"] for c in corrections] == ["corrected"] and corrections[0]["task"] == "t1"
    row = next(o for o in record["evidence_obligations"] if o["task"] == "t1" and o["kind"] == "acceptance")
    assert dict(row["scenario"])["state"] == "nothing is stored" and len(row["scenario_lineage"]) == 1
    assert row["execution"] == "ran" and row["receipts"] == ["check-1"], "merged, then shown by the merge gate"
    assert record["requirements"]["listed"]["R1"] == "add(a, b) returns the sum"
    assert next(f for f in record["review_findings"] if f["fid"] == "RFT1-1")["state"] == "resolved"
    assert replay.result.completed, replay.result.error
