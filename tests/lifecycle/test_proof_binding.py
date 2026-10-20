"""Proof scenarios (proof/1), their binding to executed cases, and the
immutable trim evidence with its independent preservation disposition.

Real Session and controller with scripted replies; every claim here is a
record the harness wrote, never a model's word. The mapping is validated
for shape and correspondence only: whether the scenarios are enough is the
independent review's judgment, and nothing here infers MET from a bound or
executed case."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus import checkpoint, delivery
from quadratus.scope import TaskScope
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FILES_OK, FIXED, Script

CASE = "tests/test_app.py::test_add"
NEGATIVE = "tests/test_app.py::test_negative"


def _scope(covers=("R1",), proofs=None, max_lines=20):
    scope = dict(permitted_paths=["app.py", "tests/test_app.py"], intended_result="add works",
                 acceptance=["add(1, 2) == 3"], max_lines=max_lines)
    if proofs is not None:
        scope["proofs"] = proofs
    return scope


def _proofs(*rids, state="two tickets exist", expect="the sum is returned", location="tests/test_app.py",
            evidence=None):
    return [dict(id=f"P{i}", requirements=[rid], operation=f"add is called for {rid}", state=state,
                 expect=expect, location=location, **({"evidence": evidence} if evidence else {}))
            for i, rid in enumerate(rids, 1)]


def _plan(scope, covers="R1", requirements="R1: add(a, b) returns the sum"):
    return (f"REQUIREMENTS:\n{requirements}\nKIND: backend simple\nSCOPE: {json.dumps(scope)}\n"
            f"Implement add in app.py.\nCOVERS: {covers}")


def _lead(proves=(CASE,), tests=None, lines=None):
    """``lines``: verbatim PROVES lines (A4 cases) in place of the built ones."""
    def lead(call, replay):
        files = {"app.py": FIXED}
        if tests is not None:
            files["tests/test_app.py"] = tests
        H.write(call, files)
        built = "".join(f"\nPROVES: P1 - {case}" for case in proves) if lines is None else "".join(f"\n{line}" for line in lines)
        return f'Implemented add.{built}\nCHANGED: {json.dumps(sorted(files))}'
    return lead


def _script(plan, lead, **kw):
    return Script(orchestrator=lambda c, r: plan if len(r.of("orchestrator")) == 1 else "DONE", lead=lead,
                  collaborator=kw.pop("collaborator", lambda c, r: "NO FINDINGS\nR1: MET - app.py"),
                  auditor=kw.pop("auditor", lambda c, r: "R1: MET - tests/test_app.py::test_add"),
                  **{"requirements-review": lambda c, r: "COMPLETE"}, **kw)


def _at(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def _acceptance(replay, task="t1"):
    return [o for o in H.result_json(replay)["evidence_obligations"] if o["task"] == task and o["kind"] == "acceptance"]


def _session(tmp_path, project=None):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    return Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                   config=SessionConfig(project=project))


# -- 1. the declared mapping is required and corrected once ---------------------------------------

@pytest.mark.requirements_ledger
def test_a_planned_write_task_without_proof_scenarios_is_sent_back_once_and_then_runs(tmp_path, monkeypatch):
    plans = [_plan(_scope()), _plan(_scope(proofs=_proofs("R1")))]

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plans[n - 1] if n <= 2 else "DONE"
    script = _script(None, _lead())
    script.overrides["orchestrator"] = orchestrator
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    second = replay.of("orchestrator")[1].prompt
    assert "TASK SENT BACK" in second and "declares no proof scenarios" in second and "R1" in second
    assert [c.task for c in replay.of("lead")] == ["t1"], "the corrected declaration ran, the first never did"
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("covers, proofs, text", [
    ("R1, R2", _proofs("R1"), "COVERS names R2 but no proof scenario shows it"),
    ("R1", _proofs("R1", "R2"), "Proof scenarios name requirements the task does not COVER: R2"),
])
def test_a_mapping_that_does_not_match_covers_is_sent_back_naming_the_gap(tmp_path, monkeypatch, covers, proofs, text):
    requirements = "R1: add(a, b) returns the sum\nR2: README documents add"
    plans = [_plan(_scope(), covers, requirements), _plan(_scope(proofs=_proofs("R1")), "R1", requirements)]
    plans[0] = _plan(_scope(proofs=proofs), covers, requirements)

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plans[n - 1] if n <= 2 else "DONE"
    script = _script(None, _lead())
    script.overrides["orchestrator"] = orchestrator
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    assert text in replay.of("orchestrator")[1].prompt
    assert [c.task for c in replay.of("lead")] == ["t1"]


@pytest.mark.requirements_ledger
def test_a_parallel_batch_block_without_proof_scenarios_is_sent_back_the_same_way(tmp_path, monkeypatch):
    """A2: the batch intake runs the mapping check per block; the block
    omitting its scenarios is named and the batch does not dispatch until
    the corrected batch arrives."""
    requirements = "REQUIREMENTS:\nR1: add(a, b) returns the sum\nR2: README documents add\n"

    def block(path, covers, proofs):
        scope = dict(permitted_paths=[path], intended_result=f"{path} written", acceptance=[f"{path} meets the task"],
                     max_lines=20)
        if proofs is not None:
            scope["proofs"] = proofs
        return f"KIND: backend simple\nSCOPE: {json.dumps(scope)}\nWrite {path}.\nCOVERS: {covers}"
    docs = _proofs("R2", location="README.md", evidence="reading")
    bad = requirements + "PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n" + block("README.md", "R2", None)
    good = requirements + "PARALLEL\n" + block("app.py", "R1", _proofs("R1")) + "\n---\n" + block("README.md", "R2", docs)

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return bad if n == 1 else good if n == 2 else "DONE"

    def lead(call, replay):
        if "Write README.md." in call.prompt:
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented add.\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
    def auditor(call, replay):
        docs = H.obligation_ids(call.prompt, modality="reading", requirement="R2")
        return (f"R1: MET - {CASE}\nR2: MET - README.md"
                + "".join(f"\n{o}: SUFFICIENT README.md names add(a, b) and its result, which is all R2 asks" for o in docs))
    script = _script(None, lead, auditor=auditor,
                     collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py\nR2: MET - README.md")
    script.overrides["orchestrator"] = orchestrator
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=4, record_complete=False)
    second = replay.of("orchestrator")[1].prompt
    assert "SENT BACK" in second and "declares no proof scenarios" in second and "R2" in second
    assert sorted(c.task for c in replay.of("lead")) == ["t1", "t2"], "only the corrected batch ran"
    assert replay.result.completed, replay.result.error


# -- 2. binding and discharge by named executed cases --------------------------------------------

@pytest.mark.requirements_ledger
def test_a_bound_case_the_checks_name_discharges_and_an_unrelated_green_check_discharges_nothing(tmp_path,
                                                                                                 monkeypatch):
    """Two jobs on the same green suite. The first lead binds P1 to the case
    the task's check names as passed: executed on the final source, judged
    by the audit. The second delivers no PROVES line: the same green check
    runs and the scenario stays unbound and unexecuted, exactly as a legacy
    acceptance string would (no false coverage from a generic pass)."""
    bound = H.run(_at(tmp_path, "bound"), monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead()),
                  files=FILES, max_tasks=1, record_complete=False)
    assert bound.result.completed, bound.result.error
    row = _acceptance(bound)[0]
    assert (row["proof_id"], row["binding"], row["cases"]) == ("P1", "bound", [CASE])
    assert row["execution"] == "ran" and row["discharged_cases"] == [CASE] and row["established"]
    assert row["source_digest"] == H.result_json(bound)["source_fingerprint"]
    assert "binding bound: tests/test_app.py::test_add" in bound.of("auditor")[-1].prompt

    unbound = H.run(_at(tmp_path, "unbound"), monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), _lead(proves=())),
                    files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(unbound)[0]
    assert (row["binding"], row["cases"], row["execution"], row["established"]) == ("unbound", [], "missing", False)
    assert H.result_json(unbound)["checks"][-1]["passed"], "the green check ran and discharged nothing"
    assert "no case bound" in unbound.of("auditor")[-1].prompt
    assert not unbound.result.completed and "not executed" in unbound.result.error, "an executable claim unshown lowers R1"


@pytest.mark.requirements_ledger
def test_a_legacy_acceptance_string_on_an_explicit_task_stays_unknown_and_is_never_discharged_by_a_green_check(
        tmp_path, monkeypatch):
    """A1 (root's control on d515d51): the mapping stays visibly unknown
    on the record, the green check discharges nothing, and the audit's
    MET on R1 is not read as the row's sufficiency. The row counts only
    on the audit's own explicit line about it; it claims no execution, so
    it is not lowered as unexecuted either."""
    task = ("KIND: backend simple\nSCOPE: " + json.dumps(_scope()) + "\nImplement add in app.py.\nCOVERS: R1")
    plan = "REQUIREMENTS:\nR1: add(a, b) returns the sum\n" + task
    script = _script(None, _lead(proves=()))
    script.overrides["orchestrator"] = lambda c, r: plan
    implied = H.run(_at(tmp_path, "implied"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False,
                    tasks=[plan])
    rows = _acceptance(implied)
    assert rows and all(r["binding"].startswith("unknown (legacy acceptance)") and r["execution"] == "missing"
                        and not r["established"] and not r["executable_claim"] and r["legacy"]
                        and r["modality"] == "legacy" and r["runner"] is None for r in rows), rows
    assert H.result_json(implied)["checks"] and H.result_json(implied)["checks"][-1]["passed"]
    assert rows[0]["sufficiency"] == "unjudged" and not rows[0]["judged_by"], "a MET on R1 judges nothing about O1"
    prompt = implied.of("auditor")[-1].prompt
    assert "unknown (legacy acceptance)" in prompt and "evidence legacy" in prompt
    assert "needs its own '<O-id>: SUFFICIENT" in prompt
    assert not implied.result.completed and "not executed" not in (implied.result.error or "")
    audit = H.result_json(implied)["requirements"]["audits"][-1]
    assert audit["acceptance"]["R1"]["met"] is False
    assert "legacy acceptance (mapping unknown) judged unjudged" in " ".join(audit["acceptance"]["R1"]["reasons"])

    script = _script(None, _lead(proves=()), auditor=lambda c, r: f"R1: MET - {CASE}\nO1: SUFFICIENT app.py returns a + b and test_add passed")
    script.overrides["orchestrator"] = lambda c, r: plan
    judged = H.run(_at(tmp_path, "judged"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False,
                   tasks=[plan])
    row = _acceptance(judged)[0]
    assert row["sufficiency"] == "sufficient" and row["judged_by"] and row["established"]
    assert row["binding"].startswith("unknown (legacy acceptance)") and row["execution"] == "missing"
    assert row["source_digest"] == H.result_json(judged)["source_fingerprint"]
    assert judged.result.completed, judged.result.error


@pytest.mark.requirements_ledger
def test_every_proves_line_is_read_and_a_malformed_one_invalidates_the_binding_it_names(tmp_path, monkeypatch):
    """A4. One reply, one valid line and one malformed line for the same
    scenario: the declaration is invalid for P1, its cases are cleared,
    the valid line does not survive beside it, and the green check
    discharges nothing. A malformed line naming no scenario (or a scenario
    not of this task) invalidates the whole declaration."""
    named = H.run(_at(tmp_path, "named"), monkeypatch,
                  _script(_plan(_scope(proofs=_proofs("R1"))),
                          _lead(lines=[f"PROVES: P1 - {CASE}", "PROVES: P1 - add works"])),
                  files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(named)[0]
    assert row["binding"].startswith("invalid declaration") and row["cases"] == [] and row["execution"] == "missing"
    assert not row["established"] and row["binding_history"][-1]["invalid"]
    problems = H.result_json(named)["proof_binding_problems"]
    assert len(problems) == 1 and problems[0]["problem"].startswith("P1: ") and "add works" in problems[0]["problem"]
    assert H.result_json(named)["checks"][-1]["passed"] and not named.result.completed

    whole = H.run(_at(tmp_path, "whole"), monkeypatch,
                  _script(_plan(_scope(proofs=_proofs("R1"))),
                          _lead(lines=[f"PROVES: P1 - {CASE}", f"PROVES: P7 - {CASE}", "PROVES: see the tests"])),
                  files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(whole)[0]
    assert row["binding"].startswith("invalid declaration") and row["cases"] == []
    problems = [p["problem"] for p in H.result_json(whole)["proof_binding_problems"]]
    assert any(p.startswith("P7: not a scenario") for p in problems) and any("see the tests" in p for p in problems)
    assert not whole.result.completed

    # A well-formed shape pointing outside the test files is a malformed
    # binding for that scenario, not a case.
    non_test = H.run(_at(tmp_path, "non_test"), monkeypatch,
                     _script(_plan(_scope(proofs=_proofs("R1"))), _lead(lines=["PROVES: P1 - app.py::test_add"])),
                     files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(non_test)[0]
    assert row["binding"].startswith("invalid declaration") and "not a file::[Class::]test_case" in row["binding"]


@pytest.mark.requirements_ledger
def test_an_exact_duplicate_proves_line_is_an_invalid_declaration_not_a_collapsed_one(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch,
                   _script(_plan(_scope(proofs=_proofs("R1"))), _lead(lines=[f"PROVES: P1 - {CASE}", f"PROVES: P1 - {CASE}"])),
                   files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(replay)[0]
    assert row["binding"].startswith("invalid declaration") and "duplicate PROVES line" in row["binding"]
    assert row["cases"] == [] and not row["established"] and not replay.result.completed
    problems = H.result_json(replay)["proof_binding_problems"]
    assert len(problems) == 1 and "duplicate" in problems[0]["problem"]


@pytest.mark.requirements_ledger
def test_a_later_valid_declaration_restores_a_binding_and_two_lines_bind_two_cases(tmp_path, monkeypatch):
    """A4: the draft's malformed line invalidates P1; the review blocks; the
    revision's two well-formed lines bind both cases and the check
    discharges both on the revised source."""
    both = BIG_TESTS.split("\n\n\ndef test_case_0")[0] + "\n\n\ndef test_negative():\n    assert add(-1, -2) == -3\n"

    def revision(call, replay):
        H.write(call, {"tests/test_app.py": both})
        return f'Added the negative case.\nPROVES: P1 - {CASE}\nPROVES: P1 - {NEGATIVE}\nCHANGED: ["tests/test_app.py"]'
    script = _script(_plan(_scope(proofs=_proofs("R1"))), _lead(lines=["PROVES: P1 - add works"]),
                     collaborator=lambda c, r: "BLOCKING: no negative case | basis: R1 | impact: correctness\nR1: MET - app.py")
    script.overrides["revision"] = revision
    script.overrides["recheck"] = lambda c, r: "RFT1-1: RESOLVED test_negative added"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert replay.result.completed, replay.result.error
    row = _acceptance(replay)[0]
    assert row["binding"] == "bound" and row["cases"] == [CASE, NEGATIVE] and row["discharged_cases"] == [CASE, NEGATIVE]
    assert row["execution"] == "ran" and row["established"] and not row["reason"]
    assert [h.get("invalid") is not None for h in row["binding_history"]] == [True, False]


@pytest.mark.requirements_ledger
def test_a_binding_to_a_case_the_check_never_ran_stays_undischarged(tmp_path, monkeypatch):
    missing = H.run(_at(tmp_path, "missing"), monkeypatch,
                    _script(_plan(_scope(proofs=_proofs("R1"))), _lead(proves=("tests/test_app.py::test_absent",))),
                    files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(missing)[0]
    assert row["binding"] == "bound" and row["cases"] == ["tests/test_app.py::test_absent"]
    assert row["execution"] == "missing" and not row["established"]
    assert H.result_json(missing)["checks"][-1]["passed"]


# -- 2b. the declared reading modality (A1) -----------------------------------------------------------

DOCS_REQ = "R1: README documents add"


def _docs_plan(location="README.md", evidence="reading", requirement=DOCS_REQ):
    scope = dict(permitted_paths=["README.md"], intended_result="README documents add",
                 acceptance=["README names add"], max_lines=20,
                 proofs=_proofs("R1", location=location, evidence=evidence))
    return _plan(scope, "R1", requirement).replace("Implement add in app.py.", "Document add in README.md.")


def _docs_lead(call, replay):
    H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
    return 'Documented add.\nCHANGED: ["README.md"]'


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("location, requirement, text", [
    ("app.py", DOCS_REQ, "declares reading evidence at app.py, which is not a documentation file"),
    ("README.md", "R1: README documents that add raises TypeError on a non-number",
     "declares reading evidence for an error or refusal requirement"),
])
def test_reading_evidence_is_refused_outside_documentation_and_for_error_requirements(tmp_path, monkeypatch,
                                                                                       location, requirement, text):
    plans = [_docs_plan(location, "reading", requirement), _docs_plan("README.md", "reading", requirement)]
    if "raises" in requirement:
        plans[1] = _docs_plan("README.md", None, requirement)

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return plans[n - 1] if n <= 2 else "DONE"
    script = _script(None, _docs_lead, auditor=lambda c, r: "R1: NOT MET - no case shown")
    script.overrides["orchestrator"] = orchestrator
    replay = H.run(tmp_path, monkeypatch, script, files=FILES_OK, max_tasks=2, record_complete=False)
    assert text in replay.of("orchestrator")[1].prompt
    assert [c.task for c in replay.of("lead")] == ["t1"], "the corrected declaration ran, the refused one never did"


def _docs_auditor(line):
    """A scripted audit that addresses its own explicit line to the reading
    row the prompt lists (the judgment text is this fixture's)."""
    def auditor(call, replay):
        ids = H.obligation_ids(call.prompt, modality="reading")
        return "R1: MET - README.md" + "".join(f"\n{o}: {line}" for o in ids)
    return auditor


@pytest.mark.requirements_ledger
def test_a_reading_claim_counts_only_on_an_explicit_per_obligation_judgment_citing_its_file(tmp_path, monkeypatch):
    """Root's paired control on a1b1840: a reading scenario is never
    discharged by the green check and never lowered as unexecuted, and the
    audit's MET on its requirement, however it cites, judges nothing about
    it. It counts only on the audit's own explicit per-obligation line, on
    the current source, citing the file the scenario names. The same
    declaration for a runtime requirement, with app.py returning 0 and no
    check run, stays unproved on a bare MET."""
    judged = H.run(_at(tmp_path, "judged"), monkeypatch,
                   _script(_docs_plan(), _docs_lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                           auditor=_docs_auditor("SUFFICIENT reading README.md shows the one sentence R1 asks for: "
                                                 "add(a, b) returns the sum")),
                   files=FILES_OK, max_tasks=1, record_complete=False)
    assert judged.result.completed, judged.result.error
    row = _acceptance(judged)[0]
    assert row["modality"] == "reading" and not row["executable_claim"] and row["runner"] is None
    assert row["binding"].startswith("reading claim") and row["execution"] == "missing" and row["cases"] == []
    assert row["sufficiency"] == "sufficient" and row["judged_by"] and row["established"]
    assert row["source_digest"] == H.result_json(judged)["source_fingerprint"]
    assert row["location"] == "README.md" and dict(row["scenario"])["evidence"] == "reading"
    prompt = judged.of("auditor")[-1].prompt
    assert "evidence reading" in prompt and "judged only by your own line" in prompt

    bare = H.run(_at(tmp_path, "bare"), monkeypatch,
                 _script(_docs_plan(), _docs_lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                         auditor=lambda c, r: "R1: MET - README.md"),
                 files=FILES_OK, max_tasks=1, record_complete=False)
    row = _acceptance(bare)[0]
    assert row["sufficiency"] == "unjudged" and not row["established"] and not bare.result.completed
    audit = H.result_json(bare)["requirements"]["audits"][-1]
    assert audit["acceptance"]["R1"]["met"] is False and "reading claim judged unjudged" in " ".join(
        audit["acceptance"]["R1"]["reasons"])

    uncited = H.run(_at(tmp_path, "uncited"), monkeypatch,
                    _script(_docs_plan(), _docs_lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                            auditor=_docs_auditor("SUFFICIENT the documentation covers it")),
                    files=FILES_OK, max_tasks=1, record_complete=False)
    row = _acceptance(uncited)[0]
    assert row["sufficiency"] == "unjudged" and "did not cite README.md" in row.get("sufficiency_note", "")
    assert not uncited.result.completed

    runtime = "R1: add(a, b) returns the sum"
    broken = H.run(_at(tmp_path, "runtime"), monkeypatch,
                   _script(_docs_plan(requirement=runtime), _docs_lead,
                           collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                           auditor=lambda c, r: "R1: MET - README.md"),
                   files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(broken)[0]
    assert row["modality"] == "reading" and row["sufficiency"] == "unjudged" and not row["established"]
    assert not broken.result.completed, "a runtime requirement mapped to README with a bare MET stays unproved"
    audits = H.result_json(broken)["requirements"]["audits"]
    assert not audits or audits[-1]["acceptance"]["R1"]["met"] is False


@pytest.mark.requirements_ledger
def test_a_reading_judgment_is_stale_when_the_scenario_or_the_effective_identity_moves(tmp_path):
    """Root's currency control on a1b1840: the binding a reading judgment
    is about carries the scenario's location and metadata, modality, and
    the effective gate and environment; a change to any of them after the
    judgment makes it stale and moves the packet."""
    session = _session(tmp_path)
    base = dict(oid="O1", task="t1", kind="acceptance", behavior="P1: README read", requirements=[["R1", 1]],
                gate="g", environment="e", modality="reading", location="README.md",
                scenario=[["evidence", "reading"], ["expect", "names add"], ["location", "README.md"],
                          ["operation", "read"], ["proof", "P1"], ["state", "add exists"]],
                binding="reading claim", cases=[], execution="missing", receipts=[], runner=None,
                sufficiency="unjudged", judged_by="", judged_binding="")
    judged = delivery.judge_sufficiency(delivery.EvidenceObligation.from_dict(base), "sufficient", judged_by="auditor",
                                        reading=True)
    assert judged.established and judged.sufficiency_current
    session.evidence_obligations = [dict(base, **judged.to_dict())]
    first = session._packet().digest
    for change in (dict(location="OTHER.md"), dict(modality="legacy"), dict(gate="g2"), dict(environment="e2"),
                   dict(scenario=[["evidence", "reading"], ["expect", "names subtract"], ["location", "README.md"],
                                  ["operation", "read"], ["proof", "P1"], ["state", "add exists"]])):
        moved = delivery.EvidenceObligation.from_dict({**base, **judged.to_dict(), **change})
        assert not moved.sufficiency_current and not moved.established, change
        session.evidence_obligations = [{**base, **moved.to_dict()}]
        assert session._packet().digest != first, change
        accepted = delivery.acceptance({"R1": (True, "README.md")}, session.evidence_obligations, [], {"t1": ["R1"]},
                                       current_requirements={"R1": 1})
        assert not accepted["R1"].met and "judged on other evidence" in " ".join(accepted["R1"].reasons), change


@pytest.mark.requirements_ledger
def test_a_test_scenario_in_a_documentation_file_is_still_an_executable_claim(tmp_path, monkeypatch):
    """A1: location is not modality. A scenario at README.md that declares
    no reading evidence claims a runnable proof and, unbound and
    unexecuted, lowers its requirement whatever the audit wrote."""
    replay = H.run(tmp_path, monkeypatch,
                   _script(_docs_plan(evidence=None), _docs_lead,
                           collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                           auditor=lambda c, r: "R1: MET - README.md"),
                   files=FILES_OK, max_tasks=1, record_complete=False)
    row = _acceptance(replay)[0]
    assert row["modality"] == "test" and row["executable_claim"] and row["binding"] == "unbound"
    assert not replay.result.completed and "not executed" in replay.result.error


@pytest.mark.requirements_ledger
def test_rebinding_on_the_same_source_resets_execution_and_binds_the_new_case_set(tmp_path, monkeypatch):
    """The draft binds P1 to test_add and the check discharges it; the
    review blocks; the revision adds test_negative and rebinds P1 to it.
    The old execution and any judgment of it are dropped (root's
    stale-binding control) and the scenario is discharged again only by
    the new case on the revised source."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def revision(call, replay):
        old = Path(call.cwd, "tests/test_app.py").read_text()
        H.write(call, {"tests/test_app.py": old + "\n\ndef test_negative():\n    assert add(-1, -2) == -3\n"})
        return f'Added the negative case.\nPROVES: P1 - {NEGATIVE}\nCHANGED: ["tests/test_app.py"]'
    script = _script(_plan(_scope(proofs=_proofs("R1"))), lead,
                     collaborator=lambda c, r: "BLOCKING: no negative case | basis: R1 | impact: correctness\nR1: MET - app.py")
    script.overrides["revision"] = revision
    script.overrides["recheck"] = lambda c, r: "RFT1-1: RESOLVED test_negative added"
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    assert replay.result.completed, replay.result.error
    row = _acceptance(replay)[0]
    assert [h["cases"] for h in row["binding_history"]] == [[CASE], [NEGATIVE]]
    assert row["cases"] == [NEGATIVE] and row["discharged_cases"] == [NEGATIVE]
    assert row["execution"] == "ran" and row["source_digest"] == H.result_json(replay)["source_fingerprint"]
    assert row["established"]


@pytest.mark.requirements_ledger
def test_a_bound_executed_case_with_an_independent_not_met_stays_unmet(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch,
                   _script(_plan(_scope(proofs=_proofs("R1"))), _lead(),
                           auditor=lambda c, r: "R1: NOT MET - the sum is returned but negative sums are not shown"),
                   files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(replay)[0]
    assert row["execution"] == "ran" and row["sufficiency"] == "insufficient" and not row["established"]
    assert not replay.result.completed


def test_the_packet_moves_with_an_obligation_s_evidence_and_not_with_its_judgment(tmp_path):
    """A3: cases, behaviour, bound revisions, modality and binding are the
    obligation's identity; sufficiency and judged_by are a judgment of it.
    A trim record is part of the packet too, except its disposition."""
    session = _session(tmp_path)
    base = dict(oid="O1", task="t1", kind="acceptance", behavior="add(1, 2) == 3", requirements=[["R1", 1]],
                gate="g", environment="e", modality="test", binding="bound", cases=["tests/test_app.py::test_add"],
                execution="ran", receipts=[], runner="gate", sufficiency="unjudged", judged_by="", judged_binding="")
    session.evidence_obligations = [dict(base)]
    first = session._packet().digest
    session.evidence_obligations = [dict(base, sufficiency="sufficient", judged_by="auditor", judged_binding="x")]
    assert session._packet().digest == first, "a judgment of the evidence is not the evidence"
    for change in (dict(cases=["tests/test_app.py::test_negative"]), dict(behavior="add(2, 2) == 4"),
                   dict(requirements=[["R1", 2]]), dict(modality="reading"), dict(binding="invalid declaration: x"),
                   dict(gate="g2"), dict(environment="e2")):
        session.evidence_obligations = [dict(base, **change)]
        assert session._packet().digest != first, change
    session.evidence_obligations = [dict(base)]
    session.trim_records = [dict(tid="T1", task="t1", site="draft", artifact="a1", artifact_sha256="s", hashes={},
                                 complete=True, changed=True, disposition=None, state="pending")]
    with_trim = session._packet().digest
    assert with_trim != first
    session.trim_records[0]["disposition"] = dict(verdict="PRESERVED", by="auditor", evidence="x")
    session.trim_records[0]["state"] = "preserved by auditor"
    assert session._packet().digest == with_trim, "a trim's disposition is a judgment of the trim record"


# -- 3. trim evidence and the independent preservation disposition -------------------------------

BIG_TESTS = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(2, 2) == 4\n"
             + "".join(f"\n\ndef test_case_{i}():\n    assert add({i}, 1) == {i + 1}\n" for i in range(12)))
TRIMMED_LOSS = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
                + "".join(f"\n\ndef test_case_{i}():\n    assert add({i}, 1) == {i + 1}\n" for i in range(4)))
TRIMMED_KEPT = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(2, 2) == 4\n"
                + "\n\ndef test_cases():\n    for i in range(4):\n        assert add(i, 1) == i + 1\n")


def _trim_job(tmp_path, monkeypatch, trimmed, review, *, auditor=None, max_chars=None, later=None, recheck=None,
              files=FILES):
    """A draft past the 30-line stop (bound 20), trimmed by the lead to
    ``trimmed``; the reviewer answers ``review``. ``later``: what a second
    revision (a fix after the review) writes to the test file."""
    if max_chars is not None:
        monkeypatch.setattr("quadratus.session._TRIM_EVIDENCE_MAX_CHARS", max_chars)
    script = _script(_plan(_scope(proofs=_proofs("R1"))), _lead(tests=BIG_TESTS),
                     collaborator=lambda c, r: review,
                     auditor=auditor or (lambda c, r: "R1: MET - tests/test_app.py::test_add"))

    def revision(call, replay):
        if len(replay.of("revision")) > 1 and later is not None:
            H.write(call, {"tests/test_app.py": later})
            return f'Fixed the finding.\nPROVES: P1 - {CASE}\nCHANGED: ["tests/test_app.py"]'
        H.write(call, {"tests/test_app.py": trimmed})
        return f'Trimmed the tests.\nPROVES: P1 - {CASE}\nCHANGED: ["tests/test_app.py"]'
    script.overrides["revision"] = revision
    if recheck is not None:
        script.overrides["recheck"] = recheck
    return H.run(tmp_path, monkeypatch, script, files=files, max_tasks=1, record_complete=False)


def _trim(replay):
    return H.result_json(replay)["trim_records"]


@pytest.mark.requirements_ledger
def test_a_trim_that_deletes_an_assertion_inside_an_unchanged_case_is_shown_whole_and_judged_lost(tmp_path,
                                                                                                    monkeypatch):
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_LOSS,
                       "BLOCKING: the add(2, 2) assertion was deleted by the trim | basis: R1 | impact: correctness\n"
                       "R1: MET - app.py\nTRIM: LOST - add(2, 2) assertion deleted\nREVIEW: COMPLETE")
    records = _trim(replay)
    assert len(records) == 1 and records[0]["site"] == "draft" and records[0]["changed"] and records[0]["complete"]
    assert records[0]["tid"] == "T-t1-1" and records[0]["state"] == "preservation owed; lost by claude:opus"
    # The overrun draft never delivered (its PROVES line is not read past
    # the stop), so the snapshot holds the scenario unbound; the trim's own
    # delivery binds it afterwards.
    assert records[0]["proofs"] == [dict(proof="P1", cases=[])]
    assert _acceptance(replay)[0]["cases"] == [CASE]
    pre = json.loads(replay.artifact_texts("pre-trim-tests")[0])
    assert pre["tests/test_app.py"] == BIG_TESTS, "the full pre-trim contents are on file, not a case-name list"
    review = replay.of("collaborator")[-1].prompt
    assert "## Trim evidence" in review and "T-t1-1: task t1, draft trim" in review
    assert "-    assert add(2, 2) == 4" in review and "'TRIM <id>: PRESERVED'" in review
    assert records[0]["disposition"]["verdict"] == "LOST" and "add(2, 2)" in records[0]["disposition"]["detail"]
    assert records[0]["disposition"]["evidence"] and records[0]["disposition"]["by"] == "claude:opus"
    assert not replay.result.completed
    assert "T-t1-1 (t1, draft trim: preservation owed; lost by claude:opus)" in replay.result.error


@pytest.mark.requirements_ledger
def test_an_equivalent_refactor_the_reviewer_judges_preserved_completes_and_names_decide_nothing(tmp_path,
                                                                                                   monkeypatch):
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM T-t1-1: PRESERVED")
    record = _trim(replay)[0]
    assert record["changed"] and record["disposition"]["verdict"] == "PRESERVED"
    assert record["disposition"]["evidence"] == record["shown"]
    assert replay.result.completed, replay.result.error

    other = _trim_job(_at(tmp_path, "other"), monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM T-t1-2: PRESERVED")
    assert _trim(other)[0]["disposition"] is None, "a verdict on another trim id judges nothing here"
    assert not other.result.completed


@pytest.mark.requirements_ledger
def test_a_missing_disposition_is_owed_to_the_final_audit_which_may_judge_it(tmp_path, monkeypatch):
    silent = _trim_job(_at(tmp_path, "silent"), monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py")
    record = _trim(silent)[0]
    assert record["disposition"] is None and "preservation owed" in record["state"]
    assert "## Trim evidence" in silent.of("auditor")[-1].prompt, "the audit is shown the owed trim"
    assert not silent.result.completed and "preserved" in silent.result.error, silent.result.error

    judged = _trim_job(_at(tmp_path, "judged"), monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py",
                       auditor=lambda c, r: "TRIM: PRESERVED\nR1: MET - tests/test_app.py::test_add")
    record = _trim(judged)[0]
    assert record["disposition"]["verdict"] == "PRESERVED" and record["disposition"]["by"]
    assert judged.result.completed, judged.result.error


@pytest.mark.requirements_ledger
def test_truncated_trim_evidence_is_never_preserved_and_two_lines_settle_nothing(tmp_path, monkeypatch):
    short = _trim_job(_at(tmp_path, "short"), monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM: PRESERVED",
                      max_chars=10)
    record = _trim(short)[0]
    assert not record["complete"] and record["changed"] is None and record["artifact"]
    assert "over the evidence bound" in record["state"] and "incomplete" in record["state"]
    assert "not on file" in short.of("collaborator")[-1].prompt
    assert record["disposition"]["verdict"] == "REFUSED" and not short.result.completed
    assert not short.artifact_texts("pre-trim-tests") and short.artifact_texts("pre-trim-hashes")

    double = _trim_job(_at(tmp_path, "double"), monkeypatch, TRIMMED_KEPT,
                       "NO FINDINGS\nR1: MET - app.py\nTRIM: PRESERVED\nTRIM: LOST - unsure")
    assert _trim(double)[0]["disposition"] is None and not double.result.completed


# -- 3b. root's four controls: unknown is owed, the artifact is verified, a stale approval is renewed --

@pytest.mark.requirements_ledger
def test_a_trim_whose_pre_trim_evidence_could_not_be_captured_is_owed_and_never_preserved(tmp_path, monkeypatch):
    from quadratus.session import Session
    real = Session._trim_files
    calls = []

    def flaky(self, policy, before):
        calls.append(1)
        return (None, "the project could not be read") if len(calls) == 1 else real(self, policy, before)
    monkeypatch.setattr(Session, "_trim_files", flaky)
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM T-t1-1: PRESERVED",
                       auditor=lambda c, r: "TRIM T-t1-1: PRESERVED\nR1: MET - tests/test_app.py::test_add")
    record = _trim(replay)[0]
    assert record["artifact"] is None and record["changed"] is None and "pre-trim evidence unavailable" in record["state"]
    assert record["evidence_state"].startswith("pre-trim evidence unavailable")
    assert "cannot be judged PRESERVED" in replay.of("collaborator")[-1].prompt
    assert record["disposition"]["verdict"] == "REFUSED"
    assert "T-t1-1" in replay.of("auditor")[-1].prompt and not replay.result.completed
    assert "no independent judgment bound to the current source" in replay.result.error
    assert "T-t1-1 (t1, draft trim: pre-trim evidence unavailable" in replay.result.error


@pytest.mark.requirements_ledger
def test_a_trim_whose_post_trim_files_could_not_be_read_is_owed_not_unchanged(tmp_path, monkeypatch):
    from quadratus.session import Session
    real = Session._trim_files
    calls = []

    def flaky(self, policy, before):
        calls.append(1)
        return (None, "the project could not be read") if len(calls) == 2 else real(self, policy, before)
    monkeypatch.setattr(Session, "_trim_files", flaky)
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py")
    record = _trim(replay)[0]
    assert record["changed"] is None and "post-trim evidence unavailable" in record["state"]
    assert "## Trim evidence" in replay.of("collaborator")[-1].prompt, "an unknown effect is shown and owed"
    assert not replay.result.completed


def test_pre_trim_evidence_is_read_back_through_its_digest_and_preserved_is_refused_when_it_does_not_hold(tmp_path):
    """The record's artifact is verified on every consumption: a missing,
    unreadable or altered artifact is not evidence, so the trim is owed
    and PRESERVED about it is refused; the honest LOST still records."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "tests").mkdir()
    (project / "tests" / "test_app.py").write_text("def test_add():\n    assert 1 + 2 == 3\n")
    session = _session(tmp_path, project)
    spec = SimpleNamespace(task_id="t1", scope=TaskScope.unbounded())
    session._active_spec = spec
    record = session._trim_snapshot(spec, "draft", None)
    (project / "tests" / "test_app.py").write_text("def test_add():\n    pass\n")
    session._trim_settle(record, None)
    assert record["tid"] == "T-t1-1" and record["complete"] and record["changed"] and record["state"] == "preservation owed"
    assert session._trim_pre_contents(record) == {"tests/test_app.py": "def test_add():\n    assert 1 + 2 == 3\n"}
    assert session._trim_block(spec) and record["shown"] == session._trim_evidence_id(record)
    session._read_trim_disposition(spec, "reviewer", "TRIM T-t1-1: PRESERVED")
    assert record["disposition"]["verdict"] == "PRESERVED" and not session._trim_owed()

    record["disposition"] = None
    good_sha = record["artifact_sha256"]
    record["artifact_sha256"] = hashlib.sha256(b"other").hexdigest()
    assert session._trim_pre_contents(record) is None and session._trim_owed() == [record]
    block = session._trim_block(spec)
    assert "not on file" in block and "cannot be judged PRESERVED" in block
    session._read_trim_disposition(spec, "reviewer", "TRIM T-t1-1: PRESERVED")
    assert record["disposition"]["verdict"] == "REFUSED" and session._trim_owed() == [record]
    session._read_trim_disposition(spec, "auditor", "TRIM T-t1-1: LOST - the assertion is gone")
    assert record["disposition"]["verdict"] == "LOST" and session._trim_owed() == [record]

    record["artifact_sha256"] = good_sha
    record["artifact"] = "no-such-artifact"
    assert session._trim_pre_contents(record) is None


def test_a_trim_shown_once_is_not_judged_on_that_display_after_the_files_become_unreadable(tmp_path, monkeypatch):
    """Root 4a on a1b1840: a render clears what an earlier render showed,
    and a PRESERVED is consumed only against the current readability and
    identity, never against a display authority that has since lapsed."""
    from quadratus.session import Session
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_app.py").write_text("def test_add():\n    assert 1 + 2 == 3\n")
    session = _session(tmp_path, project)
    spec = SimpleNamespace(task_id="t1", scope=TaskScope.unbounded())
    session._active_spec = spec
    record = session._trim_snapshot(spec, "draft", None)
    (project / "tests" / "test_app.py").write_text("def test_add():\n    pass\n")
    session._trim_settle(record, None)
    assert "```diff" in session._trim_block(spec) and record["shown"]
    real = Session._trim_files
    monkeypatch.setattr(Session, "_trim_files", lambda self, policy, before: (None, "the project could not be read"))
    block = session._trim_block(spec)
    assert "could not be read" in block and record["shown"] is None
    session._read_trim_disposition(spec, "auditor", "TRIM T-t1-1: PRESERVED")
    assert record["disposition"]["verdict"] == "REFUSED" and session._trim_owed() == [record]
    monkeypatch.setattr(Session, "_trim_files", real)
    record["disposition"] = None
    assert "```diff" in session._trim_block(spec)
    monkeypatch.setattr(Session, "_trim_files", lambda self, policy, before: (None, "the project could not be read"))
    session._read_trim_disposition(spec, "auditor", "TRIM T-t1-1: PRESERVED")
    assert record["disposition"]["verdict"] == "REFUSED", "readability is re-checked when the verdict is consumed"


def test_a_late_review_of_a_trim_stays_within_the_policy_the_trim_was_taken_under(tmp_path):
    """Root 4b on a1b1840: the final audit (no active task) reads the
    current files under the record's own persisted scope and operator
    limits, never an unbounded fallback; a file outside the task's scope
    is not in the comparison, and a record without its policy is not
    judged."""
    from quadratus.scope import TaskScope as TS
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "private").mkdir()
    (project / "tests" / "test_app.py").write_text("def test_add():\n    assert 1 + 2 == 3\n")
    (project / "private" / "test_secret.py").write_text("SENTINEL = 1\n")
    session = _session(tmp_path, project)
    scope = TS(permitted_paths=["tests/**", "app.py"], intended_result="x", acceptance=["y"], max_lines=10)
    spec = SimpleNamespace(task_id="t1", scope=scope)
    session._active_spec = spec
    record = session._trim_snapshot(spec, "draft", None)
    assert record["policy"] == [scope.to_dict()] and "private/test_secret.py" not in record["hashes"]
    (project / "tests" / "test_app.py").write_text("def test_add():\n    pass\n")
    (project / "private" / "test_secret.py").write_text("SENTINEL = 2\n")
    session._trim_settle(record, None)
    session._active_spec = None
    block = session._trim_block(None)
    assert "-    assert 1 + 2 == 3" in block and "SENTINEL" not in block and "test_secret" not in block
    record["policy"] = None
    block = session._trim_block(None)
    assert "policy the trim was taken under was not recorded" in block
    session._read_trim_disposition(None, "auditor", "TRIM T-t1-1: PRESERVED")
    assert record["disposition"]["verdict"] == "REFUSED"


def test_a_preserved_verdict_lapses_when_a_covered_requirement_or_scenario_changes_without_a_source_edit(tmp_path):
    """Root 4c on a1b1840: preservation is judged against what the covered
    requirements and declared scenarios need, so their current identity is
    part of what the verdict binds; a reworded requirement or a rebound
    scenario after the approval owes the trim again."""
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_app.py").write_text("def test_add():\n    assert 1 + 2 == 3\n")
    session = _session(tmp_path, project)
    session.config.requirements_ledger = True
    session.memory.ledger.requirements["R1"] = "add returns the sum"
    session.evidence_obligations = [dict(oid="O1", task="t1", kind="acceptance", proof_id="P1", behavior="P1: add",
                                         requirements=[["R1", 1]], cases=["tests/test_app.py::test_add"],
                                         modality="test", location="tests/test_app.py", execution="missing",
                                         sufficiency="unjudged")]
    spec = SimpleNamespace(task_id="t1", scope=TaskScope.unbounded())
    session._active_spec = spec
    record = session._trim_snapshot(spec, "draft", None)
    (project / "tests" / "test_app.py").write_text("def test_add():\n    pass\n")
    session._trim_settle(record, None)
    session._trim_block(spec)
    session._read_trim_disposition(spec, "reviewer", "TRIM T-t1-1: PRESERVED")
    assert session._trim_owed() == []
    session.memory.ledger.requirements["R1"] = "add returns the sum and refuses strings"
    assert session._requirement_revisions()["R1"] == 2
    assert session._trim_owed() == [record], "a reworded covered requirement owes the trim again"
    session._trim_block(spec)
    session._read_trim_disposition(spec, "reviewer", "TRIM T-t1-1: PRESERVED")
    assert session._trim_owed() == []
    session.evidence_obligations[0]["cases"] = ["tests/test_app.py::test_other"]
    assert session._trim_owed() == [record], "a rebound scenario owes the trim again"


@pytest.mark.requirements_ledger
def test_an_edit_after_a_preserved_verdict_renews_the_review_against_the_full_pre_trim_evidence(tmp_path, monkeypatch):
    """Root's fourth control. The reviewer judges the trim PRESERVED and
    blocks on something else; the fix revision deletes the add(2, 2)
    assertion the trim had kept. The rechecker and the audit are shown the
    comparison from the immutable pre-trim evidence to the files as they
    are now (the deleted assertion visible), the earlier approval is
    named stale, and silence does not approve it: the run does not
    complete until a judgment bound to the current source says PRESERVED."""
    review = ("BLOCKING: the docstring is wrong | basis: R1 | impact: correctness\nR1: MET - app.py\n"
              "TRIM T-t1-1: PRESERVED")
    silent = _trim_job(_at(tmp_path, "silent"), monkeypatch, TRIMMED_KEPT, review, later=TRIMMED_LOSS,
                       recheck=lambda c, r: "RFT1-1: RESOLVED docstring fixed")
    record = _trim(silent)[0]
    recheck = silent.of("recheck")[-1].prompt
    assert "## Trim evidence" in recheck and "an earlier PRESERVED is stale" in recheck
    assert "-    assert add(2, 2) == 4" in recheck, "the renewed review shows the full pre-trim comparison"
    audit = silent.of("auditor")[-1].prompt
    assert "## Trim evidence" in audit and "-    assert add(2, 2) == 4" in audit and "T-t1-1:" in audit
    assert record["disposition"]["verdict"] == "PRESERVED" and record["disposition"]["evidence"] != silent_evidence(silent, record)
    assert not silent.result.completed and "preserved" in silent.result.error.lower()

    renewed = _trim_job(_at(tmp_path, "renewed"), monkeypatch, TRIMMED_KEPT, review, later=TRIMMED_LOSS,
                        recheck=lambda c, r: "RFT1-1: RESOLVED docstring fixed\nTRIM T-t1-1: LOST - add(2, 2) deleted by the fix")
    record = _trim(renewed)[0]
    assert record["disposition"]["verdict"] == "LOST" and record["disposition"]["by"] and not renewed.result.completed


def silent_evidence(replay, record):
    """The evidence id a verdict on the final source would carry: the
    record's id, its artifact digest and the final source fingerprint."""
    return hashlib.sha256(json.dumps(dict(tid=record["tid"], pre=record["artifact_sha256"],
                                          source=H.result_json(replay)["source_fingerprint"]),
                                     sort_keys=True).encode()).hexdigest()


def test_trim_evidence_covers_the_support_files_assertions_lean_on(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    for name, text in {"tests/test_app.py": "x", "tests/conftest.py": "y", "tests/helpers.py": "z",
                       "fixtures_data.py": "w", "app.py": "code", "README.md": "doc"}.items():
        (project / name).write_text(text)
    session = _session(tmp_path, project)
    files, problem = session._trim_files([TaskScope.unbounded().to_dict()], None)
    assert files == {"tests/test_app.py": "x", "tests/conftest.py": "y", "tests/helpers.py": "z", "fixtures_data.py": "w",
                     "app.py": "code"}, "every permitted Python source and support text, whole; README is neither"
    assert problem is None
    bounded = TaskScope(permitted_paths=["tests/test_app.py"], intended_result="x", acceptance=["y"], max_lines=10)
    assert session._trim_files([bounded.to_dict()], None) == ({"tests/test_app.py": "x"}, None)
    assert session._trim_files(None, None)[0] is None, "no policy is unknown, never unbounded"
    # The conservative capture: every permitted Python source file and
    # support text, whole, with no import grammar deciding what an
    # assertion leans on (imports of any form, aliased, multiline or
    # dynamic, change nothing about what is captured).
    (project / "tests" / "test_app.py").write_text("import app, checks.asserts as ca\nfrom pkg import (\n    child,\n)\n")
    (project / "checks").mkdir()
    (project / "checks" / "asserts.py").write_text("from .deep import more\n")
    (project / "checks" / "deep.py").write_text("import importlib\nmod = importlib.import_module('support_x')\n")
    (project / "support_x.py").write_text("X = 1\n")
    (project / "pkg").mkdir()
    (project / "pkg" / "__init__.py").write_text("")
    (project / "pkg" / "child.py").write_text("Y = 1\n")
    files, problem = session._trim_files([TaskScope.unbounded().to_dict()], None)
    assert problem is None
    for name in ("app.py", "checks/asserts.py", "checks/deep.py", "support_x.py", "pkg/__init__.py", "pkg/child.py"):
        assert name in files, name
    assert "README.md" not in files
    narrow = TaskScope(permitted_paths=["tests/**", "app.py"], intended_result="x", acceptance=["y"], max_lines=10)
    files, problem = session._trim_files([narrow.to_dict()], None)
    assert "app.py" in files and "checks/asserts.py" not in files, "the capture never reaches past the policy"


def test_a_capture_past_the_bound_is_never_called_complete_and_the_trim_stays_owed(tmp_path, monkeypatch):
    """Root's ninth control on 00cfc02: more permitted modules than the
    bound, each holding a required assertion; the record never says
    complete, an assertion removed from a module past the bound is not an
    unchanged trim, and PRESERVED is refused. The same past the contents
    bound."""
    monkeypatch.setattr("quadratus.session._TRIM_SUPPORT_MAX_FILES", 50)
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    n = 51
    (project / "tests" / "test_state.py").write_text("".join(f"import m{i:03d}\n" for i in range(n)))
    for i in range(n):
        (project / f"m{i:03d}.py").write_text(f"def check_{i}():\n    assert {i} == {i}\n")
    session = _session(tmp_path, project)
    spec = SimpleNamespace(task_id="t1", scope=TaskScope.unbounded())
    session._active_spec = spec
    files, problem = session._trim_files([TaskScope.unbounded().to_dict()], None)
    assert problem and "over the 50-file evidence bound" in problem
    record = session._trim_snapshot(spec, "draft", None)
    assert not record["complete"] and record["inventory_problem"] and "not on file" in record["state"]
    (project / f"m{n - 1:03d}.py").write_text(f"def check_{n - 1}():\n    pass\n")
    session._trim_settle(record, None)
    assert record["changed"] is None and "incomplete" in record["state"]
    assert session._trim_owed() == [record]
    block = session._trim_block(spec)
    assert "not on file" in block and "cannot be judged PRESERVED" in block
    session._read_trim_disposition(spec, "auditor", "TRIM T-t1-1: PRESERVED")
    assert record["disposition"]["verdict"] == "REFUSED" and session._trim_owed() == [record]

    monkeypatch.setattr("quadratus.session._TRIM_EVIDENCE_MAX_CHARS", 100)
    small = tmp_path / "small"
    (small / "tests").mkdir(parents=True)
    (small / "tests" / "test_x.py").write_text("x" * 200)
    session = _session(tmp_path / "s", small)
    files, problem = session._trim_files([TaskScope.unbounded().to_dict()], None)
    assert "over the evidence bound" in problem and "tests/test_x.py" in files


def test_the_audit_is_shown_the_whole_canonical_scenario_or_told_it_is_not_shown(tmp_path):
    """Astra's finding on 00cfc02: the audit and a reassessment read the
    full scenario and location, never a prefix that hides the expected
    result; a scenario over the render bound is said to be not shown."""
    from quadratus.session import _SCENARIO_RENDER_MAX_CHARS
    session = _session(tmp_path)
    long_op = "x" * 260
    row = dict(oid="O1", task="t1", kind="acceptance", behavior=f"P1: {long_op}; given s; expect e; in README.md"[:300],
               requirements=[["R1", 1]], runner=None, execution="missing", sufficiency="unjudged", modality="reading",
               location="README.md", cases=[],
               scenario=[["evidence", "reading"], ["expect", "the README names add and its result"],
                         ["location", "README.md"], ["operation", long_op], ["proof", "P1"], ["state", "add exists"]])
    block = session._obligations_block([row])
    assert "expect: the README names add and its result" in block and "location: README.md" in block
    assert long_op in block
    huge = dict(row, scenario=[["evidence", "reading"], ["expect", "e"], ["location", "README.md"],
                               ["operation", "y" * (_SCENARIO_RENDER_MAX_CHARS + 1)], ["proof", "P1"], ["state", "s"]])
    block = session._obligations_block([huge])
    assert "not shown; leave this row unjudged" in block and "y" * 100 not in block


def test_trim_records_and_binding_problems_are_carried_by_the_checkpoint(tmp_path):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "", config=SessionConfig())
    session.trim_records = [dict(task="t1", site="draft", changed=True, disposition=None)]
    session.proof_binding_problems = [dict(task="t1", problem="P7: not a scenario of this task")]
    state = checkpoint.capture_state(session)
    fresh = Session("goal", ArtifactStore(tmp_path / "b"), lambda *a, **k: "", config=SessionConfig())
    checkpoint.restore_state(fresh, state)
    assert fresh.trim_records == session.trim_records and fresh.proof_binding_problems == session.proof_binding_problems
    older = Session("goal", ArtifactStore(tmp_path / "c"), lambda *a, **k: "", config=SessionConfig())
    state.pop("trim_records")
    state.pop("proof_binding_problems")
    checkpoint.restore_state(older, state)
    assert older.trim_records == [] and older.proof_binding_problems == []
