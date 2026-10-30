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

import pytest

from quadratus.session import SurveyConfig
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
    assert "t2 overlaps work still owed by t1 (requirements R1; files app.py)" in sent_back
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
                            "work still owed by t1 (requirements R1; files app.py)"), error
    assert "Owed work: t1: covers R1; files app.py; open findings RFT1-1" in error
    assert H.result_json(replay)["turn_limited_tasks"] == ["t1"] and _survey(replay)["recovery_used"] == 0
    planner = replay.of("orchestrator")[1].prompt
    assert "Recovery tasks (CONTINUES or RESOLVES) used: 0 of 0; none is left, and naming one stops the run" in planner
    assert "a task over its requirements or files stops the run before any call" in planner


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
