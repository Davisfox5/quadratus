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

from quadratus import checkpoint
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
    script = _script(None, lead, auditor=lambda c, r: f"R1: MET - {CASE}\nR2: MET - README.md",
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
    task = ("KIND: backend simple\nSCOPE: " + json.dumps(_scope()) + "\nImplement add in app.py.\nCOVERS: R1")
    script = _script(None, _lead(proves=()))
    script.overrides["orchestrator"] = lambda c, r: "REQUIREMENTS:\nR1: add(a, b) returns the sum\n" + task
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False,
                   tasks=["REQUIREMENTS:\nR1: add(a, b) returns the sum\n" + task])
    rows = _acceptance(replay)
    assert rows and all(r["binding"].startswith("unknown (legacy acceptance)") and r["execution"] == "missing"
                        and not r["established"] and r["executable_claim"] and r["legacy"]
                        and r["modality"] == "test" for r in rows), rows
    assert H.result_json(replay)["checks"] and H.result_json(replay)["checks"][-1]["passed"]
    # A1: unknown stays visibly unknown and unproved. The green check
    # discharged nothing, the audit's MET is lowered as any unexecuted
    # executable claim is, and no judgment of sufficiency is inferred from
    # a verdict that cited the source.
    assert rows[0]["sufficiency"] == "unjudged" and not rows[0]["judged_by"]
    assert "unknown (legacy acceptance)" in replay.of("auditor")[-1].prompt
    assert not replay.result.completed and "not executed" in replay.result.error, replay.result.error
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert audit["acceptance"]["R1"]["met"] is False


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


@pytest.mark.requirements_ledger
def test_a_reading_claim_counts_only_on_an_audit_verdict_citing_its_file_on_the_current_source(tmp_path, monkeypatch):
    """A1: a documentation scenario declared as reading evidence is never
    discharged by the green check and never lowered as unexecuted; it is
    accepted only when the audit's own MET cites the file it names. A MET
    citing something else judges nothing about it, and R1 stays unmet."""
    cited = H.run(_at(tmp_path, "cited"), monkeypatch,
                  _script(_docs_plan(), _docs_lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                          auditor=lambda c, r: "R1: MET - README.md"),
                  files=FILES_OK, max_tasks=1, record_complete=False)
    assert cited.result.completed, cited.result.error
    row = _acceptance(cited)[0]
    assert row["modality"] == "reading" and not row["executable_claim"] and row["runner"] is None
    assert row["binding"].startswith("reading claim") and row["execution"] == "missing" and row["cases"] == []
    assert row["sufficiency"] == "sufficient" and row["judged_by"] and row["established"]
    assert row["source_digest"] == H.result_json(cited)["source_fingerprint"]
    assert "declared evidence: reading" in cited.of("auditor")[-1].prompt or "reading" in cited.of("auditor")[-1].prompt

    elsewhere = H.run(_at(tmp_path, "elsewhere"), monkeypatch,
                      _script(_docs_plan(), _docs_lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - README.md",
                              auditor=lambda c, r: "R1: MET - app.py"),
                      files=FILES_OK, max_tasks=1, record_complete=False)
    row = _acceptance(elsewhere)[0]
    assert row["sufficiency"] == "unjudged" and "did not cite README.md" in row.get("sufficiency_note", "")
    assert not row["established"] and not elsewhere.result.completed
    audit = H.result_json(elsewhere)["requirements"]["audits"][-1]
    assert audit["acceptance"]["R1"]["met"] is False and "reading claim judged unjudged" in " ".join(
        audit["acceptance"]["R1"]["reasons"])


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
    assert records[0]["tid"] == "T1" and records[0]["state"] == "preservation owed; lost by claude:opus"
    # The overrun draft never delivered (its PROVES line is not read past
    # the stop), so the snapshot holds the scenario unbound; the trim's own
    # delivery binds it afterwards.
    assert records[0]["proofs"] == [dict(proof="P1", cases=[])]
    assert _acceptance(replay)[0]["cases"] == [CASE]
    pre = json.loads(replay.artifact_texts("pre-trim-tests")[0])
    assert pre["tests/test_app.py"] == BIG_TESTS, "the full pre-trim contents are on file, not a case-name list"
    review = replay.of("collaborator")[-1].prompt
    assert "## Trim evidence" in review and "T1: task t1, draft trim" in review
    assert "-    assert add(2, 2) == 4" in review and "'TRIM T<n>: PRESERVED'" in review
    assert records[0]["disposition"]["verdict"] == "LOST" and "add(2, 2)" in records[0]["disposition"]["detail"]
    assert records[0]["disposition"]["evidence"] and records[0]["disposition"]["by"] == "claude:opus"
    assert not replay.result.completed
    assert "T1 (t1, draft trim: preservation owed; lost by claude:opus)" in replay.result.error


@pytest.mark.requirements_ledger
def test_an_equivalent_refactor_the_reviewer_judges_preserved_completes_and_names_decide_nothing(tmp_path,
                                                                                                   monkeypatch):
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM T1: PRESERVED")
    record = _trim(replay)[0]
    assert record["changed"] and record["disposition"]["verdict"] == "PRESERVED"
    assert record["disposition"]["evidence"] == record["shown"]
    assert replay.result.completed, replay.result.error

    other = _trim_job(_at(tmp_path, "other"), monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM T2: PRESERVED")
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
    assert not record["complete"] and record["changed"] and record["artifact"]
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

    def flaky(self, spec, before):
        calls.append(1)
        return None if len(calls) == 1 else real(self, spec, before)
    monkeypatch.setattr(Session, "_trim_files", flaky)
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM T1: PRESERVED",
                       auditor=lambda c, r: "TRIM T1: PRESERVED\nR1: MET - tests/test_app.py::test_add")
    record = _trim(replay)[0]
    assert record["artifact"] is None and record["changed"] is None and "pre-trim evidence unavailable" in record["state"]
    assert record["evidence_state"].startswith("pre-trim evidence unavailable")
    assert "cannot be judged PRESERVED" in replay.of("collaborator")[-1].prompt
    assert record["disposition"]["verdict"] == "REFUSED"
    assert "T1" in replay.of("auditor")[-1].prompt and not replay.result.completed
    assert "no independent judgment bound to the current source" in replay.result.error
    assert "T1 (t1, draft trim: pre-trim evidence unavailable" in replay.result.error


@pytest.mark.requirements_ledger
def test_a_trim_whose_post_trim_files_could_not_be_read_is_owed_not_unchanged(tmp_path, monkeypatch):
    from quadratus.session import Session
    real = Session._trim_files
    calls = []

    def flaky(self, spec, before):
        calls.append(1)
        return None if len(calls) == 2 else real(self, spec, before)
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
    assert record["tid"] == "T1" and record["complete"] and record["changed"] and record["state"] == "preservation owed"
    assert session._trim_pre_contents(record) == {"tests/test_app.py": "def test_add():\n    assert 1 + 2 == 3\n"}
    assert session._trim_block(spec) and record["shown"] == session._trim_evidence_id(record)
    session._read_trim_disposition(spec, "reviewer", "TRIM T1: PRESERVED")
    assert record["disposition"]["verdict"] == "PRESERVED" and not session._trim_owed()

    record["disposition"] = None
    good_sha = record["artifact_sha256"]
    record["artifact_sha256"] = hashlib.sha256(b"other").hexdigest()
    assert session._trim_pre_contents(record) is None and session._trim_owed() == [record]
    block = session._trim_block(spec)
    assert "not on file" in block and "cannot be judged PRESERVED" in block
    session._read_trim_disposition(spec, "reviewer", "TRIM T1: PRESERVED")
    assert record["disposition"]["verdict"] == "REFUSED" and session._trim_owed() == [record]
    session._read_trim_disposition(spec, "auditor", "TRIM T1: LOST - the assertion is gone")
    assert record["disposition"]["verdict"] == "LOST" and session._trim_owed() == [record]

    record["artifact_sha256"] = good_sha
    record["artifact"] = "no-such-artifact"
    assert session._trim_pre_contents(record) is None


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
              "TRIM T1: PRESERVED")
    silent = _trim_job(_at(tmp_path, "silent"), monkeypatch, TRIMMED_KEPT, review, later=TRIMMED_LOSS,
                       recheck=lambda c, r: "RFT1-1: RESOLVED docstring fixed")
    record = _trim(silent)[0]
    recheck = silent.of("recheck")[-1].prompt
    assert "## Trim evidence" in recheck and "an earlier PRESERVED is stale" in recheck
    assert "-    assert add(2, 2) == 4" in recheck, "the renewed review shows the full pre-trim comparison"
    audit = silent.of("auditor")[-1].prompt
    assert "## Trim evidence" in audit and "-    assert add(2, 2) == 4" in audit and "T1:" in audit
    assert record["disposition"]["verdict"] == "PRESERVED" and record["disposition"]["evidence"] != silent_evidence(silent, record)
    assert not silent.result.completed and "preserved" in silent.result.error.lower()

    renewed = _trim_job(_at(tmp_path, "renewed"), monkeypatch, TRIMMED_KEPT, review, later=TRIMMED_LOSS,
                        recheck=lambda c, r: "RFT1-1: RESOLVED docstring fixed\nTRIM T1: LOST - add(2, 2) deleted by the fix")
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
    files = session._trim_files(SimpleNamespace(task_id="t1", scope=TaskScope.unbounded()), None)
    assert files == {"tests/test_app.py": "x", "tests/conftest.py": "y", "tests/helpers.py": "z", "fixtures_data.py": "w"}
    bounded = TaskScope(permitted_paths=["tests/test_app.py"], intended_result="x", acceptance=["y"], max_lines=10)
    assert session._trim_files(SimpleNamespace(task_id="t1", scope=bounded), None) == {"tests/test_app.py": "x"}


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
