"""The completion workflow batch (Davis, 2026-10-30), at the units, after
evidence-authority-02 ended FindingsOpen with every contract behaviour
delivered: recovery accounting that charges a real recovery once and never
a first coverage task resolving findings on closed work; continuation and
resolution ownership declared against the record and a legal closure route
checked before any call; one finding id across the typed record, the held
entries, the legacy texts and a parallel child's deferrals, closed
atomically by the designated judge; unassigned requirements named at a
stop; the NOT RUN discharge display written from the one discharge
decision, and a documented ``python -m unittest <module>`` command bound to
exactly that command. No controller run here; the whole-run controls are
in tests/lifecycle/test_completion_workflow.py."""

import inspect
import os
import shutil
import sys

import pytest

from quadratus import judgment
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.integration import IntegrationGate
from quadratus.outcome import TaskOutcome
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, SurveyConfig, TaskSpec
from tests.test_evidence_lifecycle_units import _check, _entry, _project
from tests.test_finding_identity_units import _audit, _review

UNITTEST_FILES = {
    "app.py": "def add(a, b):\n    return a + b\n",
    "tests/__init__.py": "",
    "tests/test_app.py": ("import unittest\n\nfrom app import add\n\n\nclass AddTests(unittest.TestCase):\n"
                          "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
                          "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"),
}
MODULE_CASES = ["tests/test_app.py::AddTests::test_add", "tests/test_app.py::AddTests::test_zero"]
COMMAND = "python3 -m unittest tests.test_app -v"
#: The invocation identity ``python3`` resolves to here, as the engine resolves it.
PYTHON3 = S._resolved_interpreter("python3")
EXACT = f"{sys.executable} -m unittest tests.test_app -v"
EXECUTABLE = S._resolved_interpreter(sys.executable)


def _venv(root):
    """A real virtual environment whose ``bin/python`` is a link to this
    interpreter (no pip, no provider): the shape root's control used."""
    import venv
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    return root


def _session(tmp_path, *, project=None, gate=None, survey=None, requirements=("R1", "R2"), replies=("DONE",)):
    it = iter(replies)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: next(it),
                      config=SessionConfig(project=str(project) if project else None, integration_gate=gate,
                                           requirements_ledger=True, survey=survey))
    for rid in requirements:
        session.memory.ledger.requirements[rid] = f"{rid} holds"
    return session


def _outcome(task, *, covers=("R1",), continues="", capped=False, failed=False, closed_as="closed"):
    outcome = TaskOutcome(task_id=task, intent="implementation", lead="x", covers=list(covers), continues=continues)
    outcome.closed_as = closed_as
    if capped:
        outcome.note("cap", "stopped at the lead turn limit (8 turns)")
    if failed:
        outcome.note("failed", "scope: changed paths outside its declared scope")
    return outcome


def _capped(session, task="t1", *, covers=("R1",), changed=("app.py",)):
    """A capped task as the record holds one: its outcome's cap fact, the
    legacy partial set and the turn-limited record naming its files."""
    session.task_outcomes.append(_outcome(task, covers=covers, capped=True, closed_as="stopped:turn_limited"))
    session._partial_tasks.add(task)
    session.turn_limited_records[task] = dict(description="work", changed=list(changed), turns=8)


def _spec(task="t2", paths=("app.py",)):
    return TaskSpec(task_id=task, description="work", scope=TaskScope(permitted_paths=list(paths)))


# -- 1. recovery accounting: a real recovery is charged once; ordinary coverage never ----------

def test_a_resolves_on_closed_work_is_ordinary_coverage_and_a_continuation_of_owed_work_is_a_recovery(tmp_path):
    session = _session(tmp_path, survey=SurveyConfig(recovery_tasks=2))
    session.task_outcomes.append(_outcome("t1"))
    _capped(session, "t2", covers=("R2",))
    session.review_findings.extend([_review("RFT1-1", "claude:opus", "t1"), _review("RFT2-1", "openai:sol", "t2")])
    ordinary = session._recovery_decision(_spec("t3"), None, ["RFT1-1"])
    assert not ordinary["recovery"] and ordinary["recovers"] == [] and ordinary["owed"] == ["t2"]
    assert ordinary["why"].startswith("resolves findings on closed work (t1: RFT1-1): ordinary coverage, not charged")
    recovery = session._recovery_decision(_spec("t3"), "t2", [])
    assert recovery["recovery"] and recovery["recovers"] == ["t2"] and not recovery["charged"]
    by_finding = session._recovery_decision(_spec("t3"), None, ["RFT2-1"])
    assert by_finding["recovery"] and by_finding["recovers"] == ["t2"], "a finding raised on owed work is its recovery"
    mixed = session._recovery_decision(_spec("t3"), "t2", ["RFT1-1", "RFT2-1"])
    assert mixed["recovery"] and mixed["recovers"] == ["t2"], "charged once whatever the lines name"
    plain = session._recovery_decision(_spec("t3"), None, None)
    assert not plain["recovery"] and plain["why"] == "ordinary coverage"


def test_an_audit_finding_on_a_failed_task_is_a_recovery_and_one_on_closed_work_is_not(tmp_path):
    session = _session(tmp_path, survey=SurveyConfig(recovery_tasks=1))
    session.task_outcomes.append(_outcome("t1"))
    session.task_outcomes.append(_outcome("t2", covers=("R2",), failed=True, closed_as="failed"))
    session._partial_tasks.add("t2")
    session.findings.extend([_audit("F1", task="t1"), _audit("F2", task="t2")])
    assert not session._recovery_decision(_spec("t3"), None, ["F1"])["recovery"]
    assert session._recovery_decision(_spec("t3"), None, ["F2"])["recovers"] == ["t2"]


def test_the_dispatch_charges_only_a_real_recovery_and_records_every_decision():
    source = inspect.getsource(Session._run_tasks)
    assert 'decision = self._recovery_decision(spec, continues, resolves)' in source
    assert 'if decision["recovery"]:' in source and 'self.survey["recovery_used"] += 1' in source
    assert 'self.survey.setdefault("charges", []).append(decision)' in source
    assert 'if self.config.survey is not None and (continues or resolves):' not in source, \
        "the old rule charged any RESOLVES"
    assert "recovery_used" not in inspect.getsource(Session._run_batch), \
        "a batch refuses CONTINUES and RESOLVES at intake; nothing bypasses the serial charge"


# -- 2. ownership: CONTINUES is validated against the record; overlap never takes over ---------

def test_continues_must_name_a_dispatched_task_whose_work_is_still_owed(tmp_path):
    session = _session(tmp_path)
    session.task_outcomes.append(_outcome("t1"))
    _capped(session, "t2")
    session.task_outcomes.append(_outcome("t3", covers=("R2",), continues="t4"))
    _capped(session, "t4", covers=("R2",))
    session._partial_tasks.discard("t4")
    session.task_outcomes[-1].recover("cap")
    assert session._continues_problem(None) == ""
    assert session._continues_problem("t2") == ""
    assert session._continues_problem("t9") == "CONTINUES names t9, a task this run has no record of."
    assert session._continues_problem("t1").startswith("CONTINUES names t1, whose work is not owed (it closed)")
    assert session._continues_problem("t4").startswith("CONTINUES names t4, whose work is not owed (t3 took it over)")
    assert "new work does not continue it" in session._continues_problem("t1")


def test_a_task_over_owed_requirements_or_files_without_continues_is_sent_back_naming_the_route(tmp_path):
    session = _session(tmp_path, survey=SurveyConfig(recovery_tasks=2))
    _capped(session, "t1", covers=("R1",), changed=("app.py", "tests/test_app.py"))
    session.review_findings.append(_review("RFT1-1", "claude:opus", "t1"))
    route, problem = session._closure_route_problem(_spec("t2", paths=("README.md",)), None, None, ["R1"])
    assert route == "refuse" and problem.startswith("t2 overlaps work still owed by t1 (requirements R1)")
    assert "The harness never infers a takeover" in problem and "'CONTINUES: <task id>'" in problem
    assert "2 of 2 left" in problem
    route, problem = session._closure_route_problem(_spec("t2", paths=("app.py",)), None, None, ["R1"])
    assert route == "refuse" and "t1 (requirements R1)" in problem and "files" not in problem
    assert session._closure_route_problem(_spec("t2", paths=("README.md",)), None, None, ["R2"]) == ("", "")
    assert session._closure_route_problem(_spec("t2", paths=("app.py",)), None, None, ["R2"]) == ("", ""), \
        "first coverage of another requirement in the same file is ordinary work (root review of d48014d)"
    assert session._closure_route_problem(_spec("t2", paths=("app.py",)), "t1", None, ["R1"]) == ("", "")
    assert session._closure_route_problem(_spec("t2", paths=("app.py",)), None, ["RFT1-1"], ["R1"]) == ("", ""), \
        "a RESOLVES of the owed task's finding takes it over explicitly"


def test_with_the_allowance_spent_an_overlap_stops_the_run_naming_the_owed_work_before_any_call(tmp_path):
    session = _session(tmp_path, survey=SurveyConfig(recovery_tasks=1))
    session.survey["recovery_used"] = 1
    _capped(session, "t1", covers=("R1",), changed=("app.py",))
    session.review_findings.append(_review("RFT1-1", "claude:opus", "t1"))
    route, problem = session._closure_route_problem(_spec("t2"), None, None, ["R1"])
    assert route == "stop"
    assert problem.startswith("SurveyAllowanceSpent: the recovery allowance of 1 task(s) is spent and t2 overlaps "
                              "work still owed by t1 (requirements R1)")
    assert "Owed work: t1: covers R1; files app.py; open findings RFT1-1" in problem
    assert problem.endswith("Work preserved.")
    assert session._closure_route_problem(_spec("t2", paths=("README.md",)), None, None, ["R2"]) == ("", ""), \
        "work that does not cover the owed requirement still runs on a survey"
    assert session._closure_route_problem(_spec("t2", paths=("app.py",)), None, None, ["R2"]) == ("", ""), \
        "and so does first coverage in the same file; t1 stays owed"
    plain = _session(tmp_path / "plain")
    _capped(plain, "t1")
    route, problem = plain._closure_route_problem(_spec("t2"), None, None, ["R1"])
    assert route == "refuse" and "charged to the recovery allowance)" in problem, \
        "without a survey allowance there is nothing to spend: the overlap is sent back, never a stop"


def test_the_owed_report_and_the_planner_note_name_the_route_and_the_charging_rule(tmp_path):
    session = _session(tmp_path, survey=SurveyConfig(recovery_tasks=1))
    _capped(session, "t1", covers=("R1",), changed=("app.py",))
    from types import SimpleNamespace
    session.history.append(SimpleNamespace(task_id="t1", outcome="turn_limited"))
    session.review_findings.append(_review("RFT1-1", "claude:opus", "t1"))
    assert session._owed_report() == "t1: covers R1; files app.py; open findings RFT1-1"
    note = session._unfinished_state_note()
    assert "still owed: t1; a task that takes one over says CONTINUES with its id" in note
    assert "Owed work: t1: covers R1; files app.py; open findings RFT1-1" in note
    assert "the harness never infers a takeover" in note
    assert "a RESOLVES that names only findings on closed work is ordinary coverage and is not charged" in note
    assert "a task over its requirements stops the run before any call" not in note
    session.survey["recovery_used"] = 1
    spent = session._unfinished_state_note()
    assert "Recovery tasks (CONTINUES or RESOLVES) used: 1 of 1; none is left, and naming one stops the run" in spent
    assert "a task over its requirements stops the run before any call, serial or in a batch" in spent
    text = session._review_resolves_note([_review("RFT1-1", "claude:opus", "t1")])
    assert "a recovery and is charged to the recovery allowance, once with any CONTINUES" in text
    assert "naming only findings on closed work is ordinary coverage and is not charged" in text


def test_the_dispatch_validates_ownership_and_the_closure_route_before_any_call():
    source = inspect.getsource(Session._run_tasks)
    assert "problem = self._continues_problem(continues)" in source
    assert "route, problem = self._closure_route_problem(spec, continues, resolves, covers)" in source
    head = source.index("self._continues_problem(continues)")
    assert head < source.index("summary = self.run_task(spec)"), "validated before the task's first call"
    batch = inspect.getsource(Session._run_batch)
    assert "route, problem = self._closure_route_problem(spec, None, None, covers)" in batch
    assert batch.index("_closure_route_problem") < batch.index("pool.submit(child.run_task"), \
        "a batch block is admitted before any child's writer call"


# -- 3. one finding id: deferrals carry it, closes are atomic, a rechecked deferral is debt ------

def test_a_deferral_carries_its_finding_id_and_is_held_out_of_the_child_s_rechecks(tmp_path):
    from tests.test_parallel_tasks import Orchestrated
    from tests.test_parallel_tasks import _session as parallel_session
    session, _ = parallel_session(tmp_path, Orchestrated(["DONE"]))
    session._batch_siblings = [("t2", ["b.py"], "implement sibling function")]
    spec = TaskSpec(task_id="t1", description="implement a.py")
    session._record_deferred(spec, "openai:sol", "DEFERRED: t2 - sibling function must reject invalid input")
    record = [r for r in session.review_findings if r["task"] == "t1"][0]
    entry = [e for e in session.unresolved_reviews if e.get("deferred")][0]
    assert entry["finding_id"] == record["fid"] and S._finding_id(entry["text"]) == record["fid"]
    assert session.open_findings == [entry["text"]]
    assert entry["text"].startswith(f"{record['fid']}: Task t1: deferred by openai:sol to parallel task t2")
    assert record["held"].startswith("deferred to parallel task t2: rechecked by openai:sol on the merged source")
    assert not session._disputable(), "unrechecked, a deferral is nobody's to test yet"
    entry["deferred_rechecked"] = True
    assert [d["identity"] for d in session._disputable().values()], "rechecked and kept, it is held review debt"
    source = inspect.getsource(Session._recheck_blocking)
    assert 'held = [r for r in mine if r.get("held")]' in source, "a held record is not re-asked in the child"


def test_a_designated_close_updates_the_typed_record_the_held_entry_and_the_legacy_text_at_once(tmp_path):
    session = _session(tmp_path)
    spec = TaskSpec(task_id="t1", description="work")
    outcome = _outcome("t1")
    session.task_outcomes.append(outcome)
    session._outcome = outcome
    session.review_findings.append(_review("RFT1-1", "claude:opus", "t1"))
    text = "RFT1-1: UNRESOLVED (left open at the cap): RFT1-1 claim"
    session._open_finding("unverified", text)
    session.unresolved_reviews.append(dict(task="t1", peer="claude:opus", finding="BLOCKING: RFT1-1 claim",
                                           text=text, finding_id="RFT1-1"))
    def read(peer, line):
        # The binding is taken fresh before each reading, as the engine does.
        binding = session._judgment_binding(spec, peer, "recheck")
        session._apply_review_reading(spec, peer, "recheck", judgment.recheck_verdicts(line, ["RFT1-1"]), binding)
    read("claude:opus", "RFT1-1: UNRESOLVED still missing")
    assert session.open_findings == [text] and not session.review_findings[0]["closed"]
    read("openai:sol", "RFT1-1: RESOLVED the revision adds it")
    assert session.open_findings == [text] and not session.review_findings[0]["closed"], \
        "another seat's verdict closes nothing: the designated judge decides"
    read("claude:opus", "RFT1-1: RESOLVED the revision adds it")
    assert session.review_findings[0]["closed"]
    assert session.open_findings == [], "the legacy text closed in the same step"
    assert all(not (f.kind == "unverified" and f.active) for f in outcome.facts), "and the fact is history"
    assert session._review_debt_hold() == "" and not session.open_findings


def test_the_atomic_close_is_in_the_reading_and_not_only_in_the_post_merge_loop():
    source = inspect.getsource(Session._apply_review_reading)
    assert 'self._close_review_finding_by_id(fid, f"{now[\'state\']} by {peer} on {role}")' in source


# -- 4. unassigned requirements are named at a stop --------------------------------------------

def test_open_work_names_the_requirements_no_task_covered(tmp_path):
    session = _session(tmp_path, requirements=("R1", "R2", "R3"))
    session.task_outcomes.append(_outcome("t1"))
    session.memory.ledger.requirement_status["R1"] = "covered by t1"
    _capped(session, "t2", covers=("R2",))
    reasons = session._open_work()
    assert "capped or failed task(s) t2 not continued to completion" in reasons
    assert "requirements no task has covered: R2, R3" in reasons
    session.memory.ledger.requirement_status["R2"] = "met"
    session.memory.ledger.requirement_status["R3"] = "covered by t3"
    assert not any(r.startswith("requirements no task") for r in session._open_work())


# -- 5. the NOT RUN discharge display and the unittest module command --------------------------

@pytest.mark.parametrize("item, parsed", [
    ("python3 -m unittest tests.test_app -v", (["python3", "-m", "unittest", "tests.test_app", "-v"], ["tests.test_app"])),
    ("python -m unittest tests.test_app tests.test_other", (["python", "-m", "unittest", "tests.test_app", "tests.test_other"],
                                                            ["tests.test_app", "tests.test_other"])),
    ("/opt/venv/bin/python3.11 -m unittest -b tests.test_app", (["/opt/venv/bin/python3.11", "-m", "unittest", "-b", "tests.test_app"],
                                                              ["tests.test_app"])),
    ("python3 -m unittest discover -s tests", None),
    ("python3 -m unittest tests/test_app.py", None),
    ("python3 -m unittest -k add tests.test_app", None),
    ("python3 -m unittest", None),
    ("pytest tests/test_app.py", None),
    ("sh -c 'python3 -m unittest tests.test_app'", None),
    ("python3 -m unittest tests.test_app 'unbalanced", None),
])
def test_only_a_plain_unittest_module_command_is_read_as_one(item, parsed):
    assert S._unittest_module_command(item) == parsed


def test_the_same_python_command_needs_the_same_arguments_and_the_same_resolved_interpreter():
    binding = dict(command=COMMAND, interpreter="/real/python3.11")
    run = dict(command="/opt/venv/bin/python -m unittest tests.test_app -v --quadratus-report=/tmp/r.json",
               interpreter="/real/python3.11")
    assert S._same_python_command(run, binding), "the report token is left out; the resolved interpreter decides"
    assert not S._same_python_command(dict(run, interpreter="/other/python3.11"), binding)
    assert not S._same_python_command(dict(run, interpreter=None), binding), "an unresolved interpreter matches nothing"
    assert not S._same_python_command(run, dict(binding, interpreter=None))
    assert not S._same_python_command(dict(run, command="python3 -m unittest tests.test_app"), binding), "-v is part of it"
    assert not S._same_python_command(dict(run, command="python3 -m pytest tests/test_app.py"), binding)
    assert not S._same_python_command(dict(run, command="sh -c 'python3 -m unittest tests.test_app -v'"), binding)


def test_an_interpreter_is_resolved_as_the_shell_would_and_never_by_its_basename(tmp_path, monkeypatch):
    assert S._resolved_interpreter(sys.executable) == EXECUTABLE
    assert EXECUTABLE.startswith(os.path.realpath(sys.executable) + " env=")
    assert S._resolved_interpreter("python3") == PYTHON3
    assert S._resolved_interpreter("/nonexistent/location/python") is None
    assert S._resolved_interpreter("pytest") is None and S._resolved_interpreter("") is None
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert S._resolved_interpreter("python3") is None, "an alias nothing resolves is no interpreter"
    monkeypatch.setattr(shutil, "which", lambda name: sys.executable)
    assert S._resolved_interpreter("python3.99") == EXECUTABLE, "an alias matches by what it resolves to"
    assert S._command_interpreter("/nonexistent/location/python -m unittest tests.test_app -v") is None
    assert S._command_interpreter(f"{sys.executable} -m unittest x") == EXECUTABLE


def test_two_virtual_environments_on_one_base_executable_are_two_identities(tmp_path):
    """Root's P2 on 405f856: a second venv's bin/python resolves by real
    path to the same base executable as the configured one, but its
    sys.prefix and packages differ; a command declared there is not the
    command the configured gate ran. The identity keeps the invoked
    environment, read from the invoked path's own pyvenv.cfg."""
    first, second = _venv(tmp_path / "first"), _venv(tmp_path / "second")
    one, two = S._resolved_interpreter(str(first / "bin" / "python")), S._resolved_interpreter(str(second / "bin" / "python"))
    assert os.path.realpath(first / "bin" / "python") == os.path.realpath(second / "bin" / "python"), \
        "the links converge on one base executable"
    assert one and two and one != two
    assert one.endswith(f" env={os.path.realpath(first)}") and two.endswith(f" env={os.path.realpath(second)}")
    assert one.split(" env=")[0] == two.split(" env=")[0] == os.path.realpath(sys.executable)
    assert S._resolved_interpreter(str(first / "bin" / "python3")) == one, "an alias inside the same environment"
    assert S._resolved_interpreter(sys.executable) != one, "the base installation is not either venv"
    base = S._resolved_interpreter(sys.executable)
    assert base.endswith(" env=base") or base.endswith(f" env={os.path.realpath(sys.prefix)}")
    binding = dict(command="python -m unittest tests.test_app -v", interpreter=one)
    assert S._same_python_command(dict(command=f"{first}/bin/python3 -m unittest tests.test_app -v", interpreter=one), binding)
    assert not S._same_python_command(dict(command=f"{second}/bin/python -m unittest tests.test_app -v", interpreter=two), binding)
    assert not S._same_python_command(dict(command=f"{sys.executable} -m unittest tests.test_app -v", interpreter=base), binding)


def test_a_unittest_module_item_binds_to_the_module_s_cases_under_the_command_route(tmp_path):
    root = _project(tmp_path, UNITTEST_FILES)
    session = _session(tmp_path, project=root, gate=IntegrationGate(["python3", "-m", "pytest", "-q"], cwd=root))
    entry = _entry(session, COMMAND)
    assert entry["binding"]["kind"] == "command" and entry["binding"]["problem"] == ""
    assert entry["binding"]["command"] == COMMAND and entry["binding"]["files"] == ["tests/test_app.py"]
    assert entry["binding"]["interpreter"] == PYTHON3 and entry["cases"] == MODULE_CASES
    ghost = _entry(session, "/nonexistent/location/python -m unittest tests.test_app -v")
    assert ghost["binding"]["kind"] == "command" and ghost["cases"] == [] and ghost["binding"]["interpreter"] is None
    assert ghost["binding"]["problem"].startswith("interpreter /nonexistent/location/python cannot be resolved")
    missing = _entry(session, "python3 -m unittest tests.test_missing -v")
    assert missing["binding"]["kind"] == "command" and missing["cases"] == []
    assert missing["binding"]["problem"] == "tests.test_missing is not a test module file in the project"
    inside = _entry(session, "python3 -m unittest tests.test_app.AddTests -v")
    assert inside["cases"] == [] and "is not a test module file" in inside["binding"]["problem"], \
        "a selection inside a module is not the module and binds nothing"


def test_the_command_route_is_discharged_only_by_exactly_that_command_on_the_same_interpreter(tmp_path):
    root = _project(tmp_path, UNITTEST_FILES)
    session = _session(tmp_path, project=root, gate=IntegrationGate(["python3", "-m", "pytest", "-q"], cwd=root))
    entry = _entry(session, COMMAND)
    session.unexecuted_acceptance.append(entry)
    row = session._register_obligation(entry, kind="not_run")
    assert row["runner"] is None, "the gate runs pytest, not this command: a prerequisite"

    def ran(cases, command, interpreter=PYTHON3, **over):
        return dict(_check(session, cases, command=command, **over), interpreter=interpreter)
    session.checks.append(ran(MODULE_CASES, "python3 -m pytest -q"))
    assert session._discharge_record(entry) is None, "the same cases under pytest are not this command's execution"
    session.checks.append(ran(MODULE_CASES[:1], "/opt/venv/bin/python -m unittest tests.test_app -v"))
    assert session._discharge_record(entry) is None, "one case short"
    session.checks.append(ran(MODULE_CASES, "python -m unittest tests.test_app -v", cwd="tests"))
    assert session._discharge_record(entry) is None, "not from the project root"
    session.checks.append(ran(MODULE_CASES, "/opt/venv/bin/python -m unittest tests.test_app -v", interpreter="/opt/venv/bin/python3.12"))
    assert session._discharge_record(entry) is None, "another resolved interpreter, whatever the basename"
    session.checks.append(ran(MODULE_CASES, "python3 -m unittest tests.test_app -v", interpreter=None))
    assert session._discharge_record(entry) is None, "an interpreter the run could not resolve"
    session.checks.append(ran(MODULE_CASES, f"{sys.executable} -m unittest tests.test_app -v --quadratus-report=/tmp/r.json"))
    assert session._discharge_record(entry) == dict(index=5, run="check", cases=MODULE_CASES), \
        "the same arguments on the interpreter python3 resolves to here"
    session._refresh_obligations()
    assert entry["discharge_display"] == "bound/1" and entry["discharged_at"] == 5
    assert entry["discharged_run"] == "check" and entry["discharged_cases"] == MODULE_CASES
    exact = _session(tmp_path / "exact", project=root,
                     gate=IntegrationGate([sys.executable, "-m", "unittest", "tests.test_app", "-v"], cwd=root))
    fresh = _entry(exact, EXACT)
    assert fresh["binding"]["interpreter"] == EXECUTABLE
    assert exact._register_obligation(fresh, kind="not_run")["runner"] == "gate", \
        "a gate that runs exactly the command on the same interpreter is its runner"
    ghost = _session(tmp_path / "ghost", project=root,
                     gate=IntegrationGate(["/nonexistent/location/python", "-m", "unittest", "tests.test_app", "-v"], cwd=root))
    assert ghost._register_obligation(_entry(ghost, COMMAND), kind="not_run")["runner"] is None, \
        "a configured interpreter that does not exist runs nothing (root's negative)"
    assert session._register_obligation(_entry(session, COMMAND), kind="not_run")["oid"] == row["oid"], \
        "the same item registers once while its row is not shown run"


def test_the_display_follows_the_one_discharge_decision_and_is_cleared_when_it_no_longer_holds(tmp_path):
    root = _project(tmp_path)
    session = _session(tmp_path, project=root, gate=IntegrationGate(["python3", "-m", "pytest", "-q"], cwd=root))
    entry = _entry(session, "tests/test_app.py::TestApi::test_history - the sandbox denies sockets")
    session.unexecuted_acceptance.append(entry)
    session._register_obligation(entry, kind="not_run")
    session._refresh_obligations()
    assert entry["discharge_display"] == "bound/1" and entry["discharged_at"] is None
    session.checks.append(_check(session, ["tests/test_app.py::TestApi::test_history"]))
    session._refresh_obligations()
    assert entry["discharged_at"] == 0 and entry["discharged_run"] == "check"
    assert entry["discharged_cases"] == ["tests/test_app.py::TestApi::test_history"]
    (root / "app.py").write_text("def add(a, b):\n    return a + b  # changed\n")
    session._refresh_obligations()
    assert entry["discharged_at"] is None and entry["discharged_cases"] is None, \
        "a later source change leaves no execution of the item on the current source"
    skip = dict(task="t1", role="check", author="harness", item="ui: 1 skipped test case(s): browser",
                why="skipped", requirements=["R1"], after_check=0, receipt="ui", kind="skipped", cases=["browser"],
                discharged_at=0, discharged_run="ui", discharged_cases=["browser"])
    session.unexecuted_acceptance.append(skip)
    session._mark_not_run_display()
    assert skip["discharged_at"] == 0 and "discharge_display" not in skip, "a skip fact keeps its own decision"
    assert "self._mark_not_run_display_safely()" in inspect.getsource(Session._finish_run)
    assert "self._mark_not_run_display_safely()" in inspect.getsource(Session.run)


# -- 6. the independent checker agrees without the engine ----------------------------------------

def _truth():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("truth_check", Path("tools/truth_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(checks, entries, source="f" * 64):
    return dict(source_fingerprint=source, checks=checks, unexecuted_acceptance=entries, evidence_obligations=[])


def _command_entry(**over):
    entry = dict(task="t5", role="lead", author="codex:sol", item=COMMAND, why="not on the approved list",
                 requirements=["R1"], after_check=0, gate="g1", environment="e1",
                 binding=dict(kind="command", scenario=None, cases=MODULE_CASES, problem="",
                              command=COMMAND, files=["tests/test_app.py"], interpreter="/real/python3.11"),
                 cases=list(MODULE_CASES), discharge_display="bound/1", discharged_at=None)
    entry.update(over)
    return entry


def _truth_check(command, cases, *, source="f" * 64, gate="g1", environment="e1", cwd=".", passed=True,
                 interpreter="/real/python3.11"):
    return dict(passed=passed, command=command, output="", command_cwd=cwd, source=source, gate=gate,
                environment=environment, receipts=[], interpreter=interpreter,
                report=dict(state="parsed", passed_cases=list(cases), passed_truncated=False,
                            counts=dict(passed=len(cases), failed=0, errors=0, skipped=0)))


def test_the_checker_clears_a_command_item_only_by_exactly_that_command_and_every_bound_case():
    truth = _truth()
    good = _truth_check("/opt/venv/bin/python -m unittest tests.test_app -v --quadratus-report=/tmp/r.json", MODULE_CASES)
    entry = _command_entry()
    assert truth._command_not_run_cleared(entry, [good], _record([good], [entry]))
    assert truth._not_run_cleared(entry, [good], _record([good], [entry]))
    rejected = {
        "pytest ran the same cases": _truth_check("python3 -m pytest -q", MODULE_CASES),
        "one case short": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES[:1]),
        "without -v": _truth_check("python3 -m unittest tests.test_app", MODULE_CASES),
        "from another cwd": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES, cwd="tests"),
        "another source": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES, source="0" * 64),
        "another gate": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES, gate="g2"),
        "failed": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES, passed=False),
        "another interpreter": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES,
                                            interpreter="/other/python3.11"),
        "no interpreter recorded": _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES, interpreter=None),
    }
    for name, bad in rejected.items():
        assert not truth._not_run_cleared(entry, [bad], _record([bad], [entry])), name
    forged = {
        "forged cases outside the module": _command_entry(cases=["tests/test_other.py::test_x"],
                                                          binding=dict(_command_entry()["binding"], cases=["tests/test_other.py::test_x"])),
        "a discover command bound as a module": _command_entry(item="python3 -m unittest discover -s tests",
                                                                binding=dict(_command_entry()["binding"],
                                                                             command="python3 -m unittest discover -s tests")),
        "item and binding disagree": _command_entry(item="python3 -m unittest tests.test_other -v"),
        "no files": _command_entry(binding=dict(_command_entry()["binding"], files=[])),
        "no gate": _command_entry(gate=None),
        "no interpreter bound": _command_entry(binding=dict(_command_entry()["binding"], interpreter=None)),
    }
    for name, bad in forged.items():
        assert not truth._not_run_cleared(bad, [good], _record([good], [bad])), name


def test_the_checker_reports_a_discharge_display_that_disagrees_with_the_record(tmp_path):
    truth = _truth()
    good = _truth_check("python3 -m unittest tests.test_app -v", MODULE_CASES)
    agree = _command_entry(discharged_at=0, discharged_run="check", discharged_cases=MODULE_CASES)
    marked_wrong = _command_entry(discharged_at=0, discharged_run="check", discharged_cases=MODULE_CASES)
    unmarked = _command_entry()

    def unshown(entries, checks):
        import json
        run = tmp_path / "run"
        run.mkdir(exist_ok=True)
        record = dict(_record(checks, entries), completed=False, error="x", requirements=dict(status={}, listed=[]))
        (run / "result.json").write_text(json.dumps(record))
        return truth.judge(str(run), None)["unverified"]
    assert not any("the engine shows the NOT RUN item" in u for u in unshown([agree], [good]))
    assert any(u.startswith("task t5: the engine shows the NOT RUN item discharged but the record does not show it ran")
               for u in unshown([marked_wrong], []))
    assert any(u.startswith("task t5: the engine shows the NOT RUN item not discharged but the record shows it ran")
               for u in unshown([unmarked], [good]))
    legacy = _command_entry()
    del legacy["discharge_display"]
    assert not any("the engine shows" in u for u in unshown([legacy], [good])), "a record with no display is read as before"
