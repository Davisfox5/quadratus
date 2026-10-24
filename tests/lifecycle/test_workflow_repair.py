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
    # Kept or unanswered, the finding is the reviewer's to judge; it held it
    # open, so the ordinary editor round followed and the run ends on it:
    # the plan channel cannot make a finding disappear.
    assert "RFT1-1" in rechecks[0] and "revision" in _roles(replay)
    assert "RFT1-1: UNRESOLVED" in seen.prompts["revision"][0], "the ordinary fix round carried it to the editor"
    assert not replay.result.completed and "RFT1-1" in str(replay.result.error)
    assert len([c for c in replay.calls if CORRECTION_HEAD in c.prompt]) == 1, "one correction per task"


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
def test_a_scenario_claim_naming_no_scenario_of_the_task_is_an_ordinary_finding(tmp_path, monkeypatch):
    seen = Seen()
    stray = SCENARIO_FINDING.replace("SCENARIO P1", "SCENARIO P7")
    replay = H.run(_at(tmp_path, "stray"), monkeypatch,
                   _script(seen, collaborator=lambda c, r: stray + MET),
                   files=FILES, max_tasks=1, record_complete=False)
    assert not any(CORRECTION_HEAD in c.prompt for c in replay.calls)
    assert "SCENARIO P7" in seen.prompts["revision"][0]
    record = H.result_json(replay)
    assert record["scenario_corrections"] == []
    assert record["review_findings"][0]["route"] == "editor" and record["review_findings"][0]["route_note"] == \
        "not a scenario of this task"


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
    assert finding["state"] == "resolved" and finding["history"][-1]["author"] == "openai:gpt-5.6-sol" or \
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
    assert not replay.result.completed and str(replay.result.error).startswith("FindingsOpen")
    assert "RFT1-1" in str(replay.result.error)
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
