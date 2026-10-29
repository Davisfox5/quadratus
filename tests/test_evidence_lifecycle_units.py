"""The evidence lifecycle batch (Davis, 2026-10-29), at the units: NOT RUN
identity binding and discharge, sufficiency kept apart from lowered
requirement flags, typed command receipts as citations, finish routing
and the framing lens. No controller run; the whole-run controls are in
tests/lifecycle/test_evidence_lifecycle.py."""

import pytest

from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.integration import IntegrationGate
from quadratus.session import Session, SessionConfig, TaskSpec, _gate_identity, _run_passed_cases

CASE = "tests/test_app.py::test_add"
TESTS = {
    "app.py": "def add(a, b):\n    return a + b\n",
    "tests/test_app.py": ("from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                          "class TestApi:\n    def test_history(self):\n        assert add(0, 0) == 0\n\n"
                          "    def test_complete(self):\n        assert add(2, 2) == 4\n"),
    "tests/test_other.py": ("def test_shared():\n    assert True\n\n\nclass TestOther:\n"
                            "    def test_complete(self):\n        assert True\n\n"
                            "    def test_complete_extra(self):\n        assert True\n"),
    "tests/test_more.py": "def test_shared():\n    assert True\n",
}


def _project(tmp_path, files=TESTS):
    root = tmp_path / "project"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _session(tmp_path, *, project=None, gate=None, requirements=("R1",)):
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=str(project) if project else None, integration_gate=gate,
                                           requirements_ledger=True))
    for rid in requirements:
        session.memory.ledger.requirements[rid] = f"{rid} holds"
    return session


def _gate(root):
    return IntegrationGate(["python3", "-m", "pytest", "-q"], cwd=root)


def _acceptance_row(task="t1", proof="P1", cases=(CASE,), oid="O1", rids=("R1",)):
    return dict(oid=oid, task=task, kind="acceptance", proof_id=proof, cases=list(cases), behavior=f"{proof}: add",
                requirements=[[r, 1] for r in rids], gate="", runner="gate", environment="", execution="ran",
                sufficiency="unjudged", judged_by="", judged_binding="", receipts=[], modality="test",
                source_digest=None, policy="review/2", location="tests/test_app.py", scenario=[])


def _entry(session, item, task="t1", after_check=0, rids=("R1",)):
    entry = dict(task=task, role="lead", author="codex:sol", item=item, why="the sandbox denies sockets",
                 requirements=list(rids), after_check=after_check,
                 gate=_gate_identity(session._current_gate()), environment=session._environment_id())
    session._bind_not_run(entry)
    return entry


def _check(session, passed_cases, *, passed=True, output="1 passed in 0.1s", command="python3 -m pytest -q",
           source=None, gate=None, environment=None, cwd=".", skipped=0):
    return dict(passed=passed, command=command, output=output, command_cwd=cwd, cases=None, receipts=[],
                report=dict(passed_cases=list(passed_cases), passed_truncated=False, state="parsed",
                            counts=dict(passed=len(passed_cases), failed=0, errors=0, skipped=skipped)),
                source=source if source is not None else session._source_fingerprint(),
                gate=gate if gate is not None else _gate_identity(session._current_gate()),
                environment=environment if environment is not None else session._environment_id())


# -- 1. NOT RUN items bind to identities the harness can execute -------------------------------------

@pytest.mark.parametrize("item, kind, cases, problem", [
    ("P1 - the socket is denied", "scenario", [CASE], ""),
    ("P1", "scenario", [CASE], ""),
    ("P10", "scenario", [], "P10 is not a scenario this task declares"),
    ("P1 and P2", "scenario", [], "P2 is not a scenario this task declares"),
    ("P2", "scenario", [], "P2 is not a scenario this task declares"),
    (CASE, "case", [CASE], ""),
    ("tests/test_app.py::TestApi::test_history", "case", ["tests/test_app.py::TestApi::test_history"], ""),
    ("tests/test_app.py::test_add[1]", "case", [CASE], ""),
    ("tests/test_app.py::test_absent", "case", [], "tests/test_app.py::test_absent is not defined where it says"),
    ("tests/test_app.py::test_add_more", "case", [], "tests/test_app.py::test_add_more is not defined where it says"),
    ("TestApi.test_history", "case", ["tests/test_app.py::TestApi::test_history"], ""),
    ("TestApi::test_history", "case", ["tests/test_app.py::TestApi::test_history"], ""),
    ("TestApi.test_history and test_add", "case", ["tests/test_app.py::TestApi::test_history", CASE], ""),
    ("test_add", "case", [CASE], ""),
    ("test_history", "case", ["tests/test_app.py::TestApi::test_history"], ""),
    ("test_complete", "case", [], "test_complete is defined in 2 places"),
    ("TestOther.test_complete", "case", ["tests/test_other.py::TestOther::test_complete"], ""),
    ("TestNope.test_complete", "case", [], "TestNope.test_complete is not defined in any test file"),
    ("test_shared", "case", [], "test_shared is defined in 2 places"),
    ("test_absent", "case", [], "test_absent is not defined in any test file"),
    ("tests/test_app.py", "paths", [], ""),
    ("the browser scenario", "none", [], "names no scenario, case or path the harness can identify"),
])
def test_a_not_run_item_binds_to_exactly_what_it_names_or_to_nothing(tmp_path, item, kind, cases, problem):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.evidence_obligations.append(_acceptance_row())
    entry = _entry(session, item)
    assert entry["binding"]["kind"] == kind, entry["binding"]
    assert entry["cases"] == cases and entry["binding"]["cases"] == cases, entry["binding"]
    assert (problem in entry["binding"]["problem"]) if problem else entry["binding"]["problem"] == "", entry["binding"]


def test_a_scenario_with_no_valid_proves_line_binds_nothing(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.evidence_obligations.append(_acceptance_row(cases=()))
    entry = _entry(session, "P1 - no socket")
    assert entry["cases"] == [] and "binds no case yet" in entry["binding"]["problem"]


def test_a_scenario_of_another_task_never_binds(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.evidence_obligations.append(_acceptance_row(task="t2"))
    entry = _entry(session, "P1 - no socket", task="t1")
    assert entry["cases"] == [] and "not a scenario this task declares" in entry["binding"]["problem"]


def test_run_passed_cases_reads_the_report_and_pytest_qualified_names_through_the_cwd():
    run = dict(cwd="pkg", report_cases=["tests/test_app.py::test_add[1-2]"],
               qualified_names={"tests/test_x.py::test_y"}, executed_names={"tests/test_x.py::test_y": 1, "test_z": 1})
    got = _run_passed_cases(run)
    assert got == {"pkg/tests/test_app.py::test_add[1-2]", "pkg/tests/test_app.py::test_add", "pkg/tests/test_x.py::test_y"}
    assert _run_passed_cases(dict(cwd=".", report_cases=[], executed_names={"test_z": 1})) == set(), "a bare name has no file"


def test_a_bound_item_is_discharged_only_by_a_complete_passing_run_of_the_same_identity(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.evidence_obligations.append(_acceptance_row())
    entry = _entry(session, CASE)
    session.unexecuted_acceptance.append(entry)
    session.checks.append(_check(session, [CASE]))
    assert session._discharging_check(entry) == 0
    assert session._standing_not_run() == []
    negatives = {
        "failed run": _check(session, [CASE], passed=False, output="1 failed in 0.1s"),
        "filtered run": _check(session, [CASE], command="python3 -m pytest -q -k add"),
        "a skip": _check(session, [CASE], output="1 passed, 1 skipped in 0.1s", skipped=1),
        "other file's same name": _check(session, ["tests/other/test_app.py::test_add"]),
        "suffix case": _check(session, ["tests/test_app.py::test_add_more"]),
        "wrong source": _check(session, [CASE], source="0" * 24),
        "wrong gate": _check(session, [CASE], gate="other-gate"),
        "wrong environment": _check(session, [CASE], environment="elsewhere"),
        "no case record": _check(session, []),
    }
    for name, check in negatives.items():
        session.checks[:] = [check]
        assert session._discharging_check(entry) is None, name
        assert session._standing_not_run() == [entry], name
    session.checks[:] = [_check(session, [CASE])]
    entry["after_check"] = 1
    assert session._discharging_check(entry) is None, "a run before the report discharges nothing"
    partial = _entry(session, "TestApi.test_history and test_add")
    session.checks[:] = [_check(session, [CASE])]
    assert session._discharging_check(partial) is None, "every bound case must have passed"
    session.checks[:] = [_check(session, [CASE, "tests/test_app.py::TestApi::test_history"])]
    assert session._discharging_check(partial) == 0
    unbound = _entry(session, "the browser scenario")
    session.unexecuted_acceptance.append(unbound)
    assert session._discharging_check(unbound) is None and unbound in session._standing_not_run()


def test_the_prompt_block_and_the_obligation_say_how_a_bound_item_is_discharged(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.evidence_obligations.append(_acceptance_row())
    bound, held = _entry(session, "P1 - no socket"), _entry(session, "test_absent - no socket")
    session.unexecuted_acceptance.extend([bound, held])
    block = session._not_run_block()
    assert f"[bound to {CASE}: discharged only by the harness's gate executing it whole]" in block
    assert "[not bound to any case: test_absent is not defined in any test file]" in block
    row = session._register_obligation(bound, kind="not_run")
    assert row["runner"] == "gate" and list(row["cases"]) == [CASE] and row["kind"] == "not_run"
    row = session._register_obligation(held, kind="not_run")
    assert row["runner"] is None and list(row["cases"]) == []
    gateless = _session(tmp_path / "g", project=root)
    gateless.evidence_obligations.append(_acceptance_row())
    row = gateless._register_obligation(_entry(gateless, "P1 - no socket"), kind="not_run")
    assert row["runner"] is None and list(row["cases"]) == [CASE], "the identity survives a runner-less registration"


def test_the_lead_is_told_the_identity_forms_the_harness_can_discharge(tmp_path):
    """The prompt sentence beside its parser (_bind_not_run): the forms the
    guidance names are exactly the forms the binding reads."""
    import inspect
    text = inspect.getsource(Session._configured_checks_guidance) if hasattr(Session, "_configured_checks_guidance") \
        else inspect.getsource(Session)
    assert "'NOT RUN: P<n> - <why>' for a declared scenario, or " in text
    assert "'NOT RUN: <file>::<Class>::<test> - <why>' for a case; a report naming neither is " in text
    assert "kept as written and nothing can discharge it" in text


# -- 2. sufficiency is read from the auditor's own verdicts and a prior judgment stands ----------------

def _executed_row(session, oid="O1", rids=("R1", "R2")):
    row = _acceptance_row(oid=oid, rids=rids)
    row.update(gate=_gate_identity(session._current_gate()), environment=session._environment_id(),
               source_digest=session._source_fingerprint(), receipts=["check-1"])
    session.evidence_obligations.append(row)
    session._sync_obligation(row)
    return row


def test_implied_sufficiency_reads_the_raw_verdicts_never_a_verdict_lowered_for_another_record(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root), requirements=("R1", "R2"))
    _executed_row(session)
    raw = {"R1": (True, CASE), "R2": (True, CASE)}
    lowered = {"R1": (True, CASE), "R2": (False, "acceptance reported NOT RUN by the covering task")}
    session._judge_obligations("claude:opus", "R1: MET\nR2: MET", dict(lowered), ids=["R1", "R2"], raw=raw)
    row = session.evidence_obligations[0]
    assert row["sufficiency"] == "sufficient" and row["judged_by"] == "claude:opus"
    assert row["sufficiency_source"] == "implied from the auditor's own verdicts on R1 MET, R2 MET"
    session.evidence_obligations[0] = dict(row, sufficiency="unjudged", judged_by="", judged_binding="")
    session._judge_obligations("claude:opus", "", dict(lowered), ids=["R1", "R2"])
    assert session.evidence_obligations[0]["sufficiency"] == "insufficient", "without raw, the lowered flag leaks in"
    session.evidence_obligations[0] = dict(row, sufficiency="unjudged", judged_by="", judged_binding="")
    session._judge_obligations("claude:opus", "", dict(lowered), ids=["R1", "R2"], raw={"R1": (True, CASE), "R2": (False, "x")})
    assert session.evidence_obligations[0]["sufficiency"] == "insufficient"
    assert session.evidence_obligations[0]["sufficiency_source"] == "implied from the auditor's own verdicts on R1 MET, R2 NOT MET"


def test_an_explicit_line_decides_and_a_conflict_leaves_the_row_unjudged_with_no_source(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root), requirements=("R1", "R2"))
    _executed_row(session)
    raw = {"R1": (True, CASE), "R2": (True, CASE)}
    session._judge_obligations("claude:opus", "O1: INSUFFICIENT negative sums are not shown", dict(raw), raw=raw)
    row = session.evidence_obligations[0]
    assert row["sufficiency"] == "insufficient" and row["sufficiency_source"] == "explicit line by claude:opus"
    session._judge_obligations("claude:opus", "O1: SUFFICIENT fine\nO1: INSUFFICIENT not fine", dict(raw), raw=raw)
    row = session.evidence_obligations[0]
    assert row["sufficiency"] == "unjudged" and row["sufficiency_source"] == "" and row["sufficiency_note"]


def test_a_prior_judgment_about_exactly_this_evidence_stands_until_a_seat_speaks_again(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root), requirements=("R1", "R2"))
    _executed_row(session)
    session._judge_obligations("openai:sol", "O1: INSUFFICIENT the zero divisor is not exercised", {}, ids=["R1"])
    before = dict(session.evidence_obligations[0])
    assert before["sufficiency"] == "insufficient" and before["judged_by"] == "openai:sol"
    raw = {"R1": (True, CASE), "R2": (True, CASE)}
    session._judge_obligations("claude:opus", "R1: MET\nR2: MET", dict(raw), ids=["R1", "R2"], raw=raw)
    after = session.evidence_obligations[0]
    assert (after["sufficiency"], after["judged_by"], after["judged_binding"]) == ("insufficient", "openai:sol", before["judged_binding"])
    session._judge_obligations("claude:opus", "O1: SUFFICIENT the case divides by zero on this source", dict(raw), raw=raw)
    after = session.evidence_obligations[0]
    assert (after["sufficiency"], after["judged_by"]) == ("sufficient", "claude:opus"), "an explicit line still decides"
    # New evidence makes the judgment stale, and the implication applies again.
    stale = dict(after, receipts=["check-2"])
    session.evidence_obligations[0] = stale
    session._sync_obligation(stale)
    assert not stale["sufficiency_current"]
    session._judge_obligations("claude:opus", "R1: NOT MET\nR2: MET", {}, ids=["R1", "R2"],
                               raw={"R1": (False, "x"), "R2": (True, CASE)})
    assert session.evidence_obligations[0]["sufficiency"] == "insufficient"
    assert session.evidence_obligations[0]["judged_by"] == "claude:opus"


# -- 3. a command the requirement names is cited by the harness's own receipt ------------------------

@pytest.mark.parametrize("text, argv", [
    ("Run `python3 -m unittest discover -s tests` before declaring completion", ["python3", "-m", "unittest", "discover", "-s", "tests"]),
    ("Run `/usr/bin/python3 -m pytest -q` before declaring completion", ["/usr/bin/python3", "-m", "pytest", "-q"]),
    ("run pytest before declaring completion.", ["pytest"]),
    ("the suite is run with python3 -m unittest discover -s tests before completion", ["python3", "-m", "unittest", "discover", "-s", "tests"]),
    ("`npm test` passes and `pytest` passes", ["npm", "test"]),
    ("a makefile target documents add", None),
    ("add returns the sum", None),
])
def test_the_command_a_requirement_names_is_read_as_argv(tmp_path, text, argv):
    session = _session(tmp_path)
    session.memory.ledger.requirements["R1"] = text
    assert session._command_requirement("R1") == argv


def _receipt_check(session, command, *, status="passed", required=True, source=None, cwd=".", gate=None, environment=None):
    check = _check(session, [CASE], passed=status == "passed", command=command, source=source, gate=gate,
                   environment=environment)
    check["receipts"] = [dict(id="check", status=status, required=required, command=command, cwd=cwd)]
    return check


def test_a_typed_receipt_of_exactly_the_named_command_on_the_current_identity_is_a_citation(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root))
    session.memory.ledger.requirements["R1"] = "Run `python3 -m pytest -q` before declaring completion"
    assert session._command_receipt_citation("R1") == (None, "the harness has no receipt for `python3 -m pytest -q`")
    session.checks.append(_receipt_check(session, "/usr/bin/python3.11 -m pytest -q -p no:cacheprovider --quadratus-report={report}"))
    receipt, problem = session._command_receipt_citation("R1")
    assert problem == "" and receipt["id"] == "check-1"
    assert receipt["command"] == "/usr/bin/python3.11 -m pytest -q -p no:cacheprovider", "the receipt as recorded"
    assert receipt["source"] == session._source_fingerprint()
    negatives = {
        "the harness has no receipt for `python3 -m pytest -q` (its checks ran other commands)":
            _receipt_check(session, "/private/grader/run.py --all"),
        "its run failed": _receipt_check(session, "python3 -m pytest -q", status="failed"),
        "it is an optional command": _receipt_check(session, "python3 -m pytest -q", required=False),
        "it ran on another source": _receipt_check(session, "python3 -m pytest -q", source="0" * 24),
        "it ran under another gate": _receipt_check(session, "python3 -m pytest -q", gate="other"),
        "it ran in another environment": _receipt_check(session, "python3 -m pytest -q", environment="elsewhere"),
        "it ran from another directory, not the project root": _receipt_check(session, "python3 -m pytest -q", cwd="tests"),
    }
    for expected, check in negatives.items():
        session.checks[:] = [check]
        receipt, problem = session._command_receipt_citation("R1")
        assert receipt is None and expected in problem, (expected, problem)
        assert "/private/grader" not in problem and "tests" not in problem.replace("pytest", ""), "never the harness's own command or cwd"
    session.memory.ledger.requirements["R1"] = "add returns the sum"
    assert session._command_receipt_citation("R1") == (None, ""), "a requirement naming no command has no receipt to cite"


# -- 4. finish routing -------------------------------------------------------------------------------

def _obligation(oid, rid, *, runner="gate", execution="missing", reason="", modality="test"):
    return dict(oid=oid, task="t1", kind="acceptance", behavior=f"{oid} behavior", requirements=[[rid, 1]],
                gate="", runner=runner, cases=[], environment="", execution=execution, reason=reason,
                sufficiency="unjudged", judged_by="", judged_binding="", receipts=[], modality=modality)


def test_the_finish_routes_execution_debt_prerequisites_failures_and_findings_apart(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=_gate(root), requirements=("R1", "R2", "R3", "R4", "R5"))
    session.evidence_obligations.append(_acceptance_row())
    debt = _entry(session, CASE, rids=("R1",))
    held = _entry(session, "the browser scenario", rids=("R1",))
    session.unexecuted_acceptance.extend([debt, held])
    session.evidence_obligations.append(_obligation("O2", "R2", runner=None))
    session.evidence_obligations.append(_obligation("O3", "R3", execution="failed", reason="1 failed"))
    session.evidence_obligations.append(_obligation("O4", "R4", execution="ran"))
    session.evidence_obligations.append(_obligation("O5", "R5", execution="stale", reason="executed on another source"))
    routes = {rid: session._evidence_route(rid, "x") for rid in ("R1", "R2", "R3", "R4", "R5")}
    assert routes["R1"]["kind"] == "execution debt" and not routes["R1"]["challengeable"]
    assert f"discharged only by the harness's gate executing {CASE} whole" in routes["R1"]["route"]
    assert "1 NOT RUN item(s) bound to no case" in routes["R1"]["route"] and "never a challenge" in routes["R1"]["route"]
    assert routes["R2"]["kind"] == "prerequisite" and "O2 has no authorized runner" in routes["R2"]["route"]
    assert routes["R3"]["kind"] == "failed execution" and "O3 failed (1 failed)" in routes["R3"]["route"]
    assert routes["R4"] == dict(kind="finding", challengeable=True, route=routes["R4"]["route"])
    assert "a false positive is legitimate" in routes["R4"]["route"]
    assert routes["R5"]["kind"] == "finding" and routes["R5"]["challengeable"], "a stale or missing execution may be the challenge's own proof"
    session.checks.append(_check(session, [CASE]))
    session.unexecuted_acceptance[:] = [debt]
    assert session._evidence_route("R1")["kind"] == "finding", "a discharged item holds nothing"
    session.review_findings.append(dict(fid="RFT1-1", author="claude:opus", role="collaborator", task="t1",
                                        claim="x", text="BLOCKING: x", basis=dict(kind="none", ref=""),
                                        impact="correctness", source_digest="abc", requirement=["R4", 1],
                                        history=[], state="open", closed=False, holds_effects=False,
                                        audit_carried=False))
    route = session._evidence_route("R4")
    assert route["kind"] == "open review finding" and not route["challengeable"] and "RFT1-1" in route["route"]


def test_finish_routing_is_carried_and_exported(tmp_path):
    from quadratus import checkpoint
    assert "finish_routing" in checkpoint._CARRIED and "finish_routing" in checkpoint._ADDED_LATER
    import inspect

    from quadratus import project_run
    assert "'finish_routing'" in inspect.getsource(project_run)


# -- 5. the framing lens, generic by construction ----------------------------------------------------

def test_the_review_lens_covers_framing_and_no_mutation_on_rejected_input_without_the_fixture_answer(tmp_path):
    session = _session(tmp_path)
    prompt = session._collaborator_prompt(TaskSpec(task_id="t1", description="add a CLI"), "draft", "claude:opus")
    assert "Framing is input too" in prompt
    assert "a stream or file that ends early, a partial read, a record cut mid-field" in prompt
    assert "a request or record it rejects must leave no change behind" in prompt
    assert "by reading the state after the refusal, not by reading the error path" in prompt
    for word in ("Content-Length", "JSON prefix", "ApiDependencies", "socket", "sqlite3", "truncated body"):
        assert word not in S._ERROR_BOUNDARY_LENS
