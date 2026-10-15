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
        return 'Implemented add.\nCHANGED: ["app.py"]'

    replay = H.run(tmp_path, monkeypatch, Script(
        orchestrator=orchestrator, lead=lead,
        collaborator=lambda c, r: "BLOCKING: negative sums are not shown; at app.py add; example: add(-1, -2)\n"
                                  "R1: MET - app.py:2",
        recheck=lambda c, r: "UNRESOLVED: still open",
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
        return 'Implemented add.\nCHANGED: ["app.py"]'

    (tmp_path / "revision").mkdir()
    revised = H.run(tmp_path / "revision", monkeypatch, Script(
        orchestrator=_plan_once(DECLARED), lead=lead, collaborator=collaborator, recheck=lambda c, r: "RESOLVED",
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
        orchestrator=orchestrator, lead=lead, collaborator=collaborator, recheck=lambda c, r: "RESOLVED",
        auditor=lambda c, r: "R1: MET - ratio.py:4", **{"requirements-review": lambda c, r: "COMPLETE"}),
        files={**FILES, "app.py": FIXED, "ratio.py": "def safe_ratio(a, b):\n    return a / b\n"},
        max_tasks=3 if two else 2, record_complete=False, limits=_ROOT_LIMITS)
    (audit,) = replay.of("auditor")
    section = audit.prompt.split("## Earlier task-review observations")
    if case == "resolved-blocking":
        assert len(section) == 1, "a resolved BLOCKING finding is history, not carried"
    else:
        assert len(section) == 2, "the warning reached the final audit"
        carried = section[1]
        assert _NOTE in carried and "R9" not in carried, "only notes on the requirements judged"
        label = "written about an earlier source" if two else "written about the current source"
        assert f"t1 review, about R1, {label}" in carried
        assert "neither a finding nor an approval" in carried
    assert replay.result.completed, "an observation decides nothing by itself"
