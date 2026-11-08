"""Cycle09 terminal (root's assessment, PR73 6722947726), three engine causes:

A. The final audit held R17 and R24 NOT MET and the planner named the repair
   ``RESOLVES: R17 (audit), R24 (audit)``. RESOLVES reads finding ids only,
   so every token was an unknown finding and the sendbacks spent the
   correction allowance until RunStalled. Now a requirement id the audit
   alone holds NOT MET is read as the COVERS route the DONE refusal names
   (recorded, never silent), an annotation is dropped, and any other
   requirement id is refused with its typed route.
B. "Run python3 -m unittest discover -s tests successfully before declaring
   completion" parsed argv ending in "successfully". The unquoted reader now
   takes the runner and its own options and operands and stops at prose.
C. One free-text NOT RUN line about tests/test_http.py was re-filed by every
   later task and held each task's unrelated requirements, and DONE refused
   on coverage above the declaration route, so done_declarations stayed
   empty. A repeat about files the task did not change is kept with the item
   it repeats; declarations are offered before the coverage gate.

Real Session and controller with scripted providers and the project's own
pytest gate, and units on a hand-set record."""
import json
from types import SimpleNamespace

import pytest

from tests.lifecycle import harness as H
from tests.lifecycle.replies import judging_scenarios
from tests.lifecycle.test_done_declarations import _answering, _auditor, _clean, _declaring
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED
from tests.lifecycle.test_proof_binding import CASE, PROOF_R1, SHOWN, _at, _plan, _proofs, _scope
from tests.lifecycle.test_reachable_evidence import _item, _lead
from tests.lifecycle.test_workflow_repair import SKIP_TESTS, Seen, _script

pytestmark = pytest.mark.requirements_ledger


def _session(tmp_path, requirements):
    from quadratus.artifacts import ArtifactStore
    from quadratus.session import Session, SessionConfig
    session = Session("goal", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(project=tmp_path, allow_writes=True, requirements_ledger=True))
    session.memory.ledger.requirements.update(requirements)
    return session


# -- B. the bare command is the runner and its own words ------------------------------------------

R22 = "python3 -m unittest discover -s tests passes on the delivered source."
R25 = "Run python3 -m unittest discover -s tests successfully before declaring completion."
UNITTEST = ["python3", "-m", "unittest", "discover", "-s", "tests"]


@pytest.mark.parametrize("text, argv, pure", [
    (R22, UNITTEST, True),
    (R25, UNITTEST, True),
    ("Run `python3 -m unittest discover -s tests` successfully before declaring completion.", UNITTEST, True),
    ("Run python3 -m unittest discover -s tests -p test_*.py successfully.",
     UNITTEST + ["-p", "test_*.py"], True),
    ("pytest --maxfail=1 -q passes cleanly.", ["pytest", "--maxfail=1", "-q"], True),
    ("Run pytest -q tests/test_app.py successfully before completion.", ["pytest", "-q", "tests/test_app.py"], True),
    ("npm test passes on the delivered source.", ["npm", "test"], True),
    # A selection, an application command or a mixed claim stays a scenario.
    ("python -m pytest -k refunds passes successfully.", None, False),
    ("python app.py pytest prints the runner name.", None, False),
    ("python -m app serve starts successfully.", None, False),
    ("pytest -q passes and refunds are atomic when the bank rejects a transfer.", None, False),
    ("npm test passes reliably under load.", None, False),
])
def test_a_bare_command_is_read_structurally_and_its_identity_kept(tmp_path, text, argv, pure):
    session = _session(tmp_path, {"R1": text})
    assert session._test_command_requirement("R1") is pure, (text, session._command_requirement("R1"))
    if argv is not None:
        assert list(session._command_requirement("R1")) == argv


# -- A. RESOLVES is read by typed ids ---------------------------------------------------------------

def _spec(text, task="t7"):
    from quadratus.session import TaskSpec
    return TaskSpec(task_id=task, description=text)


def _audited(tmp_path):
    session = _session(tmp_path, {"R1": "add returns the sum", "R17": "GET /tickets/<id> returns 404 when unknown",
                                  "R24": "the export is byte-identical", "R30": "README documents add"})
    status = session.memory.ledger.requirement_status
    status.update({"R1": "met (audited)", "R17": "NOT MET: the handler returns 500 for an unknown id",
                   "R24": "NOT MET: one trailing newline differs", "R30": "covered (t2)"})
    session.findings.append(dict(id="F3", status="open", requirements=["R24"], target=None))
    return session


def test_an_audit_not_met_requirement_in_resolves_is_the_covers_route_and_is_recorded(tmp_path):
    from quadratus.session import _read_covers, _read_resolves
    session = _audited(tmp_path)
    spec = session._normalize_resolves(_spec("Fix the unknown-id handler.\nRESOLVES: R17 (audit)"))
    covers, spec = _read_covers(spec)
    resolves, spec = _read_resolves(spec)
    assert covers == ["R17"] and resolves is None, spec.description
    assert session._resolves_problem(resolves, covers, spec) == ""
    (note,) = session.resolves_normalized
    assert note == dict(task="t7", written="R17 (audit)", moved_to_covers=["R17"], annotations=["(audit)"], kept=[])
    # An existing COVERS keeps its ids and gains the moved one once.
    spec = session._normalize_resolves(_spec("Fix it.\nCOVERS: R1, R17\nRESOLVES: R17, R17 (audit)"))
    assert _read_covers(spec)[0] == ["R1", "R17"] and _read_resolves(spec)[0] is None


@pytest.mark.parametrize("line, refused", [
    ("RESOLVES: R24 (audit)", "R24 is held by open finding(s) F3: name those in RESOLVES and R24 in COVERS"),
    ("RESOLVES: R1", "R1 is a requirement (met (audited)), not a finding: name it in COVERS"),
    ("RESOLVES: R30", "R30 is a requirement (covered (t2)), not a finding: name it in COVERS"),
    ("RESOLVES: R99", "R99 is not a listed requirement"),
    ("RESOLVES: F9", "RESOLVES names findings that do not exist: F9"),
    ("RESOLVES: (audit)", "RESOLVES names no finding"),
    ("RESOLVES: R17 audit", "RESOLVES names findings that do not exist: audit"),
])
def test_a_requirement_id_never_impersonates_a_finding(tmp_path, line, refused):
    """Malformed, unknown, closed, finding-held and not-NOT-MET references
    are refused with their typed route; only the moved id leaves the line."""
    from quadratus.session import _read_covers, _read_resolves
    session = _audited(tmp_path)
    spec = session._normalize_resolves(_spec(f"Repair.\n{line}"))
    covers, spec = _read_covers(spec)
    resolves, spec = _read_resolves(spec)
    problem = session._resolves_problem(resolves, covers, spec)
    assert refused in problem, problem


def test_the_done_refusal_names_the_typed_route():
    import inspect

    from quadratus.session import Session
    source = inspect.getsource(Session._requirements_satisfied)
    assert "RESOLVES names only " in source and "finding ids, F<n> or RFT<task>-<n>, never a requirement" in source


def _repair_run(tmp_path, monkeypatch, name, *, second, line="RESOLVES: R1 (audit)"):
    """t1 delivers add; the audit holds R1 NOT MET; the planner names the
    repair with a requirement id in RESOLVES; the same auditor judges again."""
    repair_scope = _scope(proofs=_proofs("R1"))
    repair = (f"KIND: backend simple\nSCOPE: {json.dumps(repair_scope)}\nRepair add for negatives in app.py.\n"
              f"{line}")
    audits = []

    def orchestrator(call, replay):
        calls = len(replay.of("orchestrator"))
        if calls == 1:
            return _plan(_scope(proofs=_proofs("R1")))
        if calls == 2 or any(c.task == "t2" for c in replay.of("lead")):
            return "DONE"
        return repair

    def verdict(call, replay):
        audits.append(call)
        if len(audits) == 1:
            return "R1: NOT MET - add drops negative operands; example: add(-1, -2) returns 3"
        return second

    def lead(call, replay):
        body = FIXED if call.task == "t1" else "def add(a, b):\n    # negatives too\n    return a + b\n"
        H.write(call, {"app.py": body})
        return f'Implemented add.\nPROVES: P1 - {CASE}\nCHANGED: ["app.py"]'
    script = _script(Seen(), orchestrator=orchestrator, lead=lead,
                     auditor=judging_scenarios(verdict, PROOF_R1, basis=SHOWN))
    return H.run(_at(tmp_path, name), monkeypatch, script, files=FILES, max_tasks=2, record_complete=False), audits


def test_a_final_audit_repair_named_by_requirement_id_is_admitted_repaired_and_judged_again(tmp_path, monkeypatch):
    """On 743bbdc this was sent back as unknown findings 'R1' and '(audit)'
    until the correction allowance stopped the run with no repair call."""
    replay, audits = _repair_run(tmp_path, monkeypatch, "repaired", second=f"R1: MET - {CASE}")
    record = H.result_json(replay)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"], "the repair was admitted and dispatched"
    (note,) = record["resolves_normalized"]
    assert note["task"] == "t2" and note["moved_to_covers"] == ["R1"] and note["annotations"] == ["(audit)"]
    assert not any("TASK SENT BACK" in c.prompt for c in replay.of("orchestrator"))
    assert len(audits) == 2 and record["requirements"]["status"]["R1"].startswith("met")
    assert replay.result.completed, replay.result.error


def test_an_adverse_second_audit_keeps_the_requirement_not_met(tmp_path, monkeypatch):
    replay, audits = _repair_run(tmp_path, monkeypatch, "adverse",
                                 second="R1: NOT MET - add(-1, -2) still returns 3 on the repaired source")
    record = H.result_json(replay)
    assert [c.task for c in replay.of("lead")] == ["t1", "t2"] and len(audits) >= 2
    assert record["requirements"]["status"]["R1"].startswith("NOT MET")
    assert not replay.result.completed


def test_an_unlisted_requirement_in_resolves_is_sent_back_with_its_typed_route(tmp_path, monkeypatch):
    """The typed route is named, and no repair call is spent on a line the
    record cannot read."""
    replay, _ = _repair_run(tmp_path, monkeypatch, "unknown", second=f"R1: MET - {CASE}",
                            line="COVERS: R1\nRESOLVES: R9 (audit)")
    sent = [c.prompt for c in replay.of("orchestrator") if "TASK SENT BACK" in c.prompt]
    assert sent and all("R9 is not a listed requirement" in p for p in sent)
    assert [c.task for c in replay.of("lead")] == ["t1"]
    assert not replay.result.completed


# -- C1. only the same report on the same source is inherited ---------------------------------------

HTTP = "NOT RUN: tests/test_http.py live HTTP acceptance cases/steps - no server can be started in my copy"


def _reporting(tmp_path, report=HTTP):
    """t1 (R1) reports the HTTP item on the project as it is; the next call
    is t2's (R2). A real project, so the source fingerprint is measured."""
    project = tmp_path / "p"
    (project / "ticket_app").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "ticket_app" / "api.py").write_text("def patch():\n    return 200\n")
    (project / "tests" / "test_http.py").write_text("def test_live():\n    pass\n")
    session = _session(project, {"R1": "GET /tickets returns 200", "R2": "PATCH conflicts return 409"})
    session._active_spec = SimpleNamespace(task_id="t1")
    session._current_covers = ["R1"]
    session._record_not_run("openai:sol", "lead", report)
    (first,) = session.unexecuted_acceptance
    session._active_spec = SimpleNamespace(task_id="t2")
    session._current_covers = ["R2"]
    session._task_changed_files = lambda spec: []
    return session, first, project


def _blockers(session):
    session.checks.append(dict(source=session._source_fingerprint(), passed=True, results=[]))
    return session._deterministic_blockers()


def test_root_control_1_the_same_report_on_the_same_source_is_linked_to_its_owner(tmp_path):
    session, first, _ = _reporting(tmp_path)
    obligations = len(session.evidence_obligations)
    session._record_not_run("grok:worker", "lead", HTTP)
    assert session.unexecuted_acceptance == [first] and len(session.evidence_obligations) == obligations
    (repeat,) = session.inherited_not_run
    assert repeat["task"] == "t2" and repeat["owner"] == "t1" and repeat["of"] == first["oid"]
    assert first["repeated_by"] == [dict(task="t2", role="lead", after_check=0)]
    assert set(_blockers(session)) == {"R1"}, "the owner's hold stands; nothing new is invented"


def _own(session, first):
    assert session.inherited_not_run == [] and "repeated_by" not in first
    assert len(session.unexecuted_acceptance) == 2 and session.unexecuted_acceptance[-1]["task"] == "t2"
    assert set(_blockers(session)) == {"R1", "R2"}, "the later report keeps its own requirement's hold"


def test_root_control_2_a_different_scenario_about_the_same_file_is_its_own_report(tmp_path):
    """Root's texts: the same single path, two different scenarios."""
    session, first, _ = _reporting(tmp_path, "NOT RUN: tests/test_http.py live HTTP acceptance - no server")
    session._record_not_run("grok:worker", "lead", "NOT RUN: tests/test_http.py concurrent PATCH conflict scenario"
                                                     " - no server")
    _own(session, first)


def test_root_control_3_the_same_text_after_the_application_changed_is_a_fresh_report(tmp_path):
    session, first, project = _reporting(tmp_path)
    (project / "ticket_app" / "api.py").write_text("def patch():\n    return 409\n")
    session._task_changed_files = lambda spec: ["ticket_app/api.py"]
    session._record_not_run("grok:worker", "lead", HTTP)
    _own(session, first)


def test_a_changed_source_is_fresh_even_when_the_tasks_own_changes_are_not_measured(tmp_path):
    """Per-task changed-file data unavailable: the source fingerprint decides."""
    session, first, project = _reporting(tmp_path)
    (project / "ticket_app" / "api.py").write_text("def patch():\n    return 409\n")
    session._record_not_run("grok:worker", "lead", HTTP)
    _own(session, first)


def test_measured_changes_keep_the_report_even_where_the_fingerprint_came_back_equal(tmp_path):
    session, first, _ = _reporting(tmp_path)
    session._task_changed_files = lambda spec: ["ticket_app/api.py"]
    session._record_not_run("grok:worker", "lead", HTTP)
    _own(session, first)


@pytest.mark.parametrize("change", ["gate", "environment", "claim", "unknown source", "no path", "case"])
def test_a_different_gate_environment_claim_case_or_unknown_identity_keeps_its_own_debt(tmp_path, change):
    session, first, project = _reporting(tmp_path)
    reply = HTTP
    if change == "gate":
        session._current_gate = lambda: dict(commands=["python -m pytest -q tests/test_other.py"])
    elif change == "environment":
        session._environment_id = lambda: "another-environment"
    elif change == "claim":
        reply = HTTP + " | for: R2"
    elif change == "unknown source":
        session._source_fingerprint = lambda: None
    elif change == "no path":
        first["item"] = "the live HTTP acceptance steps"
        reply = "NOT RUN: the live HTTP acceptance steps - no server"
    elif change == "case":
        reply = "NOT RUN: tests/test_http.py::test_live - no server"
    session._record_not_run("grok:worker", "lead", reply)
    assert session.inherited_not_run == [] and len(session.unexecuted_acceptance) == 2, change


# -- C2. declarations are offered before the coverage gate --------------------------------------------

TWO = ("R1: add(a, b) returns the sum\nR2: README documents add")
PLAN_TWO = _plan(_scope(proofs=_proofs("R1")), requirements=TWO).replace("KIND: backend simple", "KIND: backend complex")


def _then(*plans):
    replies = iter(plans)
    return lambda call, replay: next(replies, "DONE")


@pytest.mark.parametrize("cap", [2, 1], ids=["done", "cap"])
def test_binding_only_debt_is_declared_even_while_another_requirement_is_uncovered(tmp_path, monkeypatch, cap):
    """On 743bbdc DONE and the cap's final check were refused on R2's
    coverage above the declaration route: done_declarations stayed empty and
    t1's item stayed held."""
    script = _script(Seen(), collaborator=_clean, orchestrator=_then(PLAN_TWO), lead=_lead(), auditor=_auditor())
    script.overrides["proof-binding"] = _declaring()
    replay = H.run(_at(tmp_path, f"early{cap}"), monkeypatch, script, files=FILES, max_tasks=cap,
                   record_complete=False)
    record = H.result_json(replay)
    oid = _item(replay)["oid"]
    decl = record["done_declarations"][0]
    assert decl["owner"] == "t1" and decl["oids"] == [oid] and decl["settled"] == [oid]
    assert len(replay.of("proof-binding")) == 1, "asked once for the job, not on every DONE"
    assert "not covered by any finished task: R2" in (replay.result.error or "") + "".join(
        c.prompt for c in replay.of("orchestrator"))
    assert [c.task for c in replay.of("lead")] == ["t1"], "no editing task was spent on binding-only debt"
    assert not record["requirements"]["status"].get("R2", "").startswith("met"), "no MET without coverage"
    assert not replay.result.completed


def test_mixed_editing_and_unbound_debt_asks_no_declaration_and_meets_nothing(tmp_path, monkeypatch):
    script = _script(Seen(), collaborator=_clean, orchestrator=_then(PLAN_TWO), lead=_lead(tests=SKIP_TESTS),
                     auditor=_answering)
    script.overrides["proof-binding"] = _declaring()
    replay = H.run(_at(tmp_path, "mixed"), monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    record = H.result_json(replay)
    assert record["done_declarations"] == [] and replay.of("proof-binding") == []
    status = record["requirements"]["status"]
    assert not status.get("R1", "").startswith("met") and not status.get("R2", "").startswith("met")
    assert not replay.result.completed
