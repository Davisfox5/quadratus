"""Cycle04 (PR73 6713487831), scope-blocked repair routing in the real
controller.

Completion-cycle03's t5 had a README/tests grant; its lead and reviewer both
said the blocker was in a production file, and it still received a second
revision, its recheck and a gate fix under the same grant. Here t1 holds a
README/tests grant while add() in app.py is wrong. Its first revision does
the in-scope work and declares 'OUTSIDE SCOPE' for the production finding
and the failed case: no second same-scope round, no same-grant gate fix; the
failed check and the finding stay open with the declared path; the planner
sees the path and names a correctly scoped RESOLVES task; that task's own
check passes on the exact source and the original reviewer's recheck closes
the finding; the audit decides. A declaration is a claim: it never grants,
edits, closes or proves anything."""
import json

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import expected_finding_ids, recheck_reply
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_proof_binding import CASE, _at, _default_auditor, _proofs

pytestmark = pytest.mark.requirements_ledger

PRODUCTION = "BLOCKING: add returns 0, so add(1, 2) is not 3; app.py must return the sum | basis: R1 | impact: correctness"
DOCS = "BLOCKING: README does not document add | basis: R1 | impact: contract"
README = "# app\n\n`add(a, b)` returns the sum of a and b.\n"
ROUTE = "its lead declared the repair site outside t1's scope: app.py"


def _scope(paths, **extra):
    return dict(permitted_paths=paths, intended_result="add documented and correct", acceptance=["add(1, 2) == 3"],
                max_lines=40, proofs=_proofs("R1"), **extra)


T1 = ("REQUIREMENTS:\nR1: add(a, b) returns the sum and is documented\nKIND: backend simple\nSCOPE: "
      + json.dumps(_scope(["README.md", "tests/test_app.py"])) + "\nDocument add and keep its test.\nCOVERS: R1")
T2 = ("KIND: backend simple\nSCOPE: " + json.dumps(_scope(["app.py"]))
      + "\nMake add return the sum.\nCOVERS: R1\nRESOLVES: F1, RFT1-1")


def _planning(replay):
    return [c for c in replay.of("orchestrator") if "Name the single next task" in c.prompt]


def _script(*, t1_recheck, revision, plans=(T1, T2)):
    plans = list(plans)

    def orchestrator(call, replay):
        n = len(_planning(replay)) if "Name the single next task" in call.prompt else 0
        return plans[n - 1] if 0 < n <= len(plans) else "DONE"

    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"README.md": "# app\n"})
            return f'Started the docs.\nPROVES: P1 - {CASE}\nCHANGED: ["README.md"]'
        H.write(call, {"app.py": FIXED})
        return f'add returns the sum.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        if call.task == "t1":
            return f"{PRODUCTION}\n{DOCS}\nR1: NOT MET - add is wrong"
        return "NO FINDINGS\nR1: MET - app.py"

    def recheck(call, replay):
        if "RFT1-2" in expected_finding_ids(call.prompt):
            return t1_recheck(call, replay)
        return recheck_reply(call.prompt, default="RESOLVED app.py now returns the sum")
    return Script(orchestrator=orchestrator, lead=lead, collaborator=collaborator, recheck=recheck,
                  revision=revision, auditor=_default_auditor, **{"requirements-review": lambda c, r: "COMPLETE"})


def _declaring(call, replay):
    H.write(call, {"README.md": README})
    return ("Documented add.\nOUTSIDE SCOPE: RFT1-1 - app.py\n"
            f"OUTSIDE SCOPE: {CASE} - app.py\nCHANGED: [\"README.md\"]")


def _docs_fixed(call, replay):
    return recheck_reply(call.prompt, {"RFT1-1": "UNRESOLVED app.py still returns 0", "RFT1-2": "RESOLVED documented"})


def _of(replay, role, task):
    return [c for c in replay.calls if c.role == role and c.task == task]


def test_a_declared_scope_blocker_routes_to_a_scoped_resolving_task_and_genuine_acceptance(tmp_path, monkeypatch):
    replay = H.run(_at(tmp_path, "routed"), monkeypatch, _script(t1_recheck=_docs_fixed, revision=_declaring),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    # The rule is taught in the draft and the revision under the restricted grant.
    assert "OUTSIDE SCOPE: <finding id or failed case id>" in _of(replay, "lead", "t1")[0].prompt
    # First useful correction kept; no second same-scope round; no same-grant gate fix.
    assert len(_of(replay, "revision", "t1")) == 1
    assert not _of(replay, "gate-fix", "t1")
    (skip,) = record["scope_gate_skips"]
    assert skip["task"] == "t1" and skip["cases"] == [CASE] and skip["paths"] == ["app.py"]
    routed = {(d["id"], d["status"]) for d in record["scope_declarations"]}
    assert routed == {("RFT1-1", "routed"), (CASE, "routed")}
    # Planner-visible debt carries the path, for the check finding and the review finding.
    second = _planning(replay)[1].prompt
    assert "F1 (found by t1" in second and ROUTE in second
    f1 = next(f for f in record["findings"] if f["id"] == "F1")
    assert f1["scope"]["paths"] == ["app.py"] and f1["scope"]["cases_declared"] == [CASE]
    # The scoped repair: its own exact-source check passes, F1 settles on it,
    # and the original reviewer's recheck closes RFT1-1.
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"]
    assert f1["status"] == "resolved" and f1["resolved_by"] == "t2"
    rft = next(r for r in record["review_findings"] if r["fid"] == "RFT1-1")
    assert rft["closed"] and rft["route"] == "scope" and rft["scope_paths"] == ["app.py"]
    assert replay.of("auditor") and replay.result.completed, replay.result.error


def test_mixed_work_keeps_its_in_scope_round_and_drops_only_the_declared_finding(tmp_path, monkeypatch):
    """RFT1-2 (in scope) is still open after the first revision: the second
    round is bought for it alone; RFT1-1 is never put back to the editor."""
    revisions = []

    def revision(call, replay):
        revisions.append(call.prompt)
        if len(revisions) == 1:
            return _declaring(call, replay)
        H.write(call, {"README.md": README + "\nExample: add(1, 2) == 3.\n"})
        return 'Added an example.\nCHANGED: ["README.md"]'
    rounds = []

    def t1_recheck(call, replay):
        rounds.append(call.prompt)
        if len(rounds) == 1:
            return recheck_reply(call.prompt, {"RFT1-1": "UNRESOLVED app.py still returns 0",
                                               "RFT1-2": "UNRESOLVED no example yet"})
        # The same reviewer rechecks its whole round; it still holds RFT1-1.
        return recheck_reply(call.prompt, {"RFT1-1": "UNRESOLVED app.py still returns 0"},
                             default="RESOLVED now documented")
    replay = H.run(_at(tmp_path, "mixed"), monkeypatch, _script(t1_recheck=t1_recheck, revision=revision),
                   files=FILES, max_tasks=3, record_complete=False)
    assert len(revisions) == 2
    assert "RFT1-2" in revisions[1] and "RFT1-1" not in revisions[1].split("OUTSIDE SCOPE")[0], \
        "the declared finding is not the editor's again"
    assert replay.result.completed, replay.result.error


def test_without_a_declaration_the_same_scope_rounds_run_as_before(tmp_path, monkeypatch):
    def revision(call, replay):
        H.write(call, {"README.md": README})
        return 'Documented add; app.py is outside my grant.\nCHANGED: ["README.md"]'
    replay = H.run(_at(tmp_path, "prose"), monkeypatch, _script(t1_recheck=_docs_fixed, revision=revision),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    assert record["scope_declarations"] == [] and record["scope_gate_skips"] == []
    assert len(_of(replay, "revision", "t1")) == 2, "prose alone routes nothing"
    assert _of(replay, "gate-fix", "t1"), "the gate fix is bought as before"


def test_a_resolving_task_without_the_declared_path_is_sent_back_once_then_proceeds(tmp_path, monkeypatch):
    narrow = ("KIND: backend simple\nSCOPE: " + json.dumps(_scope(["tests/test_app.py"]))
              + "\nTry again from the tests.\nCOVERS: R1\nRESOLVES: F1, RFT1-1")
    plans = [T1, narrow, narrow]
    replay = H.run(_at(tmp_path, "sendback"), monkeypatch,
                   _script(t1_recheck=_docs_fixed, revision=_declaring, plans=plans),
                   files=FILES, max_tasks=3, record_complete=False)
    third = _planning(replay)[2].prompt
    assert "TASK SENT BACK" in third and "declared repair site: app.py" in third
    assert [c.task for c in replay.of("lead")][:2] == ["t1", "t2"], "named again as written, it proceeds"
    assert H.result_json(replay)["scope_sendbacks"] == ["F1:app.py", "RFT1-1:app.py"]
    assert not replay.result.completed, "a repair that cannot touch app.py completes nothing"


# -- S2: a check finding's cause, persisted and read the same way everywhere ----------------------

NEW_BLOCKER = "BLOCKING: add accepts strings without an error | basis: R1 | impact: correctness"


def _cycle03_shape(tmp_path, monkeypatch, limits, name):
    """t1 leaves the check failing (F1); t2 RESOLVES F1 and its own check
    passes, but its reviewer raises a new blocker; t2 is the last slot."""
    from tests.lifecycle import test_check_debt as D
    from tests.lifecycle.replies import judging_scenarios
    (tmp_path / name).mkdir()
    over = dict(orchestrator=D._plan(D.REQS + D.IMPLEMENT, D.REPAIR),
                lead=lambda c, r: {"t1": D._looked, "t2": D._fixes}[c.task](c, r),
                collaborator=lambda c, r: (NEW_BLOCKER + "\nR1: MET - app.py") if c.task == "t2"
                else "NO FINDINGS\nR1: MET - app.py",
                **{"requirements-review": lambda c, r: "COMPLETE",
                   "auditor": judging_scenarios(lambda c, r: D.MET, D.ADD_PROOF, basis=D.ADD_SHOWN)})
    return H.run(tmp_path / name, monkeypatch, Script(**over), files=D.FILES, max_tasks=2, limits=limits,
                 record_complete=False)


def test_an_unsettled_check_finding_names_its_cause_on_the_stop_the_record_and_the_checker(tmp_path, monkeypatch):
    from tests.lifecycle.test_affordable_planning import HELD, PER_CALL, _limits
    from tools import truth_check
    base = _cycle03_shape(tmp_path, monkeypatch, _limits(6_000_000), "calibrate")
    calls = next(i for i, c in enumerate(base.calls) if c.role == "collaborator" and c.task == "t2") + 1
    # 250 left after t2's review: the correction (revision 300 + recheck 100) is refused.
    replay = _cycle03_shape(tmp_path, monkeypatch, _limits(PER_CALL * calls + HELD + 250), "limited")
    record = H.result_json(replay)
    f1 = next(f for f in record["findings"] if f["id"] == "F1")
    assert f1["status"] == "open", "a green check is not acceptance"
    unsettled = f1["unsettled"]
    assert unsettled["check"] == dict(state="passed", attempt=1, source="current",
                                      counts=dict(passed=1, failed=0, skipped=0))
    assert unsettled["blockers_left"] == ["RFT2-1"]
    assert unsettled["refused_round"]["needed"] == 400 and unsettled["refused_round"]["available"] == 250
    assert unsettled["refused_round"]["short"] == 150
    assert "adverse" not in unsettled["why"]
    error = replay.result.error
    assert error.startswith("FindingsUnresolved: task t2 named RESOLVES F1") and unsettled["why"] in error
    unverified = truth_check.judge(replay.result.run_dir, None)["unverified"]
    assert any(u.startswith("open finding F1:") and unsettled["why"][:200] in u for u in unverified), unverified
    assert not replay.result.completed


def test_a_child_declaration_survives_the_parallel_merge(tmp_path, monkeypatch):
    from tests.lifecycle.test_finish_capacity import BATCH
    from tests.lifecycle.test_finish_capacity import _script as batch_script
    script = batch_script([BATCH])
    revisions = []

    def revision(call, replay):
        revisions.append(call.task)
        if call.task == "t2":
            H.write(call, {"README.md": "# app\n\n`add(a, b)` returns the sum.\n"})
            return 'Documented.\nOUTSIDE SCOPE: RFT2-1 - app.py\nCHANGED: ["README.md"]'
        return "Nothing to change.\nCHANGED: []"
    script.overrides["revision"] = revision
    script.overrides["recheck"] = lambda call, replay: "UNRESOLVED: the example needs app.py"
    replay = H.run(_at(tmp_path, "batch"), monkeypatch, script, files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    assert revisions.count("t2") == 1, "no second same-scope round in the child"
    (decl,) = [d for d in record["scope_declarations"] if d["task"] == "t2"]
    assert decl["status"] == "routed" and decl["paths"] == ["app.py"]
    rft = next(r for r in record["review_findings"] if r["fid"] == "RFT2-1")
    assert rft["route"] == "scope" and rft["scope_paths"] == ["app.py"], "kept whole through the merge"
    assert not rft["closed"] and not replay.result.completed


def test_a_materially_new_failure_of_a_declared_case_gets_its_in_scope_gate_fix(tmp_path, monkeypatch):
    """Root review of b9d136e, R2, at the lifecycle boundary: the case was
    declared against the observed run (AssertionError from app.py), then the
    lead's own in-scope test edit makes it fail with a NameError. That is a
    new failure the grant can repair: the gate fix is bought, never skipped
    on the old claim."""
    def revision(call, replay):
        H.write(call, {"README.md": README,
                       "tests/test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == three\n"})
        return ("Documented add and touched the test.\nOUTSIDE SCOPE: RFT1-1 - app.py\n"
                f"OUTSIDE SCOPE: {CASE} - app.py\nCHANGED: [\"README.md\", \"tests/test_app.py\"]")
    replay = H.run(_at(tmp_path, "newfail"), monkeypatch, _script(t1_recheck=_docs_fixed, revision=revision),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    assert record["scope_gate_skips"] == [], "the old claim suppressed nothing"
    assert len(_of(replay, "gate-fix", "t1")) == 1
    assert "NameError" in _of(replay, "gate-fix", "t1")[0].prompt
