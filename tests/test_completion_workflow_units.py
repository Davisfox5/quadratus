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


# -- 7. whole-class claims: bound to a declared floor, discharged only by a run whose own report
#    attests the class's membership (class-claim batch, 2026-10-30; root's selective-command
#    counterexample on 6527479725 and the load_tests / python_functions counterexamples on 6528355944)

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


def _attest(cases):
    """The class attestation a faithful producer writes for a run whose
    passed cases are exactly ``cases``: one record per class, its members
    the method names shown (parameters stripped)."""
    classes = {}
    for case in cases:
        parts = str(case).split("::")
        if len(parts) < 3:
            continue
        classes.setdefault("::".join(parts[:-1]), set()).add(parts[-1].split("[", 1)[0])
    return [dict(id=cid, members=sorted(members)) for cid, members in sorted(classes.items())]


def _class_session(tmp_path, gate=("python3", "-m", "pytest", "-q")):
    root = _project(tmp_path, CLASS_FILES)
    return root, _session(tmp_path, project=root, gate=IntegrationGate(list(gate), cwd=root) if gate else None)


def _class_check(session, cases, command, *, classes=None, classes_truncated=False, **over):
    check = _check(session, cases, command=command, **over)
    check["report"]["classes"] = _attest(cases) if classes is None else classes
    check["report"]["classes_truncated"] = classes_truncated
    return check


@pytest.mark.parametrize("item, kind, cases, problem", [
    (CLASS_ID, "class", DECLARED, ""),
    ("./" + CLASS_ID, "class", DECLARED, ""),
    ("tests/test_loaded.py::Loaded", "class", ["tests/test_loaded.py::Loaded::test_one"], ""),
    ("tests/test_app.py::Missing", "class", [], "Missing is not defined at the top level of tests/test_app.py"),
    ("tests/test_app.py::Bare", "class", [], "Bare declares no test case in its body; an inherited-only or dynamic"),
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
    if kind == "class" and not problem and item.endswith("AddTests"):
        assert entry["binding"]["class_id"] == CLASS_ID and entry["binding"]["file"] == "tests/test_app.py"
        assert entry["binding"]["class_name"] == "AddTests" and entry["binding"]["declared"] == DECLARED


def test_a_class_defined_twice_is_held_and_a_nested_class_is_not_its_parent(tmp_path):
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
    run = dict(id="check", cwd=".", command="python3 -m pytest -q tests/test_nested.py",
               report_cases={"tests/test_nested.py::Outer::Inner::test_inner"},
               report_classes={"tests/test_nested.py::Outer::Inner": ["test_inner"], "tests/test_nested.py::Outer": ["test_outer"]})
    assert S._class_run_cases(run, outer["binding"], outer["cases"]) == [], "only the nested class's case ran"
    run["report_cases"].add("tests/test_nested.py::Outer::test_outer")
    assert S._class_run_cases(run, outer["binding"], outer["cases"]) == ["tests/test_nested.py::Outer::test_outer"]


@pytest.mark.parametrize("command", [
    "python3 -m pytest -q tests/test_app.py",
    "python3 -m pytest -q -p no:cacheprovider tests/test_app.py --quadratus-report=/tmp/r.json",
    "python3 -m pytest -q",
    "python3 -m unittest tests.test_app -v",
])
def test_a_whole_class_claim_is_discharged_by_a_complete_run_whose_report_attests_the_class_passed_whole(tmp_path, command):
    """The positive, including the inherited method: the run is complete,
    its report attests AddTests's membership (the producer read it from
    the live class: declared and inherited), every attested member and
    every declared one is among the passed cases, and the discharged set
    is the attested population, never the floor alone."""
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    session.checks.append(_class_check(session, WHOLE_FILE, command))
    decided = session._discharge_record(entry)
    assert decided == dict(index=0, run="check", cases=sorted(DECLARED + [INHERITED])), command
    session._refresh_obligations()
    assert entry["discharged_at"] == 0 and entry["discharged_cases"] == sorted(DECLARED + [INHERITED])


def test_a_dynamically_attached_member_is_part_of_the_attested_population(tmp_path):
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    dynamic = "tests/test_app.py::AddTests::test_dyn"
    session.checks.append(_class_check(session, WHOLE_FILE + [dynamic], "python3 -m pytest -q tests/test_app.py"))
    assert session._discharge_record(entry)["cases"] == sorted(DECLARED + [INHERITED, dynamic])
    # The same attestation with the dynamic member not passed holds the claim.
    session.checks.append(_class_check(session, WHOLE_FILE, "python3 -m pytest -q tests/test_app.py",
                                       classes=_attest(WHOLE_FILE + [dynamic])))
    entry2 = _entry(session, CLASS_ID, after_check=1)
    assert session._discharge_record(entry2) is None


def test_root_s_counterexamples_never_clear_the_whole_class(tmp_path):
    """Root's controls (6527479725, 6528355944) at the units, with the
    attestation a faithful producer writes. (1) ``python -m unittest
    tests.test_sample.Target.test_own`` runs one method: a complete run by
    execution success, and the dotted token resolves to no module file, so
    the runner is not recognised. (2) ``python -m unittest
    tests.test_sample`` under ``from helper import choose as load_tests``
    runs only Target.test_own: the module form, selection established,
    complete. (3) ``pytest -c custom.ini`` with ``python_functions =
    test_own`` runs only TestTarget.test_own. In every case the runner
    attests Target's membership as [test_inherited, test_own] from the
    live class, test_inherited is not among the passed cases, and the
    claim stands. Only a run that attests and passes the whole membership
    discharges it."""
    root, session = _class_session(tmp_path)
    entry = _entry(session, "tests/test_sample.py::Target")
    assert entry["cases"] == ["tests/test_sample.py::Target::test_own"]
    session.unexecuted_acceptance.append(entry)
    attested = [dict(id="tests/test_sample.py::Target", members=["test_inherited", "test_own"])]
    own = ["tests/test_sample.py::Target::test_own"]
    selective = "python3 -m unittest tests.test_sample.Target.test_own -v"
    check = _class_check(session, own, selective, classes=attested)
    session.checks.append(check)
    runs = S._check_runs(check, root)
    assert len(runs) == 1 and S._complete_run(runs[0]), "execution success alone reads complete (root's point)"
    assert runs[0]["files"] == set() and runs[0]["report_classes"] == {"tests/test_sample.py::Target": ["test_inherited", "test_own"]}
    assert session._discharge_record(entry) is None
    session.checks.append(_class_check(session, own, "python3 -m unittest tests.test_sample --quadratus-report=/tmp/r.json",
                                       classes=attested))
    assert session._discharge_record(entry) is None, "load_tests shrank the module run; the attestation shows the member it left out"
    session.checks.append(_class_check(session, ["tests/test_sample.py::TestTarget::test_own"],
                                       "python3 -m pytest -q tests/test_sample.py --quadratus-report=/tmp/r.json -c custom.ini",
                                       classes=[dict(id="tests/test_sample.py::TestTarget", members=["test_inherited", "test_own"])]))
    cfg = _entry(session, "tests/test_sample.py::Target")  # the same claim; TestTarget is another class
    assert session._discharge_record(cfg) is None
    from quadratus.integration import config_selection, runner_of, unittest_modules
    argv = selective.split()
    assert unittest_modules(argv, root) is None and runner_of(argv, root) is None
    assert unittest_modules(argv) == ["tests.test_sample.Target.test_own"], "without a root the shape alone is read, as before"
    assert config_selection(argv, root, root).startswith("test runner not recognised from argv; selection not established")
    assert unittest_modules("python3 -m unittest tests.test_sample -v".split(), root) == ["tests.test_sample"]
    assert unittest_modules("python3 -m unittest tests -v".split(), root) is None, "a package is not a module file"
    whole = _class_check(session, ["tests/test_sample.py::Parent::test_inherited", "tests/test_sample.py::Target::test_own",
                                   "tests/test_sample.py::Target::test_inherited"], "python3 -m unittest tests.test_sample -v")
    session.checks.append(whole)
    assert session._discharge_record(entry) == dict(index=3, run="check", cases=[
        "tests/test_sample.py::Target::test_inherited", "tests/test_sample.py::Target::test_own"])


@pytest.mark.parametrize("name, command, cases, classes, over", [
    ("-k filter", "python3 -m pytest -q tests/test_app.py -k add", WHOLE_FILE, None, {}),
    ("a :: operand", "python3 -m pytest -q tests/test_app.py::AddTests", WHOLE_FILE, None, {}),
    ("no attestation", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, [], {}),
    ("attestation for other classes only", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE,
     [c for c in _attest(WHOLE_FILE) if c["id"] != CLASS_ID], {}),
    ("an attested member the run did not pass (root's load_tests shape)", "python3 -m pytest -q tests/test_app.py",
     [c for c in WHOLE_FILE if c != INHERITED], _attest(WHOLE_FILE), {}),
    ("a declared method missing from the run and the attestation", "python3 -m pytest -q tests/test_app.py",
     [c for c in WHOLE_FILE if not c.endswith("test_zero")], None, {}),
    ("the class not collected", "python3 -m pytest -q tests/test_app.py",
     ["tests/test_app.py::AddTestsExtra::test_extra", "tests/test_app.py::Parent::test_inherited"], None, {}),
    ("a longer class name only", "python3 -m pytest -q tests/test_app.py",
     ["tests/test_app.py::AddTestsExtra::test_extra", "tests/test_app.py::AddTestsExtra::test_add",
      "tests/test_app.py::AddTestsExtra::test_zero"], None, {}),
    ("a skipped case", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, None, dict(skipped=1)),
    ("a failed check", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, None, dict(passed=False)),
    ("another source", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, None, dict(source="0" * 64)),
    ("another gate", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, None, dict(gate="other")),
    ("another environment", "python3 -m pytest -q tests/test_app.py", WHOLE_FILE, None, dict(environment="other")),
    ("from another cwd", "python3 -m pytest -q test_app.py", WHOLE_FILE, None, dict(cwd="tests")),
])
def test_a_whole_class_claim_is_never_cleared_without_the_attestation_the_floor_and_the_identity(
        tmp_path, name, command, cases, classes, over):
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    session.checks.append(_class_check(session, cases, command, classes=classes, **over))
    assert session._discharge_record(entry) is None, name
    session._refresh_obligations()
    assert entry["discharged_at"] is None and entry["discharge_display"] == "bound/1", name


def test_a_truncated_or_unparsed_report_or_attestation_clears_nothing(tmp_path):
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
    check["report"]["classes_truncated"] = True
    assert session._discharge_record(entry) is None, "a class list cut at the bound attests nothing"
    check["report"]["classes_truncated"] = False
    del check["report"]["classes"]
    assert session._discharge_record(entry) is None, "a report without the field attests nothing"
    check["report"]["classes"] = _attest(WHOLE_FILE)
    assert session._discharge_record(entry) is not None


def test_a_class_that_changed_since_the_claim_leaves_the_claim_standing(tmp_path):
    """Source drift: a method added to the class after the claim joins the
    floor, so a report that predates it clears nothing; a declared method
    removed is a class the harness can no longer read against the claim,
    and the claim stands rather than shrinking. Nothing is rebound."""
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
    assert entry["binding"]["kind"] == "class" and entry["cases"] == DECLARED, "nothing was rebound"


def test_a_class_claim_s_typed_row_is_the_gate_s_and_runs_only_on_an_attested_whole_run(tmp_path):
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    row = session._register_obligation(entry, kind="not_run")
    assert row["runner"] == "gate" and list(row["cases"]) == DECLARED
    session.checks.append(dict(_class_check(session, WHOLE_FILE, "python3 -m pytest -q", classes=[]), requirements={"R1": 1, "R2": 1}))
    session._refresh_obligations()
    row = next(o for o in session.evidence_obligations if o["oid"] == row["oid"])
    assert row["execution"] == "missing", "a passing run that attests nothing for the class is no execution of it"
    session.checks.append(dict(_class_check(session, WHOLE_FILE, "python3 -m pytest -q"), requirements={"R1": 1, "R2": 1}))
    session._refresh_obligations()
    row = next(o for o in session.evidence_obligations if o["oid"] == row["oid"])
    assert row["execution"] == "ran" and list(row["receipts"]) == ["check-2"], row
    assert session._obligation_receipt(row)["passed_cases"] == sorted(DECLARED + [INHERITED])
    assert session._standing_not_run() == [] and session._not_run_block() == ""
    other = _class_session(tmp_path / "other", gate=None)[1]
    held = _entry(other, CLASS_ID)
    other.unexecuted_acceptance.append(held)
    assert other._register_obligation(held, kind="not_run")["runner"] is None, "no gate, no runner"
    block = other._not_run_block()
    assert "reported NOT RUN: tests/test_app.py::AddTests" in block and "[bound to " + ", ".join(DECLARED) in block
    route = other._evidence_route("R1")
    assert route["kind"] == "execution debt" and ", ".join(DECLARED) in route["route"]


# -- 8. the independent checker reads the attestation from the rows alone ------------------------

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
    command, each with its own command, cwd, status and parsed report; the
    report attests the classes of the cases it names unless a receipt
    tuple carries its own attestation."""
    rows = []
    for rid, command, cases, ok, truncated, *rest in receipts:
        classes = rest[0] if rest else _attest(cases)
        rows.append(dict(id=rid, status="passed" if ok else "failed", required=True, command=command, cwd=".",
                         output="", tests=len(cases), interpreter="/real/python3.12 env=base",
                         report=dict(state="parsed", passed_cases=list(cases), passed_truncated=truncated,
                                     classes=classes, classes_truncated=False,
                                     counts=dict(passed=len(cases), failed=0, errors=0, skipped=0))))
    return dict(passed=passed, command="gate suite", output="", command_cwd=".", source=source, gate=gate,
                environment=environment, report=None, receipts=rows)


HISTORICAL = sorted(f"tests/test_api.py::HandlerDependencyTests::test_{i}" for i in "abcdefgh")
OTHERS = sorted(f"tests/test_api.py::DispatchDependencyTests::test_{i}" for i in "abcdefgh") + [
    "tests/test_cli.py::CliTests::test_x", "tests/test_dependencies.py::DepTests::test_y", "tests/test_existing.py::OldTests::test_z"]


def test_the_checker_reconstructs_the_historical_class_shape_and_clears_it_from_the_rows_alone():
    """The offline reconstruction of completion-workflow-02 t6's shape, in
    synthetic names: a ``file::Class`` item, the class's eight methods as
    the floor, and a final gate suite of two receipts, ``python3 -m
    unittest discover -s tests`` and a pytest run naming the file whole,
    each with a complete parsed report listing every method and attesting
    the class. The pytest receipt clears it; the discover receipt's
    recorded selection says its runner was not recognised, so it does not."""
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
    both["receipts"][0]["selection"] = "test runner not recognised from argv; selection not established"
    assert truth._class_not_run_cleared(entry, [both], _record([both], [entry]))
    assert truth._not_run_cleared(entry, [both], _record([both], [entry]))
    unittest_alone, pytest_alone = (dict(both, receipts=[both["receipts"][i]]) for i in (0, 1))
    assert truth._not_run_cleared(entry, [pytest_alone], _record([pytest_alone], [entry]))
    assert not truth._not_run_cleared(entry, [unittest_alone], _record([unittest_alone], [entry])), \
        "the discover receipt's selection was not established (as the engine recorded on the sealed record)"
    # Root's counterexamples in the record's own shape: a complete passing
    # report whose attestation names the member the run left out.
    attested = [dict(id="tests/test_api.py::HandlerDependencyTests", members=[f"test_{i}" for i in "abcdefgh"])]
    for command in ("python3 -m unittest tests.test_api.HandlerDependencyTests.test_a -v",
                    "python3 -m unittest tests.test_api --quadratus-report=/tmp/u.json",
                    "python3 -m pytest -q tests/test_api.py --quadratus-report=/tmp/p.json -c custom.ini"):
        one = _suite_check([("check", command, [HISTORICAL[0]], True, False, attested)])
        assert truth._row_ok(truth._rows(one)[0]), "execution success reads whole (root's point)"
        assert not truth._not_run_cleared(entry, [one], _record([one], [entry])), command


def test_the_checker_refuses_every_class_row_that_does_not_attest_the_whole_class_passed():
    truth = _truth()
    entry = _class_entry()
    whole = sorted(DECLARED + [INHERITED, "tests/test_app.py::AddTestsExtra::test_extra"])
    good = _suite_check([("check", "python3 -m pytest -q tests/test_app.py --quadratus-report=/tmp/r.json", whole, True, False)])
    assert truth._not_run_cleared(entry, [good], _record([good], [entry]))
    shapes = {
        "-k": _suite_check([("check", "python3 -m pytest -q tests/test_app.py -k add", whole, True, False)]),
        ":: operand": _suite_check([("check", "python3 -m pytest -q tests/test_app.py::AddTests", whole, True, False)]),
        "truncated passed list": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, True)]),
        "no attestation": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False, [])]),
        "other classes attested": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False,
                                                 [c for c in _attest(whole) if c["id"] != CLASS_ID])]),
        "an attested member not passed": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                                        [c for c in whole if c != INHERITED], True, False, _attest(whole))]),
        "a declared method missing": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                                    [c for c in whole if not c.endswith("test_zero")], True, False)]),
        "the class not collected": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                                  ["tests/test_app.py::AddTestsExtra::test_extra"], True, False)]),
        "a longer class name": _suite_check([("check", "python3 -m pytest -q tests/test_app.py",
                                             ["tests/test_app.py::AddTestsExtra::test_add", "tests/test_app.py::AddTestsExtra::test_zero"],
                                             True, False)]),
        "two records for the class": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False,
                                                    _attest(whole) + [dict(id=CLASS_ID, members=["test_add"])])]),
        "a parametrised member name": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False,
                                                     [dict(id=CLASS_ID, members=["test_add[1]", "test_zero"])])]),
        "an empty member list": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False,
                                               [dict(id=CLASS_ID, members=[])])]),
        "failed receipt": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, False, False)]),
        "another source": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)], source="0" * 64),
        "another gate": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)], gate="g2"),
        "another environment": _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)], environment="e2"),
    }
    for name, bad in shapes.items():
        assert not truth._not_run_cleared(entry, [bad], _record([bad], [entry])), name
    truncated = _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)])
    truncated["receipts"][0]["report"]["classes_truncated"] = True
    assert not truth._not_run_cleared(entry, [truncated], _record([truncated], [entry])), "a truncated class list"
    absent = _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)])
    del absent["receipts"][0]["report"]["classes"]
    del absent["receipts"][0]["report"]["classes_truncated"]
    assert not truth._not_run_cleared(entry, [absent], _record([absent], [entry])), "a report without the field"
    skipped = _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)])
    skipped["receipts"][0]["report"]["counts"]["skipped"] = 1
    assert not truth._not_run_cleared(entry, [skipped], _record([skipped], [entry])), "a skip"
    selected = _suite_check([("check", "python3 -m pytest -q tests/test_app.py", whole, True, False)])
    selected["receipts"][0]["selection"] = "tests/conftest.py: pytest_collection_modifyitems"
    assert not truth._not_run_cleared(entry, [selected], _record([selected], [entry])), "a recorded selection"
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


# -- 9. root's counterexamples on the real runners (6528355944): the harness producers attest what
#    the live class holds, so a hook or a configured pattern that leaves a member out is visible ----

ROOT_FILES = {
    "helper.py": ("import unittest\n\n\ndef choose(loader, tests, pattern):\n"
                  "    return unittest.TestSuite(c for group in tests for c in group\n"
                  "                              if type(c).__name__ == 'Target' and c._testMethodName == 'test_own')\n"),
    "tests/__init__.py": "",
    "tests/test_sample.py": ("import unittest\n\n\nclass Parent(unittest.TestCase):\n"
                             "    def test_inherited(self):\n        self.fail('must not be omitted')\n\n\n"
                             "class Target(Parent):\n    def test_own(self):\n        pass\n\n\n"
                             "from helper import choose as load_tests  # noqa: E402\n"),
    "tests/test_conditional.py": ("import unittest\n\n\nclass Parent(unittest.TestCase):\n"
                                  "    def test_inherited(self):\n        self.fail('must not be omitted')\n\n\n"
                                  "class Target(Parent):\n    def test_own(self):\n        pass\n\n\n"
                                  "if True:\n    from helper import choose\n\n    def load_tests(loader, tests, pattern):\n"
                                  "        return choose(loader, tests, pattern)\n"),
    "tests/test_cfg.py": ("class Base:\n    def test_inherited(self):\n        assert False\n\n\n"
                          "class TestTarget(Base):\n    def test_own(self):\n        pass\n"),
    "tests/test_dyn.py": ("import unittest\n\n\nclass Parent(unittest.TestCase):\n"
                          "    def test_inherited(self):\n        pass\n\n\n"
                          "class Dyn(Parent):\n    def test_own(self):\n        pass\n\n\n"
                          "def _extra(self):\n    pass\n\n\nsetattr(Dyn, 'test_dyn', _extra)\n"),
    "custom.ini": "[pytest]\npython_functions = test_own\n",
}
PY = sys.executable
TOKEN = "--quadratus-report={report}"


def _real_run(tmp_path, gate_argv, item, *, files=ROOT_FILES, ini=None):
    """A real IntegrationGate run under the shipped producer, recorded the
    way the session records a check, against a NOT RUN item bound on that
    project; returns (session, entry, check)."""
    root = _project(tmp_path, dict(files, **({"pytest.ini": ini} if ini else {})))
    gate = IntegrationGate(list(gate_argv), cwd=root)
    session = _session(tmp_path, project=root, gate=gate)
    entry = _entry(session, item)
    session.unexecuted_acceptance.append(entry)
    session._register_obligation(entry, kind="not_run")
    result = gate.run()
    from quadratus.integration import config_selection
    check = dict(passed=result.passed, command=result.command, output=result.output, cases=result.cases,
                 command_cwd=".", selection=config_selection(list(gate_argv), root, root), report=result.report,
                 interpreter=S._command_interpreter(result.command, root), receipts=[],
                 source=session._source_fingerprint(), gate=S._gate_identity(session._current_gate()),
                 environment=session._environment_id(), requirements={"R1": 1})
    session.checks.append(check)
    session._refresh_obligations()
    return session, entry, check


def _checker_clears(session, entry, check):
    truth = _truth()
    record = dict(source_fingerprint=session._source_fingerprint(), checks=[check], unexecuted_acceptance=[entry],
                  evidence_obligations=list(session.evidence_obligations))
    return truth._not_run_cleared(entry, [check], record)


@pytest.mark.parametrize("name, gate, item, ini, passed_case, omitted", [
    ("imported load_tests alias", (PY, "-m", "unittest", "tests.test_sample", TOKEN), "tests/test_sample.py::Target", None,
     "tests/test_sample.py::Target::test_own", "test_inherited"),
    ("conditional load_tests", (PY, "-m", "unittest", "tests.test_conditional", TOKEN), "tests/test_conditional.py::Target", None,
     "tests/test_conditional.py::Target::test_own", "test_inherited"),
    ("pytest -c custom.ini python_functions", (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_cfg.py", TOKEN, "-c", "custom.ini"),
     "tests/test_cfg.py::TestTarget", None, "tests/test_cfg.py::TestTarget::test_own", "test_inherited"),
    ("default pytest.ini python_functions", (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_cfg.py", TOKEN),
     "tests/test_cfg.py::TestTarget", "[pytest]\npython_functions = test_own\n", "tests/test_cfg.py::TestTarget::test_own", "test_inherited"),
])
def test_root_s_real_runner_negatives_hold_in_the_engine_and_the_checker(tmp_path, name, gate, item, ini, passed_case, omitted):
    session, entry, check = _real_run(tmp_path, gate, item, ini=ini)
    report = check["report"]
    assert check["passed"] and report["state"] == "parsed", (name, report)
    assert report["counts"] == dict(passed=1, failed=0, errors=0, skipped=0) and report["passed_cases"] == [passed_case], name
    assert report["classes_truncated"] is False
    attested = {c["id"]: c["members"] for c in report["classes"]}
    assert attested[item] == sorted([omitted, "test_own"]), (name, attested)
    assert entry["binding"]["kind"] == "class" and entry["cases"] == [f"{item}::test_own"]
    assert session._discharge_record(entry) is None, name
    assert entry["discharged_at"] is None and entry["discharge_display"] == "bound/1"
    row = next(o for o in session.evidence_obligations if o["kind"] == "not_run")
    assert row["execution"] != "ran", (name, row)
    assert not _checker_clears(session, entry, check), name


@pytest.mark.parametrize("name, gate, item, members", [
    ("unittest module, inherited and dynamic members", (PY, "-m", "unittest", "tests.test_dyn", TOKEN),
     "tests/test_dyn.py::Dyn", ["test_dyn", "test_inherited", "test_own"]),
    ("pytest file, inherited and dynamic members", (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_dyn.py", TOKEN),
     "tests/test_dyn.py::Dyn", ["test_dyn", "test_inherited", "test_own"]),
    ("pytest whole discovery", (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_dyn.py", "tests/test_cfg.py", TOKEN),
     "tests/test_dyn.py::Dyn", ["test_dyn", "test_inherited", "test_own"]),
])
def test_a_real_whole_run_attests_inherited_and_dynamic_members_and_discharges_the_class(tmp_path, name, gate, item, members):
    files = {k: v for k, v in ROOT_FILES.items() if k in ("tests/__init__.py", "tests/test_dyn.py", "tests/test_cfg.py")}
    files["tests/test_cfg.py"] = "class TestOther:\n    def test_fine(self):\n        pass\n"
    session, entry, check = _real_run(tmp_path, gate, item, files=files)
    report = check["report"]
    assert check["passed"] and report["state"] == "parsed" and report["counts"]["failed"] == 0, (name, report)
    attested = {c["id"]: c["members"] for c in report["classes"]}
    assert attested[item] == members, (name, attested)
    assert entry["cases"] == [f"{item}::test_own"], "the floor is the one body-declared method"
    decided = session._discharge_record(entry)
    assert decided is not None and decided["cases"] == [f"{item}::{m}" for m in members], (name, decided)
    assert entry["discharged_at"] == 0 and entry["discharged_cases"] == decided["cases"]
    row = next(o for o in session.evidence_obligations if o["kind"] == "not_run")
    assert row["execution"] == "ran", (name, row)
    assert _checker_clears(session, entry, check), name


# -- 10. optional reporting never damages the run it observes (root's review of 55e07d4) -----------

META_FILE = ("import unittest\n\n\nclass Meta(type):\n    calls = 0\n\n    def __dir__(cls):\n"
             "        cls.calls += 1\n        if cls.calls > 1:\n"
             "            raise RuntimeError('metadata enumeration not repeatable')\n        return super().__dir__()\n\n\n"
             "class Target(unittest.TestCase, metaclass=Meta):\n    def test_ok(self):\n        pass\n")
GHOST_FILE = ("import unittest\n\n\nGhost = type('Ghost', (unittest.TestCase,), "
              "{'test_g': lambda self: None, '__module__': 'nowhere.at.all'})\n\n\n"
              "class Placed(unittest.TestCase):\n    def test_p(self):\n        pass\n")


def _big_module(classes=20, members=1000):
    """Root's size fixture: within both count bounds, about 1.8 MB of
    member names; ``load_tests`` keeps the first method of each class."""
    lines = ["import unittest", "", ""]
    for c in range(classes):
        lines += [f"class C{c:02d}(unittest.TestCase):", "    pass", ""]
    lines += ["", "def _ok(self):", "    pass", "", "",
              "for _cls in [" + ", ".join(f"C{c:02d}" for c in range(classes)) + "]:",
              f"    for _i in range({members}):",
              "        setattr(_cls, 'test%04d_details_%s' % (_i, 'x' * 70), _ok)",
              "del _cls", "", "",
              "def load_tests(loader, tests, pattern):",
              "    suite = unittest.TestSuite()",
              "    for group in tests:",
              "        first = next(iter(group), None)",
              "        if first is not None:",
              "            suite.addTest(first)",
              "    return suite", ""]
    return "\n".join(lines)


@pytest.mark.parametrize("runner", ["unittest", "pytest"])
def test_a_metaclass_that_breaks_on_enumeration_does_not_break_the_run_and_the_class_is_still_attested(tmp_path, runner):
    """Root's P2 fixture: ``__dir__`` raises on its second call. The
    producer never calls ``__dir__`` (the membership is read from the MRO
    dictionaries through ``getattr_static``), so the run passes, the report
    parses, the class is attested from what the loader collected and a
    claim on it discharges."""
    gate = ((PY, "-m", "unittest", "tests.test_meta", TOKEN) if runner == "unittest"
            else (PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_meta.py", TOKEN))
    session, entry, check = _real_run(tmp_path, gate, "tests/test_meta.py::Target",
                                      files={"tests/__init__.py": "", "tests/test_meta.py": META_FILE})
    report = check["report"]
    assert check["passed"] and report["state"] == "parsed", (runner, report, check["output"])
    assert report["counts"] == dict(passed=1, failed=0, errors=0, skipped=0)
    assert report["passed_cases"] == ["tests/test_meta.py::Target::test_ok"]
    assert report["classes"] == [dict(id="tests/test_meta.py::Target", members=["test_ok"])] and report["classes_truncated"] is False
    assert session._discharge_record(entry) is not None and _checker_clears(session, entry, check)


def test_a_class_the_producer_cannot_place_or_read_attests_nothing_and_the_run_is_intact(tmp_path):
    session, entry, check = _real_run(tmp_path, (PY, "-m", "unittest", "tests.test_ghost", TOKEN), "tests/test_ghost.py::Placed",
                                      files={"tests/__init__.py": "", "tests/test_ghost.py": GHOST_FILE})
    report = check["report"]
    assert check["passed"] and report["state"] == "parsed", (report, check["output"])
    assert report["counts"] == dict(passed=2, failed=0, errors=0, skipped=0)
    assert sorted(report["passed_cases"]) == ["nowhere.at.all.Ghost.test_g", "tests/test_ghost.py::Placed::test_p"], \
        "a class whose module cannot be placed keeps unittest's dotted id"
    assert report["classes"] == [dict(id="tests/test_ghost.py::Placed", members=["test_p"])], "Ghost is not attested"
    assert session._discharge_record(entry) is not None, "the placed class is still discharged"


def test_metadata_within_the_count_bounds_never_costs_the_core_report(tmp_path):
    """Root's P2 size fixture: 20 classes of 1000 long-named members are
    within MAX_CLASSES and MAX_CLASS_MEMBERS and serialize to about 1.8
    MB; ``load_tests`` runs 20 methods. The producer drops the attestation
    and flags it, the core report parses with its 20 passes, nothing is
    attested, and a class claim on the deliberately partial run stays
    held."""
    session, entry, check = _real_run(tmp_path, (PY, "-m", "unittest", "tests.test_big", TOKEN), "tests/test_big.py::C00",
                                      files={"tests/__init__.py": "", "tests/test_big.py": _big_module()})
    report = check["report"]
    assert check["passed"] and report["state"] == "parsed", (report.get("detail"), check["output"][-300:])
    assert report["counts"] == dict(passed=20, failed=0, errors=0, skipped=0) and len(report["passed_cases"]) == 20
    assert report["classes"] == [] and report["classes_truncated"] is True
    assert entry["binding"]["kind"] == "class" and entry["cases"] == [], "setattr members declare no body floor"
    assert entry["binding"]["problem"].startswith("C00 declares no test case in its body")
    assert session._discharge_record(entry) is None and not _checker_clears(session, entry, check)


def _producer(name):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(f"qp_{name}", Path("quadratus/_gate_producer") / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["quadratus_unittest_report", "quadratus_gate_report"])
def test_the_producers_read_membership_statically_and_an_unreadable_class_attests_nothing(name):
    producer = _producer(name)
    import unittest

    class Base(unittest.TestCase):
        def test_inherited(self):
            pass

        @staticmethod
        def test_static():
            pass

        @property
        def test_property(self):
            return 1

        test_attr = 3

        class TestInner:
            def test_nested(self):
                pass

    class Child(Base):
        def test_own(self):
            pass

    class Dir(type):
        def __dir__(cls):
            raise RuntimeError("never called")

    class Loud(unittest.TestCase, metaclass=Dir):
        def test_loud(self):
            pass

    class Broken(type):
        __mro__ = property(lambda cls: (_ for _ in ()).throw(RuntimeError("unreadable")))

    class Unreadable(unittest.TestCase, metaclass=Broken):
        def test_u(self):
            pass
    assert producer._class_members(Child) == ["test_inherited", "test_own", "test_static"], \
        "inherited and static members count; a property, a plain attribute and a nested class do not"
    assert producer._class_members(Loud) == ["test_loud"], "__dir__ is never called"
    assert producer._class_members(Unreadable) is None, "an unreadable class attests nothing, never a smaller set"
    assert producer.MAX_REPORT_BYTES_WITH_CLASSES < 1024 * 1024


def test_the_unittest_producer_s_whole_attestation_pass_is_contained(monkeypatch):
    import unittest
    producer = _producer("quadratus_unittest_report")

    class One(unittest.TestCase):
        def test_one(self):
            pass
    recorder = producer._Recorder()
    recorder.attest(unittest.TestSuite([One("test_one")]))
    assert recorder.classes and recorder.classes_truncated is False
    recorder = producer._Recorder()
    monkeypatch.setattr(producer, "_class_members", lambda kind: (_ for _ in ()).throw(RuntimeError("boom")))
    recorder.attest(unittest.TestSuite([One("test_one")]))
    assert recorder.classes == [] and recorder.classes_truncated is True, "a failure of the pass attests nothing and raises nothing"


def test_the_parser_accepts_a_dropped_attestation_and_the_engine_reads_it_as_none():
    from quadratus.integration import _malformed
    base = dict(producer="quadratus-unittest/3", exitstatus=0, collected=2, collect_errors=0,
                counts=dict(passed=2, failed=0, errors=0, skipped=0), failures=[], truncated=False,
                passed_cases=["tests/test_app.py::AddTests::test_add", "tests/test_app.py::AddTests::test_zero"],
                passed_truncated=False)
    assert _malformed(dict(base, classes=[], classes_truncated=True)) == "", "dropped and flagged is a valid report"
    assert _malformed(dict(base, classes=[dict(id=CLASS_ID, members=["test_add"])], classes_truncated=True)) == "", \
        "a flagged partial list is valid too, and attests nothing"
    assert _malformed(dict(base, classes=[dict(id=CLASS_ID, members=["test_add"])], classes_truncated=False)) == ""
    assert _malformed(dict(base, classes=[dict(id=CLASS_ID, members=[])], classes_truncated=False)) == "a class record"
    assert _malformed(dict(base, classes=[dict(id=CLASS_ID, members=["nope"])], classes_truncated=False)) == "a class record"
    assert _malformed(dict(base, classes=[dict(id=CLASS_ID, members=["test_a"])] * 2, classes_truncated=False)) == "duplicate class records"
    assert _malformed(dict(base, classes="x", classes_truncated=False)) == "classes"
    assert _malformed(base) == "", "a report without the field is valid and attests nothing"
    assert S._report_classes(dict(state="parsed", classes=[dict(id=CLASS_ID, members=["test_add"])], classes_truncated=True)) == {}
    assert S._report_classes(dict(state="parsed", classes=[dict(id=CLASS_ID, members=["test_add"])], classes_truncated=False)) == {CLASS_ID: ["test_add"]}


def test_a_contradictory_saved_command_clears_nothing_in_either_consumer(tmp_path):
    """Root's record-consistency controls: a true complete attested receipt
    whose saved command was replaced by ``python -c unrelated`` or by a
    method selection ``python -m unittest tests.test_app.AddTests.test_add``.
    Neither command could have produced that report: the first starts no
    runner with a producer, the second selects inside a module the report
    names. A cheap command/report consistency check refuses both without
    reading membership from argv; the plain module form still clears."""
    from quadratus.integration import report_consistent_command
    root, session = _class_session(tmp_path)
    entry = _entry(session, CLASS_ID)
    session.unexecuted_acceptance.append(entry)
    truth = _truth()
    for command in ("python3 -c unrelated", "python3 -m unittest tests.test_app.AddTests.test_add -v",
                    "python3 -m unittest tests.test_app.AddTests -v", "make test"):
        check = _class_check(session, WHOLE_FILE, command)
        session.checks = [check]
        assert session._discharge_record(entry) is None, command
        row = _suite_check([("check", command, WHOLE_FILE, True, False)])
        assert not truth._not_run_cleared(_class_entry(), [row], _record([row], [_class_entry()])), command
        assert not report_consistent_command(command.split(), WHOLE_FILE), command
    for command in ("python3 -m unittest tests.test_app -v", "python3 -m pytest -q tests/test_app.py", "python3 -m pytest -q"):
        session.checks = [_class_check(session, WHOLE_FILE, command)]
        assert session._discharge_record(entry) is not None, command
        row = _suite_check([("check", command, WHOLE_FILE, True, False)])
        assert truth._not_run_cleared(_class_entry(), [row], _record([row], [_class_entry()])), command
        assert report_consistent_command(command.split(), WHOLE_FILE), command
    assert report_consistent_command("python3 -m unittest tests.other_module".split(), WHOLE_FILE), \
        "an operand that names no reported module is left alone (no membership is read from argv)"
