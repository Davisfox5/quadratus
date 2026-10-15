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
import sys
from pathlib import Path

import pytest

from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
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


def _auditor(wrong: bool):
    """Reads the delivered app.py in its copy. ``wrong``: an auditor that
    calls the correct sum NOT MET until it is shown passing executed
    evidence (R18's shape: a false positive about an error path)."""
    def auditor(call, replay):
        source = (Path(call.cwd) / "app.py").read_text()
        if "abs(" in source:
            return "R1: NOT MET - negative sums are wrong; at app.py add; example: add(-1, -2) returns 3"
        if wrong and not ("Result: passed" in call.prompt or "## Executed evidence" in call.prompt):
            return "R1: NOT MET - negative sums are not handled; at app.py add; example: add(-1, -2)"
        return "R1: MET - app.py:2"
    return auditor


def _run(tmp_path, monkeypatch, *, source=FIXED, body="asserts", wrong=True, limits=None, again=False):
    """t1 delivers ``source``; the first DONE meets the audit's NOT MET; the
    orchestrator answers with one evidence request (``again``: the same
    request once more on its receipt), repairs a confirmed defect with one
    task, else DONE."""
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
        return 'Implemented add.\nCHANGED: ["app.py"]'

    return H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - app.py:2",
        auditor=_auditor(wrong), **{"requirements-review": lambda c, r: "COMPLETE"}),
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
    calibration = _run(tmp_path / "calibrate", monkeypatch)
    before = next(i for i, c in enumerate(calibration.calls) if REASSESS in c.prompt)
    limits = RunLimits(max_calls=before, max_reported_tokens=6_000_000, wall_seconds=600,
                       max_concurrent_workers=2)
    replay = _run(tmp_path / "limited", monkeypatch, limits=limits)
    assert not _reassessments(replay) and not replay.result.completed
    assert "call_limit" in (replay.result.error or "") or "RunBudgetExceeded" in (replay.result.error or "")
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
        return ("BLOCKING: negative sums are not shown; at app.py add; example: add(-1, -2) should be -3\n"
                "R1: MET - app.py:2")

    def recheck(call, replay):
        return verdict if "Result: passed" in call.prompt else "UNRESOLVED: negative sums are not shown"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return 'Implemented add.\nCHANGED: ["app.py"]'

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
        assert not replay.result.completed and "open finding" in (replay.result.error or "")


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
        return 'Implemented add.\nCHANGED: ["app.py"]'

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
                    acceptance=["safe_ratio(1, 0) is None"], max_lines=40)
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


def _ratio_run(tmp_path, monkeypatch, *, source=_SAFE, test="zero", keeps=False, limits=_ROOT_LIMITS):
    """max_tasks=1: t1 writes ratio.py, the cap question is answered DONE,
    the final audit finds R1 NOT MET claiming ZeroDivisionError escapes
    (false for _SAFE), and the orchestrator challenges R1 once with a new
    pytest file. The auditor turns MET only when shown a passing test that
    divides by zero (``keeps``: never)."""
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
        return 'Implemented safe_ratio.\nCHANGED: ["ratio.py"]'

    def auditor(call, replay):
        shown = "Result: passed" in call.prompt and "assert safe_ratio(1, 0) is None" in call.prompt
        if shown and not keeps:
            return "R1: MET - ratio.py:4"
        return ("R1: NOT MET - ZeroDivisionError escapes safe_ratio; at ratio.py safe_ratio; "
                "example: safe_ratio(1, 0) raises ZeroDivisionError")

    return H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead, collaborator=lambda c, r: "NO FINDINGS\nR1: MET - ratio.py:4",
        auditor=auditor, **{"requirements-review": lambda c, r: "COMPLETE"}),
        files={**FILES, "app.py": FIXED, "ratio.py": "def safe_ratio(a, b):\n    return a / b\n"},
        max_tasks=1, record_complete=False, limits=limits)


def _writers(replay):
    return [c for c in replay.calls if c.role in ("lead", "revision", "gate-fix", "gate_fix", "trim", "design-fix")]


@pytest.mark.requirements_ledger
def test_a_false_final_audit_finding_is_challenged_after_the_cap_without_another_writer(tmp_path, monkeypatch):
    """Root boundary on 83b658c: seven controller controls stopped
    RequirementsUnmet after max_tasks=1 with 8 of 20 calls used, and no
    challenge was offered after the final audit. Now the orchestrator
    challenges R1 read-only, the harness runs its test on the delivered
    source, the same auditor reassesses once and the run completes, with no
    writer, slot or recovery bought and inside the unchanged 20-call
    allowance."""
    if not _sandboxed():
        pytest.skip("no scratch sandbox here")
    replay = _ratio_run(tmp_path, monkeypatch)
    assert replay.result.completed, replay.result.error
    (record,) = _records(replay)
    assert record["target"] == "R1" and record["receipt"]["status"] == "passed"
    assert record["disposition"].startswith("MET on reassessment by the same auditor")
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
        calibration = _ratio_run(tmp_path / "calibrate", monkeypatch)
        before = next(i for i, c in enumerate(calibration.calls) if REASSESS in c.prompt)
        kw = dict(limits=RunLimits(max_calls=before, max_reported_tokens=2_000_000, wall_seconds=1800,
                                   max_concurrent_workers=2, reserve_tokens_per_call=200_000,
                                   max_tokens_per_call=500_000))
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
