"""The completion workflow batch (Davis, 2026-10-30) on the real controller
below the vendor CLIs, after evidence-authority-02 ended FindingsOpen with
every contract behaviour delivered: t3's RESOLVES of findings on closed work
was charged as a recovery, t4's real continuation spent the last one, t5
then re-did t4's work without taking it over, t6's reviewer deferred a
finding whose legacy text carried no id and outlived its typed record, and
the batch close stopped on t4 still owed with the audit never asked.

What these controls pin:

- a RESOLVES naming only findings on closed work is ordinary coverage: not
  charged, and the finding still settles only through its own reviewer;
- a task over the requirements or files of owed work without CONTINUES is
  sent back naming the route, and the explicit continuation is charged once;
- with the allowance spent, that overlap stops the run naming the owed
  work before any lead call; non-overlapping work still runs;
- CONTINUES is validated against the record before any call;
- a parallel child's deferral is one finding under one id: held out of the
  child's own rechecks, rechecked once on the merged source, and closed in
  every representation when its reviewer resolves it;
- a documented ``python -m unittest <module>`` NOT RUN item binds to that
  command and is shown undischarged while only pytest ran its cases;
- a real recovery still reaches the goal question and the independent
  audit with nothing automatic.
"""

import sys

import pytest

from quadratus.session import SurveyConfig, _resolved_interpreter
from tests.lifecycle import harness as H
from tests.lifecycle import replies
from tests.lifecycle.test_lifecycle_matrix import FILES, FIXED, Script
from tests.lifecycle.test_recovery_handoff import (
    BATCH,
    CONTINUE,
    GROK_CAP,
    README_DONE,
    REQS,
    TASK,
    TYPED,
    _script,
)
from tests.lifecycle.test_step2_recovery import _plan, _simple
from tests.lifecycle.test_survey_run import _survey

DUPLICATE = _simple("app.py", "Validate both arguments of add in app.py.", "R1")
README = _simple("README.md", "Document add in README.md.", "R2")
REQS2 = REQS + "R2: README documents add and states what it returns\n"
CODE_FINDING = "BLOCKING: add has no docstring | basis: R1 | impact: other"


def _leads(replay):
    return [c.task for c in replay.of("lead")]


def _charges(replay):
    return _survey(replay)["charges"]


# -- 1. ownership and the closure route, serially ---------------------------------------------------

@pytest.mark.requirements_ledger
def test_a_task_over_owed_work_without_continues_is_sent_back_and_the_continuation_is_charged_once(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, DUPLICATE, CONTINUE)),
                   files=FILES, max_tasks=3, record_complete=False, survey=SurveyConfig(recovery_tasks=2))
    assert _leads(replay) == ["t1", "t2"], "the duplicate never reached a lead; the continuation did"
    sent_back = replay.of("orchestrator")[2].prompt
    assert "TASK SENT BACK" in sent_back
    assert "t2 overlaps work still owed by t1 (requirements R1)" in sent_back
    assert "The harness never infers a takeover" in sent_back and "2 of 2 left" in sent_back
    assert "Owed work: t1: covers R1; files app.py; open findings RFT1-1" in replay.of("orchestrator")[1].prompt
    survey = _survey(replay)
    assert survey["recovery_used"] == 1
    assert [c["task"] for c in _charges(replay)] == ["t1", "t2"], "every dispatched task's decision is recorded"
    assert not _charges(replay)[0]["charged"] and _charges(replay)[0]["why"] == "ordinary coverage"
    charge = _charges(replay)[1]
    assert (charge["task"], charge["continues"], charge["recovers"], charge["charged"]) == ("t2", "t1", ["t1"], True)
    assert charge["why"] == "recovers the capped, failed or unmerged work of t1"
    assert (replay.project / "app.py").read_text() == TYPED
    assert "capped or failed task(s)" not in (replay.result.error or ""), replay.result.error


@pytest.mark.requirements_ledger
def test_with_the_allowance_spent_an_overlap_stops_the_run_naming_the_owed_work_before_any_call(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(GROK_CAP, orchestrator=_plan(REQS + TASK, DUPLICATE)),
                   files=FILES, max_tasks=3, record_complete=False, survey=SurveyConfig(recovery_tasks=0))
    assert _leads(replay) == ["t1"], "no lead call was spent on the duplicate"
    error = replay.result.error or ""
    assert error.startswith("SurveyAllowanceSpent: the recovery allowance of 0 task(s) is spent and t2 overlaps "
                            "work still owed by t1 (requirements R1)"), error
    assert "Owed work: t1: covers R1; files app.py; open findings RFT1-1" in error
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"] and _survey(replay)["recovery_used"] == 0
    planner = replay.of("orchestrator")[1].prompt
    assert "Recovery tasks (CONTINUES or RESOLVES) used: 0 of 0; none is left, and naming one stops the run" in planner
    assert "a task over its requirements stops the run before any call, serial or in a batch" in planner


@pytest.mark.requirements_ledger
def test_non_overlapping_work_still_runs_on_a_spent_allowance_and_the_stop_names_the_uncovered_requirement(tmp_path, monkeypatch):
    def lead(call, replay):
        if call.task == "t2":
            H.write(call, {"README.md": README_DONE})
            return 'Documented add.\nCHANGED: ["README.md"]'
        return _script(GROK_CAP, orchestrator=None).overrides.get("lead", None) or None
    base = _script(GROK_CAP, orchestrator=_plan(REQS2 + TASK, README))
    first_lead = base.overrides["lead"]

    def leads(call, replay):
        return lead(call, replay) if call.task == "t2" else first_lead(call, replay)
    base.overrides["lead"] = leads
    base.overrides["collaborator"] = (lambda inner: (lambda c, r: ("NO FINDINGS\nR2: MET - README.md\nREVIEW: COMPLETE"
                                                                  if c.task == "t2" else inner(c, r))))(
        base.overrides["collaborator"])
    replay = H.run(tmp_path, monkeypatch, base, files=FILES, max_tasks=3, record_complete=False,
                   survey=SurveyConfig(recovery_tasks=0))
    assert _leads(replay) == ["t1", "t2"], "the README task does not overlap t1 and ran"
    assert _survey(replay)["recovery_used"] == 0 and _charges(replay)[-1] == dict(
        task="t2", continues=None, resolves=[], owed=["t1"], recovers=[], recovery=False, charged=False,
        why="ordinary coverage")
    record = H.result_json(replay)
    assert record["turn_limited_tasks"] == ["t1"] and not replay.result.completed
    assert "t1" in replay.result.error, replay.result.error


@pytest.mark.parametrize("target, problem", [
    ("t1", "CONTINUES names t1, whose work is not owed (it closed); new work does not continue it."),
    ("t9", "CONTINUES names t9, a task this run has no record of."),
])
def test_a_continues_that_names_closed_or_unknown_work_is_sent_back_before_any_call(tmp_path, monkeypatch, target, problem):
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, DECL_T2
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(DECL_T1, DECL_T2 + f"\nCONTINUES: {target}", DECL_T2)),
                   files=FILES, max_tasks=3)
    assert _leads(replay) == ["t1", "t2"]
    sent_back = replay.of("orchestrator")[2].prompt
    assert "TASK SENT BACK" in sent_back and problem in sent_back
    assert replay.result.completed, "the corrected plan completes as before"


# -- 2. recovery accounting: ordinary coverage that resolves closed work is not charged ---------

@pytest.mark.requirements_ledger
def test_a_resolves_of_a_finding_on_closed_work_is_not_charged_and_settles_by_its_reviewer(tmp_path, monkeypatch):
    """t1 closes clean with a held finding (its two fix cycles spent); t2
    RESOLVES it with the allowance at zero. Before, the RESOLVES line alone
    stopped the run SurveyAllowanceSpent; it is ordinary coverage."""
    from tests.lifecycle.test_close_family import _debt_script, _plan_with
    from tests.lifecycle.test_workflow_repair import Seen
    seen = Seen()
    resolving = (_simple("app.py", "Add the negative case to tests/test_app.py.", "R1") + "\nRESOLVES: RFT1-1")
    resolving = resolving.replace('"permitted_paths": ["app.py"]', '"permitted_paths": ["app.py", "tests/test_app.py"]')
    script = _debt_script(seen, _plan_with(resolving, requirements="R1: add(a, b) returns the sum"))
    inner_lead, inner_recheck = script.overrides["lead"], script.overrides["recheck"]

    def lead(call, replay):
        if call.task == "t2":
            H.write(call, {"app.py": FIXED, "tests/test_app.py": FILES["tests/test_app.py"]
                           + "\n\ndef test_negative():\n    assert add(-1, -2) == -3\n"})
            return 'Added the negative case.\nCHANGED: ["tests/test_app.py"]'
        return inner_lead(call, replay)

    def recheck(call, replay):
        if call.task == "t2":
            return replies.recheck_reply(call.prompt, default="RESOLVED tests/test_app.py::test_negative covers it")
        return inner_recheck(call, replay)
    script.overrides["lead"], script.overrides["recheck"] = lead, recheck
    script.overrides["collaborator"] = (lambda inner: (lambda c, r: ("NO FINDINGS\nR1: MET - app.py\nREVIEW: COMPLETE"
                                                                    if c.task == "t2" else inner(c, r))))(
        script.overrides["collaborator"])
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=3, record_complete=False,
                   survey=SurveyConfig(recovery_tasks=0))
    assert _leads(replay) == ["t1", "t2"], replay.result.error
    assert "SurveyAllowanceSpent" not in (replay.result.error or ""), replay.result.error
    survey = _survey(replay)
    assert survey["recovery_used"] == 0 and survey["charges"][-1] == dict(
        task="t2", continues=None, resolves=["RFT1-1"], owed=[], recovers=[], recovery=False, charged=False,
        why="resolves findings on closed work (t1: RFT1-1): ordinary coverage, not charged")
    record = H.result_json(replay)
    finding = {r["fid"]: r for r in record["review_findings"]}["RFT1-1"]
    assert finding["closed"] and finding["history"][-1]["author"] == finding["author"], \
        "closed only by the reviewer who raised it, at t2's clean close"
    assert not any("RFT1-1" in text for text in record["open_findings"])
    assert record["review_resolutions"][0]["named"] == ["RFT1-1"]


# -- 3. one finding id through a parallel child's deferral ------------------------------------------

def _deferral_script(*, after_merge="RESOLVED the merged README states what add returns"):
    def lead(call, replay):
        if call.task == "t1":
            H.write(call, {"app.py": FIXED})
            return 'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nCHANGED: ["app.py"]'
        H.write(call, {"README.md": README_DONE})
        return 'Documented add.\nCHANGED: ["README.md"]'

    def collaborator(call, replay):
        if call.task == "t1":
            return (CODE_FINDING + "\nDEFERRED: t2 - README must state what add returns\n"
                    "R1: MET - app.py adds\nREVIEW: COMPLETE")
        return "NO FINDINGS\nR2: MET - README.md states what add returns\nREVIEW: COMPLETE"

    def revision(call, replay):
        H.write(call, {"app.py": 'def add(a, b):\n    """Return the sum."""\n    return a + b\n'})
        return 'Added the docstring.\nCHANGED: ["app.py"]'

    def recheck(call, replay):
        if "now merged" in call.prompt or "Rows the harness deferred" in call.prompt:
            return replies.recheck_reply(call.prompt, default=after_merge) + "\nP1: SHOWN test_add ran"
        return replies.recheck_reply(call.prompt, default="RESOLVED the docstring is there")

    script = Script(orchestrator=_plan(BATCH), lead=lead, collaborator=collaborator,
                    auditor=lambda c, r: "R1: MET - app.py\nR2: MET - README.md",
                    **{"requirements-review": lambda c, r: "COMPLETE"})
    script.overrides["revision"] = revision
    script.overrides["recheck"] = recheck
    script.overrides["gate-fix"] = lead
    return script


@pytest.mark.requirements_ledger
def test_a_deferral_is_held_out_of_the_child_s_recheck_and_closes_everywhere_on_the_merged_source(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _deferral_script(), files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    findings = [r for r in record["review_findings"] if r["task"] == "t1"]
    deferrals = {r["fid"]: r for r in findings if "(deferred to t2)" in r["text"]}
    codes = {r["fid"]: r for r in findings if r["claim"].startswith("add has no docstring")}
    assert deferrals and codes and len(deferrals) == len(codes), "one deferral and one code finding per reviewer"
    child = [c for c in replay.of("recheck") if "now merged" not in c.prompt and "Rows the harness deferred" not in c.prompt]
    merged = [c for c in replay.of("recheck") if "now merged" in c.prompt or "Rows the harness deferred" in c.prompt]
    assert child and all(set(replies.expected_finding_ids(c.prompt)) and
                         not set(replies.expected_finding_ids(c.prompt)) & set(deferrals) for c in child), \
        "the child's rechecks ask about the code findings only; a deferral is held for the merged source"
    assert len(merged) == len(deferrals)
    assert {fid for c in merged for fid in replies.expected_finding_ids(c.prompt)} == set(deferrals)
    for deferral in deferrals.values():
        assert deferral["closed"] and deferral["history"][-1]["author"] == deferral["author"]
        assert deferral["history"][-1]["role"] == "recheck"
    assert all(r["closed"] for r in codes.values())
    assert record["open_findings"] == [], "the legacy texts closed with the typed records"
    rows = [r for r in record["parallel_batches"][0]["post_merge_rechecks"] if r["task"] == "t1"][0]["rechecked"]
    assert sorted(fid for r in rows for fid in r["resolved"]) == sorted(deferrals)
    assert not (replay.result.error or "").startswith("FindingsOpen"), replay.result.error


@pytest.mark.requirements_ledger
def test_a_deferral_kept_on_the_merged_source_is_held_debt_under_its_own_id(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _deferral_script(after_merge="UNRESOLVED the README still says nothing"),
                   files=FILES, max_tasks=3, record_complete=False)
    record = H.result_json(replay)
    deferrals = [r for r in record["review_findings"] if r["task"] == "t1" and "(deferred to t2)" in r["text"]]
    assert deferrals and not any(r["closed"] for r in deferrals)
    texts = [t for t in record["open_findings"] if "deferred by" in t]
    assert sorted(t.split(":")[0] for t in texts) == sorted(r["fid"] for r in deferrals)
    assert all(t.startswith(f"{t.split(':')[0]}: Task t1: deferred by") for t in texts)
    assert not (replay.result.error or "").startswith("FindingsOpen"), \
        "a deferral the reviewer kept on the merged source is held review debt, not a batch stop"
    assert any(c["holds"] for c in record["close_decisions"] if c["site"] == "batch")


# -- 4. a documented unittest module command is bound to that command ----------------------------

@pytest.mark.requirements_ledger
def test_a_unittest_module_not_run_item_stays_undischarged_while_only_pytest_ran_its_cases(tmp_path, monkeypatch):
    item = "python3 -m unittest tests.test_app -v"

    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return (f'Implemented add.\nPROVES: P1 - tests/test_app.py::test_add\nNOT RUN: {item} - not on the approved '
                'check command list\nCHANGED: ["app.py"]')
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=_plan(REQS + TASK), lead=lead,
                                                 **{"requirements-review": lambda c, r: "COMPLETE"}),
                   files=FILES, max_tasks=2, record_complete=False)
    record = H.result_json(replay)
    entry = next(e for e in record["unexecuted_acceptance"] if e["item"] == item)
    assert entry["binding"]["kind"] == "command" and entry["binding"]["command"] == item
    assert entry["cases"] == ["tests/test_app.py::test_add"] and entry["binding"]["files"] == ["tests/test_app.py"]
    assert entry["discharge_display"] == "bound/1" and entry["discharged_at"] is None
    row = next(o for o in record["evidence_obligations"] if o["kind"] == "not_run" and o["task"] == "t1")
    assert row["runner"] is None and row["execution"] == "missing", "pytest ran the case; this command never ran"
    assert any(c.get("passed") for c in record["checks"]), "the pytest gate did pass on the delivered source"
    from tools import truth_check
    verdict = truth_check.judge(replay.result.run_dir, None)
    assert any("reported NOT RUN and no later check shows it ran: python3 -m unittest tests.test_app -v" in u
               for u in verdict["unverified"])
    assert not any("the engine shows the NOT RUN item" in u for u in verdict["unverified"]), "display and record agree"
    assert not replay.result.completed, "a standing NOT RUN item keeps its requirement from MET"


# -- 5. a real recovery still reaches the goal question and the audit -------------------------------

@pytest.mark.requirements_ledger
def test_a_charged_continuation_reaches_done_and_the_independent_audit(tmp_path, monkeypatch):
    from tests.lifecycle.test_step2_recovery import SIMPLE_SHOWN, simple_proofs
    base = _script(GROK_CAP, orchestrator=_plan(REQS + TASK, CONTINUE))
    base.overrides["auditor"] = replies.judging_scenarios(
        lambda c, r: "R1: MET - tests/test_app.py::test_add on the delivered app.py, which rejects a non-number",
                                                          *simple_proofs("R1"), basis=SIMPLE_SHOWN)
    replay = H.run(tmp_path, monkeypatch, base, files=FILES, max_tasks=3, record_complete=False,
                   survey=SurveyConfig(recovery_tasks=1))
    assert _leads(replay) == ["t1", "t2"] and _survey(replay)["recovery_used"] == 1
    assert replay.of("auditor"), "the audit was asked after the recovery"
    assert "DONE" in [c.reply for c in replay.of("orchestrator")][-1] or len(replay.of("orchestrator")) >= 3
    record = H.result_json(replay)
    assert record["turn_limited_tasks"] == ["t1"] and "t1" not in (record.get("survey") or {}).get("open", [])
    assert record["requirements"]["status"]["R1"].startswith("met"), record["requirements"]["status"]
    assert replay.result.completed, replay.result.error


# -- 6. root's correction (review of d48014d): declared unittest module runner, requirement-only overlap,
#       parallel intake admission, interpreter identity --------------------------------------------

UNITTEST_FILES = {
    "app.py": FILES["app.py"],
    "tests/test_app.py": ("import unittest\n\nfrom app import add\n\n\nclass AddTests(unittest.TestCase):\n"
                          "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n\n"
                          "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"),
    "README.md": FILES["README.md"],
}
MODULE_CASES = ["tests/test_app.py::AddTests::test_add", "tests/test_app.py::AddTests::test_zero"]
MODULE_COMMAND = f"{sys.executable} -m unittest tests.test_app -v"
DECLARED_GATE = MODULE_COMMAND + " --quadratus-report={report}"
EXECUTABLE = _resolved_interpreter(sys.executable)


def _venv(root):
    import venv
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    return root


def _module_script(item, *, proves=MODULE_CASES[0]):
    def lead(call, replay):
        H.write(call, {"app.py": FIXED})
        return (f'Implemented add.\nPROVES: P1 - {proves}\nNOT RUN: {item} - not on the approved check '
                'command list\nCHANGED: ["app.py"]')
    return Script(orchestrator=_plan(REQS + TASK), lead=lead, **{"requirements-review": lambda c, r: "COMPLETE"})


def _entry_for(replay, item):
    return next(e for e in H.result_json(replay)["unexecuted_acceptance"] if e["item"] == item)


def _not_run_row(replay, task="t1"):
    return next(o for o in H.result_json(replay)["evidence_obligations"] if o["kind"] == "not_run" and o["task"] == task)


@pytest.mark.requirements_ledger
def test_the_declared_unittest_module_gate_discharges_the_command_item_end_to_end(tmp_path, monkeypatch):
    """Root's positive: the real gate runs the same interpreter,
    ``-m unittest tests.test_app -v --quadratus-report={report}``, from the
    project root; its own producer's report names the module's cases, its
    selection is established (whole modules), and that single run clears
    the item the lead could not run, in the engine and in the checker."""
    replay = H.run(tmp_path, monkeypatch, _module_script(MODULE_COMMAND), files=UNITTEST_FILES, max_tasks=2,
                   record_complete=False, check=DECLARED_GATE)
    record = H.result_json(replay)
    check = next(c for c in record["checks"] if c.get("passed"))
    receipt = check["receipts"][0] if check.get("receipts") else check
    assert receipt.get("selection", "") == "" and receipt["interpreter"] == EXECUTABLE
    assert receipt["report"]["state"] == "parsed" and sorted(receipt["report"]["passed_cases"]) == MODULE_CASES
    entry = _entry_for(replay, MODULE_COMMAND)
    assert entry["binding"]["kind"] == "command" and entry["binding"]["interpreter"] == EXECUTABLE
    assert entry["cases"] == MODULE_CASES and entry["discharge_display"] == "bound/1"
    assert entry["discharged_at"] is not None and entry["discharged_cases"] == MODULE_CASES
    row = _not_run_row(replay)
    assert row["runner"] == "gate" and row["execution"] == "ran", row
    from tools import truth_check
    verdict = truth_check.judge(replay.result.run_dir, None)
    assert not any("reported NOT RUN" in u or "the engine shows the NOT RUN item" in u for u in verdict["unverified"]), \
        verdict["unverified"]


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("case", ["undeclared report", "single method", "nonexistent interpreter", "file item"])
def test_the_declared_module_route_refuses_what_it_cannot_show(tmp_path, monkeypatch, case):
    item, check = MODULE_COMMAND, DECLARED_GATE
    if case == "undeclared report":
        check = MODULE_COMMAND
    elif case == "single method":
        item = f"{sys.executable} -m unittest tests.test_app.AddTests.test_add -v"
    elif case == "nonexistent interpreter":
        item = "/nonexistent/location/python -m unittest tests.test_app -v"
    elif case == "file item":
        item = "tests/test_app.py"
    replay = H.run(tmp_path, monkeypatch, _module_script(item), files=UNITTEST_FILES, max_tasks=2,
                   record_complete=False, check=check)
    record = H.result_json(replay)
    assert any(c.get("passed") for c in record["checks"]), "the gate itself passed on the delivered source"
    entry = _entry_for(replay, item)
    from tools import truth_check
    verdict = truth_check.judge(replay.result.run_dir, None)
    assert not any("the engine shows the NOT RUN item" in u for u in verdict["unverified"]), "display and record agree"
    if case == "file item":
        # An item naming the module's file is discharged by the declared
        # module run, whose operands are its files (root's note).
        assert entry["binding"]["kind"] == "paths" and entry["discharged_at"] is not None
        assert not any("reported NOT RUN" in u for u in verdict["unverified"])
        return
    assert entry["binding"]["kind"] == "command" and entry["discharged_at"] is None
    assert any(f"reported NOT RUN and no later check shows it ran: {item}"[:140] in u for u in verdict["unverified"])
    if case == "undeclared report":
        assert entry["cases"] == MODULE_CASES and _not_run_row(replay)["runner"] == "gate"
        assert _not_run_row(replay)["execution"] != "ran", "no parsed report, no case shown executed"
    else:
        assert entry["cases"] == [] and entry["binding"]["problem"]


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("shape", ["distinct venv", "same venv alias", "base for a venv command"])
def test_a_command_declared_in_another_environment_is_not_the_command_the_gate_ran(tmp_path, monkeypatch, shape):
    """Root's P2 reproduction, on the real controller: the configured gate
    runs in one environment with its declared report; the lead's NOT RUN
    item names the same arguments under another venv whose bin/python is
    a link to the same base executable. Realpath alone collapsed them and
    cleared a command that would fail there. Now only the same invocation
    environment discharges it; an alias inside the configured venv still
    does."""
    first, second = _venv(tmp_path / "first"), _venv(tmp_path / "second")
    gate_python = str(first / "bin" / "python")
    if shape == "distinct venv":
        item_python, cleared = str(second / "bin" / "python"), False
    elif shape == "same venv alias":
        item_python, cleared = str(first / "bin" / "python3"), True
    else:
        gate_python, item_python, cleared = sys.executable, str(first / "bin" / "python"), False
    item = f"{item_python} -m unittest tests.test_app -v"
    check = f"{gate_python} -m unittest tests.test_app -v --quadratus-report={{report}}"
    replay = H.run(tmp_path, monkeypatch, _module_script(item), files=UNITTEST_FILES, max_tasks=2,
                   record_complete=False, check=check)
    record = H.result_json(replay)
    passed = next(c for c in record["checks"] if c.get("passed"))
    receipt = passed["receipts"][0] if passed.get("receipts") else passed
    assert receipt["interpreter"] == _resolved_interpreter(gate_python) and receipt["report"]["state"] == "parsed"
    entry = _entry_for(replay, item)
    assert entry["binding"]["kind"] == "command" and entry["cases"] == MODULE_CASES
    assert entry["binding"]["interpreter"] == _resolved_interpreter(item_python)
    assert (entry["binding"]["interpreter"] == receipt["interpreter"]) is cleared
    assert (entry["discharged_at"] is not None) is cleared
    assert (_not_run_row(replay)["execution"] == "ran") is cleared and (_not_run_row(replay)["runner"] == "gate") is cleared
    from tools import truth_check
    verdict = truth_check.judge(replay.result.run_dir, None)
    assert any("reported NOT RUN" in u for u in verdict["unverified"]) is not cleared
    assert not any("the engine shows the NOT RUN item" in u for u in verdict["unverified"]), "display and record agree"


@pytest.mark.requirements_ledger
def test_first_coverage_of_another_requirement_in_the_same_file_runs_and_leaves_the_owed_task_owed(tmp_path, monkeypatch):
    """Root's control: t1 caps implementing R1 in app.py with recovery 0;
    t2 covers the untouched R2 with a documentation comment in app.py. The
    shared file is not a takeover: t2 runs, nothing is charged, t1 stays
    owed."""
    same_file = _simple("app.py", "Document the arguments and result of add in a comment in app.py.", "R2")
    base = _script(GROK_CAP, orchestrator=_plan(REQS2 + TASK, same_file))
    first_lead, first_review = base.overrides["lead"], base.overrides["collaborator"]

    def lead(call, replay):
        if call.task == "t2":
            from pathlib import Path
            current = Path(call.cwd, "app.py").read_text()
            H.write(call, {"app.py": current + "# add(a, b): the arguments a and b; returns their sum\n"})
            return 'Documented add in a comment.\nCHANGED: ["app.py"]'
        return first_lead(call, replay)
    base.overrides["lead"] = lead
    base.overrides["collaborator"] = lambda c, r: ("NO FINDINGS\nR2: MET - app.py comment\nREVIEW: COMPLETE"
                                                   if c.task == "t2" else first_review(c, r))
    replay = H.run(tmp_path, monkeypatch, base, files=FILES, max_tasks=3, record_complete=False,
                   survey=SurveyConfig(recovery_tasks=0))
    assert _leads(replay) == ["t1", "t2"], replay.result.error
    assert "TASK SENT BACK" not in replay.of("orchestrator")[1].prompt
    record = H.result_json(replay)
    assert record["turn_limited_tasks"] == ["t1"] and _survey(replay)["recovery_used"] == 0
    assert _charges(replay)[-1]["task"] == "t2" and not _charges(replay)[-1]["charged"]
    assert "t1" in record["survey"]["open"], "incidental edits in the same file never clear the owed work"
    assert not replay.result.completed and "t1" in (replay.result.error or ""), replay.result.error


def _batch(*blocks):
    return "PARALLEL\n" + "\n---\n".join(blocks)


README_R2 = _simple("README.md", "Document add in README.md.", "R2")
NOTES_R3 = _simple("NOTES.md", "Write NOTES.md about add.", "R3")
REQS3 = REQS2 + "R3: NOTES.md explains add\n"


@pytest.mark.requirements_ledger
@pytest.mark.parametrize("recovery", [0, 2])
def test_a_parallel_block_over_owed_work_meets_the_same_admission_before_any_writer(tmp_path, monkeypatch, recovery):
    """Root's control: t1 caps (real grok max_turn_requests); the next plan is
    PARALLEL with t2 finishing R1 in app.py without CONTINUES and t3 an
    ordinary README task. With the allowance spent the run stops naming the
    owed work; with allowance left the batch is sent back naming the route.
    No child's writer is paid either way."""
    base = _script(GROK_CAP, orchestrator=_plan(REQS2 + TASK, _batch(DUPLICATE, README_R2), CONTINUE))
    replay = H.run(tmp_path, monkeypatch, base, files=FILES, max_tasks=4, record_complete=False,
                   survey=SurveyConfig(recovery_tasks=recovery))
    record = H.result_json(replay)
    assert not record.get("parallel_batches"), "the batch never started"
    if recovery == 0:
        assert _leads(replay) == ["t1"], "no child writer ran"
        error = replay.result.error or ""
        assert error.startswith("SurveyAllowanceSpent: the recovery allowance of 0 task(s) is spent and t2 overlaps "
                                "work still owed by t1 (requirements R1)"), error
        assert "Owed work: t1: covers R1; files app.py" in error
    else:
        sent_back = replay.of("orchestrator")[2].prompt
        assert "BATCH SENT BACK" in sent_back and "t2: t2 overlaps work still owed by t1 (requirements R1)" in sent_back
        assert "The harness never infers a takeover" in sent_back
        assert _leads(replay) == ["t1", "t2"], "then the explicit continuation ran alone"
        assert _survey(replay)["recovery_used"] == 1


@pytest.mark.requirements_ledger
def test_an_independent_parallel_batch_still_runs_beside_owed_work(tmp_path, monkeypatch):
    base = _script(GROK_CAP, orchestrator=_plan(REQS3 + TASK, _batch(README_R2, NOTES_R3)))
    first_lead, first_review = base.overrides["lead"], base.overrides["collaborator"]

    def lead(call, replay):
        if call.task == "t1":
            return first_lead(call, replay)
        path = "README.md" if call.task == "t2" else "NOTES.md"
        H.write(call, {path: "# app\n\n`add(a, b)` returns the sum.\n"})
        return f'Documented add.\nCHANGED: ["{path}"]'
    base.overrides["lead"] = lead
    base.overrides["collaborator"] = lambda c, r: (first_review(c, r) if c.task == "t1" else "NO FINDINGS\n" + "".join(
        f"{rid}: MET - documented\n" for rid in ("R2", "R3") if f"{rid}: " in c.prompt) + "REVIEW: COMPLETE")
    replay = H.run(tmp_path, monkeypatch, base, files=FILES, max_tasks=4, record_complete=False,
                   survey=SurveyConfig(recovery_tasks=0))
    assert sorted(_leads(replay)) == ["t1", "t2", "t3"], replay.result.error
    record = H.result_json(replay)
    assert record["parallel_batches"] and record["parallel_batches"][0]["tasks"] == ["t2", "t3"]
    assert record["turn_limited_tasks"] == ["t1"] and _survey(replay)["recovery_used"] == 0
