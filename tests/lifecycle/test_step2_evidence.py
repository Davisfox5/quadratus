"""Step 2 evidence loop (2026-10-15, approved on #73): a disputed finding is
tested by the harness, not argued.

The orchestrator writes one new test file and names the finding it tests;
the harness runs it with the project's own discovering check in a
disposable, sandboxed copy of the current source; a failing test confirms
the finding and returns it to the ordinary repair path; a passing test goes
once to the same auditor or reviewer, which reassesses that finding alone.
Whole-controller replays below the vendor CLI (tests.lifecycle.harness).
"""

import json
import re
import sys
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.replies import audit_reply, recheck_reply, requested_obligations
from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, FIXED, T1, Script

_R1 = "add(a, b) returns the sum of two numbers"
DECLARED = "REQUIREMENTS:\nR1: " + _R1 + "\n" + DECL_T1 + "\nCOVERS: R1"
REPAIR = ("KIND: architect complex\nSCOPE: " + json.dumps(T1)
          + "\nRepair add so negative numbers sum correctly.\nCOVERS: R1")
ABS = "def add(a, b):\n    return abs(a) + abs(b)\n"
RECEIPTS = "--- EVIDENCE RECEIPTS ---"
SENT_BACK = "DONE SENT BACK"
REASSESS = "Reassess R1 alone"
EVIDENCE_PATH = "tests/test_evidence_negative.py"

_TESTS = {
    "asserts": "from app import add\n\n\ndef test_negative_sum():\n    assert add(-1, -2) == -3\n",
    "skips": "import pytest\n\n\ndef test_negative_sum():\n    pytest.skip('not decided here')\n",
    "mutates": ("from pathlib import Path\n\n\ndef test_negative_sum():\n"
                "    Path('app.py').write_text('def add(a, b):\\n    return -1\\n')\n"),
}


def _request(target="R1", body="asserts", path=EVIDENCE_PATH):
    return (f"EVIDENCE: {target} - add sums negative numbers\nFILE: {path}\n```python\n"
            f"{_TESTS[body]}```")


def _sandboxed() -> bool:
    import os
    import platform
    return ((platform.system() == "Linux" and os.path.isfile("/usr/bin/bwrap"))
            or (platform.system() == "Darwin" and os.path.exists("/usr/bin/sandbox-exec")))


#: T1's one declared acceptance behavior, the executed evidence obligation
#: the add scenario registers at dispatch (tests.lifecycle.test_lifecycle_matrix.T1).
_ADD_OBLIGATION = "P1: add(1, 2); given nothing stored; expect 3 is returned; in tests/test_app.py"


def _add_reassessment(prompt: str) -> str:
    """The positive reassessment of R1 on the shown negative-sum proof. The
    reassessment judges one requirement and passes the audit's own
    implication no map (Session._reassess), so an executed obligation it
    leaves unjudged keeps the insufficiency its earlier NOT MET implied:
    the auditor says so explicitly for the obligation T1 declared, on its
    own basis (the project check ran add(1, 2) == 3 on this source and the
    proof adds the negative case). Authored for that obligation alone; any
    other obligation in the prompt is left unjudged."""
    judged = {o["oid"]: "SUFFICIENT the project check ran add(1, 2) == 3 on this source and the shown proof "
                        "executed the negative case; together they show the sum"
              for o in requested_obligations(prompt)
              if o["behavior"] == _ADD_OBLIGATION and o["execution"] == "ran"}
    return audit_reply(prompt, {"R1": "MET - app.py:2"}, judged)


def _auditor(wrong: bool, *, reassessment=None):
    """Reads the delivered app.py in its copy. ``wrong``: an auditor that
    calls the correct sum NOT MET until it is shown passing executed
    evidence (R18's shape: a false positive about an error path). The
    positive reassessment is typed (``_add_reassessment``): R1 and the
    executed obligation it covers; ``reassessment`` replaces it for the
    cases that test a reply without that judgment."""
    def auditor(call, replay):
        source = (Path(call.cwd) / "app.py").read_text()
        if "abs(" in source:
            return "R1: NOT MET - negative sums are wrong; at app.py add; example: add(-1, -2) returns 3"
        if REASSESS in call.prompt and "Result: passed" in call.prompt:
            return (reassessment or _add_reassessment)(call.prompt)
        if wrong and not ("Result: passed" in call.prompt or "## Executed evidence" in call.prompt):
            return "R1: NOT MET - negative sums are not handled; at app.py add; example: add(-1, -2)"
        return "R1: MET - app.py:2"
    return auditor


def _run(tmp_path, monkeypatch, *, source=FIXED, body="asserts", wrong=True, limits=None, again=False,
         reassessment=None, blocking=False):
    """t1 delivers ``source``; the first DONE meets the audit's NOT MET; the
    orchestrator answers with one evidence request (``again``: the same
    request once more on its receipt), repairs a confirmed defect with one
    task, else DONE. ``blocking``: each reviewer raises one standing finding
    on the draft and resolves it on the recheck, so the task spends the
    revision and the rechecks its selected route counts (the call
    calibrations need the route's calls actually made)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return DECLARED
        if RECEIPTS in call.prompt:
            served = call.prompt.split(RECEIPTS)[-1]
            if again and "already tested" not in served:
                return _request(body=body)
            confirmed = "Disposition: confirmed" in served
            return REPAIR if confirmed and not any(c.task == "t2" for c in replay.calls) else "DONE"
        asked = sum(RECEIPTS in c.prompt for c in replay.of("orchestrator")[:-1])
        if SENT_BACK in call.prompt and not asked and not any(c.task == "t2" for c in replay.calls):
            return _request(body=body)
        return "DONE"

    def lead(call, replay):
        H.write(call, {"app.py": source if call.task == "t1" else FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    def collaborator(call, replay):
        first = not any(c.role == "revision" and c.task == call.task for c in replay.calls)
        return f"{_HELD}\nR1: MET - app.py:2" if blocking and first else "NO FINDINGS\nR1: MET - app.py:2"

    return H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=collaborator,
        recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED the revision shows negative sums"),
        auditor=_auditor(wrong, reassessment=reassessment), **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=3, record_complete=False, limits=limits)


def _records(replay):
    return H.result_json(replay).get("evidence_requests") or []


def _reassessments(replay):
    return [c for c in replay.of("auditor") if REASSESS in c.prompt]


@pytest.mark.requirements_ledger
def test_a_false_audit_finding_is_tested_and_reassessed_once_by_the_same_auditor(tmp_path, monkeypatch):
    """Old/new control. R18's shape: the auditor found a correct requirement
    NOT MET. Before, the orchestrator could only argue or repeat DONE, and the
    run ended RequirementsUnmet (an EVIDENCE reply was read as a task). Now
    the harness runs the orchestrator's test on the delivered source, it
    passes, and the same auditor reassesses R1 once and finds it met; the
    final audit, shown that executed evidence, completes the run. The first
    audit's NOT MET stays on the record beside the evidence and the
    reassessment, and no command line reaches the planner or the auditor."""
    replay = _run(tmp_path, monkeypatch)
    if not _sandboxed():
        pytest.skip("no scratch sandbox here: the evidence is not run (covered by the environment case)")
    assert replay.result.completed, replay.result.error
    (record,) = _records(replay)
    assert record["target"] == "R1" and record["kind"] == "audit"
    assert record["receipt"]["status"] == "passed"
    assert record["receipt"]["cases"] == [f"{EVIDENCE_PATH}::test_negative_sum: passed"]
    assert record["disposition"].startswith("MET on reassessment by the same auditor")
    assert record["original_disposition"] == "NOT MET" and "NOT MET" in record["finding"]
    audits = (H.result_json(replay).get("requirements") or {}).get("audits") or []
    assert audits[0]["verdicts"]["R1"]["met"] is False, "the original finding is kept"
    auditors = replay.of("auditor")
    assert len(_reassessments(replay)) == 1 and len({c.vendor for c in auditors}) == 1, \
        "one reassessment, by the auditor that made the finding"
    assert "## Executed evidence on this exact source" in auditors[-1].prompt
    assert not (replay.project / EVIDENCE_PATH).exists(), "the evidence never reaches the project"
    added = ([c.prompt.split(RECEIPTS)[-1] for c in replay.of("orchestrator") if RECEIPTS in c.prompt]
             + [c.prompt for c in _reassessments(replay)]
             + [auditors[-1].prompt.split("## Executed evidence on this exact source")[-1]])
    for text in added:
        for leak in (sys.executable, "-m pytest", "--quadratus-report", "quadratus-review-", str(replay.project)):
            assert leak not in text, f"the evidence loop adds no command line or path: {leak}"


@pytest.mark.requirements_ledger
def test_a_true_defect_is_confirmed_by_execution_and_repaired_through_the_ordinary_path(tmp_path, monkeypatch):
    """The test fails on the delivered source: the finding stands without a
    second auditor call, and the orchestrator repairs it with a task that
    COVERS the requirement; the next audit judges the repaired source."""
    replay = _run(tmp_path, monkeypatch, source=ABS, wrong=False)
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    (record,) = _records(replay)
    assert record["receipt"]["status"] == "failed"
    assert record["disposition"].startswith("confirmed: the test failed")
    assert not _reassessments(replay), "a confirmed defect needs no reassessment"
    assert any(c.task == "t2" for c in replay.of("lead")), "repaired by an ordinary task"
    assert replay.result.completed, replay.result.error
    assert (replay.project / "app.py").read_text() == FIXED


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["inconclusive", "environment", "source-mutation"])
def test_evidence_that_did_not_run_leaves_the_finding_held_and_is_never_retried(tmp_path, monkeypatch, case):
    """A skipped test, no sandbox, or a test that rewrites handed source in
    its copy: not run, so there is no new evidence and no reassessment. The
    finding stays NOT MET and the run ends RequirementsUnmet. Asked again on
    the same source, the request is refused as already tested."""
    if case == "environment":
        monkeypatch.setattr("quadratus.runtime._scratch_isolation", lambda directory, private: None)
    body = {"inconclusive": "skips", "environment": "asserts", "source-mutation": "mutates"}[case]
    replay = _run(tmp_path, monkeypatch, body=body, again=True)
    (record,) = _records(replay)
    assert record["receipt"]["status"] == "not run"
    reason = {"inconclusive": "not shown executed and passed", "environment": "no sandbox",
              "source-mutation": "handed file changed"}[case]
    if _sandboxed() or case == "environment":
        assert reason in record["receipt"]["reason"], record
    assert record["disposition"].startswith("held: the evidence did not run")
    assert not _reassessments(replay)
    assert not replay.result.completed and "RequirementsUnmet" in (replay.result.error or "")
    assert any("already tested on this source (E1" in c.prompt for c in replay.of("orchestrator")), \
        "the same finding on the same source is not tested twice"
    assert (replay.project / "app.py").read_text() == FIXED, "the project is never changed"


@pytest.mark.requirements_ledger
def test_a_reassessment_the_budget_cannot_admit_stops_the_run_with_the_request_on_record(tmp_path, monkeypatch):
    """The reassessment is an ordinary reserved call under the unchanged
    allowance: with the call limit reached it is refused, the run stops on
    the budget, the request and its passing receipt stay on the record, and
    the finding is still NOT MET."""
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    # The route's calls are made (``blocking``: one standing finding per
    # reviewer, resolved on the recheck): t1 spends its known three (lead
    # draft, two reviews; the harness record costs no call) and the correction the findings
    # selected (revision, two rechecks), admitted on its own, so the call
    # allowance that admits the route at dispatch (two calls used, two held
    # for the finish) is the one that is spent exactly at the reassessment
    # (root's calibration finding on bc421f8).
    calibration = _run(tmp_path / "calibrate", monkeypatch, blocking=True)
    before = next(i for i, c in enumerate(calibration.calls) if REASSESS in c.prompt)
    roles = [c.role for c in calibration.calls[:before]]
    assert roles.count("revision") == 1 and roles.count("recheck") == 2, roles
    admitted = [a for a in H.result_json(calibration)["plan_admissions"] if a["task"] == "t1" and a["scope"] == "full"]
    assert admitted[0]["stages"] == ["lead draft(editing/write)",
                                    "review by openai:sol(reserve)", "review by grok:default(reserve)"]
    assert before + 1 - admitted[0]["at_attempt"] - 2 >= 3, (admitted, before)
    # The reassessment follows the DONE audit, which has started; the cap's
    # goal question is still held one call (Davis, 2026-10-16), so with one
    # call over the reassessment's position it is the ordinary call refused.
    held = json.loads((Path(calibration.result.run_dir) / "budget.json").read_text())["finish_protection"]
    assert held["started"] == ["audit"] and held["pending"] == ["goal"], held
    limits = RunLimits(max_calls=before + 1, max_reported_tokens=6_000_000, wall_seconds=600,
                       max_concurrent_workers=2)
    replay = _run(tmp_path / "limited", monkeypatch, limits=limits, blocking=True)
    assert [c.role for c in replay.calls[:before]] == roles, "the same calls up to the reassessment's position"
    assert not _reassessments(replay) and not replay.result.completed
    # The reassessment would spend the cap's held goal-question call, so it is
    # refused as FinishReserved, a budget stop (Davis, 2026-10-16).
    assert "FinishReserved" in (replay.result.error or "")
    (record,) = _records(replay)
    assert record["receipt"]["status"] == "passed" and record["disposition"] == "held: reassessment not made"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("verdict", ["RESOLVED", "UNRESOLVED: still not shown for zero"])
def test_a_held_review_finding_is_tested_and_rechecked_once_by_its_own_reviewer(tmp_path, monkeypatch, verdict):
    """Old/new control for reviewer findings. A blocking finding the reviewer
    kept through both fix cycles is held open, and the run used to end with
    open work. Tested by a passing harness run, it goes back once to the
    same reviewer: RESOLVED closes exactly that finding and the run
    completes; UNRESOLVED keeps it, and the run ends with the finding open."""
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return DECLARED
        if n == 2:
            return _request(target="U1")
        if RECEIPTS in call.prompt and "EVIDENCE E2" not in call.prompt:
            return _request(target="U2")
        return "DONE"

    def collaborator(call, replay):
        # A standing finding (basis given): held open through the fix
        # cycles, so the evidence loop may test it as U<n>.
        return f"{_HELD}\nR1: MET - app.py:2"

    def recheck(call, replay):
        # One disposition per expected finding id, as the recheck protocol
        # reads it; the finding stays held through the fix cycles and is
        # judged only on the shown passing proof.
        word, _, why = verdict.partition(": ")
        return recheck_reply(call.prompt, default=(f"{word} {why or 'the executed test shows negative sums'}"
                                                   if "Result: passed" in call.prompt
                                                   else "UNRESOLVED negative sums are not shown"))

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=collaborator, recheck=recheck,
        auditor=lambda c, r: "R1: MET - app.py:2", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=3, record_complete=False)
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    records = _records(replay)
    assert [r["target"] for r in records] == ["U1", "U2"], "one held finding per reviewer"
    assert all(r["kind"] == "review" and r["receipt"]["status"] == "passed" for r in records)
    rechecks = [c for c in replay.of("recheck") if "Result: passed" in c.prompt]
    cli = {"openai": "codex", "grok": "grok", "claude": "claude"}
    assert [c.vendor for c in rechecks] == [cli[r["reviewer"].partition(":")[0]] for r in records], \
        "one reassessment each, by the reviewer that made the finding"
    if verdict == "RESOLVED":
        assert all(r["disposition"] == "resolved on reassessment by the same reviewer" for r in records)
        assert replay.result.completed, replay.result.error
    else:
        assert all(r["disposition"].startswith("confirmed on reassessment") for r in records)
        # The findings stay open on the typed record: the final audit lowers
        # R1 for them by id, and the capped run ends RequirementsUnmet.
        assert not replay.result.completed and "RequirementsUnmet" in (replay.result.error or "")
        result = H.result_json(replay)
        fids = [f["fid"] for f in result["review_findings"]]
        assert len(fids) == 2 and not any(f["closed"] for f in result["review_findings"])
        lowered = result["requirements"]["audits"][-1]["lowered"]
        assert lowered and "open review finding" in lowered[0]["reason"] and all(fid in lowered[0]["reason"]
                                                                                  for fid in fids)


def test_an_evidence_request_is_read_exactly_or_answered_never_run_as_a_task():
    from quadratus.session import _parse_evidence
    ok = _parse_evidence(_request())
    assert ok["target"] == "R1" and ok["path"] == EVIDENCE_PATH and ok["text"] == _TESTS["asserts"]
    assert not ok["error"]
    assert _parse_evidence("Implement add.\nEVIDENCE: R1") is None, "only a reply that starts with it"
    assert _parse_evidence("EVIDENCE: R1 - no file")["error"]
    assert _parse_evidence("EVIDENCE: everything\nFILE: tests/test_x.py\n```\nx\n```")["error"]


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("path,refusal", [
    ("app_evidence.py", "must be a test file"),
    ("../tests/test_out.py", "project-relative"),
    ("tests/test_app.py", "already exists"),
])
def test_an_evidence_file_must_be_a_new_project_test_file(tmp_path, monkeypatch, path, refusal):
    """Never an existing file, a path outside the project or a file the
    runner would not collect; refused in words, nothing runs."""
    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return DECLARED
        if RECEIPTS in call.prompt:
            return "DONE"
        if SENT_BACK in call.prompt and not any(RECEIPTS in c.prompt for c in replay.of("orchestrator")[:-1]):
            return _request(path=path)
        return "DONE"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:2",
        auditor=_auditor(True), **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=3, record_complete=False)
    served = [c.prompt.split(RECEIPTS)[-1] for c in replay.of("orchestrator") if RECEIPTS in c.prompt]
    if refusal == "already exists":
        # A path check passes; the harness refuses the existing file itself.
        records = _records(replay)
        assert records and records[0]["receipt"]["status"] == "not run"
        assert refusal in records[0]["receipt"]["reason"]
    else:
        assert served and "EVIDENCE request not served" in served[0] and refusal in served[0]
        assert not _records(replay)
    assert (replay.project / "tests" / "test_app.py").read_text() == FILES["tests/test_app.py"]
    assert not replay.result.completed


# -- root boundary on 83b658c: the challenge after the final task cap ---------------------

_RATIO_REQ = "safe_ratio(a, b) returns None instead of raising when b is zero"
_SAFE = ("def safe_ratio(a, b):\n    try:\n        return a / b\n"
         "    except ArithmeticError:\n        return None\n")
_UNSAFE = _SAFE.replace("ArithmeticError", "ValueError")
_RATIO_SCOPE = dict(permitted_paths=["ratio.py"], intended_result="safe_ratio is safe",
                    acceptance=["safe_ratio(1, 0) is None"], max_lines=40,
                    proofs=H.proofs_for("R1", "tests/test_ratio_evidence.py", operation="safe_ratio(1, 0)",
                                        state="no state", expect="None is returned, nothing raised"))
_RATIO_DECL = ("REQUIREMENTS:\nR1: " + _RATIO_REQ + "\nKIND: architect complex\nSCOPE: "
               + json.dumps(_RATIO_SCOPE) + "\nImplement safe_ratio in ratio.py.\nCOVERS: R1")
_RATIO_TESTS = {
    "zero": "from ratio import safe_ratio\n\n\ndef test_zero_divisor():\n    assert safe_ratio(1, 0) is None\n",
    "irrelevant": "from ratio import safe_ratio\n\n\ndef test_zero_divisor():\n    assert safe_ratio(4, 2) == 2\n",
    "skips": "import pytest\n\n\ndef test_zero_divisor():\n    pytest.skip('not decided here')\n",
    "mutates": ("from pathlib import Path\n\n\ndef test_zero_divisor():\n"
                "    Path('ratio.py').write_text('def safe_ratio(a, b):\\n    return None\\n')\n"),
}
_CHALLENGE = "may challenge it with executed evidence"
_ROOT_LIMITS = RunLimits(max_calls=20, max_reported_tokens=2_000_000, wall_seconds=1800,
                         max_concurrent_workers=2, reserve_tokens_per_call=200_000,
                         max_tokens_per_call=500_000)
#: The ratio family runs under the default-shaped bound (_ROOT_LIMITS). The
#: fresh end-to-end job on 21b342d showed the old whole-route admission
#: refusing every reviewed task there (a 1,600,000 route against the
#: 1,473,864 left after a 126,136 intake and the 400,000 finish hold);
#: under admission/2 the known stages (the draft at the 500,000 ceiling,
#: two reviews at the 200,000 reserve: 900,000, with a model close-out
#: ahead of later tasks 1,100,000) are paid from what is left and a
#: correction is admitted when a finding selects it. A smaller allowance
#: that cannot pay the known stages still refuses the task before any call
#: (test_an_allowance_that_cannot_pay_the_required_stages_refuses_the_task_
#: before_any_call).
_STARVED_LIMITS = RunLimits(max_calls=20, max_reported_tokens=1_200_000, wall_seconds=1800,
                            max_concurrent_workers=2, reserve_tokens_per_call=200_000,
                            max_tokens_per_call=500_000)
#: The typed shape of a standing review finding (a basis given): held open
#: through the fix cycles and offered to the evidence loop as U<n>; a bare
#: BLOCKING line with no basis is carried to the final audit by id instead.
_HELD = "BLOCKING: negative sums are not shown; at app.py add; example: add(-1, -2) should be -3 | basis: R1 | impact: correctness"
#: The ratio scenario's one declared acceptance behavior (_RATIO_SCOPE).
_RATIO_OBLIGATION = ("P1: safe_ratio(1, 0); given no state; expect None is returned, nothing raised; "
                     "in tests/test_ratio_evidence.py")


def _ratio_reassessment(prompt: str) -> str:
    """The positive reassessment of R1 on a shown proof that executed the
    zero divisor: R1 MET, and the obligation the task declared judged
    sufficient on that basis alone (the proof executed exactly its
    behavior on this source). Only that obligation; the irrelevant proof
    never reaches here (the auditor keeps its finding)."""
    judged = {o["oid"]: (f"SUFFICIENT {o['oid']} (P1) is shown executed: the evidence file "
                         "tests/test_ratio_evidence.py::test_zero_divisor divided by zero on this source and passed, "
                         "which is exactly the scenario's zero-divisor expectation")
              for o in requested_obligations(prompt)
              if o["behavior"] == _RATIO_OBLIGATION and o["execution"] == "ran"}
    # R1 is an error clause: its MET cites the delivered file and the exact
    # case shown executed (the executed-evidence rule), never the file alone.
    return audit_reply(prompt, {"R1": "MET - ratio.py:4, shown by tests/test_ratio_evidence.py::test_zero_divisor"},
                       judged)


def _ratio_run(tmp_path, monkeypatch, *, source=_SAFE, test="zero", keeps=False, limits=_ROOT_LIMITS,
               review="NO FINDINGS\nR1: MET - ratio.py:4", blocking=False):
    """max_tasks=1: t1 writes ratio.py, the cap question is answered DONE,
    the final audit finds R1 NOT MET claiming ZeroDivisionError escapes
    (false for _SAFE), and the orchestrator challenges R1 once with a new
    pytest file. The auditor turns MET only when shown a passing test that
    divides by zero (``keeps``: never). ``blocking``: each reviewer raises
    one standing finding on the draft and resolves it on the recheck, so the
    task spends the revision and the rechecks its selected route counts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    request = ("EVIDENCE: R1 - a zero divisor returns None\nFILE: tests/test_ratio_evidence.py\n```python\n"
               + _RATIO_TESTS[test] + "```")

    def orchestrator(call, replay):
        if len(replay.of("orchestrator")) == 1:
            return _RATIO_DECL
        if _CHALLENGE in call.prompt:
            return "ACCEPT" if RECEIPTS in call.prompt else request
        return "DONE"

    def lead(call, replay):
        H.write(call, {"ratio.py": source})
        # A binding is a claim of where the proof lives, never execution:
        # the case is written by the challenge, run by the harness.
        return 'Implemented safe_ratio.\nPROVES: P1 - tests/test_ratio_evidence.py::test_zero_divisor\nCHANGED: ["ratio.py"]'

    def auditor(call, replay):
        shown = "Result: passed" in call.prompt and "assert safe_ratio(1, 0) is None" in call.prompt
        if shown and not keeps:
            return _ratio_reassessment(call.prompt) if REASSESS in call.prompt else "R1: MET - ratio.py:4"
        return ("R1: NOT MET - ZeroDivisionError escapes safe_ratio; at ratio.py safe_ratio; "
                "example: safe_ratio(1, 0) raises ZeroDivisionError")

    held = "BLOCKING: the zero divisor is not shown; at ratio.py safe_ratio | basis: R1 | impact: correctness"

    def collaborator(call, replay):
        first = not any(c.role == "revision" for c in replay.calls)
        return f"{held}\nR1: MET - ratio.py:4" if blocking and first else review

    return H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=collaborator,
        recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED the revision returns None for zero"),
        auditor=auditor, **{"requirements-review": lambda c, r: "COMPLETE"}),
        files={**FILES, "app.py": FIXED, "ratio.py": "def safe_ratio(a, b):\n    return a / b\n"},
        max_tasks=1, record_complete=False, limits=limits)


def _writers(replay):
    """Writer calls made after the final audit started (none may be): the
    lead's revision answering an UNVERIFIED error-clause review line comes
    before the cap (Davis, 2026-10-16) and is not one of them."""
    roles = ("lead", "revision", "gate-fix", "gate_fix", "trim", "design-fix")
    audit = next((i for i, c in enumerate(replay.calls) if c.role == "auditor"), len(replay.calls))
    first = [c for c in replay.calls if c.role == "lead"][:1]
    return first + [c for c in replay.calls[audit:] if c.role in roles]


@pytest.mark.requirements_ledger
def test_a_false_final_audit_finding_is_challenged_after_the_cap_without_another_writer(tmp_path, monkeypatch):
    """Root boundary on 83b658c: seven controller controls stopped
    RequirementsUnmet after max_tasks=1 with 8 of 20 calls used, and no
    challenge was offered after the final audit. Now the orchestrator
    challenges R1 read-only, the harness runs its test on the delivered
    source, the same auditor reassesses once and the run completes, with no
    writer, slot or recovery bought and inside the unchanged 20-call
    allowance (the default-shaped bound, _ROOT_LIMITS: the positive proof
    under 2,000,000). The served record came in while the final audit was
    current and the acceptance on record consumed it: the terminal's
    acceptance is current, names E1, and the auditor's own packet beside
    it (the production path of Session._consume_evidence)."""
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    replay = _ratio_run(tmp_path, monkeypatch)
    assert replay.result.completed, replay.result.error
    (record,) = _records(replay)
    assert record["target"] == "R1" and record["receipt"]["status"] == "passed"
    assert record["disposition"].startswith("MET on reassessment by the same auditor")
    assert record["audit_current"] is True, "the final audit was current when the challenge came in"
    from quadratus import jobstore
    acceptance = jobstore.read_terminal(Path(replay.result.run_dir))["acceptance"]
    assert acceptance["current"] is True and acceptance["consumed"] == ["E1"]
    assert acceptance["packet_audited"] != acceptance["packet"] and acceptance["requirements"]["R1"]["met"]
    audit = H.result_json(replay)["requirements"]["audits"][-1]
    assert acceptance["packet_audited"] == audit["packet_settled"] and acceptance["binding"] != audit["binding"]
    auditors = replay.of("auditor")
    assert len(auditors) == 2 and len({c.vendor for c in auditors}) == 1, "the audit, then its one reassessment"
    assert [c.task for c in _writers(replay)] == ["t1"], "no writer after the cap"
    assert len(replay.calls) <= 20
    budget = json.loads((Path(replay.result.run_dir) / "budget.json").read_text())
    assert budget["limits"]["max_calls"] == 20 and budget["reserved_attempts"] == len(replay.calls)
    assert (replay.project / "ratio.py").read_text() == _SAFE
    assert not (replay.project / "tests" / "test_ratio_evidence.py").exists()


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("variant", ["true-defect", "irrelevant", "skipped", "mutating", "environment",
                                     "auditor-keeps-finding", "budget"])
def test_a_final_challenge_that_does_not_establish_the_requirement_leaves_it_held(tmp_path, monkeypatch, variant):
    """Each challenge is actually made and its receipt delivered to the
    orchestrator; none of these establishes R1, so the run still stops
    RequirementsUnmet and the project is unchanged."""
    if variant == "environment":
        monkeypatch.setattr("quadratus.runtime._scratch_isolation", lambda directory, private: None)
    elif not _sandboxed():
        pytest.skip("no scratch sandbox here")
    kw = {"true-defect": dict(source=_UNSAFE), "irrelevant": dict(test="irrelevant"),
          "skipped": dict(test="skips"), "mutating": dict(test="mutates"),
          "auditor-keeps-finding": dict(keeps=True)}.get(variant, {})
    if variant == "budget":
        # The route's calls made (``blocking``), so the allowance that admits
        # t1's six-call route at dispatch is spent exactly at the
        # reassessment (see test_a_reassessment_the_budget_cannot_admit...).
        calibration = _ratio_run(tmp_path / "calibrate", monkeypatch, blocking=True)
        before = next(i for i, c in enumerate(calibration.calls) if REASSESS in c.prompt)
        assert [c.role for c in calibration.calls[:before]].count("recheck") == 2
        kw = dict(blocking=True, limits=RunLimits(
            max_calls=before, max_reported_tokens=_ROOT_LIMITS.max_reported_tokens, wall_seconds=1800,
            max_concurrent_workers=2, reserve_tokens_per_call=200_000, max_tokens_per_call=500_000))
    replay = _ratio_run(tmp_path / "run", monkeypatch, **kw)
    source = kw.get("source", _SAFE)
    (record,) = _records(replay)
    status, disposition = record["receipt"]["status"], record["disposition"]
    expected = {"true-defect": ("failed", "confirmed: the test failed"),
                "irrelevant": ("passed", "confirmed on reassessment"),
                "skipped": ("not run", "held: the evidence did not run"),
                "mutating": ("not run", "held: the evidence did not run"),
                "environment": ("not run", "held: the evidence did not run"),
                "auditor-keeps-finding": ("passed", "confirmed on reassessment"),
                "budget": ("passed", "held: reassessment not made")}[variant]
    assert (status, disposition[:len(expected[1])]) == expected, record
    if variant != "budget":
        challenged = [c for c in replay.of("orchestrator") if _CHALLENGE in c.prompt and RECEIPTS in c.prompt]
        assert challenged and "EVIDENCE E1 for R1" in challenged[-1].prompt, "the receipt reached the orchestrator"
    assert not replay.result.completed and "RequirementsUnmet" in (replay.result.error or "")
    assert [c.task for c in _writers(replay)] == ["t1"], "no writer after the cap"
    assert (replay.project / "ratio.py").read_text() == source


# -- root review of 8f68ea0: proof identity and durability ------------------------------

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("drift", ["source-after-run", "gate-after-run", "source-during-reassessment"])
def test_a_passing_proof_is_relied_on_only_while_its_source_and_checks_hold(tmp_path, monkeypatch, drift):
    """Root on 8f68ea0: the proof's source was hashed before execution and
    never compared again, so a change to the project, or a gate replaced by
    the same command with minimum_tests=999, between the real run and the
    reassessment let the same auditor's MET stand on stale evidence. The
    proof is now bound to the source and the effective checks: drift after
    the run spends no reassessment, drift during it discards the verdict,
    and R1 stays NOT MET either way."""
    from quadratus.integration import IntegrationGate
    from quadratus.session import Session
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    real = Session._run_evidence

    def run_evidence(self, path, text):
        receipt = real(self, path, text)
        assert receipt["status"] == "passed", receipt
        if drift == "source-after-run":
            (Path(self.project) / "ratio.py").write_text(_SAFE + "\n# changed after the proof\n")
        elif drift == "gate-after-run":
            gate = self.config.integration_gate
            assert isinstance(gate, IntegrationGate), type(gate)
            self.config.integration_gate = IntegrationGate(gate.command, cwd=gate.cwd, timeout=gate.timeout,
                                                           minimum_tests=999)
        return receipt
    monkeypatch.setattr(Session, "_run_evidence", run_evidence)
    if drift == "source-during-reassessment":
        real_invoke = Session._invoke_model

        def invoke(self, key, prompt, *a, **kw):
            reply = real_invoke(self, key, prompt, *a, **kw)
            if REASSESS in prompt:
                (Path(self.project) / "ratio.py").write_text(_SAFE + "\n# changed during the reassessment\n")
            return reply
        monkeypatch.setattr(Session, "_invoke_model", invoke)
    replay = _ratio_run(tmp_path, monkeypatch)
    (record,) = _records(replay)
    assert record["receipt"]["status"] == "passed"
    assert record["disposition"].startswith("held: the source or checks changed"), record["disposition"]
    assert bool(_reassessments(replay)) == (drift == "source-during-reassessment"), \
        "no reassessment is spent on a proof that no longer holds"
    assert record["execution"]["checks"] and record["execution"]["receipts"], "runner identity kept privately"
    assert not replay.result.completed and "RequirementsUnmet" in (replay.result.error or "")
    status = (H.result_json(replay).get("requirements") or {}).get("status") or {}
    assert status.get("R1", "").startswith("NOT MET")


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("where", ["before-reassessment", "reassessment-started"])
def test_an_interrupted_challenge_resumes_its_unstarted_judgement_once_and_never_replays(
        tmp_path, monkeypatch, where):
    """Root on a93334b: interrupted at the reassessment's entry after a real
    passing proof, the resumed episode left E1 held and stopped
    RequirementsUnmet ("R1 never covered") because the final audit's own
    NOT MET had overwritten coverage before its reuse. The checkpoint now
    records each request's stage. An unstarted judgement is finished once on
    resume by the same auditor, from the retained test, finding, authority
    and bound identity: no second proof, no original audit, goal question,
    planner or lead call, and the job's allowance carried exactly. A
    judgement that may have started is never retried and stays held."""
    from quadratus import checkpoint
    from quadratus.session import Session
    from tests.lifecycle.test_step2_recovery import _budget, _episode
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    runs = []
    real_run = Session._run_evidence

    def run_evidence(self, *a, **kw):
        runs.append(1)
        return real_run(self, *a, **kw)
    monkeypatch.setattr(Session, "_run_evidence", run_evidence)
    fired = []
    if where == "before-reassessment":
        real = Session._reassess

        def reassess(self, *a, **kw):
            if not fired:
                fired.append(True)
                raise KeyboardInterrupt
            return real(self, *a, **kw)
        monkeypatch.setattr(Session, "_reassess", reassess)
    else:
        real_invoke = Session._invoke_model

        def invoke(self, key, prompt, *a, **kw):
            if REASSESS in prompt and not fired:
                fired.append(True)
                raise KeyboardInterrupt
            return real_invoke(self, key, prompt, *a, **kw)
        monkeypatch.setattr(Session, "_invoke_model", invoke)
    replay = _ratio_run(tmp_path, monkeypatch)
    parent = Path(replay.result.run_dir)
    assert "KeyboardInterrupt" in (replay.result.error or "")
    parent_result = (parent / "result.json").read_bytes()
    body = checkpoint.load(parent)
    assert body["resumable"], body.get("uncarried")
    (carried,) = checkpoint._decode(body["state"]["evidence_requests"])
    stage = {"before-reassessment": "awaiting reassessment", "reassessment-started": "reassessment started"}[where]
    assert (carried["id"], carried["receipt"]["status"], carried["stage"]) == ("E1", "passed", stage)
    spent = _budget(parent)
    assert spent["in_flight"] == 0
    before = len(replay.calls)
    second = Path(_episode(replay, resume_from=parent.name, max_tasks=1, limits=_ROOT_LIMITS).run_dir)
    new = replay.calls[before:]
    result = json.loads((second / "result.json").read_text())
    assert len(runs) == 1, "the proof is never run again"
    assert len(replay.of("lead")) == 1
    assert not [c for c in new if c.role in ("orchestrator", "lead")], "no planner, goal question or writer"
    assert (parent / "result.json").read_bytes() == parent_result, "the parent's record is unchanged"
    budget = _budget(second)
    assert budget["carried_from_earlier_episodes"]["calls"] == spent["reserved_attempts"]
    assert budget["reserved_attempts"] == spent["reserved_attempts"] + len(new) <= 20
    (record,) = result["evidence_requests"]
    if where == "before-reassessment":
        assert [c.role for c in new] == ["auditor"] and REASSESS in new[0].prompt, \
            "one reassessment, and no new original audit"
        assert new[0].vendor == replay.of("auditor")[0].vendor, "by the same auditor"
        assert len(replay.of("auditor")) == 2
        assert record["disposition"].startswith("MET on reassessment by the same auditor")
        assert result.get("completed"), result.get("error")
    else:
        assert not [c for c in new if c.role == "auditor"], "a judgement that may have started is not retried"
        assert record["stage"] == "reassessment started" and record["disposition"] == "held: reassessment not made"
        assert not result.get("completed") and "RequirementsUnmet" in (result.get("error") or "")


# -- root review: the reassessment keeps the original authority context ------------------

_RULING = "Q: may safe_ratio return 0.0 for a zero divisor? -- A: no; RULING-4417 requires None"


@pytest.mark.requirements_ledger
def test_an_audit_reassessment_is_bound_by_the_rulings_checks_and_goal_of_the_original(tmp_path, monkeypatch):
    """Root on 8f68ea0: with a real passing proof, the reassessment prompt
    omitted ledger.rulings. It now reads the same authority context as the
    final audit: verbatim goal, the requirement, the redacted check facts
    and configured checks, decisions and operator rulings, NOT RUN records,
    and the rule that a test cannot authorize another reading."""
    from quadratus.session import Session
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    real = Session._audit_requirements

    def audit(self, *a, **kw):
        if _RULING not in self.memory.ledger.rulings:
            self.memory.ledger.rulings.append(_RULING)
        return real(self, *a, **kw)
    monkeypatch.setattr(Session, "_audit_requirements", audit)
    replay = _ratio_run(tmp_path, monkeypatch)
    (judge,) = _reassessments(replay)
    first = replay.of("auditor")[0]
    assert _RULING in first.prompt, "the original audit had the ruling"
    for needed in (_RULING, "## Goal (verbatim)", "Build the feature.", "R1: " + _RATIO_REQ,
                   "## Latest project checks", "## Configured project checks",
                   "A test cannot authorize a different reading"):
        assert needed in judge.prompt, needed
    for leak in (sys.executable, "--quadratus-report", "quadratus-review-"):
        assert leak not in judge.prompt
    assert replay.result.completed, replay.result.error


@pytest.mark.requirements_ledger
def test_a_review_reassessment_is_bound_by_the_original_goal_not_only_the_task_text(tmp_path, monkeypatch):
    """Root on 8f68ea0: the peer reassessment had only the generated task
    description. It now carries the verbatim goal and the task's covered
    requirements as well, so a full-goal condition the task text left out
    still binds the reviewer."""
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    goal = "Build the feature. GOAL-CONDITION-7: add must also accept negative integers."

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        return DECLARED if n == 1 else _request(target="U1") if n == 2 else "DONE"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead,
        collaborator=lambda c, r: f"{_HELD}\nR1: MET - app.py:2",
        recheck=lambda c, r: recheck_reply(c.prompt, default="UNRESOLVED still open"),
        auditor=lambda c, r: "R1: MET - app.py:2", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=3, record_complete=False, goal=goal)
    judges = [c for c in replay.of("recheck") if "Result: passed" in c.prompt]
    assert len(judges) == 1 and _records(replay)[0]["receipt"]["status"] == "passed"
    for needed in ("GOAL-CONDITION-7", "## Goal (verbatim)", "R1: " + _R1, "## Task t1",
                   "A test cannot authorize a different reading"):
        assert needed in judges[0].prompt, needed


# -- R19 (assessed on e2a46a4): the order of authority ------------------------------------

_ORDER = "Authority runs in this order: the original goal and operator rulings"
_WAIVER = "not consistency with existing commands, endpoints or functions"


@pytest.mark.requirements_ledger
def test_every_judge_and_writer_after_the_draft_is_given_the_order_of_authority(tmp_path, monkeypatch):
    """R19's t3 reviewer found the goal's JSON refusal broken for malformed
    new-command IDs, then waived it because the task asked for the type=int
    convention and the old commands behaved that way, and marked the
    requirement MET. The goal block was already labelled as outranking the
    task text; what no prompt said is that a prescribed convention, existing
    behaviour or an older choice cannot excuse a goal contract on the work
    it asks for, or that a requirement is never MET beside a note that it is
    not. Old/new control: on e2a46a4 none of these prompts carries the
    order; now the revision writer, every task reviewer, the final auditor
    and the evidence reassessment do. Severity is not forced and nothing is
    marked MET or NOT MET by the harness."""
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    replay = _ratio_run(tmp_path / "audit", monkeypatch)
    judged = {"collaborator": replay.of("collaborator"), "auditor": replay.of("auditor")}
    assert _reassessments(replay), "the challenge ran"
    for role, calls in judged.items():
        assert calls and all(_ORDER in c.prompt and _WAIVER in c.prompt for c in calls), role

    def collaborator(call, replay):
        first = not any(c.role == "revision" for c in replay.calls)
        return ("BLOCKING: negative sums are not shown; at app.py add; example: add(-1, -2)\nR1: MET - app.py:2"
                if first else "NO FINDINGS\nR1: MET - app.py:2")

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'

    (tmp_path / "revision").mkdir()
    revised = H.run(tmp_path / "revision", monkeypatch, Script(
        orchestrator=_plan_once(DECLARED), lead=lead, collaborator=collaborator,
        recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED the revision shows negative sums"),
        auditor=lambda c, r: "R1: MET - app.py:2", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files=FILES, max_tasks=2, record_complete=False)
    revisions = revised.of("revision")
    assert revisions and all(_ORDER in c.prompt for c in revisions), "the writer after the draft"
    assert not any(_ORDER in c.prompt for c in revised.of("orchestrator")), "planning prompts are unchanged"


def _plan_once(declared):
    def orchestrator(call, replay):
        return declared if len(replay.of("orchestrator")) == 1 else "DONE"
    return orchestrator


# -- R19 (assessed on e2a46a4): the reviewer's waived warning reaches the audit -------------

_NOTE = ("Low severity, medium confidence: safe_ratio(1, 0) raises instead of returning None. This is "
         "about R1, but the task's convention allows it, so I do not count it as blocking.")
_OFF = "Style: the module docstring mentions R9, which no task covers."


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["current", "earlier-source", "resolved-blocking"])
def test_a_non_blocking_review_warning_reaches_the_final_audit_labelled_never_as_a_verdict(
        tmp_path, monkeypatch, case):
    """R19's final auditor prompt (9538 characters) held neither the t3
    reviewer's explicit malformed-ID warning nor its waiver. A task review's
    non-blocking paragraph that names a covered requirement is now carried,
    with its task and whether it was written about the current source, into
    the final audit (and any reassessment) as an observation to reconcile,
    never a finding or approval. A resolved BLOCKING finding and a note about
    a requirement the task does not cover are not carried. No model call is
    added. Old/new: on e2a46a4 the auditor never sees the warning."""
    two = case == "earlier-source"
    second = ("KIND: architect complex\nSCOPE: " + json.dumps(_RATIO_SCOPE)
              + "\nTidy safe_ratio's docstring.\nCOVERS: R1")

    def orchestrator(call, replay):
        n = len(replay.of("orchestrator"))
        if n == 1:
            return _RATIO_DECL
        if n == 2 and two:
            return second
        return "ACCEPT" if _CHALLENGE in call.prompt else "DONE"

    def lead(call, replay):
        H.write(call, {"ratio.py": _SAFE if call.task == "t1" else '"""Ratios."""\n' + _SAFE})
        return 'Implemented safe_ratio.\nCHANGED: ["ratio.py"]'

    def collaborator(call, replay):
        if call.task != "t1":
            return "NO FINDINGS\nR1: MET - ratio.py:4"
        if case == "resolved-blocking":
            first = not any(c.role == "revision" for c in replay.calls)
            return (f"BLOCKING: {_NOTE}\nR1: MET - ratio.py:4" if first else "NO FINDINGS\nR1: MET - ratio.py:4")
        return f"{_NOTE}\n\n{_OFF}\n\nR1: MET - ratio.py:4\n{'REVIEW: COMPLETE'}"

    tmp_path.mkdir(parents=True, exist_ok=True)
    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=collaborator,
        recheck=lambda c, r: recheck_reply(c.prompt, default="RESOLVED the revision returns None for zero"),
        auditor=lambda c, r: "R1: MET - ratio.py:4", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files={**FILES, "app.py": FIXED, "ratio.py": "def safe_ratio(a, b):\n    return a / b\n"},
        max_tasks=3 if two else 2, record_complete=False, limits=_ROOT_LIMITS)
    (audit,) = replay.of("auditor")
    section = audit.prompt.split("## Earlier task-review observations")
    if case == "resolved-blocking":
        # The harness's own hold note on the unexecuted MET may be carried;
        # the resolved BLOCKING text never is.
        assert _NOTE not in "".join(section[1:]), "a resolved BLOCKING finding is history, not carried"
    else:
        assert len(section) == 2, "the warning reached the final audit"
        carried = section[1]
        assert _NOTE in carried and "R9" not in carried, "only notes on the requirements judged"
        label = "written about an earlier source" if two else "written about the current source"
        assert f"t1 review, about R1, {label}" in carried
        assert "neither a finding nor an approval" in carried
    # An observation decides nothing by itself: R1 is an error clause the
    # auditor cites by file only, so the executed-evidence rule (not the
    # observation) holds it NOT MET (Davis, 2026-10-16).
    lowered = H.result_json(replay)["requirements"]["audits"][-1]["lowered"]
    assert [entry["requirement"] for entry in lowered] == ["R1"] and "error or refusal" in lowered[0]["reason"]
    assert not replay.result.completed



# -- root review of 728c0fc: carried observations are whole, and never blocking prose -------

_LATE = ("WARNING-TAIL-7731: for R1, safe_ratio(1, 0) raises ZeroDivisionError instead of returning None; "
         + "the zero-divisor branch is never reached on this path. " * 12)
_CONTEXT = "\n".join(f"Valid input works for case {k} of R1." for k in range(22))


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("shape", ["late-in-a-long-paragraph", "oldest-of-21-paragraphs"])
def test_a_carried_review_warning_reaches_the_audit_whole(tmp_path, monkeypatch, shape):
    """Root on 728c0fc: the note was cut to 600 characters and the list to
    the last 20, so a warning after 22 benign lines, or the oldest of 21
    paragraphs, never reached the auditor, which could then mark R1 MET.
    Notes are now kept and carried whole; the review volume is already
    bounded by the job's calls and tokens."""
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    if shape == "late-in-a-long-paragraph":
        review = f"{_CONTEXT}\n{_LATE}\n\nR1: MET - ratio.py:4\nREVIEW: COMPLETE"
    else:
        benign = "\n\n".join(f"R1 note {k}: valid input works." for k in range(20))
        review = f"{_LATE}\n\n{benign}\n\nR1: MET - ratio.py:4\nREVIEW: COMPLETE"
    assert len(_LATE) > 600
    replay = _ratio_run(tmp_path, monkeypatch, review=review)
    audit = replay.of("auditor")[0].prompt
    carried = audit.split("## Earlier task-review observations")[1]
    assert _LATE.strip() in carried, "the whole warning, not a prefix"
    if shape == "oldest-of-21-paragraphs":
        assert all(f"R1 note {k}: valid input works." in carried for k in range(20))
    assert any(_LATE.strip() in n["text"] for n in H.result_json(replay)["review_notes"]), "stored whole"


def test_a_blocking_finding_and_its_continuation_are_never_carried_as_an_observation():
    """Root on 728c0fc: only the BLOCKING line was dropped, so its
    continuation was carried as 'not marked blocking and never rechecked'.
    The finding and the rest of its paragraph belong to the blocking and
    recheck route; a separate non-blocking paragraph is still kept."""
    from types import SimpleNamespace

    from quadratus.session import Session
    session = Session.__new__(Session)
    session.project, session.review_notes = None, []
    note = ("BLOCKING: invalid input leaks an exception.\nFor R1 this violates the required error object.\n\n"
            "Separately, R1's README example omits the zero case.\n\nR1: NOT MET - app.py\nREVIEW: COMPLETE")
    session._record_review_notes(SimpleNamespace(task_id="t1"), "openai:x", note, ["R1"])
    assert [n["text"] for n in session.review_notes] == ["Separately, R1's README example omits the zero case."]
    session.review_notes = []
    session._record_review_notes(SimpleNamespace(task_id="t1"), "openai:x",
                                 "- **BLOCKING:** leaks.\nFor R1 the error object is missing.", ["R1"])
    assert session.review_notes == []


@pytest.mark.requirements_ledger
def test_a_carried_warning_survives_a_pause_and_reaches_the_resumed_reassessment(tmp_path, monkeypatch):
    """Positive resume control: the warning is checkpointed with the closed
    task, and the reassessment finished by the resumed episode reads it."""
    from quadratus.session import Session
    from tests.lifecycle.test_step2_recovery import _episode
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    real = Session._reassess
    fired = []

    def reassess(self, *a, **kw):
        if not fired:
            fired.append(True)
            raise KeyboardInterrupt
        return real(self, *a, **kw)
    monkeypatch.setattr(Session, "_reassess", reassess)
    review = f"{_LATE}\n\nR1: MET - ratio.py:4\nREVIEW: COMPLETE"
    replay = _ratio_run(tmp_path, monkeypatch, review=review)
    before = len(replay.calls)
    _episode(replay, resume_from=Path(replay.result.run_dir).name, max_tasks=1, limits=_ROOT_LIMITS)
    (judge,) = [c for c in replay.calls[before:] if REASSESS in c.prompt]
    assert _LATE.strip() in judge.prompt


# -- root review of 225367f: a condition stated without an id ------------------------------

_UNNUMBERED = ("PEER-OBSERVED-UNCAUGHT-INPUT: the required error-object condition is broken in app.py "
               "parse_quantity; parse_quantity(\"bad\") raises ValueError. Legacy convention is the reviewer "
               "reason not to block.")


def test_a_review_condition_stated_without_an_id_is_kept_as_unassigned():
    """Root on 225367f: _FINDING_SHAPE lets a finding state 'the requirement
    or condition it breaks', and a paragraph stating only the condition was
    dropped because it named no id. It is now kept against the task's covered
    requirements, labelled as naming none, so the audit can reconcile it
    without the harness claiming which requirement it concerns. A paragraph
    naming only an uncovered id stays out."""
    from types import SimpleNamespace

    from quadratus.session import Session
    session = Session.__new__(Session)
    session.project, session.review_notes = None, []
    note = f"{_UNNUMBERED}\n\nR99 is out of scope here.\n\nR1: MET - app.py\nREVIEW: COMPLETE"
    session._record_review_notes(SimpleNamespace(task_id="t1"), "openai:x", note, ["R1"])
    assert [(n["text"], n["requirements"], n["association"]) for n in session.review_notes] == [
        (_UNNUMBERED, ["R1"], "unassigned")]
    block = session._review_notes_block({"R1": "text"})
    assert "naming no requirement (the task covered R1; which one, if any, it concerns is uncertain)" in block
    assert _UNNUMBERED in block and "R99" not in block


@pytest.mark.requirements_ledger
def test_an_unnumbered_review_warning_reaches_the_final_audit(tmp_path, monkeypatch):
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    replay = _ratio_run(tmp_path, monkeypatch, review=f"{_UNNUMBERED}\n\nR1: MET - ratio.py:4\nREVIEW: COMPLETE")
    audit = replay.of("auditor")[0].prompt
    assert _UNNUMBERED in audit and "naming no requirement (the task covered R1" in audit
    assert len(replay.of("auditor")) == 2, "no added call: the audit and the challenge's one reassessment"


# -- root's Mac receipt on bc421f8: the protocol of a positive reassessment, and the bound ----

@pytest.mark.requirements_ledger
@pytest.mark.parametrize("shape", ["missing", "insufficient", "conflicting"])
def test_a_positive_reassessment_without_a_sufficiency_judgment_keeps_the_requirement_held(tmp_path, monkeypatch,
                                                                                            shape):
    """Root's Mac receipt on bc421f8: the proof executed and passed, the
    auditor reassessed R1 MET, and R1 stayed held with 'O1: sufficiency
    insufficient', because the reassessment passes the audit's implication
    no map (Session._reassess) and the scripted auditor judged no
    obligation. That is the protocol, kept: a positive line about one
    requirement never overwrites the insufficiency an earlier independent
    NOT MET implied. Missing, explicitly insufficient and contradictory
    sufficiency lines all keep R1 held, the record says why, and the run
    ends RequirementsUnmet; the proof and the reassessment stay on record."""
    def reply(prompt):
        judged = {o["oid"]: {"insufficient": "INSUFFICIENT the proof shows one case only",
                             "conflicting": "SUFFICIENT shown\n{oid}: INSUFFICIENT not shown"}.get(shape, "")
                  for o in requested_obligations(prompt) if o["behavior"] == _ADD_OBLIGATION}
        judged = {oid: text.replace("{oid}", oid) for oid, text in judged.items() if text}
        return audit_reply(prompt, {"R1": "MET - app.py:2"}, judged)
    replay = _run(tmp_path, monkeypatch, reassessment=reply)
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    (record,) = _records(replay)
    assert record["receipt"]["status"] == "passed" and record["reassessment"].startswith("R1: MET")
    assert record["disposition"].startswith("held: acceptance predicate:"), record["disposition"]
    assert "sufficiency" in record["disposition"]
    obligation = next(o for o in H.result_json(replay)["evidence_obligations"] if o["behavior"] == _ADD_OBLIGATION)
    assert obligation["sufficiency"] == {"conflicting": "unjudged"}.get(shape, "insufficient")
    assert len(_reassessments(replay)) == 1
    assert not replay.result.completed and "RequirementsUnmet" in (replay.result.error or "")


@pytest.mark.requirements_ledger
def test_an_allowance_that_cannot_pay_the_required_stages_refuses_the_task_before_any_call(tmp_path, monkeypatch):
    """The family's bound control (root's Mac receipt on bc421f8, restaged
    under admission/2): an allowance that cannot pay t1's known stages
    (the draft at the 500,000 ceiling and two reviews at the 200,000
    reserve: 900,000, against the 800,000 left of 1,200,000 after the held
    finish and the intake) refuses the task before any task call: no
    effect is launched, no evidence or audit record exists, the shortfall
    is named, and the result is incomplete and says a route was forfeited.
    No review is dropped to fit, and the default bound's positive stands
    in test_a_false_final_audit_finding_is_challenged_after_the_cap_
    without_another_writer."""
    replay = _ratio_run(tmp_path, monkeypatch, limits=_STARVED_LIMITS)
    result = H.result_json(replay)
    assert not replay.result.completed
    assert {c.role for c in replay.calls} <= {"orchestrator", "requirements-review"}, [c.role for c in replay.calls]
    assert _records(replay) == [] and not replay.of("auditor") and not replay.of("lead")
    (refusal,) = [r for r in result["finish_refusals"] if r["task"] == "t1"]
    detail = refusal["detail"]
    assert "lead draft" in detail and detail.count("review by") == 2, detail
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{2,}", detail)]
    assert 900_000 in numbers, detail
    assert [n["task"] for n in result["not_started"]] == ["t1"]
    assert "t1's route could not be paid" in (replay.result.error or "") and "forfeited" in (replay.result.error or "")
    assert (replay.project / "ratio.py").read_text() == "def safe_ratio(a, b):\n    return a / b\n"
