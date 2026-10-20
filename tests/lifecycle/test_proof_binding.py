"""Proof scenarios (proof/1), their binding to executed cases, and the
immutable trim evidence with its independent preservation disposition.

Real Session and controller with scripted replies; every claim here is a
record the harness wrote, never a model's word. The mapping is validated
for shape and correspondence only: whether the scenarios are enough is the
independent review's judgment, and nothing here infers MET from a bound or
executed case."""
import json
from pathlib import Path

import pytest

from quadratus import checkpoint
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script

CASE = "tests/test_app.py::test_add"
NEGATIVE = "tests/test_app.py::test_negative"


def _scope(covers=("R1",), proofs=None, max_lines=20):
    scope = dict(permitted_paths=["app.py", "tests/test_app.py"], intended_result="add works",
                 acceptance=["add(1, 2) == 3"], max_lines=max_lines)
    if proofs is not None:
        scope["proofs"] = proofs
    return scope


def _proofs(*rids, state="two tickets exist", expect="the sum is returned"):
    return [dict(id=f"P{i}", requirements=[rid], operation=f"add is called for {rid}", state=state,
                 expect=expect, location="tests/test_app.py") for i, rid in enumerate(rids, 1)]


def _plan(scope, covers="R1", requirements="R1: add(a, b) returns the sum"):
    return (f"REQUIREMENTS:\n{requirements}\nKIND: backend simple\nSCOPE: {json.dumps(scope)}\n"
            f"Implement add in app.py.\nCOVERS: {covers}")


def _lead(proves=(CASE,), tests=None):
    def lead(call, replay):
        files = {"app.py": FIXED}
        if tests is not None:
            files["tests/test_app.py"] = tests
        H.write(call, files)
        lines = "".join(f"\nPROVES: P1 - {case}" for case in proves)
        return f'Implemented add.{lines}\nCHANGED: {json.dumps(sorted(files))}'
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
                        and not r["established"] and not r["executable_claim"] and r["legacy"] for r in rows), rows
    assert H.result_json(replay)["checks"] and H.result_json(replay)["checks"][-1]["passed"]
    assert "not executed" not in (replay.result.error or ""), "a legacy string claims no executable proof"
    # Accepted only on the audit's own current judgment, recorded as such;
    # the mapping stays unknown on the record.
    assert rows[0]["sufficiency"] == "sufficient" and rows[0]["judged_by"] and rows[0]["judged_source"]
    assert "unknown (legacy acceptance)" in replay.of("auditor")[-1].prompt


@pytest.mark.requirements_ledger
def test_a_malformed_or_unexecuted_binding_fails_closed(tmp_path, monkeypatch):
    """PROVES naming an unknown scenario or a non-case binds nothing and is
    recorded as a problem; PROVES naming a case the check never ran stays
    bound and undischarged although the check passed."""
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return ('Implemented add.\nPROVES: P7 - tests/test_app.py::test_add\nPROVES: P1 - add works\n'
                'PROVES: P1 - app.py::test_add\nCHANGED: ["app.py"]')
    bad = H.run(_at(tmp_path, "bad"), monkeypatch, _script(_plan(_scope(proofs=_proofs("R1"))), lead), files=FILES,
                max_tasks=1, record_complete=False)
    row = _acceptance(bad)[0]
    assert (row["binding"], row["cases"], row["execution"]) == ("unbound", [], "missing")
    problems = H.result_json(bad)["proof_binding_problems"]
    assert [p["problem"][:4] for p in problems] == ["P7: ", "P1: "], problems

    missing = H.run(_at(tmp_path, "missing"), monkeypatch,
                    _script(_plan(_scope(proofs=_proofs("R1"))), _lead(proves=("tests/test_app.py::test_absent",))),
                    files=FILES, max_tasks=1, record_complete=False)
    row = _acceptance(missing)[0]
    assert row["binding"] == "bound" and row["cases"] == ["tests/test_app.py::test_absent"]
    assert row["execution"] == "missing" and not row["established"]
    assert H.result_json(missing)["checks"][-1]["passed"]


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


# -- 3. trim evidence and the independent preservation disposition -------------------------------

BIG_TESTS = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(2, 2) == 4\n"
             + "".join(f"\n\ndef test_case_{i}():\n    assert add({i}, 1) == {i + 1}\n" for i in range(12)))
TRIMMED_LOSS = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
                + "".join(f"\n\ndef test_case_{i}():\n    assert add({i}, 1) == {i + 1}\n" for i in range(4)))
TRIMMED_KEPT = ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n    assert add(2, 2) == 4\n"
                + "\n\ndef test_cases():\n    for i in range(4):\n        assert add(i, 1) == i + 1\n")


def _trim_job(tmp_path, monkeypatch, trimmed, review, *, auditor=None, max_chars=None):
    """A draft past the 30-line stop (bound 20), trimmed by the lead to
    ``trimmed``; the reviewer answers ``review``."""
    if max_chars is not None:
        monkeypatch.setattr("quadratus.session._TRIM_EVIDENCE_MAX_CHARS", max_chars)
    script = _script(_plan(_scope(proofs=_proofs("R1"))), _lead(tests=BIG_TESTS),
                     collaborator=lambda c, r: review,
                     auditor=auditor or (lambda c, r: "R1: MET - tests/test_app.py::test_add"))

    def revision(call, replay):
        H.write(call, {"tests/test_app.py": trimmed})
        return f'Trimmed the tests.\nPROVES: P1 - {CASE}\nCHANGED: ["tests/test_app.py"]'
    script.overrides["revision"] = revision
    return H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)


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
    # The overrun draft never delivered (its PROVES line is not read past
    # the stop), so the snapshot holds the scenario unbound; the trim's own
    # delivery binds it afterwards.
    assert records[0]["diff_complete"] and records[0]["proofs"] == [dict(proof="P1", cases=[])]
    assert _acceptance(replay)[0]["cases"] == [CASE]
    pre = json.loads(replay.artifact_texts("pre-trim-tests")[0])
    assert pre["tests/test_app.py"] == BIG_TESTS, "the full pre-trim contents are on file, not a case-name list"
    review = replay.of("collaborator")[-1].prompt
    assert "## Trim evidence" in review and "-    assert add(2, 2) == 4" in review and "TRIM: PRESERVED" in review
    assert records[0]["disposition"]["verdict"] == "LOST" and "add(2, 2)" in records[0]["disposition"]["detail"]
    assert records[0]["disposition"]["source"] == H.result_json(replay)["source_fingerprint"]
    assert not replay.result.completed and "LOST" in replay.result.error or not replay.result.completed


@pytest.mark.requirements_ledger
def test_an_equivalent_refactor_the_reviewer_judges_preserved_completes_and_names_decide_nothing(tmp_path,
                                                                                                   monkeypatch):
    replay = _trim_job(tmp_path, monkeypatch, TRIMMED_KEPT, "NO FINDINGS\nR1: MET - app.py\nTRIM: PRESERVED")
    record = _trim(replay)[0]
    assert record["changed"] and record["disposition"]["verdict"] == "PRESERVED"
    assert replay.result.completed, replay.result.error


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
    assert not record["complete"] and not record["diff_complete"] and record["changed"]
    assert "not on file" in short.of("collaborator")[-1].prompt
    assert record["disposition"]["verdict"] == "REFUSED" and not short.result.completed
    assert not short.artifact_texts("pre-trim-tests") and short.artifact_texts("pre-trim-hashes")

    double = _trim_job(_at(tmp_path, "double"), monkeypatch, TRIMMED_KEPT,
                       "NO FINDINGS\nR1: MET - app.py\nTRIM: PRESERVED\nTRIM: LOST - unsure")
    assert _trim(double)[0]["disposition"] is None and not double.result.completed


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
