"""Regression tests for the 2026-11-10 review, workstream loop_c: the
requirements ledger, the audit and the check accounting
(reports/session_second_half.md items 4, 5, 9, 10, 12, 17, 21, 22, 23).

Each test names its item. All offline: scripted replies, no vendor CLI."""

from collections import Counter
from types import SimpleNamespace

import pytest

from quadratus import completion_decision as C
from quadratus import session as S
from quadratus.artifacts import ArtifactStore
from quadratus.integration import GateCommand, GateReceipt, GateResult, GateSuite
from quadratus.memory import TaskMemory
from quadratus.outcome import TaskOutcome
from quadratus.providers import ProviderError
from quadratus.session import RunStalled, Session, SessionConfig
from tests.lifecycle.test_lifecycle_matrix import covered_met

pytestmark = pytest.mark.requirements_ledger

REQS = "REQUIREMENTS:\nR1: POST endpoint previews rows\nR2: UI button #btn-import-preview\nR3: must not write the store\n"
TASK = "KIND: backend simple\nBuild the parser.\nCOVERS: R1, R2, R3"
ALL_MET = "R1: MET - app.py\nR2: MET - app.py\nR3: MET - app.py"


class Script:
    """A fake invoke: orchestrator replies in order; an audit entry that is
    an exception is raised, as a failed auditor call would be."""

    def __init__(self, orchestrator, review="COMPLETE", audits=()):
        self.orchestrator, self.review, self.audits, self.prompts = list(orchestrator), review, list(audits), []

    def __call__(self, model, prompt, system=None, allow_writes=False):
        self.prompts.append((model, prompt))
        if "Compare the list with the goal" in prompt:
            return self.review
        if "independent auditor" in prompt:
            answer = self.audits.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if "The task cap for this run has been reached" in prompt:
            return "DONE"
        if "Name the single next task" in prompt:
            return self.orchestrator.pop(0) if self.orchestrator else "DONE"
        if "The task is finished" in prompt:
            return "SUMMARY: done\nREASONING: done"
        if "contributing an independent read" in prompt:
            return "NO FINDINGS\n" + covered_met(prompt)
        return "work"

    def asked(self, marker):
        return [p for _, p in self.prompts if marker in p]


def _run(tmp_path, script, max_tasks=8, **config):
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script, config=SessionConfig(**config))
    session.run(max_tasks=max_tasks)
    return session


def _covered(session):
    status = session.memory.ledger.requirement_status
    return all(status[r].startswith("covered") for r in ("R1", "R2", "R3"))


# -- item 4: an audit that did not happen is its own result ----------------------------

def test_item4_a_failed_audit_keeps_the_statuses_and_the_next_done_retries_it(tmp_path):
    script = Script([REQS + TASK, "DONE", "DONE"], audits=[ProviderError("auditor unreachable"), ALL_MET])
    session = _run(tmp_path, script)
    assert session.completed
    missed, ran = session.requirement_audits
    assert missed["ran"] is False and "ProviderError: auditor unreachable" in missed["result"]
    assert "verdicts" not in missed and all(v["met"] for v in ran["verdicts"].values())
    sent_back = [p for p in script.asked("Name the single next task") if "DONE SENT BACK" in p]
    assert len(sent_back) == 1 and "requirements audit did not run" in sent_back[0]
    assert "nothing needs covering again" in sent_back[0]
    assert not script.asked("not covered by any finished task"), "finished work is not sent to be covered again"
    assert not script.asked("found these requirements not met")
    assert len(script.asked("You are leading")) == 1, "no task was spent on the missed audit"
    assert session._audit_misses == 0, "an audit that ran ends the run of misses"


def test_item4_a_second_miss_stops_with_a_named_operator_stop_and_judges_nothing(tmp_path):
    script = Script([REQS + TASK, "DONE", "DONE", "DONE"],
                    audits=[ProviderError("down"), ProviderError("still down")])
    session = _run(tmp_path, script)
    assert not session.completed
    assert session.stop_reason.startswith("AuditUnavailable: the independent requirements audit did not run "
                                          "in 2 attempt(s) at DONE (the auditor call failed: ProviderError: "
                                          "still down)")
    stop = session.run_outcome.stop()
    assert (stop.kind, stop.legacy) == ("operator", "AuditUnavailable")
    assert _covered(session), "no requirement is written NOT MET by an audit that never ran"
    assert [a["ran"] for a in session.requirement_audits] == [False, False]
    assert len(script.asked("independent auditor")) == 2, "retried once, not more"
    assert len(script.asked("You are leading")) == 1


def test_item4_no_auditor_from_another_vendor_is_unavailable_not_unmet(tmp_path):
    script = Script([REQS + TASK, "DONE", "DONE", "DONE"])
    session = Session("Build the preview", ArtifactStore(tmp_path / "a"), script, config=SessionConfig())
    # The list was reviewed while a second vendor was reachable; none is by
    # the time DONE asks for the audit.
    session.requirement_reviews.append(dict(reviewer="openai:gpt-5.6-sol", result="reviewed"))
    session._auditor = lambda: None
    session.run(max_tasks=8)
    assert not session.completed and session.stop_reason.startswith("AuditUnavailable: ")
    assert "no auditor from another vendor available" in session.stop_reason
    assert _covered(session) and not script.asked("independent auditor")
    assert [a["auditor"] for a in session.requirement_audits] == [None, None]


def test_item4_a_miss_at_the_cap_is_the_same_stop_never_requirements_unmet(tmp_path):
    script = Script([REQS + TASK], audits=[ProviderError("down")])
    session = _run(tmp_path, script, max_tasks=1)
    assert not session.completed
    assert session.stop_reason.startswith("AuditUnavailable: ") and "task cap (1)" in session.stop_reason
    assert "RequirementsUnmet" not in session.stop_reason and _covered(session)
    assert session.run_outcome.done_accepted is False


def _project_session(tmp_path, reply, files=None):
    root = tmp_path / "project"
    root.mkdir(parents=True)
    for name, text in (files or {"app.py": "x = 1\n"}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True, requirements_ledger=True))
    ledger = session.memory.ledger
    ledger.requirements.update({"R1": "typing filters the cards", "R5": "no console errors while typing"})
    ledger.requirement_status.update({"R1": "covered by t1", "R5": "covered by t3"})
    session.requirement_reviews.append(dict(reviewer="openai:gpt-5.6-sol", result="reviewed"))
    session._auditor = lambda: "openai:gpt-5.6-sol"
    prompts = []
    session._invoke_model = lambda key, prompt, **kw: prompts.append(prompt) or (
        reply(root) if callable(reply) else reply)
    return session, root, prompts


def test_item4_a_tree_that_moved_under_the_audit_is_void_and_writes_no_status(tmp_path):
    def moves(root):
        (root / "left_by_the_auditor.txt").write_text("x")
        return "R1: MET - app.py\nR5: MET - app.py"
    session, _, _ = _project_session(tmp_path, moves)
    assert session._requirements_satisfied() is False
    assert session._audit_unavailable == "void: the tree changed during the audit"
    assert session.memory.ledger.requirement_status == {"R1": "covered by t1", "R5": "covered by t3"}
    assert "audit did not run" in session._done_refusal
    # The merged record also keeps the development line's fields (the reply
    # artifact, the ids asked for, the omitted ids, the completion).
    void = session.requirement_audits[-1]
    assert {k: void[k] for k in ("auditor", "ran", "result")} == dict(
        auditor="openai:gpt-5.6-sol", ran=False, result="void: the tree changed during the audit")
    assert "verdicts" not in void and void["omitted"] == [] and void["completion"] is None


def test_item4_a_reply_with_no_verdict_at_all_judged_nothing_but_one_missing_line_is_that_ones(tmp_path):
    session, _, _ = _project_session(tmp_path, "I could not open the working directory.")
    verdicts = session._audit_requirements()
    assert session._audit_unavailable.startswith("the auditor's reply held no verdict")
    assert all(ok is False for ok, _ in verdicts.values()), "the caller still gets nothing it could read as met"
    assert session.requirement_audits[-1]["ran"] is False
    assert session.requirement_audits[-1]["reply"].startswith("I could not open")
    session._invoke_model = lambda key, prompt, **kw: "R1: MET - app.py"
    verdicts = session._audit_requirements()
    assert session._audit_unavailable == "" and session._audit_misses == 0
    assert verdicts == {"R1": (True, "app.py"), "R5": (False, "the auditor gave no verdict")}
    assert session._requirements_satisfied() is False
    assert session.memory.ledger.requirement_status["R5"].startswith("NOT MET"), "a ran audit still writes"


def _inputs(site=C.DONE_REPLY, **kw):
    from tests.test_completion_decision import inputs
    return inputs(site, **kw)


@pytest.mark.parametrize("fields, status", [
    (dict(audit_misses=1), C.SEND_BACK),
    (dict(audit_misses=2), C.OPERATOR_STOP),
    (dict(audit_misses=1, reopens=3, max_reopens=3), C.OPERATOR_STOP),
    (dict(audit_misses=1, reopens=0, max_reopens=0), C.OPERATOR_STOP),   # an explicit list: nobody to ask again
])
def test_item4_the_decision_sends_back_once_then_stops_for_the_operator(fields, status):
    decision = C.decide(_inputs(requirements_satisfied=False, audit_unavailable="no auditor", **fields))
    assert decision.status == status and decision.completed is False
    if status == C.SEND_BACK:
        assert decision.reopen_fact == "DONE sent back: the requirements audit did not run" and decision.stop is None
    else:
        assert (decision.stop.kind, decision.stop.name) == ("operator", "AuditUnavailable")
        assert "No requirement was judged met or not met" in decision.stop.reason


def test_item4_the_decision_at_the_cap_and_what_it_refuses():
    decision = C.decide(_inputs(C.CAP, goal_confirmed=True, requirements_satisfied=False,
                                audit_unavailable="the auditor call failed", audit_misses=1))
    assert decision.status == C.OPERATOR_STOP and decision.done_accepted is False
    assert decision.stop.name == "AuditUnavailable" and "task cap (3)" in decision.stop.reason
    with pytest.raises(ValueError, match="did not run"):
        C.decide(_inputs(requirements_satisfied=True, dependencies_verified=True, audit_unavailable="x"))


# -- item 5: one count per send-back kind, cleared at dispatch --------------------------

def _part(n, covers="R1, R2, R3", extra=""):
    return f"KIND: backend simple\nBuild part {n}.\nCOVERS: {covers}{extra}"


def test_item5_send_backs_corrected_first_time_never_add_up_to_a_stall(tmp_path):
    replies = [REQS + _part(1, "R9"), _part(1)]
    for n in range(2, 6):
        replies += [_part(n, "R9"), _part(n)]
    script = Script(replies + ["DONE"], audits=[ALL_MET])
    session = _run(tmp_path, script, max_tasks=12, max_requirement_reopens=3)
    assert session.completed and len(script.asked("You are leading")) == 5
    assert len([p for p in script.asked("Name the single next task") if "TASK SENT BACK" in p]) == 5
    assert not session._covers_corrections, "cleared when the last task was dispatched"


def test_item5_two_kinds_in_a_row_are_counted_apart(tmp_path):
    replies = [REQS + _part(1, "R9"), _part(1, extra="\nRESOLVES: F9"), _part(1), "DONE"]
    session = _run(tmp_path, Script(replies, audits=[ALL_MET]), max_requirement_reopens=1)
    assert session.completed, "one bad COVERS and one bad RESOLVES are one of each, not two of one"


def test_item5_the_limit_and_the_message_stay_for_one_kind(tmp_path):
    script = Script([REQS + _part(1, "R9")] + [_part(1, "R9")] * 3)
    with pytest.raises(RunStalled, match="kept naming tasks without valid COVERS: COVERS names requirements "
                                         "that do not exist: R9"):
        _run(tmp_path, script, max_requirement_reopens=2)
    assert not script.asked("You are leading")


# -- item 9: a failed check with no failed command id is resolvable ---------------------

def _debt_session(tmp_path, receipts):
    session = Session("g", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(requirements_ledger=True))
    session.memory.ledger.requirements["R1"] = "x"
    outcome = TaskOutcome(task_id="t1", intent="implementation")
    outcome.checks = [dict(passed=False, attempt=2, gate="full", output_artifact="a1", receipts=receipts)]
    session.task_outcomes.append(outcome)
    fid = session._record_check_debt(SimpleNamespace(task_id="t1"), ["R1"])
    session._current_resolves = [fid]
    return session, session.findings[-1]


def _gate(passed, **statuses):
    return dict(passed=passed, command="gate suite",
                receipts=[dict(id=i, status=s) for i, s in statuses.items()])


def test_item9_a_whole_gate_failure_records_a_sentinel_a_whole_passing_gate_settles(tmp_path):
    moved = [dict(id="py", status="passed"), dict(id="node", status="passed")]
    assert S._check_ids(moved, passed=False) == [], "the tree moved: every receipt still reads passed"
    session, finding = _debt_session(tmp_path, moved)
    assert finding["check"]["failed_checks"] == [S._WHOLE_GATE]
    assert finding["check"]["gate_checks"] == ["py", "node"]
    assert "no single command, the gate as a whole" in finding["message"]
    repair = SimpleNamespace(task_id="t2")
    session._resolve_check_findings(repair, [_gate(False, py="passed", node="passed")])
    assert finding["status"] == "open", "a gate whose tree moved again is not a pass"
    assert finding["last_attempt"] == "t2: none of its checks passed as a whole with node, py rerun"
    session._resolve_check_findings(repair, [_gate(True, py="passed")])
    assert finding["status"] == "open", "a smaller gate did not rerun what failed"
    session._resolve_check_findings(repair, [_gate(True, py="passed", node="passed", scope="passed")])
    assert finding["status"] == "resolved" and finding["resolved_by"] == "t2"
    assert finding["resolution"]["checks"] == [S._WHOLE_GATE]


def test_item9_only_the_scope_receipt_failing_is_the_same_case(tmp_path):
    session, finding = _debt_session(tmp_path, [dict(id="check", status="passed"),
                                                 dict(id="scope", status="failed")])
    assert finding["check"]["failed_checks"] == [S._WHOLE_GATE] and finding["check"]["gate_checks"] == ["check"]
    session._resolve_check_findings(SimpleNamespace(task_id="t2"), [_gate(True, check="passed", scope="passed")])
    assert finding["status"] == "resolved"


def test_item9_a_named_failed_command_resolves_as_before(tmp_path):
    session, finding = _debt_session(tmp_path, [dict(id="py", status="passed"), dict(id="node", status="failed")])
    assert finding["check"]["failed_checks"] == ["node"] and "gate_checks" not in finding["check"]
    assert finding["message"].endswith("failed: node)")
    session._resolve_check_findings(SimpleNamespace(task_id="t2"), [_gate(True, py="passed")])
    assert finding["last_attempt"] == "t2: its checks did not rerun node with a passing receipt"
    session._resolve_check_findings(SimpleNamespace(task_id="t2"), [_gate(True, py="passed", node="passed")])
    assert finding["status"] == "resolved"
    single, one = _debt_session(tmp_path / "single", [])
    assert one["check"]["failed_checks"] == ["check"], "a single-command gate is still the check 'check'"


# -- item 10: added requirements are numbered past the highest id ----------------------

def test_item10_an_added_requirement_never_overwrites_a_numbered_one(tmp_path):
    gapped = "REQUIREMENTS:\nR1: POST endpoint previews rows\nR3: UI button\nR4: must not write the store\n"
    script = Script([gapped + "KIND: backend simple\nBuild the parser.\nCOVERS: R1", "DONE"],
                    review="ADD: docs/CSV_IMPORT.md documents the format\nADD: the README links to it")
    ledger = _run(tmp_path, script, max_requirement_reopens=0).memory.ledger
    assert ledger.requirements["R4"] == "must not write the store", "the orchestrator's R4 is kept"
    assert ledger.requirements["R5"] == "docs/CSV_IMPORT.md documents the format"
    assert ledger.requirements["R6"] == "the README links to it"
    assert list(ledger.requirements) == ["R1", "R3", "R4", "R5", "R6"]
    assert not str(ledger.requirement_status.get("R4", "")).startswith("open (added")


# -- item 12: an undischargeable NOT RUN report says so --------------------------------

SCENARIO = "tests/browser/scenarios/09-project-search.js"


def _report(session, task, covers, line):
    session._active_spec = SimpleNamespace(task_id=task)
    session._current_covers = list(covers)
    session._record_not_run("openai:gpt-5.6-sol", "lead", line)
    session._active_spec, session._current_covers = None, []


def test_item12_a_report_that_names_no_file_is_called_unliftable_not_fixable(tmp_path):
    # Changed 2026-11-12 (merge, exception 1): the development line's
    # restatement route (RUNS lines, a DISCHARGES task) does lift such a
    # report, so the refusal names that route and no longer says nothing can.
    assert S._item_paths("the browser scenario") == []
    session, _, _ = _project_session(tmp_path, "R1: MET - app.py\nR5: MET - app.py")
    _report(session, "t3", ["R5"], "NOT RUN: the browser scenario - no playwright")
    assert session._requirements_satisfied() is False
    refusal = session._done_refusal
    assert "- R5: acceptance reported NOT RUN by the covering task: t3: the browser scenario" in refusal
    assert ("No check runs it as written. It is lifted when its own task's lead, or the lead of a later task "
            "naming 'DISCHARGES: O1', restates it with 'RUNS: O1 - file::[Class::]test_case' lines") in refusal
    assert "No task lifts the report" not in refusal and "stands for the rest of this run" not in refusal
    assert session.memory.ledger.requirement_status["R5"].startswith("NOT MET"), "the lowering rule is unchanged"
    # The next DONE finds R5 uncovered; that refusal carries the same signpost.
    assert session._requirements_satisfied() is False
    assert "not covered by any finished task: R5" in session._done_refusal
    assert "naming 'DISCHARGES: O1'" in session._done_refusal


def test_item12_a_report_that_names_a_file_says_what_lifts_it(tmp_path):
    session, _, _ = _project_session(tmp_path, "R1: NOT MET - no filter\nR5: MET - app.py")
    _report(session, "t3", ["R5"], f"BLOCKED: node {SCENARIO} - not an approved command")
    assert session._requirements_satisfied() is False
    refusal = session._done_refusal
    assert (f"Lifted only when a later required check passes while naming {SCENARIO} as a whole file, "
            "with no filter and nothing skipped.") in refusal
    assert "Name a task that fixes them (with COVERS)." in refusal and "No task lifts" not in refusal
    assert "lifted by execution, never by covering its requirement again" in refusal


def test_item12_a_fixable_and_an_unliftable_requirement_are_told_apart(tmp_path):
    session, _, _ = _project_session(tmp_path, "R1: NOT MET - no filter\nR5: MET - app.py")
    _report(session, "t3", ["R5"], "NOT RUN: the browser scenario - no playwright")
    assert session._requirements_satisfied() is False
    # Changed 2026-11-12 (merge, exception 1): R5's report has a route, so
    # both are asked for and R5's line says which kind of task lifts it.
    assert "Name a task that fixes them (with COVERS)." in session._done_refusal
    assert "naming 'DISCHARGES: O1'" in session._done_refusal
    assert "No task lifts the report" not in session._done_refusal


def test_item12_a_skip_record_names_its_check_and_no_report_changes_nothing(tmp_path):
    session, _, _ = _project_session(tmp_path, "R1: MET - app.py\nR5: MET - app.py")
    report = dict(state="parsed", counts=dict(passed=3, failed=0, errors=0, skipped=2))
    receipt = GateReceipt(id="extra-1", status="passed", reason="exit 0", required=True,
                          command="node --test a.test.js", output="# pass 3\n# fail 0\n# skipped 2\n", report=report)
    session._current_covers = ["R5"]
    session._note_skips(SimpleNamespace(task_id="t4"), GateResult(True, "gate suite", 0, receipt.output, (receipt,)))
    session._current_covers = []
    assert session._requirements_satisfied() is False
    assert "Lifted when a later run of required check extra-1 passes with those cases executed and none " \
           "skipped." in session._done_refusal
    plain, _, _ = _project_session(tmp_path / "plain", "R1: NOT MET - no filter\nR5: MET - app.py")
    assert plain._requirements_satisfied() is False
    # The plain refusal is the development line's now (a basis label, a route
    # line and the RESOLVES rule after the sentence); a run with no report
    # still adds nothing about reports to it.
    assert plain._done_refusal.startswith("\n\n--- DONE SENT BACK ---\nAn independent audit found these "
                                          "requirements not met:\n- R1: no filter")
    assert "\nName a task that fixes them (with COVERS)." in plain._done_refusal
    assert plain._not_run_remedy(["R1"]) == ("", [])
    assert "NOT RUN report" not in plain._done_refusal and "No task lifts" not in plain._done_refusal
    # Nothing follows the instruction: its last sentence closes the refusal.
    assert plain._done_refusal.endswith(S._REPAIR_OUTCOME_RULE)


def test_item12_the_editing_call_is_told_the_rule_the_discharge_holds_it_to(tmp_path, monkeypatch):
    """Prompt parity: the real editing call's instructions state the rule,
    and the sentence and the parser agree on its own examples."""
    from quadratus.config import Settings
    from quadratus.delegation import invocation
    from quadratus.project import Project
    from quadratus.runtime import Fleet
    (tmp_path / "a.py").write_text("x = 1\n")
    fleet = Fleet(Settings(backend="cli"), project=Project(tmp_path), allow_writes=True)
    fleet.check_commands = ("python -m pytest -q",)
    provider = SimpleNamespace(restricted=False, in_directory=lambda *a, **kw: SimpleNamespace())
    seen = []
    monkeypatch.setattr(fleet, "provider_for", lambda key: provider)
    monkeypatch.setattr(fleet, "_generate", lambda *a: seen.append(a[3]) or "Done.\nCHANGED: []")
    try:
        with invocation("t1", "lead"):
            fleet.invoke("claude:opus", "edit", allow_writes=True)
    finally:
        fleet.close()
    told = " ".join(seen[0].split())
    # The development line's NOT_RUN_RULE names the item forms (cycle22).
    assert "'NOT RUN: <item> - <why>'" in told, "the line format is still told"
    # Changed 2026-11-12 (merge, exception 1): a scenario id, case or class is
    # a valid item that names no file, so the sentence no longer says a line
    # with no file path can never be matched.
    assert ("Where the item is a test file, name it alone or as the file a command runs "
            "(tests/ui/search.test.js, or node tests/browser/run.js)") in told
    assert ("A line in words only, such as 'the browser scenario', names nothing a check can run: no later "
            "check discharges it") in told
    assert "can never be matched" not in told and "must name the test file" not in told
    # The examples the sentence gives, read by the parser the discharge uses.
    assert S._item_paths("tests/ui/search.test.js") == ["tests/ui/search.test.js"]
    assert S._item_paths("node tests/browser/run.js") == ["tests/browser/run.js"]
    assert S._item_paths("the browser scenario") == []
    for marker in ("NOT RUN", "BLOCKED"):
        assert S._NOT_RUN_LINE.search(f"{marker}: node tests/browser/run.js - denied").group(1).startswith("node ")


# -- item 17: the unreadable-project fallback takes the arguments its callers pass -----

def test_item17_an_unreadable_project_counts_every_skip_instead_of_raising(tmp_path, monkeypatch):
    session, root, _ = _project_session(tmp_path, "", files={"a.test.js": "test('old', () => {});\n"})
    gate = GateSuite([GateCommand(id="ui", argv=("node", "--test", "a.test.js"))], cwd=root)
    key = session._command_keys(gate)["ui"]
    # A complete baseline in which 'old' was the one skipped case: only the
    # stable() predicate stands between it and "historical".
    session._start_skips = {key: (1, Counter({"old": 1}), Counter({"old": 1, "unit": 1}))}
    receipt = GateReceipt(id="ui", status="passed", reason="exit 0", required=True,
                          command="node --test a.test.js", output="# pass 1\n# fail 0\n# skipped 1\n",
                          report=dict(state="parsed", counts=dict(passed=1, failed=0, errors=0, skipped=1)),
                          cases=dict(skipped={"old": 1}, executed={"unit": 1}, other={}))

    def unreadable(self):
        raise ProviderError("Project source exceeds 100 MiB; select a smaller project folder.")
    monkeypatch.setattr("quadratus.project.Project.contents", unreadable)
    texts, stable = session._case_texts()
    assert texts == {} and stable("old", False) is False and stable("old") is False
    session._current_covers = ["R5"]
    session._note_skips(SimpleNamespace(task_id="t1"), GateResult(True, "gate suite", 0, receipt.output, (receipt,)),
                        gate)
    (entry,) = session.unexecuted_acceptance
    assert entry["cases"] == ["old"] and entry["kind"] == "skipped", "nothing is stable, so the skip counts"


# -- item 21: a comment that names a test file does not run it -------------------------

NEW = "tests/ui/project_search.test.js"
NODE = ("node", "--test", "tests/ui/load_app.test.js")
PYTEST = ("python", "-m", "pytest", "-q")


def _unnamed(tmp_path, files):
    root = tmp_path / "project"
    (root / "tests" / "ui").mkdir(parents=True)
    (root / "tests" / "ui" / "load_app.test.js").write_text("// existing\n")
    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), lambda *a, **k: "",
                      config=SessionConfig(project=root, allow_writes=True))
    session._task_before = session._capture_source()
    (root / NEW).write_text("// new\n")
    for name, text in files.items():
        (root / name).write_text(text)
    gate = SimpleNamespace(commands=(GateCommand(id="check", argv=PYTEST), GateCommand(id="extra-1", argv=NODE)))
    session._note_unrun_tests(SimpleNamespace(task_id="t2"), TaskMemory("t2", "claude:opus", session.store), gate)
    return getattr(session, "unnamed_test_files", [])


@pytest.mark.parametrize("files", [
    {"tests/ui/load_app.test.js": "// see project_search.test.js for the search cases\nrequire('node:test');\n"},
    {"tests/ui/load_app.test.js": "/* moved to\n   project_search.test.js */\nconst t = require('node:test');\n"},
    {"tests/test_bridge.py": "# the node cases live in project_search.test.js\ndef test_x():\n    assert True\n"},
])
def test_item21_a_mention_in_a_comment_keeps_the_observation(tmp_path, files):
    assert _unnamed(tmp_path, files) == [NEW]


@pytest.mark.parametrize("files", [
    {"tests/ui/load_app.test.js": "// runs the search cases too\nrequire('./project_search.test.js');\n"},
    {"tests/test_bridge.py": "import subprocess\n# search cases\ndef test_search():\n"
                             "    subprocess.run(['node', '--test', 'tests/ui/project_search.test.js'], check=True)\n"},
    # What the scan cannot read for certain counts, as before: a regex
    # literal ends its certainty, and so does text that is not Python.
    {"tests/ui/load_app.test.js": "const ok = /a\\/\\/b/.test(x);\n// project_search.test.js\n"},
    {"tests/test_bridge.py": "def broken(:\n    '''\n# project_search.test.js\n"},
])
def test_item21_a_real_mention_or_an_unreadable_file_still_claims_nothing(tmp_path, files):
    assert _unnamed(tmp_path, files) == []


def test_item21_the_shared_lexical_step_reads_what_the_call_scan_read():
    text = "test('a', () => { /* ) */ return ')'; // )\n});\nrest"
    assert S._enclosing_call(text, text.index("'a'")) == text[:text.index(";\nrest")]
    assert S._enclosing_call("test('a', () => /x)/.test(y))", 5) is None, "a regex literal is still unknown"
    assert S._skip_lexeme("// tail", 0, 7) == 7 and S._skip_lexeme("'x' y", 0, 5) == 2
    assert S._skip_lexeme("`${x}`", 0, 6) is None and S._skip_lexeme("a / b", 2, 5) is None
    assert S._named_outside_comments("x = 1  # a.test.js\n", "a.test.js", True) is False
    assert S._named_outside_comments("x = 'a.test.js'  # a.test.js\n", "a.test.js", True) is True


# -- item 22: one index of test functions per audit, never a dependency tree ------------

TESTS = {
    "app.py": "x = 1\n",
    "tests/test_app.py": "def test_preview_writes_nothing():\n    pass\n\n\nclass TestApi:\n"
                         "    async def test_async_case(self):\n        pass\n",
    "node_modules/pkg/test_dep.py": "def test_only_in_a_dependency():\n    pass\n",
    ".venv/lib/test_site.py": "def test_only_in_the_venv():\n    pass\n",
}


def test_item22_dependency_trees_are_never_walked_and_names_resolve_exactly(tmp_path):
    session, _, _ = _project_session(tmp_path, "", files=TESTS)
    assert session._resolve_citations("test_preview_writes_nothing and test_async_case") == [
        "tests/test_app.py::test_preview_writes_nothing", "tests/test_app.py::test_async_case"]
    assert session._resolve_citations("test_only_in_a_dependency test_only_in_the_venv") == []
    assert session._resolve_citations("test_preview") == [], "a prefix of a test name is not that test"
    assert set(session._test_functions()) == {"test_preview_writes_nothing", "test_async_case"}


def test_item22_one_index_serves_a_whole_audit_and_the_next_audit_reads_afresh(tmp_path, monkeypatch):
    reply = "R1: MET - test_preview_writes_nothing and test_async_case\nR5: MET - test_async_case, test_added_later"
    session, root, _ = _project_session(tmp_path, reply, files=TESTS)
    built, real = [], Session._test_functions

    def counted(self):
        built.append(1)
        return real(self)
    monkeypatch.setattr(Session, "_test_functions", counted)
    verdicts = session._audit_requirements()
    assert verdicts["R1"][0] is True and verdicts["R5"][0] is True
    assert len(built) == 1, "four cited names, the lowering pass included, read the project once"
    assert session._audit_tests is None and session._auditing is False, "nothing is kept past the audit"
    (root / "tests" / "test_more.py").write_text("def test_added_later():\n    pass\n")
    session._audit_requirements()
    assert len(built) == 2
    assert session._resolve_citations("test_added_later") == ["tests/test_more.py::test_added_later"]


# -- item 23: a changed selection is recorded once per set of files --------------------

def test_item23_the_same_changed_selection_is_recorded_once(tmp_path):
    from quadratus.project import Project
    session, root, _ = _project_session(tmp_path, "", files={
        "app.py": "x = 1\n", "pyproject.toml": '[project]\nname = "x"\n',
        "tests/test_app.py": "def test_a():\n    assert True\n"})
    session._snapshot_original_tests()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n[tool.pytest.ini_options]\naddopts = "-k a"\n')

    def current():
        return Project(root).contents()
    for where in ("t1", "t1", "t2", "DONE"):
        assert session._selection_changed(current(), where) is True
    assert [(r["task"], r["changed"], r["passed"]) for r in session.original_test_runs] == [
        ("t1", ["pyproject.toml"], None)]
    assert sum("test selection in pyproject.toml changed" in f for f in session.open_findings) == 3, \
        "each place that met it still says so; only the run record is not repeated"
    (root / "setup.cfg").write_text("[tool:pytest]\naddopts = -k a\n")
    assert session._selection_changed(current(), "t3") is True
    assert session._selection_changed(current(), "DONE") is True
    assert [r["changed"] for r in session.original_test_runs] == [
        ["pyproject.toml"], ["pyproject.toml", "setup.cfg"]]


# -- independent review of the workstream: what the first pass left open ---------------

def test_item9_a_finding_with_no_failed_ids_on_record_is_not_closed_by_any_passing_gate(tmp_path):
    """Only the recorded sentinel means "the gate as a whole". A finding that
    is not a check finding (no failed ids on record) keeps the old reading,
    the single check "check", so a passing multi-command gate does not close
    it without the evidence its own kind needs."""
    session = Session("g", ArtifactStore(tmp_path / "a"), lambda *a, **k: "",
                      config=SessionConfig(requirements_ledger=True))
    session.findings.append(dict(id="F1", task="t1", requirements=["R1"], kind="product.overflow", target=None,
                                 message="the page overflows", status="open"))
    session._current_resolves = ["F1"]
    repair = SimpleNamespace(task_id="t2")
    session._resolve_check_findings(repair, [_gate(True, py="passed", node="passed")])
    assert session.findings[0]["status"] == "open"
    assert session.findings[0]["last_attempt"] == "t2: its checks did not rerun check with a passing receipt"
    session._resolve_check_findings(repair, [dict(passed=True, command="pytest -q", receipts=[])])
    assert session.findings[0]["status"] == "resolved", "the single-command reading is the one kept"


def test_item10_an_id_too_long_to_be_a_number_never_stops_the_review(tmp_path):
    huge = "R" + "9" * 5000
    listed = f"REQUIREMENTS:\nR1: POST endpoint previews rows\n{huge}: must not write the store\n"
    script = Script([listed + "KIND: backend simple\nBuild the parser.\nCOVERS: R1", "DONE"],
                    review="ADD: the README links to it")
    ledger = _run(tmp_path, script, max_requirement_reopens=0).memory.ledger
    assert ledger.requirements["R2"] == "the README links to it" and huge in ledger.requirements


def test_item12_an_uncovered_requirement_nothing_can_lift_is_not_asked_for_a_task(tmp_path):
    session, _, _ = _project_session(tmp_path, "")
    status = session.memory.ledger.requirement_status
    status["R5"] = "NOT MET: acceptance reported NOT RUN"
    _report(session, "t3", ["R5"], "NOT RUN: the browser scenario - no playwright")
    assert session._requirements_satisfied() is False
    refusal = session._done_refusal
    # Changed 2026-11-12 (merge, exception 1): the report has the
    # restatement route, so a task is asked for and the route is named.
    assert "not covered by any finished task: R5" in refusal and "Name a task for them (with COVERS)" in refusal
    assert "naming 'DISCHARGES: O1'" in refusal and "No task lifts the report" not in refusal
    # Beside one a task can still cover, only that one is asked for.
    status["R1"] = "open"
    assert session._requirements_satisfied() is False
    assert ("Name a task for them (with COVERS), or explain in the task why one cannot be done."
            in session._done_refusal)
    assert "naming 'DISCHARGES: O1'" in session._done_refusal
    # With no standing report the wording is the old one, unchanged.
    plain, _, _ = _project_session(tmp_path / "plain", "")
    plain.memory.ledger.requirement_status["R1"] = "open"
    assert plain._requirements_satisfied() is False
    assert plain._done_refusal.endswith("covered again since). Name a task for them (with COVERS), or explain "
                                        "in the task why one cannot be done.")


def test_item22_the_index_is_read_under_the_root_the_files_are_listed_from(tmp_path):
    """A project reached through a link lists its files under the real
    folder; the index is relative to that, and never raises."""
    session, root, _ = _project_session(tmp_path, "", files=TESTS)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    session.project = link
    assert session._test_functions() == {
        "test_preview_writes_nothing": "tests/test_app.py::test_preview_writes_nothing",
        "test_async_case": "tests/test_app.py::test_async_case"}
