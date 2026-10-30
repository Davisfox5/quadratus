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

def test_uncovered_requirements_are_reported_at_a_stop_and_are_never_open_work(tmp_path):
    """Root's full suite on 7650cb0: the report line inside ``_open_work``
    made the verification handoff treat an uncovered requirement as a
    blocker and dispatch a writer. The report lives beside the debt, not
    in it: ``_open_work`` names only what blocks, the stop appends the
    uncovered list, and ``_verification_problem`` reads only the debt."""
    session = _session(tmp_path, requirements=("R1", "R2", "R3"))
    session.task_outcomes.append(_outcome("t1"))
    session.memory.ledger.requirement_status["R1"] = "covered by t1"
    _capped(session, "t2", covers=("R2",))
    assert session._uncovered_requirements() == ["R2", "R3"]
    reasons = session._open_work()
    assert reasons == ["capped or failed task(s) t2 not continued to completion"], reasons
    assert not any("requirements no task" in r for r in reasons)
    session._stop_open_work("FindingsOpen", "a task closed with open work")
    assert session.stop_reason == ("FindingsOpen: a task closed with open work, but capped or failed task(s) t2 not "
                                   "continued to completion; requirements no task has covered: R2, R3. Work preserved.")
    session.memory.ledger.requirement_status["R2"] = "met"
    session.memory.ledger.requirement_status["R3"] = "covered by t3"
    assert session._uncovered_requirements() == []
    source = inspect.getsource(Session._verification_problem)
    assert "self._open_work()" in source and "self._uncovered_requirements()" not in source, \
        "the handoff reads blocking debt only; an uncovered requirement is the audit's to judge"
    assert "self._uncovered_requirements()" not in inspect.getsource(Session._open_work)
    assert "self._uncovered_requirements()" not in inspect.getsource(Session._close_policy)
    assert "self._uncovered_requirements()" in inspect.getsource(Session._stop_open_work)


def test_an_uncovered_requirement_alone_is_no_verification_blocker(tmp_path):
    """The three consumers, with nothing blocking: the handoff finds no
    problem beyond the record's own facts, the close policy records no
    open work, and the stop still reports the uncovered requirement."""
    from quadratus.outcome import RunOutcome
    session = _session(tmp_path, requirements=("R1", "R2"))
    session.task_outcomes.append(_outcome("t1"))
    session.memory.ledger.requirement_status["R1"] = "covered by t1"
    assert session._open_work() == []
    assert session._uncovered_requirements() == ["R2"]
    session.run_outcome = RunOutcome()
    session._stop_open_work("FindingsOpen", "a task closed with open work")
    assert session.stop_reason.endswith("but the record shows no single open item; see the task outcomes; "
                                        "requirements no task has covered: R2. Work preserved.")


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


# -- 7. whole-class claims: bound to a declared floor, discharged only with the scope proved -------
#    (class-claim batch, 2026-10-30; root's counterexample on 6527479725)

CLASS_FILES = {
    "app.py": "def add(a, b):\n    return a + b\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "import unittest\n\nfrom app import add\n\n\nclass Parent(unittest.TestCase):\n"
        "    def test_inherited(self):\n        self.assertEqual(add(2, 2), 4)\n\n\n"
        "class AddTests(Parent):\n"
        "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n\n\n"
        "class AddTestsExtra(unittest.TestCase):\n"
        "    def test_extra(self):\n        self.assertEqual(add(3, 3), 6)\n\n\n"
        "class Bare(Parent):\n    pass\n\n\n"
        "def helper():\n    return 1\n"),
    "tests/test_sample.py": (
        "import unittest\n\n\nclass Parent(unittest.TestCase):\n"
        "    def test_inherited(self):\n        self.fail('inherited failure')\n\n\n"
        "class Target(Parent):\n"
        "    def test_own(self):\n        self.assertTrue(True)\n"),
    "tests/test_loaded.py": (
        "import unittest\n\n\nclass Loaded(unittest.TestCase):\n"
        "    def test_one(self):\n        self.assertTrue(True)\n\n\n"
        "def load_tests(loader, tests, pattern):\n    return unittest.TestSuite()\n"),
}
CLASS_ID = "tests/test_app.py::AddTests"
DECLARED = ["tests/test_app.py::AddTests::test_add", "tests/test_app.py::AddTests::test_zero"]
INHERITED = "tests/test_app.py::AddTests::test_inherited"
#: What a whole-file run of tests/test_app.py reports (unittest and pytest
#: both collect the inherited method under the subclass's name).
WHOLE_FILE = sorted(DECLARED + [INHERITED, "tests/test_app.py::Parent::test_inherited",
                                "tests/test_app.py::AddTestsExtra::test_extra", "tests/test_app.py::Bare::test_inherited"])


def _class_session(tmp_path, gate=("python3", "-m", "pytest", "-q")):
    root = _project(tmp_path, CLASS_FILES)
    return root, _session(tmp_path, project=root, gate=IntegrationGate(list(gate), cwd=root))


@pytest.mark.parametrize("item, kind, cases, problem", [
    (CLASS_ID, "class", DECLARED, ""),
    ("./" + CLASS_ID, "class", DECLARED, ""),
    ("tests/test_app.py::Missing", "class", [], "Missing is not defined at the top level of tests/test_app.py"),
    ("tests/test_app.py::Bare", "class", [], "Bare declares no test case in its body; an inherited-only or dynamic"),
    ("tests/test_loaded.py::Loaded", "class", [], "tests/test_loaded.py defines load_tests"),
    ("tests/test_absent.py::AddTests", "class", [], "tests/test_absent.py cannot be read as a Python test module"),
    ("../outside/test_x.py::AddTests", "class", [], "is not a project test file"),
    ("tests/test_app.py::AddTests.extra", "case", [], "names a case id the harness cannot read whole"),
    ("tests/test_app.py::AddTests::", "case", [], "names a case id the harness cannot read whole"),
    ("P1 and tests/test_app.py::AddTests", "case", [], "names a whole test class beside other claims"),
    ("tests/test_app.py::AddTests tests/test_app.py::AddTestsExtra", "case", [],
     "names a whole test class beside other claims"),
    ("tests/test_app.py::AddTests and tests/test_app.py::AddTests::test_add", "case", [],
     "names a whole test class beside other claims"),
    ("AddTests", "none", [], "names no scenario, case or path the harness can identify"),
    ("tests/test_app.py::test_helper_free", "case", [], "is not defined where it says"),
])
def test_a_whole_class_item_binds_to_the_class_s_declared_floor_or_is_held_explicitly(tmp_path, item, kind, cases, problem):
    root, session = _class_session(tmp_path)
    entry = _entry(session, item)
    assert entry["binding"]["kind"] == kind, entry["binding"]
    assert entry["cases"] == cases and entry["binding"]["cases"] == cases
    assert problem in entry["binding"]["problem"], entry["binding"]["problem"]
    if kind == "class" and not problem:
        assert entry["binding"]["class_id"] == CLASS_ID and entry["binding"]["file"] == "tests/test_app.py"
        assert entry["binding"]["class_name"] == "AddTests" and entry["binding"]["declared"] == DECLARED


def test_a_class_defined_twice_or_under_a_bare_name_is_held_and_a_nested_class_is_not_its_parent(tmp_path):
    root, session = _class_session(tmp_path)
    (root / "tests" / "test_twice.py").write_text(
        "import unittest\n\nclass Dup(unittest.TestCase):\n    def test_a(self):\n        pass\n\n"
        "class Dup(unittest.TestCase):\n    def test_b(self):\n        pass\n")
    twice = _entry(session, "tests/test_twice.py::Dup")
    assert twice["cases"] == [] and twice["binding"]["problem"] == "Dup is defined 2 times in tests/test_twice.py"
    (root / "tests" / "test_nested.py").write_text(
        "import unittest\n\nclass Outer(unittest.TestCase):\n    def test_outer(self):\n        pass\n\n"
        "    class Inner(unittest.TestCase):\n        def test_inner(self):\n            pass\n")
    outer = _entry(session, "tests/test_nested.py::Outer")
    assert outer["cases"] == ["tests/test_nested.py::Outer::test_outer"], "a nested class's method is not the parent's floor"
    binding = outer["binding"]
    run = dict(id="check", cwd=".", command="python3 -m pytest -q tests/test_nested.py",
               report_cases={"tests/test_nested.py::Outer::Inner::test_inner"})
    assert S._class_run_cases(run, binding, outer["cases"], root) == [], "only the nested class's case ran"
    run["report_cases"].add("tests/test_nested.py::Outer::test_outer")
    assert S._class_run_cases(run, binding, outer["cases"], root) == ["tests/test_nested.py::Outer::test_outer"]


def _class_check(session, cases, command, **over):
    return _check(session, cases, command=command, **over)


@pytest.mark.parametrize("command", [
    "python3 -m pytest -q tests/test_app.py",
    "python3 -m pytest -q -p no:cacheprovider tests/test_app.py --quadratus-report=/tmp/r.json",
    "python3 -m pytest -q ./tests/test_app.py",
    "python3 -m pytest -q tests",
    "python3 -m pytest -q tests/",
    "python3 -m pytest -q",
    "python3 -m pytest -q -p no:cacheprovider",
    "python3 -m unittest tests.test_app -v",
])
def test_a_whole_class_claim_is_discharged_by_a_complete_whole_file_run_that_shows_the_class_passed(tmp_path, command):
    """The positive, including the inherited method: the command collects
    tests/test_app.py whole, the complete report lists every case the
    runner collected under AddTests, the declared floor is among them, and
    the discharged set is the runner's population (declared plus the
    inherited method), never the floor alone."""
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    session.checks.append(_class_check(session, WHOLE_FILE, command))
    decided = session._discharge_record(entry)
    assert decided == dict(index=0, run="check", cases=sorted(DECLARED + [INHERITED])), command
    session._refresh_obligations()
    assert entry["discharged_at"] == 0 and entry["discharged_cases"] == sorted(DECLARED + [INHERITED])


def test_root_s_selective_unittest_counterexample_never_clears_the_whole_class(tmp_path):
    """Root's control (6527479725): Target(Parent).test_own passes while the
    inherited test_inherited fails; ``python -m unittest
    tests.test_sample.Target.test_own`` runs one method, its report names
    Target::test_own with passed 1, skipped 0, failed 0, errors 0 and a
    complete passed list, so the row is a complete run by execution
    success. The declared floor {test_own} is among the passed prefix, and
    an ast-only rule would clear the whole class. The scope rule refuses:
    a selection inside a module collects no file whole."""
    root, session = _class_session(tmp_path)
    entry = _entry(session, "tests/test_sample.py::Target")
    assert entry["cases"] == ["tests/test_sample.py::Target::test_own"]
    session.unexecuted_acceptance.append(entry)
    selective = "python3 -m unittest tests.test_sample.Target.test_own -v"
    check = _class_check(session, ["tests/test_sample.py::Target::test_own"], selective)
    session.checks.append(check)
    runs = S._check_runs(check, root)
    assert len(runs) == 1 and S._complete_run(runs[0]), "execution success alone reads complete (root's point)"
    assert runs[0]["files"] == set(), "but the selective token names no file whole"
    assert session._discharge_record(entry) is None
    from quadratus.integration import (
        config_selection,
        runner_of,
        unittest_modules,
        whole_file_collected,
    )
    argv = selective.split()
    assert unittest_modules(argv, root) is None and runner_of(argv, root) is None
    assert unittest_modules(argv) == ["tests.test_sample.Target.test_own"], "without a root the shape alone is read, as before"
    assert config_selection(argv, root, root).startswith("test runner not recognised from argv; selection not established")
    assert not whole_file_collected(argv, "tests/test_sample.py", root)
    assert whole_file_collected("python3 -m unittest tests.test_sample -v".split(), "tests/test_sample.py", root)
    assert unittest_modules("python3 -m unittest tests.test_sample -v".split(), root) == ["tests.test_sample"]
    assert unittest_modules("python3 -m unittest tests -v".split(), root) is None, "a package is not a module file"
    # The same items as whole-file execution: the real run would fail on the
    # inherited case; a report that showed the whole file passed clears it.
    whole = _class_check(session, ["tests/test_sample.py::Parent::test_inherited", "tests/test_sample.py::Target::test_own",
                                   "tests/test_sample.py::Target::test_inherited"], "python3 -m unittest tests.test_sample -v")
    session.checks.append(whole)
    assert session._discharge_record(entry) == dict(index=1, run="check", cases=[
        "tests/test_sample.py::Target::test_inherited", "tests/test_sample.py::Target::test_own"])


@pytest.mark.parametrize("name, command, cases, over", [
    ("-k filter", "python3 -m pytest -q tests/test_app.py -k add", WHOLE_FILE, {}),
    ("a :: operand", "python3 -m pytest -q tests/test_app.py::AddTests", WHOLE_FILE, {}),
    ("a case operand", "python3 -m pytest -q tests/test_app.py::AddTests::test_add", WHOLE_FILE, {}),
    ("another directory", "python3 -m pytest -q docs", WHOLE_FILE, {}),
    ("another file", "python3 -m pytest -q tests/test_sample.py", WHOLE_FILE, {}),
    ("-o override", "python3 -m pytest -q -o python_functions=test_add tests/test_app.py", WHOLE_FILE, {}),
    ("--override-ini", "python3 -m pytest -q --override-ini=python_classes=AddTests tests/test_app.py", WHOLE_FILE, {}),
    ("unittest discover, whose runner config_selection does not recognise", "python3 -m unittest discover -s tests", WHOLE_FILE, {}),
    ("discover with a pattern", "python3 -m unittest discover -s tests -p test_a*.py", WHOLE_FILE, {}),
    ("a wrapper", "make test", WHOLE_FILE, {}),
    ("a selective module token", "python3 -m unittest tests.test_app.AddTests -v", WHOLE_FILE, {}),
    ("a declared method missing", "python3 -m pytest -q tests/test_app.py", [c for c in WHOLE_FILE if not c.endswith("test_zero")], {}),
    ("the class not collected", "python3 -m pytest -q tests/test_app.py",
     ["tests/test_app.py::AddTestsExtra::test_extra", "tests/test_app.py::Parent::test_inherited"], {}),
    ("a longer class name only", "python3 -m pytest -q tests/test_app.py",
     ["tests/test_app.py::AddTestsExtra::test_extra", "tests/test_app.py::AddTestsExtra::test_add",
      "tests/test_app.py::AddTestsExtra::test_zero"], {}),
    ("a skipped case", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, dict(skipped=1)),
    ("a failed check", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, dict(passed=False)),
    ("another source", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, dict(source="0" * 64)),
    ("another gate", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, dict(gate="other")),
    ("another environment", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, dict(environment="other")),
    ("from another cwd", "python3 -m pytest -q test_app.py", WHOLE_FILE, dict(cwd="tests")),
])
def test_a_whole_class_claim_is_never_cleared_without_the_scope_the_floor_and_the_identity(tmp_path, name, command, cases, over):
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    session.checks.append(_class_check(session, cases, command, **over))
    assert session._discharge_record(entry) is None, name
    session._refresh_obligations()
    assert entry["discharged_at"] is None and entry["discharge_display"] == "bound/1", name


def test_a_truncated_or_unparsed_report_and_a_collection_override_in_the_configuration_clear_nothing(tmp_path):
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    check = _class_check(session, WHOLE_FILE, "python3 -m pytest -q tests/test_app.py")
    session.checks.append(check)
    assert session._discharge_record(entry) is not None
    check["report"]["passed_truncated"] = True
    assert session._discharge_record(entry) is None, "a passed list cut at the bound credits nothing"
    check["report"]["passed_truncated"] = False
    check["report"]["state"] = "invalid"
    assert session._discharge_record(entry) is None
    check["report"]["state"] = "parsed"
    assert session._discharge_record(entry) is not None
    def configured(name, text):
        # A configuration file is source: the check is re-stamped to the
        # source as it stands, so only the collection rule is under test.
        for stale in ("pytest.ini", "pyproject.toml", "setup.cfg"):
            (root / stale).unlink(missing_ok=True)
        if name:
            (root / name).write_text(text)
        check["source"] = session._source_fingerprint()
        return session._discharge_record(entry)
    assert configured("pytest.ini", "[pytest]\npython_functions = test_a*\n") is None, \
        "a collection key in the configuration is an unmodelled collection"
    assert configured("pytest.ini", "[pytest]\naddopts = -q\n") is not None, \
        "a configuration without a collection key is read as before"
    assert configured("pyproject.toml", "[tool.pytest.ini_options]\npython_classes = ['Add*']\n") is None
    assert configured("setup.cfg", "[tool:pytest]\npython_files = check_*.py\n") is None
    assert configured("", "") is not None
    import os
    before = os.environ.get("PYTEST_ADDOPTS")
    try:
        os.environ["PYTEST_ADDOPTS"] = "-o python_functions=test_a*"
        assert session._discharge_record(entry) is None, "an override from the environment is read too"
    finally:
        if before is None:
            os.environ.pop("PYTEST_ADDOPTS", None)
        else:
            os.environ["PYTEST_ADDOPTS"] = before


def test_a_class_that_changed_since_the_claim_leaves_the_claim_standing(tmp_path):
    """Source drift: a method added to the class after the claim joins the
    floor, so a report that predates it clears nothing; a declared method
    removed, or a load_tests added to the module, is a class the harness
    can no longer read against the claim, and the claim stands rather than
    shrinking. Nothing is rebound."""
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    text = (root / "tests" / "test_app.py").read_text()
    grown = text.replace("    def test_zero(self):", "    def test_new(self):\n        pass\n\n    def test_zero(self):")
    (root / "tests" / "test_app.py").write_text(grown)
    session.checks.append(_class_check(session, WHOLE_FILE, "python3 -m pytest -q tests/test_app.py"))
    assert entry["binding"]["declared"] == DECLARED, "the claim's own floor is as recorded"
    assert session._discharge_record(entry) is None, "the class now declares test_new, which the report does not show"
    session.checks.append(_class_check(session, WHOLE_FILE + ["tests/test_app.py::AddTests::test_new"],
                                       "python3 -m pytest -q tests/test_app.py"))
    assert session._discharge_record(entry) == dict(index=1, run="check",
                                                   cases=sorted(DECLARED + [INHERITED, "tests/test_app.py::AddTests::test_new"]))
    shrunk = text.replace("    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n", "")
    (root / "tests" / "test_app.py").write_text(shrunk)
    session.checks.append(_class_check(session, [c for c in WHOLE_FILE if not c.endswith("test_zero")],
                                       "python3 -m pytest -q tests/test_app.py"))
    assert session._discharge_record(entry) is None, "a declared method the claim recorded is gone: the claim stands"
    (root / "tests" / "test_app.py").write_text(text + "\n\ndef load_tests(loader, tests, pattern):\n    return tests\n")
    session.checks.append(_class_check(session, WHOLE_FILE, "python3 -m pytest -q tests/test_app.py"))
    assert session._discharge_record(entry) is None, "load_tests added since the claim"
    assert entry["binding"]["kind"] == "class" and entry["cases"] == DECLARED, "nothing was rebound"


def test_a_class_claim_s_typed_row_is_the_gate_s_only_when_a_gate_command_collects_the_file_whole(tmp_path):
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    row = session._register_obligation(entry, kind="not_run")
    assert row["runner"] == "gate" and list(row["cases"]) == DECLARED, "pytest -q from the root discovers the file"
    session.checks.append(dict(_class_check(session, WHOLE_FILE, "python3 -m pytest -q"), requirements={"R1": 1, "R2": 1}))
    session._refresh_obligations()
    row = next(o for o in session.evidence_obligations if o["oid"] == row["oid"])
    assert row["execution"] == "ran" and list(row["receipts"]) == ["check-1"], row
    assert session._obligation_receipt(row)["passed_cases"] == sorted(DECLARED + [INHERITED])
    assert session._standing_not_run() == [] and session._not_run_block() == ""
    selective = _class_session(tmp_path / "sel", gate=("python3", "-m", "unittest", "tests.test_app.AddTests.test_add", "-v"))[1]
    held = _entry(selective, CLASS_ID)
    selective.unexecuted_acceptance.append(held)
    assert selective._register_obligation(held, kind="not_run")["runner"] is None, "a selective gate is no runner for it"
    other = _class_session(tmp_path / "other", gate=("python3", "-m", "pytest", "-q", "tests/test_sample.py"))[1]
    held = _entry(other, CLASS_ID)
    other.unexecuted_acceptance.append(held)
    assert other._register_obligation(held, kind="not_run")["runner"] is None
    block = other._not_run_block()
    assert "reported NOT RUN: tests/test_app.py::AddTests" in block and "[bound to " + ", ".join(DECLARED) in block
    route = other._evidence_route("R1")
    assert route["kind"] == "execution debt" and ", ".join(DECLARED) in route["route"]


# -- 8. the independent checker reconstructs the class scope from the rows alone -----------------

def _class_entry(**over):
    entry = dict(task="t6", role="lead", author="openai:sol", item=CLASS_ID, why="the sandbox denies sockets",
                 requirements=["R14"], after_check=0, gate="g1", environment="e1",
                 binding=dict(kind="class", scenario=None, cases=list(DECLARED), problem="", class_id=CLASS_ID,
                              file="tests/test_app.py", class_name="AddTests", declared=list(DECLARED)),
                 cases=list(DECLARED), discharge_display="bound/1", discharged_at=None)
    entry.update(over)
    return entry


def _suite_check(receipts, *, source="f" * 64, gate="g1", environment="e1", passed=True):
    """A gate-suite check in the shape the sealed completion-workflow-02
    record has: an aggregate ``gate suite`` row with one receipt per
    command, each with its own command, cwd, status and parsed report."""
    return dict(passed=passed, command="gate suite", output="", command_cwd=".", source=source, gate=gate,
                environment=environment, report=None, receipts=[
                    dict(id=rid, status="passed" if ok else "failed", required=True, command=command, cwd=".",
                         output="", tests=len(cases), interpreter="/real/python3.12 env=base",
                         report=dict(state="parsed", passed_cases=list(cases), passed_truncated=truncated,
                                     counts=dict(passed=len(cases), failed=0, errors=0, skipped=0)))
                    for rid, command, cases, ok, truncated in receipts])


HISTORICAL = sorted(f"tests/test_api.py::HandlerDependencyTests::test_{i}" for i in "abcdefgh")
OTHERS = sorted(f"tests/test_api.py::DispatchDependencyTests::test_{i}" for i in "abcdefgh") + [
    "tests/test_cli.py::CliTests::test_x", "tests/test_dependencies.py::DepTests::test_y", "tests/test_existing.py::OldTests::test_z"]


def test_the_checker_reconstructs_the_historical_class_shape_and_clears_it_from_the_rows_alone():
    """The offline reconstruction of completion-workflow-02 t6's shape, in
    synthetic names: a ``file::Class`` item, the class's eight methods as
    the floor, and a final gate suite of two receipts, ``python3 -m
    unittest discover -s tests`` and a pytest run naming the file whole,
    each with a complete parsed report listing every method. Either
    receipt alone establishes it; the sealed run had both."""
    truth = _truth()
    floor = list(HISTORICAL)
    entry = _class_entry(item="tests/test_api.py::HandlerDependencyTests", requirements=["R14", "R15", "R16"],
                         cases=floor, binding=dict(kind="class", scenario=None, cases=floor, problem="",
                                                   class_id="tests/test_api.py::HandlerDependencyTests",
                                                   file="tests/test_api.py", class_name="HandlerDependencyTests",
                                                   declared=floor))
    everything = sorted(HISTORICAL + OTHERS)
    both = _suite_check([
        ("public-unittest", "python3 -m unittest discover -s tests --quadratus-report=/tmp/u.json", everything, True, False),
        ("public-pytest", "/runtime/bin/python -m pytest -v tests/test_api.py tests/test_cli.py tests/test_dependencies.py "
                          "tests/test_existing.py --quadratus-report=/tmp/p.json", everything, True, False)])
    # As the engine recorded it: the discover receipt's selection says the
    # runner was not recognised, so that row is never whole.
    both["receipts"][0]["selection"] = "test runner not recognised from argv; selection not established"
    assert truth._class_not_run_cleared(entry, [both], _record([both], [entry]))
    assert truth._not_run_cleared(entry, [both], _record([both], [entry]))
    unittest_alone, pytest_alone = (dict(both, receipts=[both["receipts"][i]]) for i in (0, 1))
    assert truth._not_run_cleared(entry, [pytest_alone], _record([pytest_alone], [entry])), "the pytest receipt names the file whole"
    assert not truth._not_run_cleared(entry, [unittest_alone], _record([unittest_alone], [entry])), \
        "the discover receipt is not a whole run the record can show (as the engine decided on the sealed record)"
    del unittest_alone["receipts"][0]["selection"]
    assert not truth._not_run_cleared(entry, [unittest_alone], _record([unittest_alone], [entry])), \
        "and discover is not a form the scope rule reads"
    # Root's counterexample, in the record's own shape: a selective dotted
    # command whose complete report names the one method it ran.
    selective = _suite_check([("public-unittest", "python3 -m unittest tests.test_api.HandlerDependencyTests.test_a -v",
                               ["tests/test_api.py::HandlerDependencyTests::test_a"], True, False)])
    narrow = _class_entry(item="tests/test_api.py::HandlerDependencyTests", cases=floor[:1],
                          binding=dict(entry["binding"], cases=floor[:1], declared=floor[:1]))
    assert truth._row_ok(truth._rows(selective)[0]), "execution success reads whole (root's point)"
    assert not truth._not_run_cleared(narrow, [selective], _record([selective], [narrow]))
    assert not truth._collects_whole_file("python3 -m unittest tests.test_api.HandlerDependencyTests.test_a -v", "tests/test_api.py")


def test_the_checker_refuses_every_class_row_that_does_not_prove_the_scope_the_floor_or_the_identity():
    truth = _truth()
    entry = _class_entry()
    whole = sorted(DECLARED + [INHERITED, "tests/test_app.py::AddTestsExtra::test_extra"])
    good = _suite_check([("check", "python3 -m pytest -q tests/test_app.py --quadratus-report=/tmp/r.json", whole, True, False)])
    assert truth._not_run_cleared(entry, [good], _record([good], [entry]))
    rows = {
        "-k": "python3 -m pytest -q tests/test_app.py -k add",
        ":: operand": "python3 -m pytest -q tests/test_app.py::AddTests",
        "another file": "python3 -m pytest -q tests/test_sample.py",
        "another directory": "python3 -m pytest -q docs",
        "-o": "python3 -m pytest -q -o python_functions=test_add tests/test_app.py",
        "selective unittest": "python3 -m unittest tests.test_app.AddTests -v",
        "discover with pattern": "python3 -m unittest discover -s tests -p 'test_a*.py'",
        "discover": "python3 -m unittest discover -s tests",
        "a wrapper": "make test",
    }
    for name, command in rows.items():
        bad = _suite_check([("check", command, whole, True, False)])
        assert not truth._not_run_cleared(entry, [bad], _record([bad], [entry])), name
    shapes = {
        "truncated passed list": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, True)]),
        "a declared method missing": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                                    [c for c in whole if not c.endswith("test_zero")], True, False)]),
        "the class not collected": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                                  ["tests/test_app.py::AddTestsExtra::test_extra"], True, False)]),
        "a longer class name": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                             ["tests/test_app.py::AddTestsExtra::test_add", "tests/test_app.py::AddTestsExtra::test_zero"],
                                             True, False)]),
        "failed receipt": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, False, False)]),
        "another source": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)], source="0" * 64),
        "another gate": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)], gate="g2"),
        "another environment": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)], environment="e2"),
    }
    for name, bad in shapes.items():
        assert not truth._not_run_cleared(entry, [bad], _record([bad], [entry])), name
    skipped = _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)])
    skipped["receipts"][0]["report"]["counts"]["skipped"] = 1
    assert not truth._not_run_cleared(entry, [skipped], _record([skipped], [entry])), "a skip"
    elsewhere = _suite_check([("check", "python3 -m pytest -q test_app.py", whole, True, False)])
    elsewhere["receipts"][0]["cwd"] = "tests"
    assert not truth._not_run_cleared(entry, [elsewhere], _record([elsewhere], [entry])), "another cwd"
    forged = {
        "a declared subset as the typed cases": _class_entry(cases=DECLARED[:1]),
        "an empty floor": _class_entry(cases=[], binding=dict(_class_entry()["binding"], declared=[], cases=[])),
        "a floor outside the class": _class_entry(cases=["tests/test_app.py::AddTestsExtra::test_extra"],
                                                 binding=dict(_class_entry()["binding"], declared=["tests/test_app.py::AddTestsExtra::test_extra"])),
        "a nested id in the floor": _class_entry(cases=["tests/test_app.py::AddTests::Inner::test_x"],
                                                binding=dict(_class_entry()["binding"], declared=["tests/test_app.py::AddTests::Inner::test_x"])),
        "item and binding disagree": _class_entry(item="tests/test_app.py::AddTestsExtra"),
        "a mixed item": _class_entry(item="P1 and tests/test_app.py::AddTests"),
        "class id disagrees": _class_entry(binding=dict(_class_entry()["binding"], class_id="tests/test_app.py::Other")),
        "a case-shaped class": _class_entry(item="tests/test_app.py::test_add", binding=dict(_class_entry()["binding"],
                                                                                         class_id="tests/test_app.py::test_add",
                                                                                         class_name="test_add")),
        "no gate": _class_entry(gate=None),
        "no after_check": _class_entry(after_check=None),
        "an engine mark without a row": _class_entry(discharged_at=0, discharged_run="check", discharged_cases=DECLARED),
    }
    for name, bad in forged.items():
        checks = [] if name == "an engine mark without a row" else [good]
        assert not truth._not_run_cleared(bad, checks, _record(checks, [bad])), name
    # The typed cases must be the recorded floor; a floor understated in
    # both places is not a forgery the record can show, and the clearance
    # does not rest on it: the row's whole-file scope and complete report
    # are what clear the class, the floor only refuses a row that misses a
    # method the engine read as declared.
    understated = _class_entry(cases=DECLARED[:1], binding=dict(_class_entry()["binding"], declared=DECLARED[:1],
                                                                cases=DECLARED[:1]))
    assert truth._class_not_run_cleared(understated, [good], _record([good], [understated]))
    assert truth._class_not_run_cleared(_class_entry(), [good], _record([good], [_class_entry()]))
